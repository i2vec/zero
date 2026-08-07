"""Smoke: Harbor host grading + inventory markdown render (no model, no sandbox)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from zero.grading import grade_harbor
from zero.labwright.inventory import render_environment_md
from zero.protocol.environment_inventory import EnvironmentInventory, ImageRecord
from zero.protocol.grading import GradeStatus
from zero.protocol.teaching import CompletionReview, GraderAmendment, TeachingKind
from zero.teacher.optimize import materialize_optimized_task


def main() -> None:
    root = Path(__file__).resolve().parents[2]  # monorepo or package?
    # Prefer parent monorepo tasks/; fall back relative to this package.
    candidates = [
        Path("/personal/zero/tasks/deepbsde-0953fb9735212f3c"),
        root.parent / "tasks" / "deepbsde-0953fb9735212f3c",
        root / "tasks" / "deepbsde-0953fb9735212f3c",
    ]
    pkg = next((p for p in candidates if (p / "tests" / "checker.py").is_file()), None)
    if pkg is None:
        raise SystemExit("deepbsde task package with tests/ not found")

    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td)
        (run_dir / "grading").mkdir(parents=True)
        outputs = run_dir / "grading" / "outputs"
        outputs.mkdir(parents=True)
        # Near-correct Black–Scholes-ish stubs so checker produces a score.
        (outputs / "black_scholes_solution.json").write_text(
            json.dumps({"u0": 57.3}), encoding="utf-8",
        )
        (outputs / "hjb_solution.json").write_text(
            json.dumps({"u0": 4.5901}), encoding="utf-8",
        )
        (outputs / "allen_cahn_solution.json").write_text(
            json.dumps({"u0": 0.0528}), encoding="utf-8",
        )

        grade = grade_harbor(
            run_dir=run_dir,
            tests=pkg / "tests",
            host_outputs=outputs,
        )
        assert grade.status == GradeStatus.SCORED, grade
        assert grade.score is not None and grade.score > 0.9, grade.score
        print(f"OK grade score={grade.score} mode={grade.mode}")

        review = CompletionReview(
            kind=TeachingKind.NO_CHANGE,
            summary="smoke: package already matches literature",
            literature_fidelity_notes="Synthetic perfect outputs; no package edits.",
        )
        out = materialize_optimized_task(
            run_dir=run_dir,
            task_package=pkg,
            task_prompt=(pkg / "instruction.md").read_text(encoding="utf-8"),
            task_key="smoke",
            review=review,
            grade=grade,
        )
        assert (out / "instruction.md").is_file()
        assert (out / "OPTIMIZATION.md").is_file()
        assert (out / "tests" / "checker.py").is_file()
        # Full package seed: environment / solution when present on source.
        for name in ("environment", "solution", "calibration", "paper"):
            if (pkg / name).exists():
                assert (out / name).exists(), f"missing {name}/ in optimized_task"
        print(f"OK optimized_task at {out}")

        # Full-file grader amendment lands in tests/checker.py (not a symbol-named junk file).
        marker = "# SMOKE_GRADER_FULL_FILE\n"
        full_checker = marker + (pkg / "tests" / "checker.py").read_text(encoding="utf-8")
        run_dir2 = Path(td) / "run2"
        run_dir2.mkdir()
        review2 = CompletionReview(
            kind=TeachingKind.GRADER_AMENDMENT,
            summary="smoke full-file grader write",
            grader_amendment=GraderAmendment(
                target="checker.py",
                patch=full_checker,
                reason="smoke",
                literature_basis="smoke literature note",
            ),
            literature_fidelity_notes="smoke literature note",
        )
        out2 = materialize_optimized_task(
            run_dir=run_dir2,
            task_package=pkg,
            task_prompt="smoke task",
            task_key="smoke-grader",
            review=review2,
            grade=grade,
        )
        written = (out2 / "tests" / "checker.py").read_text(encoding="utf-8")
        assert written.startswith(marker), "full file should replace checker.py"
        assert not (out2 / "tests" / "check_consumption_derivative").exists()
        opt_md = (out2 / "OPTIMIZATION.md").read_text(encoding="utf-8")
        assert marker in opt_md, "OPTIMIZATION.md must include full Teacher patch"
        print("OK grader full-file materialize")

        # Legacy unified-diff still applies into checker.py.
        run_dir3 = Path(td) / "run3"
        run_dir3.mkdir()
        seed = (pkg / "tests" / "checker.py").read_text(encoding="utf-8")
        lines = seed.splitlines(keepends=True)
        if not lines:
            raise AssertionError("empty checker")
        n = min(5, len(lines))
        diff = (
            "--- a/tests/checker.py\n"
            "+++ b/tests/checker.py\n"
            f"@@ -1,{n} +1,{n} @@\n"
        )
        for i, line in enumerate(lines[:n]):
            body = line.rstrip("\n")
            if i == 0:
                diff += f"-{body}\n"
                diff += f"+{body}  # via-diff\n"
            else:
                diff += f" {body}\n"

        review3 = CompletionReview(
            kind=TeachingKind.GRADER_AMENDMENT,
            summary="smoke unified diff",
            grader_amendment=GraderAmendment(
                target="check_something",  # legacy symbol name
                patch=diff,
                reason="smoke",
                literature_basis="smoke",
            ),
        )
        out3 = materialize_optimized_task(
            run_dir=run_dir3,
            task_package=pkg,
            task_prompt="smoke",
            task_key="smoke-diff",
            review=review3,
            grade=grade,
        )
        assert "# via-diff" in (out3 / "tests" / "checker.py").read_text(encoding="utf-8")
        assert not (out3 / "tests" / "check_something").exists()
        print("OK grader unified-diff apply")

        inv = EnvironmentInventory(
            environment_id="sha256:deadbeef",
            task_id="smoke",
            sandbox_id="sandbox-smoke-v1",
            backend="local",
            runtime={"python_version": "3.11"},
            packages={"numpy": "2.0"},
            pip_freeze=["numpy==2.0"],
            image=ImageRecord(status="not_publishable", note="smoke"),
        )
        md = render_environment_md(inv)
        assert "## Packaged image" in md
        assert "## Python packages" in md
        print("OK environment.md render")

    print("all smoke checks passed")


if __name__ == "__main__":
    main()
