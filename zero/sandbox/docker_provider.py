"""Docker sandbox backend (doc section 5.1).

A long-lived container per sandbox (``sleep infinity``), the workspace bind
mounted read-write, datasets/models bind mounted read-only at the canonical
``/datasets`` and ``/models`` paths, ``docker exec`` for commands, and
``docker commit`` as a snapshot cache. The authoritative reproducibility source
remains the package lock + manifest, not the commit.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from zero.sandbox.base import (
    ExecResult,
    MountSpec,
    SandboxHandle,
    SandboxInfo,
    SandboxProvider,
    SandboxSpec,
)


class DockerProvider(SandboxProvider):
    name = "docker"

    def __init__(self, base_image: str = "python:3.11-slim"):
        self._base_image = base_image
        self._specs: dict[str, SandboxSpec] = {}

    @staticmethod
    def _container(sandbox_id: str) -> str:
        return f"zero-{sandbox_id}"

    @staticmethod
    def _docker(args: list[str], timeout: int = 600, input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", *args], capture_output=True, timeout=timeout, input=input_bytes,
        )

    def create_sandbox(self, spec: SandboxSpec) -> SandboxHandle:
        self._specs[spec.sandbox_id] = spec
        name = self._container(spec.sandbox_id)
        # Remove any stale container with the same name.
        self._docker(["rm", "-f", name], timeout=60)

        args = ["run", "-d", "--name", name,
                "-v", f"{spec.workspace_host_path}:/workspace:rw",
                "-w", "/workspace"]
        if spec.gpu_count > 0:
            args += ["--gpus", "all"]
        args += [f"--cpus={spec.cpu_count}", f"--memory={spec.memory_gb}g"]

        handle = SandboxHandle(
            sandbox_id=spec.sandbox_id, backend=self.name, workspace_path="/workspace",
        )
        for m in spec.mounts:
            target = m.ref.target_path()
            mode = "ro" if m.read_only else "rw"
            args += ["-v", f"{m.ref.host_path}:{target}:{mode}"]
            handle.resource_paths[m.ref.uri()] = target

        args += [spec.base_image or self._base_image, "sleep", "infinity"]
        proc = self._docker(args, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed: {proc.stderr.decode(errors='replace')}")
        return handle

    def exec(self, sandbox_id: str, command: str, timeout: int = 600,
             workdir: Optional[str] = None, env: Optional[dict[str, str]] = None) -> ExecResult:
        args = ["exec"]
        if workdir:
            args += ["-w", workdir]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        args += [self._container(sandbox_id), "bash", "-lc", command]
        try:
            proc = self._docker(args, timeout=timeout)
            return ExecResult(
                proc.returncode,
                proc.stdout.decode(errors="replace"),
                proc.stderr.decode(errors="replace"),
            )
        except subprocess.TimeoutExpired:
            return ExecResult(124, "", f"timeout after {timeout}s")

    def put_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        target = path if path.startswith("/") else f"/workspace/{path}"
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(content)
            tmp.flush()
            self.exec(sandbox_id, f"mkdir -p {str(Path(target).parent)!r}")
            proc = self._docker(["cp", tmp.name, f"{self._container(sandbox_id)}:{target}"], timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(f"docker cp failed: {proc.stderr.decode(errors='replace')}")

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        target = path if path.startswith("/") else f"/workspace/{path}"
        with tempfile.NamedTemporaryFile() as tmp:
            proc = self._docker(["cp", f"{self._container(sandbox_id)}:{target}", tmp.name], timeout=120)
            if proc.returncode != 0:
                raise RuntimeError(f"docker cp failed: {proc.stderr.decode(errors='replace')}")
            return Path(tmp.name).read_bytes()

    def mount(self, sandbox_id: str, mount: MountSpec) -> str:
        # Docker mounts are fixed at container creation; adding a resource means
        # a new sandbox version. Return the canonical target path.
        return mount.ref.target_path()

    def snapshot(self, sandbox_id: str) -> str:
        image = f"zero-sandbox:{sandbox_id}"
        proc = self._docker(["commit", self._container(sandbox_id), image], timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"docker commit failed: {proc.stderr.decode(errors='replace')}")
        digest = proc.stdout.decode(errors="replace").strip()
        return digest or f"docker:{image}"

    def destroy(self, sandbox_id: str) -> None:
        self._docker(["rm", "-f", self._container(sandbox_id)], timeout=60)
        self._specs.pop(sandbox_id, None)

    def info(self, sandbox_id: str) -> SandboxInfo:
        spec = self._specs.get(sandbox_id)
        state = self._docker(["inspect", "-f", "{{.State.Running}}", self._container(sandbox_id)], timeout=30)
        running = state.returncode == 0 and state.stdout.decode().strip() == "true"
        disk = self.exec(sandbox_id, "df -Pk /workspace | tail -1 | awk '{print $4}'") if running else None
        disk_free = float(disk.stdout.strip()) / (1024 ** 2) if disk and disk.ok and disk.stdout.strip() else 0.0
        return SandboxInfo(
            sandbox_id=sandbox_id,
            running=running,
            cpu_count=spec.cpu_count if spec else 0,
            memory_gb=float(spec.memory_gb) if spec else 0.0,
            gpu_count=spec.gpu_count if spec else 0,
            disk_free_gb=round(disk_free, 1),
        )
