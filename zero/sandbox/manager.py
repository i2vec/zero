"""SandboxManager: single owner of the provider + versioned sandbox registry.

Both Labwright (provision) and the Researcher's ``run_in_sandbox`` tool go
through here. Workspaces are keyed on ``task_id`` and outlive any individual
sandbox version (doc section 16 / principle 4).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from zero.config import Config
from zero.sandbox.base import (
    ExecResult,
    MountSpec,
    SandboxHandle,
    SandboxInfo,
    SandboxProvider,
    SandboxSpec,
)
from zero.sandbox.docker_provider import DockerProvider
from zero.sandbox.lbg_provider import LbgProvider
from zero.sandbox.local_provider import LocalProvider


class SandboxManager:
    def __init__(self, config: Config):
        self._config = config
        backend = config.resolved_backend()
        if backend == "docker":
            self._provider: SandboxProvider = DockerProvider(config.docker_base_image)
        elif backend == "lbg":
            self._provider = LbgProvider(config)
        else:
            self._provider = LocalProvider(config)
        self._lock = threading.Lock()
        self._handles: dict[str, SandboxHandle] = {}
        self._sandbox_task: dict[str, str] = {}
        self._version: dict[str, int] = {}

    @property
    def backend(self) -> str:
        return self._provider.name

    def workspace_for(self, task_id: str) -> str:
        ws = self._config.ensure_run_dirs(task_id) / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        return str(ws)

    def next_sandbox_id(self, task_id: str) -> str:
        with self._lock:
            v = self._version.get(task_id, 0) + 1
            self._version[task_id] = v
        short = task_id.replace("task-", "")
        return f"sandbox-{short}-v{v}"

    def create(
        self,
        task_id: str,
        *,
        base_image: str,
        mounts: Optional[list[MountSpec]] = None,
        cpu_count: int = 2,
        memory_gb: int = 8,
        gpu_count: int = 0,
        python_version: str = "3.11",
        sandbox_id: Optional[str] = None,
    ) -> SandboxHandle:
        sandbox_id = sandbox_id or self.next_sandbox_id(task_id)
        spec = SandboxSpec(
            task_id=task_id,
            sandbox_id=sandbox_id,
            base_image=base_image,
            workspace_host_path=self.workspace_for(task_id),
            mounts=mounts or [],
            cpu_count=cpu_count,
            memory_gb=memory_gb,
            gpu_count=gpu_count,
            python_version=python_version,
        )
        handle = self._provider.create_sandbox(spec)
        with self._lock:
            self._handles[sandbox_id] = handle
            self._sandbox_task[sandbox_id] = task_id
        return handle

    def get_handle(self, sandbox_id: str) -> Optional[SandboxHandle]:
        return self._handles.get(sandbox_id)

    def task_of(self, sandbox_id: str) -> Optional[str]:
        return self._sandbox_task.get(sandbox_id)

    def exec(self, sandbox_id: str, command: str, timeout: int = 600,
             workdir: Optional[str] = None, env: Optional[dict[str, str]] = None) -> ExecResult:
        return self._provider.exec(sandbox_id, command, timeout=timeout, workdir=workdir, env=env)

    def put_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        self._provider.put_file(sandbox_id, path, content)

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        return self._provider.get_file(sandbox_id, path)

    def mount(self, sandbox_id: str, mount: MountSpec) -> str:
        path = self._provider.mount(sandbox_id, mount)
        handle = self._handles.get(sandbox_id)
        if handle is not None:
            handle.resource_paths[mount.ref.uri()] = path
        return path

    def snapshot(self, sandbox_id: str) -> str:
        return self._provider.snapshot(sandbox_id)

    def destroy(self, sandbox_id: str) -> None:
        self._provider.destroy(sandbox_id)
        with self._lock:
            self._handles.pop(sandbox_id, None)
            self._sandbox_task.pop(sandbox_id, None)

    def info(self, sandbox_id: str) -> SandboxInfo:
        return self._provider.info(sandbox_id)
