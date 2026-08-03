"""Delivery verification (doc section 14).

READY means "the Researcher can actually use it", not "install returned 0".
Each resource kind has its own real check, run inside the sandbox.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from zero.sandbox.manager import SandboxManager

# distribution name -> import name for the common mismatches.
_IMPORT_NAME = {
    "scikit-learn": "sklearn",
    "pillow": "PIL",
    "pyyaml": "yaml",
    "opencv-python": "cv2",
    "beautifulsoup4": "bs4",
    "python-dateutil": "dateutil",
    "huggingface-hub": "huggingface_hub",
    "hf-transfer": "hf_transfer",
    "pytorch": "torch",
}

# First character that starts a version specifier / extras / marker / URL ref.
_SPEC_BOUNDARY = re.compile(r"[<>=!~;\[@\s]")


def normalize_dist(item: Any) -> str:
    """Reduce a package entry to its bare distribution name.

    Agents pass packages in several natural shapes — plain ``"numpy"``,
    pinned ``"scikit-learn==1.9.0"``, or a dict ``{"name": "numpy", ...}``.
    verify/publish only care about the distribution name, so strip any
    version specifier, extras, or environment marker.
    """
    if isinstance(item, dict):
        raw = item.get("name") or item.get("package") or item.get("dist") or ""
    else:
        raw = str(item)
    return _SPEC_BOUNDARY.split(raw.strip(), 1)[0].strip()


def import_name(dist: str) -> str:
    key = normalize_dist(dist).lower()
    return _IMPORT_NAME.get(key, key.replace("-", "_"))


@dataclass
class PackageCheck:
    name: str
    ok: bool
    version: str = ""
    error: str = ""


class Verifier:
    def __init__(self, manager: SandboxManager):
        self._mgr = manager

    def verify_package(self, sandbox_id: str, dist_name: str) -> PackageCheck:
        dist = normalize_dist(dist_name)
        mod = import_name(dist)
        code = (
            f"import importlib, importlib.metadata as m; importlib.import_module('{mod}'); "
            f"print(m.version('{dist}'))"
        )
        r = self._mgr.exec(sandbox_id, f'python -c "{code}"', timeout=120)
        if r.ok:
            return PackageCheck(dist, True, version=r.stdout.strip())
        # Fall back to import-only (version metadata may be absent).
        r2 = self._mgr.exec(sandbox_id, f"python -c \"import {mod}; print(getattr({mod},'__version__',''))\"", timeout=120)
        if r2.ok:
            return PackageCheck(dist, True, version=r2.stdout.strip())
        return PackageCheck(dist, False, error=(r.stderr or r2.stderr).strip()[:500])

    def verify_tool(self, sandbox_id: str, command: str) -> tuple[bool, str]:
        r = self._mgr.exec(sandbox_id, f"{command} --version", timeout=60)
        out = (r.stdout or r.stderr).strip()
        return r.ok, out[:300]

    def verify_model(self, sandbox_id: str, path: str) -> tuple[bool, str]:
        r = self._mgr.exec(sandbox_id, f"test -d {path!r} && ls -A {path!r} | head -1", timeout=60)
        return (r.ok and bool(r.stdout.strip())), r.stdout.strip()[:200]

    def verify_dataset(self, sandbox_id: str, path: str) -> tuple[bool, str]:
        r = self._mgr.exec(sandbox_id, f"test -e {path!r} && (ls -A {path!r} 2>/dev/null | head -1 || echo present)", timeout=60)
        return (r.ok and bool(r.stdout.strip())), r.stdout.strip()[:200]
