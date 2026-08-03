"""TeacherService: the glue between the Researcher's ask and one Teacher turn.

Same execution model as Labwright — **blocking handoff**. ``ask()`` drives one
Teacher turn to completion under a lock and returns its terminal answer, so only
one agent is ever mid-turn.

Two things make the Teacher different from Labwright:

- **Budget.** Asks are capped (``ZERO_TEACHER_MAX_ASKS``) so the Researcher
  cannot outsource its thinking, and every ask is counted in the trace — a run
  that leaned on hints must remain distinguishable from one that did not.
- **Amendments are run artifacts.** A ``TASK_AMENDMENT`` is written under
  ``runs/<id>/teacher/task_addendum.md`` (no separate cross-run tree).
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
from zero.protocol.teaching import TeacherAnswer, TeacherAsk, TeachingKind
from zero.teacher.agent import TeacherAgent
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
    ):
        self._config = config
        self._task_id = task_id
        self._task_key = task_key or task_id
        self._task_prompt = task_prompt
        self._emit = emit or (lambda a, e, d: None)
        self._max_asks = config.teacher_max_asks if max_asks is None else max_asks
        self._run_dir = config.ensure_run_dirs(task_id)
        self._teacher_dir = self._run_dir / "teacher"

        self._ctx = TeacherContext(
            config=config, task_id=task_id, task_key=self._task_key, emit=self._emit,
        )
        self._agent = TeacherAgent(
            config, task_id=task_id, ctx=self._ctx, on_event=on_agent_event,
        )

        self._counter = itertools.count(1)
        self._asks: list[dict] = []            # one record per ask, for the run archive
        self._amendments: list[dict] = []      # TASK_AMENDMENT subset, in order
        self._introduced = False               # has the task statement been sent yet
        self._lock = asyncio.Lock()

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
        self._record(ask, answer)
        return self._budget_view(answer)

    # ---- run artifacts --------------------------------------------------- #
    def stats(self) -> dict:
        """Compact summary for run metadata (keeps hinted runs comparable)."""
        kinds: dict[str, int] = {}
        for rec in self._asks:
            kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
        return {
            "task_key": self._task_key,
            "asks_used": len(self._asks),
            "max_asks": self._max_asks,
            "by_kind": kinds,
            "amendments": len(self._amendments),
        }

    def write_artifacts(self, run_dir: Optional[Path] = None) -> None:
        """Drop the Q&A log and amended statement under ``runs/<id>/teacher/``."""
        if not self._asks:
            return
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
        except OSError:  # noqa: S110 - artifacts must never break a run
            pass

    async def close(self) -> None:
        await self._agent.close()

    # ---- internals ------------------------------------------------------- #
    async def _run_turn(self, ask: TeacherAsk) -> TeacherAnswer:
        async with self._lock:
            self._ctx.set_ask(ask)
            self._emit("teacher", "ask_received", {
                "ask_id": ask.ask_id,
                "question": ask.question[:300],
                "where_stuck": ask.where_stuck[:200],
            })
            try:
                result = await self._agent.run_turn(self._prompt(ask))
            except Exception as exc:  # noqa: BLE001 - a broken Teacher must not fail the run
                self._emit("teacher", "agent_error", {
                    "ask_id": ask.ask_id, "error": str(exc)[:500],
                })
                return TeacherAnswer(
                    kind=TeachingKind.NO_HELP, ask_id=ask.ask_id,
                    content=f"teacher unavailable: {exc}",
                )

            answer = self._ctx.answer
            if answer is None:
                # Ended without a terminal tool — same treatment as Labwright.
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
                "This is the task the Researcher is working on (sent once; later "
                "asks will not repeat it):\n"
                f"```\n{self._task_prompt.strip()}\n```\n"
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
            "\nDecide (skill `teaching`): (1) scientific defect in the task "
            "statement → amend_task_statement → kind TASK_AMENDMENT; "
            "(2) difficulty in the science/method → give_hint → kind HINT; "
            "(3) environment/path/image/packaging → decline → kind NO_HELP "
            "and point at Labwright; (4) nothing useful → decline → kind "
            "NO_HELP. End with exactly one of give_hint / "
            "amend_task_statement / decline."
        )
        return "".join(parts)

    def _record(self, ask: TeacherAsk, answer: TeacherAnswer) -> None:
        rec = {
            "ts": time.time(),
            "ask_id": ask.ask_id,
            "question": ask.question,
            "what_i_tried": ask.what_i_tried,
            "where_stuck": ask.where_stuck,
            "kind": answer.kind.value,
            "content": answer.content,
        }
        if answer.amendment is not None:
            rec["amendment"] = answer.amendment.model_dump(exclude_none=True)
            self._amendments.append(rec)
            self._append_task_addendum(ask, answer)
        self._asks.append(rec)

    def _append_task_addendum(self, ask: TeacherAsk, answer: TeacherAnswer) -> None:
        """Append one amendment into this run's ``teacher/task_addendum.md``."""
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
        except OSError:  # noqa: S110 - never break a run over an artifact
            return
        self._emit("teacher", "task_addendum_written", {
            "path": str(path), "ask_id": ask.ask_id,
        })

    def seed_hint_bank(self, src: Optional[Path]) -> list[str]:
        """Copy a file or directory of ``*.md`` into ``teacher/hint_bank/``.

        Existing files in the bank are left alone (no overwrite). Returns the
        list of newly written paths.
        """
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
        return answer
