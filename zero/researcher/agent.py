"""Researcher Claude Code session configuration + runner (doc section 22.1)."""

from __future__ import annotations

from typing import Callable, Optional

from claude_agent_sdk import ClaudeAgentOptions

from zero.claude_runtime import RunResult, TurnEvent, build_env, run_agent, skill_plugins
from zero.config import Config
from zero.researcher.hooks import bash_guard_hook, make_intercept_hook
from zero.researcher.prompts import RESEARCHER_SYSTEM

_ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite",
    "mcp__sandbox__run_in_sandbox",
    "mcp__sandbox__inspect_artifact",
    "mcp__labwright__ensure_environment",
    "mcp__labwright__get_environment_manifest",
    "mcp__labwright__add_resources",
    "mcp__labwright__resolve_environment_decision",
    "mcp__labwright__report_environment_issue",
    "mcp__teacher__ask_teacher",
    "mcp__researcher_skill_capture__propose_reusable_skill",
    "mcp__experience__search_experience",
    "mcp__experience__get_experience",
    "mcp__experience__record_experience",
    "mcp__playground__claim_challenge",
    "mcp__playground__submit_attempt",
    "mcp__playground__check_submission_status",
]


def build_researcher_options(
    config: Config,
    *,
    task_id: str,
    workspace: str,
    mcp_servers: dict,
    on_intercept: Optional[Callable[[str], None]] = None,
    max_turns: int = 60,
) -> ClaudeAgentOptions:
    session_key = f"{task_id}/trace/researcher"
    plugins = skill_plugins(config.researcher_skills_dir)
    return ClaudeAgentOptions(
        system_prompt=RESEARCHER_SYSTEM,
        allowed_tools=_ALLOWED_TOOLS,
        disallowed_tools=["Bash", "WebFetch", "WebSearch", "NotebookEdit"],
        mcp_servers=mcp_servers,
        # acceptEdits auto-approves file edits; every other tool the Researcher
        # may use is pre-approved via allowed_tools, so nothing prompts. (root
        # forbids the --dangerously-skip-permissions flag, and can_use_tool needs
        # streaming mode, so the allowlist is how we stay non-interactive.)
        permission_mode="acceptEdits",
        cwd=workspace,
        env=build_env(config, session_key),
        model=config.model,
        setting_sources=[],
        # Researcher-private skills; "all" enables every skill discovered under
        # the plugin root and auto-adds the Skill tool to the allowlist.
        plugins=plugins,
        skills="all" if plugins else None,
        hooks={"PreToolUse": [make_intercept_hook(on_intercept), bash_guard_hook(on_intercept)]},
        max_turns=max_turns,
    )


async def run_researcher(
    config: Config,
    *,
    task_id: str,
    workspace: str,
    task_prompt: str,
    mcp_servers: dict,
    on_event: Optional[Callable[[TurnEvent], None]] = None,
    on_intercept: Optional[Callable[[str], None]] = None,
    max_turns: int = 60,
) -> RunResult:
    options = build_researcher_options(
        config, task_id=task_id, workspace=workspace, mcp_servers=mcp_servers,
        on_intercept=on_intercept, max_turns=max_turns,
    )
    return await run_agent(task_prompt, options, on_event=on_event)
