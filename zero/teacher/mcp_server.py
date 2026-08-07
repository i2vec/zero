"""In-process MCP server exposing the Teacher to the Researcher.

To the Researcher this is one tool (``mcp__teacher__ask_teacher``); to the system
the Teacher is an independent agent with its own session and its own privileged
material. The handler closes over a ``TeacherService`` in the orchestrator
process and blocks until the Teacher's turn finishes.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from zero.protocol.teaching import TeacherAnswer, TeachingKind
from zero.teacher.service import TeacherService


def _text(obj: Any) -> dict:
    if isinstance(obj, str):
        payload = obj
    else:
        payload = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    return {"content": [{"type": "text", "text": payload}]}


def _answer_view(answer: TeacherAnswer) -> dict:
    view: dict[str, Any] = {
        "kind": answer.kind.value,
        "ask_id": answer.ask_id,
        "asks_used": answer.asks_used,
        "asks_remaining": answer.asks_remaining,
        "package_revision": answer.package_revision,
    }
    if answer.package_delta:
        view["package_delta"] = answer.package_delta
        view["package_note"] = (
            "Live task package updated. Follow package_delta; where it conflicts "
            "with earlier statement text, the new package wins. No graded answers "
            "are included."
        )
    if answer.kind is TeachingKind.TASK_AMENDMENT and answer.amendment is not None:
        view["task_amendment"] = answer.amendment.model_dump(exclude_none=True)
        view["note"] = (
            "题面本身有缺陷。以这段订正与 package_delta 为准继续解题，"
            "并在 conclusion.md 里记录它。"
        )
    elif answer.kind in (TeachingKind.GRADER_AMENDMENT, TeachingKind.BOTH_AMENDMENT):
        view["content"] = answer.content or (
            "Package/grader was amended for literature fidelity. "
            "Continue under the updated constraints in package_delta."
        )
        if answer.amendment is not None:
            view["task_amendment"] = answer.amendment.model_dump(exclude_none=True)
    else:
        view["content"] = answer.content
    return view


def build_teacher_server(service: TeacherService) -> dict:
    @tool(
        "ask_teacher",
        "卡住时问老师（仅科学障碍，不问环境/路径/镜像）。老师持有本题额外 hint（你看不到），"
        "返回动作标识 kind：HINT（方法提示）、TASK_AMENDMENT / GRADER_AMENDMENT / "
        "BOTH_AMENDMENT（题包订正，看 package_delta）、或 NO_HELP。"
        "此调用阻塞直到老师答完。提问次数有上限——只在真正的科学/题包障碍上花它。",
        {"question": str, "what_i_tried": str, "where_stuck": str},
    )
    async def ask_teacher(args):
        answer = await service.ask(
            args.get("question") or "",
            args.get("what_i_tried") or "",
            args.get("where_stuck") or "",
        )
        return _text(_answer_view(answer))

    return create_sdk_mcp_server("teacher", "1.0.0", [ask_teacher])
