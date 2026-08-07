"""Smoke tests for package lint + live package apply/rollback."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from zero.protocol.teaching import GraderAmendment, TaskAmendment
from zero.teacher.live_package import LivePackageManager
from zero.teacher.package_lint import lint_task_package


def _minimal_pkg(root: Path, *, plan: dict, hidden: dict | None = None) -> Path:
    (root / "instruction.md").write_text("# Task\n\nDo science.\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "checker.py").write_text("print('ok')\n", encoding="utf-8")
    (tests / "verification_plan.json").write_text(
        json.dumps(plan, indent=2) + "\n", encoding="utf-8",
    )
    if hidden is not None:
        hid = tests / "hidden"
        hid.mkdir()
        (hid / "extra.json").write_text(json.dumps(hidden, indent=2) + "\n", encoding="utf-8")
    return root


def test_lint_catches_bound_zero_and_hidden_dict():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "pkg"
        root.mkdir()
        plan = {
            "checks": [
                {
                    "id": "rae",
                    "reference": "paper_value",
                    "topology": "none",
                    "decision": {"curve": "threshold_or_better", "direction": "<=", "bound": 0.0},
                    "config": {"field": "rae"},
                },
                {
                    "id": "extra",
                    "topology": "hidden_case",
                    "decision": {"curve": "decay", "rel_tol": 0.05},
                    "config": {
                        "hidden_bundle": "extra.json",
                        "field": "r_star",
                    },
                },
            ],
            "caps": [],
        }
        hidden = {
            "cases": [
                {"id": "extra_1", "expected": {"r_star": 0.02, "S": 0.0}},
            ]
        }
        _minimal_pkg(root, plan=plan, hidden=hidden)
        report = lint_task_package(root)
        codes = {i.code for i in report.issues if i.severity == "error"}
        assert "bound_zero_threshold" in codes
        assert "hidden_expected_dict" in codes
        assert report.ok is False


def test_lint_catches_case_id_mismatch():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "pkg"
        root.mkdir()
        plan = {
            "checks": [
                {
                    "id": "mp",
                    "topology": "hidden_case",
                    "decision": {"curve": "decay", "rel_tol": 0.25},
                    "config": {
                        "hidden_bundle": "extra.json",
                        "case_field": "case_id",
                        "case_value_field": "rho",
                        "field": "rho",
                    },
                },
            ],
        }
        hidden = {
            "cases": [
                {"case_id": "c1", "rho": 0.5},
            ]
        }
        _minimal_pkg(root, plan=plan, hidden=hidden)
        report = lint_task_package(root)
        codes = {i.code for i in report.issues if i.severity == "error"}
        assert "hidden_id_field" in codes
        assert "hidden_expected_field" in codes


def test_live_package_task_amend_and_rollback_on_bad_grader():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        src = td_path / "src"
        src.mkdir()
        plan = {
            "checks": [
                {
                    "id": "ok",
                    "topology": "none",
                    "reference": "constraint_only",
                    "decision": {"curve": "threshold_or_better", "direction": ">=", "bound": 0},
                    "config": {"field": "n"},
                }
            ],
            "caps": [],
        }
        _minimal_pkg(src, plan=plan)
        run_dir = td_path / "run"
        run_dir.mkdir()
        live = LivePackageManager(run_dir, max_revisions=5)
        live.seed(src, fallback_instruction="# Task\n")
        assert live.revision == 0

        ok = live.apply_task_amendment(
            TaskAmendment(patch="Clarify grid I>=500.", reason="paper sec 5", section="Method")
        )
        assert ok["ok"] is True
        assert live.revision == 1
        assert "ZERO_LIVE_AMENDMENTS_START" in live.instruction_text()

        # Bad grader plan should fail lint and roll back.
        bad_plan = {
            "checks": [
                {
                    "id": "rae",
                    "reference": "paper_value",
                    "topology": "none",
                    "decision": {
                        "curve": "threshold_or_better",
                        "direction": "<=",
                        "bound": 0.0,
                    },
                    "config": {"field": "rae"},
                }
            ],
            "caps": [],
        }
        bad = live.apply_grader_amendment(
            GraderAmendment(
                target="verification_plan.json",
                patch=json.dumps(bad_plan, indent=2),
                reason="bad",
                literature_basis="n/a",
            )
        )
        assert bad["ok"] is False
        assert live.revision == 1  # unchanged
        # Live plan should still be the good one.
        plan_now = json.loads((live.path / "tests" / "verification_plan.json").read_text())
        assert plan_now["checks"][0]["id"] == "ok"


if __name__ == "__main__":
    test_lint_catches_bound_zero_and_hidden_dict()
    test_lint_catches_case_id_mismatch()
    test_live_package_task_amend_and_rollback_on_bad_grader()
    print("all package loop smokes passed")
