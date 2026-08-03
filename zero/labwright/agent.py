"""Labwright full agent: a persistent ClaudeSDKClient session.

Unlike Researcher (one-shot ``query()``), Labwright keeps one Claude Code
session alive for the whole task so it retains memory across
ensure_environment / add_resources / resolve_decision turns.
"""

from __future__ import annotations

from typing import Callable, Optional

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from zero.claude_runtime import RunResult, TurnEvent, build_env, consume_message, skill_plugins
from zero.config import Config
from zero.labwright.prompts import LABWRIGHT_SYSTEM
from zero.labwright.tools import LabwrightContext, build_labenv_server

_ALLOWED_TOOLS = [
    # Read-only file access: environment debugging genuinely needs to read
    # manifests / lockfiles / logs, and it lets reference-based skills work.
    # Write/Edit stay off — authoring experiment code is the Researcher's job.
    "Read", "Glob", "Grep",
    "mcp__labenv__create_sandbox",
    "mcp__labenv__sandbox_exec",
    "mcp__labenv__collect_resource",
    "mcp__labenv__mount_resource",
    "mcp__labenv__verify_resource",
    "mcp__labenv__publish_manifest",
    "mcp__labenv__request_researcher_decision",
    "mcp__labenv__mark_failed",
    "mcp__labenv__propose_reusable_skill",
]


class LabwrightAgent:
    """Persistent Labwright Claude Code session bound to one task."""

    def __init__(
        self,
        config: Config,
        *,
        task_id: str,
        ctx: LabwrightContext,
        on_event: Optional[Callable[[TurnEvent], None]] = None,
        max_turns: int = 40,
        cwd: Optional[str] = None,
    ):
        self._config = config
        self._task_id = task_id
        self._ctx = ctx
        self._on_event = on_event
        self._max_turns = max_turns
        self._cwd = cwd or str(config.ensure_run_dirs(task_id) / "workspace")
        self._client: Optional[ClaudeSDKClient] = None
        self._connected = False

    def _options(self) -> ClaudeAgentOptions:
        session_key = f"{self._task_id}/trace/labwright"
        plugins = skill_plugins(self._config.labwright_skills_dir)
        return ClaudeAgentOptions(
            system_prompt=LABWRIGHT_SYSTEM,
            allowed_tools=_ALLOWED_TOOLS,
            disallowed_tools=["Bash", "WebFetch", "WebSearch", "NotebookEdit",
                              "Write", "Edit"],
            mcp_servers={"labenv": build_labenv_server(self._ctx)},
            permission_mode="acceptEdits",
            cwd=self._cwd,
            env=build_env(self._config, session_key),
            model=self._config.model,
            setting_sources=[],
            # Labwright-private skills (see plugin root under agent_skills/labwright).
            plugins=plugins,
            skills="all" if plugins else None,
            max_turns=self._max_turns,
        )

    async def start(self) -> None:
        if self._connected:
            return
        self._client = ClaudeSDKClient(options=self._options())
        await self._client.connect()
        self._connected = True

    async def run_turn(self, prompt: str) -> RunResult:
        """Send one user message and drain until ResultMessage."""
        if not self._connected or self._client is None:
            await self.start()
        assert self._client is not None

        result = RunResult()

        def emit(ev: TurnEvent) -> None:
            result.events.append(ev)
            if self._on_event:
                self._on_event(ev)

        await self._client.query(prompt)
        async for message in self._client.receive_response():
            consume_message(message, result, emit)
        return result

    async def close(self) -> None:
        if self._client is not None and self._connected:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._connected = False
        self._client = None
