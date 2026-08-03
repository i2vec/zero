"""Convert zero's authoritative capgw captures into Playground ARM JSONL."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The uploaded trace should be evidence-rich but compact; the original capture
# remains intact for full replay in zero's viewer.
_MAX_TEXT = 1_200
# Playground rejects oversized bundles with HTTP 413, so the export shrinks text
# and, as a last resort, drops the oldest steps until it fits.
_TEXT_LIMITS = (_MAX_TEXT, 600, 320, 180)
_MAX_BYTES = 300_000
_PRICE_PER_MILLION = {
    "deepseek-v4-pro": (0.27, 1.10),
    "deepseek-v3": (0.14, 0.28),
    "claude-opus-4-7": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-5": (15.0, 75.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.5, 10.0),
}
_FALLBACK_PRICE = (0.50, 1.50)


def convert_capgw_jsonl(path: Path, limit: int = _MAX_TEXT) -> list[dict[str, Any]]:
    """Convert a Researcher capture file without inventing agent activity."""
    previous_messages: list[Any] = []
    seen_messages: set[str] = set()
    steps: list[dict[str, Any]] = []
    outstanding: dict[str, dict[str, Any]] = {}

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise RuntimeError(f"could not read Researcher capture: {exc}") from exc

    for record_index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue

        timestamp = _timestamp(record.get("ts"))
        request = record.get("original_request") if isinstance(record.get("original_request"), dict) else {}
        messages = request.get("messages") if isinstance(request.get("messages"), list) else []
        new_messages = _message_delta(previous_messages, messages)
        for message in new_messages:
            signature = _message_signature(message)
            if signature in seen_messages:
                continue
            seen_messages.add(signature)
            _append_request_steps(steps, outstanding, message, timestamp, record_index, limit)
        previous_messages = messages

        added = _append_response_steps(steps, outstanding, record, timestamp, record_index, limit)
        if not added and record.get("error"):
            steps.append(_step(
                "error", timestamp, record_index, len(steps),
                body=_clip(str(record["error"]), limit),
            ))

    # A terminated run can leave a real tool call without a returned result.
    # Represent that truthfully as an error result so the trace remains a paired
    # chronology rather than silently dropping the call.
    for call_id, call in outstanding.items():
        steps.append(_step(
            "tool_result",
            call["timestamp"],
            len(lines),
            len(steps),
            tool_call_id=call_id,
            tool_output="Tool execution did not return before the Researcher session ended.",
            is_error=True,
        ))
    return steps


def write_arm_jsonl(steps: list[dict[str, Any]], path: Path) -> Path:
    if not steps:
        raise RuntimeError("Researcher capture produced no Playground ARM steps")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for step in steps:
            handle.write(json.dumps(step, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path


def export_capgw_to_arm(
    capture: Path, destination: Path, max_bytes: int = _MAX_BYTES,
) -> Path:
    """Export the capture as ARM JSONL that fits Playground's upload budget."""
    steps: list[dict[str, Any]] = []
    for limit in _TEXT_LIMITS:
        steps = convert_capgw_jsonl(capture, limit)
        if _payload_size(steps) <= max_bytes:
            return write_arm_jsonl(steps, destination)
    return write_arm_jsonl(_drop_to_budget(steps, max_bytes), destination)


def _payload_size(steps: list[dict[str, Any]]) -> int:
    return sum(_step_size(step) for step in steps)


def _step_size(step: dict[str, Any]) -> int:
    return len(json.dumps(step, ensure_ascii=False, separators=(",", ":")).encode()) + 1


def _drop_to_budget(steps: list[dict[str, Any]], max_bytes: int) -> list[dict[str, Any]]:
    """Drop the oldest steps until the payload fits, keeping tool pairs intact."""
    sized = [[_step_size(step), step] for step in steps]
    total = sum(size for size, _ in sized)

    for step_type in ("observation", "thought"):
        index = 0
        while total > max_bytes and index < len(sized):
            if sized[index][1].get("step_type") == step_type:
                total -= sized.pop(index)[0]
                continue
            index += 1

    index = 0
    while total > max_bytes and index < len(sized):
        step = sized[index][1]
        call_id = step.get("tool_call_id") if step.get("step_type") == "tool_call" else None
        if not call_id:
            index += 1
            continue
        remaining: list[list[Any]] = []
        for size, item in sized:
            if item.get("tool_call_id") == call_id:
                total -= size
            else:
                remaining.append([size, item])
        sized = remaining
    return _renumber([step for _, step in sized])


