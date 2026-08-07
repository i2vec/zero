"""TeacherService: blocking handoff + live package evolution.

Same execution model as Labwright — ``ask()`` / ``preflight()`` / ``review_completion()``
drive one Teacher turn under a lock.

Live package (``runs/<id>/task_package/``) is the single source of truth for the
statement and grader. Amendments rewrite that tree, then lint (+ optional verify).
Researcher receives only a safe ``package_delta`` (no graded answers).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

from zero.claude_runtime import TurnEvent
from zero.config import Config
from zero.protocol.grading import GradeResult
from zero.protocol.teaching import (
    CompletionReview,
    TeacherAnswer,
    TeacherAsk,
    TeachingKind,
)
from zero.teacher.agent import TeacherAgent
from zero.teacher.live_package import LivePackageManager
from zero.teacher.optimize import materialize_optimized_task
from zero.teacher.package_lint import lint_task_package
from zero.teacher.tools import TeacherContext

EmitFn = Callable[[str, str, dict], None]


class TeacherService:
    def __init__(
        self,
        config: Config,
        task_id: str,
        *,
        task_key: str = "",
        task_prompt: str = "",
        emit: Optional[EmitFn] = None,
        on_agent_event: Optional[Callable[[TurnEvent], None]] = None,
        max_asks: Optional[int] = None,
        task_package: Optional[Path] = None,
    ):
        self._config = config
        self._task_id = task_id
        self._task_key = task_key or task_id
        self._task_prompt = task_prompt
        self._source_package = Path(task_package) if task_package else None
        self._emit = emit or (lambda a, e, d: None)
        self._max_asks = config.teacher_max_asks if max_asks is None else max_asks
        self._run_dir = config.ensure_run_dirs(task_id)
        self._teacher_dir = self._run_dir / "teacher"

        self._live = LivePackageManager(
            self._run_dir,
            max_revisions=getattr(config, "package_max_revisions", 12),
        )
        self._live.seed(self._source_package, fallback_instruction=task_prompt)
        # Prefer live instruction as the working prompt.
        live_text = self._live.instruction_text().strip()
        if live_text:
            self._task_prompt = live_text

        self._ctx = TeacherContext(
            config=config, task_id=task_id, task_key=self._task_key, emit=self._emit,
        )
        self._agent = TeacherAgent(
            config, task_id=task_id, ctx=self._ctx, on_event=on_agent_event,
        )

        self._counter = itertools.count(1)
        self._asks: list[dict] = []
        self._amendments: list[dict] = []
        self._introduced = False
        self._lock = asyncio.Lock()
        self._preflight_done = False

    @property
    def live_package(self) -> Path:
        return self._live.path

    def instruction_text(self) -> str:
        return self._live.instruction_text() or self._task_prompt

    # ---- MCP-facing interface (blocking) -------------------------------- #
    async def ask(self, question: str, what_i_tried: str = "", where_stuck: str = "") -> TeacherAnswer:
        question = (question or "").strip()
        used = len(self._asks)
        if not question:
            return self._budget_view(TeacherAnswer(
                kind=TeachingKind.NO_HELP, ask_id="", content="empty question",
            ))
        if used >= self._max_asks:
            self._emit("teacher", "ask_budget_exhausted", {
                "asks_used": used, "max_asks": self._max_asks,
            })
            return self._budget_view(TeacherAnswer(
                kind=TeachingKind.NO_HELP, ask_id="",
                content=(
                    f"ask budget exhausted ({used}/{self._max_asks}); "
                    "solve the rest on your own"
                ),
            ))

        ask = TeacherAsk(
            ask_id=f"ask-{next(self._counter)}",
            question=question,
            what_i_tried=(what_i_tried or "").strip(),
            where_stuck=(where_stuck or "").strip(),
        )
        answer = await self._run_turn(ask)
        answer = self._apply_answer_to_live_package(ask, answer)
        self._record(ask, answer)
        return self._budget_view(answer)

    async def preflight(self) -> dict:
        """Teacher reviews the package before Researcher starts; applies lint+edits."""
        async with self._lock:
            self._preflight_done = True
            lint = lint_task_package(self._live.path)
            self._emit("teacher", "preflight_started", {
                "lint_ok": lint.ok,
                "issues": len(lint.issues),
            })
            # Always surface lint errors to Teacher; they may amend.
            self._ctx.set_review({
                "status": "preflight",
                "lint": lint.to_dict(),
                "note": "preflight — improve package before Researcher starts",
            })
            prompt = self._preflight_prompt(lint.to_dict())
            try:
                result = await self._agent.run_turn(prompt)
            except Exception as exc:  # noqa: BLE001
                self._emit("teacher", "preflight_error", {"error": str(exc)[:500]})
                return {"ok": lint.ok, "error": str(exc)[:500], "lint": lint.to_dict()}

            review = self._ctx.review
            apply_result: dict = {"ok": True, "revision": self._live.revision}
            if review is not None and review.kind is not TeachingKind.NO_CHANGE:
                apply_result = self._apply_review_to_live(review)
                if review.task_amendment is not None:
                    self._amendments.append({
                        "ts": time.time(),
                        "ask_id": "preflight",
                        "question": "preflight",
                        "kind": review.kind.value,
                        "content": review.task_amendment.patch,
                        "amendment": review.task_amendment.model_dump(exclude_none=True),
                    })
                    self._task_prompt = self.instruction_text()
            elif not lint.ok:
                # Lint failed and Teacher did not amend — still allow run but warn.
                apply_result = {
                    "ok": False,
                    "error": "preflight lint failed and teacher did not amend",
                    "lint": lint.to_dict(),
                    "revision": self._live.revision,
                }
            else:
                # Refresh lint after no-change.
                apply_result = {
                    "ok": True,
                    "revision": self._live.revision,
                    "lint": self._live.lint().to_dict(),
                    "teacher_output": (result.final_text or "")[:300],
                }

            self._emit("teacher", "preflight_finished", {
                "ok": apply_result.get("ok"),
                "revision": self._live.revision,
                "kind": review.kind.value if review else "NO_CHANGE",
            })
            # Drop pending delta so Researcher is not notified of preflight noise.
            self._live.consume_researcher_delta()
            return apply_result

    # ---- run artifacts --------------------------------------------------- #
    def stats(self) -> dict:
        kinds: dict[str, int] = {}
        for rec in self._asks:
            kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
        return {
            "task_key": self._task_key,
            "asks_used": len(self._asks),
            "max_asks": self._max_asks,
            "by_kind": kinds,
            "amendments": len(self._amendments),
            "package_revision": self._live.revision,
            "live_package": str(self._live.path),
            "preflight_done": self._preflight_done,
        }

    def resolved_task_markdown(self) -> str:
        """Current live instruction (single source of truth)."""
        body = self.instruction_text().strip()
        return (
            f"# Resolved task statement: {self._task_key}\n\n"
            f"> Live package revision r{self._live.revision:03d}. "
            f"This file is the authoritative statement for the run.\n\n"
            f"{body}\n"
        )

    def write_artifacts(self, run_dir: Optional[Path] = None) -> None:
        try:
            base = Path(run_dir) if run_dir is not None else self._run_dir
            teacher_dir = base / "teacher"
            teacher_dir.mkdir(parents=True, exist_ok=True)
            with (teacher_dir / "asks.jsonl").open("w", encoding="utf-8") as f:
                for rec in self._asks:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if self._amendments:
                (teacher_dir / "task_addendum.md").write_text(
                    self._render_addendum(), encoding="utf-8",
                )
        except OSError:
            pass

    async def review_completion(
        self,
        *,
        grade: GradeResult,
        resolved_task: str,
        task_package: Optional[Path] = None,
    ) -> CompletionReview:
        payload = grade.model_dump(mode="json")
        async with self._lock:
            self._ctx.set_review(payload)
            self._emit("teacher", "completion_review_started", {
                "grade_status": grade.status.value,
                "score": grade.score,
            })
            prompt = self._review_prompt(grade, resolved_task, self._live.path)
            try:
                result = await self._agent.run_turn(prompt)
            except Exception as exc:  # noqa: BLE001
                self._emit("teacher", "completion_review_error", {"error": str(exc)[:500]})
                return CompletionReview(
                    kind=TeachingKind.NO_CHANGE,
                    summary=f"teacher review unavailable: {exc}",
                    literature_fidelity_notes="review skipped due to teacher error",
                )
            review = self._ctx.review
            if review is None:
                review = CompletionReview(
                    kind=TeachingKind.NO_CHANGE,
                    summary=(
                        "teacher ended review without a terminal tool; "
                        f"last output: {result.final_text[:300]}"
                    ),
                )
            if review.kind is not TeachingKind.NO_CHANGE:
                apply = self._apply_review_to_live(review)
                self._emit("teacher", "completion_apply", apply)
                if review.task_amendment is not None:
                    self._amendments.append({
                        "ts": time.time(),
                        "ask_id": "completion-review",
                        "question": "completion review",
                        "kind": review.kind.value,
                        "content": review.task_amendment.patch,
                        "amendment": review.task_amendment.model_dump(exclude_none=True),
                    })
                    self._task_prompt = self.instruction_text()

            finalized = self._live.finalize()
            # Also keep legacy materialize path for OPTIMIZATION.md richness when needed.
            try:
                materialize_optimized_task(
                    run_dir=self._run_dir,
                    task_package=self._live.path,
                    task_prompt=self.instruction_text(),
                    task_key=self._task_key,
                    review=review,
                    grade=grade,
                    mid_run_amendments=self._amendments,
                )
            except Exception as exc:  # noqa: BLE001
                self._emit("teacher", "legacy_materialize_failed", {"error": str(exc)[:300]})

            review_path = self._teacher_dir / "completion_review.json"
            try:
                review_path.write_text(
                    review.model_dump_json(indent=2) + "\n", encoding="utf-8",
                )
            except OSError:
                pass
            self._emit("teacher", "completion_review_finished", {
                "kind": review.kind.value,
                "optimized_task": str(self._run_dir / "optimized_task"),
                "finalized_task": str(finalized),
                "package_revision": self._live.revision,
            })
            return review

    async def close(self) -> None:
        await self._agent.close()

    def seed_hint_bank(self, src: Optional[Path]) -> list[str]:
        if src is None:
            return []
        src = Path(src)
        dest = self._teacher_dir / "hint_bank"
        dest.mkdir(parents=True, exist_ok=True)
        to_copy: list[Path] = []
        if src.is_file():
            to_copy = [src]
        elif src.is_dir():
            to_copy = sorted(p for p in src.glob("*.md") if p.is_file())
        else:
            raise FileNotFoundError(f"hints path not found: {src}")
        copied: list[str] = []
        for path in to_copy:
            target = dest / path.name
            if target.exists():
                continue
            try:
                shutil.copy2(path, target)
                copied.append(str(target))
            except OSError:
                pass
        if copied:
            self._emit("teacher", "hint_bank_seeded", {
                "source": str(src), "files": copied,
            })
        return copied

    # ---- internals ------------------------------------------------------- #
    def _apply_answer_to_live_package(self, ask: TeacherAsk, answer: TeacherAnswer) -> TeacherAnswer:
        try:
            if answer.kind is TeachingKind.TASK_AMENDMENT and answer.amendment is not None:
                result = self._live.apply_task_amendment(answer.amendment)
            elif answer.kind is TeachingKind.GRADER_AMENDMENT and answer.grader_amendment is not None:
                result = self._live.apply_grader_amendment(answer.grader_amendment)
            elif (
                answer.kind is TeachingKind.BOTH_AMENDMENT
                and answer.amendment is not None
                and answer.grader_amendment is not None
            ):
                result = self._live.apply_both(answer.amendment, answer.grader_amendment)
            else:
                answer.package_revision = self._live.revision
                return answer
        except Exception as exc:  # noqa: BLE001
            answer.content = (
                f"{answer.content}\n\n(package apply failed: {exc})"
            ).strip()
            answer.package_revision = self._live.revision
            return answer

        if not result.get("ok"):
            # Surface failure to Researcher without leaking grader internals deeply.
            answer.content = (
                f"{answer.content}\n\n"
                f"(live package reject: {result.get('error', 'lint/verify failed')})"
            ).strip()
            answer.package_revision = self._live.revision
            self._emit("teacher", "package_apply_rejected", result)
            return answer

        self._task_prompt = self.instruction_text()
        answer.package_revision = int(result.get("revision") or self._live.revision)
        answer.package_delta = str(result.get("researcher_delta") or "")
        self._emit("teacher", "package_applied", {
            "ask_id": ask.ask_id,
            "kind": answer.kind.value,
            "revision": answer.package_revision,
        })
        return answer

    def _apply_review_to_live(self, review: CompletionReview) -> dict:
        try:
            if review.kind is TeachingKind.TASK_AMENDMENT and review.task_amendment:
                return self._live.apply_task_amendment(review.task_amendment, kind=review.kind.value)
            if review.kind is TeachingKind.GRADER_AMENDMENT and review.grader_amendment:
                return self._live.apply_grader_amendment(review.grader_amendment, kind=review.kind.value)
            if (
                review.kind is TeachingKind.BOTH_AMENDMENT
                and review.task_amendment
                and review.grader_amendment
            ):
                return self._live.apply_both(review.task_amendment, review.grader_amendment)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:500], "revision": self._live.revision}
        return {"ok": True, "revision": self._live.revision}

    async def _run_turn(self, ask: TeacherAsk) -> TeacherAnswer:
        async with self._lock:
            self._ctx.set_ask(ask)
            self._emit("teacher", "ask_received", {
                "ask_id": ask.ask_id,
                "question": ask.question,
                "where_stuck": ask.where_stuck,
            })
            try:
                result = await self._agent.run_turn(self._prompt(ask))
            except Exception as exc:  # noqa: BLE001
                self._emit("teacher", "agent_error", {
                    "ask_id": ask.ask_id, "error": str(exc)[:500],
                })
                return TeacherAnswer(
                    kind=TeachingKind.NO_HELP, ask_id=ask.ask_id,
                    content=f"teacher unavailable: {exc}",
                )

            answer = self._ctx.answer
            if answer is None:
                answer = TeacherAnswer(
                    kind=TeachingKind.NO_HELP, ask_id=ask.ask_id,
                    content=(
                        "teacher ended its turn without an answer; last output: "
                        f"{result.final_text[:300]}"
                    ),
                )
            self._emit("teacher", "answered", {
                "ask_id": ask.ask_id,
                "kind": answer.kind.value,
                "num_turns": result.num_turns,
                "is_error": result.is_error,
            })
            return answer

    def _prompt(self, ask: TeacherAsk) -> str:
        parts = []
        if not self._introduced:
            self._introduced = True
            parts.append(
                "This is the **live** task statement (revision "
                f"r{self._live.revision:03d}; later asks will not always repeat it):\n"
                f"```\n{self.instruction_text().strip()[:14000]}\n```\n"
                f"Live package path: `{self._live.path}` — you may Read tests/.\n"
            )
        remaining = max(0, self._max_asks - len(self._asks) - 1)
        parts.append(
            f"Ask {ask.ask_id} (ask {len(self._asks) + 1} of {self._max_asks}; "
            f"{remaining} left after this one).\n\n"
            f"Question:\n{ask.question}\n"
        )
        if ask.what_i_tried:
            parts.append(f"\nAlready tried:\n{ask.what_i_tried}\n")
        if ask.where_stuck:
            parts.append(f"\nWhere it is stuck:\n{ask.where_stuck}\n")
        parts.append(
            "\nDecide per skill `teaching`; one terminal tool. "
            "Amend only for literature fidelity + coherence — not this score; "
            "do not leak graded answers.\n"
        )
        return "".join(parts)

    def _preflight_prompt(self, lint: dict) -> str:
        return (
            "PREFLIGHT (before Researcher). Follow skill `teaching`: fix clear "
            "package defects or declare_no_change.\n\n"
            f"Lint:\n```\n{json.dumps(lint, ensure_ascii=False, indent=2)[:8000]}\n```\n"
            f"Live package: `{self._live.path}`\n"
            "read_hint_bank; Read tests/ as needed; one terminal amend tool.\n"
        )

    def _review_prompt(
        self,
        grade: GradeResult,
        resolved_task: str,
        task_package: Optional[Path],
    ) -> str:
        tests_hint = ""
        if task_package is not None:
            tests_hint = (
                f"\nLive package: `{task_package}` "
                f"(tests `{Path(task_package) / 'tests'}`).\n"
            )
        return (
            "COMPLETION REVIEW. Follow skill `teaching` — improve the package, "
            "not this Researcher's score.\n\n"
            f"Grade status={grade.status.value} score={grade.score} "
            f"mode={grade.mode} error={grade.error!r}\n"
            "read_grade_result + read_hint_bank.\n"
            f"{tests_hint}\n"
            "Statement:\n"
            f"```\n{resolved_task.strip()[:12000]}\n```\n\n"
            "One terminal: declare_no_change / amend_*.\n"
        )

    def _record(self, ask: TeacherAsk, answer: TeacherAnswer) -> None:
        rec = {
            "ts": time.time(),
            "ask_id": ask.ask_id,
            "question": ask.question,
            "what_i_tried": ask.what_i_tried,
            "where_stuck": ask.where_stuck,
            "kind": answer.kind.value,
            "content": answer.content,
            "package_revision": answer.package_revision,
            "package_delta": answer.package_delta,
        }
        if answer.amendment is not None:
            rec["amendment"] = answer.amendment.model_dump(exclude_none=True)
            self._amendments.append(rec)
            self._append_task_addendum(ask, answer)
        if answer.grader_amendment is not None:
            rec["grader_amendment"] = answer.grader_amendment.model_dump(exclude_none=True)
        self._asks.append(rec)

    def _append_task_addendum(self, ask: TeacherAsk, answer: TeacherAnswer) -> None:
        amendment = answer.amendment
        if amendment is None:
            return
        path = self._teacher_dir / "task_addendum.md"
        block = self._render_amendment(ask, answer)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not path.is_file() or path.stat().st_size == 0
            with path.open("a", encoding="utf-8") as f:
                if new_file:
                    f.write(f"# 题面订正：{self._task_key}\n")
                f.write(block)
        except OSError:
            return
        self._emit("teacher", "task_addendum_written", {
            "path": str(path), "ask_id": ask.ask_id,
        })

    def _render_amendment(self, ask: TeacherAsk, answer: TeacherAnswer) -> str:
        amendment = answer.amendment
        assert amendment is not None
        head = f"\n## {amendment.section or '未指明章节'}（{self._task_id} / {ask.ask_id}）\n"
        body = f"\n{amendment.patch.strip()}\n"
        why = f"\n> 订正理由：{amendment.reason.strip()}\n" if amendment.reason.strip() else ""
        trigger = f"> 触发提问：{ask.question.strip()[:300]}\n"
        return head + body + why + trigger

    def _render_addendum(self) -> str:
        parts = [f"# 题面订正：{self._task_key}\n",
                 f"\n来自运行 `{self._task_id}`。\n"]
        for rec in self._amendments:
            amendment = rec.get("amendment") or {}
            parts.append(f"\n## {amendment.get('section') or '未指明章节'}（{rec['ask_id']}）\n")
            parts.append(f"\n{(amendment.get('patch') or '').strip()}\n")
            if (amendment.get("reason") or "").strip():
                parts.append(f"\n> 订正理由：{amendment['reason'].strip()}\n")
            parts.append(f"> 触发提问：{rec['question'].strip()[:300]}\n")
        return "".join(parts)

    def _budget_view(self, answer: TeacherAnswer) -> TeacherAnswer:
        answer.asks_used = len(self._asks)
        answer.asks_remaining = max(0, self._max_asks - answer.asks_used)
        answer.package_revision = answer.package_revision or self._live.revision
        return answer
