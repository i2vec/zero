"""MCP surface through which agents can stage (not publish) skill candidates."""

from __future__ import annotations

import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from zero.skills.candidates import Role, SkillCandidates, SkillProposal


def build_skill_capture_server(candidates: SkillCandidates, *, role: Role, task_id: str) -> dict:
    @tool(
        "propose_reusable_skill",
        "提出一个可复用的本地 Skill 候选。只在你已验证可复现的经验时调用；"
        "候选会人工审核后才生效，绝不包含密钥、任务 id 或一次性路径。",
        {
            "name": str, "description": str, "trigger": str,
            "instructions": str, "verification": str, "evidence": list,
        },
    )
    async def propose_reusable_skill(args):
        try:
            proposal_id = candidates.propose(
                role,
                task_id,
                SkillProposal(
                    name=args["name"],
                    description=args["description"],
                    trigger=args["trigger"],
                    instructions=args["instructions"],
                    verification=args.get("verification") or "Validate with the stated workflow.",
                    evidence=[str(value) for value in args.get("evidence") or []],
                ),
            )
            return {"content": [{"type": "text", "text": json.dumps(
                {"ok": True, "candidate_id": proposal_id, "review_required": True},
                ensure_ascii=False,
            )}]}
        except Exception as exc:  # noqa: BLE001
            return {"content": [{"type": "text", "text": json.dumps(
                {"ok": False, "error": str(exc)}, ensure_ascii=False,
            )}]}

    return create_sdk_mcp_server(
        f"{role}_skill_capture", "1.0.0", [propose_reusable_skill],
    )
