"""Live smoke test for LbgProvider — creates one cheap CPU sandbox, exercises
exec/put/get/info, then always destroys it. Costs a small amount of Bohrium
credit; safe to delete after validation.
"""

from __future__ import annotations

import time

from zero.config import Config
from zero.sandbox.base import SandboxSpec
from zero.sandbox.lbg_provider import LbgProvider


def main() -> None:
    cfg = Config(sandbox_backend="lbg")
    prov = LbgProvider(cfg)
    spec = SandboxSpec(
        task_id="smoke",
        sandbox_id="sandbox-smoke-v1",
        base_image="",              # discover a Basic Image via bohr
        workspace_host_path="/tmp/zero-smoke-ws",
        cpu_count=1,
        memory_gb=2,
        gpu_count=0,
        python_version="3.10",
    )

    t0 = time.time()
    print("[create] launching sandbox ...", flush=True)
    handle = prov.create_sandbox(spec)
    print(f"[create] handle={handle} remote={prov._remote} ({time.time()-t0:.1f}s)", flush=True)
    try:
        r = prov.exec("sandbox-smoke-v1", "echo hello-from-sandbox && python3 --version", timeout=60)
        print(f"[exec] ok={r.ok} code={r.exit_code}\n  stdout={r.stdout!r}\n  stderr={r.stderr!r}", flush=True)

        prov.put_file("sandbox-smoke-v1", "conclusion.md", b"# smoke\nroundtrip ok\n")
        got = prov.get_file("sandbox-smoke-v1", "conclusion.md")
        print(f"[files] roundtrip got={got!r}", flush=True)

        info = prov.info("sandbox-smoke-v1")
        print(f"[info] {info}", flush=True)
    finally:
        print("[destroy] killing sandbox ...", flush=True)
        prov.destroy("sandbox-smoke-v1")
        print(f"[destroy] done (total {time.time()-t0:.1f}s)", flush=True)


if __name__ == "__main__":
    main()
