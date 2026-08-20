"""Cross-run resource registry and immutable lock models."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ResourceKind(str, Enum):
    TOOL = "tool"
    MODEL = "model"
    DATASET = "dataset"


class ArtifactRef(BaseModel):
    type: Literal["oci_image", "object_bundle", "hf_snapshot", "url"]
    uri: str
    digest: Optional[str] = None
    version: Optional[str] = None
    revision: Optional[str] = None
    platform: Optional[str] = None
    format: Optional[str] = None
    size_bytes: Optional[int] = None

    def immutable(self) -> bool:
        """Whether this reference is content-addressed for release purposes."""
        return bool(self.digest and self.digest.startswith("sha256:"))


class VerificationEvidence(BaseModel):
    status: Literal["passed", "failed", "unknown"]
    commands: list[str] = Field(default_factory=list)
    results_digest: Optional[str] = None
    evidence_path: Optional[str] = None


class RegistryCandidate(BaseModel):
    kind: ResourceKind
    resource_unique_key: str
    name: str
    match: Literal["exact", "compatible", "partial"]
    score: Optional[float] = None
    artifact: Optional[ArtifactRef] = None
    capabilities: list[str] = Field(default_factory=list)
    license: Optional[str] = None
    entry_command: Optional[str] = None
    verification: Optional[VerificationEvidence] = None
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResourceLockEntry(BaseModel):
    requirement_id: str
    kind: ResourceKind
    resource_ref: str
    resolution: Literal["existing", "collected", "built"]
    artifact: ArtifactRef
    verification: VerificationEvidence
    provenance: dict[str, Any] = Field(default_factory=dict)


class ResourceLock(BaseModel):
    schema_version: int = 1
    task_id: str
    entries: list[ResourceLockEntry] = Field(default_factory=list)
