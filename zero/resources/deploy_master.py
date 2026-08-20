"""Typed Deploy Master build adapter; it never mutates manifests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

from zero.resources.errors import DeployMasterBuildFailed, DeployMasterVerificationFailed


class _RebuildableBuildFailed(DeployMasterBuildFailed):
    """A task reached an explicit terminal build failure state."""


class BuildToolRequest(BaseModel):
    github_url: str
    need_mcp: bool = False
    build_instructions: Optional[str] = None
    verify_commands: list[str] = Field(default_factory=list)
    dockerfile_path: Optional[str] = None
    build_context: Optional[str] = None
    build_args: Optional[dict[str, str]] = None
    repository_dockerfile_policy: Optional[str] = None


class BuiltToolArtifact(BaseModel):
    task_id: str
    image_uri: str
    image_digest: Optional[str] = None
    platform: Optional[str] = None
    source_commit: Optional[str] = None
    dockerfile_digest: Optional[str] = None
    verification_digest: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    build_attempts: int = 1
    task_ids: list[str] = Field(default_factory=list)


class DeployMasterClient:
    def __init__(self, base_url: str, *, poll_interval: float = 5, deadline: float = 3600,
                 auth_token: str = "", transport=None):
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers,
                                         timeout=30, transport=transport)
        self.poll_interval = poll_interval
        self.deadline = deadline

    @staticmethod
    def _transient(response: httpx.Response) -> bool:
        return response.status_code in (408, 425, 429) or response.status_code >= 500

    async def _request(self, method: str, path: str, *, attempts: int = 3,
                       **kwargs) -> httpx.Response:
        """Retry transient transport/status failures for an idempotent request.

        POST callers deliberately pass attempts=1: once a build submission may
        have reached Deploy Master, replaying it could create a duplicate task.
        """
        last_error: Optional[Exception] = None
        for attempt in range(max(1, attempts)):
            try:
                response = await self._client.request(method, path, **kwargs)
                if not self._transient(response) or attempt + 1 >= attempts:
                    response.raise_for_status()
                    return response
            except httpx.TransportError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise DeployMasterBuildFailed(f"Deploy Master transport failure: {exc}") from exc
            except httpx.HTTPStatusError as exc:
                raise DeployMasterBuildFailed(
                    f"Deploy Master HTTP {exc.response.status_code}: {exc.response.text[:500]}"
                ) from exc
            await asyncio.sleep(self.poll_interval)
        raise DeployMasterBuildFailed(f"Deploy Master request failed: {last_error}")

    async def aclose(self):
        await self._client.aclose()

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, Any]:
        body = response.json()
        if not isinstance(body, dict):
            raise DeployMasterBuildFailed("Deploy Master returned a non-object response")
        data = body.get("data", body)
        if not isinstance(data, dict):
            raise DeployMasterBuildFailed("Deploy Master response has invalid data")
        return data

    @staticmethod
    def _digest(value: Any) -> Optional[str]:
        if value in (None, "", [], {}):
            return None
        if isinstance(value, str):
            raw = value.encode()
        else:
            raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(raw).hexdigest()}"

    async def build(self, request: BuildToolRequest, *, max_rebuilds: int = 0) -> BuiltToolArtifact:
        """Build a tool, optionally resubmitting after a terminal build failure.

        Rebuilds are explicit and bounded. Verification failures are never
        rebuilt because they require the caller to revise the request first.
        The deadline covers all attempts rather than resetting per rebuild.
        """
        if max_rebuilds < 0:
            raise ValueError("max_rebuilds must be non-negative")
        end = time.monotonic() + self.deadline
        task_ids: list[str] = []
        for build_attempt in range(max_rebuilds + 1):
            try:
                return await self._build_once(request, end=end, task_ids=task_ids,
                                              build_attempt=build_attempt)
            except DeployMasterVerificationFailed:
                raise
            except _RebuildableBuildFailed:
                if build_attempt >= max_rebuilds or time.monotonic() >= end:
                    raise

        raise DeployMasterBuildFailed("Deploy Master rebuild budget exhausted")

    async def _build_once(self, request: BuildToolRequest, *, end: float,
                          task_ids: list[str], build_attempt: int) -> BuiltToolArtifact:
        response = await self._request(
            "POST", "/api/v1/build", attempts=1,
            json=request.model_dump(exclude_none=True),
        )
        task_id = str(self._payload(response).get("task_id") or "")
        if not task_id:
            raise DeployMasterBuildFailed("build response missing task_id")
        task_ids.append(task_id)
        while time.monotonic() < end:
            status = await self._request("GET", f"/api/v1/build/{task_id}")
            data = self._payload(status)
            state = str(data.get("status") or data.get("state") or "").lower()
            if state in ("failed", "failure", "error", "cancelled", "canceled"):
                message = data.get("error_message") or data.get("message") or data.get("progress") or state
                if "verif" in str(data.get("failure_stage") or "").lower():
                    raise DeployMasterVerificationFailed(f"{task_id}: {message}")
                raise _RebuildableBuildFailed(f"{task_id}: {message}")
            if state in ("succeeded", "success", "completed", "ready"):
                image_uri = (data.get("docker_image_uri") or data.get("image_uri")
                             or data.get("image") or data.get("image_url"))
                if not image_uri:
                    raise DeployMasterBuildFailed(f"{task_id}: result missing image URI")
                digest = data.get("image_digest") or data.get("digest")
                warnings = [] if digest else ["mutable_reference"]
                return BuiltToolArtifact(
                    task_id=task_id, image_uri=image_uri, image_digest=digest,
                    platform=data.get("platform"), source_commit=data.get("source_commit") or data.get("commit"),
                    dockerfile_digest=data.get("dockerfile_digest") or self._digest(data.get("dockerfile")),
                    verification_digest=(data.get("verification_digest")
                                         or self._digest(data.get("verification_results"))),
                    warnings=warnings,
                    build_attempts=build_attempt + 1,
                    task_ids=list(task_ids),
                )
            await asyncio.sleep(self.poll_interval)
        raise DeployMasterBuildFailed(f"{task_id}: build deadline exceeded")
