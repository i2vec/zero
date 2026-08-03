"""Labwright's own MCP tool surface (``labenv``).

These tools are what the Labwright Claude Code agent calls while it tries,
fails, and retries environment provisioning. They close over a
``LabwrightContext`` that tracks the current request's sandbox, collected
resources, and terminal outcome (READY / NEEDS_DECISION / FAILED).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from claude_agent_sdk import create_sdk_mcp_server, tool

from zero.config import Config
from zero.labwright.resolver import Resolver
from zero.labwright.verifier import Verifier, normalize_dist
from zero.protocol.manifest import (
    DatasetEntry,
    EnvironmentManifest,
    ModelEntry,
    PackageEntry,
    ToolEntry,
    VerificationReport,
)
from zero.protocol.spec import (
    DatasetRequest,
    EnvironmentSpec,
    ModelRequest,
)
from zero.protocol.status import (
    DecisionRequest,
    EnvironmentResponse,
    EnvironmentStatus,
)
from zero.resources.cache import CachedResource, ResourceCache
from zero.sandbox.base import MountSpec, ResourceRef
from zero.sandbox.manager import SandboxManager
from zero.skills.candidates import SkillCandidates, SkillProposal

EmitFn = Callable[[str, str, dict], None]


def _dedupe(names) -> list[str]:
    """Drop empties and duplicates while preserving order."""
    seen: dict[str, None] = {}
    for n in names:
        n = (n or "").strip()
        if n and n not in seen:
            seen[n] = None
    return list(seen)


def _text(obj: Any) -> dict:
    if isinstance(obj, str):
        payload = obj
    else:
        payload = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    return {"content": [{"type": "text", "text": payload}]}


@dataclass
class LabwrightContext:
    """Mutable per-request (and longer-lived) state the labenv tools mutate."""

    config: Config
    manager: SandboxManager
    resolver: Resolver
    verifier: Verifier
    task_id: str
    emit: EmitFn

    request_id: str = ""
    spec: Optional[EnvironmentSpec] = None
    sandbox_id: Optional[str] = None
    collected: dict[str, CachedResource] = field(default_factory=dict)  # "model:name" -> res
    source_overrides: dict[str, str] = field(default_factory=dict)
    pending_decision: Optional[DecisionRequest] = None
    response: Optional[EnvironmentResponse] = None
    sandbox_manifest: dict[str, EnvironmentManifest] = field(default_factory=dict)
    sandbox_spec: dict[str, EnvironmentSpec] = field(default_factory=dict)
    skill_candidates: Optional[SkillCandidates] = None

    def set_request(self, request_id: str, spec: EnvironmentSpec, *,
                    sandbox_id: Optional[str] = None) -> None:
        self.request_id = request_id
        self.spec = spec
        self.sandbox_id = sandbox_id
        self.pending_decision = None
        self.response = None


def build_labenv_server(ctx: LabwrightContext):
    """In-process MCP server bound to a shared LabwrightContext."""

    @tool(
        "create_sandbox",
        "创建实验 Sandbox。可选 mounts：[{kind,name,version,host_path}]。"
        "返回 sandbox_id 与 workspace。",
        {
            "python_version": str,
            "cpu_count": int,
            "memory_gb": int,
            "gpu_count": int,
            "mounts": list,
        },
    )
    async def create_sandbox(args):
        mounts_in = args.get("mounts") or []
        mounts: list[MountSpec] = []
        for m in mounts_in:
            ref = ResourceRef(
                kind=m["kind"], name=m["name"], version=m.get("version") or "main",
                host_path=m["host_path"],
            )
            mounts.append(MountSpec(ref=ref, read_only=True))
        python = args.get("python_version") or (
            ctx.spec.base_environment.python if ctx.spec else "3.11"
        )
        handle = await asyncio.to_thread(
            lambda: ctx.manager.create(
                ctx.task_id,
                base_image=ctx.config.docker_base_image,
                mounts=mounts,
                cpu_count=int(args.get("cpu_count") or (ctx.spec.compute.cpu_count if ctx.spec else 2)),
                memory_gb=int(args.get("memory_gb") or (ctx.spec.compute.memory_gb if ctx.spec else 8)),
                gpu_count=int(args.get("gpu_count") or (ctx.spec.compute.gpu_count if ctx.spec else 0)),
                python_version=str(python),
            )
        )
        ctx.sandbox_id = handle.sandbox_id
        ctx.emit("labwright", "sandbox_created", {
            "request_id": ctx.request_id, "sandbox_id": handle.sandbox_id,
            "backend": handle.backend, "workspace": handle.workspace_path,
        })
        return _text({
            "ok": True,
            "sandbox_id": handle.sandbox_id,
            "workspace": handle.workspace_path,
            "backend": handle.backend,
            "resource_paths": handle.resource_paths,
        })

    @tool(
        "sandbox_exec",
        "在指定（或当前）Sandbox 中执行 shell 命令，返回 exit_code/stdout/stderr。"
        "用于 pip install、诊断、验证脚本等。",
        {"command": str, "sandbox_id": str, "timeout": int},
    )
    async def sandbox_exec(args):
        sid = args.get("sandbox_id") or ctx.sandbox_id
        if not sid:
            return _text({"ok": False, "error": "no sandbox_id; call create_sandbox first"})
        command = args["command"]
        timeout = int(args.get("timeout") or 900)
        result = await asyncio.to_thread(ctx.manager.exec, sid, command, timeout)
        ctx.emit("labwright", "sandbox_exec", {
            "request_id": ctx.request_id, "sandbox_id": sid,
            "command": command[:300], "exit_code": result.exit_code,
        })
        return _text({
            "ok": result.ok,
            "exit_code": result.exit_code,
            "stdout": result.stdout[-8000:],
            "stderr": result.stderr[-8000:],
        })

    @tool(
        "collect_resource",
        "搜集 model 或 dataset 到本地缓存。参数：kind(model|dataset), name, "
        "可选 revision/version/source。歧义时返回 needs_decision=true 与 candidates。",
        {
            "kind": str,
            "name": str,
            "revision": str,
            "version": str,
            "source": str,
            "precision": str,
        },
    )
    async def collect_resource(args):
        kind = args["kind"]
        name = args["name"]
        override = ctx.source_overrides.get(name) or args.get("source") or None
        if kind == "model":
            req = ModelRequest(
                name=name,
                revision=args.get("revision") or None,
                precision=args.get("precision") or None,
                source=override,
            )
            res = await asyncio.to_thread(ctx.resolver.resolve_model, req, override)
        elif kind == "dataset":
            req = DatasetRequest(
                name=name,
                version=args.get("version") or None,
                source=override,
            )
            res = await asyncio.to_thread(ctx.resolver.resolve_dataset, req, override)
        else:
            return _text({"ok": False, "error": f"unknown kind {kind}"})

        if res.decision is not None:
            ctx.emit("labwright", "collect_ambiguous", {
                "request_id": ctx.request_id, "kind": kind, "name": name,
            })
            return _text({
                "ok": False,
                "needs_decision": True,
                "decision": res.decision.model_dump(exclude_none=True),
                "hint": "调用 request_researcher_decision，然后结束本轮",
            })
        if res.unavailable is not None:
            return _text({"ok": False, "unavailable": res.unavailable})

        assert res.resource is not None
        key = f"{kind}:{name}"
        ctx.collected[key] = res.resource
        ctx.emit("labwright", "resource_collected", {
            "request_id": ctx.request_id, "kind": kind, "name": name,
            "source": res.resource.source, "host_path": res.resource.host_path,
        })
        return _text({
            "ok": True,
            "kind": kind,
            "name": name,
            "version": res.resource.version,
            "host_path": res.resource.host_path,
            "source": res.resource.source,
            "sha256": res.resource.sha256,
            "notes": res.notes,
        })

    @tool(
        "mount_resource",
        "把已搜集的资源挂进 Sandbox。参数：kind, name, version(可选), host_path, sandbox_id(可选)。",
        {
            "kind": str,
            "name": str,
            "version": str,
            "host_path": str,
            "sandbox_id": str,
        },
    )
    async def mount_resource(args):
        sid = args.get("sandbox_id") or ctx.sandbox_id
        if not sid:
            return _text({"ok": False, "error": "no sandbox_id"})
        version = args.get("version") or "main"
        host_path = args.get("host_path")
        if not host_path:
            cached = ctx.collected.get(f"{args['kind']}:{args['name']}")
            if cached is None:
                return _text({"ok": False, "error": "resource not collected; call collect_resource first"})
            host_path = cached.host_path
            version = cached.version
        ref = ResourceRef(kind=args["kind"], name=args["name"], version=version, host_path=host_path)
        path = await asyncio.to_thread(ctx.manager.mount, sid, MountSpec(ref=ref, read_only=True))
        ctx.emit("labwright", "resource_mounted", {
            "request_id": ctx.request_id, "sandbox_id": sid,
            "uri": ref.uri(), "path": path,
        })
        return _text({"ok": True, "sandbox_id": sid, "uri": ref.uri(), "path": path})

    @tool(
        "verify_resource",
        "验证 Sandbox 内资源是否可用。kind: package|tool|model|dataset。"
        "package/tool 用 name；model/dataset 用 path（或 name 从已挂载路径推断）。",
        {"kind": str, "name": str, "path": str, "sandbox_id": str},
    )
    async def verify_resource(args):
        sid = args.get("sandbox_id") or ctx.sandbox_id
        if not sid:
            return _text({"ok": False, "error": "no sandbox_id"})
        kind = args["kind"]
        name = args.get("name") or ""
        path = args.get("path")

        if kind == "package":
            chk = await asyncio.to_thread(ctx.verifier.verify_package, sid, name)
            return _text({"ok": chk.ok, "kind": kind, "name": name,
                          "version": chk.version, "error": chk.error})
        if kind == "tool":
            ok, out = await asyncio.to_thread(ctx.verifier.verify_tool, sid, name)
            return _text({"ok": ok, "kind": kind, "name": name, "output": out})
        if kind in ("model", "dataset"):
            if not path:
                handle = ctx.manager.get_handle(sid)
                if handle is None:
                    return _text({"ok": False, "error": "unknown sandbox"})
                # Prefer exact uri match from collected version.
                cached = ctx.collected.get(f"{kind}:{name}")
                version = cached.version if cached else "main"
                uri = f"{kind}://{name}/{version}"
                path = handle.resource_paths.get(uri)
                if not path:
                    top = "datasets" if kind == "dataset" else "models"
                    path = f"/{top}/{name}/{version}"
            if kind == "model":
                ok, out = await asyncio.to_thread(ctx.verifier.verify_model, sid, path)
            else:
                ok, out = await asyncio.to_thread(ctx.verifier.verify_dataset, sid, path)
            return _text({"ok": ok, "kind": kind, "name": name, "path": path, "output": out})
        return _text({"ok": False, "error": f"unknown kind {kind}"})

    @tool(
        "publish_manifest",
        "验证通过后发布 EnvironmentManifest，将本请求标记为 ENVIRONMENT_READY。"
        "可选传入 packages/tools 覆盖列表；默认从当前 spec + collected 构建。",
        {
            "sandbox_id": str,
            "packages": list,
            "tools": list,
            "message": str,
            "as_resource_added": bool,
        },
    )
    async def publish_manifest(args):
        sid = args.get("sandbox_id") or ctx.sandbox_id
        if not sid or ctx.spec is None:
            return _text({"ok": False, "error": "need sandbox_id and an active spec"})
        handle = ctx.manager.get_handle(sid)
        if handle is None:
            return _text({"ok": False, "error": f"unknown sandbox {sid}"})

        packages: dict[str, PackageEntry] = {}
        pkg_items = args.get("packages") or [p.name for p in ctx.spec.packages]
        pkg_names = _dedupe(normalize_dist(item) for item in pkg_items)
        pkg_ok = True
        for name in pkg_names:
            chk = await asyncio.to_thread(ctx.verifier.verify_package, sid, name)
            packages[name] = PackageEntry(version=chk.version or "unknown", verified=chk.ok)
            pkg_ok = pkg_ok and chk.ok

        tools: dict[str, ToolEntry] = {}
        tool_items = args.get("tools") or [t.name for t in ctx.spec.tools]
        tool_names = _dedupe(
            item.get("name") if isinstance(item, dict) else str(item) for item in tool_items
        )
        for name in tool_names:
            ok, _ = await asyncio.to_thread(ctx.verifier.verify_tool, sid, name)
            tools[name] = ToolEntry(version=None, command=name, verified=ok)

        models: dict[str, ModelEntry] = {}
        model_ok = True
        for key, res in ctx.collected.items():
            if not key.startswith("model:"):
                continue
            name = key.split(":", 1)[1]
            uri = res.kind + "://" + res.name + "/" + res.version
            path = handle.resource_paths.get(uri, f"/models/{res.name}/{res.version}")
            ok, _ = await asyncio.to_thread(ctx.verifier.verify_model, sid, path)
            models[name] = ModelEntry(
                path=path, revision=res.version, read_only=True, verified=ok,
                source=res.source, sha256=res.sha256, collected_at=res.collected_at,
            )
            model_ok = model_ok and ok

        datasets: dict[str, DatasetEntry] = {}
        ds_ok = True
        for key, res in ctx.collected.items():
            if not key.startswith("dataset:"):
                continue
            name = key.split(":", 1)[1]
            uri = res.kind + "://" + res.name + "/" + res.version
            path = handle.resource_paths.get(uri, f"/datasets/{res.name}/{res.version}")
            ok, _ = await asyncio.to_thread(ctx.verifier.verify_dataset, sid, path)
            datasets[name] = DatasetEntry(
                path=path, version=res.version, read_only=True, verified=ok,
                source=res.source, sha256=res.sha256, collected_at=res.collected_at,
            )
            ds_ok = ds_ok and ok

        ver = VerificationReport(gpu_check="skipped")
        if pkg_names:
            ver.package_import = "passed" if pkg_ok else "failed"
        if tool_names:
            ver.tool_healthcheck = "passed" if all(t.verified for t in tools.values()) else "failed"
        if models:
            ver.model_load = "passed" if model_ok else "failed"
        if datasets:
            ver.dataset_read = "passed" if ds_ok else "failed"

        if ver.package_import == "failed" or ver.model_load == "failed" or ver.dataset_read == "failed":
            return _text({
                "ok": False,
                "error": "verification failed; fix resources before publishing",
                "verification": ver.model_dump(exclude_none=True),
            })

        digest = await asyncio.to_thread(ctx.manager.snapshot, sid)
        manifest = EnvironmentManifest(
            task_id=ctx.task_id,
            experiment_id=ctx.spec.experiment_id,
            sandbox_id=sid,
            environment_status="ready",
            workspace=handle.workspace_path,
            runtime={
                "python": ctx.spec.base_environment.python,
                "cuda": ctx.spec.base_environment.cuda,
            },
            packages=packages, tools=tools, models=models, datasets=datasets,
            verification=ver,
            image_digest=digest,
            package_lock={n: e.version for n, e in packages.items()},
        )
        as_added = bool(args.get("as_resource_added"))
        status = EnvironmentStatus.RESOURCE_ADDED if as_added else EnvironmentStatus.ENVIRONMENT_READY
        msg = args.get("message") or (
            "资源已加入当前 Sandbox（Manifest 已更新）" if as_added else "环境已就绪"
        )
        ctx.response = EnvironmentResponse(
            status=status, request_id=ctx.request_id, sandbox_id=sid,
            manifest=manifest, message=msg,
            detail={"ts": time.time()},
        )
        ctx.sandbox_manifest[sid] = manifest
        ctx.sandbox_spec[sid] = ctx.spec
        ctx.sandbox_id = sid
        ctx.emit("labwright", "manifest_published", {
            "request_id": ctx.request_id, "sandbox_id": sid, "status": status.value,
        })
        return _text({
            "ok": True,
            "status": status.value,
            "sandbox_id": sid,
            "summary": manifest.researcher_summary(),
        })

    @tool(
        "request_researcher_decision",
        "需要 Researcher 判断时请求决策。传入 decision 对象"
        "（resource_type/resource_name/reason，可选 candidates/scientific_impact）。"
        "若无可枚举候选，用 question 字段提开放式问题（自由文本）。"
        "调用后结束本轮，控制权交回 Researcher，等待其回复。",
        {"decision": dict},
    )
    async def request_researcher_decision(args):
        try:
            decision = DecisionRequest.model_validate(args["decision"])
        except Exception as exc:  # noqa: BLE001
            return _text({"ok": False, "error": f"invalid decision: {exc}"})
        ctx.pending_decision = decision
        ctx.response = EnvironmentResponse(
            status=EnvironmentStatus.NEEDS_DECISION,
            request_id=ctx.request_id,
            sandbox_id=ctx.sandbox_id,
            decision=decision,
            message=decision.reason,
            detail={"ts": time.time()},
        )
        ctx.emit("labwright", "needs_decision", {
            "request_id": ctx.request_id,
            "resource": decision.resource_name,
            "resource_type": decision.resource_type,
        })
        return _text({
            "ok": True,
            "status": "NEEDS_DECISION",
            "hint": "已通知 Researcher；请立即结束本轮，不要继续猜测来源",
        })

    @tool(
        "mark_failed",
        "确认无法交付环境时调用。status 可选 ENVIRONMENT_FAILED / ENVIRONMENT_BLOCKED / RESOURCE_UNAVAILABLE。",
        {"message": str, "status": str},
    )
    async def mark_failed(args):
        raw = (args.get("status") or "ENVIRONMENT_FAILED").upper()
        try:
            status = EnvironmentStatus(raw)
        except ValueError:
            status = EnvironmentStatus.ENVIRONMENT_FAILED
        if status not in (
            EnvironmentStatus.ENVIRONMENT_FAILED,
            EnvironmentStatus.ENVIRONMENT_BLOCKED,
            EnvironmentStatus.RESOURCE_UNAVAILABLE,
        ):
            status = EnvironmentStatus.ENVIRONMENT_FAILED
        message = args.get("message") or "Labwright 无法交付该环境"
        ctx.response = EnvironmentResponse(
            status=status, request_id=ctx.request_id, sandbox_id=ctx.sandbox_id,
            message=message, detail={"ts": time.time()},
        )
        ctx.emit("labwright", "failed", {
            "request_id": ctx.request_id, "status": status.value, "message": message[:500],
        })
        return _text({"ok": True, "status": status.value})

    @tool(
        "propose_reusable_skill",
        "为已验证的、可复用的环境/工具/资源处理经验提出 Skill 候选。"
        "候选需要人工审核；不得放入科学决策、密钥、task id 或一次性路径。",
        {
            "name": str, "description": str, "trigger": str,
            "instructions": str, "verification": str, "evidence": list,
        },
    )
    async def propose_reusable_skill(args):
        if ctx.skill_candidates is None:
            return _text({"ok": False, "error": "skill candidate storage is unavailable"})
        try:
            candidate_id = ctx.skill_candidates.propose(
                "labwright",
                ctx.task_id,
                SkillProposal(
                    name=args["name"], description=args["description"],
                    trigger=args["trigger"], instructions=args["instructions"],
                    verification=args.get("verification") or "Run the documented verification.",
                    evidence=[str(value) for value in args.get("evidence") or []],
                ),
            )
            ctx.emit("labwright", "skill_candidate_proposed", {
                "candidate_id": candidate_id, "request_id": ctx.request_id,
            })
            return _text({"ok": True, "candidate_id": candidate_id, "review_required": True})
        except Exception as exc:  # noqa: BLE001
            return _text({"ok": False, "error": str(exc)})

    return create_sdk_mcp_server(
        "labenv", "1.0.0",
        [
            create_sandbox, sandbox_exec, collect_resource, mount_resource,
            verify_resource, publish_manifest, request_researcher_decision, mark_failed,
            propose_reusable_skill,
        ],
    )
