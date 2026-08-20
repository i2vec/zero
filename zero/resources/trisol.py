"""Host-side Trisol adapter used to pin and materialize data/model assets."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit


class TrisolError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrisolAsset:
    kind: str
    asset_id: str
    name: str
    team: str
    version_code: str
    version_name: str
    splits: tuple[str, ...] = ()
    size_bytes: int = 0

    def uri(self) -> str:
        query: list[tuple[str, str]] = [("team", self.team), ("name", self.name)]
        query.extend(("split", split) for split in self.splits)
        return (
            f"trisol://{self.kind}/{self.asset_id}/{self.version_code}"
            f"?{urlencode(query)}"
        )


class TrisolClient:
    def __init__(self, *, binary: str = "trisol", team: str = "", api_url: str = "",
                 token: str = ""):
        self.binary = binary
        self.team = team
        self.api_url = api_url
        self.token = token

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.api_url:
            env["TRISOL_API_URL"] = self.api_url
        if self.token:
            env["TRISOL_TOKEN"] = self.token
        if self.team:
            env["TRISOL_TEAM"] = self.team
        return env

    def _run(self, *args: str, json_output: bool = False) -> Any:
        command = [self.binary, "--no-input", "--no-color"]
        if self.team:
            command.extend(["-t", self.team])
        if json_output:
            command.extend(["-o", "json"])
        command.extend(args)
        try:
            result = subprocess.run(
                command, env=self._env(), capture_output=True, text=True,
                timeout=3600, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TrisolError(f"Trisol command failed: {type(exc).__name__}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "unknown error").strip()
            raise TrisolError(message[:1000])
        if not json_output:
            return result.stdout
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise TrisolError("Trisol returned invalid JSON") from exc

    def resolve(self, kind: str, asset_id: str, requested_version: Optional[str] = None) -> TrisolAsset:
        if kind not in {"dataset", "model"}:
            raise TrisolError(f"unsupported Trisol kind: {kind}")
        body = self._run(kind, "get", asset_id, json_output=True)
        versions = [v for v in body.get("versions", []) if v.get("status") == "ready"]
        if not versions:
            raise TrisolError(f"Trisol {kind} {asset_id} has no ready version")
        selected = None
        if requested_version and requested_version not in {"main", "latest"}:
            selected = next((v for v in versions if str(v.get("version_code")) == requested_version
                             or str(v.get("version_name")) == requested_version), None)
            if selected is None:
                raise TrisolError(f"Trisol {kind} {asset_id} has no ready version {requested_version}")
        selected = selected or max(versions, key=lambda v: int(v.get("version_code") or 0))
        splits = tuple(
            str(s["split_name"]) for s in selected.get("splits", []) if s.get("split_name")
        )
        size = int(selected.get("total_size_bytes") or selected.get("file_size") or 0)
        return TrisolAsset(
            kind=kind, asset_id=str(body.get("id") or asset_id), name=str(body.get("name") or asset_id),
            team=str(body.get("team_id") or self.team),
            version_code=str(selected.get("version_code")),
            version_name=str(selected.get("version_name") or selected.get("version_code")),
            splits=splits, size_bytes=size,
        )

    def materialize(self, source: str, destination: Path) -> TrisolAsset:
        parsed = urlsplit(source)
        if parsed.scheme != "trisol" or parsed.netloc not in {"dataset", "model"}:
            raise TrisolError(f"invalid Trisol URI: {source}")
        parts = parsed.path.strip("/").split("/")
        if not parts or not parts[0]:
            raise TrisolError(f"missing Trisol asset ID: {source}")
        query = parse_qs(parsed.query)
        requested = parts[1] if len(parts) > 1 else None
        scoped = TrisolClient(
            binary=self.binary, team=(query.get("team") or [self.team])[0],
            api_url=self.api_url, token=self.token,
        )
        asset = scoped.resolve(parsed.netloc, parts[0], requested)
        destination.mkdir(parents=True, exist_ok=True)
        if asset.kind == "dataset":
            splits = tuple(unquote(v) for v in query.get("split", [])) or asset.splits
            if not splits:
                raise TrisolError(f"Trisol dataset {asset.asset_id} has no downloadable splits")
            for split in splits:
                scoped._run("dataset", "download", asset.asset_id, asset.version_code, split,
                            "--output", str(destination) + "/")
        else:
            scoped._run("model", "download", f"{asset.name}:{asset.version_code}",
                        "--output", str(destination) + "/")
        return asset

    def publish(self, kind: str, unique_key: str, local_path: Path, digest: str,
                description: str = "") -> TrisolAsset:
        """Create/reuse an asset and upload an immutable content version."""
        if kind not in {"dataset", "model"}:
            raise TrisolError(f"unsupported Trisol kind: {kind}")
        suffix = hashlib.sha256(f"{self.team}:{unique_key}".encode()).hexdigest()[:8]
        slug = re.sub(r"[^a-z0-9-]+", "-", unique_key.lower()).strip("-") or kind
        name = f"zero-{slug[:48]}-{suffix}"[:63].rstrip("-")
        existing = self._run(kind, "list", "--name", name, "--limit", "50", json_output=True)
        records = existing if isinstance(existing, list) else (existing.get("items") or [])
        exact = next((record for record in records if record.get("name") == name), None)
        if exact:
            asset_id = str(exact["id"])
        else:
            created = self._run(kind, "create", name, "--description", description,
                                json_output=True)
            asset_id = str(created.get("id") or "")
            if not asset_id:
                raise TrisolError(f"Trisol {kind} create returned no ID")
        version = "sha256-" + digest.removeprefix("sha256:")[:16]
        if kind == "dataset":
            args = ["dataset", "upload", asset_id, str(local_path),
                    "--version", version, "--progress", "none"]
            if local_path.is_dir():
                files = sorted(path for path in local_path.iterdir() if path.is_file())
                if not files:
                    raise TrisolError("dataset upload directory has no top-level files")
                for path in files:
                    args.extend(["--split", f"{path.name}={path}"])
            self._run(*args, json_output=True)
        else:
            self._run("model", "upload", asset_id, str(local_path),
                      "--version", version, "--progress", "none", "--yes", json_output=True)
        return self.resolve(kind, asset_id, version)
