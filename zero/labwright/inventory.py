"""Collect and persist a frozen environment inventory + human-readable md."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from pathlib import Path
from typing import Any, Optional

from zero.protocol.environment_inventory import (
    EnvironmentInventory,
    ImageRecord,
    MountInventoryEntry,
    ToolInventoryEntry,
)
from zero.protocol.manifest import EnvironmentManifest
from zero.protocol.resources import ResourceLock
from zero.resources.locks import lock_digest
from zero.sandbox.manager import SandboxManager

_ENV_DIR = "environment"
_BASELINE_TOOLS = (
    ("python", "python3"),
    ("pip", "pip"),
    ("bash", "bash"),
    ("git", "git"),
)


def validate_inventory_lock_consistency(
    manifest: EnvironmentManifest,
    inventory: EnvironmentInventory,
    lock: ResourceLock,
) -> list[str]:
    """Cross-check the immutable bindings copied into release artifacts."""
    errors: list[str] = []
    actual_digest = lock_digest(lock)
    if manifest.resources_lock_digest != actual_digest:
        errors.append("manifest_lock_digest_mismatch")
    if inventory.resources_lock_digest != actual_digest:
        errors.append("inventory_lock_digest_mismatch")
    entries = {entry.requirement_id: entry for entry in lock.entries}
    mounts = {(mount.kind, mount.name): mount for mount in inventory.mounts}
    for kind, records in (("model", manifest.models), ("dataset", manifest.datasets)):
        for name, record in records.items():
            requirement_id = f"{kind}:{name}"
            entry = entries.get(requirement_id)
            mount = mounts.get((kind, name))
            if entry is None:
                errors.append(f"manifest_resource_not_locked:{requirement_id}")
                continue
            if mount is None:
                errors.append(f"inventory_mount_missing:{requirement_id}")
                continue
            if record.sha256 != entry.artifact.digest or mount.sha256 != entry.artifact.digest:
                errors.append(f"artifact_digest_mismatch:{requirement_id}")
            if record.source != mount.source:
                errors.append(f"artifact_source_mismatch:{requirement_id}")
    return errors


def collect_and_write_inventory(
    *,
    run_dir: Path,
    manager: SandboxManager,
    manifest: EnvironmentManifest,
    backend: str,
    base_image: str = "",
    image: Optional[dict[str, Any]] = None,
    scope: str = "clean_baseline",
    spawn_mode: Optional[str] = None,
    env_sandbox_id: Optional[str] = None,
    exp_sandbox_id: Optional[str] = None,
    resource_lock: Optional[ResourceLock] = None,
) -> EnvironmentInventory:
    """Probe the sandbox, write ``environment/`` artifacts, return inventory."""
    env_dir = run_dir / _ENV_DIR
    env_dir.mkdir(parents=True, exist_ok=True)

    sid = manifest.sandbox_id
    runtime = _probe_runtime(manager, sid, manifest)
    pip_freeze = _probe_pip_freeze(manager, sid)
    tools = _tools_from_manifest(manifest, manager, sid)

    mounts: list[MountInventoryEntry] = []
    for name, m in manifest.models.items():
        mounts.append(MountInventoryEntry(
            kind="model", name=name, path=m.path, source=m.source,
            sha256=m.sha256, revision=m.revision, read_only=m.read_only,
        ))
    for name, d in manifest.datasets.items():
        mounts.append(MountInventoryEntry(
            kind="dataset", name=name, path=d.path, source=d.source,
            sha256=d.sha256, revision=d.version, read_only=d.read_only,
        ))

    packages = {n: e.version for n, e in manifest.packages.items()}
    if not packages and pip_freeze:
        packages = _packages_from_freeze(pip_freeze)

    env_id = _environment_id(manifest, pip_freeze, backend)
    image_rec = _image_record(manifest, image)

    files: dict[str, str] = {}
    freeze_path = env_dir / "pip-freeze.txt"
    freeze_path.write_text("\n".join(pip_freeze) + ("\n" if pip_freeze else ""), encoding="utf-8")
    files["pip-freeze.txt"] = str(freeze_path)

    tools_path = env_dir / "tools.txt"
    tools_path.write_text(
        "\n".join(
            f"{t.name}\t{t.version or ''}\t{t.command or ''}\t{t.path or ''}"
            for t in tools
        ) + ("\n" if tools else ""),
        encoding="utf-8",
    )
    files["tools.txt"] = str(tools_path)

    mounts_path = env_dir / "mounts.json"
    mounts_path.write_text(
        json.dumps([m.model_dump() for m in mounts], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    files["mounts.json"] = str(mounts_path)

    notes = [
        "Dual-sandbox: packages were frozen on an env sandbox; Researcher uses a separate exp sandbox.",
        f"scope={scope}",
    ]
    if env_sandbox_id:
        notes.append(f"env_sandbox_id={env_sandbox_id}")
    if exp_sandbox_id:
        notes.append(f"exp_sandbox_id={exp_sandbox_id}")
    if spawn_mode:
        notes.append(f"exp_spawn_mode={spawn_mode}")

    inventory = EnvironmentInventory(
        environment_id=env_id,
        task_id=manifest.task_id,
        sandbox_id=sid,
        backend=backend,
        scope=scope,
        snapshot_timing="Labwright publish_manifest on env sandbox (pre-exp)",
        base_image={"reference": base_image or None},
        runtime=runtime,
        packages=packages,
        pip_freeze=pip_freeze,
        tools=tools,
        mounts=mounts,
        verification=manifest.verification.model_dump(exclude_none=True),
        image=image_rec,
        files=files,
        notes=notes,
        resources_lock_digest=manifest.resources_lock_digest,
    )
    if resource_lock is not None:
        consistency_errors = validate_inventory_lock_consistency(
            manifest, inventory, resource_lock,
        )
        if consistency_errors:
            raise ValueError(
                "resource lock/manifest/inventory mismatch: "
                + ", ".join(consistency_errors)
            )

    inv_path = env_dir / "inventory.json"
    inv_path.write_text(inventory.model_dump_json(indent=2) + "\n", encoding="utf-8")
    files["inventory.json"] = str(inv_path)
    inventory.files = files

    md = render_environment_md(inventory)
    md_path = env_dir / "environment.md"
    md_path.write_text(md, encoding="utf-8")
    files["environment.md"] = str(md_path)
    inventory.files = files
    inv_path.write_text(inventory.model_dump_json(indent=2) + "\n", encoding="utf-8")

    image_path = env_dir / "image.json"
    image_path.write_text(
        json.dumps(image_rec.model_dump(exclude_none=True), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lineage = {
        "env_sandbox_id": env_sandbox_id,
        "exp_sandbox_id": exp_sandbox_id,
        "spawn_mode": spawn_mode,
    }
    revisions_dir = env_dir / "revisions"
    revisions_dir.mkdir(parents=True, exist_ok=True)
    rev_name = f"{int(time.time())}-{sid}.json"
    (revisions_dir / rev_name).write_text(
        json.dumps({
            "environment_id": env_id,
            "manifest": manifest.model_dump(mode="json"),
            "lineage": lineage,
            "image": image_rec.model_dump(exclude_none=True),
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    index = {
        "schema_version": 2,
        "backend": backend,
        "snapshot_scope": scope,
        "snapshot_timing": inventory.snapshot_timing,
        "environment_id": env_id,
        "inventory_path": str(inv_path),
        "environment_md": str(md_path),
        "lineage": lineage,
        "manifest": manifest.model_dump(mode="json"),
        "resources_lock_digest": manifest.resources_lock_digest,
        "image": image_rec.model_dump(exclude_none=True),
        "collected_at": time.time(),
    }
    (run_dir / "environment.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return inventory


def update_image_on_inventory(
    run_dir: Path,
    image: dict[str, Any],
    *,
    manifest: Optional[EnvironmentManifest] = None,
) -> None:
    """Refresh image fields after async LBG commit resolves."""
    env_dir = run_dir / _ENV_DIR
    env_dir.mkdir(parents=True, exist_ok=True)
    image_rec = ImageRecord(
        status=str(image.get("status") or "unknown"),
        url=image.get("url"),
        reference=image.get("reference"),
        content_digest=image.get("digest") or image.get("content_digest"),
        provider_commit_id=image.get("commit_id") or image.get("provider_commit_id"),
        kind=image.get("kind"),
        note=image.get("note") or image.get("reason"),
    )
    (env_dir / "image.json").write_text(
        json.dumps(image_rec.model_dump(exclude_none=True), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    inv_path = env_dir / "inventory.json"
    if inv_path.is_file():
        try:
            data = json.loads(inv_path.read_text(encoding="utf-8"))
            data["image"] = image_rec.model_dump(exclude_none=True)
            inv_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            md = render_environment_md(EnvironmentInventory.model_validate(data))
            (env_dir / "environment.md").write_text(md, encoding="utf-8")
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    index_path = run_dir / "environment.json"
    index: dict[str, Any] = {}
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            index = {}
    index["schema_version"] = index.get("schema_version", 2)
    index["image"] = image_rec.model_dump(exclude_none=True)
    if manifest is not None:
        index["manifest"] = manifest.model_dump(mode="json")
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_environment_md(inv: EnvironmentInventory) -> str:
    """Fixed-section human-readable environment description (Dockerfile-like)."""
    lines = [
        f"# Environment: `{inv.environment_id}`",
        "",
        "This document describes the **published** scientific execution environment.",
        "Prefer the packaged image below for reuse; the lockfiles alongside this file",
        "are the source of truth for package versions.",
        "",
        "## Base image",
        "",
        f"- backend: `{inv.backend}`",
        f"- reference: `{(inv.base_image or {}).get('reference') or 'n/a'}`",
        f"- sandbox_id: `{inv.sandbox_id}`",
        f"- task_id: `{inv.task_id}`",
        "",
        "## Runtime",
        "",
    ]
    for k, v in (inv.runtime or {}).items():
        lines.append(f"- {k}: `{v}`")
    if not inv.runtime:
        lines.append("- (unprobed)")
    lines.extend(["", "## Python packages", ""])
    if inv.pip_freeze:
        lines.append("```")
        lines.extend(inv.pip_freeze[:400])
        if len(inv.pip_freeze) > 400:
            lines.append(f"# … {len(inv.pip_freeze) - 400} more lines in pip-freeze.txt")
        lines.append("```")
    elif inv.packages:
        lines.append("```")
        for n, v in sorted(inv.packages.items()):
            lines.append(f"{n}=={v}")
        lines.append("```")
    else:
        lines.append("_No packages recorded._")

    lines.extend(["", "## CLI / system tools", ""])
    if inv.tools:
        for t in inv.tools:
            ver = t.version or "?"
            lines.append(f"- `{t.name}` version=`{ver}` command=`{t.command or t.name}`")
    else:
        lines.append("_No tools recorded._")

    lines.extend(["", "## Mounted resources", ""])
    if inv.mounts:
        for m in inv.mounts:
            lines.append(
                f"- [{m.kind}] `{m.name}` → `{m.path}`"
                + (f" source=`{m.source}`" if m.source else "")
            )
    else:
        lines.append("_No model/dataset mounts._")

    lines.extend(["", "## Verification", ""])
    if inv.verification:
        for k, v in inv.verification.items():
            lines.append(f"- {k}: `{v}`")
    else:
        lines.append("_n/a_")

    lines.extend(["", "## Packaged image", ""])
    img = inv.image
    lines.append(f"- status: `{img.status}`")
    if img.url:
        lines.append(f"- url: `{img.url}`")
    if img.reference:
        lines.append(f"- reference: `{img.reference}`")
    if img.content_digest:
        lines.append(f"- digest: `{img.content_digest}`")
    if img.provider_commit_id:
        lines.append(f"- commit_id: `{img.provider_commit_id}`")
    if img.note:
        lines.append(f"- note: {img.note}")
    if img.status not in ("ready",) or not (img.url or img.reference):
        lines.append(
            "- _Image not yet publishable. For LBG set `ZERO_LBG_PROJECT_ID` "
            "and allow `ZERO_LBG_IMAGE_WAIT_TIMEOUT` to resolve `imageUrl`._"
        )

    lines.extend(["", "## Side files", ""])
    for name, path in sorted(inv.files.items()):
        lines.append(f"- `{name}` → `{path}`")
    lines.append("")
    return "\n".join(lines)


def _probe_runtime(manager: SandboxManager, sandbox_id: str, manifest: EnvironmentManifest) -> dict[str, Any]:
    runtime = dict(manifest.runtime or {})
    try:
        r = manager.exec(
            sandbox_id,
            "python3 - <<'PY'\n"
            "import platform, sys\n"
            "print('python_executable=' + sys.executable)\n"
            "print('python_version=' + platform.python_version())\n"
            "print('platform=' + platform.platform())\n"
            "PY",
            timeout=60,
        )
        for line in (r.stdout or "").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                runtime[k.strip()] = v.strip()
    except Exception:  # noqa: BLE001
        pass
    return runtime


def _probe_pip_freeze(manager: SandboxManager, sandbox_id: str) -> list[str]:
    try:
        r = manager.exec(sandbox_id, "python3 -m pip freeze 2>/dev/null", timeout=120)
        if r.exit_code != 0:
            return []
        return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip() and not ln.startswith("#")]
    except Exception:  # noqa: BLE001
        return []


def _tools_from_manifest(
    manifest: EnvironmentManifest, manager: SandboxManager, sandbox_id: str,
) -> list[ToolInventoryEntry]:
    out: list[ToolInventoryEntry] = []
    for name, t in manifest.tools.items():
        cmd = t.command or name
        out.append(_probe_tool(
            manager, sandbox_id, name=name, command=cmd,
            version=t.version, verified=t.verified,
        ))

    declared = {entry.command or entry.name for entry in out}
    for name, command in _BASELINE_TOOLS:
        if command in declared:
            continue
        entry = _probe_tool(
            manager, sandbox_id, name=name, command=command,
            version=None, verified=False,
        )
        if entry.path:
            out.append(entry)
    return out


def _probe_tool(
    manager: SandboxManager,
    sandbox_id: str,
    *,
    name: str,
    command: str,
    version: Optional[str],
    verified: bool,
) -> ToolInventoryEntry:
    path = None
    try:
        quoted = shlex.quote(command)
        r = manager.exec(
            sandbox_id,
            f"command -v {quoted} 2>/dev/null; {quoted} --version 2>/dev/null | head -1",
            timeout=60,
        )
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        if lines:
            path = lines[0]
        if len(lines) > 1 and not version:
            version = lines[1][:200]
    except Exception:  # noqa: BLE001
        pass
    return ToolInventoryEntry(
        name=name, command=command, version=version, path=path, verified=verified,
    )


def _packages_from_freeze(lines: list[str]) -> dict[str, str]:
    pkgs: dict[str, str] = {}
    for line in lines:
        if "==" in line:
            name, _, ver = line.partition("==")
            pkgs[name.strip()] = ver.strip()
    return pkgs


def _environment_id(manifest: EnvironmentManifest, pip_freeze: list[str], backend: str) -> str:
    h = hashlib.sha256()
    h.update(backend.encode())
    h.update(b"|")
    h.update((manifest.image_digest or "").encode())
    h.update(b"|")
    h.update("\n".join(pip_freeze).encode())
    h.update(b"|")
    h.update(json.dumps(manifest.package_lock, sort_keys=True).encode())
    return "sha256:" + h.hexdigest()[:16]


def _image_record(manifest: EnvironmentManifest, image: Optional[dict[str, Any]]) -> ImageRecord:
    if image:
        return ImageRecord(
            status=str(image.get("status") or "unknown"),
            url=image.get("url"),
            reference=image.get("reference"),
            content_digest=image.get("digest") or image.get("content_digest") or manifest.image_digest,
            provider_commit_id=image.get("commit_id"),
            kind=image.get("kind"),
            note=image.get("note") or image.get("reason"),
        )
    digest = manifest.image_digest or ""
    if digest.startswith("lbg:commit:"):
        return ImageRecord(
            status="submitted",
            provider_commit_id=digest.rsplit(":", 1)[-1],
            kind="lbg_commit",
            content_digest=digest,
            note="Awaiting imageUrl resolution at run completion.",
        )
    if digest.startswith("sha256:") or digest:
        return ImageRecord(
            status="not_publishable" if not digest.startswith("docker") else "ready",
            content_digest=digest,
            kind="reproducibility_digest",
        )
    return ImageRecord(status="unavailable")
