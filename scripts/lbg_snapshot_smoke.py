"""Live smoke test for ``LbgProvider.snapshot()`` / ``wait_for_image()``.

Companion to ``lbg_smoke.py`` (exec/put/get/info); this one exercises the
image-commit path specifically. Spins up the smallest possible Bohrium
sandbox, submits a real ``lbg sdbx image commit``, polls it to a terminal
state, prints the result, and destroys the sandbox in a ``finally`` so it
never leaks a billed resource. Costs a small amount of Bohrium credit
(sandbox minutes + one committed container image, which the CLI cannot
delete -- clean it up from the Bohrium console if desired). Run manually:

    ZERO_LBG_PROJECT_ID=<id> python3 scripts/lbg_snapshot_smoke.py

Requires ``bohrium_key`` resolvable via ``Config`` (env var or ``.env``) and
``ZERO_LBG_PROJECT_ID`` (the backend requires a project id to bill image
storage against -- without it ``snapshot()`` degrades to a pip-freeze digest).
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zero.config import Config
from zero.sandbox.base import SandboxSpec
from zero.sandbox.lbg_provider import LbgProvider


def main() -> int:
    cfg = Config()
    if not cfg.bohrium_key:
        print("FAIL: no bohrium_key resolvable (env or .env)")
        return 1
    if not cfg.lbg_project_id:
        print("FAIL: ZERO_LBG_PROJECT_ID not set")
        return 1

    provider = LbgProvider(cfg)
    sandbox_id = f"snaptest-{uuid.uuid4().hex[:8]}"
    spec = SandboxSpec(
        task_id="lbg-snapshot-live-test",
        sandbox_id=sandbox_id,
        base_image="",  # let the provider auto-pick a small Basic Image
        workspace_host_path="/tmp",
        cpu_count=2,
        memory_gb=4,
        gpu_count=0,
        python_version="3.11",
    )

    print(f"[1/4] creating sandbox ({sandbox_id}) ...")
    t0 = time.time()
    handle = provider.create_sandbox(spec)
    print(f"      ok in {time.time() - t0:.0f}s -> rid={provider._rid(sandbox_id)}")

    try:
        print("[2/4] submitting snapshot (lbg sdbx image commit) ...")
        t0 = time.time()
        digest = provider.snapshot(sandbox_id)
        print(f"      ok in {time.time() - t0:.0f}s -> digest={digest}")

        if not digest.startswith("lbg:commit:"):
            print(f"FAIL: expected an lbg:commit:<id> digest, got {digest!r} "
                  "(fell back to pip-freeze digest -- commit call failed)")
            return 1

        commit_id = digest.rsplit(":", 1)[-1]
        print(f"[3/4] polling lbg sdbx image get {commit_id} ...")
        t0 = time.time()
        record = provider.wait_for_image(commit_id, timeout=1500, poll_interval=15)
        print(f"      terminal after {time.time() - t0:.0f}s -> {record}")

        status = record.get("status")
        if status == 2:
            print(f"PASS: image ready -> imageUrl={record.get('imageUrl')}")
            return 0
        print(f"FAIL: commit did not succeed (status={status}, "
              f"errorMsg={record.get('errorMsg') or record.get('statusReason')})")
        return 1
    finally:
        print("[4/4] destroying sandbox ...")
        provider.destroy(sandbox_id)
        print("      done")


if __name__ == "__main__":
    raise SystemExit(main())
