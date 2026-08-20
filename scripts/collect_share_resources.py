#!/usr/bin/env python3
"""Materialize and validate real task resources under /share.

This deliberately treats metadata-only directories as unresolved.  The first
supported ingestion path is an already-downloaded dataset archive; it is useful
for resumable collection and for sources that Deploy Master cannot download
(the current service exposes build-only endpoints).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


COLLECTION = Path("/personal/zero/task_collections/final-scored-with-images-20260813")
RESOURCE_ROOT = Path("/share/xumj/resources")
STATE = COLLECTION / "resource-collection-state-share.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def zip_manifest(source: Path) -> tuple[list[dict], str]:
    files: list[dict] = []
    logical = hashlib.sha256()
    with zipfile.ZipFile(source) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"corrupt ZIP member: {bad}")
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            if info.is_dir():
                continue
            digest = hashlib.sha256()
            with archive.open(info) as handle:
                for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                    digest.update(chunk)
            record = {
                "relative_path": info.filename,
                "size": info.file_size,
                "sha256": "sha256:" + digest.hexdigest(),
                "media_type": mimetypes.guess_type(info.filename)[0] or "application/octet-stream",
            }
            files.append(record)
            logical.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
            logical.update(b"\n")
    if not files:
        raise RuntimeError("ZIP archive contains no files")
    return files, "sha256:" + logical.hexdigest()


def package_dataset(resource_id: str, source: Path, *, name: str, source_url: str,
                    version: str, task: str) -> dict:
    if not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError(f"missing or empty source: {source}")
    files, logical_digest = zip_manifest(source)
    destination = RESOURCE_ROOT / "datasets" / resource_id
    final_archive = destination / "payload.tar.zst"
    if destination.exists() and final_archive.exists():
        existing = load_json(destination / "resource.json", {})
        if isinstance(existing, dict) and existing.get("artifact_digest") == sha256(final_archive):
            backwrite_result = backwrite(resource_id)
            return {
                "id": resource_id,
                "status": "skipped_verified",
                "artifact_path": str(final_archive),
                "backwrite": backwrite_result,
            }
        raise RuntimeError(f"conflict at existing destination: {destination}")

    staging_root = Path("/personal/zero/resource_staging/.staging")
    staging_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f"{resource_id}-", dir=staging_root))
    staged_archive = temporary / "payload.tar.zst"
    try:
        command = [
            "tar", "--zstd", "-cf", str(staged_archive), "-C", str(source.parent), source.name,
        ]
        subprocess.run(command, check=True)
        subprocess.run(["zstd", "-t", str(staged_archive)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["tar", "--zstd", "-tf", str(staged_archive)], check=True,
                       stdout=subprocess.DEVNULL)
        artifact_digest = sha256(staged_archive)
        manifest = {
            "schema_version": 1,
            "resource_id": resource_id,
            "generated_at": now(),
            "source_container": source.name,
            "file_count": len(files),
            "files": files,
            "logical_digest": logical_digest,
        }
        metadata = {
            "schema_version": 1,
            "id": resource_id,
            "type": "dataset",
            "name": name,
            "status": "ready",
            "source": {"kind": "official_url", "url": source_url},
            "source_url": source_url,
            "version": version,
            "artifact_path": str(final_archive),
            "artifact_digest": artifact_digest,
            "logical_digest": logical_digest,
            "size": staged_archive.stat().st_size,
            "file_count": len(files),
            "media_type": "application/x-tar+zstd",
            "compression": "zstd",
            "referenced_by_tasks": [task],
            "collection": {
                "executor": "official_source_fallback",
                "deploy_master_task_id": None,
                "operation": "package_preexisting_download",
                "status": "succeeded",
                "started_at": None,
                "completed_at": now(),
                "reason": "Current Deploy Master OpenAPI exposes tool build endpoints only; dataset download is unsupported. The official source archive was downloaded to resumable local staging.",
                "output_path": str(final_archive),
                "output_digest": artifact_digest,
            },
            "verification": {
                "source_zip_test": "passed",
                "zstd_test": "passed",
                "tar_list": "passed",
                "share_digest_recheck": "pending",
            },
            "provenance": {"staged_source": str(source), "trisol_upload": False},
        }
        atomic_json(temporary / "files.manifest.json", manifest)
        atomic_json(temporary / "resource.json", metadata)
        (temporary / "SHA256SUMS").write_text(
            f"{artifact_digest.removeprefix('sha256:')}  payload.tar.zst\n", encoding="ascii"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            entries = list(destination.iterdir())
            if any(item.name != "resource.json" for item in entries):
                raise RuntimeError(f"refusing to replace non-placeholder directory: {destination}")
            backup = destination.with_name(destination.name + ".metadata-only-backup")
            if backup.exists():
                raise RuntimeError(f"backup already exists: {backup}")
            destination.rename(backup)
        # /personal and /share are separate filesystems, so the final rename
        # must originate from a hidden directory on /share itself.
        final_temporary = Path(tempfile.mkdtemp(prefix=f".{resource_id}-", dir=destination.parent))
        try:
            for item in temporary.iterdir():
                if item.is_dir():
                    shutil.copytree(item, final_temporary / item.name)
                else:
                    shutil.copy2(item, final_temporary / item.name)
            copied_archive = final_temporary / "payload.tar.zst"
            if sha256(copied_archive) != artifact_digest:
                raise RuntimeError("digest changed while copying to /share staging")
            os.replace(final_temporary, destination)
        finally:
            if final_temporary.exists():
                shutil.rmtree(final_temporary)
        share_digest = sha256(final_archive)
        if share_digest != artifact_digest:
            raise RuntimeError("digest changed after move to /share")
        subprocess.run(["zstd", "-t", str(final_archive)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["tar", "--zstd", "-tf", str(final_archive)], check=True,
                       stdout=subprocess.DEVNULL)
        metadata["verification"]["share_digest_recheck"] = "passed"
        metadata["verification"]["share_archive_test"] = "passed"
        atomic_json(destination / "resource.json", metadata)
        state = load_json(STATE, {"schema_version": 1, "resources": {}})
        assert isinstance(state, dict)
        state.setdefault("resources", {})[resource_id] = {
            "status": "ready", "artifact_path": str(final_archive),
            "artifact_digest": artifact_digest, "logical_digest": logical_digest,
            "updated_at": now(),
        }
        state["updated_at"] = now()
        atomic_json(STATE, state)
        metadata["backwrite"] = backwrite(resource_id)
        return metadata
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def audit() -> dict:
    index = load_json(COLLECTION / "resource-index.json", {})
    resources = index.get("resources", []) if isinstance(index, dict) else []
    kinds: dict[str, int] = {}
    for item in resources:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    real = 0
    metadata_only = 0
    for kind in ("datasets", "tools", "models"):
        root = RESOURCE_ROOT / kind
        if not root.exists():
            continue
        for directory in root.iterdir():
            if not directory.is_dir():
                continue
            files = [path for path in directory.iterdir() if path.is_file()]
            if any(path.name in {"payload.tar.zst", "image.oci.tar.zst"} and path.stat().st_size > 0
                   for path in files):
                real += 1
            elif files:
                metadata_only += 1
    return {
        "task_count": len([path for path in (COLLECTION / "tasks").iterdir() if path.is_dir()]),
        "resource_reference_count": index.get("counts", {}).get("task_resource_refs"),
        "unique_resource_count": len(resources), "kinds": kinds,
        "real_artifact_count": real, "metadata_only_directory_count": metadata_only,
    }


def backwrite(resource_id: str) -> dict:
    state = load_json(STATE, {})
    record = state.get("resources", {}).get(resource_id) if isinstance(state, dict) else None
    if not isinstance(record, dict) or record.get("status") != "ready":
        raise RuntimeError(f"resource is not ready in state: {resource_id}")
    artifact_path = Path(record["artifact_path"])
    metadata = load_json(artifact_path.parent / "resource.json", {})
    if not isinstance(metadata, dict) or metadata.get("status") != "ready":
        raise RuntimeError(f"resource metadata is not ready: {resource_id}")
    if not artifact_path.is_file() or sha256(artifact_path) != record["artifact_digest"]:
        raise RuntimeError(f"resource artifact failed digest gate: {resource_id}")
    changed: list[str] = []
    for task in metadata.get("referenced_by_tasks", []):
        task_dir = COLLECTION / "tasks" / task
        resources_path = task_dir / "resources.json"
        resources = load_json(resources_path, [])
        found = False
        for item in resources:
            if item.get("id") != resource_id:
                continue
            found = True
            item["resource_ref"] = f"share-resource://dataset/{resource_id}"
            item["resolution"] = {
                "status": "ready", "artifact_type": "archive",
                "artifact_path": str(artifact_path),
                "artifact_digest": metadata["artifact_digest"],
                "logical_digest": metadata["logical_digest"],
                "version": metadata["version"], "source": metadata["source"],
                "deploy_master_task_id": metadata["collection"].get("deploy_master_task_id"),
                "verification": metadata["verification"],
            }
        if not found:
            raise RuntimeError(f"resource missing from {resources_path}")
        atomic_json(resources_path, resources)
        lock_path = task_dir / "resources.lock.json"
        lock = load_json(lock_path, {})
        found = False
        for entry in lock.get("entries", []):
            if entry.get("id") != resource_id:
                continue
            found = True
            entry.clear()
            entry.update({
                "id": resource_id, "kind": "dataset", "status": "ready",
                "resource_ref": f"share-resource://dataset/{resource_id}",
                "artifact_path": str(artifact_path),
                "artifact_digest": metadata["artifact_digest"],
                "logical_digest": metadata["logical_digest"],
                "version": metadata["version"], "source": metadata["source"],
                "deploy_master_task_id": metadata["collection"].get("deploy_master_task_id"),
            })
        if not found:
            raise RuntimeError(f"resource missing from {lock_path}")
        lock["updated_at"] = now()
        atomic_json(lock_path, lock)
        changed.append(task)
    return {"id": resource_id, "status": "backwritten", "tasks": changed}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("audit")
    backwrite_parser = sub.add_parser("backwrite")
    backwrite_parser.add_argument("resource_id")
    package = sub.add_parser("package-dataset")
    package.add_argument("resource_id")
    package.add_argument("source", type=Path)
    package.add_argument("--name", required=True)
    package.add_argument("--source-url", required=True)
    package.add_argument("--version", required=True)
    package.add_argument("--task", required=True)
    args = parser.parse_args()
    if args.command == "audit":
        result = audit()
    elif args.command == "backwrite":
        result = backwrite(args.resource_id)
    else:
        result = package_dataset(args.resource_id, args.source, name=args.name,
                                 source_url=args.source_url, version=args.version, task=args.task)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
