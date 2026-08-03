"""Structured orchestration event trace (doc section 27.3).

Records task-state transitions, agent-call relations, sandbox lifecycle,
Labwright state, permission/hook interceptions, and error routing. One
append-only JSONL per task under ``runs/<task_id>/trace/events.jsonl``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional, Union


class TraceWriter:
    def __init__(self, task_id: str, events_path: Union[Path, str]):
        """Write Layer-2 events to ``events_path`` (typically ``.../trace/events.jsonl``)."""
        self._task_id = task_id
        self._path = Path(events_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, agent: str, event: str, detail: Optional[dict[str, Any]] = None,
             sandbox_id: Optional[str] = None) -> None:
        record = {
            "ts": time.time(),
            "task_id": self._task_id,
            "agent": agent,          # orchestrator | researcher | labwright | teacher
            "event": event,
            "sandbox_id": sandbox_id,
            "detail": detail or {},
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def agent_turn(self, agent: str, kind: str, data: dict[str, Any]) -> None:
        """Mirror of a Claude Code turn event (full trajectory is in capgw).

        Chain-of-thought (``thinking``) is kept separate from spoken ``text`` so
        the trace viewer can render the two distinctly. Text/inputs/results are
        stored in full — no truncation — so the viewer shows the complete turn.
        """
        summary: dict[str, Any] = {"kind": kind}
        if kind == "tool_use":
            summary["tool"] = data.get("name")
            summary["input"] = data.get("input")
        elif kind in ("thinking", "text"):
            summary["text"] = data.get("text")
        elif kind == "tool_result":
            summary["is_error"] = data.get("is_error")
            summary["content"] = data.get("content")
        elif kind == "result":
            summary["num_turns"] = data.get("num_turns")
            summary["is_error"] = data.get("is_error")
        self.emit(agent, f"turn:{kind}", summary)
