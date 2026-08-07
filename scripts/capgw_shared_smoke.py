"""Smoke: shared CapgwRunner lease / refcount (no real LLM required)."""

from __future__ import annotations

import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from zero.capgw_runner import CapgwRunner
from zero.config import Config


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/health"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A003
        return


def main() -> None:
    os.environ["ZERO_CAPGW_SHARED"] = "1"
    port = _free_port()
    os.environ["ZERO_CAPGW_PORT"] = str(port)
    os.environ["ZERO_CAPGW_URL"] = f"http://127.0.0.1:{port}"

    # Fake already-up gateway (simulates peer-started capgw).
    httpd = HTTPServer(("127.0.0.1", port), _HealthHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    cfg = Config()
    # Point runs_dir at a temp area via env if Config supports ZERO_ROOT.
    a = CapgwRunner(cfg)
    b = CapgwRunner(cfg)
    assert a.ensure(timeout=5), "A ensure"
    assert b.ensure(timeout=5), "B ensure"
    refs = a._read_refs()
    assert refs == 2, refs
    a.stop()
    assert a._read_refs() == 1
    assert b.is_up()
    b.stop()
    assert b._read_refs() == 0
    # Fake server still up (we didn't own its pid) — that's fine for external serve.
    httpd.shutdown()
    print("OK shared lease refcount")


if __name__ == "__main__":
    main()
