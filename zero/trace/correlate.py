"""Stitch the two trace layers together by task_id / session_id.

Layout::

    runs/<task>/trace/{researcher,labwright,teacher,events}.jsonl
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zero.config import Config


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def resolve_trace_paths(task_id: str, config: Config) -> dict[str, Path]:
    """Return Layer-1/2 paths under ``runs/<task_id>/trace/``."""
    run_trace = config.run_dir(task_id) / "trace"
    return {
        "researcher": run_trace / "researcher.jsonl",
        "labwright": run_trace / "labwright.jsonl",
        "teacher": run_trace / "teacher.jsonl",
        "events": run_trace / "events.jsonl",
    }


def correlate_traces(task_id: str, config: Config, sandbox_ids: list[str] | None = None) -> dict[str, Any]:
    paths = resolve_trace_paths(task_id, config)
    return {
        "task_id": task_id,
        "correlation_keys": {
            "task_id": task_id,
            "researcher_session_id": f"{task_id}/trace/researcher",
            "labwright_session_id": f"{task_id}/trace/labwright",
            "teacher_session_id": f"{task_id}/trace/teacher",
            "sandbox_ids": sandbox_ids or [],
        },
        "layer1_model_calls": {
            "researcher": {"path": str(paths["researcher"]), "calls": _count_lines(paths["researcher"])},
            "labwright": {"path": str(paths["labwright"]), "calls": _count_lines(paths["labwright"])},
            "teacher": {"path": str(paths["teacher"]), "calls": _count_lines(paths["teacher"])},
        },
        "layer2_orchestration_events": {
            "path": str(paths["events"]), "events": _count_lines(paths["events"]),
        },
    }
