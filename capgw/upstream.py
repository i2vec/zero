"""Upstream client: forwards OpenAI-Chat requests to the configured endpoint.

Differences from Polar's InferenceClient (intentional):
- Injects ``Authorization: Bearer <api_key>`` so it can talk to authenticated
  remote endpoints (Polar's client only sent Content-Type).
- Passthrough only: it does NOT inject training params (``logprobs`` /
  ``return_token_ids``) that remote closed endpoints reject.
- Always calls the upstream non-streaming and returns the full response, so the
  gateway can both capture the complete body and synthesize a stream from it.
"""

from __future__ import annotations

from typing import Any

import httpx

from capgw.config import Config


class UpstreamError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"upstream returned {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


class UpstreamClient:
    """Thin async client around the upstream chat/completions endpoint."""

    def __init__(self, config: Config):
        self._config = config
        self._client = httpx.AsyncClient(timeout=config.request_timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    def prepare_request(self, chat_request: dict[str, Any]) -> dict[str, Any]:
        """Build the exact JSON body sent upstream.

        Passthrough of the canonical OpenAI-Chat request produced by the
        transformer, with two adjustments:
        - ``model`` forced to the configured upstream model.
        - ``stream`` forced off (we always fetch the full response).
        """
        body = dict(chat_request)
        body["model"] = self._config.model
        body.pop("stream", None)
        return body

    async def chat_completion(self, chat_request: dict[str, Any]) -> dict[str, Any]:
        """POST to the upstream and return the parsed non-streaming response."""
        body = self.prepare_request(chat_request)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        }
        response = await self._client.post(
            self._config.chat_completions_url,
            json=body,
            headers=headers,
        )
        if response.status_code >= 400:
            raise UpstreamError(response.status_code, response.text)
        return response.json()
