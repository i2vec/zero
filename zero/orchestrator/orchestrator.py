"""The 0号机 orchestrator.

Receives a research task, creates the task_id + workspace, assigns each agent
its capgw session key, registers Labwright as MCP tools for the Researcher,
routes/records everything, and returns the result + a trace index.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from zero.capgw_runner import CapgwRunner
from zero.claude_runtime import TurnEvent
from zero.config import Config, get_config
from zero.experience.mcp_server import build_experience_server
from zero.experience.store import ExperienceStore
from zero.export import export_run
from zero.labwright.mcp_server import build_labwright_server
from zero.labwright.service import LabwrightService
from zero.preparation import ExternalTaskPreparer
from zero.researcher.agent import run_researcher
from zero.sandbox.manager import SandboxManager
from zero.sandbox.mcp_server import build_sandbox_server
from zero.skills.candidates import SkillCandidates
from zero.skills.mcp_server import build_skill_capture_server
from zero.state.db import StateDB, TaskRecord
from zero.teacher.mcp_server import build_teacher_server
from zero.teacher.service import TeacherService
from zero.trace.correlate import correlate_traces
from zero.trace.events import TraceWriter
from zero.trace.server import TraceViewerServer


@dataclass
class TaskResult:
    task_id: str
    status: str
    conclusion: str = ""
    final_text: str = ""
    workspace: str = ""
    sandbox_ids: list[str] = field(default_factory=list)
    backend: str = ""
    trace_index: dict[str, Any] = field(default_factory=dict)
    interceptions: int = 0
    export_dir: str = ""
    teacher_stats: dict[str, Any] = field(default_factory=dict)


class Orchestrator:
    def __init__(
        self,
        config: Optional[Config] = None,
        *,
        manage_capgw: bool = True,
        serve_trace: bool = True,
    ):
        self._config = config or get_config()
        self._db = StateDB(self._config)
        self._capgw = CapgwRunner(self._config)
        self._manage_capgw = manage_capgw
        self._viewer: Optional[TraceViewerServer] = (
            TraceViewerServer(
                self._config.runs_dir,
                host=self._config.trace_ui_host,
                port=self._config.trace_ui_port,
            )
            if serve_trace
            else None
        )

    @property
    def viewer_url(self) -> Optional[str]:
        return self._viewer.url if self._viewer is not None else None

    def stop_viewer(self) -> None:
        if self._viewer is not None:
            self._viewer.stop()

    def _new_task_id(self) -> str:
        return f"task-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"

    def _resolve_task_id(self, run_name: Optional[str]) -> str:
        """Pick the run id (== ``runs/<id>/`` folder name).

        With no ``run_name`` we auto-generate ``task-<date>-<hex>``. A custom
        ``run_name`` is sanitized to a filesystem-safe charset. Uniqueness is
        the run folder itself (``meta/task.json`` / ``run.json`` live there):

        * **completed** ``run.json`` → refuse (delete ``runs/<id>/`` to reuse)
        * interrupted / failed / partial → **reuse** the same folder
        """
        if not run_name or not run_name.strip():
            for _ in range(8):
                tid = self._new_task_id()
                if not self._config.run_dir(tid).exists():
                    return tid
            return self._new_task_id()
        # Keep unicode letters/digits (so Chinese names work), collapse anything
        # path-unsafe (slashes, spaces, punctuation) to '-'.
        cleaned = re.sub(r"[^\w.\-]+", "-", run_name.strip(), flags=re.UNICODE).strip("-_.")[:120]
        if not cleaned or cleaned in (".", ".."):
            raise ValueError(f"run name {run_name!r} has no usable characters")
        run_json = self._config.run_dir(cleaned) / "run.json"
        if run_json.is_file():
            try:
                status = json.loads(run_json.read_text(encoding="utf-8")).get("status") or ""
            except (OSError, json.JSONDecodeError):
                status = ""
            if status == "task_completed":
                raise ValueError(
                    f"run name {cleaned!r} already finished under "
                    f"{self._config.run_dir(cleaned)}; delete that folder or pick another name"
                )
            print(f"[run] reusing incomplete run folder: {self._config.run_dir(cleaned)}")
        elif self._config.run_dir(cleaned).exists():
            print(f"[run] reusing existing run folder: {self._config.run_dir(cleaned)}")
        return cleaned

    async def run_task(
        self,
        task_prompt: str,
        *,
        max_turns: int = 60,
        run_name: Optional[str] = None,
        export: bool = True,
        preparer: Optional[ExternalTaskPreparer] = None,
        mcp_server_factory: Optional[Callable[[SandboxManager, str, str], dict[str, Any]]] = None,
        task_key: Optional[str] = None,
        teacher_enabled: Optional[bool] = None,
        hints: Optional[str] = None,
    ) -> TaskResult:
        task_id = self._resolve_task_id(run_name)
        resolved_task_key = (task_key or task_id).strip() or task_id
        teacher_on = self._config.teacher_enabled if teacher_enabled is None else teacher_enabled
        run_dir = self._config.ensure_run_dirs(task_id)

        if self._manage_capgw and not self._capgw.ensure(log_path=run_dir / "logs" / "capgw.log"):
            raise RuntimeError("capgw gateway did not come up; cannot reach the local model")

        if self._viewer is not None:
            try:
                url = self._viewer.start()
                print(f"[trace] 实时查看器: {url}")
            except Exception as exc:  # noqa: BLE001 - viewer must never block a run
                print(f"[trace] 查看器启动失败（忽略）: {exc}")
                self._viewer = None

        manager = SandboxManager(self._config)
        workspace = manager.workspace_for(task_id)
        trace = TraceWriter(task_id, run_dir / "trace" / "events.jsonl")

        record = TaskRecord(
            task_id=task_id,
            task_status="task_received",
            researcher_session_id=f"{task_id}/trace/researcher",
            labwright_session_id=f"{task_id}/trace/labwright",
            teacher_session_id=f"{task_id}/trace/teacher" if teacher_on else "",
            task_key=resolved_task_key,
            workspace=workspace,
        )
        self._db.create(record)
        trace.emit("orchestrator", "task_received", {"prompt": task_prompt, "backend": manager.backend})

        def on_labwright_event(ev: TurnEvent) -> None:
            trace.agent_turn("labwright", ev.kind, ev.data)

        labwright = LabwrightService(
            self._config, manager, task_id,
            emit=self._labwright_emit(trace),
            on_agent_event=on_labwright_event,
            skill_candidates=SkillCandidates(self._config),
        )
        mcp_servers = {
            "labwright": build_labwright_server(labwright),
            "sandbox": build_sandbox_server(manager),
            "researcher_skill_capture": build_skill_capture_server(
                SkillCandidates(self._config), role="researcher", task_id=task_id,
            ),
            "experience": build_experience_server(
                ExperienceStore(self._config, source_run=task_id),
            ),
        }

        teacher: Optional[TeacherService] = None
        if teacher_on:
            def on_teacher_event(ev: TurnEvent) -> None:
                trace.agent_turn("teacher", ev.kind, ev.data)

            teacher = TeacherService(
                self._config, task_id,
                task_key=resolved_task_key,
                task_prompt=task_prompt,
                emit=lambda a, e, d: trace.emit(a, e, d),
                on_agent_event=on_teacher_event,
            )
            if hints:
                teacher.seed_hint_bank(Path(hints))
            mcp_servers["teacher"] = build_teacher_server(teacher)

        if mcp_server_factory is not None:
            mcp_servers.update(mcp_server_factory(manager, task_id, workspace))
        researcher_prompt = task_prompt
        if preparer is not None:
            try:
                prepared = await labwright.prepare_external_task(preparer, workspace)
            except Exception:
                await labwright.close()
                raise
            researcher_prompt = (
                f"{task_prompt}\n\n"
                "## 已由 Labwright 准备的任务材料\n"
                f"{prepared.context}\n\n"
                "只使用以上已经准备好的题面与资源；不要自行下载资源或安装工具。"
            )

        interceptions = {"n": 0}

        def on_intercept(command: str) -> None:
            interceptions["n"] += 1
            trace.emit("orchestrator", "hook_intercept", {"command": command[:300]})

        def on_event(ev: TurnEvent) -> None:
            trace.agent_turn("researcher", ev.kind, ev.data)

        self._db.update(task_id, task_status="research_planning")
        trace.emit("orchestrator", "researcher_started", {})

        try:
            result = await run_researcher(
                self._config,
                task_id=task_id,
                workspace=workspace,
                task_prompt=researcher_prompt,
                mcp_servers=mcp_servers,
                on_event=on_event,
                on_intercept=on_intercept,
                max_turns=max_turns,
            )
        except Exception:
            await labwright.close()
            if teacher is not None:
                await teacher.close()
            raise

        try:
            rec = self._db.get(task_id)
            sandbox_ids = rec.sandbox_ids if rec else []
            conclusion = self._read_conclusion(manager, workspace, sandbox_ids)
            status = "task_failed" if result.is_error else "task_completed"
            self._db.update(task_id, task_status=status, result=result.final_text)
            trace.emit("orchestrator", status, {"num_turns": result.num_turns, "interceptions": interceptions["n"]})

            trace_index = correlate_traces(task_id, self._config, sandbox_ids)

            teacher_stats: dict[str, Any] = {}
            if teacher is not None:
                teacher_stats = teacher.stats()
                if teacher_stats.get("asks_used"):
                    trace.emit("orchestrator", "teacher_stats", teacher_stats)

            export_dir = ""
            if export:
                path = export_run(
                    self._config, manager,
                    task_id=task_id, workspace=workspace, sandbox_ids=sandbox_ids,
                    prompt=task_prompt, status=status, backend=manager.backend,
                    conclusion=conclusion,
                )
                if path is not None:
                    export_dir = str(path)
                    trace.emit("orchestrator", "run_exported", {"path": export_dir})
                    if teacher is not None:
                        teacher.write_artifacts(path)

            if preparer is not None:
                if not export_dir:
                    status = "task_failed"
                    result.final_text = "external task validation requires a run export"
                    self._db.update(task_id, task_status=status, result=result.final_text)
                else:
                    try:
                        deliverables = Path(export_dir) / "deliverables"
                        await labwright.validate_external_deliverables(
                            preparer,
                            deliverables if deliverables.is_dir() else Path(export_dir),
                        )
                    except Exception as exc:  # noqa: BLE001
                        status = "task_failed"
                        result.final_text = str(exc)
                        self._db.update(task_id, task_status=status, result=result.final_text)
                        trace.emit("orchestrator", "external_deliverable_validation_failed", {
                            "error": result.final_text[:500],
                        })

            return TaskResult(
                task_id=task_id,
                status=status,
                conclusion=conclusion,
                final_text=result.final_text,
                workspace=workspace,
                sandbox_ids=sandbox_ids,
                backend=manager.backend,
                trace_index=trace_index,
                interceptions=interceptions["n"],
                export_dir=export_dir,
                teacher_stats=teacher_stats,
            )
        finally:
            await labwright.close()
            if teacher is not None:
                await teacher.close()

    def _labwright_emit(self, trace: TraceWriter):
        def emit(agent: str, event: str, detail: dict) -> None:
            sandbox_id = detail.get("sandbox_id")
            if event in ("sandbox_created", "manifest_published") and sandbox_id:
                try:
                    self._db.add_sandbox(trace._task_id, sandbox_id)  # noqa: SLF001
                except KeyError:
                    pass
            trace.emit(agent, event, detail, sandbox_id=sandbox_id)
        return emit

    @staticmethod
    def _read_conclusion(manager: SandboxManager, workspace: str, sandbox_ids: list[str]) -> str:
        """Read the run's ``conclusion.md`` in a backend-agnostic way.

        Prefer the sandbox provider (``manager.get_file``) so the same path
        works whether the workspace is a host directory (local/docker) or a
        remote cloud sandbox (lbg). Fall back to the host workspace file — the
        Researcher writes its final ``conclusion.md`` there regardless of
        backend, which also covers runs that created no sandbox.
        """
        for sid in reversed(sandbox_ids or []):
            try:
                data = manager.get_file(sid, "conclusion.md")
            except Exception:  # noqa: BLE001 - missing file / dead sandbox -> try next
                continue
            if data:
                return data.decode("utf-8", errors="replace")
        p = Path(workspace) / "conclusion.md"
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
        return ""

    def close(self) -> None:
        self._db.close()
        if self._manage_capgw:
            self._capgw.stop()
