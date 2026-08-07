"""Live mutable task package under ``runs/<id>/task_package/`` with revisions."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from zero.grading import grade_harbor
from zero.protocol.grading import GradeResult
from zero.protocol.teaching import GraderAmendment, TaskAmendment
from zero.teacher.optimize import _apply_grader_amendment
from zero.teacher.package_lint import LintReport, lint_task_package

_PACKAGE_IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.py[cod]",
    ".git",
    ".DS_Store",
    ".pytest_cache",
    "*.egg-info",
)


@dataclass
class RevisionRecord:
    revision: int
    ts: float
    kind: str
    summary: str
    lint_ok: bool
    verify_score: Optional[float] = None
    verify_status: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "ts": self.ts,
            "kind": self.kind,
            "summary": self.summary,
            "lint_ok": self.lint_ok,
            "verify_score": self.verify_score,
            "verify_status": self.verify_status,
            "detail": self.detail,
        }


class LivePackageManager:
    """Seed, amend, lint, verify, and finalize the run's live task package."""

    def __init__(self, run_dir: Path, *, max_revisions: int = 12):
        self.run_dir = Path(run_dir)
        self.live_dir = self.run_dir / "task_package"
        self.revisions_dir = self.run_dir / "package_revisions"
        self.finalized_dir = self.run_dir / "finalized_task"
        self.max_revisions = max_revisions
        self._revision = 0
        self._history: list[RevisionRecord] = []
        self._pending_researcher_delta: str = ""

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def path(self) -> Path:
        return self.live_dir

    def instruction_text(self) -> str:
        path = self.live_dir / "instruction.md"
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return ""

    def consume_researcher_delta(self) -> str:
        delta = self._pending_researcher_delta
        self._pending_researcher_delta = ""
        return delta

    def seed(self, source: Optional[Path], *, fallback_instruction: str = "") -> Path:
        """Copy source package into live tree (or create minimal package)."""
        if self.live_dir.exists():
            shutil.rmtree(self.live_dir)
        self.revisions_dir.mkdir(parents=True, exist_ok=True)

        if source is not None and Path(source).is_dir():
            shutil.copytree(Path(source), self.live_dir, ignore=_PACKAGE_IGNORE)
        else:
            self.live_dir.mkdir(parents=True, exist_ok=True)
            (self.live_dir / "instruction.md").write_text(
                fallback_instruction.strip() + "\n", encoding="utf-8",
            )
            (self.live_dir / "tests").mkdir(exist_ok=True)

        if fallback_instruction.strip() and not (self.live_dir / "instruction.md").is_file():
            (self.live_dir / "instruction.md").write_text(
                fallback_instruction.strip() + "\n", encoding="utf-8",
            )

        self._revision = 0
        self._snapshot("seed", "initial seed from source package", lint_ok=True)
        self._write_state()
        return self.live_dir

    def lint(self) -> LintReport:
        return lint_task_package(self.live_dir)

    def verify(self, *, host_outputs: Optional[Path] = None) -> GradeResult:
        """Run Harbor checker against calibration/full or provided outputs."""
        tests = self.live_dir / "tests"
        grading_dir = self.run_dir / "grading"
        grading_dir.mkdir(parents=True, exist_ok=True)
        outputs = host_outputs
        if outputs is None:
            calib = self.live_dir / "calibration" / "full"
            if calib.is_dir() and any(calib.iterdir()):
                outputs = calib
        # Persist verify under a revision-tagged copy as well.
        result = grade_harbor(
            run_dir=self.run_dir,
            tests=tests,
            host_outputs=outputs,
        )
        tag = grading_dir / f"rev{self._revision:03d}_verify.json"
        try:
            tag.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
        return result

    def apply_task_amendment(self, amendment: TaskAmendment, *, kind: str = "TASK_AMENDMENT") -> dict[str, Any]:
        """Write statement into live instruction.md (single source of truth)."""
        self._ensure_revision_budget()
        before = self.instruction_text()
        new_text = self._merge_instruction(before, amendment)
        (self.live_dir / "instruction.md").write_text(
            new_text if new_text.endswith("\n") else new_text + "\n",
            encoding="utf-8",
        )
        delta = (
            f"Task statement updated (rev pending): "
            f"section={amendment.section or 'general'}; "
            f"reason={(amendment.reason or '')[:200]}"
        )
        return self._commit_amendment(
            kind=kind,
            summary=amendment.reason or "task statement amended",
            researcher_delta=delta,
            detail={"section": amendment.section, "literature_basis": amendment.literature_basis},
        )

    def apply_grader_amendment(self, amendment: GraderAmendment, *, kind: str = "GRADER_AMENDMENT") -> dict[str, Any]:
        self._ensure_revision_budget()
        tests = self.live_dir / "tests"
        if not tests.is_dir():
            tests.mkdir(parents=True, exist_ok=True)
        apply = _apply_grader_amendment(tests, amendment.target, amendment.patch)
        if not apply.get("ok"):
            return {
                "ok": False,
                "error": apply.get("detail") or "grader apply failed",
                "grader_apply": apply,
                "revision": self._revision,
            }
        delta = (
            f"Grader updated (rev pending): target={amendment.target}; "
            f"reason={(amendment.reason or '')[:200]}"
        )
        return self._commit_amendment(
            kind=kind,
            summary=amendment.reason or f"grader {amendment.target} amended",
            researcher_delta=delta,
            detail={"grader_apply": apply, "literature_basis": amendment.literature_basis},
        )

    def apply_both(
        self,
        task: TaskAmendment,
        grader: GraderAmendment,
    ) -> dict[str, Any]:
        self._ensure_revision_budget()
        before = self.instruction_text()
        new_text = self._merge_instruction(before, task)
        (self.live_dir / "instruction.md").write_text(
            new_text if new_text.endswith("\n") else new_text + "\n",
            encoding="utf-8",
        )
        tests = self.live_dir / "tests"
        tests.mkdir(parents=True, exist_ok=True)
        apply = _apply_grader_amendment(tests, grader.target, grader.patch)
        if not apply.get("ok"):
            # Roll back instruction to previous text.
            (self.live_dir / "instruction.md").write_text(before, encoding="utf-8")
            return {
                "ok": False,
                "error": apply.get("detail") or "grader apply failed",
                "grader_apply": apply,
                "revision": self._revision,
            }
        delta = (
            f"Task + grader updated: section={task.section or 'general'}; "
            f"grader={grader.target}; reason={(task.reason or grader.reason or '')[:200]}"
        )
        return self._commit_amendment(
            kind="BOTH_AMENDMENT",
            summary=task.reason or grader.reason or "task and grader amended",
            researcher_delta=delta,
            detail={
                "section": task.section,
                "grader_apply": apply,
                "literature_basis": "\n".join(
                    x for x in [task.literature_basis, grader.literature_basis] if x
                ),
            },
        )

    def finalize(self) -> Path:
        """Freeze live package to finalized_task/ and optimized_task/ (compat)."""
        for dest_name in ("finalized_task", "optimized_task"):
            dest = self.run_dir / dest_name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(self.live_dir, dest, ignore=_PACKAGE_IGNORE)
            changelog = self._changelog_markdown()
            (dest / "OPTIMIZATION.md").write_text(changelog, encoding="utf-8")
            (dest / "provenance.json").write_text(
                json.dumps(
                    {
                        "revision": self._revision,
                        "history": [h.to_dict() for h in self._history],
                        "live_package": str(self.live_dir),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        self._write_state()
        return self.finalized_dir

    # ---- internals ------------------------------------------------------- #

    def _commit_amendment(
        self,
        *,
        kind: str,
        summary: str,
        researcher_delta: str,
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        report = self.lint()
        verify: Optional[GradeResult] = None
        verify_error = ""
        if report.ok:
            try:
                verify = self.verify()
            except Exception as exc:  # noqa: BLE001
                verify_error = str(exc)[:500]

        lint_ok = report.ok
        # Fail closed on lint errors: restore previous snapshot.
        if not lint_ok:
            self._restore_previous_snapshot()
            return {
                "ok": False,
                "error": "package lint failed; amendment rolled back",
                "lint": report.to_dict(),
                "revision": self._revision,
            }

        self._revision += 1
        self._pending_researcher_delta = f"[package r{self._revision:03d}] {researcher_delta}"
        rec = self._snapshot(
            kind,
            summary,
            lint_ok=True,
            verify_score=verify.score if verify else None,
            verify_status=verify.status.value if verify else (verify_error or None),
            detail={**detail, "lint": report.to_dict(), "verify_error": verify_error or None},
        )
        self._write_state()
        return {
            "ok": True,
            "revision": self._revision,
            "lint": report.to_dict(),
            "verify_score": rec.verify_score,
            "verify_status": rec.verify_status,
            "researcher_delta": self._pending_researcher_delta,
        }

    def _merge_instruction(self, current: str, amendment: TaskAmendment) -> str:
        patch = (amendment.patch or "").strip()
        if not patch:
            return current
        # Full replacement when Teacher sends a complete statement.
        if patch.lstrip().startswith("#") and patch.count("\n") >= 8 and len(patch) > 400:
            return patch
        section = (amendment.section or "Amendment").strip()
        marker_start = "<!-- ZERO_LIVE_AMENDMENTS_START -->"
        marker_end = "<!-- ZERO_LIVE_AMENDMENTS_END -->"
        block = (
            f"{marker_start}\n"
            f"## Live package amendments (authoritative)\n\n"
            f"### {section}\n\n{patch}\n"
        )
        if amendment.reason.strip():
            block += f"\n> Reason: {amendment.reason.strip()}\n"
        if amendment.literature_basis.strip():
            block += f"\n> Literature: {amendment.literature_basis.strip()}\n"
        block += f"\n{marker_end}\n"

        if marker_start in current and marker_end in current:
            pre = current.split(marker_start, 1)[0].rstrip()
            post = current.split(marker_end, 1)[1].lstrip()
            # Keep only the latest live block as single source in the marked region.
            return f"{pre}\n\n{block}\n{post}".rstrip() + "\n"
        return current.rstrip() + "\n\n" + block

    def _snapshot(
        self,
        kind: str,
        summary: str,
        *,
        lint_ok: bool,
        verify_score: Optional[float] = None,
        verify_status: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> RevisionRecord:
        rev = self._revision
        dest = self.revisions_dir / f"r{rev:03d}"
        if dest.exists():
            shutil.rmtree(dest)
        if self.live_dir.is_dir():
            shutil.copytree(self.live_dir, dest, ignore=_PACKAGE_IGNORE)
        rec = RevisionRecord(
            revision=rev,
            ts=time.time(),
            kind=kind,
            summary=summary[:500],
            lint_ok=lint_ok,
            verify_score=verify_score,
            verify_status=verify_status,
            detail=detail or {},
        )
        self._history.append(rec)
        (self.revisions_dir / "CHANGELOG.jsonl").open("a", encoding="utf-8").write(
            json.dumps(rec.to_dict(), ensure_ascii=False) + "\n"
        )
        return rec

    def _restore_previous_snapshot(self) -> None:
        if self._revision <= 0:
            return
        src = self.revisions_dir / f"r{self._revision:03d}"
        if not src.is_dir():
            return
        if self.live_dir.exists():
            shutil.rmtree(self.live_dir)
        shutil.copytree(src, self.live_dir, ignore=_PACKAGE_IGNORE)

    def _ensure_revision_budget(self) -> None:
        if self._revision >= self.max_revisions:
            raise RuntimeError(
                f"package revision budget exhausted ({self._revision}/{self.max_revisions})"
            )

    def _write_state(self) -> None:
        state = {
            "revision": self._revision,
            "live_dir": str(self.live_dir),
            "history": [h.to_dict() for h in self._history],
        }
        path = self.run_dir / "package_state.json"
        try:
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _changelog_markdown(self) -> str:
        lines = [
            f"# Package revision history",
            "",
            "Live amendments were linted and verified where possible. "
            "Edits aim at literature fidelity and instruction↔grader coherence, "
            "not raising a single Researcher's score.",
            "",
        ]
        for h in self._history:
            lines.append(f"## r{h.revision:03d} — {h.kind}")
            lines.append("")
            lines.append(f"- summary: {h.summary}")
            lines.append(f"- lint_ok: `{h.lint_ok}`")
            if h.verify_status is not None:
                lines.append(f"- verify: status=`{h.verify_status}` score=`{h.verify_score}`")
            lines.append("")
        return "\n".join(lines)
