"""The Teacher's own MCP tool surface (``hintbank``).

These are the only tools the Teacher agent has: read the privileged hint bank,
then end the turn with exactly one answer. It gets no file, shell, or sandbox
access — the Researcher's workspace stays invisible to it, the same way
Labwright never authors experiment code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from claude_agent_sdk import create_sdk_mcp_server, tool

from zero.config import Config
from zero.protocol.teaching import TaskAmendment, TeacherAnswer, TeacherAsk, TeachingKind

EmitFn = Callable[[str, str, dict], None]


def _text(obj: Any) -> dict:
    if isinstance(obj, str):
        payload = obj
    else:
        payload = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    return {"content": [{"type": "text", "text": payload}]}


@dataclass
class TeacherContext:
    """Mutable per-ask state the hintbank tools mutate."""

    config: Config
    task_id: str
    task_key: str
    emit: EmitFn

    ask: Optional[TeacherAsk] = None
    answer: Optional[TeacherAnswer] = None
    hint_bank_read: bool = False

    def set_ask(self, ask: TeacherAsk) -> None:
        self.ask = ask
        self.answer = None

    @property
    def ask_id(self) -> str:
        return self.ask.ask_id if self.ask is not None else ""

    def hint_bank_dir(self) -> Path:
        """This run's hint bank: ``runs/<task_id>/teacher/hint_bank/``."""
        return self.config.run_dir(self.task_id) / "teacher" / "hint_bank"

    def hint_bank_sources(self) -> list[Path]:
        """``.md`` files under this run's ``teacher/hint_bank/``, in name order."""
        bank = self.hint_bank_dir()
        if not bank.is_dir():
            return []
        return sorted(p for p in bank.glob("*.md") if p.is_file())


def build_hintbank_server(ctx: TeacherContext):
    """In-process MCP server bound to a shared TeacherContext."""

    @tool(
        "read_hint_bank",
        "读取本题的人工 hint（Researcher 看不到）。可能不存在——不存在时只能基于题面给方法级提示，不要编造特权信息。",
        {},
    )
    async def read_hint_bank(args):
        sources = ctx.hint_bank_sources()
        ctx.hint_bank_read = True
        ctx.emit("teacher", "hint_bank_read", {
            "ask_id": ctx.ask_id,
            "task_key": ctx.task_key,
            "sources": [str(p) for p in sources],
        })
        if not sources:
            return _text({
                "ok": False,
                "task_key": ctx.task_key,
                "reason": "no hint bank for this run",
                "searched": str(ctx.hint_bank_dir()),
            })
        chunks = []
        for path in sources:
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                chunks.append(f"# {path.name}\n(unreadable: {exc})")
                continue
            chunks.append(f"# {path.name}\n{body}")
        return _text({
            "ok": True,
            "task_key": ctx.task_key,
            "sources": [str(p) for p in sources],
            "hint_bank": "\n\n".join(chunks),
        })

    @tool(
        "give_hint",
        "以操作提示回答本次提问，并结束本轮；返回动作标识 kind: HINT。"
        "题面科学内容没问题时用。给能解开当前障碍的最小提示，优先方法而不是结论数值。",
        {"hint": str, "basis": str},
    )
    async def give_hint(args):
        hint = (args.get("hint") or "").strip()
        if not hint:
            return _text({"ok": False, "error": "hint is empty"})
        ctx.answer = TeacherAnswer(
            kind=TeachingKind.HINT, ask_id=ctx.ask_id, content=hint,
        )
        ctx.emit("teacher", "hint_given", {
            "ask_id": ctx.ask_id,
            "basis": (args.get("basis") or "")[:200],
            "used_hint_bank": ctx.hint_bank_read,
            "chars": len(hint),
        })
        return _text({"ok": True, "kind": TeachingKind.HINT.value})

    @tool(
        "amend_task_statement",
        "当问题出在题面的科学内容本身（缺量/缺单位/缺容差/缺输出契约/科学表述自相矛盾）时，"
        "用订正后的题面文本回答，并结束本轮；返回动作标识 kind: TASK_AMENDMENT。"
        "patch 要能被没看过这段对话的人独立读懂。"
        "不要用于路径、镜像、挂载、装包等环境/部署问题——那些应 decline（kind: NO_HELP），交给 Labwright。",
        {"patch": str, "reason": str, "section": str},
    )
    async def amend_task_statement(args):
        patch = (args.get("patch") or "").strip()
        if not patch:
            return _text({"ok": False, "error": "patch is empty"})
        amendment = TaskAmendment(
            patch=patch,
            reason=(args.get("reason") or "").strip(),
            section=(args.get("section") or "").strip() or None,
        )
        ctx.answer = TeacherAnswer(
            kind=TeachingKind.TASK_AMENDMENT, ask_id=ctx.ask_id,
            content=patch, amendment=amendment,
        )
        ctx.emit("teacher", "task_amended", {
            "ask_id": ctx.ask_id,
            "task_key": ctx.task_key,
            "section": amendment.section,
            "reason": amendment.reason[:300],
            "chars": len(patch),
        })
        return _text({"ok": True, "kind": TeachingKind.TASK_AMENDMENT.value})

    @tool(
        "decline",
        "结束本轮并返回动作标识 kind: NO_HELP。"
        "本次没有可给的东西（hint 里没有、科学题面也没问题），或问题属于环境/路径/镜像/"
        "装包（应找 Labwright）时用它。拒答是合法答案，不要为了显得有用而编造 HINT。",
        {"reason": str},
    )
    async def decline(args):
        reason = (args.get("reason") or "nothing useful to add").strip()
        ctx.answer = TeacherAnswer(
            kind=TeachingKind.NO_HELP, ask_id=ctx.ask_id, content=reason,
        )
        ctx.emit("teacher", "declined", {"ask_id": ctx.ask_id, "reason": reason[:300]})
        return _text({"ok": True, "kind": TeachingKind.NO_HELP.value})

    return create_sdk_mcp_server(
        "hintbank", "1.0.0",
        [read_hint_bank, give_hint, amend_task_statement, decline],
    )
