"""Offline smoke test for the Teacher agent: no model calls, no sandbox.

Exercises:
  1. the hintbank MCP tools directly (read_hint_bank / give_hint /
     amend_task_statement / decline) against a TeacherContext,
  2. TeacherService end to end with TeacherAgent.run_turn stubbed out, covering
     a HINT answer, a TASK_AMENDMENT answer (and its artifacts), and the ask
     budget running out.

Run inside the ``zero`` conda env (has claude_agent_sdk):
    conda run -n zero python zero/scripts/teacher_smoke.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp.types as mcp_types  # noqa: E402

from zero.claude_runtime import RunResult  # noqa: E402
from zero.config import Config  # noqa: E402
from zero.protocol.teaching import TeacherAsk, TeachingKind  # noqa: E402
from zero.teacher.service import TeacherService  # noqa: E402
from zero.teacher.tools import TeacherContext, build_hintbank_server  # noqa: E402


def _ok(label: str) -> None:
    print(f"  ok  · {label}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  FAIL· {label}  {detail}")
    raise SystemExit(1)


async def _call_tool(server_cfg: dict, name: str, arguments: dict) -> str:
    """Invoke an in-process SDK MCP tool the same way the real SDK does.

    ``create_sdk_mcp_server`` hands back an ``mcp.server.lowlevel.Server``; the
    only supported entry point is its registered ``CallToolRequest`` handler,
    so we go through that rather than reaching into private tool tables.
    """
    handler = server_cfg["instance"].request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await handler(req)
    block = result.root.content[0]
    return block.text if isinstance(block, mcp_types.TextContent) else str(block)


async def test_hintbank_tools(root: Path) -> None:
    print("== hintbank tools ==")
    cfg = Config(root=root)
    cfg.ensure_dirs()

    events: list[tuple[str, str, dict]] = []
    ctx = TeacherContext(config=cfg, task_id="t1", task_key="demo-task",
                          emit=lambda a, e, d: events.append((a, e, d)))
    server = build_hintbank_server(ctx)

    ctx.set_ask(TeacherAsk(ask_id="ask-1", question="q"))

    body = await _call_tool(server, "read_hint_bank", {})
    if '"ok": false' not in body.lower():
        _fail("read_hint_bank without a bank file should report ok=false", body)
    _ok("read_hint_bank reports missing bank cleanly")

    bank = cfg.ensure_run_dirs("t1") / "teacher" / "hint_bank"
    (bank / "demo.md").write_text("- the tricky bit is X\n", encoding="utf-8")
    body = await _call_tool(server, "read_hint_bank", {})
    if "tricky bit is X" not in body:
        _fail("read_hint_bank should return the file body once present", body)
    _ok("read_hint_bank reads an existing bank")

    await _call_tool(server, "give_hint", {"hint": "check the cutoff radius", "basis": "hint bank"})
    if ctx.answer is None or ctx.answer.kind is not TeachingKind.HINT:
        _fail("give_hint should set a HINT answer")
    _ok("give_hint sets a HINT answer")

    ctx.set_ask(TeacherAsk(ask_id="ask-2", question="q2"))
    await _call_tool(server, "amend_task_statement", {
        "patch": "Output units are kJ/mol, not eV.",
        "reason": "unit omitted",
        "section": "Output",
        "literature_basis": "Paper reports energies in kJ/mol.",
    })
    if ctx.answer is None or ctx.answer.kind is not TeachingKind.TASK_AMENDMENT:
        _fail("amend_task_statement should set a TASK_AMENDMENT answer")
    _ok("amend_task_statement sets a TASK_AMENDMENT answer")

    ctx.set_ask(TeacherAsk(ask_id="ask-3", question="q3"))
    await _call_tool(server, "decline", {"reason": "nothing to add"})
    if ctx.answer is None or ctx.answer.kind is not TeachingKind.NO_HELP:
        _fail("decline should set a NO_HELP answer")
    _ok("decline sets a NO_HELP answer")

    kinds = {e for _, e, _ in events}
    expected = {"hint_bank_read", "hint_given", "task_amended", "declined"}
    if not expected.issubset(kinds):
        _fail("expected trace events missing", f"{expected - kinds}")
    _ok("all expected trace events fired")


class _StubAgent:
    """Stands in for TeacherAgent: pretends to run a turn, doesn't touch a model.

    Each call pulls the next canned answer from ``script`` and applies it
    directly to the context's tool machinery, mirroring what a real turn would
    have done by calling one of the hintbank tools.
    """

    def __init__(self, ctx: TeacherContext, script: list[str]):
        self._ctx = ctx
        self._script = list(script)
        self._server = build_hintbank_server(ctx)

    async def run_turn(self, prompt: str) -> RunResult:
        action = self._script.pop(0)
        if action == "hint":
            await _call_tool(self._server, "give_hint",
                              {"hint": "try a smaller step size", "basis": "reasoning"})
        elif action == "amend":
            await _call_tool(self._server, "amend_task_statement", {
                "patch": "The reference tolerance is +/-5%, not +/-1%.",
                "reason": "original tolerance was a typo",
                "section": "Scoring",
                "literature_basis": "Paper Table 2 lists +/-5%.",
            })
        elif action == "decline":
            await _call_tool(self._server, "decline", {"reason": "no matching hint"})
        elif action == "silent":
            pass  # simulates ending the turn without calling any hintbank tool
        return RunResult(final_text="(stub turn)", num_turns=1)

    async def close(self) -> None:
        return None


async def test_service_flow(root: Path) -> None:
    print("== TeacherService (stubbed agent) ==")
    cfg = Config(root=root)
    cfg.ensure_dirs()

    service = TeacherService(
        cfg, "run-1", task_key="demo-task-2", task_prompt="solve X",
        max_asks=3,
    )
    service._agent = _StubAgent(service._ctx, ["hint", "amend", "decline"])  # noqa: SLF001

    a1 = await service.ask("how do I start?", where_stuck="don't know the method")
    if a1.kind is not TeachingKind.HINT or a1.asks_used != 1 or a1.asks_remaining != 2:
        _fail("first ask should be a HINT with budget 1/3", str(a1))
    _ok("HINT answer + correct budget accounting")

    a2 = await service.ask("the tolerance in the task looks wrong")
    if a2.kind is not TeachingKind.TASK_AMENDMENT or a2.amendment is None:
        _fail("second ask should be a TASK_AMENDMENT", str(a2))
    _ok("TASK_AMENDMENT answer carries an amendment payload")

    a3 = await service.ask("anything else?")
    if a3.kind is not TeachingKind.NO_HELP:
        _fail("third ask should decline", str(a3))
    _ok("NO_HELP (decline) answer")

    a4 = await service.ask("one more?")
    if a4.kind is not TeachingKind.NO_HELP or "budget" not in a4.content:
        _fail("fourth ask should hit the budget wall", str(a4))
    _ok("ask budget enforced after max_asks")

    addendum_path = cfg.run_dir(service._task_id) / "teacher" / "task_addendum.md"
    if not addendum_path.is_file():
        _fail("amendment should be written under runs/<id>/teacher/task_addendum.md")
    text = addendum_path.read_text(encoding="utf-8")
    if "tolerance is +/-5%" not in text:
        _fail("task addendum should contain the amendment patch text", text)
    _ok("run-scoped teacher/task_addendum.md written")

    with tempfile.TemporaryDirectory() as run_dir:
        run_path = Path(run_dir)
        service.write_artifacts(run_path)
        if not (run_path / "teacher" / "asks.jsonl").is_file():
            _fail("write_artifacts should drop teacher/asks.jsonl into the run dir")
        if not (run_path / "teacher" / "task_addendum.md").is_file():
            _fail("write_artifacts should drop teacher/task_addendum.md into the run dir")
        _ok("run-scoped artifacts written")

    # The 4th call hit the budget wall and short-circuits before _run_turn, so
    # it is never recorded (by design: a budget-exhausted call didn't ask
    # anything of the agent).
    stats = service.stats()
    if stats["asks_used"] != 3 or stats["amendments"] != 1:
        _fail("stats() should reflect the 3 recorded asks and 1 amendment", str(stats))
    _ok("stats() summary correct")


async def test_silent_turn_is_no_help(root: Path) -> None:
    print("== silent turn safety net ==")
    cfg = Config(root=root)
    cfg.ensure_dirs()
    service = TeacherService(cfg, "run-2", task_key="demo-task-3", max_asks=1)
    service._agent = _StubAgent(service._ctx, ["silent"])  # noqa: SLF001
    answer = await service.ask("hello?")
    if answer.kind is not TeachingKind.NO_HELP:
        _fail("a turn that calls no hintbank tool must fall back to NO_HELP", str(answer))
    _ok("agent ending its turn without an answer degrades to NO_HELP")


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zero-teacher-smoke-"))
    try:
        await test_hintbank_tools(tmp / "a")
        await test_service_flow(tmp / "b")
        await test_silent_turn_is_no_help(tmp / "c")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nall teacher smoke checks passed")


if __name__ == "__main__":
    asyncio.run(main())
