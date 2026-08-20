"""Deterministic Search+Detail and verified publish facade."""

from __future__ import annotations

from typing import Any, Optional
import asyncio
from urllib.parse import urlsplit

from zero.protocol.resources import (
    ArtifactRef, RegistryCandidate, ResourceKind, VerificationEvidence,
)
from zero.resources.errors import (
    RegistryConflict, RegistryUnavailable, RegistryValidationError,
)
from zero.resources.literature_sage import LiteratureSageClient


def _items(body: dict) -> list[dict]:
    data = body.get("data") or {}
    for key in ("items", "tools", "datasets", "models", "list", "records", "results", "details"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, list):
            return value
    if isinstance(data, list):
        return data
    return []


def _key(item: dict) -> str:
    return str(
        item.get("resource_unique_key") or item.get("tool_unique_key")
        or item.get("dataset_unique_key") or item.get("model_unique_key")
        or item.get("unique_key") or item.get("key") or ""
    )


def _capabilities(item: dict) -> list[str]:
    """Flatten the different Search/Detail taxonomy shapes into names."""
    raw = item.get("capabilities") or item.get("capability") or item.get("tags") or []
    found: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
        elif isinstance(value, list):
            for entry in value:
                add(entry)
        elif isinstance(value, dict):
            if isinstance(value.get("name"), str):
                add(value["name"])
            else:
                for entry in value.values():
                    add(entry)

    add(raw)
    return list(dict.fromkeys(found))


def _tag_ids(metadata: dict[str, Any], capabilities: list[Any]) -> list[int]:
    values: list[Any] = list(metadata.get("tag_ids") or [])
    tags = metadata.get("tags") or []
    if isinstance(tags, list):
        values.extend(tag.get("id") for tag in tags if isinstance(tag, dict))
    values.extend(value for value in capabilities if isinstance(value, int)
                  or (isinstance(value, str) and value.isdigit()))
    return list(dict.fromkeys(int(value) for value in values if value not in (None, "")))


def _materializable_uri(kind: ResourceKind, uri: str) -> bool:
    """Reject catalog strings that cannot identify retrievable resource bytes."""
    if kind == ResourceKind.TOOL:
        return bool(uri.strip())
    parsed = urlsplit(uri.strip())
    if parsed.scheme in {"http", "https"}:
        return bool(parsed.netloc)
    if parsed.scheme in {"s3", "gs", "oss", "hf", "trisol"}:
        return bool(parsed.netloc and parsed.path.strip("/"))
    return False


def _same_artifact(left: ArtifactRef, right: ArtifactRef) -> bool:
    a, b = urlsplit(left.uri), urlsplit(right.uri)
    if a.scheme == b.scheme == "trisol":
        return (a.netloc == b.netloc
                and a.path.strip("/").split("/", 1)[0] == b.path.strip("/").split("/", 1)[0])
    return left.uri == right.uri and left.digest == right.digest


def _artifact(kind: ResourceKind, item: dict) -> tuple[Optional[ArtifactRef], list[str]]:
    raw = item.get("artifact") or item.get("version") or item.get("latest_version") or {}
    if not isinstance(raw, dict):
        raw = {}
    if kind == ResourceKind.TOOL:
        # Imports produced by this client use the canonical ArtifactRef shape
        # (``artifact.uri``); older registry records may use image-specific
        # field names.  Search-after-write must understand both forms.
        uri = (raw.get("uri") or raw.get("docker_image_uri") or raw.get("image_uri")
               or raw.get("image") or raw.get("image_url")
               or item.get("docker_image_uri") or item.get("image_uri") or item.get("image"))
        artifact_type = "oci_image"
    else:
        trisol_id = raw.get("trisol_id") or item.get("trisol_id")
        uri = (f"trisol://{kind.value}/{trisol_id}" if trisol_id
               else raw.get("uri") or raw.get("url") or item.get("url") or item.get("source"))
        artifact_type = ("object_bundle" if trisol_id else
                         "hf_snapshot" if isinstance(uri, str) and "huggingface" in uri else "url")
    if not uri:
        return None, ["missing_artifact"]
    if not _materializable_uri(kind, str(uri)):
        return None, ["invalid_artifact_uri"]
    digest = (raw.get("digest") or raw.get("docker_image_id") or raw.get("image_digest")
              or item.get("docker_image_id") or item.get("digest") or item.get("sha256"))
    warnings = [] if digest else ["mutable_reference"]
    version = raw.get("version")
    if not isinstance(version, str):
        version = item.get("version") if isinstance(item.get("version"), str) else None
    return ArtifactRef(
        type=artifact_type, uri=str(uri), digest=digest,
        version=version,
        revision=raw.get("revision") or item.get("revision"),
        platform=raw.get("platform") or item.get("platform"),
        format=raw.get("format") or item.get("format"),
        size_bytes=raw.get("size_bytes") or item.get("size_bytes"),
    ), warnings


