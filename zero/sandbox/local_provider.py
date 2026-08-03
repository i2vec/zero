"""Local sandbox backend (no Docker required).

Each sandbox lives under ``runs/<task_id>/sandboxes/<sandbox_id>/`` with a
dedicated Python virtualenv. Commands run on the host with that venv's ``bin``
prepended to PATH and cwd defaulting to the task workspace.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
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


class LocalProvider(SandboxProvider):
    name = "local"

    def __init__(self, config: Config):
        self._config = config
        self._roots: dict[str, Path] = {}
        self._workspaces: dict[str, str] = {}

    def _dir(self, sandbox_id: str) -> Path:
        if sandbox_id in self._roots:
            return self._roots[sandbox_id]
        # Recover path after process restart: scan run sandboxes dirs.
        for path in self._config.runs_dir.glob(f"*/sandboxes/{sandbox_id}"):
            if path.is_dir():
                self._roots[sandbox_id] = path
                return path
        # Last resort (should not happen mid-run): place under runs/_orphan/
        orphan = self._config.runs_dir / "_orphan" / "sandboxes" / sandbox_id
        self._roots[sandbox_id] = orphan
        return orphan

    def _venv(self, sandbox_id: str) -> Path:
        return self._dir(sandbox_id) / "venv"

    def _venv_bin(self, sandbox_id: str) -> Path:
        return self._venv(sandbox_id) / "bin"

    def create_sandbox(self, spec: SandboxSpec) -> SandboxHandle:
        sdir = self._config.run_sandboxes_dir(spec.task_id) / spec.sandbox_id
        self._roots[spec.sandbox_id] = sdir
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "datasets").mkdir(exist_ok=True)
        (sdir / "models").mkdir(exist_ok=True)

        venv = self._venv(spec.sandbox_id)
        if not venv.exists():
            subprocess.run(
                [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
                check=True, capture_output=True,
            )

        self._workspaces[spec.sandbox_id] = spec.workspace_host_path
        Path(spec.workspace_host_path).mkdir(parents=True, exist_ok=True)

        handle = SandboxHandle(
            sandbox_id=spec.sandbox_id,
            backend=self.name,
            workspace_path=spec.workspace_host_path,
        )
        for m in spec.mounts:
            handle.resource_paths[m.ref.uri()] = self.mount(spec.sandbox_id, m)
        return handle

    def exec(self, sandbox_id: str, command: str, timeout: int = 600,
             workdir: Optional[str] = None, env: Optional[dict[str, str]] = None) -> ExecResult:
        bin_dir = self._venv_bin(sandbox_id)
        run_env = os.environ.copy()
        run_env["PATH"] = f"{bin_dir}:{run_env.get('PATH', '')}"
        run_env["VIRTUAL_ENV"] = str(self._venv(sandbox_id))
        run_env["SANDBOX_ID"] = sandbox_id
        run_env["SANDBOX_ROOT"] = str(self._dir(sandbox_id))
        if env:
            run_env.update(env)
        cwd = workdir or self._workspaces.get(sandbox_id) or str(self._dir(sandbox_id))
        try:
            proc = subprocess.run(
                ["bash", "-lc", command],
                cwd=cwd, env=run_env, capture_output=True, text=True, timeout=timeout,
            )
            return ExecResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as exc:
            return ExecResult(124, exc.stdout or "", f"timeout after {timeout}s")

    def put_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        target = self._resolve(sandbox_id, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        return self._resolve(sandbox_id, path).read_bytes()

    def _resolve(self, sandbox_id: str, path: str) -> Path:
        p = Path(path)
        if p.is_absolute():
            return p
        ws = self._workspaces.get(sandbox_id, str(self._dir(sandbox_id)))
        return Path(ws) / p

    def mount(self, sandbox_id: str, mount: MountSpec) -> str:
        ref = mount.ref
        top = "datasets" if ref.kind == "dataset" else "models"
        link = self._dir(sandbox_id) / top / ref.name / ref.version
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            if link.is_symlink():
                link.unlink()
            else:
                shutil.rmtree(link, ignore_errors=True)
        os.symlink(ref.host_path, link, target_is_directory=True)
        return str(link)

    def snapshot(self, sandbox_id: str) -> str:
        freeze = self.exec(sandbox_id, "pip freeze", timeout=120)
        digest = hashlib.sha256(freeze.stdout.encode("utf-8")).hexdigest()[:16]
        (self._dir(sandbox_id) / "lock.txt").write_text(freeze.stdout, encoding="utf-8")
        return f"local:{digest}"

    def destroy(self, sandbox_id: str) -> None:
        shutil.rmtree(self._dir(sandbox_id), ignore_errors=True)
        self._workspaces.pop(sandbox_id, None)
        self._roots.pop(sandbox_id, None)

    def info(self, sandbox_id: str) -> SandboxInfo:
        usage = shutil.disk_usage(str(self._dir(sandbox_id)))
        try:
            mem_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
        except (ValueError, OSError):
            mem_gb = 0.0
        return SandboxInfo(
            sandbox_id=sandbox_id,
            running=self._dir(sandbox_id).exists(),
            cpu_count=os.cpu_count() or 1,
            memory_gb=round(mem_gb, 1),
            gpu_count=0,
            disk_free_gb=round(usage.free / (1024 ** 3), 1),
        )
