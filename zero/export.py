"""Finalize a run: pull curated deliverables into ``runs/<name>/deliverables/``.

Traces are written live under ``runs/<name>/trace/``; this module only:

1. Copies the Researcher's ``export/repo`` + ``export/output`` into
   ``deliverables/``
2. Writes ``conclusion.md`` and ``run.json``

Backend-agnostic: for local/docker the workspace is a host dir; for remote
``lbg`` the tree is streamed via ``exec`` + ``get_file``. Safety net: skip
``__pycache__``/``.git`` and any single file above a size cap.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tarfile
import time
from pathlib import Path
from typing import Optional

from zero.config import Config
from zero.sandbox.manager import SandboxManager

_PRUNE_DIRS = {"__pycache__", ".git", ".ipynb_checkpoints"}
_OUT_RE = re.compile(r"ZERO_EXPORT_OUT=(\S+)")


def export_run(
    config: Config,
    manager: SandboxManager,
    *,
    task_id: str,
    workspace: str,
    sandbox_ids: list[str],
    prompt: str,
    status: str,
    backend: str,
    conclusion: str = "",
    resolved_task: str = "",
    environment: Optional[dict] = None,
    pull_deliverables: bool = True,
    grading: Optional[dict] = None,
    optimized_task: Optional[str] = None,
    error: Optional[str] = None,
) -> Optional[Path]:
    """Finalize run metadata, optionally pulling Researcher deliverables."""
    try:
        cap = max(1, int(config.export_max_file_mb)) * 1024 * 1024
        run_dir = config.ensure_run_dirs(task_id)
        deliverables = run_dir / "deliverables"

        got = (
            _collect_export(manager, workspace, sandbox_ids, deliverables, cap)
            if pull_deliverables
            else "skipped"
        )

        if conclusion.strip():
            (run_dir / "conclusion.md").write_text(conclusion, encoding="utf-8")
        if resolved_task.strip():
            (run_dir / "resolved_task.md").write_text(resolved_task, encoding="utf-8")
        if environment is not None:
            (run_dir / "environment.json").write_text(
                json.dumps(environment, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        trace_dir = run_dir / "trace"
        payload = {
            "task_id": task_id,
            "status": status,
            "backend": backend,
            "sandbox_ids": sandbox_ids,
            "prompt": prompt,
            "created_at": time.time(),
            "has_repo": (deliverables / "repo").is_dir(),
            "has_output": (deliverables / "output").is_dir(),
            "export_source": got,
            "layout": "unified_run_folder",
            "grading": grading or {},
            "artifacts": {
                "resolved_task": str(run_dir / "resolved_task.md") if resolved_task.strip() else None,
                "environment": str(run_dir / "environment.json") if environment is not None else None,
                "environment_md": (environment or {}).get("environment_md"),
                "inventory": (environment or {}).get("inventory_path"),
                "image_url": ((environment or {}).get("image") or {}).get("url"),
                "grading": str(run_dir / "grading" / "result.json")
                if (run_dir / "grading" / "result.json").is_file()
                else None,
                "optimized_task": optimized_task,
            },
            "paths": {
                "workspace": str(run_dir / "workspace"),
                "deliverables": str(deliverables),
                "trace": str(trace_dir),
                "teacher": str(run_dir / "teacher"),
                "resources": str(run_dir / "resources"),
                "logs": str(run_dir / "logs"),
                "grading": str(run_dir / "grading"),
                "environment": str(run_dir / "environment"),
                "optimized_task": optimized_task,
            },
        }
        if error:
            payload["error"] = error[:2000]
        (run_dir / "run.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return run_dir
    except Exception:  # noqa: BLE001 - export must never break a run
        return None


def _collect_export(
    manager: SandboxManager,
    workspace: str,
    sandbox_ids: list[str],
    deliverables: Path,
    cap: int,
) -> str:
    """Copy the Researcher's ``export/`` contents into ``deliverables/``."""
    deliverables.mkdir(parents=True, exist_ok=True)
    host_export = Path(workspace) / "export"
    if _has_files(host_export):
        _copy_tree_filtered(host_export, deliverables, cap)
        return "host"

    for sid in reversed(sandbox_ids or []):
        if _pull_sandbox_export(manager, sid, deliverables, cap):
            return f"sandbox:{sid}"
    return "none"


def _has_files(path: Path) -> bool:
    return path.is_dir() and any(p.is_file() for p in path.rglob("*"))


def _copy_tree_filtered(src: Path, dst: Path, cap: int) -> None:
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        if any(part in _PRUNE_DIRS for part in item.relative_to(src).parts):
            continue
        try:
            if item.stat().st_size > cap:
                continue
        except OSError:
            continue
        target = dst / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(item, target)
        except OSError:
            pass


def _pull_sandbox_export(manager: SandboxManager, sid: str, dst: Path, cap: int) -> bool:
    handle = manager.get_handle(sid)
    ws = (handle.workspace_path if handle else "/workspace").rstrip("/")
    prune = " -o ".join(f"-name {d}" for d in sorted(_PRUNE_DIRS))
    script = (
        "set +e; ROOT=; "
        f'for c in "{ws}/export" "$(pwd)/export"; do [ -d "$c" ] && {{ ROOT="$c"; break; }}; done; '
        '[ -n "$ROOT" ] || { echo ZERO_NO_EXPORT; exit 0; }; '
        'OUT="/tmp/zero_export.$$.tgz"; '
        'cd "$ROOT" || { echo ZERO_NO_EXPORT; exit 0; }; '
        f'find . -type d \\( {prune} \\) -prune -o -type f -size -{cap}c -print0 '
        '| tar --null --no-recursion -czf "$OUT" -T - 2>/dev/null; '
        'echo "ZERO_EXPORT_OUT=$OUT"'
    )
    try:
        res = manager.exec(sid, script, timeout=300)
    except Exception:  # noqa: BLE001
        return False
    if "ZERO_NO_EXPORT" in (res.stdout or ""):
        return False
    m = _OUT_RE.search(res.stdout or "")
    if not m:
        return False
    try:
        blob = manager.get_file(sid, m.group(1))
    except Exception:  # noqa: BLE001
        return False
    return _safe_extract(blob, dst)


def _safe_extract(blob: bytes, dst: Path) -> bool:
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            members = []
            for m in tar.getmembers():
                if not m.isfile():
                    continue
                rel = Path(m.name.lstrip("./"))
                if rel.is_absolute() or ".." in rel.parts:
                    continue
                m.name = str(rel)
                members.append(m)
            if not members:
                return False
            tar.extractall(dst, members=members)  # noqa: S202 - members sanitized above
        return True
    except (tarfile.TarError, OSError):
        return False
