"""The Teacher's own MCP tool surface (``hintbank``).

Preflight / mid-run / completion review: read hint bank, then end with HINT /
TASK_AMENDMENT / GRADER_AMENDMENT / BOTH_AMENDMENT / NO_HELP / NO_CHANGE.
Amendments write the live task package and must pass package lint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from claude_agent_sdk import create_sdk_mcp_server, tool

from zero.config import Config
from zero.protocol.teaching import (
    CompletionReview,
    GraderAmendment,
    TaskAmendment,
    TeacherAnswer,
    TeacherAsk,
    TeachingKind,
)

EmitFn = Callable[[str, str, dict], None]

_LITERATURE_RULE = (
    "All amendments must restore fidelity to the source literature so a future "
    "agent can reproduce the original paper from the task statement. Improve "
    "the package, not this Researcher's score. Do not loosen graders to raise "
    "the grade; do not invent parameters absent from the paper/hints without "
    "flagging uncertainty."
)

_COHERENCE_RULE = (
    " Keep instruction ↔ tests coherent: no statement constraints the grader "
    "does not check; no grader numeric gates undeclared in the statement. "
    "If the verifiable surface changes, prefer amend_task_and_grader "
    "(BOTH_AMENDMENT)."
)


def _text(obj: Any) -> dict:
    if isinstance(obj, str):
        payload = obj
    else:
        payload = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    return {"content": [{"type": "text", "text": payload}]}


@dataclass
class TeacherContext:
    """Mutable per-ask / review state the hintbank tools mutate."""

    config: Config
    task_id: str
    task_key: str
    emit: EmitFn

    ask: Optional[TeacherAsk] = None
    answer: Optional[TeacherAnswer] = None
    review: Optional[CompletionReview] = None
    mode: str = "ask"  # ask | review
    hint_bank_read: bool = False
    grade_payload: dict[str, Any] = field(default_factory=dict)

    def set_ask(self, ask: TeacherAsk) -> None:
        self.mode = "ask"
        self.ask = ask
        self.answer = None
        self.review = None

    def set_review(self, grade_payload: dict[str, Any]) -> None:
        self.mode = "review"
        self.ask = None
        self.answer = None
        self.review = None
        self.grade_payload = grade_payload

    @property
    def ask_id(self) -> str:
        return self.ask.ask_id if self.ask is not None else "completion-review"

    def hint_bank_dir(self) -> Path:
        return self.config.run_dir(self.task_id) / "teacher" / "hint_bank"

    def hint_bank_sources(self) -> list[Path]:
        bank = self.hint_bank_dir()
        if not bank.is_dir():
            return []
        return sorted(p for p in bank.glob("*.md") if p.is_file())


def build_hintbank_server(ctx: TeacherContext):
    """In-process MCP server bound to a shared TeacherContext."""

    @tool(
        "read_hint_bank",
        "读取本题的人工 hint / 论文材料（Researcher 看不到）。可能不存在。",
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
        "read_grade_result",
        "结题审阅：读取编排层跑出的 Harbor 打分结果（score / breakdown / status）。",
        {},
    )
    async def read_grade_result(args):
        if not ctx.grade_payload:
            return _text({"ok": False, "error": "no grade payload in this turn"})
        return _text({"ok": True, "grade": ctx.grade_payload})

    @tool(
        "give_hint",
        "以操作提示回答本次提问，并结束本轮；返回动作标识 kind: HINT。"
        "题面科学内容没问题时用。给能解开当前障碍的最小提示，优先方法而不是结论数值。",
        {"hint": str, "basis": str},
    )
    async def give_hint(args):
        if ctx.mode != "ask":
            return _text({"ok": False, "error": "give_hint only valid during ask mode"})
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
        "题面科学内容缺陷时订正并结束本轮（kind: TASK_AMENDMENT）。"
        + _LITERATURE_RULE
        + " 提供 literature_basis：说明如何对齐原论文。"
        + " 结题审阅时：若改动约束/阈值/可验证表面，应改用 amend_task_and_grader，"
        "保证题面与 grader 互恰；本工具仅在 grader 已与订正后题面一致时使用。",
        {"patch": str, "reason": str, "section": str, "literature_basis": str},
    )
    async def amend_task_statement(args):
        patch = (args.get("patch") or "").strip()
        if not patch:
            return _text({"ok": False, "error": "patch is empty"})
        amendment = TaskAmendment(
            patch=patch,
            reason=(args.get("reason") or "").strip(),
            section=(args.get("section") or "").strip() or None,
            literature_basis=(args.get("literature_basis") or "").strip(),
        )
        if ctx.mode == "review":
            ctx.review = CompletionReview(
                kind=TeachingKind.TASK_AMENDMENT,
                summary=(args.get("reason") or "task statement amended")[:500],
                task_amendment=amendment,
                literature_fidelity_notes=amendment.literature_basis,
            )
            warn = ""
            low = (patch + " " + amendment.reason).lower()
            if any(
                k in low
                for k in (
                    "constraint", "grid", "threshold", "tolerance", "至少",
                    "a_max", "i=", "checker", "grader", "≥", "<=", ">=",
                )
            ):
                warn = (
                    " Note: this looks like a verifiable-surface change; "
                    "prefer amend_task_and_grader next time so instruction "
                    "and tests stay coherent."
                )
            ctx.emit("teacher", "task_amended", {
                "ask_id": ctx.ask_id,
                "task_key": ctx.task_key,
                "section": amendment.section,
                "reason": amendment.reason[:300],
                "literature_basis": amendment.literature_basis[:300],
                "chars": len(patch),
                "mode": ctx.mode,
            })
            return _text({
                "ok": True,
                "kind": TeachingKind.TASK_AMENDMENT.value,
                "coherence_hint": warn.strip() or None,
            })
        ctx.answer = TeacherAnswer(
            kind=TeachingKind.TASK_AMENDMENT, ask_id=ctx.ask_id,
            content=patch, amendment=amendment,
        )
        ctx.emit("teacher", "task_amended", {
            "ask_id": ctx.ask_id,
            "task_key": ctx.task_key,
            "section": amendment.section,
            "reason": amendment.reason[:300],
            "literature_basis": amendment.literature_basis[:300],
            "chars": len(patch),
            "mode": ctx.mode,
        })
        return _text({"ok": True, "kind": TeachingKind.TASK_AMENDMENT.value})

    @tool(
        "amend_grader",
        "打分器违背原论文或契约、或存在题面未声明的硬阈值时订正"
        "（kind: GRADER_AMENDMENT）。解题中与结题审阅均可使用；写入 live task package，"
        "须通过 lint。"
        + _LITERATURE_RULE
        + _COHERENCE_RULE
        + " `target` 为 tests/ 下文件名（优先 verification_plan.json / checker.py / "
        "grading_spec.json / test.sh）；"
        " `patch` 必须是该文件的**完整最终内容**（不是 unified diff）。"
        " 必须给出 literature_basis。不要向 Researcher 泄露参考数值。",
        {"patch": str, "reason": str, "target": str, "literature_basis": str, "summary": str},
    )
    async def amend_grader(args):
        patch = (args.get("patch") or "").strip()
        if not patch:
            return _text({"ok": False, "error": "patch is empty"})
        g = GraderAmendment(
            patch=patch,
            reason=(args.get("reason") or "").strip(),
            target=(args.get("target") or "verification_plan.json").strip() or "verification_plan.json",
            literature_basis=(args.get("literature_basis") or "").strip(),
        )
        if ctx.mode == "review":
            ctx.review = CompletionReview(
                kind=TeachingKind.GRADER_AMENDMENT,
                summary=(args.get("summary") or args.get("reason") or "grader amended")[:500],
                grader_amendment=g,
                literature_fidelity_notes=g.literature_basis,
            )
        else:
            ctx.answer = TeacherAnswer(
                kind=TeachingKind.GRADER_AMENDMENT,
                ask_id=ctx.ask_id,
                content=(args.get("summary") or args.get("reason") or "grader amended")[:500],
                grader_amendment=g,
            )
        ctx.emit("teacher", "grader_amended", {
            "target": g.target,
            "reason": g.reason[:300],
            "literature_basis": g.literature_basis[:300],
            "mode": ctx.mode,
        })
        return _text({"ok": True, "kind": TeachingKind.GRADER_AMENDMENT.value})

    @tool(
        "amend_task_and_grader",
        "题面与打分器需一起改时使用（kind: BOTH_AMENDMENT）。"
        "改约束/阈值/可验证表面时优先本工具。解题中与结题审阅均可使用。"
        + _LITERATURE_RULE
        + " grader_target 为 tests/ 文件名；grader_patch 为该文件完整最终内容。"
        "不要向 Researcher 泄露参考数值。",
        {
            "task_patch": str, "task_reason": str, "task_section": str, "task_literature_basis": str,
            "grader_patch": str, "grader_reason": str, "grader_target": str, "grader_literature_basis": str,
            "summary": str,
        },
    )
    async def amend_task_and_grader(args):
        task_patch = (args.get("task_patch") or "").strip()
        grader_patch = (args.get("grader_patch") or "").strip()
        if not task_patch or not grader_patch:
            return _text({"ok": False, "error": "task_patch and grader_patch required"})
        t = TaskAmendment(
            patch=task_patch,
            reason=(args.get("task_reason") or "").strip(),
            section=(args.get("task_section") or "").strip() or None,
            literature_basis=(args.get("task_literature_basis") or "").strip(),
        )
        g = GraderAmendment(
            patch=grader_patch,
            reason=(args.get("grader_reason") or "").strip(),
            target=(args.get("grader_target") or "verification_plan.json").strip()
            or "verification_plan.json",
            literature_basis=(args.get("grader_literature_basis") or "").strip(),
        )
        notes = "\n".join(x for x in [t.literature_basis, g.literature_basis] if x)
        summary = (args.get("summary") or "task and grader amended")[:500]
        if ctx.mode == "review":
            ctx.review = CompletionReview(
                kind=TeachingKind.BOTH_AMENDMENT,
                summary=summary,
                task_amendment=t,
                grader_amendment=g,
                literature_fidelity_notes=notes,
            )
        else:
            ctx.answer = TeacherAnswer(
                kind=TeachingKind.BOTH_AMENDMENT,
                ask_id=ctx.ask_id,
                content=summary,
                amendment=t,
                grader_amendment=g,
            )
        ctx.emit("teacher", "both_amended", {"summary": summary[:300], "mode": ctx.mode})
        return _text({"ok": True, "kind": TeachingKind.BOTH_AMENDMENT.value})

    @tool(
        "declare_no_change",
        "Preflight / 结题审阅：题面与打分器均忠实于原论文且互恰，无需改包"
        "（kind: NO_CHANGE）。不为照顾 Researcher 分数而改包。",
        {"summary": str, "literature_fidelity_notes": str},
    )
    async def declare_no_change(args):
        if ctx.mode != "review":
            return _text({"ok": False, "error": "only valid during preflight/completion review"})
        ctx.review = CompletionReview(
            kind=TeachingKind.NO_CHANGE,
            summary=(args.get("summary") or "no package changes needed")[:500],
            literature_fidelity_notes=(args.get("literature_fidelity_notes") or "").strip(),
        )
        ctx.emit("teacher", "review_no_change", {"summary": ctx.review.summary[:300]})
        return _text({"ok": True, "kind": TeachingKind.NO_CHANGE.value})

    @tool(
        "decline",
        "结束本轮并返回动作标识 kind: NO_HELP。"
        "环境/路径/镜像/装包问题应找 Labwright。拒答是合法答案。",
        {"reason": str},
    )
    async def decline(args):
        if ctx.mode == "review":
            return _text({
                "ok": False,
                "error": "during completion review use declare_no_change / amend_* tools",
            })
        reason = (args.get("reason") or "nothing useful to add").strip()
        ctx.answer = TeacherAnswer(
            kind=TeachingKind.NO_HELP, ask_id=ctx.ask_id, content=reason,
        )
        ctx.emit("teacher", "declined", {"ask_id": ctx.ask_id, "reason": reason[:300]})
        return _text({"ok": True, "kind": TeachingKind.NO_HELP.value})

    return create_sdk_mcp_server(
        "hintbank",
        "1.1.0",
        [
            read_hint_bank,
            read_grade_result,
            give_hint,
            amend_task_statement,
            amend_grader,
            amend_task_and_grader,
            declare_no_change,
            decline,
        ],
    )
