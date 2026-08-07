"""Central runtime configuration for 0号机.

Everything is derived from a small set of environment variables so the same
code runs unchanged whether the sandbox backend is Docker or the local
fallback. Paths default under ``ZERO_ROOT`` (the repo root by default).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Installable project root (directory that contains ``pyproject.toml`` / ``.env``).
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_path(key: str, default: Path) -> Path:
    raw = os.environ.get(key)
    return Path(raw).expanduser().resolve() if raw else default


def _load_project_dotenv() -> None:
    """Load ``KEY=VALUE`` lines from the project ``.env`` into ``os.environ``.

    A single project ``.env`` can then hold everything — secrets (``LLM_*``,
    ``bohrium_key``) *and* settings (``ZERO_*``). Variables already present in
    the real environment win (so an explicit ``export`` / prefix overrides the
    file); ``export KEY=VALUE`` lines and ``#`` comments are tolerated.
    """
    env_file = os.environ.get("ZERO_ENV_FILE")
    path = Path(env_file).expanduser() if env_file else _REPO_ROOT / ".env"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, _, value = line.partition("=")
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = value.strip().strip('"').strip("'")


# Populate os.environ from the project dotenv before any Config field defaults
# (which read os.environ) are evaluated.
_load_project_dotenv()


@dataclass
class Config:
    # System root: shared inputs (``agent_skills/``, ``tasks/``, …) and ``runs/``.
    # Defaults to this repo root so a standalone clone works out of the box;
    # set ``ZERO_ROOT`` when those trees live outside the package (e.g. a parent
    # monorepo layout).
    root: Path = field(default_factory=lambda: _env_path("ZERO_ROOT", _REPO_ROOT))

    # capgw gateway the two Claude Code sessions talk to.
    capgw_url: str = field(default_factory=lambda: os.environ.get("ZERO_CAPGW_URL", "http://127.0.0.1:8900"))
    capgw_port: int = field(default_factory=lambda: int(os.environ.get("ZERO_CAPGW_PORT", "8900")))
    # capgw's upstream-model config (LLM_BASE_URL / LLM_PRO / LLM_API_KEY). Defaults
    # to the project ``.env`` — the same file that may also hold ``bohrium_key`` —
    # so a single dotenv drives both. capgw ignores the non-LLM_* lines.
    llm_env_file: str = field(default_factory=lambda: os.environ.get(
        "ZERO_LLM_ENV", str(_REPO_ROOT / ".env")))

    # Model name handed to Claude Code (capgw overrides it upstream anyway).
    model: str = field(default_factory=lambda: os.environ.get("ZERO_MODEL", "claude-sonnet-4"))

    # Sandbox backend: auto | docker | local | lbg.
    sandbox_backend: str = field(default_factory=lambda: os.environ.get("ZERO_SANDBOX_BACKEND", "auto"))

    # Base image used by the Docker backend.
    docker_base_image: str = field(default_factory=lambda: os.environ.get("ZERO_DOCKER_BASE_IMAGE", "python:3.11-slim"))

    # --- Bohrium cloud sandbox backend (``lbg``) -------------------------
    # Both CLIs are driven as subprocesses; the ``bohr`` binary is usually not
    # on PATH (image discovery), while ``lbg`` normally is (sandbox lifecycle).
    lbg_bin: str = field(default_factory=lambda: os.environ.get("ZERO_LBG_BIN", "lbg"))
    bohr_bin: str = field(default_factory=lambda: os.environ.get("ZERO_BOHR_BIN", "/root/.bohrium/bohr"))
    # Auto-destroy lifetime (seconds) for each created sandbox; bounds cost if a
    # run is abandoned. Align with ``lbg``'s own default (12h) so long CPU/GPU
    # training (e.g. Deep BSDE) is not paused mid-run under the old 3h default.
    lbg_sandbox_timeout: int = field(default_factory=lambda: int(os.environ.get("ZERO_LBG_TIMEOUT", "43200")))
    # Extra overlay disk on top of the fixed 30Gi default, set at template time.
    lbg_extra_disk_gb: int = field(default_factory=lambda: int(os.environ.get("ZERO_LBG_EXTRA_DISK_GB", "0")))
    # Optional project id: when set, sandboxes bill to that project's budget
    # instead of the personal wallet (passed as ``--project-id``).
    lbg_project_id: str = field(default_factory=lambda: os.environ.get("ZERO_LBG_PROJECT_ID", ""))
    # Completion waits for an LBG image commit so environment.json can record
    # its real imageUrl. Set 0 to only persist the async commit id.
    lbg_image_wait_timeout: int = field(default_factory=lambda: int(
        os.environ.get("ZERO_LBG_IMAGE_WAIT_TIMEOUT", "1800")))
    # How long publish_manifest may block waiting for imageUrl before spawning
    # the experiment sandbox. On timeout, exp is spawned via freeze reinstall.
    lbg_spawn_wait_timeout: int = field(default_factory=lambda: int(
        os.environ.get("ZERO_LBG_SPAWN_WAIT_TIMEOUT", "300")))
    # Domestic pip mirror baked into every lbg sandbox at creation, so installs
    # don't stall on the (often unreachable) default pypi.org. Empty = leave the
    # image's own pip config untouched.
    pip_index_url: str = field(default_factory=lambda: os.environ.get(
        "ZERO_PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple"))

    # --- Teacher agent ----------------------------------------------------
    # The third agent: holds this task's human-written hint bank and answers the
    # Researcher's asks. Its session is created lazily, so a run that never asks
    # costs nothing; set 0/false to remove the tool from the Researcher entirely
    # (useful when a benchmark score must be comparable to an unhinted run).
    teacher_enabled: bool = field(default_factory=lambda: os.environ.get(
        "ZERO_TEACHER_ENABLED", "1").strip().lower() not in ("0", "false", "no", ""))
    # Ask budget for one run, so the Researcher cannot outsource its thinking.
    teacher_max_asks: int = field(default_factory=lambda: int(
        os.environ.get("ZERO_TEACHER_MAX_ASKS", "8")))
    # Max live package revisions (preflight + mid-run + completion) per run.
    package_max_revisions: int = field(default_factory=lambda: int(
        os.environ.get("ZERO_PACKAGE_MAX_REVISIONS", "12")))
    # Teacher preflight of the task package before Researcher starts.
    teacher_preflight: bool = field(default_factory=lambda: os.environ.get(
        "ZERO_TEACHER_PREFLIGHT", "1").strip().lower() not in ("0", "false", "no", ""))

    # Live trace viewer (three-column web dashboard) settings.
    trace_ui_port: int = field(default_factory=lambda: int(os.environ.get("ZERO_TRACE_UI_PORT", "8901")))
    trace_ui_host: str = field(default_factory=lambda: os.environ.get("ZERO_TRACE_UI_HOST", "0.0.0.0"))

    # Finalize deliverables: pull the Researcher's curated ``export/`` (repo vs
    # output) into ``runs/<id>/deliverables/``. This cap is a safety net so a
    # stray dataset/weight file cannot bloat the run.
    export_max_file_mb: int = field(default_factory=lambda: int(os.environ.get("ZERO_EXPORT_MAX_FILE_MB", "50")))

    @property
    def runs_dir(self) -> Path:
        """One folder per task/run — the sole runtime output root."""
        return _env_path("ZERO_RUNS_DIR", self.root / "runs")

    @property
    def researcher_skills_dir(self) -> Path:
        """Plugin root whose ``skills/`` folder is loaded only by the Researcher."""
        return _env_path("ZERO_RESEARCHER_SKILLS", self.root / "agent_skills" / "researcher")

    @property
    def labwright_skills_dir(self) -> Path:
        """Plugin root whose ``skills/`` folder is loaded only by Labwright."""
        return _env_path("ZERO_LABWRIGHT_SKILLS", self.root / "agent_skills" / "labwright")

    @property
    def teacher_skills_dir(self) -> Path:
        """Plugin root whose ``skills/`` folder is loaded only by the Teacher."""
        return _env_path("ZERO_TEACHER_SKILLS", self.root / "agent_skills" / "teacher")

    @property
    def experience_dir(self) -> Path:
        """Shared Researcher experience library (model-written, cross-run).

        Layout: ``index.jsonl`` + ``entries/<id>.md``. Override with
        ``ZERO_EXPERIENCE_DIR``.
        """
        return _env_path("ZERO_EXPERIENCE_DIR", self.root / "experience" / "researcher")

    def run_dir(self, task_id: str) -> Path:
        """``runs/<task_id>/`` — one folder for everything belonging to this run."""
        return self.runs_dir / task_id

    def run_resources_dir(self, task_id: str) -> Path:
        """Labwright download/cache root for this run."""
        return self.run_dir(task_id) / "resources"

    def run_sandboxes_dir(self, task_id: str) -> Path:
        """Local-backend sandbox roots for this run."""
        return self.run_dir(task_id) / "sandboxes"

    def run_skill_candidates_dir(self, task_id: str) -> Path:
        """Skill candidates staged during this run (not auto-loaded)."""
        return self.run_dir(task_id) / "meta" / "skill_candidates"

    def ensure_run_dirs(self, task_id: str) -> Path:
        """Create the unified run tree and return ``runs/<task_id>/``."""
        rd = self.run_dir(task_id)
        for sub in (
            "workspace",
            "deliverables",
            "trace",
            "teacher",
            "teacher/hint_bank",
            "resources",
            "resources/models",
            "resources/datasets",
            "sandboxes",
            "logs",
            "meta",
            "meta/skill_candidates",
            "grading",
            "environment",
        ):
            (rd / sub).mkdir(parents=True, exist_ok=True)
        manifest = rd / "resources" / "manifest.json"
        if not manifest.is_file():
            try:
                manifest.write_text("{\n  \"resources\": []\n}\n", encoding="utf-8")
            except OSError:
                pass
        return rd

    @property
    def bohrium_env_file(self) -> Path:
        """Dotenv file that may hold ``bohrium_key=...`` (host-only secret).

        Defaults to the ``zero`` project directory's ``.env`` (two levels up
        from this module), overridable with ``ZERO_BOHRIUM_ENV``.
        """
        return _env_path("ZERO_BOHRIUM_ENV", _REPO_ROOT / ".env")

    @property
    def bohrium_key(self) -> Optional[str]:
        """Resolve the Bohrium access key, host-side only.

        Precedence: process env (``BOHRIUM_ACCESS_KEY`` / ``ACCESS_KEY`` /
        ``BOHRIUM_KEY``) first, then a ``bohrium_key=`` line in the dotenv file.
        The key is never written to captures nor injected into a sandbox.
        """
        for key in ("BOHRIUM_ACCESS_KEY", "ACCESS_KEY", "BOHRIUM_KEY"):
            val = os.environ.get(key)
            if val and val.strip():
                return val.strip()
        ef = self.bohrium_env_file
        if ef.exists():
            for line in ef.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip().lower() in ("bohrium_key", "bohrium_access_key", "access_key"):
                    return value.strip().strip('"').strip("'") or None
        return None

    def resolved_backend(self) -> str:
        """Resolve ``auto`` to a concrete backend based on availability."""
        if self.sandbox_backend != "auto":
            return self.sandbox_backend
        if shutil.which("docker") and os.path.exists("/var/run/docker.sock"):
            return "docker"
        return "local"

    def ensure_dirs(self) -> None:
        """Create shared roots only (per-run trees are ``ensure_run_dirs``)."""
        for p in (
            self.runs_dir,
            self.experience_dir,
            self.experience_dir / "entries",
        ):
            p.mkdir(parents=True, exist_ok=True)
        index = self.experience_dir / "index.jsonl"
        if not index.is_file():
            try:
                index.write_text("", encoding="utf-8")
            except OSError:
                pass


_CONFIG: Config | None = None


def get_config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config()
        _CONFIG.ensure_dirs()
    return _CONFIG
