"""Teacher: answers the Researcher's questions, and fixes the task statement.

A persistent Claude Code agent holding this task's human-written hint bank. On
every ask it decides whether the *scientific* task statement is defective (answer with a
correction that outlives the run) or the work is merely hard (answer with a
method-level hint). Exposed to the Researcher as a single blocking MCP tool.
"""

from zero.teacher.service import TeacherService

__all__ = ["TeacherService"]
