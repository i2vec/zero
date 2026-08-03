"""Start/stop the capgw capturing gateway that fronts the local model.

Both agents point ANTHROPIC_BASE_URL at this gateway; capgw forwards to the
upstream local model (from llm.env) and captures every model call per session.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Union

import httpx

from zero.config import Config


class CapgwRunner:
    def __init__(self, config: Config):
        self._config = config
        self._proc: Optional[subprocess.Popen] = None

    def is_up(self) -> bool:
        try:
            # Bypass any host HTTP(S)_PROXY so localhost health checks work.
            r = httpx.get(f"{self._config.capgw_url}/health", timeout=3, trust_env=False)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def ensure(
        self,
        timeout: float = 30.0,
        *,
        log_path: Optional[Union[Path, str]] = None,
    ) -> bool:
        if self.is_up():
            return True
        self.start(log_path=log_path)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_up():
                return True
            time.sleep(0.5)
        return self.is_up()

    def start(self, *, log_path: Optional[Union[Path, str]] = None) -> None:
        if not os.path.isfile(self._config.llm_env_file):
            raise FileNotFoundError(
                f"llm.env not found at {self._config.llm_env_file}; capgw needs it to reach the local model."
            )
        path = Path(log_path) if log_path is not None else self._config.runs_dir / "capgw.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        log = open(path, "a", encoding="utf-8")  # noqa: SIM115 - lifetime tied to process
        # capgw is bundled inside this project; invoke it via the current
        # interpreter so it works without a separately-installed console script.
        self._proc = subprocess.Popen(
            [
                sys.executable, "-m", "capgw.cli", "serve",
                "--env-file", self._config.llm_env_file,
                "--port", str(self._config.capgw_port),
                "--out", str(self._config.runs_dir),
                "--log-level", "warning",
            ],
            stdout=log, stderr=log,
        )

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
