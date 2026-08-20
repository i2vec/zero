"""Read-only Literature Sage connectivity and Search+Detail smoke test."""

from __future__ import annotations

import argparse
import asyncio
import json

from zero.config import Config
from zero.protocol.resources import ResourceKind
from zero.resources.literature_sage import LiteratureSageClient
from zero.resources.registry import ResourceRegistry


async def run(kind: ResourceKind, query: str) -> None:
    config = Config()
    client = LiteratureSageClient(
        config.literature_sage_base_url,
        timeout=config.literature_sage_timeout_sec,
        max_connections=1,
        max_retries=config.literature_sage_max_retries,
        auth_token=config.literature_sage_auth_token,
    )
    try:
        health = await client.health()
        candidates = await ResourceRegistry(client).search(kind=kind, text=query, limit=3)
    finally:
        await client.aclose()
    print(json.dumps({
        "ok": True,
        "health": health,
        "kind": kind.value,
        "query": query,
        "candidate_count": len(candidates),
        "keys": [item.resource_unique_key for item in candidates],
    }, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=[item.value for item in ResourceKind], default="tool")
    parser.add_argument("--query", default="torchvision")
    args = parser.parse_args()
    asyncio.run(run(ResourceKind(args.kind), args.query))


if __name__ == "__main__":
    main()
