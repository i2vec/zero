"""PreToolUse interception hook (doc section 18.2).

If the Researcher tries to run an environment-mutating command inside the
sandbox (pip/apt/conda/docker), the hook denies it and steers it to Labwright.
This keeps environment engineering out of the Researcher's job by construction.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from claude_agent_sdk import HookContext, HookMatcher

# Commands that must go through Labwright, not be run directly.
_FORBIDDEN = re.compile(
    r"\b(pip\s+install|pip3\s+install|python\s+-m\s+pip\s+install|"
    r"apt(-get)?\s+install|conda\s+(install|create)|mamba\s+install|"
    r"docker\s+(pull|build|run)|uv\s+pip\s+install)\b",
    re.IGNORECASE,
)


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def make_intercept_hook(on_intercept: Callable[[str], None] | None = None):
    async def hook(input_data: dict[str, Any], tool_use_id: str | None, context: HookContext) -> dict:
        tool_name = input_data.get("tool_name") or input_data.get("toolName") or ""
        tool_input = input_data.get("tool_input") or input_data.get("toolInput") or {}
        command = ""
        if isinstance(tool_input, dict):
            command = str(tool_input.get("command", ""))

        if _FORBIDDEN.search(command):
            reason = (
                "该命令会改变实验环境（安装/构建依赖），这属于 Labwright 的职责。"
                "请不要直接安装，改用 mcp__labwright__add_resources 声明所需资源，"
                "或在 ensure_environment 的 spec 中加入该依赖。"
            )
            if on_intercept is not None:
                on_intercept(command)
            return _deny(reason)
        return {}

    return HookMatcher(matcher="mcp__sandbox__run_in_sandbox", hooks=[hook])


def bash_guard_hook(on_intercept: Callable[[str], None] | None = None):
    """Also guard the built-in Bash tool in case it is ever enabled."""
    async def hook(input_data: dict[str, Any], tool_use_id: str | None, context: HookContext) -> dict:
        tool_input = input_data.get("tool_input") or input_data.get("toolInput") or {}
        command = str(tool_input.get("command", "")) if isinstance(tool_input, dict) else ""
        if _FORBIDDEN.search(command):
            if on_intercept is not None:
                on_intercept(command)
            return _deny("环境安装类命令请交给 Labwright，Researcher 不直接安装依赖。")
        return {}

    return HookMatcher(matcher="Bash", hooks=[hook])
