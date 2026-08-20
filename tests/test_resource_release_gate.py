from __future__ import annotations

import unittest

from zero.labwright.agent import _ALLOWED_TOOLS as LABWRIGHT_TOOLS
from zero.labwright.tools import _verification_failed
from zero.protocol.manifest import VerificationReport
from zero.researcher.agent import _ALLOWED_TOOLS as RESEARCHER_TOOLS
from zero.teacher.agent import _ALLOWED_TOOLS as TEACHER_TOOLS


class ReleaseVerificationTests(unittest.TestCase):
    def test_every_resource_verification_can_block_release(self):
        fields = ("package_import", "tool_healthcheck", "model_load", "dataset_read")
        for field in fields:
            with self.subTest(field=field):
                report = VerificationReport()
                setattr(report, field, "failed")
                self.assertTrue(_verification_failed(report))

    def test_skipped_or_passed_checks_do_not_block_release(self):
        report = VerificationReport(
            package_import="passed",
            tool_healthcheck="passed",
            model_load="skipped",
            dataset_read="skipped",
        )
        self.assertFalse(_verification_failed(report))


class AgentBoundaryTests(unittest.TestCase):
    def test_registry_and_build_tools_remain_labwright_private(self):
        private_tools = {
            "mcp__labenv__search_resource",
            "mcp__labenv__publish_resource",
            "mcp__labenv__build_tool_resource",
        }
        self.assertTrue(private_tools.issubset(LABWRIGHT_TOOLS))
        self.assertTrue(private_tools.isdisjoint(RESEARCHER_TOOLS))
        self.assertTrue(private_tools.isdisjoint(TEACHER_TOOLS))


if __name__ == "__main__":
    unittest.main()
