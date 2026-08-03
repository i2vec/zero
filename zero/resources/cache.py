"""Content cache + provenance index for collected models/datasets."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class CachedResource:
    kind: str            # model | dataset
    name: str
    version: str         # revision (model) or version (dataset)
    host_path: str
    source: Optional[str] = None
    sha256: Optional[str] = None
    collected_at: Optional[str] = None


class ResourceCache:
    def __init__(self, cache_dir: Path):
        self._dir = Path(cache_dir)
        (self._dir / "models").mkdir(parents=True, exist_ok=True)
        (self._dir / "datasets").mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "index.json"
        self._lock = threading.Lock()
        if not self._index_path.exists():
            self._index_path.write_text("{}", encoding="utf-8")

    def _top(self, kind: str) -> str:
        return "models" if kind == "model" else "datasets"

    def path_for(self, kind: str, name: str, version: str) -> Path:
        return self._dir / self._top(kind) / name / version

    def _key(self, kind: str, name: str, version: str) -> str:
        return f"{kind}:{name}:{version}"

    def _read_index(self) -> dict:
        try:
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def get(self, kind: str, name: str, version: str) -> Optional[CachedResource]:
        with self._lock:
            entry = self._read_index().get(self._key(kind, name, version))
        if not entry:
            return None
        res = CachedResource(**entry)
        # Only a cache hit if the payload directory actually exists and is
        # non-empty (guards against a recorded-but-wiped resource).
        p = Path(res.host_path)
        if p.exists() and any(p.iterdir()):
            return res
        return None

    def record(self, res: CachedResource) -> CachedResource:
        res.collected_at = res.collected_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            index = self._read_index()
            index[self._key(res.kind, res.name, res.version)] = asdict(res)
            self._index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        return res
