"""FastAPI capture gateway.

Exposes an Anthropic / OpenAI-Chat / OpenAI-Responses / Gemini compatible
surface. For every request it:

1. detects the incoming API type,
2. transforms it into a canonical OpenAI-Chat request,
3. forwards it (non-streaming) to the single configured upstream,
4. records the full exchange to ``captures/<session_id>.jsonl``,
5. returns the response in the client's original API shape (synthesizing an
   SSE stream from the full response when the client asked to stream).

This is a stripped-down, self-contained descendant of Polar's gateway server:
no session registry, node manager, storage backend, or training-signal
injection — just transparent proxying + capture.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from capgw.capture import CaptureWriter, derive_session_id
from capgw.config import Config
from capgw.detection import APIType, detect, extract_model
from capgw.transform import TransformManager
from capgw.transform.base import BaseTransformer
from capgw.upstream import UpstreamClient, UpstreamError

logger = logging.getLogger("capgw")


# --------------------------------------------------------------------------- #
# SSE formatting (adapted from polar.gateway.server)
# --------------------------------------------------------------------------- #
def _format_anthropic_events(events: list[dict[str, Any]]) -> str:
    parts = []
    for event in events:
        event_type = event.get("type", "unknown")
        parts.append(f"event: {event_type}\ndata: {json.dumps(event)}\n\n")
    return "".join(parts)


def _format_responses_events(events: list[dict[str, Any]]) -> str:
    parts = []
    for event in events:
        event_type = event.get("type", "unknown")
        parts.append(f"event: {event_type}\ndata: {json.dumps(event)}\n\n")
    return "".join(parts)


def _format_openai_sse(chunk: dict[str, Any]) -> str:
    return f"data: {json.dumps(chunk, default=str)}\n\n"


def _format_google_sse(chunk: dict[str, Any]) -> str:
    return f"data: {json.dumps(chunk)}\n\n"


def _format_stream_events(api_type: APIType, events: list[dict[str, Any]]) -> str:
    if api_type == APIType.ANTHROPIC:
        return _format_anthropic_events(events)
    if api_type == APIType.OPENAI_RESPONSES:
        return _format_responses_events(events)
    if api_type == APIType.GOOGLE:
        return _format_google_sse(events[0]) if events else ""
    return _format_openai_sse(events[0]) if events else ""


def _format_stream_output(
    api_type: APIType,
    transformer: BaseTransformer,
    chunk: dict[str, Any],
    original_request: dict[str, Any],
    is_first: bool,
) -> str:
    transformed = transformer.transform_stream_chunk(chunk, original_request, is_first=is_first)
    if api_type == APIType.ANTHROPIC:
        return _format_anthropic_events(transformed)
    if api_type == APIType.OPENAI_RESPONSES:
        if isinstance(transformed, list):
            return _format_responses_events(transformed)
        return _format_responses_events([transformed]) if transformed else ""
    if api_type == APIType.GOOGLE:
        return _format_google_sse(transformed)
    return _format_openai_sse(transformed)


def _response_to_stream_chunk(response: dict[str, Any]) -> dict[str, Any]:
    """Convert a non-streaming chat completion into a single 'delta' chunk
    suitable for a transformer's stream_state.process_chunk."""
    choices = response.get("choices") or [{}]
    choice = choices[0]
    message = choice.get("message", {}) or {}

    tool_calls_delta: list[dict[str, Any]] = []
    for i, tc in enumerate(message.get("tool_calls") or []):
        func = tc.get("function", {}) or {}
        tool_calls_delta.append(
            {
                "index": i,
                "id": tc.get("id"),
                "type": tc.get("type", "function"),
                "function": {
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", ""),
                },
            }
        )

    delta: dict[str, Any] = {"role": "assistant"}
    if message.get("content") is not None:
        delta["content"] = message.get("content")
    if message.get("reasoning_content") is not None:
        delta["reasoning_content"] = message.get("reasoning_content")
    if tool_calls_delta:
        delta["tool_calls"] = tool_calls_delta

    return {
        "id": response.get("id"),
        "object": "chat.completion.chunk",
        "created": response.get("created"),
        "model": response.get("model"),
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": choice.get("finish_reason"),
            }
        ],
        "usage": response.get("usage"),
    }


def _build_error_body(api_type: APIType, message: str) -> dict[str, Any]:
    if api_type == APIType.ANTHROPIC:
        return {"type": "error", "error": {"type": "api_error", "message": message}}
    if api_type == APIType.GOOGLE:
        return {"error": {"message": message, "status": "INTERNAL"}}
    return {"error": {"message": message, "type": "upstream_error"}}


