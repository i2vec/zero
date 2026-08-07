"""Sandbox provider abstraction.

The interface describes *capabilities* (create, exec, mount, snapshot, ...),
never platform primitives. A ``ResourceRef`` such as ``dataset://name/version``
is resolved to the platform's real storage inside each provider, so the same
Labwright/orchestrator code runs on Docker, a local venv, or (later) K8s.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ResourceRef:
    """Abstract handle to a cached resource, resolved by the provider.

    ``kind`` is ``dataset`` or ``model``; ``host_path`` is where the resource
    physically lives on the orchestration host (populated by Labwright's
    real-time collection into the resource cache).
    """

    kind: str
    name: str
    version: str
    host_path: str

    def uri(self) -> str:
        return f"{self.kind}://{self.name}/{self.version}"

    def target_path(self) -> str:
        """Canonical in-sandbox mount point (doc section 10)."""
        top = "datasets" if self.kind == "dataset" else "models"
        return f"/{top}/{self.name}/{self.version}"


@dataclass
class MountSpec:
    ref: ResourceRef
    read_only: bool = True


@dataclass
class SandboxSpec:
    task_id: str
    sandbox_id: str
    base_image: str
    workspace_host_path: str          # persistent workspace bind-mounted read-write
    mounts: list[MountSpec] = field(default_factory=list)
    cpu_count: int = 2
    memory_gb: int = 8
    gpu_count: int = 0
    python_version: str = "3.11"
    # Dual-sandbox: env = provision/freeze only; exp = Researcher experiments.
    role: str = "env"  # env | exp


@dataclass
class SandboxHandle:
    sandbox_id: str
    backend: str
    workspace_path: str               # path the Researcher/experiment sees
    resource_paths: dict[str, str] = field(default_factory=dict)  # uri -> in-sandbox path
    role: str = "env"
    parent_sandbox_id: Optional[str] = None
    environment_id: Optional[str] = None
    spawn_mode: Optional[str] = None  # from_image | reinstall_from_freeze | venv_clone


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class SandboxInfo:
    sandbox_id: str
    running: bool
    cpu_count: int
    memory_gb: float
    gpu_count: int
    disk_free_gb: float


class SandboxProvider(abc.ABC):
    """Platform-agnostic sandbox lifecycle + execution."""

    name: str = "base"

    @abc.abstractmethod
    def create_sandbox(self, spec: SandboxSpec) -> SandboxHandle: ...

    @abc.abstractmethod
    def exec(self, sandbox_id: str, command: str, timeout: int = 600,
             workdir: Optional[str] = None, env: Optional[dict[str, str]] = None) -> ExecResult: ...

    @abc.abstractmethod
    def put_file(self, sandbox_id: str, path: str, content: bytes) -> None: ...

    @abc.abstractmethod
    def get_file(self, sandbox_id: str, path: str) -> bytes: ...

    @abc.abstractmethod
    def mount(self, sandbox_id: str, mount: MountSpec) -> str:
        """Attach a resource; returns the in-sandbox path."""

    @abc.abstractmethod
    def snapshot(self, sandbox_id: str) -> str:
        """Return a reproducibility digest for the current sandbox state."""

    @abc.abstractmethod
    def destroy(self, sandbox_id: str) -> None: ...

    @abc.abstractmethod
    def info(self, sandbox_id: str) -> SandboxInfo: ...
