"""EnvironmentSpec: what the Researcher declares it needs (doc section 12).

The Researcher describes *what* it wants (constraints), never *how* to install
it. Labwright turns this into a real, verified sandbox.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class BaseEnvironment(BaseModel):
    python: str = "3.11"
    cuda: Optional[str] = None


class PackageRequest(BaseModel):
    name: str
    constraint: Optional[str] = None  # e.g. ">=2.0"


class ToolRequest(BaseModel):
    name: str
    version: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    license: Optional[str] = None
    platform: Optional[str] = None
    resource_unique_key: Optional[str] = None
    allow_compatible: bool = False


class ModelRequest(BaseModel):
    name: str
    revision: Optional[str] = None
    precision: Optional[str] = None  # fp16 / int4 / ...
    # Optional explicit source to disambiguate (e.g. hf repo id). When absent
    # and resolution is ambiguous, Labwright escalates NEEDS_DECISION.
    source: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    license: Optional[str] = None
    platform: Optional[str] = None
    resource_unique_key: Optional[str] = None
    allow_compatible: bool = False


class DatasetRequest(BaseModel):
    name: str
    version: Optional[str] = None
    access: Literal["read_only", "read_write"] = "read_only"
    source: Optional[str] = None
    capabilities: list[str] = Field(default_factory=list)
    license: Optional[str] = None
    platform: Optional[str] = None
    resource_unique_key: Optional[str] = None
    allow_compatible: bool = False


class ComputeSpec(BaseModel):
    gpu_count: int = 0
    gpu_memory_gb: int = 0
    memory_gb: int = 8
    cpu_count: int = 2


class EnvironmentSpec(BaseModel):
    experiment_id: str
    base_environment: BaseEnvironment = Field(default_factory=BaseEnvironment)
    packages: list[PackageRequest] = Field(default_factory=list)
    tools: list[ToolRequest] = Field(default_factory=list)
    models: list[ModelRequest] = Field(default_factory=list)
    datasets: list[DatasetRequest] = Field(default_factory=list)
    compute: ComputeSpec = Field(default_factory=ComputeSpec)


class ResourceAddition(BaseModel):
    """A single resource to add to an existing sandbox via add_resources."""

    type: Literal["python_package", "tool", "model", "dataset"]
    name: str
    constraint: Optional[str] = None
    version: Optional[str] = None
    revision: Optional[str] = None
    precision: Optional[str] = None
    source: Optional[str] = None
    reason: Optional[str] = None
