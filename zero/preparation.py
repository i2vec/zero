"""Platform-neutral preflight contract owned by Labwright.

External applications may implement this protocol to stage a task package and
return a verified, concise context for the Researcher.  The contract contains
no platform-specific fields, credentials, or submission logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class PreparedTask:
    """Material prepared before Researcher begins scientific work."""

    context: str
    manifest_path: str = ""
    resource_hints: list[str] = field(default_factory=list)


class ExternalTaskPreparer(Protocol):
    """Host-side preparation adapter invoked through LabwrightService."""

    async def prepare(self, workspace: Path) -> PreparedTask: ...

    async def validate_deliverables(self, run_dir: Path) -> None: ...
