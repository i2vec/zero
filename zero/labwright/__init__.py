"""Labwright: turns a declarative EnvironmentSpec into a verified sandbox.

A persistent Claude Code agent owns the full provisioning loop (collect,
install, verify, repair). Exposed to the Researcher as an in-process MCP
server; Labwright itself uses labenv tools to act on sandboxes.
"""

from zero.labwright.service import LabwrightService

__all__ = ["LabwrightService"]
