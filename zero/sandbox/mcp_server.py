"""In-process MCP server for the Researcher's sandbox execution tools.

``run_in_sandbox`` is the Researcher's only way to execute code (doc section
24: the control process is decoupled from the sandbox). The PreToolUse hook
inspects calls to this tool and blocks environment-mutating commands, steering
the Researcher to Labwright instead.
"""

from __future__ import annotations

import asyncio

from claude_agent_sdk import create_sdk_mcp_server, tool

from zero.sandbox.manager import SandboxManager

_MAX_OUTPUT = 20000


def build_sandbox_server(manager: SandboxManager) -> dict:
    @tool("run_in_sandbox",
          "在指定 Sandbox 中执行一条 shell/python 命令并返回 stdout/stderr/exit_code。用于运行实验代码。",
          {"sandbox_id": str, "command": str, "timeout": int})
    async def run_in_sandbox(args):
        sandbox_id = args["sandbox_id"]
        command = args["command"]
        timeout = int(args.get("timeout") or 600)
        result = await asyncio.to_thread(manager.exec, sandbox_id, command, timeout)
        out = result.stdout[-_MAX_OUTPUT:]
        err = result.stderr[-_MAX_OUTPUT:]
        text = f"exit_code: {result.exit_code}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
        return {"content": [{"type": "text", "text": text}]}

    @tool("inspect_artifact",
          "读取 Sandbox workspace 中某个文件的内容（用于查看日志/结果文件）。",
          {"sandbox_id": str, "path": str})
    async def inspect_artifact(args):
        try:
            data = await asyncio.to_thread(manager.get_file, args["sandbox_id"], args["path"])
            text = data.decode("utf-8", errors="replace")[:_MAX_OUTPUT]
        except Exception as exc:  # noqa: BLE001
            text = f"error reading {args['path']}: {exc}"
        return {"content": [{"type": "text", "text": text}]}

    return create_sdk_mcp_server("sandbox", "1.0.0", [run_in_sandbox, inspect_artifact])
