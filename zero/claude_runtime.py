"""Shared Claude Code (Claude Agent SDK) runtime.

Both agents are separate SDK sessions pointed at the local model through capgw.
The session key (``<task_id>/trace/<agent>``) is passed as ``ANTHROPIC_API_KEY``
so capgw writes each agent's model-call trajectory under
``runs/<task_id>/trace/<agent>.jsonl``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from zero.config import Config


async def allow_all(tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext) -> PermissionResultAllow:
    """Programmatic permission handler.

    Running as root forbids the CLI's --dangerously-skip-permissions flag, so we
    grant permission programmatically instead. Tool restriction is still enforced
    via allowed_tools/disallowed_tools, and PreToolUse hooks still run first and
    can deny (e.g. the pip/apt/docker interception).
    """
    return PermissionResultAllow()


def build_env(config: Config, session_key: str) -> dict[str, str]:
    """Environment for a spawned Claude Code process pointed at capgw."""
    env = {
        "ANTHROPIC_BASE_URL": config.capgw_url,
        "ANTHROPIC_API_KEY": session_key,          # capgw uses this as the session/namespace key
        "ANTHROPIC_AUTH_TOKEN": session_key,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_CLIENT_CERT": "",
        # Always keep localhost (capgw) off any host proxy.
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    # Preserve HF token / proxy if present for Labwright's collection.
    for k in ("HF_TOKEN", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    # Merge any existing NO_PROXY with localhost entries.
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if existing:
        merged = ",".join(dict.fromkeys(
            [p.strip() for p in (existing + ",127.0.0.1,localhost").split(",") if p.strip()]
        ))
        env["NO_PROXY"] = merged
        env["no_proxy"] = merged
    return env


def skill_plugins(skills_dir: str | os.PathLike[str]) -> list[dict[str, str]]:
    """Local-plugin config for an agent's private skills dir.

    Each agent points at its own plugin root (``<dir>/skills/<name>/SKILL.md``);
    the CLI loads it via ``--plugin-dir``, so the two agents' skills stay
    isolated. Returns an empty list when the dir is absent so a fresh checkout
    without installed skills degrades to "no skills" instead of erroring.
    """
    p = Path(skills_dir)
    return [{"type": "local", "path": str(p)}] if p.is_dir() else []


@dataclass
class TurnEvent:
    kind: str                       # thinking | text | tool_use | tool_result | result | system
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    final_text: str = ""
    is_error: bool = False
    num_turns: int = 0
    tool_uses: list[dict[str, Any]] = field(default_factory=list)
    events: list[TurnEvent] = field(default_factory=list)


def consume_message(message: Any, result: RunResult, emit: Callable[[TurnEvent], None]) -> None:
    """Parse one SDK message into TurnEvents, updating ``result`` in place.

    Shared by the one-shot ``query()`` runner and the persistent
    ``ClaudeSDKClient`` session (Labwright), so both agents emit identical
    trajectory events.
    """
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                emit(TurnEvent("text", {"text": block.text}))
            elif isinstance(block, ThinkingBlock):
                emit(TurnEvent("thinking", {"text": block.thinking}))
            elif isinstance(block, ToolUseBlock):
                tu = {"id": block.id, "name": block.name, "input": block.input}
                result.tool_uses.append(tu)
                emit(TurnEvent("tool_use", tu))
    elif isinstance(message, UserMessage):
        for block in getattr(message, "content", []) or []:
            if isinstance(block, ToolResultBlock):
                emit(TurnEvent("tool_result", {
                    "tool_use_id": block.tool_use_id,
                    "content": _stringify(block.content),
                    "is_error": bool(getattr(block, "is_error", False)),
                }))
    elif isinstance(message, SystemMessage):
        emit(TurnEvent("system", {"subtype": getattr(message, "subtype", "")}))
    elif isinstance(message, ResultMessage):
        result.final_text = getattr(message, "result", "") or ""
        result.is_error = bool(getattr(message, "is_error", False))
        result.num_turns = int(getattr(message, "num_turns", 0) or 0)
        emit(TurnEvent("result", {
            "final_text": result.final_text,
            "is_error": result.is_error,
            "num_turns": result.num_turns,
        }))


async def run_agent(
    prompt: str,
    options: ClaudeAgentOptions,
    on_event: Optional[Callable[[TurnEvent], None]] = None,
) -> RunResult:
    """Drive one agent's full autonomous loop to completion (one-shot session)."""
    result = RunResult()

    def emit(ev: TurnEvent) -> None:
        result.events.append(ev)
        if on_event:
            on_event(ev)

    async for message in query(prompt=prompt, options=options):
        consume_message(message, result, emit)
    return result


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", "") if item.get("type") == "text" else str(item))
            else:
                parts.append(getattr(item, "text", str(item)))
        return "\n".join(parts)
    return str(content)
