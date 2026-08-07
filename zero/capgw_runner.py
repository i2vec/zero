"""Start/stop the capgw capturing gateway that fronts the upstream model.

Agents point ``ANTHROPIC_BASE_URL`` at this gateway; capgw forwards to the
upstream LLM and captures every model call keyed by session id
(``<task_id>/trace/<agent>``), so **many runs can share one port**.

By default (``ZERO_CAPGW_SHARED=1``) processes take/release a lease with a
file lock + refcount under ``runs/.capgw/``. The last lease holder stops the
daemon. A background watchdog restarts the daemon if it dies mid-run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Union

import httpx

from zero.config import Config

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None  # type: ignore[assignment]


class CapgwRunner:
    def __init__(self, config: Config):
        self._config = config
        self._proc: Optional[subprocess.Popen] = None
        self._leased = False
        self._shared = os.environ.get("ZERO_CAPGW_SHARED", "1").strip().lower() not in (
            "0", "false", "no", "off",
        )
        self._watch_stop: Optional[threading.Event] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._log_path: Optional[Path] = None
        interval = os.environ.get("ZERO_CAPGW_WATCHDOG_SEC", "15").strip()
        try:
            self._watch_interval = max(5.0, float(interval))
        except ValueError:
            self._watch_interval = 15.0

    @property
    def _state_dir(self) -> Path:
        d = self._config.runs_dir / ".capgw" / f"port-{self._config.capgw_port}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def _lock_path(self) -> Path:
        return self._state_dir / "lock"

    @property
    def _refs_path(self) -> Path:
        return self._state_dir / "refs.json"

    @property
    def _pid_path(self) -> Path:
        return self._state_dir / "pid"

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
        if log_path is not None:
            self._log_path = Path(log_path)
        if self._shared:
            ok = self._ensure_shared(timeout=timeout, log_path=log_path)
        else:
            ok = self._ensure_exclusive(timeout=timeout, log_path=log_path)
        if ok:
            self._start_watchdog()
        return ok

    def stop(self) -> None:
        self._stop_watchdog()
        if self._shared:
            self._release_shared()
            return
        self._stop_owned_proc()

    def reheal_if_needed(self, *, timeout: float = 30.0) -> bool:
        """If the gateway is down while we hold a lease (or own a proc), restart it."""
        if self.is_up():
            return True
        if self._shared:
            if not self._leased:
                return False
            return self._reheal_shared(timeout=timeout)
        # Exclusive: restart the process we manage.
        if self._proc is None and not self.is_up():
            return False
        with self._file_lock():
            if self.is_up():
                return True
            self._stop_owned_proc()
            self.start(log_path=self._log_path)
        return self._wait_up(timeout=timeout)

    # ---- exclusive (legacy) ------------------------------------------------ #
    def _ensure_exclusive(
        self,
        *,
        timeout: float,
        log_path: Optional[Union[Path, str]],
    ) -> bool:
        if self.is_up():
            self._leased = True
            return True
        self.start(log_path=log_path)
        self._leased = True
        return self._wait_up(timeout=timeout)

    def start(self, *, log_path: Optional[Union[Path, str]] = None) -> None:
        if not os.path.isfile(self._config.llm_env_file):
            raise FileNotFoundError(
                f"llm.env not found at {self._config.llm_env_file}; capgw needs it to reach the local model."
            )
        path = Path(log_path) if log_path is not None else (
            self._log_path
            or (self._config.runs_dir / ".capgw" / f"port-{self._config.capgw_port}" / "capgw.log")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        self._log_path = path
        log = open(path, "a", encoding="utf-8")  # noqa: SIM115 - lifetime tied to process
        self._proc = subprocess.Popen(
            [
                sys.executable, "-m", "capgw.cli", "serve",
                "--env-file", self._config.llm_env_file,
                "--port", str(self._config.capgw_port),
                "--out", str(self._config.runs_dir),
                "--log-level", "warning",
            ],
            stdout=log, stderr=log,
            start_new_session=True,
        )

    def _stop_owned_proc(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def _wait_up(self, *, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_up():
                return True
            if self._proc is not None and self._proc.poll() is not None:
                break
            time.sleep(0.5)
        return self.is_up()

    # ---- shared ------------------------------------------------------------ #
    def _ensure_shared(
        self,
        *,
        timeout: float,
        log_path: Optional[Union[Path, str]],
    ) -> bool:
        if self._leased:
            if self.is_up():
                return True
            return self._reheal_shared(timeout=timeout)

        with self._file_lock():
            if self.is_up():
                self._bump_refs(+1)
                self._leased = True
                return True
            shared_log = self._state_dir / "capgw.log"
            self.start(log_path=log_path or shared_log)
            if self._proc is not None and self._proc.pid:
                self._pid_path.write_text(str(self._proc.pid) + "\n", encoding="utf-8")
            self._write_refs(1)
            self._leased = True

        if self._wait_up(timeout=timeout):
            return True
        with self._file_lock():
            if self._leased:
                self._bump_refs(-1)
                self._leased = False
            self._stop_owned_proc()
            self._clear_pid_if_ours()
        return False

    def _reheal_shared(self, *, timeout: float) -> bool:
        with self._file_lock():
            if self.is_up():
                return True
            # Restart without changing refcount — lease holders already counted.
            self._stop_daemon()
            shared_log = self._log_path or (self._state_dir / "capgw.log")
            self.start(log_path=shared_log)
            if self._proc is not None and self._proc.pid:
                self._pid_path.write_text(str(self._proc.pid) + "\n", encoding="utf-8")
        ok = self._wait_up(timeout=timeout)
        if ok:
            print(f"[capgw] reheal: gateway restarted on :{self._config.capgw_port}")
        else:
            print(f"[capgw] reheal: failed to restart gateway on :{self._config.capgw_port}")
        return ok

    def _release_shared(self) -> None:
        if not self._leased:
            self._stop_owned_proc()
            return
        with self._file_lock():
            n = self._bump_refs(-1)
            self._leased = False
            if n > 0:
                self._proc = None
                return
            self._stop_daemon()
            self._write_refs(0)

    def _stop_daemon(self) -> None:
        self._stop_owned_proc()
        pid = self._read_pid()
        if pid is not None and self._pid_alive(pid):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
            deadline = time.time() + 10
            while time.time() < deadline and self._pid_alive(pid):
                time.sleep(0.2)
            if self._pid_alive(pid):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
        try:
            self._pid_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _clear_pid_if_ours(self) -> None:
        if self._proc is None or self._proc.pid is None:
            return
        try:
            if self._read_pid() == self._proc.pid:
                self._pid_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _file_lock(self):
        return _Flock(self._lock_path)

    def _read_refs(self) -> int:
        try:
            data = json.loads(self._refs_path.read_text(encoding="utf-8"))
            return max(0, int(data.get("n", 0)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0

    def _write_refs(self, n: int) -> None:
        payload = {"n": max(0, int(n)), "port": self._config.capgw_port, "ts": time.time()}
        self._refs_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _bump_refs(self, delta: int) -> int:
        n = max(0, self._read_refs() + delta)
        self._write_refs(n)
        return n

    def _read_pid(self) -> Optional[int]:
        try:
            return int(self._pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # ---- watchdog ---------------------------------------------------------- #
    def _start_watchdog(self) -> None:
        if self._watch_thread is not None and self._watch_thread.is_alive():
            return
        stop = threading.Event()
        self._watch_stop = stop

        def _loop() -> None:
            while not stop.wait(self._watch_interval):
                try:
                    self.reheal_if_needed()
                except Exception:  # noqa: BLE001 - never kill the run from watchdog
                    pass

        self._watch_thread = threading.Thread(
            target=_loop, name=f"capgw-watchdog-{self._config.capgw_port}", daemon=True,
        )
        self._watch_thread.start()

    def _stop_watchdog(self) -> None:
        if self._watch_stop is not None:
            self._watch_stop.set()
        self._watch_stop = None
        self._watch_thread = None


class _Flock:
    """Best-effort exclusive file lock (no-op if fcntl unavailable)."""

    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            self._fh.close()
            self._fh = None
        return False
