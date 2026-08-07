"""Live trace viewer: Segment Inspector over Researcher / Labwright / Teacher.

Reads ``runs/<task_id>/trace/`` only:

* **Model I/O** — ``researcher.jsonl`` / ``labwright.jsonl`` / ``teacher.jsonl``
* **Layer 2 events** — ``events.jsonl`` (``turn:*`` mirrors skipped in the UI)

SSE streams orchestration events plus **compact model turns**: each model event
carries only the incremental ``turn_input`` and a lean ``output`` (no repeated
full conversation history / tool schemas / duplicate ``raw``).

Static front-end under ``static/``; this module serves ``/latest`` and ``/stream``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).parent / "static"
_INJECTED_USER_PREFIXES = (
    "<system-reminder>",
    "Available agent types for the Agent tool:",
    "The following skills are available for use with the Skill tool:",
    "As you answer the user's questions, you can use the following context:",
    "IMPORTANT: this context may or may not be relevant to your task.",
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _list_runs(runs_dir: Path) -> list[dict[str, Any]]:
    """Return runs under ``runs_dir``, newest first."""
    if not runs_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for child in runs_dir.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        trace = child / "trace"
        events = trace / "events.jsonl"
        # Include dirs that look like runs (have trace/ or run.json / meta/).
        if not (trace.is_dir() or (child / "run.json").is_file() or (child / "meta").is_dir()):
            continue
        mtime = 0.0
        for p in (events, child / "run.json", child / "meta" / "task.json", child):
            try:
                mtime = max(mtime, p.stat().st_mtime)
            except OSError:
                continue
        run_meta = _read_json(child / "run.json")
        task_meta = _read_json(child / "meta" / "task.json")
        status = (
            run_meta.get("status")
            or task_meta.get("task_status")
            or ("live" if events.is_file() else "unknown")
        )
        agents = {
            "researcher": (trace / "researcher.jsonl").is_file(),
            "labwright": (trace / "labwright.jsonl").is_file(),
            "teacher": (trace / "teacher.jsonl").is_file(),
            "events": events.is_file(),
        }
        rows.append({
            "task_id": child.name,
            "status": status,
            "mtime": mtime,
            "agents": agents,
            "backend": run_meta.get("backend") or task_meta.get("backend"),
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def _latest_task_id(runs_dir: Path) -> Optional[str]:
    rows = _list_runs(runs_dir)
    return rows[0]["task_id"] if rows else None


def _stream_sources(task_id: str, runs_dir: Path) -> list[tuple[str, Optional[str], Path]]:
    run_trace = runs_dir / task_id / "trace"
    return [
        ("orch", None, run_trace / "events.jsonl"),
        ("model", "researcher", run_trace / "researcher.jsonl"),
        ("model", "labwright", run_trace / "labwright.jsonl"),
        ("model", "teacher", run_trace / "teacher.jsonl"),
    ]


def _normalize_message(m: Any) -> dict[str, Any]:
    """Flatten one Anthropic/OpenAI message into {role, blocks:[{type,text}]}."""
    if not isinstance(m, dict):
        return {"role": "?", "blocks": [{"type": "text", "text": str(m)}]}
    role = m.get("role", "?")
    c = m.get("content")
    blocks: list[dict[str, Any]] = []
    if isinstance(c, str):
        blocks.append({"type": "text", "text": c})
    elif isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                blocks.append({"type": "text", "text": str(b)})
                continue
            t = b.get("type")
            if t == "text":
                blocks.append({"type": "text", "text": b.get("text", "")})
            elif t == "thinking":
                blocks.append({"type": "thinking", "text": b.get("thinking", "")})
            elif t == "tool_use":
                blocks.append({
                    "type": "tool_use",
                    "text": json.dumps({"name": b.get("name"), "input": b.get("input")}, ensure_ascii=False, indent=2),
                })
            elif t == "tool_result":
                rc = b.get("content")
                if isinstance(rc, list):
                    rc = "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in rc)
                blocks.append({"type": "tool_result", "text": str(rc), "is_error": bool(b.get("is_error"))})
            else:
                blocks.append({"type": t or "?", "text": json.dumps(b, ensure_ascii=False)})
    return {"role": role, "blocks": blocks}


def _parse_input(rec: dict[str, Any]) -> dict[str, Any]:
    oq = rec.get("original_request") or {}
    system = oq.get("system")
    if isinstance(system, list):
        system = "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    elif not isinstance(system, str):
        system = ""
    tools = oq.get("tools") or []
    msgs = oq.get("messages") or []
    return {
        "num_messages": len(msgs),
        "num_tools": len(tools),
        "system": system,
        "tool_names": [t.get("name") for t in tools if isinstance(t, dict)],
        "messages": [_normalize_message(m) for m in msgs],
    }


def _parse_output(rec: dict[str, Any]) -> dict[str, Any]:
    if rec.get("error"):
        return {"error": rec["error"]}
    # Prefer the raw upstream OpenAI-chat shape (clean reasoning_content).
    ur = rec.get("upstream_response") or {}
    choices = ur.get("choices") if isinstance(ur, dict) else None
    if choices:
        msg = (choices[0] or {}).get("message") or {}
        return {
            "reasoning": msg.get("reasoning_content") or "",
            "text": msg.get("content") or "",
            "tool_calls": [
                {"name": (tc.get("function") or {}).get("name"),
                 "input": (tc.get("function") or {}).get("arguments")}
                for tc in (msg.get("tool_calls") or [])
            ],
            "stop_reason": (choices[0] or {}).get("finish_reason"),
            "raw": msg,  # exact upstream assistant message (tool args are raw JSON strings)
        }
    # Fallback: Anthropic returned_response content blocks.
    rr = rec.get("returned_response") or {}
    text, reasoning, tools = "", "", []
    for b in rr.get("content") or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            text += b.get("text", "")
        elif t == "thinking":
            reasoning += b.get("thinking", "")
        elif t == "tool_use":
            tools.append({"name": b.get("name"), "input": b.get("input")})
    return {"reasoning": reasoning, "text": text, "tool_calls": tools,
            "stop_reason": rr.get("stop_reason"), "raw": rr}


def _parse_output_lean(rec: dict[str, Any]) -> dict[str, Any]:
    """Output for SSE — drop ``raw`` (duplicates text / tool_calls)."""
    out = _parse_output(rec)
    out.pop("raw", None)
    return out


def _same_message(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("role") != b.get("role"):
        return False
    ab = a.get("blocks") or []
    bb = b.get("blocks") or []
    if len(ab) != len(bb):
        return False
    for x, y in zip(ab, bb):
        if x.get("type") != y.get("type") or x.get("text") != y.get("text"):
            return False
        if bool(x.get("is_error")) != bool(y.get("is_error")):
            return False
    return True


def _without_injected_context(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("role") != "user":
        return message
    blocks = []
    for block in message.get("blocks") or []:
        text = str(block.get("text") or "").strip()
        if any(text.startswith(p) for p in _INJECTED_USER_PREFIXES):
            continue
        blocks.append(block)
    out = dict(message)
    out["blocks"] = blocks
    return out


def _turn_input(previous: list[dict[str, Any]], messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Incremental delta used by chain view (matches former front-end logic)."""
    common = 0
    while (
        common < len(previous)
        and common < len(messages)
        and _same_message(previous[common], messages[common])
    ):
        common += 1
    delta = [m for m in messages[common:] if m.get("role") != "assistant"]
    if not previous:
        delta = [
            m for m in (_without_injected_context(m) for m in delta)
            if m.get("blocks")
        ]
    return delta


