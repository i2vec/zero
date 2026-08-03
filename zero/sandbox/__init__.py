"""Platform-agnostic sandbox execution layer.

Upper layers (orchestrator + Labwright) only ever touch ``SandboxProvider`` and
``ResourceRef``; every platform difference is sealed inside a driver. MVP ships
``DockerProvider`` plus a ``LocalProvider`` fallback for hosts without Docker.
"""

from zero.sandbox.base import (
    ExecResult,
    MountSpec,
    ResourceRef,
    SandboxHandle,
    SandboxInfo,
    SandboxProvider,
    SandboxSpec,
)

__all__ = [
    "ExecResult",
    "MountSpec",
    "ResourceRef",
    "SandboxHandle",
    "SandboxInfo",
    "SandboxProvider",
    "SandboxSpec",
]
