"""In-process MCP server exposing Labwright to the Researcher (doc section 23).

To the Researcher these are just tools (``mcp__labwright__*``); to the system
Labwright is an independent agent service. Handlers close over a
``LabwrightService`` running in the orchestrator process.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from zero.labwright.service import LabwrightService
from zero.protocol.spec import EnvironmentSpec, ResourceAddition
from zero.protocol.status import EnvironmentResponse, ResearcherDecision


def _text(obj: Any) -> dict:
    if isinstance(obj, str):
        payload = obj
    else:
        payload = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    return {"content": [{"type": "text", "text": payload}]}


def _response_view(resp: EnvironmentResponse) -> dict:
    """The compact, isolation-preserving view the Researcher receives."""
    view: dict[str, Any] = {"status": resp.status.value, "request_id": resp.request_id}
    if resp.message:
        view["message"] = resp.message
    if resp.sandbox_id:
        view["sandbox_id"] = resp.sandbox_id
    if resp.decision is not None:
        view["decision"] = resp.decision.model_dump(exclude_none=True)
        if resp.decision.question:
            view["question"] = resp.decision.question
    if resp.manifest is not None:
        view["sandbox_id"] = resp.manifest.sandbox_id
        view["summary"] = resp.manifest.researcher_summary()
        view["workspace"] = resp.manifest.workspace
        view["packages"] = {k: v.version for k, v in resp.manifest.packages.items()}
        view["models"] = {k: v.path for k, v in resp.manifest.models.items()}
        view["datasets"] = {k: v.path for k, v in resp.manifest.datasets.items()}
    return view


def build_labwright_server(service: LabwrightService) -> dict:
    @tool("ensure_environment",
          "根据结构化 EnvironmentSpec 准备并验证一个实验 Sandbox。此调用会阻塞直到 Labwright "
          "完成本轮，直接返回终态：ENVIRONMENT_READY(+sandbox_id/summary) / "
          "NEEDS_DECISION(+question/candidates) / 失败。无需轮询。",
          {"spec": dict})
    async def ensure_environment(args):
        try:
            spec = EnvironmentSpec.model_validate(args["spec"])
        except Exception as exc:  # noqa: BLE001
            return _text({"status": "ENVIRONMENT_FAILED", "error": f"invalid spec: {exc}"})
        resp = await service.ensure_environment(spec)
        return _text(_response_view(resp))

    @tool("get_environment_manifest",
          "获取某个 sandbox 的完整 Environment Manifest（版本/验证/溯源）。",
          {"sandbox_id": str})
    async def get_environment_manifest(args):
        manifest = service.get_environment_manifest(args["sandbox_id"])
        if manifest is None:
            return _text({"error": f"no manifest for sandbox {args['sandbox_id']}"})
        return _text(manifest.model_dump(exclude_none=True))

    @tool("add_resources",
          "在实验过程中向现有 Sandbox 增补资源（python_package/tool/model/dataset）。返回更新后的 Manifest。",
          {"sandbox_id": str, "resources": list})
    async def add_resources(args):
        try:
            additions = [ResourceAddition.model_validate(r) for r in args["resources"]]
        except Exception as exc:  # noqa: BLE001
            return _text({"error": f"invalid resources: {exc}"})
        resp = await service.add_resources(args["sandbox_id"], additions)
        return _text(_response_view(resp))

    @tool("resolve_environment_decision",
          "当上一个调用返回 NEEDS_DECISION 时，回传决策以继续（同样阻塞，直接返回后续终态）。"
          "decision 可含 choose(候选id)/use_source(显式来源)/accept/guidance(开放式问题的自由文本回答)/abort。",
          {"request_id": str, "decision": dict})
    async def resolve_environment_decision(args):
        try:
            decision = ResearcherDecision.model_validate(args["decision"])
        except Exception as exc:  # noqa: BLE001
            return _text({"error": f"invalid decision: {exc}"})
        resp = await service.resolve_environment_decision(args["request_id"], decision)
        return _text(_response_view(resp))

    @tool("report_environment_issue",
          "报告一个疑似环境问题（不确定是否环境导致时用）。阻塞：Labwright 诊断修复后直接返回终态。",
          {"sandbox_id": str, "issue": str})
    async def report_environment_issue(args):
        resp = await service.report_environment_issue(args["sandbox_id"], args["issue"])
        return _text(_response_view(resp))

    return create_sdk_mcp_server(
        "labwright", "1.0.0",
        [ensure_environment, get_environment_manifest,
         add_resources, resolve_environment_decision, report_environment_issue],
    )