def _compact_model_event(
    agent: str,
    index: int,
    rec: dict[str, Any],
    previous_messages: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build a wire-efficient model SSE payload; return (event, new_previous)."""
    parsed = _parse_input(rec)
    messages = list(parsed.get("messages") or [])
    turn = _turn_input(previous_messages, messages)
    # Full conversation + tool schemas dominate payload size; ship meta + delta only.
    # System prompt only on the first call for that agent (rarely needed later).
    payload = {
        "t": "model",
        "agent": agent,
        "ts": rec.get("ts"),
        "index": index,
        "api_type": rec.get("api_type"),
        "input": {
            "num_messages": parsed.get("num_messages", 0),
            "num_tools": parsed.get("num_tools", 0),
            "tool_names": parsed.get("tool_names") or [],
            "system": (parsed.get("system") or "") if index == 0 else "",
        },
        "turn_input": turn,
        "output": _parse_output_lean(rec),
    }
    return payload, messages


def build_app(runs_dir: Path) -> FastAPI:
    runs_dir = Path(runs_dir)

    app = FastAPI(title="0号机 Trace Viewer")
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        """Run list — pick a task, then open its Segment Inspector."""
        return FileResponse(_STATIC_DIR / "list.html")

    @app.get("/trace")
    async def trace_view() -> FileResponse:
        """Detail viewer for one run (``?task_id=...``)."""
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/api/runs")
    async def api_runs() -> JSONResponse:
        return JSONResponse({"runs": _list_runs(runs_dir)})

    @app.get("/latest")
    async def latest() -> JSONResponse:
        return JSONResponse({"task_id": _latest_task_id(runs_dir)})

    @app.get("/stream/{task_id}")
    async def stream(task_id: str, request: Request) -> StreamingResponse:
        sources = _stream_sources(task_id, runs_dir)

        async def gen():
            pos: dict[Path, int] = {p: 0 for _, _, p in sources}
            idx = {"researcher": 0, "labwright": 0, "teacher": 0}
            prev_msgs: dict[str, list[dict[str, Any]]] = {
                "researcher": [], "labwright": [], "teacher": [],
            }
            first = True
            while True:
                if await request.is_disconnected():
                    break
                batch: list[tuple[float, dict[str, Any]]] = []
                for kind, agent, path in sources:
                    if not path.exists():
                        continue
                    try:
                        with path.open("r", encoding="utf-8") as f:
                            f.seek(pos[path])
                            chunk = f.read()
                            pos[path] = f.tell()
                    except OSError:
                        continue
                    for line in chunk.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if kind == "orch":
                            # Skip turn:* mirrors of capgw model I/O (noise).
                            if str(rec.get("event") or "").startswith("turn:"):
                                continue
                            batch.append((rec.get("ts", 0), {"t": "orch", **rec}))
                        else:
                            i = idx[agent]
                            idx[agent] += 1
                            payload, prev_msgs[agent] = _compact_model_event(
                                agent, i, rec, prev_msgs[agent],
                            )
                            batch.append((rec.get("ts", 0), payload))
                if first or len(batch) > 1:
                    batch.sort(key=lambda x: x[0])  # keep orch + model files chronologically aligned
                    first = False
                for _, payload in batch:
                    yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.4)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


class TraceViewerServer:
    """Runs the dashboard in a background uvicorn thread."""

    def __init__(self, runs_dir: Path, *, host: str = "0.0.0.0", port: int = 8901):
        self._runs_dir = Path(runs_dir)
        self._host = host
        self._port = port
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        shown = "127.0.0.1" if self._host in ("0.0.0.0", "") else self._host
        return f"http://{shown}:{self._port}"

    def start(self) -> str:
        if self._thread is not None:
            return self.url
        config = uvicorn.Config(
            build_app(self._runs_dir),
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None  # type: ignore[assignment]
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="trace-viewer")
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
