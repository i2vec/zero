"""Structured protocol for the Teacher agent.

The Researcher asks one question; the Teacher ends its turn with exactly one
answer of three kinds:

- ``HINT``            operational guidance (method level, disclosed gradually)
- ``TASK_AMENDMENT``  the *task statement itself* is defective — here is the fix
- ``NO_HELP``         nothing useful to add, or the ask budget is spent

``TASK_AMENDMENT`` is the interesting one: it is not just an answer to the
Researcher, it is a durable artifact. Solving a task is how we find out what the
task failed to say.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class TeachingKind(str, Enum):
    HINT = "HINT"
    TASK_AMENDMENT = "TASK_AMENDMENT"
    NO_HELP = "NO_HELP"


class TeacherAsk(BaseModel):
    """One question from the Researcher."""

    ask_id: str
    question: str
    what_i_tried: str = ""
    where_stuck: str = ""


class TaskAmendment(BaseModel):
    """A correction to the task statement, worth keeping after the run."""

    patch: str                          # the corrected / added statement text
    reason: str = ""                    # what the original statement got wrong
    section: Optional[str] = None       # which part of the task it amends


class TeacherAnswer(BaseModel):
    """Return shape for every ask_teacher call."""

    kind: TeachingKind
    ask_id: str
    content: str = ""
    amendment: Optional[TaskAmendment] = None
    asks_used: int = 0
    asks_remaining: int = 0
