"""Fixed-format environment inventory produced when Labwright freezes an env."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ImageRecord(BaseModel):
    status: str = "unknown"            # ready | submitted | pending | failed | not_publishable
    url: Optional[str] = None
    reference: Optional[str] = None
    content_digest: Optional[str] = None
    provider_commit_id: Optional[str] = None
    kind: Optional[str] = None
    note: Optional[str] = None


class ToolInventoryEntry(BaseModel):
    name: str
    command: Optional[str] = None
    version: Optional[str] = None
    path: Optional[str] = None
    verified: bool = False


class MountInventoryEntry(BaseModel):
    kind: str
    name: str
    path: str
    source: Optional[str] = None
    sha256: Optional[str] = None
    revision: Optional[str] = None
    read_only: bool = True


class EnvironmentInventory(BaseModel):
    """Machine-readable inventory; ``environment.md`` is the human twin."""

    schema_version: int = 1
    environment_id: str
    task_id: str
    sandbox_id: str
    backend: str
    scope: str = "published_manifest"  # published_manifest | clean_baseline (P2)
    snapshot_timing: str = "Labwright publish_manifest"
    base_image: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    packages: dict[str, str] = Field(default_factory=dict)       # name -> version
    pip_freeze: list[str] = Field(default_factory=list)
    tools: list[ToolInventoryEntry] = Field(default_factory=list)
    mounts: list[MountInventoryEntry] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    image: ImageRecord = Field(default_factory=ImageRecord)
    files: dict[str, str] = Field(default_factory=dict)          # relative paths of side files
    notes: list[str] = Field(default_factory=list)
