"""Canonical spec hashing for idempotency (doc section 12/16).

``ensure_environment`` is keyed on the normalized hash of the spec so a retry
or a task resume never re-collects the same large resources.
"""

from __future__ import annotations

import hashlib
import json

from zero.protocol.spec import EnvironmentSpec


def spec_hash(spec: EnvironmentSpec) -> str:
    payload = spec.model_dump(mode="json", exclude_none=True)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
