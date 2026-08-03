"""Pull a finished task's output files off the (still-alive) lbg sandbox into a
local dir, ready for `playground submit --outputs`. Reads the Bohrium key via
Config (never echoed). Usage: python scripts/pull_outputs.py [remote_dir] [local_dir]"""

from __future__ import annotations

import sys

from zero.config import Config
from zero.sandbox.lbg_provider import LbgProvider, _as_list, _first, _loads

REMOTE_DIR = sys.argv[1] if len(sys.argv) > 1 else "/home/user/app/outputs"
LOCAL_DIR = sys.argv[2] if len(sys.argv) > 2 else "/personal/zero/submit_outputs"


def main() -> None:
    prov = LbgProvider(Config(sandbox_backend="lbg"))
    proc = prov._lbg(["sdbx", "list", "--json"], timeout=60)
    sandboxes = _as_list(_loads(prov._text(proc)))
    print(f"live sandboxes: {len(sandboxes)}")
    for sb in sandboxes:
        rid = _first(sb, "sandboxID", "sandbox_id", "sandboxId", "id")
        tpl = _first(sb, "templateID", "templateId", "template", "templateName")
        state = _first(sb, "state", "status")
        age = _first(sb, "age_seconds", "age")
        print(f"  - {rid}  tpl={tpl}  state={state}  age={age}")

    import os
    os.makedirs(LOCAL_DIR, exist_ok=True)
    for sb in sandboxes:
        rid = _first(sb, "sandboxID", "sandbox_id", "sandboxId", "id")
        if not rid:
            continue
        ls = prov._lbg(["sdbx", "exec", "--timeout", "30", "--json", str(rid),
                        f"ls -la {REMOTE_DIR} 2>/dev/null || echo __MISSING__"], timeout=60)
        out = prov._text(ls)
        if "__MISSING__" in out or not out.strip():
            print(f"[{rid}] no {REMOTE_DIR}")
            continue
        print(f"[{rid}] found {REMOTE_DIR}:\n{out[-1500:]}")
        # tar the dir then pull it whole (preserves multiple files).
        prov._lbg(["sdbx", "exec", "--timeout", "60", "--json", str(rid),
                   f"cd {REMOTE_DIR} && tar czf /tmp/outputs.tgz ."], timeout=90)
        dst = os.path.join(LOCAL_DIR, "outputs.tgz")
        rd = prov._lbg(["sdbx", "files", "read", str(rid), "/tmp/outputs.tgz",
                        "--format", "bytes", "--output", dst], timeout=120)
        if rd.returncode == 0 and os.path.getsize(dst) > 0:
            print(f"[{rid}] pulled outputs -> {dst} ({os.path.getsize(dst)} bytes)")
            import tarfile
            with tarfile.open(dst) as t:
                t.extractall(LOCAL_DIR)
            os.remove(dst)
            print("extracted:", sorted(os.listdir(LOCAL_DIR)))
            return
        print(f"[{rid}] read failed: {prov._text(rd, 'stderr')[:300]}")
    print("no outputs pulled")


if __name__ == "__main__":
    main()
