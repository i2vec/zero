"""Safe staging and review of reusable Researcher/Labwright skills."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from zero.config import Config

Role = Literal["researcher", "labwright"]
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|(?:api[_-]?key|token|password|bohrium_key)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)
_TASK_SPECIFIC = re.compile(r"\b(?:task-|sandbox-|req-)[A-Za-z0-9_-]+", re.IGNORECASE)


@dataclass(frozen=True)
class SkillProposal:
    name: str
    description: str
    instructions: str
    trigger: str
    verification: str
    evidence: list[str]


class SkillCandidates:
    """Stage proposals outside plugin roots; publish only after review."""

    def __init__(self, config: Config):
        self._config = config

    def propose(self, role: Role, task_id: str, proposal: SkillProposal) -> str:
        self._validate_input(proposal)
        proposal_id = f"{proposal.name}-{int(time.time())}-{hashlib.sha1(task_id.encode()).hexdigest()[:6]}"
        folder = self._config.run_skill_candidates_dir(task_id) / role / proposal_id
        folder.mkdir(parents=True, exist_ok=False)
        skill = self._render(proposal)
        (folder / "SKILL.md").write_text(skill, encoding="utf-8")
        (folder / "proposal.json").write_text(
            json.dumps(
                {
                    "id": proposal_id, "role": role, "task_id": task_id,
                    "status": "proposed", "created_at": time.time(),
                    "trigger": proposal.trigger, "verification": proposal.verification,
                    "evidence": proposal.evidence, "name": proposal.name,
                    "description": proposal.description,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        return proposal_id

    def list(self, role: Role | None = None) -> list[dict]:
        roles = (role,) if role else ("researcher", "labwright")
        items: list[dict] = []
        for r in roles:
            pattern = f"*/meta/skill_candidates/{r}/*/proposal.json"
            for metadata in self._config.runs_dir.glob(pattern):
                try:
                    data = json.loads(metadata.read_text(encoding="utf-8"))
                    data["path"] = str(metadata.parent)
                    items.append(data)
                except (OSError, json.JSONDecodeError):
                    continue
        return sorted(items, key=lambda item: item.get("created_at", 0), reverse=True)

    def validate(self, role: Role, proposal_id: str) -> dict:
        folder = self._folder(role, proposal_id)
        metadata = self._metadata(folder)
        skill = (folder / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        problems = self._problems(skill, metadata)
        metadata["validation"] = {"ok": not problems, "problems": problems, "at": time.time()}
        metadata["status"] = "validated" if not problems else "rejected"
        self._write_metadata(folder, metadata)
        return metadata

    def publish(self, role: Role, proposal_id: str) -> Path:
        metadata = self.validate(role, proposal_id)
        if not metadata["validation"]["ok"]:
            raise ValueError("candidate failed validation: " + "; ".join(metadata["validation"]["problems"]))
        source = self._folder(role, proposal_id)
        root = self._active_root(role)
        destination = root / metadata["name"]
        if destination.exists():
            backup = destination.with_name(f"{destination.name}.bak.{int(time.time())}")
            shutil.copytree(destination, backup)
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        shutil.copy2(source / "SKILL.md", destination / "SKILL.md")
        content = (destination / "SKILL.md").read_bytes()
        (destination / "skill-manifest.json").write_text(
            json.dumps(
                {
                    "version": 1, "source_candidate": proposal_id,
                    "published_at": time.time(),
                    "files": {"SKILL.md": hashlib.sha256(content).hexdigest()},
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        metadata["status"] = "published"
        metadata["published_path"] = str(destination)
        self._write_metadata(source, metadata)
        return destination

    def reject(self, role: Role, proposal_id: str, reason: str) -> None:
        folder = self._folder(role, proposal_id)
        metadata = self._metadata(folder)
        metadata["status"] = "rejected"
        metadata["rejection_reason"] = reason[:1000]
        self._write_metadata(folder, metadata)

    def _active_root(self, role: Role) -> Path:
        base = self._config.researcher_skills_dir if role == "researcher" else self._config.labwright_skills_dir
        return base / "skills"

    def _folder(self, role: Role, proposal_id: str) -> Path:
        if not _SLUG.match(proposal_id.rsplit("-", 2)[0]):
            raise ValueError("invalid candidate id")
        matches = sorted(
            self._config.runs_dir.glob(f"*/meta/skill_candidates/{role}/{proposal_id}"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        for path in matches:
            if (path / "proposal.json").is_file():
                return path
        raise FileNotFoundError(proposal_id)

    @staticmethod
    def _metadata(folder: Path) -> dict:
        return json.loads((folder / "proposal.json").read_text(encoding="utf-8"))

    @staticmethod
    def _write_metadata(folder: Path, metadata: dict) -> None:
        (folder / "proposal.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _render(proposal: SkillProposal) -> str:
        return (
            f"---\nname: {proposal.name}\ndescription: {proposal.description}\n"
            "disable-model-invocation: false\n---\n\n"
            f"# {proposal.name}\n\n## When to use\n{proposal.trigger}\n\n"
            f"## Instructions\n{proposal.instructions}\n\n"
            f"## Verification\n{proposal.verification}\n"
        )

    @staticmethod
    def _validate_input(proposal: SkillProposal) -> None:
        if not _SLUG.match(proposal.name):
            raise ValueError("skill name must be lowercase kebab-case")
        if not proposal.description or not proposal.instructions or not proposal.trigger:
            raise ValueError("description, trigger and instructions are required")
        if len(proposal.instructions) > 12_000:
            raise ValueError("skill instructions exceed 12KB")

    @staticmethod
    def _problems(skill: str, metadata: dict) -> list[str]:
        problems: list[str] = []
        if not skill.startswith("---\n") or "\n---\n" not in skill:
            problems.append("missing YAML frontmatter")
        if _SECRET.search(skill):
            problems.append("contains a possible secret")
        if _TASK_SPECIFIC.search(skill):
            problems.append("contains task/sandbox-specific identifier")
        if "/root/" in skill or "/home/" in skill:
            problems.append("contains an absolute home path")
        if metadata.get("role") == "labwright" and re.search(
            r"\b(?:hypothesis|scientific conclusion|model precision|dataset semantics)\b",
            skill, re.IGNORECASE,
        ):
            problems.append("Labwright skill contains a scientific decision")
        return problems
