"""Two-layer trace (doc section 27).

Layer 1 (model-call trajectories incl. chain-of-thought) is produced by capgw
per session file. Layer 2 (structured orchestration events) is produced here.
Both are correlated by task_id / session_id / sandbox_id.
"""

from zero.trace.events import TraceWriter
from zero.trace.correlate import correlate_traces

__all__ = ["TraceWriter", "correlate_traces"]
