"""Low-level Literature Sage HTTP adapter with bounded retry."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import httpx

from zero.resources.errors import (
    RegistryBusinessError, RegistryIndexError, RegistryRateLimited,
    RegistryUnavailable,
)


class LiteratureSageClient:
    def __init__(self, base_url: str, *, timeout: float = 30, max_connections: int = 1,
                 max_retries: int = 4, auth_token: str = "",
                 proxy_url: str = "",
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.max_retries = max(0, max_retries)
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, headers=headers,
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
            proxy=proxy_url or None,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        return await self.request("GET", "/health", check_business=False)

    async def request(self, method: str, path: str, *, json_body: Optional[dict] = None,
                      check_business: bool = True, import_request: bool = False) -> dict[str, Any]:
        last: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.request(method, path, json=json_body)
            except httpx.TransportError as exc:
                last = exc
                if import_request:
                    raise RegistryUnavailable(f"ambiguous import outcome: {exc}") from exc
                if attempt < self.max_retries:
                    await asyncio.sleep(min(0.1 * (2 ** attempt), 2.0))
                    continue
                raise RegistryUnavailable(str(exc)) from exc
            if response.status_code in (429, 502, 503):
                last = RegistryRateLimited(f"HTTP {response.status_code}")
                if attempt < self.max_retries:
                    await asyncio.sleep(min(0.1 * (2 ** attempt), 2.0))
                    continue
                raise last
            if not 200 <= response.status_code < 300:
                raise RegistryUnavailable(f"HTTP {response.status_code}: {response.text[:300]}")
            try:
                body = response.json()
            except ValueError as exc:
                # The deployed health endpoint currently prefixes its JSON
                # payload with a plain-text readiness marker (for example
                # ``ok{\"status\":\"ok\"}``).  Keep business endpoints strict,
                # but accept that health-only wire format so the read-only
                # connectivity smoke reflects service availability.
                if not check_business:
                    text = response.text.strip()
                    json_start = text.find("{")
                    if json_start >= 0:
                        try:
                            body = json.loads(text[json_start:])
                        except ValueError:
                            body = {"status": text}
                    else:
                        body = {"status": text}
                else:
                    raise RegistryBusinessError("non-JSON registry response") from exc
            if check_business and body.get("code") != 0:
                raise RegistryBusinessError(str(body.get("message") or body.get("msg") or body))
            if import_request and (body.get("data") or {}).get("index_status") != "built":
                raise RegistryIndexError(str((body.get("data") or {}).get("index_status") or "missing index_status"))
            return body
        raise RegistryUnavailable(str(last or "request failed"))

    async def search(self, kind: str, payload: dict) -> dict:
        return await self.request("POST", f"/api/v1/{kind}/search/hybrid", json_body=payload)

    async def detail(self, kind: str, unique_keys: list[str]) -> dict:
        key_field = {
            "tool": "tool_unique_keys",
            "dataset": "dataset_unique_keys",
            "model": "model_unique_keys",
        }.get(kind)
        if key_field is None:
            raise ValueError(f"unsupported Literature Sage resource kind: {kind}")
        return await self.request("POST", f"/api/v1/{kind}/batch/detail",
                                  json_body={key_field: unique_keys})

    async def import_resource(self, kind: str, payload: dict) -> dict:
        return await self.request("POST", f"/api/v1/{kind}_inner/import",
                                  json_body=payload, import_request=True)
