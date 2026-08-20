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
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from claude_agent_sdk import create_sdk_mcp_server, tool

from zero.config import Config
from zero.labwright.inventory import collect_and_write_inventory
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
    DecisionCandidate, DecisionRequest,
    EnvironmentResponse,
    EnvironmentStatus,
)
from zero.protocol.resources import (
    ArtifactRef, ResourceKind, ResourceLockEntry, VerificationEvidence,
)
from zero.resources.cache import CachedResource, ResourceCache
from zero.resources.deploy_master import BuildToolRequest, DeployMasterClient
from zero.resources.errors import ResourceIntegrationError
from zero.resources.locks import ResourceLockStore, lock_digest, validate_release_lock
from zero.resources.registry import ResourceRegistry
from zero.resources.trisol import TrisolClient
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


def _trisol_provenance(uri: str) -> dict[str, Any]:
    parsed = urlsplit(uri)
    if parsed.scheme != "trisol":
        return {}
    parts = parsed.path.strip("/").split("/")
    query = parse_qs(parsed.query)
    return {
        "trisol_id": parts[0] if parts else None,
        "trisol_version": parts[1] if len(parts) > 1 else None,
        "trisol_team": (query.get("team") or [None])[0],
        "trisol_splits": query.get("split", []),
    }


def _verification_failed(report: VerificationReport) -> bool:
    """Return true when any release-critical verification has failed."""
    return any(
        status == "failed"
        for status in (
            report.package_import,
            report.tool_healthcheck,
            report.model_load,
            report.dataset_read,
        )
    )


def _text(obj: Any) -> dict:
    if isinstance(obj, str):
        payload = obj
    else:
        payload = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    return {"content": [{"type": "text", "text": payload}]}


async def _build_tool_resource(ctx: "LabwrightContext", args: dict[str, Any]) -> dict:
    """Calling-side contract for the Deploy Master MCP tool."""
    if ctx.deploy_master is None:
        return _text({"ok": False, "error": "Deploy Master is not configured"})
    try:
        request_args = dict(args)
        max_rebuilds = int(request_args.pop("max_rebuilds", 0) or 0)
        request = BuildToolRequest.model_validate(request_args)
        ctx.emit("labwright", "deploymaster_build_submitted", {
            "request_id": ctx.request_id, "max_rebuilds": max_rebuilds,
        })
        built = await ctx.deploy_master.build(request, max_rebuilds=max_rebuilds)
        ctx.emit("labwright", "deploymaster_build_finished", {
            "request_id": ctx.request_id, "task_id": built.task_id,
            "status": "succeeded", "has_image_digest": bool(built.image_digest),
            "build_attempts": built.build_attempts,
        })
        return _text({"ok": True, "artifact": built.model_dump(mode="json")})
    except (ValueError, ResourceIntegrationError) as exc:
        ctx.emit("labwright", "deploymaster_build_finished", {
            "request_id": ctx.request_id, "status": "failed",
            "error_type": type(exc).__name__,
        })
        return _text({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:500]})


