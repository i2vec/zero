"""Static lint for Harbor-style task packages (fail closed on known wiring bugs)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class LintIssue:
    severity: str  # error | warning
    code: str
    message: str
    path: str = ""


@dataclass
class LintReport:
    ok: bool
    issues: list[LintIssue] = field(default_factory=list)

    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [
                {
                    "severity": i.severity,
                    "code": i.code,
                    "message": i.message,
                    "path": i.path,
                }
                for i in self.issues
            ],
        }


def lint_task_package(package: Path) -> LintReport:
    """Lint a task package directory. ``ok`` is False if any error-severity issue."""
    package = Path(package)
    issues: list[LintIssue] = []

    if not package.is_dir():
        return LintReport(
            ok=False,
            issues=[LintIssue("error", "not_a_package", f"not a directory: {package}")],
        )

    instruction = package / "instruction.md"
    if not instruction.is_file():
        issues.append(LintIssue("error", "missing_instruction", "instruction.md missing", "instruction.md"))

    tests = package / "tests"
    if not tests.is_dir():
        issues.append(LintIssue("error", "missing_tests", "tests/ missing", "tests"))
        return LintReport(ok=False, issues=issues)

    if not (tests / "checker.py").is_file():
        issues.append(LintIssue("error", "missing_checker", "tests/checker.py missing", "tests/checker.py"))

    plan_path = tests / "verification_plan.json"
    plan: Optional[dict[str, Any]] = None
    if plan_path.is_file():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            issues.append(
                LintIssue("error", "plan_json", f"invalid JSON: {exc}", str(plan_path.relative_to(package)))
            )
    else:
        issues.append(LintIssue("warning", "no_plan", "tests/verification_plan.json missing", "tests/verification_plan.json"))

    if plan is not None:
        issues.extend(_lint_verification_plan(package, tests, plan))

    ok = not any(i.severity == "error" for i in issues)
    return LintReport(ok=ok, issues=issues)


def _lint_verification_plan(package: Path, tests: Path, plan: dict[str, Any]) -> list[LintIssue]:
    issues: list[LintIssue] = []
    checks = plan.get("checks") or []
    if not isinstance(checks, list):
        return [LintIssue("error", "checks_type", "verification_plan.checks must be a list", "tests/verification_plan.json")]

    for check in checks:
        if not isinstance(check, dict):
            continue
        cid = str(check.get("id") or "?")
        decision = check.get("decision") or {}
        config = check.get("config") or {}
        curve = str(decision.get("curve") or "")
        direction = str(decision.get("direction") or "")
        bound = decision.get("bound")
        topology = str(check.get("topology") or "none")

        # Impossible "error <= 0" gates (mlhydro-class bug).
        if (
            curve == "threshold_or_better"
            and direction in ("<=", "<")
            and _is_number(bound)
            and float(bound) == 0.0
            and str(check.get("reference") or "") in ("paper_value", "derived_reference", "")
        ):
            # Allow constraint_only non-negativity (value >= 0) but flag <= 0 on metrics.
            field = str(config.get("field") or "").lower()
            if any(k in field for k in ("rae", "rse", "error", "loss", "residual")) or not field:
                issues.append(
                    LintIssue(
                        "error",
                        "bound_zero_threshold",
                        (
                            f"check {cid!r}: threshold_or_better {direction} 0 is impossible "
                            f"for metric field {field!r}; use decay + expected or a positive bound"
                        ),
                        "tests/verification_plan.json",
                    )
                )

        if topology == "hidden_case":
            issues.extend(_lint_hidden_case(tests, check, cid))

    # Caps that depend on checks which can never score due to schema.
    for cap in plan.get("caps") or []:
        if not isinstance(cap, dict):
            continue
        when = cap.get("when") or {}
        zero_ids = when.get("all_checks_zero") or []
        for zid in zero_ids:
            for check in checks:
                if isinstance(check, dict) and check.get("id") == zid:
                    # Already covered by hidden_case lint; warn if check missing.
                    break
            else:
                issues.append(
                    LintIssue(
                        "error",
                        "cap_missing_check",
                        f"cap {cap.get('id')!r} references missing check {zid!r}",
                        "tests/verification_plan.json",
                    )
                )

    return issues


def _lint_hidden_case(tests: Path, check: dict[str, Any], cid: str) -> list[LintIssue]:
    issues: list[LintIssue] = []
    config = check.get("config") or {}
    bundle_name = str(config.get("hidden_bundle") or "")
    if not bundle_name:
        issues.append(
            LintIssue(
                "error",
                "hidden_bundle_missing",
                f"check {cid!r}: topology hidden_case needs config.hidden_bundle",
                "tests/verification_plan.json",
            )
        )
        return issues

    bundle_path = tests / "hidden" / bundle_name
    if not bundle_path.is_file():
        issues.append(
            LintIssue(
                "error",
                "hidden_file_missing",
                f"check {cid!r}: hidden bundle not found: {bundle_name}",
                f"tests/hidden/{bundle_name}",
            )
        )
        return issues

    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        issues.append(
            LintIssue("error", "hidden_json", f"check {cid!r}: {exc}", f"tests/hidden/{bundle_name}")
        )
        return issues

    cases = bundle.get("cases") if isinstance(bundle, dict) else None
    if not isinstance(cases, list) or not cases:
        issues.append(
            LintIssue(
                "error",
                "hidden_cases",
                f"check {cid!r}: hidden bundle has no cases[]",
                f"tests/hidden/{bundle_name}",
            )
        )
        return issues

    # Executor currently reads case["id"] and case["expected"] (see executors.py).
    # Config case_field/case_value_field are for submission matching only.
    sample = next((c for c in cases if isinstance(c, dict)), None)
    if sample is None:
        return issues

    if "id" not in sample and "case_id" in sample:
        issues.append(
            LintIssue(
                "error",
                "hidden_id_field",
                (
                    f"check {cid!r}: hidden cases use 'case_id' but executor reads "
                    f"case['id']; rename to id or fix executor"
                ),
                f"tests/hidden/{bundle_name}",
            )
        )

    expected = sample.get("expected")
    value_field = str(config.get("case_value_field") or "")
    if expected is None and value_field and value_field in sample:
        issues.append(
            LintIssue(
                "error",
                "hidden_expected_field",
                (
                    f"check {cid!r}: hidden cases expose {value_field!r} but executor "
                    f"reads case['expected']; set expected to that scalar (or fix executor)"
                ),
                f"tests/hidden/{bundle_name}",
            )
        )
    elif isinstance(expected, dict):
        field = str(config.get("field") or "")
        if field and field in expected:
            issues.append(
                LintIssue(
                    "error",
                    "hidden_expected_dict",
                    (
                        f"check {cid!r}: expected is a dict but decay compares scalars; "
                        f"use expected: <number> for field {field!r}"
                    ),
                    f"tests/hidden/{bundle_name}",
                )
            )
        elif not field:
            issues.append(
                LintIssue(
                    "error",
                    "hidden_expected_dict",
                    f"check {cid!r}: expected must be a numeric scalar for hidden_case",
                    f"tests/hidden/{bundle_name}",
                )
            )
    elif expected is not None and not _is_number(expected):
        issues.append(
            LintIssue(
                "error",
                "hidden_expected_type",
                f"check {cid!r}: expected must be numeric, got {type(expected).__name__}",
                f"tests/hidden/{bundle_name}",
            )
        )

    return issues


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
