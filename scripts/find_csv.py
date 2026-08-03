from __future__ import annotations

from zero.config import Config
from zero.sandbox.lbg_provider import LbgProvider, _as_list, _first, _loads

prov = LbgProvider(Config(sandbox_backend="lbg"))
proc = prov._lbg(["sdbx", "list", "--json"], timeout=60)
for sb in _as_list(_loads(prov._text(proc))):
    rid = _first(sb, "sandboxID", "sandbox_id", "id")
    if not rid:
        continue
    cmd = ("echo '--- /workspace ---'; ls -a /workspace 2>/dev/null; "
           "echo '--- find csv ---'; find / -name 'optimization_results.csv' 2>/dev/null | head; "
           "echo '--- find conclusion ---'; find / -name 'conclusion.md' 2>/dev/null | head")
    r = prov._lbg(["sdbx", "exec", "--timeout", "40", "--json", str(rid), cmd], timeout=70)
    print(f"\n===== {rid} =====")
    print(prov._text(r)[-1800:])
