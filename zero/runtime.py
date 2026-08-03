"""Platform-neutral concurrent runner for applications built on top of zero.

This module deliberately knows nothing about challenge platforms, credentials,
submissions, or scoring.  It owns one capgw process and runs isolated
``Orchestrator`` instances behind a concurrency limit, which is the safe
integration seam for external products such as a competition console.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional

from zero.capgw_runner import CapgwRunner
from zero.config import Config, get_config
from zero.orchestrator.orchestrator import Orchestrator, TaskResult
from zero.preparation import ExternalTaskPreparer


@dataclass(frozen=True)
class RunRequest:
    """A platform-neutral request to execute one scientific task."""

    prompt: str
    run_name: Optional[str] = None
    max_turns: int = 60
    export: bool = True
    preparer: Optional[ExternalTaskPreparer] = None
    mcp_server_factory: Optional[Callable[[Any, str, str], dict[str, Any]]] = None
    task_key: Optional[str] = None
    teacher_enabled: Optional[bool] = None
    hints: Optional[str] = None


class ZeroRuntime:
    """Share capgw safely while executing multiple independent zero runs."""

    def __init__(self, config: Optional[Config] = None, *, max_parallel: int = 1):
        self._config = config or get_config()
        self._capgw = CapgwRunner(self._config)
        self._slots = asyncio.Semaphore(max(1, max_parallel))
        self._started = False
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the single shared model gateway once."""
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("ZeroRuntime is closed")
            if self._started:
                return
            ready = await asyncio.to_thread(self._capgw.ensure)
            if not ready:
                raise RuntimeError("capgw gateway did not come up")
            self._started = True

    async def run(self, request: RunRequest) -> TaskResult:
        """Run one task without letting it manage the shared capgw process."""
        await self.start()
        async with self._slots:
            orchestrator = Orchestrator(
                self._config,
                manage_capgw=False,
                serve_trace=False,
            )
            try:
                return await orchestrator.run_task(
                    request.prompt,
                    max_turns=request.max_turns,
                    run_name=request.run_name,
                    export=request.export,
                    preparer=request.preparer,
                    mcp_server_factory=request.mcp_server_factory,
                    task_key=request.task_key,
                    teacher_enabled=request.teacher_enabled,
                    hints=request.hints,
                )
            finally:
                orchestrator.close()

    async def close(self) -> None:
        """Stop capgw after all callers have finished using this runtime."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            if self._started:
                await asyncio.to_thread(self._capgw.stop)
            self._closed = True
