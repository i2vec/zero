"""Teacher full agent: a persistent ClaudeSDKClient session.

Like Labwright (and unlike the Researcher's one-shot ``query()``), the Teacher
keeps one session alive for the whole task, so it remembers what it has already
disclosed and can keep escalating hints instead of repeating itself.

The session is created lazily on the first ask: a run where nobody asks the
Teacher anything spends no tokens and behaves exactly as before.
"""

from __future__ import annotations

from typing import Callable, Optional

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from zero.claude_runtime import RunResult, TurnEvent, build_env, consume_message, skill_plugins
from zero.config import Config
from zero.teacher.prompts import TEACHER_SYSTEM
from zero.teacher.tools import TeacherContext, build_hintbank_server

_ALLOWED_TOOLS = [
    # Hint bank only. No file/shell/sandbox access: the Researcher's workspace
    # and trajectory stay invisible, so the Teacher answers the question it was
    # asked rather than quietly taking over the experiment.
    "mcp__hintbank__read_hint_bank",
    "mcp__hintbank__give_hint",
    "mcp__hintbank__amend_task_statement",
    "mcp__hintbank__decline",
]


class TeacherAgent:
    """Persistent Teacher Claude Code session bound to one task."""

    def __init__(
        self,
        config: Config,
        *,
        task_id: str,
        ctx: TeacherContext,
        on_event: Optional[Callable[[TurnEvent], None]] = None,
        max_turns: int = 12,
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
        session_key = f"{self._task_id}/trace/teacher"
        plugins = skill_plugins(self._config.teacher_skills_dir)
        return ClaudeAgentOptions(
            system_prompt=TEACHER_SYSTEM,
            allowed_tools=_ALLOWED_TOOLS,
            disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep",
                              "WebFetch", "WebSearch", "NotebookEdit"],
            mcp_servers={"hintbank": build_hintbank_server(self._ctx)},
            permission_mode="acceptEdits",
            cwd=self._cwd,
            env=build_env(self._config, session_key),
            model=self._config.model,
            setting_sources=[],
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
        """Send one question and drain until ResultMessage."""
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
