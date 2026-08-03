"""Offline smoke for the Researcher experience library (no model).

Exercises: empty search → record → search hit → get → duplicate reject →
secret/path/task-id reject → audit line under runs/<id>/meta/.

    conda run -n zero python scripts/experience_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp.types as mcp_types  # noqa: E402

from zero.config import Config  # noqa: E402
from zero.experience.mcp_server import build_experience_server  # noqa: E402
from zero.experience.store import ExperienceStore  # noqa: E402


def _ok(label: str) -> None:
    print(f"  ok  · {label}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  FAIL· {label}  {detail}")
    raise SystemExit(1)


async def _call_tool(server_cfg: dict, name: str, arguments: dict) -> dict:
    handler = server_cfg["instance"].request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )
    result = await handler(req)
    text = result.root.content[0].text  # type: ignore[union-attr]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        _fail(f"tool {name} returned non-JSON", text)
        return {}


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zero-experience-smoke-"))
    try:
        print("== experience store + MCP ==")
        cfg = Config(root=tmp)
        cfg.ensure_dirs()
        store = ExperienceStore(cfg, source_run="smoke-run-1")
        server = build_experience_server(store)

        empty = await _call_tool(server, "search_experience", {
            "query": "rdf", "tags": [], "limit": 8,
        })
        if not empty.get("ok") or empty.get("count") != 0:
            _fail("empty bank search should return count=0", str(empty))
        _ok("search on empty bank")

        lesson = (
            "When estimating a radial distribution function from Monte Carlo, "
            "normalize histograms by shell volume and density; otherwise the "
            "first peak height is systematically wrong across system sizes."
        )
        recorded = await _call_tool(server, "record_experience", {
            "title": "RDF shell-volume normalization",
            "tags": ["rdf", "monte-carlo", "analysis"],
            "trigger": "Computing g(r) or comparing RDF peaks across box sizes",
            "lesson": lesson,
            "avoid": "Comparing raw bin counts without volume weights",
            "confidence": "high",
        })
        if not recorded.get("ok") or recorded.get("id") != "rdf-shell-volume-normalization":
            _fail("record_experience should succeed with slug id", str(recorded))
        _ok("record_experience writes entry")

        entry_path = Path(recorded["path"])
        if not entry_path.is_file():
            _fail("entry markdown missing", str(entry_path))
        _ok("entries/<id>.md created")

        hits = await _call_tool(server, "search_experience", {
            "query": "radial", "tags": ["rdf"], "limit": 5,
        })
        if hits.get("count", 0) < 1:
            _fail("search should find the new entry", str(hits))
        if hits["hits"][0].get("id") != "rdf-shell-volume-normalization":
            _fail("top hit should be the recorded id", str(hits))
        _ok("search finds recorded experience")

        got = await _call_tool(server, "get_experience", {
            "id": "rdf-shell-volume-normalization",
        })
        if not got.get("ok") or "shell volume" not in (got.get("entry") or {}).get("body", ""):
            _fail("get_experience should return full body", str(got))
        _ok("get_experience returns body")

        dup = await _call_tool(server, "record_experience", {
            "title": "RDF shell-volume normalization",
            "tags": ["rdf"],
            "trigger": "same trigger text for duplicate check path",
            "lesson": lesson,
            "avoid": "",
            "confidence": "medium",
        })
        if dup.get("ok") or "already exists" not in str(dup.get("error", "")):
            _fail("duplicate id should be rejected", str(dup))
        _ok("duplicate id rejected")

        bad_secret = await _call_tool(server, "record_experience", {
            "title": "leaky note",
            "tags": ["x"],
            "trigger": "never store credentials in the bank",
            "lesson": "Do not put api_key=sk-abcdefghijklmnopqrstuvwxyz into experience notes ever.",
            "avoid": "",
            "confidence": "low",
        })
        if bad_secret.get("ok"):
            _fail("secret-like content should be rejected", str(bad_secret))
        _ok("secret-like content rejected")

        bad_path = await _call_tool(server, "record_experience", {
            "title": "home path leak",
            "tags": ["x"],
            "trigger": "never use absolute homes in shared notes",
            "lesson": "Never tell future runs to read files under /home/someone/data for inputs.",
            "avoid": "",
            "confidence": "low",
        })
        if bad_path.get("ok"):
            _fail("absolute home path should be rejected", str(bad_path))
        _ok("absolute home path rejected")

        bad_task = await _call_tool(server, "record_experience", {
            "title": "task id leak",
            "tags": ["x"],
            "trigger": "when debugging a prior run without leaking ids",
            "lesson": "Do not hard-code sandbox-abc123 or task-20260101-deadbe into methods.",
            "avoid": "",
            "confidence": "low",
        })
        if bad_task.get("ok"):
            _fail("task/sandbox id should be rejected", str(bad_task))
        _ok("task/sandbox id rejected")

        audit = cfg.run_dir("smoke-run-1") / "meta" / "experience_writes.jsonl"
        if not audit.is_file() or "rdf-shell-volume-normalization" not in audit.read_text(encoding="utf-8"):
            _fail("run meta audit log missing", str(audit))
        _ok("runs/<id>/meta/experience_writes.jsonl audited")

        print("\nall experience smoke checks passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
