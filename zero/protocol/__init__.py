"""Structured protocol between Researcher and Labwright.

Only these objects cross the context boundary between the two agents:
declarative ``EnvironmentSpec`` in, verified ``EnvironmentManifest`` out,
plus the status enum and the decision payloads. Install logs never cross.
"""

from zero.protocol.spec import (
    BaseEnvironment,
    ComputeSpec,
    DatasetRequest,
    EnvironmentSpec,
    ModelRequest,
    PackageRequest,
    ResourceAddition,
    ToolRequest,
)
from zero.protocol.manifest import (
    DatasetEntry,
    EnvironmentManifest,
    ModelEntry,
    PackageEntry,
    ToolEntry,
    VerificationReport,
)
from zero.protocol.status import (
    DecisionCandidate,
    DecisionRequest,
    EnvironmentStatus,
    EnvironmentResponse,
    ResearcherDecision,
)
from zero.protocol.hashing import spec_hash

__all__ = [
    "BaseEnvironment",
    "ComputeSpec",
    "DatasetRequest",
    "EnvironmentSpec",
    "ModelRequest",
    "PackageRequest",
    "ResourceAddition",
    "ToolRequest",
    "DatasetEntry",
    "EnvironmentManifest",
    "ModelEntry",
    "PackageEntry",
    "ToolEntry",
    "VerificationReport",
    "DecisionCandidate",
    "DecisionRequest",
    "EnvironmentStatus",
    "EnvironmentResponse",
    "ResearcherDecision",
    "spec_hash",
]
