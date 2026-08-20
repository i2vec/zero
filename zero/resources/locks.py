"""Canonical, atomic resources.lock.json persistence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from zero.protocol.resources import ResourceLock, ResourceLockEntry


def validate_release_lock(
    lock: ResourceLock,
    required: dict[str, str],
    *,
    require_immutable: bool = False,
) -> list[str]:
    """Return deterministic release-gate violations for required resources."""
    entries = {entry.requirement_id: entry for entry in lock.entries}
    errors: list[str] = []
    for requirement_id, expected_kind in sorted(required.items()):
        entry = entries.get(requirement_id)
        if entry is None:
            errors.append(f"missing:{requirement_id}")
            continue
        if entry.kind.value != expected_kind:
            errors.append(
                f"kind_mismatch:{requirement_id}:{entry.kind.value}!={expected_kind}"
            )
        if entry.verification.status != "passed":
            errors.append(f"verification_not_passed:{requirement_id}")
        if require_immutable and not entry.artifact.immutable():
            errors.append(f"mutable_artifact:{requirement_id}")
    return errors


def canonical_bytes(lock: ResourceLock) -> bytes:
    return (json.dumps(
        lock.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ) + "\n").encode("utf-8")


def lock_digest(lock: ResourceLock) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(lock)).hexdigest()


class ResourceLockStore:
    def __init__(self, path: Path, task_id: str):
        self.path = Path(path)
        self.task_id = task_id

    def read(self) -> ResourceLock:
        try:
            return ResourceLock.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ResourceLock(task_id=self.task_id)

    def put(self, entry: ResourceLockEntry) -> str:
        lock = self.read()
        entries = {item.requirement_id: item for item in lock.entries}
        entries[entry.requirement_id] = entry
        lock.entries = [entries[key] for key in sorted(entries)]
        return self.write(lock)

    def write(self, lock: ResourceLock) -> str:
        if lock.task_id != self.task_id:
            raise ValueError("resource lock task_id mismatch")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = canonical_bytes(lock)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        return lock_digest(lock)
