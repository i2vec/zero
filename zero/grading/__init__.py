"""Harbor-style grading: run task ``tests/checker.py`` against experiment outputs.

Prefer executing inside the live sandbox (``/tests`` + ``/app/outputs`` +
``/logs/verifier``) so the checker sees Harbor paths. Fall back to a host
stage with path-rewritten checker source when no sandbox is available.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from zero.protocol.grading import GradeResult, GradeStatus
from zero.sandbox.manager import SandboxManager

_REWARD_RE = re.compile(r"^[0-9.]+$")


def grade_harbor(
    *,
    run_dir: Path,
    tests: Path,
    manager: Optional[SandboxManager] = None,
    sandbox_id: Optional[str] = None,
    host_outputs: Optional[Path] = None,
    timeout: int = 600,
) -> GradeResult:
    """Run the package checker and write ``runs/<id>/grading/``."""
    grading_dir = run_dir / "grading"
    grading_dir.mkdir(parents=True, exist_ok=True)

    source = {
        "tests_dir": str(tests.resolve()),
        "checker": str((tests / "checker.py").resolve()),
        "grading_spec": str((tests / "grading_spec.json").resolve())
        if (tests / "grading_spec.json").is_file()
        else None,
        "ts": time.time(),
    }

    if not (tests / "checker.py").is_file():
        result = GradeResult(
            status=GradeStatus.MISSING_PACKAGE,
            error="tests/checker.py not found",
            tests_dir=str(tests),
            grade_source=source,
        )
        _write_result(grading_dir, result)
        return result

    if manager is not None and sandbox_id:
        try:
            result = _grade_in_sandbox(
                manager=manager,
                sandbox_id=sandbox_id,
                tests=tests,
                grading_dir=grading_dir,
                timeout=timeout,
                source=source,
            )
            _write_result(grading_dir, result)
            return result
        except Exception as exc:  # noqa: BLE001
            # Fall through to host; record the sandbox attempt.
            source["sandbox_error"] = str(exc)[:500]

    result = _grade_on_host(
        tests=tests,
        grading_dir=grading_dir,
        host_outputs=host_outputs,
        timeout=timeout,
        source=source,
    )
    _write_result(grading_dir, result)
    return result


def materialize_host_outputs(
    *,
    run_dir: Path,
    manager: Optional[SandboxManager],
    sandbox_ids: list[str],
    workspace: str,
) -> Optional[Path]:
    """Best-effort: collect ``/app/outputs`` (or export/output) onto the host."""
    dest = run_dir / "grading" / "outputs"
    dest.mkdir(parents=True, exist_ok=True)

    # Prefer already-exported deliverables.
    for candidate in (
        run_dir / "deliverables" / "output",
        Path(workspace) / "export" / "output",
        Path(workspace) / "app" / "outputs",
    ):
        if candidate.is_dir() and any(candidate.iterdir()):
            _copy_tree(candidate, dest)
            return dest

    if manager is None:
        return dest if any(dest.iterdir()) else None

    for sid in reversed(sandbox_ids):
        try:
            listing = manager.exec(
                sid,
                "find /app/outputs -type f 2>/dev/null | head -500",
                timeout=60,
            )
        except Exception:  # noqa: BLE001
            continue
        if listing.exit_code != 0 or not listing.stdout.strip():
            continue
        for line in listing.stdout.splitlines():
            remote = line.strip()
            if not remote.startswith("/app/outputs/"):
                continue
            rel = remote[len("/app/outputs/"):]
            if not rel or ".." in rel:
                continue
            try:
                data = manager.get_file(sid, remote)
            except Exception:  # noqa: BLE001
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if any(dest.rglob("*")):
            return dest
    return dest if any(dest.iterdir()) else None


def _grade_in_sandbox(
    *,
    manager: SandboxManager,
    sandbox_id: str,
    tests: Path,
    grading_dir: Path,
    timeout: int,
    source: dict,
) -> GradeResult:
    manager.exec(sandbox_id, "mkdir -p /tests /app/outputs /logs/verifier", timeout=60)
    for path in tests.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(tests).as_posix()
        manager.put_file(sandbox_id, f"/tests/{rel}", path.read_bytes())

    probe = manager.exec(
        sandbox_id,
        "find /app/outputs -type f 2>/dev/null | head -20",
        timeout=60,
    )
    if not (probe.stdout or "").strip():
        return GradeResult(
            status=GradeStatus.MISSING_OUTPUTS,
            mode="sandbox",
            sandbox_id=sandbox_id,
            tests_dir=str(tests),
            error="sandbox /app/outputs has no files",
            stdout=probe.stdout,
            stderr=probe.stderr,
            exit_code=probe.exit_code,
            grade_source=source,
        )

    cmd = "bash /tests/test.sh" if (tests / "test.sh").is_file() else "python3 /tests/checker.py"
    executed = manager.exec(sandbox_id, cmd, timeout=timeout)
    reward_txt = _safe_get(manager, sandbox_id, "/logs/verifier/reward.txt")
    breakdown_raw = _safe_get(manager, sandbox_id, "/logs/verifier/breakdown.json")

    local_reward = grading_dir / "reward.txt"
    local_breakdown = grading_dir / "breakdown.json"
    score = None
    breakdown = None
    if reward_txt is not None:
        local_reward.write_bytes(reward_txt)
        score = _parse_reward(reward_txt.decode("utf-8", errors="replace"))
    if breakdown_raw is not None:
        local_breakdown.write_bytes(breakdown_raw)
        try:
            breakdown = json.loads(breakdown_raw.decode("utf-8"))
        except json.JSONDecodeError:
            breakdown = None

    status = GradeStatus.SCORED if score is not None else GradeStatus.CHECKER_ERROR
    return GradeResult(
        status=status,
        score=score,
        breakdown=breakdown,
        reward_path=str(local_reward) if reward_txt is not None else None,
        breakdown_path=str(local_breakdown) if breakdown_raw is not None else None,
        stdout=executed.stdout[-8000:],
        stderr=executed.stderr[-8000:],
        exit_code=executed.exit_code,
        mode="sandbox",
        tests_dir=str(tests),
        outputs_dir="/app/outputs",
        sandbox_id=sandbox_id,
        error=None if score is not None else "checker produced no reward.txt",
        grade_source=source,
    )


def _grade_on_host(
    *,
    tests: Path,
    grading_dir: Path,
    host_outputs: Optional[Path],
    timeout: int,
    source: dict,
) -> GradeResult:
    if host_outputs is None or not host_outputs.is_dir() or not any(host_outputs.iterdir()):
        return GradeResult(
            status=GradeStatus.MISSING_OUTPUTS,
            mode="host",
            tests_dir=str(tests),
            outputs_dir=str(host_outputs) if host_outputs else None,
            error="no host outputs to grade",
            grade_source=source,
        )

    stage = grading_dir / "_host_stage"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    out = stage / "app" / "outputs"
    log_dir = stage / "logs" / "verifier"
    out.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    _copy_tree(host_outputs, out)

    checker_src = (tests / "checker.py").read_text(encoding="utf-8", errors="replace")
    patched = _rewrite_checker_paths(checker_src, outputs=out, tests=tests, logs=log_dir)
    runner = stage / "_run_checker.py"
    runner.write_text(patched, encoding="utf-8")

    proc = subprocess.run(
        ["python3", str(runner)],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(stage),
        check=False,
    )
    reward_path = log_dir / "reward.txt"
    breakdown_path = log_dir / "breakdown.json"
    score = None
    breakdown = None
    if reward_path.is_file():
        text = reward_path.read_text(encoding="utf-8", errors="replace")
        score = _parse_reward(text)
        (grading_dir / "reward.txt").write_text(text, encoding="utf-8")
    if breakdown_path.is_file():
        raw = breakdown_path.read_text(encoding="utf-8", errors="replace")
        (grading_dir / "breakdown.json").write_text(raw, encoding="utf-8")
        try:
            breakdown = json.loads(raw)
        except json.JSONDecodeError:
            breakdown = None

    status = GradeStatus.SCORED if score is not None else GradeStatus.CHECKER_ERROR
    return GradeResult(
        status=status,
        score=score,
        breakdown=breakdown,
        reward_path=str(grading_dir / "reward.txt") if score is not None else None,
        breakdown_path=str(grading_dir / "breakdown.json") if breakdown is not None else None,
        stdout=(proc.stdout or "")[-8000:],
        stderr=(proc.stderr or "")[-8000:],
        exit_code=proc.returncode,
        mode="host",
        tests_dir=str(tests),
        outputs_dir=str(out),
        error=None if score is not None else "host checker produced no reward.txt",
        grade_source=source,
    )


def _rewrite_checker_paths(src: str, *, outputs: Path, tests: Path, logs: Path) -> str:
    purged = re.sub(r"^OUTPUTS\s*=\s*.*$", "", src, flags=re.M)
    purged = re.sub(r"^SPEC_PATH\s*=\s*.*$", "", purged, flags=re.M)
    purged = re.sub(r"^LOG_DIR\s*=\s*.*$", "", purged, flags=re.M)
    preamble = (
        "from pathlib import Path\n"
        f"OUTPUTS = Path({str(outputs.resolve())!r})\n"
        f"SPEC_PATH = Path({str((tests / 'grading_spec.json').resolve())!r})\n"
        f"LOG_DIR = Path({str(logs.resolve())!r})\n"
    )
    return preamble + "\n" + purged


def _parse_reward(text: str) -> Optional[float]:
    line = (text or "").strip().splitlines()
    if not line:
        return None
    raw = line[0].strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    if value != value:  # NaN
        return None
    return max(0.0, min(1.0, value))


def _safe_get(manager: SandboxManager, sandbox_id: str, path: str) -> Optional[bytes]:
    try:
        return manager.get_file(sandbox_id, path)
    except Exception:  # noqa: BLE001
        return None


def _copy_tree(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for path in src.rglob("*"):
        if path.is_file():
            rel = path.relative_to(src)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _write_result(grading_dir: Path, result: GradeResult) -> None:
    (grading_dir / "result.json").write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8",
    )
    (grading_dir / "grade_source.json").write_text(
        json.dumps(result.grade_source, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (grading_dir / "stdout.log").write_text(result.stdout or "", encoding="utf-8")
    (grading_dir / "stderr.log").write_text(result.stderr or "", encoding="utf-8")
