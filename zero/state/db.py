"""Per-run task state under ``runs/<task_id>/meta/task.json``.

One run folder owns its record. Deleting ``runs/<id>/`` clears the name;
there is no separate global registry that can outlive the folder.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from zero.config import Config


@dataclass
class TaskRecord:
    task_id: str
    task_status: str = "task_received"
    researcher_session_id: str = ""
    labwright_session_id: str = ""
    teacher_session_id: str = ""
    # Identifies the *task* (e.g. a challenge id) as opposed to this run.
    task_key: str = ""
    current_experiment: Optional[str] = None
    current_sandbox_id: Optional[str] = None
    workspace: str = ""
    sandbox_ids: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: Optional[str] = None


class StateDB:
    """File-backed task records: ``runs/<task_id>/meta/task.json``."""

    def __init__(self, config: Union[Config, Path]):
        # Accept Config (preferred). A bare Path is treated as runs_dir for tests.
        if isinstance(config, Config):
            self._config: Optional[Config] = config
            self._runs_dir = config.runs_dir
        else:
            self._config = None
            self._runs_dir = Path(config)
        self._lock = threading.Lock()

    def _record_path(self, task_id: str) -> Path:
        if self._config is not None:
            return self._config.run_dir(task_id) / "meta" / "task.json"
        return self._runs_dir / task_id / "meta" / "task.json"

    def _ensure_parent(self, task_id: str) -> None:
        if self._config is not None:
            self._config.ensure_run_dirs(task_id)
        else:
            self._record_path(task_id).parent.mkdir(parents=True, exist_ok=True)

    def create(self, record: TaskRecord) -> None:
        self._ensure_parent(record.task_id)
        self._save(record)

    def get(self, task_id: str) -> Optional[TaskRecord]:
        path = self._record_path(task_id)
        if not path.is_file():
            return None
        with self._lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        if not isinstance(data, dict):
            return None
        return TaskRecord(**data)

    def update(self, task_id: str, **changes: Any) -> TaskRecord:
        rec = self.get(task_id)
        if rec is None:
            raise KeyError(task_id)
        for k, v in changes.items():
            setattr(rec, k, v)
        rec.updated_at = time.time()
        self._save(rec)
        return rec

    def add_sandbox(self, task_id: str, sandbox_id: str) -> TaskRecord:
        rec = self.get(task_id)
        if rec is None:
            raise KeyError(task_id)
        if sandbox_id not in rec.sandbox_ids:
            rec.sandbox_ids.append(sandbox_id)
        rec.current_sandbox_id = sandbox_id
        rec.updated_at = time.time()
        self._save(rec)
        return rec

    def _save(self, record: TaskRecord) -> None:
        path = self._record_path(record.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(record), ensure_ascii=False, indent=2)
        with self._lock:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(payload + "\n", encoding="utf-8")
            tmp.replace(path)

    def close(self) -> None:
        return None
