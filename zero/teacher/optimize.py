"""Materialize ``runs/<id>/optimized_task/`` from Teacher completion review."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from zero.protocol.grading import GradeResult
from zero.protocol.teaching import CompletionReview, TeachingKind

# Harbor-style packages often include these trees; copy them wholesale so the
# redistributed optimized_task is runnable / inspectable like the source package.
_PACKAGE_IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.py[cod]",
    ".git",
    ".DS_Store",
    ".pytest_cache",
    "*.egg-info",
)


def materialize_optimized_task(
    *,
    run_dir: Path,
    task_package: Optional[Path],
    task_prompt: str,
    task_key: str,
    review: CompletionReview,
    grade: Optional[GradeResult],
    mid_run_amendments: Optional[list[dict]] = None,
) -> Path:
    """Write a redistributable task package reflecting Teacher's review.

    Principle: every edit must improve fidelity to the **source literature** so
    a future agent can reproduce the original paper from the statement alone.
    Completion review improves the **package** (and keeps instruction ↔ tests
    coherent); it does not accommodate this run's score.

    Seeds from the **full** source package when present (``environment/``,
    ``solution/``, ``calibration/``, ``tests/``, ``paper/``, metadata, …), then
    overwrites ``instruction.md`` and applies grader amendments under ``tests/``.
    The materialized package contains the **final** file contents, not a raw diff.
    """
    out = run_dir / "optimized_task"
    if out.exists():
        shutil.rmtree(out)

    # Seed from original package when available (full tree, not a metadata subset).
    if task_package is not None and task_package.is_dir():
        shutil.copytree(task_package, out, ignore=_PACKAGE_IGNORE)
    else:
        out.mkdir(parents=True, exist_ok=True)

    instruction = _compose_instruction(
        task_prompt=task_prompt,
        task_key=task_key,
        review=review,
        mid_run_amendments=mid_run_amendments or [],
    )
    (out / "instruction.md").write_text(instruction, encoding="utf-8")

    grader_apply: Optional[dict[str, Any]] = None
    if review.grader_amendment is not None and (out / "tests").is_dir():
        grader_apply = _apply_grader_amendment(
            out / "tests",
            review.grader_amendment.target,
            review.grader_amendment.patch,
        )

    (out / "OPTIMIZATION.md").write_text(
        _optimization_md(review, grade, task_key, grader_apply=grader_apply),
        encoding="utf-8",
    )
    provenance = {
        "task_key": task_key,
        "source_package": str(task_package) if task_package else None,
        "review_kind": review.kind.value,
        "grade_status": grade.status.value if grade else None,
        "grade_score": grade.score if grade else None,
        "literature_principle": (
            "All task/grader edits must restore fidelity to the source paper "
            "so an agent can reproduce the original work from the statement."
        ),
        "literature_fidelity_notes": review.literature_fidelity_notes,
        "grader_apply": grader_apply,
    }
    (out / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return out


def _compose_instruction(
    *,
    task_prompt: str,
    task_key: str,
    review: CompletionReview,
    mid_run_amendments: list[dict],
) -> str:
    parts = [
        f"<!-- optimized_task for {task_key}; see OPTIMIZATION.md -->\n\n",
        task_prompt.strip(),
        "\n",
    ]
    patches: list[tuple[str, str, str]] = []
    for rec in mid_run_amendments:
        a = rec.get("amendment") or {}
        patch = (a.get("patch") or "").strip()
        if patch:
            patches.append((
                a.get("section") or "Amendment",
                patch,
                (a.get("literature_basis") or a.get("reason") or "").strip(),
            ))
    if review.task_amendment and review.task_amendment.patch.strip():
        patches.append((
            review.task_amendment.section or "Completion review amendment",
            review.task_amendment.patch.strip(),
            (review.task_amendment.literature_basis or review.task_amendment.reason or "").strip(),
        ))
    if patches:
        parts.append("\n\n---\n\n## Authoritative amendments (literature-aligned)\n\n")
        parts.append(
            "The following amendments override earlier conflicting wording. "
            "They exist to restore fidelity to the source literature.\n"
        )
        for section, patch, basis in patches:
            parts.append(f"\n### {section}\n\n{patch}\n")
            if basis:
                parts.append(f"\n> Literature basis: {basis}\n")
    return "".join(parts)


def _resolve_grader_path(tests_dir: Path, target: str) -> Path:
    """Map Teacher ``target`` to a path under ``tests/`` (no path escape)."""
    raw = (target or "grading_spec.json").strip() or "grading_spec.json"
    p = Path(raw)
    parts = list(p.parts)
    if parts and parts[0] in ("tests", ".", ".."):
        # Drop leading tests/ ; reject ..
        if ".." in parts:
            return tests_dir / "grading_spec.json"
        parts = [x for x in parts if x not in ("tests", ".")]
        p = Path(*parts) if parts else Path("grading_spec.json")

    if p.suffix:
        candidate = (tests_dir / p).resolve()
    else:
        # Symbol / check name (legacy) → checker.py when present.
        if (tests_dir / "checker.py").is_file():
            candidate = (tests_dir / "checker.py").resolve()
        elif (tests_dir / "grading_spec.json").is_file():
            candidate = (tests_dir / "grading_spec.json").resolve()
        else:
            candidate = (tests_dir / "grading_spec.json").resolve()

    root = tests_dir.resolve()
    if not str(candidate).startswith(str(root) + "/") and candidate != root:
        return root / "grading_spec.json"
    return candidate


def _looks_like_unified_diff(text: str) -> bool:
    head = text.lstrip()[:200]
    return head.startswith("--- ") or head.startswith("diff --git ")


def _try_apply_unified_diff(package_root: Path, text: str) -> tuple[bool, str]:
    """Apply unified diff with ``patch -p1`` from ``optimized_task/`` root."""
    payload = text if text.endswith("\n") else text + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8") as fh:
        fh.write(payload)
        diff_path = Path(fh.name)
    try:
        proc = subprocess.run(
            ["patch", "-p1", "--forward", "--batch", "-i", str(diff_path)],
            cwd=str(package_root),
            capture_output=True,
            text=True,
            check=False,
        )
        detail = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, detail.strip()[:2000]
    except OSError as exc:
        return False, f"patch unavailable: {exc}"
    finally:
        diff_path.unlink(missing_ok=True)


def _apply_grader_amendment(tests_dir: Path, target: str, patch: str) -> dict[str, Any]:
    """Write the final grader file. Prefer full-file replacement; apply unified diffs if given."""
    path = _resolve_grader_path(tests_dir, target)
    orig_dir = tests_dir / ".orig"
    orig_dir.mkdir(exist_ok=True)
    if path.is_file():
        shutil.copy2(path, orig_dir / path.name)

    text = patch.strip()
    try:
        resolved_rel = str(path.relative_to(tests_dir.parent))
    except ValueError:
        resolved_rel = path.name
    result: dict[str, Any] = {
        "target_arg": target,
        "resolved_path": resolved_rel,
        "mode": None,
        "ok": False,
        "detail": "",
    }

    if _looks_like_unified_diff(text):
        ok, detail = _try_apply_unified_diff(tests_dir.parent, text)
        result["mode"] = "unified_diff"
        result["ok"] = ok
        result["detail"] = detail
        if not ok:
            # Do not leave a junk file named after a symbol; keep seed copy.
            result["detail"] = (
                f"unified diff apply failed; left original file unchanged. {detail}"
            ).strip()
        return result

    # Full-file replacement (preferred Teacher contract).
    if path.suffix == ".json":
        try:
            parsed = json.loads(text)
            path.write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            result["mode"] = "full_file_json"
            result["ok"] = True
            return result
        except json.JSONDecodeError as exc:
            result["mode"] = "full_file_json"
            result["detail"] = f"invalid JSON: {exc}"
            # Still write raw text so the package shows Teacher intent.
            path.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
            result["ok"] = True
            result["detail"] += "; wrote raw text anyway"
            return result

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
    result["mode"] = "full_file"
    result["ok"] = True
    return result


def _optimization_md(
    review: CompletionReview,
    grade: Optional[GradeResult],
    task_key: str,
    *,
    grader_apply: Optional[dict[str, Any]] = None,
) -> str:
    lines = [
        f"# Optimization report: `{task_key}`",
        "",
        "## Principle",
        "",
        "Every change to the task statement or grader must **follow the source",
        "literature** more closely, so a future agent can reproduce the original",
        "paper from the statement without tribal knowledge. Completion review",
        "improves the **package** (statement ↔ grader coherent); it does not",
        "raise this Researcher's score.",
        "",
        "## Review outcome",
        "",
        f"- kind: `{review.kind.value}`",
        f"- summary: {review.summary or '(none)'}",
        "",
    ]
    if grade is not None:
        lines.extend([
            "## Grading",
            "",
            f"- status: `{grade.status.value}`",
            f"- score: `{grade.score}`",
            f"- mode: `{grade.mode}`",
            "",
        ])
    if review.literature_fidelity_notes:
        lines.extend(["## Literature fidelity", "", review.literature_fidelity_notes, ""])
    if review.task_amendment:
        lines.extend([
            "## Task amendment",
            "",
            review.task_amendment.patch,
            "",
            f"> reason: {review.task_amendment.reason}",
            f"> literature_basis: {review.task_amendment.literature_basis}",
            "",
        ])
    if review.grader_amendment:
        lines.extend([
            "## Grader amendment",
            "",
            f"- target: `{review.grader_amendment.target}`",
            f"> reason: {review.grader_amendment.reason}",
            f"> literature_basis: {review.grader_amendment.literature_basis}",
            "",
        ])
        if grader_apply:
            lines.extend([
                "### Apply result",
                "",
                f"- resolved_path: `{grader_apply.get('resolved_path')}`",
                f"- mode: `{grader_apply.get('mode')}`",
                f"- ok: `{grader_apply.get('ok')}`",
            ])
            if grader_apply.get("detail"):
                lines.append(f"- detail: {grader_apply['detail']}")
            lines.append("")
        lines.extend([
            "### Teacher-supplied content (full)",
            "",
            "```",
            review.grader_amendment.patch,
            "```",
            "",
        ])
    if review.kind == TeachingKind.NO_CHANGE:
        lines.extend([
            "## No change",
            "",
            "Task statement and grader are considered literature-faithful;",
            "any low score is attributed to the solve attempt, not the package.",
            "",
        ])
    return "\n".join(lines)
