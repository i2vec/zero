"""Status enum + async request/decision payloads (doc sections 14, 19)."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from zero.protocol.manifest import EnvironmentManifest


class EnvironmentStatus(str, Enum):
    ENVIRONMENT_READY = "ENVIRONMENT_READY"
    RESOURCE_ADDED = "RESOURCE_ADDED"
    NEEDS_DECISION = "NEEDS_DECISION"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    ENVIRONMENT_BLOCKED = "ENVIRONMENT_BLOCKED"
    ENVIRONMENT_FAILED = "ENVIRONMENT_FAILED"


class DecisionCandidate(BaseModel):
    id: str
    source: str
    note: Optional[str] = None
    precision: Optional[str] = None
    version: Optional[str] = None


class DecisionRequest(BaseModel):
    """Raised by Labwright when it needs Researcher input mid-turn.

    Two shapes, both delivered the same way (Labwright ends its turn, control
    returns to the Researcher):
    - Structured choice: ``candidates`` lists concrete options to pick from.
    - Open-ended question: ``question`` is free text when there is nothing to
      enumerate (e.g. a scientific trade-off). ``candidates`` may be empty.
    """

    resource_type: str            # model / dataset / tool
    resource_name: str
    reason: str
    candidates: list[DecisionCandidate] = Field(default_factory=list)
    scientific_impact: Optional[str] = None
    question: Optional[str] = None    # open-ended ask when candidates is empty


class ResearcherDecision(BaseModel):
    """The structured reply from the Researcher to a DecisionRequest."""

    choose: Optional[str] = None       # candidate id
    use_source: Optional[str] = None   # explicit source override
    accept: Optional[str] = None       # accept a named alternative (e.g. "int4")
    guidance: Optional[str] = None     # free-text answer to an open-ended question
    abort: bool = False


class EnvironmentResponse(BaseModel):
    """Return shape for every Labwright interface call."""

    status: EnvironmentStatus
    request_id: str
    sandbox_id: Optional[str] = None
    manifest: Optional[EnvironmentManifest] = None
    decision: Optional[DecisionRequest] = None
    message: Optional[str] = None
    detail: dict[str, Any] = Field(default_factory=dict)
