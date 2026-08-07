"""SandboxManager: single owner of the provider + versioned sandbox registry.

Dual-sandbox lifecycle (P2):

1. Labwright creates an **env** sandbox (scratch workspace) and installs packages.
2. ``publish_manifest`` freezes that env (inventory + image commit) and spawns an
   **exp** sandbox for the Researcher with a clean experiment workspace.
3. Snapshots are refused on exp sandboxes; mid-run dependency changes require a
   new env revision, then a new exp.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any, Optional

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
        self._roles: dict[str, str] = {}
        self._env_parent: dict[str, str] = {}  # exp_id -> env_id
        self._specs: dict[str, SandboxSpec] = {}

    @property
    def backend(self) -> str:
        return self._provider.name

    def workspace_for(self, task_id: str, *, role: str = "exp") -> str:
        """Host workspace path. Env uses scratch; exp uses the Researcher tree."""
        if role == "env":
            ws = self._config.ensure_run_dirs(task_id) / "environment" / "scratch"
        else:
            ws = self._config.ensure_run_dirs(task_id) / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        return str(ws)

    def next_sandbox_id(self, task_id: str) -> str:
        with self._lock:
            v = self._version.get(task_id, 0) + 1
            self._version[task_id] = v
        short = task_id.replace("task-", "")
        return f"sandbox-{short}-v{v}"

    def role_of(self, sandbox_id: str) -> Optional[str]:
        return self._roles.get(sandbox_id)

    def env_parent_of(self, exp_sandbox_id: str) -> Optional[str]:
        return self._env_parent.get(exp_sandbox_id)

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
        role: str = "env",
        parent_sandbox_id: Optional[str] = None,
        environment_id: Optional[str] = None,
        spawn_mode: Optional[str] = None,
    ) -> SandboxHandle:
        role = "exp" if role == "exp" else "env"
        sandbox_id = sandbox_id or self.next_sandbox_id(task_id)
        spec = SandboxSpec(
            task_id=task_id,
            sandbox_id=sandbox_id,
            base_image=base_image,
            workspace_host_path=self.workspace_for(task_id, role=role),
            mounts=mounts or [],
            cpu_count=cpu_count,
            memory_gb=memory_gb,
            gpu_count=gpu_count,
            python_version=python_version,
            role=role,
        )
        handle = self._provider.create_sandbox(spec)
        handle.role = role
        handle.parent_sandbox_id = parent_sandbox_id
        handle.environment_id = environment_id
        handle.spawn_mode = spawn_mode
        with self._lock:
            self._handles[sandbox_id] = handle
            self._sandbox_task[sandbox_id] = task_id
            self._roles[sandbox_id] = role
            self._specs[sandbox_id] = spec
            if parent_sandbox_id and role == "exp":
                self._env_parent[sandbox_id] = parent_sandbox_id
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
        role = self._roles.get(sandbox_id, "env")
        if role == "exp":
            raise RuntimeError(
                f"refusing to snapshot experiment sandbox {sandbox_id}; "
                "freeze only env sandboxes (dual-sandbox clean baseline)"
            )
        return self._provider.snapshot(sandbox_id)

    def prepare_env_for_freeze(self, sandbox_id: str) -> None:
        """Clear experiment-ish paths inside an env sandbox before image commit.

        Docker bind-mounts usually exclude /workspace from commit already; LBG
        stores /workspace inside the image, so wiping matters there.
        """
        try:
            self.exec(
                sandbox_id,
                "rm -rf /workspace/* /workspace/.[!.]* /workspace/..?* 2>/dev/null || true; "
                "mkdir -p /workspace /app/outputs /tmp/labwright-scratch && "
                "chmod 777 /workspace /app /app/outputs /tmp/labwright-scratch",
                timeout=120,
            )
        except Exception:  # noqa: BLE001
            pass

    def resolve_snapshot(self, sandbox_id: str, digest: str, *, timeout: int = 1800) -> dict[str, Any]:
        """Materialize a provider snapshot into a reusable image reference."""
        if self.backend == "lbg" and digest.startswith("lbg:commit:"):
            commit_id = digest.rsplit(":", 1)[-1]
            wait_for_image = getattr(self._provider, "wait_for_image", None)
            if not callable(wait_for_image):
                return {
                    "kind": "lbg_commit",
                    "commit_id": commit_id,
                    "status": "unsupported",
                    "url": None,
                }
            record = wait_for_image(commit_id, timeout=timeout)
            status = record.get("status")
            return {
                "kind": "lbg_commit",
                "commit_id": commit_id,
                "status": "ready" if status == 2 else ("failed" if status == 3 else "pending"),
                "url": record.get("imageUrl"),
                "error": record.get("errorMsg") or record.get("statusReason"),
                "provider_record": record,
            }
        if self.backend == "docker":
            return {
                "kind": "docker_local",
                "status": "ready",
                "url": None,
                "reference": f"zero-sandbox:{sandbox_id}",
                "digest": digest,
            }
        return {
            "kind": "reproducibility_digest",
            "status": "not_publishable",
            "url": None,
            "digest": digest,
        }

    def spawn_experiment_sandbox(
        self,
        *,
        task_id: str,
        env_sandbox_id: str,
        digest: str,
        mounts: Optional[list[MountSpec]] = None,
        environment_id: Optional[str] = None,
        python_version: str = "3.11",
        cpu_count: int = 2,
        memory_gb: int = 8,
        gpu_count: int = 0,
        pip_freeze: Optional[list[str]] = None,
    ) -> SandboxHandle:
        """Create the Researcher-facing exp sandbox from a frozen env.

        Prefer launching from the published image. If the image URL is not yet
        available (common on LBG), fall back to a fresh sandbox + pip reinstall
        from the freeze list, and record ``spawn_mode`` accordingly.
        """
        parent_spec = self._specs.get(env_sandbox_id)
        cpu_count = parent_spec.cpu_count if parent_spec else cpu_count
        memory_gb = parent_spec.memory_gb if parent_spec else memory_gb
        gpu_count = parent_spec.gpu_count if parent_spec else gpu_count
        python_version = (
            parent_spec.python_version if parent_spec else python_version
        )
        inherited_mounts = list(mounts or (parent_spec.mounts if parent_spec else []))

        spawn_wait = int(getattr(self._config, "lbg_spawn_wait_timeout", 300) or 300)
        image_info = self.resolve_snapshot(env_sandbox_id, digest, timeout=spawn_wait)
        base_image, spawn_mode = self._choose_exp_base_image(
            env_sandbox_id, digest, image_info,
        )

        handle = self.create(
            task_id,
            base_image=base_image,
            mounts=inherited_mounts,
            cpu_count=cpu_count,
            memory_gb=memory_gb,
            gpu_count=gpu_count,
            python_version=python_version,
            role="exp",
            parent_sandbox_id=env_sandbox_id,
            environment_id=environment_id,
            spawn_mode=spawn_mode,
        )

        if spawn_mode == "venv_clone" and self.backend == "local":
            self._clone_local_venv(env_sandbox_id, handle.sandbox_id)
        elif spawn_mode == "reinstall_from_freeze" and pip_freeze:
            self._reinstall_from_freeze(handle.sandbox_id, pip_freeze)

        # Ensure Harbor paths exist on the fresh exp sandbox.
        try:
            self.exec(
                handle.sandbox_id,
                "mkdir -p /workspace /app/outputs && chmod 777 /workspace /app /app/outputs",
                timeout=60,
            )
        except Exception:  # noqa: BLE001
            pass

        # Re-apply mounts that backends cannot express at create time (lbg).
        for m in inherited_mounts:
            try:
                self.mount(handle.sandbox_id, m)
            except Exception:  # noqa: BLE001
                pass

        return handle

    def _choose_exp_base_image(
        self,
        env_sandbox_id: str,
        digest: str,
        image_info: dict[str, Any],
    ) -> tuple[str, str]:
        if self.backend == "docker":
            ref = image_info.get("reference") or f"zero-sandbox:{env_sandbox_id}"
            return ref, "from_image"
        if self.backend == "lbg":
            url = image_info.get("url")
            if url and image_info.get("status") == "ready":
                return str(url), "from_image"
            # Fall back: same base image family as the env sandbox, reinstall pkgs.
            parent = self._specs.get(env_sandbox_id)
            base = (parent.base_image if parent and parent.base_image else "") or self._config.docker_base_image
            return base, "reinstall_from_freeze"
        # local
        return "local", "venv_clone"

    def _clone_local_venv(self, env_sandbox_id: str, exp_sandbox_id: str) -> None:
        env_root = self._config.run_sandboxes_dir(
            self._sandbox_task[env_sandbox_id]
        ) / env_sandbox_id / "venv"
        exp_root = self._config.run_sandboxes_dir(
            self._sandbox_task[exp_sandbox_id]
        ) / exp_sandbox_id / "venv"
        if not env_root.is_dir():
            return
        if exp_root.exists():
            shutil.rmtree(exp_root, ignore_errors=True)
        shutil.copytree(env_root, exp_root, symlinks=True)

    def _reinstall_from_freeze(self, sandbox_id: str, pip_freeze: list[str]) -> None:
        lines = [ln.strip() for ln in pip_freeze if ln.strip() and not ln.strip().startswith("#")]
        if not lines:
            return
        req = "\n".join(lines) + "\n"
        remote = "/tmp/labwright-freeze.txt"
        try:
            self.put_file(sandbox_id, remote, req.encode("utf-8"))
            self.exec(
                sandbox_id,
                f"python3 -m pip install -q -r {remote}",
                timeout=1800,
            )
        except Exception:  # noqa: BLE001 - spawn should still return a usable shell
            pass

    def destroy(self, sandbox_id: str) -> None:
        self._provider.destroy(sandbox_id)
        with self._lock:
            self._handles.pop(sandbox_id, None)
            self._sandbox_task.pop(sandbox_id, None)
            self._roles.pop(sandbox_id, None)
            self._specs.pop(sandbox_id, None)
            self._env_parent.pop(sandbox_id, None)
            # Drop reverse links where this was a parent.
            dead = [k for k, v in self._env_parent.items() if v == sandbox_id]
            for k in dead:
                self._env_parent.pop(k, None)

    def info(self, sandbox_id: str) -> SandboxInfo:
        return self._provider.info(sandbox_id)
