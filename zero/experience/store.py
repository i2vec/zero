"""Shared Researcher experience bank under ``experience/researcher/``.

Layout::

    experience/researcher/
      index.jsonl
      entries/<id>.md

The Researcher decides what to record via MCP; this module only validates,
persists, and searches. No automatic ingest from traces or conclusions.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from zero.config import Config

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|(?:api[_-]?key|token|password|bohrium_key)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_TASK_SPECIFIC = re.compile(r"\b(?:task-|sandbox-|req-)[A-Za-z0-9_-]+", re.IGNORECASE)
_CONFIDENCE = frozenset({"high", "medium", "low"})
_MIN_LESSON = 40
_MAX_LESSON = 4_000
_MAX_TITLE = 120
_MAX_TRIGGER = 800
_MAX_AVOID = 1_500
_MAX_TAGS = 12
_SEARCH_DEFAULT_LIMIT = 8
_SEARCH_BODY_CHARS = 600


@dataclass
class ExperienceEntry:
    title: str
    tags: list[str]
    trigger: str
    lesson: str
    avoid: str = ""
    confidence: str = "medium"
    id: str = ""
    created_at: float = 0.0
    source_run: str = ""


@dataclass
class ExperienceStore:
    """File-backed experience library for the Researcher."""

    config: Config
    source_run: str = ""

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.is_file():
            self.index_path.write_text("", encoding="utf-8")

    @property
    def root(self) -> Path:
        return self.config.experience_dir

    @property
    def entries_dir(self) -> Path:
        return self.root / "entries"

    @property
    def index_path(self) -> Path:
        return self.root / "index.jsonl"

    def search(
        self,
        *,
        query: str = "",
        tags: Optional[list[str]] = None,
        limit: int = _SEARCH_DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        """Keyword / tag search over the index; returns compact hit dicts."""
        limit = max(1, min(int(limit or _SEARCH_DEFAULT_LIMIT), 20))
        q = (query or "").strip().lower()
        want_tags = {t.strip().lower() for t in (tags or []) if t and t.strip()}
        hits: list[tuple[int, dict[str, Any]]] = []
        for meta in self._iter_index():
            score = self._score(meta, q, want_tags)
            if score <= 0 and (q or want_tags):
                continue
            if not q and not want_tags:
                score = 1
            body = self._read_lesson_preview(meta.get("id", ""))
            hits.append((score, {
                "id": meta.get("id"),
                "title": meta.get("title"),
                "tags": meta.get("tags") or [],
                "trigger": meta.get("trigger") or "",
                "confidence": meta.get("confidence") or "medium",
                "source_run": meta.get("source_run") or "",
                "created_at": meta.get("created_at"),
                "preview": body,
            }))
        hits.sort(key=lambda item: (-item[0], -(item[1].get("created_at") or 0)))
        return [h for _, h in hits[:limit]]

    def get(self, entry_id: str) -> Optional[dict[str, Any]]:
        entry_id = (entry_id or "").strip()
        if not entry_id or not _SLUG.match(entry_id):
            return None
        path = self.entries_dir / f"{entry_id}.md"
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        meta = self._index_lookup(entry_id) or {"id": entry_id}
        return {**meta, "body": text, "path": str(path)}

    def record(
        self,
        *,
        title: str,
        tags: list[str],
        trigger: str,
        lesson: str,
        avoid: str = "",
        confidence: str = "medium",
    ) -> dict[str, Any]:
        """Validate and append one experience. Raises ``ValueError`` on reject."""
        entry = self._normalize(
            title=title, tags=tags, trigger=trigger, lesson=lesson,
            avoid=avoid, confidence=confidence,
        )
        problems = self._validate(entry)
        if problems:
            raise ValueError("; ".join(problems))
        if (self.entries_dir / f"{entry.id}.md").is_file():
            raise ValueError(f"experience id already exists: {entry.id} (rename title or search first)")

        body = self._render(entry)
        path = self.entries_dir / f"{entry.id}.md"
        path.write_text(body, encoding="utf-8")
        meta = {
            "id": entry.id,
            "title": entry.title,
            "tags": entry.tags,
            "trigger": entry.trigger,
            "confidence": entry.confidence,
            "created_at": entry.created_at,
            "source_run": entry.source_run,
        }
        with self.index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        self._audit_write(meta)
        return {**meta, "path": str(path), "ok": True}

    def _normalize(
        self,
        *,
        title: str,
        tags: list[str],
        trigger: str,
        lesson: str,
        avoid: str,
        confidence: str,
    ) -> ExperienceEntry:
        title = " ".join((title or "").strip().split())
        trigger = (trigger or "").strip()
        lesson = (lesson or "").strip()
        avoid = (avoid or "").strip()
        confidence = (confidence or "medium").strip().lower()
        clean_tags: list[str] = []
        for raw in tags or []:
            t = re.sub(r"[^a-z0-9\-]+", "-", str(raw).strip().lower()).strip("-")
            if t and t not in clean_tags:
                clean_tags.append(t)
        entry_id = self._slugify(title)
        return ExperienceEntry(
            id=entry_id,
            title=title,
            tags=clean_tags[:_MAX_TAGS],
            trigger=trigger,
            lesson=lesson,
            avoid=avoid,
            confidence=confidence if confidence in _CONFIDENCE else "medium",
            created_at=time.time(),
            source_run=self.source_run or "",
        )

    @staticmethod
    def _slugify(title: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        slug = re.sub(r"-{2,}", "-", slug)
        if len(slug) > 80:
            slug = slug[:80].rstrip("-")
        return slug

    def _validate(self, entry: ExperienceEntry) -> list[str]:
        problems: list[str] = []
        if not entry.title:
            problems.append("title is required")
        elif len(entry.title) > _MAX_TITLE:
            problems.append(f"title exceeds {_MAX_TITLE} chars")
        if not entry.id or not _SLUG.match(entry.id):
            problems.append("title must yield a lowercase kebab-case id")
        if not entry.trigger:
            problems.append("trigger is required (when to recall this)")
        elif len(entry.trigger) > _MAX_TRIGGER:
            problems.append(f"trigger exceeds {_MAX_TRIGGER} chars")
        if len(entry.lesson) < _MIN_LESSON:
            problems.append(f"lesson too short (min {_MIN_LESSON} chars)")
        elif len(entry.lesson) > _MAX_LESSON:
            problems.append(f"lesson exceeds {_MAX_LESSON} chars")
        if len(entry.avoid) > _MAX_AVOID:
            problems.append(f"avoid exceeds {_MAX_AVOID} chars")
        blob = "\n".join([entry.title, entry.trigger, entry.lesson, entry.avoid, " ".join(entry.tags)])
        if _SECRET.search(blob):
            problems.append("contains a possible secret")
        if _TASK_SPECIFIC.search(blob):
            problems.append("contains task/sandbox-specific identifier")
        if "/root/" in blob or "/home/" in blob:
            problems.append("contains an absolute home path")
        return problems

    @staticmethod
    def _render(entry: ExperienceEntry) -> str:
        tags = ", ".join(entry.tags)
        avoid_block = f"\n## Avoid\n{entry.avoid}\n" if entry.avoid else ""
        return (
            f"---\n"
            f"id: {entry.id}\n"
            f"title: {json.dumps(entry.title, ensure_ascii=False)}\n"
            f"tags: [{tags}]\n"
            f"confidence: {entry.confidence}\n"
            f"source_run: {entry.source_run}\n"
            f"created_at: {entry.created_at}\n"
            f"---\n\n"
            f"# {entry.title}\n\n"
            f"## When to recall\n{entry.trigger}\n\n"
            f"## Lesson\n{entry.lesson}\n"
            f"{avoid_block}"
        )

    def _iter_index(self) -> list[dict[str, Any]]:
        if not self.index_path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = self.index_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("id"):
                rows.append(row)
        return rows

    def _index_lookup(self, entry_id: str) -> Optional[dict[str, Any]]:
        for row in self._iter_index():
            if row.get("id") == entry_id:
                return row
        return None

    def _read_lesson_preview(self, entry_id: str) -> str:
        if not entry_id:
            return ""
        path = self.entries_dir / f"{entry_id}.md"
        if not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        # Prefer the Lesson section body.
        marker = "## Lesson\n"
        if marker in text:
            body = text.split(marker, 1)[1]
            for stop in ("\n## ", "\n---\n"):
                if stop in body:
                    body = body.split(stop, 1)[0]
            text = body.strip()
        text = " ".join(text.split())
        if len(text) > _SEARCH_BODY_CHARS:
            return text[:_SEARCH_BODY_CHARS] + "…"
        return text

    @staticmethod
    def _score(meta: dict[str, Any], query: str, want_tags: set[str]) -> int:
        score = 0
        tags = {str(t).lower() for t in (meta.get("tags") or [])}
        if want_tags:
            overlap = len(want_tags & tags)
            if overlap == 0 and not query:
                return 0
            score += overlap * 3
        if query:
            hay = " ".join([
                str(meta.get("title") or ""),
                str(meta.get("trigger") or ""),
                " ".join(tags),
            ]).lower()
            for token in query.split():
                if token and token in hay:
                    score += 2
            if query in hay:
                score += 2
        return score

    def _audit_write(self, meta: dict[str, Any]) -> None:
        run = (self.source_run or "").strip()
        if not run:
            return
        try:
            meta_dir = self.config.ensure_run_dirs(run) / "meta"
            path = meta_dir / "experience_writes.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        except OSError:
            pass
