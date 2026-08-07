"""The 0号机 orchestrator.

Receives a research task, creates the task_id + workspace, assigns each agent
its capgw session key, registers Labwright as MCP tools for the Researcher,
routes/records everything, and returns the result + a trace index.
"""

from __future__ import annotations

import asyncio
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
from zero.grading import grade_harbor, materialize_host_outputs
from zero.grading.task_package import tests_dir as package_tests_dir
from zero.labwright.inventory import update_image_on_inventory
from zero.labwright.mcp_server import build_labwright_server
from zero.labwright.service import LabwrightService
from zero.protocol.grading import GradeResult, GradeStatus
from zero.protocol.manifest import EnvironmentManifest
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
    environment: dict[str, Any] = field(default_factory=dict)
    grading: dict[str, Any] = field(default_factory=dict)
    completion_review: dict[str, Any] = field(default_factory=dict)
    optimized_task: str = ""


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
        max_turns: int = 1000,
        run_name: Optional[str] = None,
        export: bool = True,
        preparer: Optional[ExternalTaskPreparer] = None,
        mcp_server_factory: Optional[Callable[[SandboxManager, str, str], dict[str, Any]]] = None,
        task_key: Optional[str] = None,
        teacher_enabled: Optional[bool] = None,
        hints: Optional[str] = None,
        task_package: Optional[Path] = None,
    ) -> TaskResult:
        task_id = self._resolve_task_id(run_name)
        resolved_task_key = (task_key or task_id).strip() or task_id
        teacher_on = self._config.teacher_enabled if teacher_enabled is None else teacher_enabled
        run_dir = self._config.ensure_run_dirs(task_id)
        resolved_package = task_package

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
                task_package=resolved_package,
            )
            if hints:
                teacher.seed_hint_bank(Path(hints))
            mcp_servers["teacher"] = build_teacher_server(teacher)
            # Prefer live package statement for the Researcher.
            task_prompt = teacher.instruction_text() or task_prompt
            resolved_package = teacher.live_package

        if mcp_server_factory is not None:
            mcp_servers.update(mcp_server_factory(manager, task_id, workspace))
        researcher_prompt = task_prompt
        prepared_context = ""
        if preparer is not None:
            try:
                prepared = await labwright.prepare_external_task(preparer, workspace)
            except Exception:
                await labwright.close()
                raise
            prepared_context = prepared.context
            researcher_prompt = (
                f"{task_prompt}\n\n"
                "## 已由 Labwright 准备的任务材料\n"
                f"{prepared_context}\n\n"
                "只使用以上已经准备好的题面与资源；不要自行下载资源或安装工具。"
            )

        # Teacher preflight: inspect/amend live package before Researcher starts.
        if teacher is not None and getattr(self._config, "teacher_preflight", True):
            try:
                self._db.update(task_id, task_status="teacher_preflight")
                trace.emit("orchestrator", "teacher_preflight_started", {})
                pre = await teacher.preflight()
                trace.emit("orchestrator", "teacher_preflight_finished", {
                    "ok": pre.get("ok"),
                    "revision": pre.get("revision"),
                    "error": (pre.get("error") or "")[:300],
                })
                task_prompt = teacher.instruction_text() or task_prompt
                if prepared_context:
                    researcher_prompt = (
                        f"{task_prompt}\n\n"
                        "## 已由 Labwright 准备的任务材料\n"
                        f"{prepared_context}\n\n"
                        "只使用以上已经准备好的题面与资源；不要自行下载资源或安装工具。"
                    )
                else:
                    researcher_prompt = task_prompt
                resolved_package = teacher.live_package
            except Exception as exc:  # noqa: BLE001
                trace.emit("orchestrator", "teacher_preflight_failed", {
                    "error": str(exc)[:500],
                })

        interceptions = {"n": 0}

        def on_intercept(command: str) -> None:
            interceptions["n"] += 1
            trace.emit("orchestrator", "hook_intercept", {"command": command[:300]})

        def on_event(ev: TurnEvent) -> None:
            trace.agent_turn("researcher", ev.kind, ev.data)

        baseline = labwright.seal_environment_baseline()
        if baseline is not None:
            trace.emit("orchestrator", "environment_baseline_sealed", {
                "sandbox_id": baseline.sandbox_id,
                "image_digest": baseline.image_digest,
            })
        self._db.update(task_id, task_status="research_planning")
        trace.emit("orchestrator", "researcher_started", {})

        exported = False
        fail_status = "interrupted"
        fail_error = ""

        try:
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
            except BaseException as exc:
                fail_status = (
                    "interrupted"
                    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
                    else "task_failed"
                )
                fail_error = f"{type(exc).__name__}: {exc}"[:1000]
                raise

            try:
                rec = self._db.get(task_id)
                sandbox_ids = rec.sandbox_ids if rec else []
                conclusion = self._read_conclusion(manager, workspace, sandbox_ids)
                status = "task_failed" if result.is_error else "task_completed"
                if result.is_error and result.final_text:
                    fail_error = result.final_text[:1000]
                self._db.update(task_id, task_status=status, result=result.final_text)
                trace.emit("orchestrator", status, {"num_turns": result.num_turns, "interceptions": interceptions["n"]})

                trace_index = correlate_traces(task_id, self._config, sandbox_ids)

                teacher_stats: dict[str, Any] = {}
                if teacher is not None:
                    teacher_stats = teacher.stats()
                    if teacher_stats.get("asks_used"):
                        trace.emit("orchestrator", "teacher_stats", teacher_stats)

                resolved_task = (
                    teacher.resolved_task_markdown()
                    if teacher is not None
                    else self._resolved_task_without_teacher(task_prompt, resolved_task_key)
                )

                # --- Grade (deterministic) then Teacher completion review -------
                grading: dict[str, Any] = {}
                completion_review: dict[str, Any] = {}
                optimized_task = ""
                grade_result = await self._run_grading_phase(
                    run_dir=run_dir,
                    manager=manager,
                    sandbox_ids=sandbox_ids,
                    workspace=workspace,
                    task_package=resolved_package,
                    trace=trace,
                )
                grading = grade_result.model_dump(mode="json")

                if teacher is not None and grade_result.status != GradeStatus.SKIPPED:
                    try:
                        review = await teacher.review_completion(
                            grade=grade_result,
                            resolved_task=resolved_task,
                            task_package=resolved_package,
                        )
                        completion_review = review.model_dump(mode="json")
                        opt = run_dir / "finalized_task"
                        if not opt.is_dir():
                            opt = run_dir / "optimized_task"
                        if opt.is_dir():
                            optimized_task = str(opt)
                        # Refresh resolved task if review added scientific amendments.
                        resolved_task = teacher.resolved_task_markdown()
                        resolved_package = teacher.live_package
                        teacher_stats = teacher.stats()
                        teacher_stats["completion_review"] = {
                            "kind": review.kind.value,
                            "summary": review.summary[:300],
                        }
                    except Exception as exc:  # noqa: BLE001
                        trace.emit("orchestrator", "completion_review_failed", {
                            "error": str(exc)[:500],
                        })
                        completion_review = {"error": str(exc)[:500]}

                environment = await self._finalize_environment_artifact(
                    labwright, manager, sandbox_ids, run_dir=run_dir,
                )

                export_dir = ""
                path = export_run(
                    self._config, manager,
                    task_id=task_id, workspace=workspace, sandbox_ids=sandbox_ids,
                    prompt=task_prompt, status=status, backend=manager.backend,
                    conclusion=conclusion,
                    resolved_task=resolved_task,
                    environment=environment,
                    pull_deliverables=export,
                    grading=grading,
                    optimized_task=optimized_task or None,
                    error=fail_error or None,
                )
                if path is not None:
                    exported = True
                    if export:
                        export_dir = str(path)
                        trace.emit("orchestrator", "run_exported", {"path": export_dir})
                    else:
                        trace.emit("orchestrator", "completion_artifacts_written", {"path": str(path)})
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
                    environment=environment,
                    grading=grading,
                    completion_review=completion_review,
                    optimized_task=optimized_task,
                )
            except BaseException as exc:
                if not fail_error:
                    fail_status = (
                        "interrupted"
                        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
                        else "task_failed"
                    )
                    fail_error = f"{type(exc).__name__}: {exc}"[:1000]
                raise
        finally:
            if not exported:
                try:
                    self._emergency_finalize(
                        task_id=task_id,
                        task_prompt=task_prompt,
                        workspace=workspace,
                        manager=manager,
                        run_dir=run_dir,
                        trace=trace,
                        status=fail_status,
                        error=fail_error,
                    )
                except Exception as exc:  # noqa: BLE001
                    try:
                        trace.emit("orchestrator", "emergency_finalize_failed", {
                            "error": str(exc)[:500],
                        })
                    except Exception:  # noqa: BLE001
                        pass
            await labwright.close()
            if teacher is not None:
                await teacher.close()

    def _emergency_finalize(
        self,
        *,
        task_id: str,
        task_prompt: str,
        workspace: str,
        manager: SandboxManager,
        run_dir: Path,
        trace: TraceWriter,
        status: str,
        error: str,
    ) -> None:
        """Best-effort ``run.json`` when the normal export path did not run."""
        try:
            rec = self._db.get(task_id)
            sandbox_ids = list(rec.sandbox_ids) if rec else []
        except Exception:  # noqa: BLE001
            sandbox_ids = []
        try:
            self._db.update(task_id, task_status=status, result=(error or "")[:2000])
        except Exception:  # noqa: BLE001
            pass
        try:
            env_path = run_dir / "environment.json"
            environment = None
            if env_path.is_file():
                environment = json.loads(env_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            environment = None
        path = export_run(
            self._config, manager,
            task_id=task_id,
            workspace=workspace,
            sandbox_ids=sandbox_ids,
            prompt=task_prompt,
            status=status,
            backend=manager.backend,
            environment=environment,
            pull_deliverables=False,
            error=error or None,
        )
        detail = {"status": status, "error": (error or "")[:500]}
        if path is not None:
            detail["path"] = str(path)
        trace.emit("orchestrator", "emergency_finalize", detail)

    @staticmethod
    def _resolved_task_without_teacher(task_prompt: str, task_key: str) -> str:
        return (
            f"# Resolved task statement: {task_key}\n"
            "\n> This run had no Teacher session, so no scientific amendments "
            "could be issued.\n"
            "\n## Original task statement\n\n"
            f"{task_prompt.strip()}\n"
            "\n## Authoritative scientific amendments\n\nNone were issued.\n"
        )

    async def _run_grading_phase(
        self,
        *,
        run_dir: Path,
        manager: SandboxManager,
        sandbox_ids: list[str],
        workspace: str,
        task_package: Optional[Path],
        trace: TraceWriter,
    ) -> GradeResult:
        tests = package_tests_dir(task_package)
        if tests is None:
            result = GradeResult(
                status=GradeStatus.SKIPPED if task_package is None else GradeStatus.MISSING_PACKAGE,
                error=(
                    "no task package / tests provided"
                    if task_package is None
                    else f"no Harbor tests under {task_package}"
                ),
                tests_dir=str(task_package / "tests") if task_package else None,
            )
            grading_dir = run_dir / "grading"
            grading_dir.mkdir(parents=True, exist_ok=True)
            (grading_dir / "result.json").write_text(
                result.model_dump_json(indent=2) + "\n", encoding="utf-8",
            )
            trace.emit("orchestrator", "grading_skipped", {"reason": result.error})
            return result

        host_outputs = await asyncio.to_thread(
            materialize_host_outputs,
            run_dir=run_dir,
            manager=manager,
            sandbox_ids=sandbox_ids,
            workspace=workspace,
        )
        sid = sandbox_ids[-1] if sandbox_ids else None
        trace.emit("orchestrator", "grading_started", {
            "tests_dir": str(tests),
            "sandbox_id": sid,
            "host_outputs": str(host_outputs) if host_outputs else None,
        })
        result = await asyncio.to_thread(
            grade_harbor,
            run_dir=run_dir,
            tests=tests,
            manager=manager,
            sandbox_id=sid,
            host_outputs=host_outputs,
        )
        trace.emit("orchestrator", "grading_finished", {
            "status": result.status.value,
            "score": result.score,
            "mode": result.mode,
        })
        return result

    async def _finalize_environment_artifact(
        self,
        labwright: LabwrightService,
        manager: SandboxManager,
        sandbox_ids: list[str],
        *,
        run_dir: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Resolve the latest READY environment into a completion artifact.

        The snapshot is created by Labwright at ``publish_manifest`` time,
        before it hands control back to the Researcher. Inventory +
        ``environment.md`` are written then; here we only resolve image URL.
        """
        manifest = labwright.get_environment_baseline()
        snapshot_scope = "environment_baseline"
        if manifest is None:
            snapshot_scope = "latest_published_manifest_fallback"
            for sandbox_id in reversed(sandbox_ids):
                candidate = labwright.get_environment_manifest(sandbox_id)
                if candidate is not None:
                    manifest = candidate
                    break

        rd = run_dir or (self._config.run_dir(sandbox_ids[0]) if sandbox_ids else None)
        # Prefer explicit run_dir from caller.
        if run_dir is None and manifest is not None:
            rd = self._config.run_dir(manifest.task_id)

        if manifest is None:
            return {
                "schema_version": 2,
                "backend": manager.backend,
                "snapshot_scope": snapshot_scope,
                "status": "unavailable",
                "reason": "No published environment manifest was available.",
                "image": {"status": "unavailable", "url": None},
            }

        digest = manifest.image_digest or ""
        if digest.startswith("lbg:commit:") and self._config.lbg_image_wait_timeout <= 0:
            image: dict[str, Any] = {
                "kind": "lbg_commit",
                "commit_id": digest.rsplit(":", 1)[-1],
                "status": "submitted",
                "url": None,
                "note": "Set ZERO_LBG_IMAGE_WAIT_TIMEOUT > 0 to wait for imageUrl.",
            }
        elif digest:
            image = await asyncio.to_thread(
                manager.resolve_snapshot,
                manifest.sandbox_id,
                digest,
                timeout=self._config.lbg_image_wait_timeout,
            )
        else:
            image = {
                "status": "unavailable",
                "url": None,
                "reason": "The environment manifest has no snapshot digest.",
            }

        if rd is not None:
            try:
                update_image_on_inventory(rd, image, manifest=manifest)
            except Exception:  # noqa: BLE001
                pass

        env_md = None
        inv_path = None
        if rd is not None:
            md = rd / "environment" / "environment.md"
            inv = rd / "environment" / "inventory.json"
            if md.is_file():
                env_md = str(md)
            if inv.is_file():
                inv_path = str(inv)

        return {
            "schema_version": 2,
            "backend": manager.backend,
            "snapshot_scope": snapshot_scope,
            "snapshot_timing": "Labwright publish_manifest before handoff to Researcher",
            "inventory_path": inv_path,
            "environment_md": env_md,
            "manifest": manifest.model_dump(mode="json"),
            "image": image,
        }

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