def _persist_environment_baseline(ctx: "LabwrightContext", manifest: EnvironmentManifest) -> None:
    """Persist the clean, pre-Researcher snapshot immediately.

    Completion later replaces this record with the resolved image URL, but
    writing it here preserves the snapshot id even if a run is interrupted.
    """
    path = ctx.config.run_dir(ctx.task_id) / "environment.json"
    payload = {
        "schema_version": 1,
        "backend": ctx.manager.backend,
        "snapshot_scope": "environment_baseline",
        "snapshot_timing": "Labwright publish_manifest before handoff to Researcher",
        "status": "snapshot_submitted",
        "manifest": manifest.model_dump(mode="json"),
        "image": {
            "digest": manifest.image_digest,
            "status": "submitted" if manifest.image_digest else "unavailable",
            "url": None,
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


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
    env_sandbox_id: Optional[str] = None
    exp_sandbox_id: Optional[str] = None
    last_environment_id: Optional[str] = None
    last_pip_freeze: list[str] = field(default_factory=list)
    collected: dict[str, CachedResource] = field(default_factory=dict)  # "model:name" -> res
    source_overrides: dict[str, str] = field(default_factory=dict)
    pending_decision: Optional[DecisionRequest] = None
    response: Optional[EnvironmentResponse] = None
    sandbox_manifest: dict[str, EnvironmentManifest] = field(default_factory=dict)
    sandbox_spec: dict[str, EnvironmentSpec] = field(default_factory=dict)
    skill_candidates: Optional[SkillCandidates] = None
    registry: Optional[ResourceRegistry] = None
    trisol: Optional[TrisolClient] = None
    deploy_master: Optional[DeployMasterClient] = None
    lock_store: Optional[ResourceLockStore] = None
    registry_lookup_unavailable: bool = False

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
        "创建 **env** Sandbox（默认）用于装依赖与验证；publish_manifest 后会自动再开 "
        "**exp** Sandbox 交给 Researcher。"
        "中途加包请重新 create_sandbox(role=env) 再建环境，不要在 exp 上 commit。"
        "可选 mounts：[{kind,name,version,host_path}]。返回 sandbox_id 与 workspace。",
        {
            "python_version": str,
            "cpu_count": int,
            "memory_gb": int,
            "gpu_count": int,
            "mounts": list,
            "role": str,
            "base_image": str,
        },
    )
    async def create_sandbox(args):
        mounts_in = args.get("mounts") or []
        mounts: list[MountSpec] = []
        for m in mounts_in:
            ref = ResourceRef(
                kind=m["kind"], name=m["name"], version=m.get("version") or "main",
                host_path=m["host_path"], source=m.get("source"),
            )
            mounts.append(MountSpec(ref=ref, read_only=True))
        python = args.get("python_version") or (
            ctx.spec.base_environment.python if ctx.spec else "3.11"
        )
        role = (args.get("role") or "env").strip().lower()
        if role not in ("env", "exp"):
            role = "env"
        if role == "exp":
            return _text({
                "ok": False,
                "error": (
                    "do not create exp sandboxes directly; create_sandbox(role=env), "
                    "install+verify, then publish_manifest (it spawns the exp sandbox)"
                ),
            })
        base_image = (args.get("base_image") or "").strip() or ctx.config.docker_base_image
        # Prefer last frozen image URL for mid-run env revisions when available.
        if not args.get("base_image"):
            img_path = ctx.config.run_dir(ctx.task_id) / "environment" / "image.json"
            if img_path.is_file():
                try:
                    prev = json.loads(img_path.read_text(encoding="utf-8"))
                    if prev.get("url"):
                        base_image = prev["url"]
                    elif prev.get("reference"):
                        base_image = prev["reference"]
                except (OSError, json.JSONDecodeError):
                    pass
        handle = await asyncio.to_thread(
            lambda: ctx.manager.create(
                ctx.task_id,
                base_image=base_image,
                mounts=mounts,
                cpu_count=int(args.get("cpu_count") or (ctx.spec.compute.cpu_count if ctx.spec else 2)),
                memory_gb=int(args.get("memory_gb") or (ctx.spec.compute.memory_gb if ctx.spec else 8)),
                gpu_count=int(args.get("gpu_count") or (ctx.spec.compute.gpu_count if ctx.spec else 0)),
                python_version=str(python),
                role="env",
            )
        )
        ctx.sandbox_id = handle.sandbox_id
        ctx.env_sandbox_id = handle.sandbox_id
        ctx.emit("labwright", "sandbox_created", {
            "request_id": ctx.request_id, "sandbox_id": handle.sandbox_id,
            "backend": handle.backend, "workspace": handle.workspace_path,
            "role": "env",
        })
        return _text({
            "ok": True,
            "sandbox_id": handle.sandbox_id,
            "role": "env",
            "workspace": handle.workspace_path,
            "backend": handle.backend,
            "resource_paths": handle.resource_paths,
            "note": (
                "This is an env sandbox (scratch). Install/verify here; "
                "publish_manifest will spawn a clean exp sandbox for the Researcher."
            ),
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
        if override is None and ctx.registry is not None and kind in {"model", "dataset"}:
            try:
                candidates = await ctx.registry.search(
                    kind=ResourceKind(kind), text=name, limit=10,
                    language="en-US", constraints={}, required_capabilities=[],
                )
                exact = [candidate for candidate in candidates if candidate.match == "exact"]
                usable = exact if len(exact) == 1 else candidates
                if len(usable) == 1:
                    override = usable[0].artifact.uri
                elif len(usable) > 1:
                    decision = DecisionRequest(
                        resource_type=kind, resource_name=name,
                        reason=f"Literature Sage returned multiple materializable {kind} assets",
                        candidates=[DecisionCandidate(
                            id=f"sage-{index}", source=candidate.artifact.uri,
                            note=f"{candidate.name} ({candidate.resource_unique_key})",
                        ) for index, candidate in enumerate(usable)],
                        scientific_impact="不同资产 ID 或版本可能改变实验结果，必须显式选择",
                    )
                    return _text({"ok": False, "needs_decision": True,
                                  "decision": decision.model_dump(exclude_none=True)})
            except ResourceIntegrationError:
                ctx.registry_lookup_unavailable = True
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
        source = None
        if not host_path:
            cached = ctx.collected.get(f"{args['kind']}:{args['name']}")
            if cached is None:
                return _text({"ok": False, "error": "resource not collected; call collect_resource first"})
            host_path = cached.host_path
            version = cached.version
            source = cached.source
        ref = ResourceRef(kind=args["kind"], name=args["name"], version=version,
                          host_path=host_path, source=source)
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
        "search_resource",
        "优先查询 Literature Sage。一次调用自动完成 Search + Detail，并过滤停用、"
        "缺制品或不满足硬约束的候选。只用于 tool/model/dataset。",
        {
            "kind": str, "text": str, "keywords": dict, "language": str,
            "limit": int, "required_capabilities": list, "constraints": dict,
        },
    )
    async def search_resource(args):
        if ctx.registry is None:
            ctx.registry_lookup_unavailable = True
            return _text({"ok": False, "degraded": True, "error": "resource registry disabled"})
        try:
            kind = ResourceKind(args["kind"])
            ctx.emit("labwright", "resource_search_started", {
                "request_id": ctx.request_id, "kind": kind.value,
                "text": str(args.get("text") or "")[:200],
            })
            candidates = await ctx.registry.search(
                kind=kind, text=args.get("text") or "",
                keywords=args.get("keywords") or {}, language=args.get("language") or "en-US",
                limit=max(1, min(int(args.get("limit") or 10), 50)),
                required_capabilities=args.get("required_capabilities") or [],
                constraints=args.get("constraints") or {},
            )
            ctx.emit("labwright", "resource_search_finished", {
                "request_id": ctx.request_id, "kind": kind.value,
                "candidate_count": len(candidates), "status": "ok",
            })
            return _text({"ok": True, "candidates": [c.model_dump(mode="json") for c in candidates]})
        except (ValueError, ResourceIntegrationError) as exc:
            ctx.registry_lookup_unavailable = True
            ctx.emit("labwright", "resource_search_finished", {
                "request_id": ctx.request_id, "status": "degraded",
                "error_type": type(exc).__name__,
            })
            return _text({"ok": False, "degraded": True, "error_type": type(exc).__name__, "error": str(exc)[:500]})

    @tool(
        "build_tool_resource",
        "Literature Sage 未命中 tool 时，调用 Deploy Master 从固定代码仓库构建并验证 OCI 镜像。"
        "返回的制品仍须在本题 Sandbox 验证，再用 publish_resource 入库并写 lock。",
        {
            "github_url": str, "build_instructions": str, "verify_commands": list,
            "dockerfile_path": str, "build_context": str,
            "repository_dockerfile_policy": str, "max_rebuilds": int,
        },
    )
    async def build_tool_resource(args):
        return await _build_tool_resource(ctx, args)

    @tool(
        "publish_resource",
        "把已物化且已验证的 tool/model/dataset 复用或写入 Literature Sage，并写入"
        "resources.lock.json。已有相同制品会幂等复用；不同制品绝不覆盖。",
        {
            "kind": str, "requirement_id": str, "resource_unique_key": str,
            "resolution": str, "metadata": dict, "artifact": dict,
            "verification": dict, "capabilities": list, "provenance": dict,
        },
    )
    async def publish_resource(args):
        if ctx.lock_store is None:
            return _text({"ok": False, "error": "resource lock storage unavailable"})
        try:
            kind = ResourceKind(args["kind"])
            artifact = ArtifactRef.model_validate(args["artifact"])
            verification = VerificationEvidence.model_validate(args["verification"])
            if verification.status != "passed":
                return _text({"ok": False, "error": "verification.status must be passed"})
            if ctx.config.resource_release_strict and not artifact.digest:
                return _text({"ok": False, "error": "strict release requires immutable artifact digest"})
            if kind == ResourceKind.TOOL and artifact.type != "oci_image":
                return _text({"ok": False, "error": "tool artifacts must be OCI images"})
            if kind != ResourceKind.TOOL and artifact.type == "oci_image":
                return _text({"ok": False, "error": "model/dataset artifacts cannot be OCI images"})
            if kind in (ResourceKind.MODEL, ResourceKind.DATASET):
                requirement_name = str(args["requirement_id"]).partition(":")[2]
                cached = ctx.collected.get(f"{kind.value}:{requirement_name}")
                if cached is None:
                    return _text({
                        "ok": False,
                        "error": "model/dataset must be materialized with collect_resource before locking",
                    })
                if not cached.sha256 or artifact.digest != cached.sha256:
                    return _text({
                        "ok": False,
                        "error": "artifact digest does not match materialized content",
                        "expected_digest": cached.sha256,
                    })
                source = cached.source or ""
                if not source.startswith("trisol://"):
                    if ctx.trisol is None:
                        return _text({"ok": False, "error": "Trisol publishing is not configured"})
                    uploaded = await asyncio.to_thread(
                        ctx.trisol.publish, kind.value, args["resource_unique_key"],
                        Path(cached.host_path), cached.sha256,
                        str((args.get("metadata") or {}).get("description") or "Zero verified asset"),
                    )
                    source = uploaded.uri()
                    cached.source = source
                artifact = artifact.model_copy(update={
                    "type": "object_bundle", "uri": source, "digest": cached.sha256,
                    "version": cached.version,
                })
            unique_key = args["resource_unique_key"]
            candidate = None
            if ctx.registry is not None and ctx.config.resource_publish_enabled:
                ctx.emit("labwright", "resource_publish_started", {
                    "request_id": ctx.request_id, "kind": kind.value,
                    "resource_unique_key": unique_key,
                })
                candidate = await ctx.registry.publish(
                    kind=kind, unique_key=unique_key,
                    metadata=args.get("metadata") or {}, artifact=artifact,
                    verification=verification, capabilities=args.get("capabilities") or [],
                )
            resolution = args.get("resolution") or "existing"
            entry = ResourceLockEntry(
                requirement_id=args["requirement_id"], kind=kind,
                resource_ref=f"literature-sage:{kind.value}:{unique_key}",
                resolution=resolution, artifact=artifact, verification=verification,
                provenance={**(args.get("provenance") or {}),
                            **_trisol_provenance(artifact.uri),
                            "registry_lookup": "unavailable" if ctx.registry_lookup_unavailable else "available"},
            )
            digest = ctx.lock_store.put(entry)
            ctx.emit("labwright", "resource_lock_written", {
                "request_id": ctx.request_id, "requirement_id": entry.requirement_id,
                "resources_lock_digest": digest,
            })
            ctx.emit("labwright", "resource_publish_finished", {
                "request_id": ctx.request_id, "kind": kind.value, "status": "ok",
            })
            return _text({
                "ok": True, "resource_ref": entry.resource_ref,
                "resources_lock_digest": digest,
                "candidate": candidate.model_dump(mode="json") if candidate else None,
                "artifact": artifact.model_dump(mode="json"),
                "warnings": [] if artifact.digest else ["mutable_reference"],
            })
        except (ValueError, ResourceIntegrationError) as exc:
            return _text({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:500]})

    @tool(
        "publish_manifest",
        "验证通过后：冻结 **env** Sandbox（inventory + image commit），再自动生成干净的 "
        "**exp** Sandbox；返回给 Researcher 的是 exp sandbox_id。"
        "禁止对 exp 调用本工具。可选传入 packages/tools 覆盖列表。",
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
        required_resources = {
            **{f"tool:{item.name}": "tool" for item in ctx.spec.tools},
            **{f"model:{item.name}": "model" for item in ctx.spec.models},
            **{f"dataset:{item.name}": "dataset" for item in ctx.spec.datasets},
        }
        resources_lock_digest = None
        resource_lock = None
        if required_resources:
            if ctx.lock_store is None:
                return _text({"ok": False, "error": "required resources have no lock store"})
            resource_lock = ctx.lock_store.read()
            violations = validate_release_lock(
                resource_lock,
                required_resources,
                require_immutable=ctx.config.resource_release_strict,
            )
            if violations:
                return _text({"ok": False, "error": "required resources must be locked and verified",
                              "violations": violations})
            resources_lock_digest = lock_digest(resource_lock)
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

        if _verification_failed(ver):
            return _text({
                "ok": False,
                "error": "verification failed; fix resources before publishing",
                "verification": ver.model_dump(exclude_none=True),
            })

        role = ctx.manager.role_of(sid) or "env"
        if role == "exp":
            return _text({
                "ok": False,
                "error": (
                    f"sandbox {sid} is an experiment sandbox; freeze/commit is only "
                    "allowed on env sandboxes. create_sandbox(role=env), apply changes, "
                    "then publish_manifest again."
                ),
            })

        # Wipe workspace so LBG image commit does not bake experiment files.
        await asyncio.to_thread(ctx.manager.prepare_env_for_freeze, sid)

        try:
            digest = await asyncio.to_thread(ctx.manager.snapshot, sid)
        except RuntimeError as exc:
            return _text({"ok": False, "error": str(exc)})

        env_manifest = EnvironmentManifest(
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
            resources_lock_digest=resources_lock_digest,
        )

        # Probe freeze for degraded spawn + inventory before spawning exp.
        pip_freeze: list[str] = []
        try:
            fr = await asyncio.to_thread(
                ctx.manager.exec, sid, "python3 -m pip freeze 2>/dev/null", 120,
            )
            if fr.exit_code == 0:
                pip_freeze = [
                    ln.strip() for ln in (fr.stdout or "").splitlines()
                    if ln.strip() and not ln.startswith("#")
                ]
        except Exception:  # noqa: BLE001
            pip_freeze = []
        ctx.last_pip_freeze = pip_freeze

        parent_mounts = []
        parent_spec = ctx.manager._specs.get(sid)  # noqa: SLF001
        if parent_spec is not None:
            parent_mounts = list(parent_spec.mounts)

        try:
            exp_handle = await asyncio.to_thread(
                lambda: ctx.manager.spawn_experiment_sandbox(
                    task_id=ctx.task_id,
                    env_sandbox_id=sid,
                    digest=digest,
                    mounts=parent_mounts,
                    python_version=ctx.spec.base_environment.python,
                    cpu_count=ctx.spec.compute.cpu_count if ctx.spec else 2,
                    memory_gb=ctx.spec.compute.memory_gb if ctx.spec else 8,
                    gpu_count=ctx.spec.compute.gpu_count if ctx.spec else 0,
                    pip_freeze=pip_freeze,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _text({
                "ok": False,
                "error": f"env frozen but failed to spawn exp sandbox: {exc}",
                "env_sandbox_id": sid,
                "image_digest": digest,
            })

        # Researcher-facing manifest points at the exp sandbox.
        manifest = env_manifest.model_copy(deep=True)
        manifest.sandbox_id = exp_handle.sandbox_id
        manifest.workspace = exp_handle.workspace_path

        as_added = bool(args.get("as_resource_added"))
        status = EnvironmentStatus.RESOURCE_ADDED if as_added else EnvironmentStatus.ENVIRONMENT_READY
        msg = args.get("message") or (
            "资源已加入；已从干净环境镜像生成新的实验 Sandbox"
            if as_added else
            "环境已就绪（env 已冻结；已生成干净 exp Sandbox）"
        )
        response = EnvironmentResponse(
            status=status, request_id=ctx.request_id, sandbox_id=exp_handle.sandbox_id,
            manifest=manifest, message=msg,
            detail={
                "ts": time.time(),
                "env_sandbox_id": sid,
                "exp_sandbox_id": exp_handle.sandbox_id,
                "spawn_mode": exp_handle.spawn_mode,
            },
        )
        try:
            base_image = ""
            if ctx.spec is not None and ctx.spec.base_environment is not None:
                base_image = (
                    f"python:{ctx.spec.base_environment.python}"
                    if ctx.spec.base_environment.python
                    else ""
                )
            inventory = collect_and_write_inventory(
                run_dir=ctx.config.run_dir(ctx.task_id),
                manager=ctx.manager,
                manifest=env_manifest,
                backend=ctx.manager.backend,
                base_image=base_image or "",
                scope="clean_baseline",
                spawn_mode=exp_handle.spawn_mode,
                env_sandbox_id=sid,
                exp_sandbox_id=exp_handle.sandbox_id,
                resource_lock=resource_lock,
            )
            ctx.last_environment_id = inventory.environment_id
            exp_handle.environment_id = inventory.environment_id
        except Exception as exc:  # noqa: BLE001
            ctx.emit("labwright", "inventory_failed", {"error": str(exc)[:500]})
            _persist_environment_baseline(ctx, env_manifest)
            return _text({
                "ok": False,
                "error": f"environment inventory validation failed: {exc}",
                "env_sandbox_id": sid,
                "exp_sandbox_id": exp_handle.sandbox_id,
                "image_digest": digest,
            })

        # Publish READY state only after the release inventory and its
        # lock/Manifest/mount consistency checks have completed successfully.
        ctx.response = response
        ctx.sandbox_manifest[sid] = env_manifest
        ctx.sandbox_manifest[exp_handle.sandbox_id] = manifest
        ctx.sandbox_spec[sid] = ctx.spec
        ctx.sandbox_spec[exp_handle.sandbox_id] = ctx.spec
        ctx.env_sandbox_id = sid
        ctx.exp_sandbox_id = exp_handle.sandbox_id
        ctx.sandbox_id = exp_handle.sandbox_id

        ctx.emit("labwright", "manifest_published", {
            "request_id": ctx.request_id,
            "sandbox_id": exp_handle.sandbox_id,
            "env_sandbox_id": sid,
            "status": status.value,
            "spawn_mode": exp_handle.spawn_mode,
            "environment_id": ctx.last_environment_id,
            "resources_lock_digest": resources_lock_digest,
        })
        return _text({
            "ok": True,
            "status": status.value,
            "sandbox_id": exp_handle.sandbox_id,
            "env_sandbox_id": sid,
            "exp_sandbox_id": exp_handle.sandbox_id,
            "spawn_mode": exp_handle.spawn_mode,
            "environment_id": ctx.last_environment_id,
            "summary": manifest.researcher_summary(),
            "environment_md": str(
                ctx.config.run_dir(ctx.task_id) / "environment" / "environment.md"
            ),
            "note": (
                "Hand the Researcher the exp sandbox_id only. "
                "Do not ask them to write code into the env sandbox."
            ),
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
            verify_resource, search_resource, build_tool_resource, publish_resource, publish_manifest,
            request_researcher_decision, mark_failed,
            propose_reusable_skill,
        ],
    )
