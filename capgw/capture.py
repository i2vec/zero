"""Capture writer for LLM-call trajectories.

Each record is a self-contained snapshot of a single agent<->model exchange:
the original client request, the canonical OpenAI-Chat request sent upstream,
the full upstream response (including ``reasoning_content`` chain-of-thought),
and the response transformed back to the client's API shape.

Two storage layouts:
- **named** (when a run ``name`` is set): every call is written as its own
  numbered JSON file at ``captures/<name>/<name>_<NNNNNN>.json``. Numbering is
  monotonic and resumes from the highest existing index across restarts.
- **session** (default): one append-only JSONL file per session at
  ``captures/<session_id>.jsonl``.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional


# Keep unicode word chars (letters/digits/underscore, incl. non-ASCII like
# Chinese) plus '.' and '-'; collapse everything else (slashes handled by the
# per-segment split, whitespace, other punctuation) to '_'. This lets custom
# run names in any language survive as capture folder names.
_SESSION_SANITIZE_RE = re.compile(r"[^\w.\-]+", re.UNICODE)


def _sanitize_session_id(session_id: str) -> str:
    """Sanitize a session id, treating ``/`` as a subdirectory separator.

    Each path segment is cleaned independently and unsafe segments (empty,
    ``.``, ``..``) are dropped, so a namespaced key like ``task-001/researcher``
    lands at ``captures/task-001/researcher.jsonl`` without allowing traversal
    outside the capture root.
    """
    segments: list[str] = []
    for raw_segment in session_id.split("/"):
        cleaned = _SESSION_SANITIZE_RE.sub("_", raw_segment).strip("_")[:120]
        if cleaned and cleaned not in (".", ".."):
            segments.append(cleaned)
    if segments:
        return "/".join(segments)
    return f"sess-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _sanitize_name(name: str) -> str:
    """Filesystem-safe run name used as both a subdir and file prefix."""
    cleaned = _SESSION_SANITIZE_RE.sub("_", name).strip("_")
    return cleaned[:120] or "run"


def derive_session_id(headers: dict[str, str], query: dict[str, str]) -> str:
    """Pick a stable session key for grouping a trajectory.

    Priority: X-Session-Id → x-api-key (Anthropic) → Authorization bearer →
    query session_id/key → generated fallback.
    """
    lower = {k.lower(): v for k, v in headers.items()}

    explicit = lower.get("x-session-id") or lower.get("x-capgw-session")
    if explicit:
        return _sanitize_session_id(explicit)

    api_key = lower.get("x-api-key")
    if api_key:
        return _sanitize_session_id(api_key)

    auth = lower.get("authorization")
    if auth:
        token = auth[len("bearer ") :] if auth.lower().startswith("bearer ") else auth
        if token:
            return _sanitize_session_id(token)

    q = query.get("session_id") or query.get("key")
    if q:
        return _sanitize_session_id(q)

    return f"sess-{int(time.time())}-{uuid.uuid4().hex[:8]}"


class CaptureWriter:
    """Writes one record per LLM call, in either named or session layout."""

    def __init__(
        self,
        out_dir: str,
        upstream_endpoint: str,
        model_used: str,
        name: Optional[str] = None,
    ):
        self._upstream_endpoint = upstream_endpoint
        self._model_used = model_used
        self._locks: dict[str, asyncio.Lock] = {}

        self._name = _sanitize_name(name) if name else None
        if self._name:
            # Named mode: dedicated subdir + monotonic numbered json files.
            self._out_dir = Path(out_dir) / self._name
            self._out_dir.mkdir(parents=True, exist_ok=True)
            self._counter_lock = asyncio.Lock()
            self._counter = self._scan_last_index()
        else:
            self._out_dir = Path(out_dir)
            self._out_dir.mkdir(parents=True, exist_ok=True)

    @property
    def out_dir(self) -> Path:
        return self._out_dir

    @property
    def name(self) -> Optional[str]:
        return self._name

    def _scan_last_index(self) -> int:
        """Highest existing index for this name, so restarts keep counting up."""
        pattern = re.compile(rf"^{re.escape(self._name or '')}_(\d+)\.json$")
        highest = 0
        for existing in self._out_dir.glob(f"{self._name}_*.json"):
            match = pattern.match(existing.name)
            if match:
                highest = max(highest, int(match.group(1)))
        return highest

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def write(
        self,
        *,
        session_id: str,
        api_type: str,
        model_requested: str,
        original_request: dict[str, Any],
        upstream_request: dict[str, Any],
        upstream_response: Optional[dict[str, Any]],
        returned_response: Any,
        streamed: bool,
        error: Optional[str] = None,
    ) -> Path:
        record = {
            "ts": time.time(),
            "name": self._name,
            "session_id": session_id,
            "upstream_endpoint": self._upstream_endpoint,
            "api_type": api_type,
            "model_requested": model_requested,
            "model_used": self._model_used,
            "streamed": streamed,
            "original_request": original_request,
            "upstream_request": upstream_request,
            "upstream_response": upstream_response,
            "returned_response": returned_response,
        }
        if error is not None:
            record["error"] = error

        if self._name:
            return await self._write_named(record)
        return await self._write_session(session_id, record)

    async def _write_named(self, record: dict[str, Any]) -> Path:
        async with self._counter_lock:
            self._counter += 1
            index = self._counter
        record["index"] = index
        path = self._out_dir / f"{self._name}_{index:06d}.json"
        payload = json.dumps(record, ensure_ascii=False, default=str, indent=2)
        await asyncio.to_thread(self._write_file, path, payload)
        return path

    async def _write_session(self, session_id: str, record: dict[str, Any]) -> Path:
        path = self._out_dir / f"{session_id}.jsonl"
        # session_id may be namespaced (e.g. task-001/researcher) -> ensure the
        # subdirectory exists before appending.
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        async with self._lock_for(session_id):
            await asyncio.to_thread(self._append_line, path, line)
        return path

    @staticmethod
    def _write_file(path: Path, payload: str) -> None:
        with path.open("w", encoding="utf-8") as f:
            f.write(payload)

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")