def _stream_error_output(api_type: APIType, message: str) -> str:
    if api_type == APIType.ANTHROPIC:
        return _format_anthropic_events(
            [{"type": "error", "error": {"type": "upstream_error", "message": message}}]
        )
    if api_type == APIType.OPENAI_RESPONSES:
        return _format_responses_events([{"type": "error", "message": message}])
    if api_type == APIType.GOOGLE:
        return _format_google_sse({"error": {"message": message, "status": "INTERNAL"}})
    return _format_openai_sse({"error": {"message": message, "type": "upstream_error"}})


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="capgw", version="0.1.0")

    transform_manager = TransformManager()
    upstream = UpstreamClient(config)
    capture = CaptureWriter(
        out_dir=config.out_dir,
        upstream_endpoint=config.endpoint,
        model_used=config.model,
        name=config.name,
    )

    app.state.config = config
    app.state.upstream = upstream

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await upstream.aclose()

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {"status": "ok", "service": "capgw", **config.redacted()}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "upstream": config.endpoint, "model": config.model}

    @app.api_route("/{path:path}", methods=["POST"])
    async def proxy_request(request: Request, path: str):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

        headers = {k: v for k, v in request.headers.items()}
        full_path = request.url.path
        query = dict(request.query_params)

        api_type = detect(full_path, headers, body)
        session_id = derive_session_id(headers, query)
        original_model = extract_model(api_type, body)
        transformer = transform_manager.get(api_type)

        logger.info(
            "\u2190 POST %s | api=%s model=%s session=%s",
            full_path,
            api_type.value,
            original_model,
            session_id,
        )

        # Google's streaming is signalled by the path, not the body.
        if api_type == APIType.GOOGLE and "streamGenerateContent" in full_path:
            body["_streaming"] = True

        transformed_body = body.copy()
        transformed_body["_polar_model_served"] = config.model
        openai_request = transformer.transform_request(transformed_body)
        openai_request["model"] = config.model
        is_streaming = bool(openai_request.get("stream", False))

        # Always fetch the full (non-streaming) upstream response.
        try:
            response = await upstream.chat_completion(openai_request)
        except UpstreamError as exc:
            await capture.write(
                session_id=session_id,
                api_type=api_type.value,
                model_requested=original_model,
                original_request=body,
                upstream_request=upstream.prepare_request(openai_request),
                upstream_response=None,
                returned_response=None,
                streamed=is_streaming,
                error=str(exc),
            )
            logger.warning("upstream error (session=%s): %s", session_id, exc)
            if is_streaming:
                return StreamingResponse(
                    _single_chunk_stream(_stream_error_output(api_type, str(exc))),
                    media_type="text/event-stream",
                )
            return JSONResponse(
                _build_error_body(api_type, str(exc)),
                status_code=exc.status_code if 400 <= exc.status_code < 600 else 502,
            )

        transformed = transformer.transform_response(response, body)

        await capture.write(
            session_id=session_id,
            api_type=api_type.value,
            model_requested=original_model,
            original_request=body,
            upstream_request=upstream.prepare_request(openai_request),
            upstream_response=response,
            returned_response=transformed,
            streamed=is_streaming,
        )

        if not is_streaming:
            return JSONResponse(transformed)

        return _synthetic_stream_response(api_type, transformer, response, body)

    return app


async def _single_chunk_stream(payload: str):
    yield payload


def _synthetic_stream_response(
    api_type: APIType,
    transformer: BaseTransformer,
    response: dict[str, Any],
    original_request: dict[str, Any],
) -> StreamingResponse:
    synthetic_chunk = _response_to_stream_chunk(response)
    stream_state = transformer.create_stream_state(original_request)

    async def generate():
        try:
            if stream_state is not None:
                events = stream_state.process_chunk(synthetic_chunk, is_first=True)
                if events:
                    yield _format_stream_events(api_type, events)
                final_events = stream_state.finalize()
                if final_events:
                    yield _format_stream_events(api_type, final_events)
            else:
                output = _format_stream_output(
                    api_type, transformer, synthetic_chunk, original_request, True
                )
                if output:
                    yield output
            if api_type == APIType.OPENAI_CHAT:
                yield "data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.error("synthetic stream error: %s", exc)
            yield _stream_error_output(api_type, str(exc))

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
