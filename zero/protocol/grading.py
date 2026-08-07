"""Deterministic grading artifacts (Harbor-style checkers).

Scoring is an orchestrator concern — not an Agent tool. Teacher only *reviews*
the GradeResult when curating an optimized task package.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class GradeStatus(str, Enum):
    SCORED = "scored"                 # checker ran; reward produced
    MISSING_PACKAGE = "missing_package"  # no task tests/ to grade against
    MISSING_OUTPUTS = "missing_outputs"
    CHECKER_ERROR = "checker_error"
    SKIPPED = "skipped"


class GradeResult(BaseModel):
    """Unified envelope written to ``runs/<id>/grading/result.json``."""

    schema_version: int = 1
    status: GradeStatus
    score: Optional[float] = None          # Harbor reward in [0, 1] when scored
    breakdown: Optional[dict[str, Any]] = None
    reward_path: Optional[str] = None
    breakdown_path: Optional[str] = None
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    mode: str = ""                         # sandbox | host
    tests_dir: Optional[str] = None
    outputs_dir: Optional[str] = None
    sandbox_id: Optional[str] = None
    error: Optional[str] = None
    grade_source: dict[str, Any] = Field(default_factory=dict)
