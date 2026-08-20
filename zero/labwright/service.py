"""LabwrightService: agent-driven environment provisioning.

Keeps the Researcher-facing MCP contract (ensure_environment / add_resources /
resolve_decision / report_issue) but drives work through a persistent Labwright
Claude Code agent that tries and retries via labenv tools — no state machine.

Execution model: **blocking handoff**. Each Researcher-facing call awaits one
Labwright turn to completion and returns its terminal result directly
(ENVIRONMENT_READY / NEEDS_DECISION / *_FAILED). There is no polling and no
background task — only one agent is ever mid-turn at a time. When Labwright
needs input it ends its turn with NEEDS_DECISION; the call returns that to the
Researcher, who answers via resolve_environment_decision (which blocks again).
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Callable, Optional

from zero.claude_runtime import TurnEvent
from zero.config import Config
from zero.labwright.agent import LabwrightAgent
from zero.labwright.resolver import Resolver
from zero.labwright.tools import LabwrightContext
from zero.labwright.verifier import Verifier
from zero.preparation import ExternalTaskPreparer, PreparedTask
from zero.protocol.hashing import spec_hash
from zero.protocol.manifest import EnvironmentManifest
from zero.protocol.spec import (
    DatasetRequest,
    EnvironmentSpec,
    ModelRequest,
    PackageRequest,
    ResourceAddition,
    ToolRequest,
)
from zero.protocol.status import (
    EnvironmentResponse,
    EnvironmentStatus,
    ResearcherDecision,
)
from zero.resources.cache import ResourceCache
from zero.resources.deploy_master import DeployMasterClient
from zero.resources.literature_sage import LiteratureSageClient
from zero.resources.locks import ResourceLockStore
from zero.resources.registry import ResourceRegistry
from zero.resources.trisol import TrisolClient
from zero.sandbox.manager import SandboxManager
from zero.skills.candidates import SkillCandidates

EmitFn = Callable[[str, str, dict], None]


class LabwrightService:
    def __init__(
        self,
        config: Config,
        manager: SandboxManager,
        task_id: str,
        emit: Optional[EmitFn] = None,
        on_agent_event: Optional[Callable[[TurnEvent], None]] = None,
        skill_candidates: Optional[SkillCandidates] = None,
    ):
        self._config = config
        self._mgr = manager
        self._task_id = task_id
        self._emit = emit or (lambda a, e, d: None)
        self._on_agent_event = on_agent_event

        self._registry_client = None
        self._deploy_master_client = None
        registry = None
        if config.resource_registry_enabled:
            self._registry_client = LiteratureSageClient(
                config.literature_sage_base_url,
                timeout=config.literature_sage_timeout_sec,
                max_connections=config.literature_sage_max_connections,
                max_retries=config.literature_sage_max_retries,
                auth_token=config.literature_sage_auth_token,
                proxy_url=config.literature_sage_proxy_url,
            )
            registry = ResourceRegistry(self._registry_client)
        if config.deploy_master_base_url:
            self._deploy_master_client = DeployMasterClient(
                config.deploy_master_base_url,
                poll_interval=config.deploy_master_poll_interval_sec,
                deadline=config.deploy_master_build_deadline_sec,
                auth_token=config.deploy_master_auth_token,
            )
        trisol = TrisolClient(
            binary=config.trisol_bin, team=config.trisol_team,
            api_url=config.trisol_api_url, token=config.trisol_token,
        )
        self._ctx = LabwrightContext(
            config=config,
            manager=manager,
            resolver=Resolver(ResourceCache(config.run_resources_dir(task_id)), trisol=trisol),
            verifier=Verifier(manager),
            task_id=task_id,
            emit=self._emit,
            skill_candidates=skill_candidates,
            registry=registry,
            trisol=trisol,
            deploy_master=self._deploy_master_client,
            lock_store=ResourceLockStore(
                config.run_dir(task_id) / "resources.lock.json", task_id,
            ),
        )
        self._agent = LabwrightAgent(
            config, task_id=task_id, ctx=self._ctx,
            on_event=on_agent_event, cwd=manager.workspace_for(task_id),
        )

        # spec_hash -> a completed READY response, for idempotent reuse across
        # retries / resume (never re-provision the same environment twice).
        self._ready: dict[str, EnvironmentResponse] = {}
        # Sealed immediately before the Researcher starts. Later environment
        # repair events must not replace the reusable, code/output-free image.
        self._environment_baseline: Optional[EnvironmentManifest] = None
        # request_id -> the spec that request is provisioning (for continuation).
        self._specs: dict[str, EnvironmentSpec] = {}
        self._counter = itertools.count(1)
        # Serializes turns: the Labwright session is single-threaded, and only
        # one Researcher-facing call should drive it at a time.
        self._lock = asyncio.Lock()

    # ---- MCP-facing interface (all blocking) ---------------------------- #
    async def ensure_environment(self, spec: EnvironmentSpec) -> EnvironmentResponse:
        h = spec_hash(spec)
        cached = self._ready.get(h)
        if cached is not None:
            return cached
        request_id = f"req-{h[:8]}-{next(self._counter)}"
        self._specs[request_id] = spec
        self._emit("labwright", "ensure_environment", {
            "request_id": request_id, "experiment_id": spec.experiment_id,
        })
        resp = await self._run_turn(request_id, self._prompt_ensure(spec, request_id), spec)
        if resp.status == EnvironmentStatus.ENVIRONMENT_READY:
            self._ready[h] = resp
            self._seal_from_response(resp)
        return resp

    async def resolve_environment_decision(
        self, request_id: str, decision: ResearcherDecision,
    ) -> EnvironmentResponse:
        spec = self._specs.get(request_id)
        if spec is None:
            return EnvironmentResponse(
                status=EnvironmentStatus.ENVIRONMENT_FAILED,
                request_id=request_id, message="unknown request_id",
            )
        self._emit("labwright", "decision_resolved", {
            "request_id": request_id,
            "decision": decision.model_dump(exclude_none=True),
        })
        if decision.abort:
            return EnvironmentResponse(
                status=EnvironmentStatus.ENVIRONMENT_FAILED,
                request_id=request_id, message="Researcher 终止了该环境请求",
            )

        source = decision.use_source
        pend = self._ctx.pending_decision
        if source is None and decision.choose and pend is not None:
            for c in pend.candidates:
                if c.id == decision.choose:
                    source = c.source
                    break
        resource_name = pend.resource_name if pend is not None else None
        if source and resource_name:
            self._ctx.source_overrides[resource_name] = source
        self._ctx.pending_decision = None

        resp = await self._run_turn(
            request_id,
            self._prompt_decision(spec, request_id, decision, source, resource_name),
            spec,
            existing_sandbox_id=self._ctx.sandbox_id,
        )
        if resp.status == EnvironmentStatus.ENVIRONMENT_READY:
            self._ready[spec_hash(spec)] = resp
            self._seal_from_response(resp)
        return resp

    async def add_resources(
        self, sandbox_id: str, additions: list[ResourceAddition],
    ) -> EnvironmentResponse:
        base_spec = self._ctx.sandbox_spec.get(sandbox_id)
        if base_spec is None:
            return EnvironmentResponse(
                status=EnvironmentStatus.ENVIRONMENT_FAILED,
                request_id="add-unknown",
                message=f"unknown sandbox {sandbox_id}",
            )
        merged = self._merge(base_spec, additions)
        request_id = f"add-{sandbox_id}-{next(self._counter)}"
        self._specs[request_id] = merged
        self._emit("labwright", "add_resources", {
            "request_id": request_id, "sandbox_id": sandbox_id,
            "resources": [a.model_dump(exclude_none=True) for a in additions],
        })
        # Dual-sandbox: never install onto the exp sandbox for a freeze.
        existing = self._ctx.env_sandbox_id or sandbox_id
        if self._mgr.role_of(sandbox_id) == "exp":
            existing = None  # force a fresh env sandbox
        resp = await self._run_turn(
            request_id,
            self._prompt_add(merged, request_id, sandbox_id, additions),
            merged,
            existing_sandbox_id=existing,
        )
        if resp.status in (EnvironmentStatus.ENVIRONMENT_READY, EnvironmentStatus.RESOURCE_ADDED):
            if resp.manifest is not None:
                self._ready[spec_hash(merged)] = EnvironmentResponse(
                    status=EnvironmentStatus.ENVIRONMENT_READY,
                    request_id=resp.request_id,
                    sandbox_id=resp.sandbox_id,
                    manifest=resp.manifest,
                    message=resp.message,
                    detail=resp.detail,
                )
                self._seal_from_response(resp)
        return resp

    def get_environment_manifest(self, sandbox_id: str) -> Optional[EnvironmentManifest]:
        return self._ctx.sandbox_manifest.get(sandbox_id)

    def _seal_from_response(self, resp: EnvironmentResponse) -> None:
        """Seal the clean env-side manifest on first READY."""
        if self._environment_baseline is not None:
            return
        env_sid = None
        if isinstance(resp.detail, dict):
            env_sid = resp.detail.get("env_sandbox_id")
        manifest = None
        if env_sid:
            manifest = self._ctx.sandbox_manifest.get(str(env_sid))
        if manifest is None and self._ctx.env_sandbox_id:
            manifest = self._ctx.sandbox_manifest.get(self._ctx.env_sandbox_id)
        if manifest is None:
            manifest = resp.manifest
        if manifest is not None:
            self._environment_baseline = manifest.model_copy(deep=True)

    def seal_environment_baseline(self) -> Optional[EnvironmentManifest]:
        """Freeze the newest READY env manifest before Researcher work begins."""
        if self._environment_baseline is not None:
            return self._environment_baseline
        env_sid = self._ctx.env_sandbox_id
        if env_sid:
            manifest = self._ctx.sandbox_manifest.get(env_sid)
            if manifest is not None:
                self._environment_baseline = manifest.model_copy(deep=True)
                return self._environment_baseline
        sandbox_id = self._ctx.sandbox_id
        manifest = self._ctx.sandbox_manifest.get(sandbox_id or "")
        if manifest is None:
            return None
        self._environment_baseline = manifest.model_copy(deep=True)
        return self._environment_baseline

    def get_environment_baseline(self) -> Optional[EnvironmentManifest]:
        return self._environment_baseline

    async def report_environment_issue(self, sandbox_id: str, issue: str) -> EnvironmentResponse:
        self._emit("labwright", "issue_reported", {
            "sandbox_id": sandbox_id, "issue": issue[:500],
        })
        base = self._ctx.sandbox_spec.get(sandbox_id)
        if base is None:
            return EnvironmentResponse(
                status=EnvironmentStatus.ENVIRONMENT_FAILED,
                request_id=f"issue-{sandbox_id}",
                sandbox_id=sandbox_id,
                message="未知 sandbox；如需修复请通过 add_resources 声明所需资源",
            )
        request_id = f"issue-{sandbox_id}-{next(self._counter)}"
        self._specs[request_id] = base
        return await self._run_turn(
            request_id,
            self._prompt_issue(base, request_id, sandbox_id, issue),
            base,
            existing_sandbox_id=sandbox_id,
        )

    async def close(self) -> None:
        await self._agent.close()
        if self._registry_client is not None:
            await self._registry_client.aclose()
        if self._deploy_master_client is not None:
            await self._deploy_master_client.aclose()

    async def prepare_external_task(
        self, preparer: ExternalTaskPreparer, workspace: str,
    ) -> PreparedTask:
        """Run an application-provided host preflight under Labwright ownership.

        The adapter stages task instructions/resources on the host because it
        may need credentials which must never be injected into a sandbox.
        Labwright records this as an environment/preparation event, then the
        Researcher receives only the returned context and staged paths.
        """
        self._emit("labwright", "external_task_preparation_started", {
            "workspace": workspace,
        })
        try:
            prepared = await preparer.prepare(Path(workspace))
        except Exception as exc:  # noqa: BLE001
            self._emit("labwright", "external_task_preparation_failed", {
                "workspace": workspace, "error": str(exc)[:500],
            })
            raise RuntimeError(f"Labwright external preparation failed: {exc}") from exc
        self._emit("labwright", "external_task_prepared", {
            "workspace": workspace,
            "manifest_path": prepared.manifest_path,
            "resource_hints": prepared.resource_hints,
        })
        return prepared

    async def validate_external_deliverables(
        self, preparer: ExternalTaskPreparer, run_dir: Path,
    ) -> None:
        """Perform the adapter's postflight checks under Labwright ownership."""
        self._emit("labwright", "external_deliverable_validation_started", {
            "run_dir": str(run_dir),
        })
        try:
            await preparer.validate_deliverables(run_dir)
        except Exception as exc:  # noqa: BLE001
            self._emit("labwright", "external_deliverable_validation_failed", {
                "run_dir": str(run_dir), "error": str(exc)[:500],
            })
            raise RuntimeError(f"Labwright deliverable validation failed: {exc}") from exc
        self._emit("labwright", "external_deliverables_validated", {
            "run_dir": str(run_dir),
        })

    # ---- internals ------------------------------------------------------ #
    async def _run_turn(
        self,
        request_id: str,
        prompt: str,
        spec: EnvironmentSpec,
        *,
        existing_sandbox_id: Optional[str] = None,
    ) -> EnvironmentResponse:
        async with self._lock:
            self._ctx.set_request(request_id, spec, sandbox_id=existing_sandbox_id)
            self._emit("labwright", "agent_turn_start", {"request_id": request_id})
            try:
                result = await self._agent.run_turn(prompt)
            except Exception as exc:  # noqa: BLE001
                resp = EnvironmentResponse(
                    status=EnvironmentStatus.ENVIRONMENT_FAILED,
                    request_id=request_id,
                    sandbox_id=self._ctx.sandbox_id,
                    message=f"Labwright agent error: {exc}",
                )
                self._emit("labwright", "agent_error", {
                    "request_id": request_id, "error": str(exc)[:500],
                })
                return resp

            if self._ctx.response is not None:
                resp = self._ctx.response
            else:
                # Agent finished without a terminal tool — treat as failure.
                resp = EnvironmentResponse(
                    status=EnvironmentStatus.ENVIRONMENT_FAILED,
                    request_id=request_id,
                    sandbox_id=self._ctx.sandbox_id,
                    message=(
                        "Labwright agent 结束但未 publish_manifest / "
                        f"request_decision / mark_failed。最后输出: {result.final_text[:400]}"
                    ),
                )
            self._emit("labwright", "agent_turn_end", {
                "request_id": request_id,
                "status": resp.status.value,
                "num_turns": result.num_turns,
                "is_error": result.is_error,
            })
            return resp

    def _prompt_ensure(self, spec: EnvironmentSpec, request_id: str) -> str:
        return (
            f"New environment request request_id={request_id}.\n"
            f"Provision and verify a usable Sandbox from the EnvironmentSpec below. "
            f"When done, call publish_manifest; on semantic ambiguity call "
            f"request_researcher_decision; if undeliverable call mark_failed.\n\n"
            f"```json\n{json.dumps(spec.model_dump(exclude_none=True), ensure_ascii=False, indent=2)}\n```"
        )

    def _prompt_decision(
        self,
        spec: EnvironmentSpec,
        request_id: str,
        decision: ResearcherDecision,
        resolved_source: Optional[str],
        resource_name: Optional[str],
    ) -> str:
        guidance = f"Researcher guidance: {decision.guidance}\n" if decision.guidance else ""
        return (
            f"The Researcher answered your question; continue provisioning "
            f"request_id={request_id}.\n"
            f"Resource: {resource_name or '?'}\n"
            f"Decision: {json.dumps(decision.model_dump(exclude_none=True), ensure_ascii=False)}\n"
            f"{guidance}"
            f"source_override set to: {resolved_source!r} (collect_resource will use it).\n"
            f"Current sandbox_id: {self._ctx.sandbox_id}\n"
            f"Original EnvironmentSpec:\n"
            f"```json\n{json.dumps(spec.model_dump(exclude_none=True), ensure_ascii=False, indent=2)}\n```\n"
            f"Continue collect / install / verify, then publish_manifest."
        )

    def _prompt_add(
        self,
        spec: EnvironmentSpec,
        request_id: str,
        sandbox_id: str,
        additions: list[ResourceAddition],
    ) -> str:
        return (
            f"Incremental resource request request_id={request_id}.\n"
            f"Researcher currently uses sandbox_id={sandbox_id} "
            f"(likely an **exp** sandbox).\n"
            f"Dual-sandbox: create a **new env** sandbox "
            f"(create_sandbox; prefer base_image from the last frozen image), "
            f"install the additions there, verify, then publish_manifest with "
            f"as_resource_added=true so a **new clean exp** is spawned. "
            f"Return that new exp sandbox_id to the Researcher.\n"
            f"Additions:\n"
            f"```json\n{json.dumps([a.model_dump(exclude_none=True) for a in additions], ensure_ascii=False, indent=2)}\n```\n"
            f"Merged full Spec (for reference):\n"
            f"```json\n{json.dumps(spec.model_dump(exclude_none=True), ensure_ascii=False, indent=2)}\n```\n"
        )

    def _prompt_issue(
        self, spec: EnvironmentSpec, request_id: str, sandbox_id: str, issue: str,
    ) -> str:
        return (
            f"Researcher reported an environment issue "
            f"request_id={request_id}, sandbox_id={sandbox_id}:\n"
            f"{issue}\n\n"
            f"Diagnose and fix (sandbox_exec / pip install / etc.). "
            f"Re-verify and publish_manifest when fixed; otherwise mark_failed.\n"
            f"Current Spec:\n"
            f"```json\n{json.dumps(spec.model_dump(exclude_none=True), ensure_ascii=False, indent=2)}\n```"
        )

    @staticmethod
    def _merge(base: EnvironmentSpec, additions: list[ResourceAddition]) -> EnvironmentSpec:
        spec = base.model_copy(deep=True)
        for a in additions:
            if a.type == "python_package":
                if not any(p.name == a.name for p in spec.packages):
                    spec.packages.append(PackageRequest(name=a.name, constraint=a.constraint))
            elif a.type == "tool":
                if not any(t.name == a.name for t in spec.tools):
                    spec.tools.append(ToolRequest(name=a.name, version=a.version))
            elif a.type == "model":
                if not any(m.name == a.name for m in spec.models):
                    spec.models.append(ModelRequest(
                        name=a.name, revision=a.revision,
                        precision=a.precision, source=a.source,
                    ))
            elif a.type == "dataset":
                if not any(d.name == a.name for d in spec.datasets):
                    spec.datasets.append(DatasetRequest(
                        name=a.name, version=a.version, source=a.source,
                    ))
        return spec
