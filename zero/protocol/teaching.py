"""Structured protocol for the Teacher agent.

Two modes:

1. **Ask (during solve)** — Researcher asks; Teacher ends with HINT /
   TASK_AMENDMENT / NO_HELP.
2. **Completion review (after grade)** — Orchestrator runs the Harbor grader,
   then Teacher reviews score + statement + checker to emit an optimized task
   package. Amendments must **faithfully follow the source literature** and keep
   statement ↔ grader coherent. Review **improves the package**, not this
   Researcher's score, so a future agent can reproduce the original paper.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class TeachingKind(str, Enum):
    HINT = "HINT"
    TASK_AMENDMENT = "TASK_AMENDMENT"
    NO_HELP = "NO_HELP"
    # Package curation (completion review, and mid-run when live package is enabled):
    NO_CHANGE = "NO_CHANGE"
    GRADER_AMENDMENT = "GRADER_AMENDMENT"
    BOTH_AMENDMENT = "BOTH_AMENDMENT"


class TeacherAsk(BaseModel):
    """One question from the Researcher."""

    ask_id: str
    question: str
    what_i_tried: str = ""
    where_stuck: str = ""


class TaskAmendment(BaseModel):
    """A correction to the task statement, worth keeping after the run."""

    patch: str
    reason: str = ""
    section: Optional[str] = None
    literature_basis: str = ""  # how this restores fidelity to the source paper


class GraderAmendment(BaseModel):
    """A correction to the Harbor grader / grading_spec.

    ``patch`` is the **full final file contents** for ``target`` (e.g. entire
    ``checker.py`` or ``verification_plan.json``). Live package / materialization
    writes that file under ``tests/``; do not send a unified diff as the primary
    contract (legacy diffs are still applied when detected).
    """

    patch: str                          # full final file contents
    reason: str = ""
    target: str = "grading_spec.json"   # grading_spec.json | checker.py | verification_plan.json | test.sh
    literature_basis: str = ""


class TeacherAnswer(BaseModel):
    """Return shape for every ask_teacher call."""

    kind: TeachingKind
    ask_id: str
    content: str = ""
    amendment: Optional[TaskAmendment] = None
    grader_amendment: Optional[GraderAmendment] = None
    asks_used: int = 0
    asks_remaining: int = 0
    package_revision: int = 0
    package_delta: str = ""  # safe change summary for Researcher (no answers)


class CompletionReview(BaseModel):
    """Terminal result of Teacher's post-grade curation turn."""

    kind: TeachingKind
    summary: str = ""
    task_amendment: Optional[TaskAmendment] = None
    grader_amendment: Optional[GraderAmendment] = None
    literature_fidelity_notes: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
