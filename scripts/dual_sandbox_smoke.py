"""Smoke dual-sandbox spawn on the local backend (no model, no docker/lbg)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from zero.config import Config
from zero.protocol.manifest import EnvironmentManifest, PackageEntry, VerificationReport
from zero.sandbox.manager import SandboxManager


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = Config(root=root, sandbox_backend="local")
        # Force local even if auto would pick docker.
        mgr = SandboxManager(cfg)
        assert mgr.backend == "local", mgr.backend

        task_id = "dual-smoke"
        cfg.ensure_run_dirs(task_id)

        env = mgr.create(task_id, base_image="local", role="env", python_version="3.11")
        assert env.role == "env"
        assert "environment/scratch" in env.workspace_path.replace("\\", "/")

        # Install something tiny into the env venv.
        r = mgr.exec(env.sandbox_id, "python -m pip install -q six==1.16.0", timeout=180)
        assert r.ok, (r.stdout, r.stderr)

        mgr.prepare_env_for_freeze(env.sandbox_id)
        digest = mgr.snapshot(env.sandbox_id)
        assert digest.startswith("local:"), digest

        # Exp snapshot must be refused.
        exp_tmp = mgr.create(task_id, base_image="local", role="exp")
        try:
            mgr.snapshot(exp_tmp.sandbox_id)
            raise AssertionError("snapshot on exp should fail")
        except RuntimeError as exc:
            assert "experiment" in str(exc).lower() or "env" in str(exc).lower()
        mgr.destroy(exp_tmp.sandbox_id)

        freeze = [
            ln.strip()
            for ln in mgr.exec(env.sandbox_id, "python -m pip freeze").stdout.splitlines()
            if ln.strip()
        ]
        exp = mgr.spawn_experiment_sandbox(
            task_id=task_id,
            env_sandbox_id=env.sandbox_id,
            digest=digest,
            pip_freeze=freeze,
            environment_id="sha256:test",
        )
        assert exp.role == "exp"
        assert exp.parent_sandbox_id == env.sandbox_id
        assert exp.spawn_mode == "venv_clone"
        assert "/workspace" in exp.workspace_path.replace("\\", "/") or exp.workspace_path.endswith("workspace")

        check = mgr.exec(exp.sandbox_id, "python -c 'import six; print(six.__version__)'")
        assert check.ok and "1.16.0" in check.stdout, (check.stdout, check.stderr)

        print(f"OK dual-sandbox local env={env.sandbox_id} exp={exp.sandbox_id} digest={digest}")


if __name__ == "__main__":
    main()
