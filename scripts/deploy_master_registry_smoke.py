"""Safe Deploy Master configuration smoke; builds require an explicit write gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import httpx

from zero.config import Config
from zero.resources.deploy_master import BuildToolRequest, DeployMasterClient


async def run(args) -> None:
    config = Config()
    if not config.deploy_master_base_url:
        raise SystemExit("DEPLOY_MASTER_BASE_URL is not configured")
    if not args.github_url:
        async with httpx.AsyncClient(
            base_url=config.deploy_master_base_url.rstrip("/"), timeout=30
        ) as client:
            health_response, stats_response, tasks_response = await asyncio.gather(
                client.get("/health"),
                client.get("/api/v1/stats"),
                client.get("/api/v1/tasks"),
            )
        health_response.raise_for_status()
        stats_response.raise_for_status()
        tasks_response.raise_for_status()
        health = health_response.json()
        stats = stats_response.json()
        tasks = tasks_response.json()
        task_items = (tasks.get("data") or tasks).get("tasks", []) if isinstance(tasks, dict) else tasks
        print(json.dumps({
            "ok": True,
            "mode": "read-only",
            "health": health,
            "task_stats": (stats.get("data") or stats).get("tasks", {}),
            "recent_task_count": len(task_items),
            "note": "pass --github-url with ZERO_ALLOW_REGISTRY_SMOKE_WRITE=1 to submit a build",
        }, ensure_ascii=False))
        return
    if os.environ.get("ZERO_ALLOW_REGISTRY_SMOKE_WRITE") != "1":
        raise SystemExit("refusing build: set ZERO_ALLOW_REGISTRY_SMOKE_WRITE=1 explicitly")
    client = DeployMasterClient(
        config.deploy_master_base_url,
        poll_interval=config.deploy_master_poll_interval_sec,
        deadline=config.deploy_master_build_deadline_sec,
        auth_token=config.deploy_master_auth_token,
    )
    try:
        artifact = await client.build(BuildToolRequest(
            github_url=args.github_url,
            build_instructions=args.build_instructions,
            verify_commands=args.verify_command,
        ))
    finally:
        await client.aclose()
    print(json.dumps(
        {"ok": True, "artifact": artifact.model_dump(mode="json")},
        ensure_ascii=False,
    ))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-url")
    parser.add_argument("--build-instructions", default="Build the repository's documented CLI")
    parser.add_argument("--verify-command", action="append", default=[])
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
