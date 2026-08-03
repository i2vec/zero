"""MCP tools: search / get / record Researcher experience entries."""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from zero.experience.store import ExperienceStore


def _text(payload: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def build_experience_server(store: ExperienceStore) -> dict:
    @tool(
        "search_experience",
        "检索跨任务共享的 Researcher 经验库。开场或卡住前可查；"
        "用关键词和/或 tags 找可迁移教训，不要指望库里有本题答案。"
        "tags 无则传 []；limit 常用 8。",
        {"query": str, "tags": list, "limit": int},
    )
    async def search_experience(args):
        query = str(args.get("query") or "")
        raw_tags = args.get("tags") or []
        tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
        try:
            limit = int(args.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        hits = store.search(query=query, tags=tags, limit=limit)
        return _text({"ok": True, "count": len(hits), "hits": hits})

    @tool(
        "get_experience",
        "按 id 读取一条经验全文（先 search 再 get）。",
        {"id": str},
    )
    async def get_experience(args):
        entry_id = str(args.get("id") or "").strip()
        got = store.get(entry_id)
        if got is None:
            return _text({"ok": False, "error": f"experience not found: {entry_id}"})
        return _text({"ok": True, "entry": got})

    @tool(
        "record_experience",
        "把一条可迁移的短经验写入共享经验库（立即生效，供后续 run 检索）。"
        "仅在教训能用于别的题时调用；禁止写入本题最终数值、一次性路径、密钥、"
        "task/sandbox id、或装环境细节。长流程请改用 propose_reusable_skill。",
        {
            "title": str,
            "tags": list,
            "trigger": str,
            "lesson": str,
            "avoid": str,
            "confidence": str,
        },
    )
    async def record_experience(args):
        raw_tags = args.get("tags") or []
        tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
        try:
            result = store.record(
                title=str(args.get("title") or ""),
                tags=tags,
                trigger=str(args.get("trigger") or ""),
                lesson=str(args.get("lesson") or ""),
                avoid=str(args.get("avoid") or ""),
                confidence=str(args.get("confidence") or "medium"),
            )
            return _text(result)
        except ValueError as exc:
            return _text({"ok": False, "error": str(exc)})
        except OSError as exc:
            return _text({"ok": False, "error": f"write failed: {exc}"})

    return create_sdk_mcp_server(
        "experience",
        "1.0.0",
        [search_experience, get_experience, record_experience],
    )
