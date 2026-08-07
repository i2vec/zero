"""``zero run <task-package-dir>`` entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from zero.config import get_config
from zero.orchestrator.orchestrator import Orchestrator


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zero", description="0号机: three-agent scientific experiment system.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser(
        "run",
        help="Run a Harbor-style task package end to end.",
        description=(
            "Pass a task package directory containing instruction.md "
            "(Researcher prompt) and preferably tests/ (grader). "
            "Teacher can Read tests/ during completion review; paper/ is "
            "auto-seeded as hints when present."
        ),
    )
    run.add_argument(
        "task",
        help="Path to a task package directory (must contain instruction.md).",
    )
    run.add_argument("--max-turns", type=int, default=1000)
    run.add_argument(
        "--run-name",
        default=None,
        help="Custom folder name for this run under runs/ "
             "(default: auto task-<date>-<hex>). Must be unique.",
    )
    run.add_argument("--no-capgw", action="store_true", help="Do not manage capgw (assume it is already running).")
    run.add_argument("--trace-ui", action="store_true",
                     help="Also launch the live trace viewer for this run "
                          "(off by default; use `zero viewer` to replay traces separately).")
    run.add_argument("--no-export", action="store_true",
                     help="Skip pulling deliverables into runs/<id>/deliverables/ (traces still write live).")
    run.add_argument("--task-key", default=None,
                     help="Identifies the *task* (e.g. a challenge id) for Teacher asks / "
                          "amendment metadata; does not locate hint files. Defaults to the "
                          "resolved run name.")
    run.add_argument(
        "--hints",
        default=None,
        help="Seed Teacher hint bank: path to a .md file or a directory of .md files. "
             "If omitted, uses <package>/paper/paper.md or <package>/paper/*.md when present.",
    )
    run.add_argument("--no-teacher", action="store_true",
                     help="Disable the Teacher agent for this run (default ZERO_TEACHER_ENABLED).")
    run.add_argument(
        "--task-dir",
        default=None,
        help=argparse.SUPPRESS,  # deprecated alias; positional task is the package
    )

    viewer = sub.add_parser("viewer", help="Serve the trace viewer over recorded traces (separate from runs).")
    viewer.add_argument("--host", default=None, help="Bind host (default ZERO_TRACE_UI_HOST).")
    viewer.add_argument("--port", type=int, default=None, help="Bind port (default ZERO_TRACE_UI_PORT).")

    skills = sub.add_parser("skills", help="Review and publish agent-proposed reusable Skills.")
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    skills_list = skills_sub.add_parser("list", help="List staged Skill candidates.")
    skills_list.add_argument("--role", choices=("researcher", "labwright"), default=None)
    skills_validate = skills_sub.add_parser("validate", help="Validate one staged candidate.")
    skills_validate.add_argument("role", choices=("researcher", "labwright"))
    skills_validate.add_argument("candidate_id")
    skills_publish = skills_sub.add_parser("publish", help="Publish a validated candidate for future sessions.")
    skills_publish.add_argument("role", choices=("researcher", "labwright"))
    skills_publish.add_argument("candidate_id")
    skills_reject = skills_sub.add_parser("reject", help="Reject a staged candidate.")
    skills_reject.add_argument("role", choices=("researcher", "labwright"))
    skills_reject.add_argument("candidate_id")
    skills_reject.add_argument("--reason", required=True)

    sub.add_parser("info", help="Print resolved configuration.")
    return parser


async def _run(args: argparse.Namespace) -> int:
    from zero.grading.task_package import default_hints_path, resolve_task_package, tests_dir

    try:
        # Deprecated --task-dir: if set, it is the package directory.
        package_arg = getattr(args, "task_dir", None) or args.task
        resolved = resolve_task_package(package_arg)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    prompt = resolved.prompt
    task_package = resolved.package
    print(f"[run] task package: {task_package}")
    print(f"[run] instruction: {resolved.instruction_path}  ({len(prompt)} chars)")
    tests = tests_dir(task_package)
    if tests is not None:
        print(f"[run] tests: {tests}")
    else:
        print("[run] tests: (none — grading/Teacher package review limited)")

    hints = args.hints
    if not hints:
        auto = default_hints_path(task_package)
        if auto is not None:
            hints = str(auto)
            print(f"[run] hints (auto): {auto}")

    orch = Orchestrator(manage_capgw=not args.no_capgw, serve_trace=args.trace_ui)
    try:
        result = await orch.run_task(
            prompt, max_turns=args.max_turns, run_name=args.run_name, export=not args.no_export,
            task_key=args.task_key, teacher_enabled=(False if args.no_teacher else None),
            hints=hints,
            task_package=task_package,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        orch.close()

    print("=" * 70)
    print(f"task_id : {result.task_id}")
    print(f"status  : {result.status}")
    print(f"backend : {result.backend}")
    print(f"sandbox : {result.sandbox_ids}")
    print(f"workspace: {result.workspace}")
    if result.export_dir:
        print(f"export  : {result.export_dir}  (deliverables/ + trace/ + teacher/)")
    if result.grading:
        print(f"grading : status={result.grading.get('status')} score={result.grading.get('score')}")
    if result.optimized_task:
        print(f"optimized_task: {result.optimized_task}")
    if result.environment.get("environment_md"):
        print(f"environment.md: {result.environment.get('environment_md')}")
    print(f"hook interceptions: {result.interceptions}")
    if result.teacher_stats:
        print(f"teacher : {json.dumps(result.teacher_stats, ensure_ascii=False)}")
    print("-" * 70)
    print("最终结论:\n" + (result.conclusion or result.final_text or "(空)"))
    print("-" * 70)
    print("trace index:\n" + json.dumps(result.trace_index, ensure_ascii=False, indent=2))

    _keep_viewer_alive(orch)
    return 0 if result.status == "task_completed" else 1


def _keep_viewer_alive(orch: Orchestrator) -> None:
    """Keep the trace viewer serving after the run so it can be inspected/replayed."""
    if orch.viewer_url is None:
        return
    print("-" * 70)
    print(f"[trace] 查看器仍在运行: {orch.viewer_url}  （按 Ctrl-C 退出）")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        orch.stop_viewer()


def _run_viewer(args: argparse.Namespace) -> int:
    """Standalone trace viewer: replay recorded traces without running a task."""
    from zero.trace.server import TraceViewerServer

    cfg = get_config()
    viewer = TraceViewerServer(
        cfg.runs_dir,
        host=args.host or cfg.trace_ui_host,
        port=args.port or cfg.trace_ui_port,
    )
    url = viewer.start()
    print(f"[trace] 轨迹查看器: {url}  （读取 {cfg.runs_dir}/<run>/trace/，按 Ctrl-C 退出）")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        viewer.stop()
    return 0


def _skills(args: argparse.Namespace) -> int:
    from zero.skills.candidates import SkillCandidates

    candidates = SkillCandidates(get_config())
    try:
        if args.skills_command == "list":
            payload = candidates.list(args.role)
        elif args.skills_command == "validate":
            payload = candidates.validate(args.role, args.candidate_id)
        elif args.skills_command == "publish":
            payload = {"published_path": str(candidates.publish(args.role, args.candidate_id))}
        else:
            candidates.reject(args.role, args.candidate_id, args.reason)
            payload = {"ok": True, "status": "rejected"}
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "info":
        cfg = get_config()
        print(json.dumps({
            "root": str(cfg.root),
            "capgw_url": cfg.capgw_url,
            "backend": cfg.resolved_backend(),
            "runs_dir": str(cfg.runs_dir),
            "experience_dir": str(cfg.experience_dir),
            "teacher_enabled": cfg.teacher_enabled,
            "teacher_max_asks": cfg.teacher_max_asks,
            "note": "per-run state lives under runs/<id>/ (resources, sandboxes, logs, meta)",
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "run":
        return asyncio.run(_run(args))
    if args.command == "viewer":
        return _run_viewer(args)
    if args.command == "skills":
        return _skills(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
