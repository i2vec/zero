"""Locate and validate Harbor-style task packages."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional


_INSTRUCTION_NAMES = ("instruction.md", "instruction.markdown")


class ResolvedTaskPackage(NamedTuple):
    """A task package directory + loaded instruction text."""

    package: Path
    instruction_path: Path
    prompt: str


def resolve_task_package(task_arg: str | Path) -> ResolvedTaskPackage:
    """Require a package directory containing ``instruction.md``.

    Only package directories are supported (no inline prompts, no bare files).
    """
    raw = str(task_arg or "").strip()
    if not raw:
        raise ValueError("task package path is empty")
    path = Path(raw).expanduser()
    try:
        path = path.resolve()
    except OSError as exc:
        raise ValueError(f"invalid task package path: {raw}") from exc

    if path.is_file():
        raise ValueError(
            f"expected a task package directory, got a file: {path}\n"
            f"Pass the package folder instead, e.g. `{path.parent}`."
        )
    if not path.is_dir():
        raise ValueError(f"task package directory not found: {path}")

    instruction = _find_instruction(path)
    if instruction is None:
        names = " / ".join(_INSTRUCTION_NAMES)
        raise ValueError(
            f"task package missing {names}: {path}\n"
            "A package must contain instruction.md (Researcher prompt) and "
            "preferably tests/ (Harbor grader for scoring + Teacher review)."
        )
    text = instruction.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        raise ValueError(f"instruction file is empty: {instruction}")
    return ResolvedTaskPackage(package=path, instruction_path=instruction, prompt=text)


def infer_task_package(task_arg: str | Path | None) -> Optional[Path]:
    """Backward-compatible helper: resolve package dir or return None."""
    if task_arg is None:
        return None
    try:
        return resolve_task_package(task_arg).package
    except ValueError:
        path = Path(task_arg).expanduser()
        try:
            path = path.resolve()
        except OSError:
            return None
        if path.is_file() and path.name.lower() in _INSTRUCTION_NAMES:
            return path.parent
        if path.is_dir() and _find_instruction(path) is not None:
            return path
        return None


def tests_dir(package: Path | None) -> Optional[Path]:
    if package is None:
        return None
    td = package / "tests"
    return td if td.is_dir() and (td / "checker.py").is_file() else None


def default_hints_path(package: Path) -> Optional[Path]:
    """Prefer ``paper/paper.md``, else a non-empty ``paper/`` directory of ``*.md``."""
    paper_md = package / "paper" / "paper.md"
    if paper_md.is_file():
        return paper_md
    paper_dir = package / "paper"
    if paper_dir.is_dir() and any(paper_dir.glob("*.md")):
        return paper_dir
    return None


def _find_instruction(package: Path) -> Optional[Path]:
    for name in _INSTRUCTION_NAMES:
        candidate = package / name
        if candidate.is_file():
            return candidate
    return None