def _renumber(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for order, step in enumerate(steps):
        digest = str(step.get("step_id") or "").rsplit("-", 1)[-1]
        step["step_order"] = order + 1
        step["step_id"] = f"{step.get('step_type')}-{order + 1}-{digest}"
    return steps


def _message_delta(previous: list[Any], current: list[Any]) -> list[Any]:
    if len(current) >= len(previous) and current[:len(previous)] == previous:
        return current[len(previous):]
    return current


def _message_signature(message: Any) -> str:
    try:
        return json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(message)


def _append_request_steps(
    steps: list[dict[str, Any]],
    outstanding: dict[str, dict[str, Any]],
    message: Any,
    timestamp: str,
    record_index: int,
    limit: int,
) -> None:
    if not isinstance(message, dict) or message.get("role") != "user":
        return
    for block in _blocks(message.get("content")):
        kind = block.get("type")
        if kind == "tool_result":
            call_id = str(block.get("tool_use_id") or "")
            if not call_id:
                continue
            steps.append(_step(
                "tool_result", timestamp, record_index, len(steps),
                tool_call_id=call_id,
                tool_output=_content_text(block.get("content"), limit),
                is_error=bool(block.get("is_error")),
            ))
            outstanding.pop(call_id, None)
        elif kind == "text":
            body = str(block.get("text") or "").strip()
            if body:
                steps.append(_step(
                    "observation", timestamp, record_index, len(steps), body=_clip(body, limit),
                ))


def _append_response_steps(
    steps: list[dict[str, Any]],
    outstanding: dict[str, dict[str, Any]],
    record: dict[str, Any],
    timestamp: str,
    record_index: int,
    limit: int,
) -> bool:
    response = record.get("returned_response")
    if not isinstance(response, dict):
        return False
    content = _blocks(response.get("content"))
    reasoning = "\n\n".join(
        str(block.get("thinking") or "") for block in content if block.get("type") == "thinking"
    ).strip()
    text = "\n\n".join(
        str(block.get("text") or "") for block in content if block.get("type") == "text"
    ).strip()
    body_parts = []
    if reasoning:
        body_parts.append(f"[reasoning]\n{reasoning}")
    if text:
        body_parts.append(text)
    metrics = _metrics(record)
    added = False
    if body_parts:
        steps.append(_step(
            "thought", timestamp, record_index, len(steps),
            body=_clip("\n\n".join(body_parts), limit),
            **metrics,
        ))
        metrics = {}
        added = True
    for block in content:
        if block.get("type") != "tool_use":
            continue
        call_id = str(block.get("id") or "")
        if not call_id:
            continue
        steps.append(_step(
            "tool_call", timestamp, record_index, len(steps),
            tool_call_id=call_id,
            tool_name=str(block.get("name") or ""),
            tool_args=_compact_value(
                block.get("input") if isinstance(block.get("input"), dict) else {}, limit,
            ),
            **metrics,
        ))
        metrics = {}
        outstanding[call_id] = {"timestamp": timestamp}
        added = True
    if metrics and added:
        steps[-1].update(metrics)
    return added


def _metrics(record: dict[str, Any]) -> dict[str, Any]:
    upstream = record.get("upstream_response")
    usage = upstream.get("usage") if isinstance(upstream, dict) and isinstance(upstream.get("usage"), dict) else {}
    tokens_in = _int(usage.get("prompt_tokens"))
    tokens_out = _int(usage.get("completion_tokens"))
    if not tokens_in and not tokens_out:
        return {}
    model_id = str(record.get("model_used") or record.get("model_requested") or "")
    return {
        "model_id": model_id,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": estimate_cost_usd(model_id, tokens_in, tokens_out),
    }


def estimate_cost_usd(model_id: str, tokens_in: int, tokens_out: int) -> float:
    lowered = model_id.lower()
    price_in, price_out = next(
        (price for name, price in _PRICE_PER_MILLION.items() if name in lowered),
        _FALLBACK_PRICE,
    )
    return round((tokens_in * price_in + tokens_out * price_out) / 1_000_000, 6)


def _step(kind: str, timestamp: str, record_index: int, order: int, **payload: Any) -> dict[str, Any]:
    digest = hashlib.sha1(f"{kind}:{record_index}:{order}:{timestamp}".encode()).hexdigest()[:12]
    return {
        "step_type": kind,
        "type": kind,
        "step_id": f"{kind}-{order + 1}-{digest}",
        "step_order": order + 1,
        "timestamp": timestamp,
        **payload,
    }


def _timestamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return "1970-01-01T00:00:00Z"


def _blocks(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _content_text(value: Any, limit: int = _MAX_TEXT) -> str:
    if isinstance(value, str):
        return _clip(value, limit)
    if isinstance(value, list):
        return _clip("\n".join(
            str(item.get("text") or item.get("content") or "") if isinstance(item, dict) else str(item)
            for item in value
        ), limit)
    return _clip(str(value or ""), limit)


def _clip(value: str, limit: int = _MAX_TEXT) -> str:
    if len(value) <= limit:
        return value
    tail = min(240, limit // 4)
    head = limit - tail
    return f"{value[:head]}\n… [truncated {len(value) - limit} chars] …\n{value[-tail:]}"


def _compact_value(value: Any, limit: int = _MAX_TEXT) -> Any:
    if isinstance(value, str):
        return _clip(value, limit)
    if isinstance(value, list):
        return [_compact_value(item, limit) for item in value[:100]]
    if isinstance(value, dict):
        return {str(key): _compact_value(item, limit) for key, item in list(value.items())[:100]}
    return value


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
