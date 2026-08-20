#!/usr/bin/env python3
"""Backfill assets.json for already finalized Zero runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from zero.export import _write_asset_summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs_dir", type=Path)
    args = parser.parse_args()
    written = 0
    skipped = 0
    for run_json in sorted(args.runs_dir.glob("*/run.json")):
        run_dir = run_json.parent
        try:
            run = json.loads(run_json.read_text(encoding="utf-8"))
            if run.get("status") != "task_completed":
                skipped += 1
                continue
            env_path = run_dir / "environment.json"
            environment = json.loads(env_path.read_text(encoding="utf-8")) if env_path.is_file() else {}
            assets = _write_asset_summary(run_dir, environment)
            if assets is None:
                skipped += 1
                continue
            run.setdefault("artifacts", {})["assets"] = str(assets)
            tmp = run_json.with_name(".run.json.assets.tmp")
            tmp.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(run_json)
            written += 1
        except (OSError, json.JSONDecodeError):
            skipped += 1
    print(json.dumps({"written": written, "skipped": skipped}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