class ResourceRegistry:
    def __init__(self, client: LiteratureSageClient):
        self.client = client

    async def search(self, *, kind: ResourceKind, text: str,
                     keywords: Optional[dict[str, float]] = None, language: str = "en-US",
                     limit: int = 10, required_capabilities: Optional[list[str]] = None,
                     constraints: Optional[dict[str, Any]] = None) -> list[RegistryCandidate]:
        payload = {"text": text, "language": language, "k": limit}
        if kind == ResourceKind.TOOL:
            payload["keywords"] = keywords or {}
        search = await self.client.search(kind.value, payload)
        search_items = _items(search)
        keys = [_key(item) for item in search_items if _key(item)]
        if not keys:
            return []
        details = await self.client.detail(kind.value, keys)
        by_key = {_key(item): item for item in _items(details) if _key(item)}
        score_by_key = {_key(item): item.get("score") for item in search_items}
        required = set(required_capabilities or [])
        platform = (constraints or {}).get("platform")
        candidates: list[RegistryCandidate] = []
        for unique_key in keys:
            item = by_key.get(unique_key)
            if not item or item.get("status") not in (None, 0, "0", "active", "enabled"):
                continue
            artifact, warnings = _artifact(kind, item)
            capabilities = _capabilities(item)
            if isinstance(capabilities, str):
                capabilities = [capabilities]
            capset = set(map(str, capabilities))
            if required and not required.issubset(capset):
                continue
            if platform and artifact and artifact.platform and artifact.platform != platform:
                continue
            name = str(item.get("name") or item.get(f"{kind.value}_name")
                       or item.get("resource_name") or unique_key)
            exact = text.casefold() in {name.casefold(), unique_key.casefold()}
            match = "exact" if exact else ("compatible" if required and required.issubset(capset) else "partial")
            # A catalog record without materializable bytes is not a usable
            # resource candidate.  Search remains a catalog operation, but the
            # Labwright must never mistake metadata-only hits for artifacts.
            if artifact is None:
                continue
            candidates.append(RegistryCandidate(
                kind=kind, resource_unique_key=unique_key, name=name, match=match,
                score=score_by_key.get(unique_key), artifact=artifact,
                capabilities=list(map(str, capabilities)), license=item.get("license"),
                entry_command=(item.get("usage_entry_command") or item.get("entry_command")
                               or item.get("entrypoint") or item.get("command")),
                verification=VerificationEvidence(status="unknown"), warnings=warnings,
                metadata=item,
            ))
        return candidates

    async def publish(self, *, kind: ResourceKind, unique_key: str, metadata: dict[str, Any],
                      artifact: ArtifactRef, verification: VerificationEvidence,
                      capabilities: Optional[list[str]] = None) -> RegistryCandidate:
        unique_key = unique_key.strip()
        if not unique_key:
            raise RegistryValidationError("resource unique key is required")
        if verification.status != "passed":
            raise RegistryValidationError("only verified resources may be published")
        if not artifact.uri:
            raise RegistryValidationError("artifact URI is required")
        if kind == ResourceKind.TOOL and artifact.type != "oci_image":
            raise RegistryValidationError("tool artifacts must be OCI images")
        if kind != ResourceKind.TOOL and artifact.type == "oci_image":
            raise RegistryValidationError("model/dataset artifacts cannot be OCI images")
        existing = await self.client.detail(kind.value, [unique_key])
        existing_items = [item for item in _items(existing) if _key(item) == unique_key]
        if existing_items:
            old_artifact, _ = _artifact(kind, existing_items[0])
            if old_artifact and _same_artifact(old_artifact, artifact):
                result = await self.search(
                    kind=kind, text=str(metadata.get("name") or unique_key), limit=10,
                )
                return next((item for item in result if item.resource_unique_key == unique_key), RegistryCandidate(
                    kind=kind, resource_unique_key=unique_key, name=str(metadata.get("name") or unique_key),
                    match="exact", artifact=artifact, verification=verification,
                    capabilities=capabilities or [], warnings=[] if artifact.digest else ["mutable_reference"],
                ))
            raise RegistryConflict(f"unique key already exists with a different artifact: {unique_key}")
        payload = dict(metadata)
        name = str(payload.pop("name", "") or unique_key)
        payload[f"{kind.value}_unique_key"] = unique_key
        payload.setdefault(f"{kind.value}_name", name)
        tag_ids = _tag_ids(payload, capabilities or [])
        if not tag_ids:
            raise RegistryValidationError(
                "Literature Sage import requires at least one taxonomy tag ID; "
                "pass metadata.tag_ids from a related Search+Detail candidate"
            )
        payload["tag_ids"] = tag_ids
        payload.pop("tags", None)
        if not payload.get("infos"):
            profile = str(payload.get("original_profile") or payload.get("description") or name)
            payload["infos"] = [{
                "language": "en-US", f"{kind.value}_profile": profile,
                "page_summary": profile, "key_points": ";".join(map(str, capabilities or [name])),
                "llm_detailed_description": str(payload.get("original_description") or profile),
            }]
        if kind == ResourceKind.TOOL:
            version = dict(payload.pop("version", {}) or {})
            version.setdefault("docker_image_uri", artifact.uri)
            if artifact.digest:
                version.setdefault("docker_image_id", artifact.digest)
            payload["version"] = version
        else:
            parsed = urlsplit(artifact.uri)
            if parsed.scheme == "trisol":
                payload.setdefault("trisol_id", parsed.path.strip("/").split("/", 1)[0])
            else:
                payload.setdefault("url", artifact.uri)
        try:
            await self.client.import_resource(kind.value, payload)
        except RegistryUnavailable:
            # Import timeouts have an ambiguous outcome.  Never blindly retry:
            # first resolve the unique key and accept only the exact artifact.
            recovered = await self.client.detail(kind.value, [unique_key])
            recovered_items = [item for item in _items(recovered) if _key(item) == unique_key]
            if not recovered_items:
                raise
            recovered_artifact, _ = _artifact(kind, recovered_items[0])
            if not recovered_artifact or recovered_artifact.uri != artifact.uri or recovered_artifact.digest != artifact.digest:
                raise RegistryConflict(
                    f"ambiguous import created a different artifact: {unique_key}"
                )
        found = None
        for attempt in range(4):
            result = await self.search(kind=kind, text=name, limit=20)
            found = next((item for item in result if item.resource_unique_key == unique_key), None)
            if found is not None:
                return found
            await asyncio.sleep(0.25 * (2 ** attempt))
        # Some indexes do not tokenize opaque unique keys or are eventually
        # consistent beyond the bounded wait. Detail is the authoritative
        # read-after-write check and prevents a duplicate retry.
        confirmed = await self.client.detail(kind.value, [unique_key])
        item = next((value for value in _items(confirmed) if _key(value) == unique_key), None)
        if item is None:
            raise RegistryValidationError("resource missing after successful import")
        confirmed_artifact, _ = _artifact(kind, item)
        if confirmed_artifact is None or not _same_artifact(confirmed_artifact, artifact):
            raise RegistryConflict(f"imported resource points to a different artifact: {unique_key}")
        return RegistryCandidate(
            kind=kind, resource_unique_key=unique_key, name=name, match="exact",
            artifact=artifact, verification=verification,
            capabilities=capabilities or [], warnings=[] if artifact.digest else ["mutable_reference"],
            metadata=item,
        )
