"""Bohrium cloud-sandbox provider (``lbg`` CLI backend).

``SandboxProvider`` (see ``base.py``) is the single seam that keeps the upper
layers (the Researcher's ``run_in_sandbox`` tool, the orchestrator) backend-
agnostic. Local / Docker / lbg all implement the same interface, so the
Researcher only ever sees "a ``sandbox_id`` + exec/read-file", regardless of
where the sandbox physically runs.

Two-phase launch
----------------
Compute sizing (``--sku-name``) and overlay disk (``--extra-ephemeral-storage-gb``,
to beat the fixed 30Gi default) can only be set at the *template* level, so
creation is two-phase:

    1. template create (idempotent, reusable):
         lbg sdbx template create --name <hash> --image <bohr-url>
             --sku-name <sku> [--extra-ephemeral-storage-gb <N>] --json
    2. sandbox launch:
         lbg sdbx create <template-name> --timeout <budget> --json

The template name is a deterministic hash of (image, sku, extra_disk) so
repeated tasks reuse the same template (prewarm-cache friendly + matches the
project's idempotency philosophy). Base-image tags are immutable (bohr Basic
Images carry fixed version tags), avoiding the ``:latest`` churn lbg warns about.

Command mapping (SandboxProvider method -> CLI)
-----------------------------------------------
    create_sandbox  -> template create (if missing) + ``lbg sdbx create <tpl> --json``
    exec            -> ``lbg sdbx exec --user root --timeout <t> <rid> <cmd> --json``
    put_file        -> ``lbg sdbx files write --source <src> <rid> <path> --json``
    get_file        -> ``lbg sdbx files read <rid> <path> --format bytes --output <dst>``
    mount           -> in-sandbox download into ``ref.target_path()`` (proxy on ->
                       huggingface-cli -> proxy off); no host bind-mount is possible
                       on a remote sandbox.
    snapshot        -> ``lbg sdbx image commit --sandbox-id --name --project-id``
                       (async; returns ``lbg:commit:<id>`` immediately). Falls
                       back to a ``pip freeze`` digest if no project id is
                       configured or the call fails. Resolve the real image
                       with ``wait_for_image(<id>)`` (not on the hot path).
    destroy         -> ``lbg sdbx kill <rid> --force`` (idempotent teardown so we
                       never leak a billed sandbox)
    info            -> ``lbg sdbx describe <rid> --json``

Base-image discovery (``bohr`` CLI)
-----------------------------------
    ACCESS_KEY=<key> bohr image list -t "Basic Image" --json
    -> [{"version","url","resourceType","desc"}, ...]
An explicit registry image (``spec.base_image`` containing ``/``) is used as-is;
otherwise a Basic Image is picked by matching python version + CPU-vs-GPU.

Auth & secret hygiene
---------------------
One Bohrium access key, two mechanisms: ``lbg`` reads ``BOHRIUM_ACCESS_KEY``,
``bohr`` reads ``ACCESS_KEY``. Both are injected only into the subprocess env
(via :attr:`Config.bohrium_key`); the key stays host-side and is never written
to captures nor into a sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from zero.config import Config
from zero.sandbox.base import (
    ExecResult,
    MountSpec,
    SandboxHandle,
    SandboxInfo,
    SandboxProvider,
    SandboxSpec,
)

# Overseas-reach HTTP proxy toggle (see lbg-cli skill: sandbox/network.md).
_PROXY_ON = (
    "mkdir -p ~/.pip && printf '[global]\\nproxy=http://pai.ga.op.xdptech.com:3128\\n' > ~/.pip/pip.conf; "
    "printf 'proxy_servers:\\n  http: http://pai.ga.op.xdptech.com:3128\\n"
    "  https: http://pai.ga.op.xdptech.com:3128\\nssl_verify: false\\n' > ~/.condarc; "
    "git config --global http.proxy http://pai.ga.op.xdptech.com:3128; "
    "git config --global https.proxy http://pai.ga.op.xdptech.com:3128"
)
_PROXY_OFF = (
    "rm -f ~/.pip/pip.conf ~/.condarc; "
    "git config --global --unset http.proxy 2>/dev/null || true; "
    "git config --global --unset https.proxy 2>/dev/null || true"
)

_WORKSPACE = "/workspace"


def _loads(text: str) -> Any:
    """Best-effort JSON parse of CLI stdout (tolerates trailing/leading noise)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the last balanced JSON object/array on the last line.
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line and line[0] in "[{":
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
    return None


def _as_list(data: Any) -> list[dict]:
    """Normalise ``--json`` output that may be a bare array or an envelope."""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for key in ("list", "data", "items", "results"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [d for d in inner if isinstance(d, dict)]
    return []


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _num(value: Any) -> float:
    m = re.findall(r"[\d.]+", str(value))
    return float(m[0]) if m else 0.0


def _mem_gb(value: Any) -> float:
    """Coerce a SKU memory field ("64", "64Gi", "9344MB") to GB."""
    s = str(value).strip().lower()
    n = _num(s)
    if ("m" in s) and ("g" not in s):  # megabytes
        return n / 1024
    return n


class LbgProvider(SandboxProvider):
    name = "lbg"

    def __init__(self, config: Config):
        self._cfg = config
        self._remote: dict[str, str] = {}          # internal sandbox_id -> remote sandboxID
        self._specs: dict[str, SandboxSpec] = {}

    # -- subprocess plumbing ----------------------------------------------
    def _env(self, *, for_bohr: bool = False) -> dict[str, str]:
        env = os.environ.copy()
        key = self._cfg.bohrium_key
        if key:
            # lbg reads BOHRIUM_ACCESS_KEY; bohr reads ACCESS_KEY. Set both so a
            # single resolved key drives either CLI.
            env["BOHRIUM_ACCESS_KEY"] = key
            env["ACCESS_KEY"] = key
        return env

    def _lbg(self, args: list[str], timeout: int = 600,
             input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self._cfg.lbg_bin, *args],
            capture_output=True, timeout=timeout, input=input_bytes, env=self._env(),
        )

    def _bohr(self, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self._cfg.bohr_bin, *args],
            capture_output=True, timeout=timeout, env=self._env(for_bohr=True),
        )

    @staticmethod
    def _text(proc: subprocess.CompletedProcess, stream: str = "stdout") -> str:
        raw = getattr(proc, stream) or b""
        return raw.decode(errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)

    def _rid(self, sandbox_id: str) -> str:
        rid = self._remote.get(sandbox_id)
        if not rid:
            raise KeyError(f"unknown lbg sandbox: {sandbox_id}")
        return rid

    def _error_message(self, proc: subprocess.CompletedProcess) -> str:
        """lbg reports failures as a JSON ``{"error","type","code"}`` on stdout
        (stderr is often empty), so check both channels."""
        payload = _loads(self._text(proc))
        if isinstance(payload, dict):
            msg = _first(payload, "error", "message", "msg")
            if msg:
                return str(msg)
        err = self._text(proc, "stderr").strip()
        return err or self._text(proc).strip() or f"exit code {proc.returncode}"

    @staticmethod
    def _is_transient(proc: subprocess.CompletedProcess) -> bool:
        blob = ""
        for stream in ("stdout", "stderr"):
            raw = getattr(proc, stream) or b""
            blob += raw.decode(errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        blob = blob.lower()
        return any(s in blob for s in (
            "502", "temporarily unavailable", "context deadline", "timeout",
            "gateway", "try again", "please retry",
        ))

    # -- discovery / sizing -----------------------------------------------
    def _list_base_images(self, filt: str = "Basic Image") -> list[dict]:
        try:
            proc = self._bohr(["image", "list", "-t", filt, "--json"], timeout=120)
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []
        return _as_list(_loads(self._text(proc)))

    def _pick_base_image(self, spec: SandboxSpec) -> str:
        # An explicit image reference wins: a full registry path
        # (e.g. "registry.dp.tech/...:tag") or an immutable commit ref
        # ("lbg:commit:<id>", which lbg itself resolves for committed images).
        if spec.base_image and (
            "/" in spec.base_image or spec.base_image.startswith("lbg:commit:")
        ):
            return spec.base_image

        images = self._list_base_images()
        if not images:
            raise RuntimeError(
                "lbg: no Basic Images returned by `bohr image list` "
                "(check the Bohrium access key / bohr binary path)"
            )

        want_gpu = spec.gpu_count > 0
        pyver = (spec.python_version or "").strip()

        # Heavy/specialised layers slow the image cold-pull (which can blow the
        # create gateway deadline) and are rarely what a generic run needs.
        heavy = ("irkernel", "-r4", "r:4", "r4.4", "intel", "pytorch", "matlab",
                 "lammps", "vasp", "conda", "tensorflow")

        def score(img: dict) -> tuple[int, int]:
            version = str(img.get("version", ""))
            blob = " ".join(
                str(img.get(k, "")) for k in ("resourceType", "version", "desc", "url", "image")
            ).lower()
            is_gpu = ("gpu" in blob) or ("cuda" in blob)
            if want_gpu != is_gpu:
                return (-10, 0)
            s = 0
            if pyver and (f"py{pyver}" in blob or f"python{pyver}" in blob or pyver in blob):
                s += 2
            if "ubuntu" in blob:
                s += 1
            if any(h in blob for h in heavy):
                s -= 3
            # Tie-break: prefer the leanest (shortest version) clean image.
            return (s, -len(version))

        best, best_score = None, (-99, 0)
        for img in images:
            sc = score(img)
            if sc > best_score:
                best, best_score = img, sc
        if best is None or best_score[0] < 0:
            best = images[0]  # nothing matched CPU/GPU intent; take the first
        url = _first(best, "url", "image", "imagePath", "image_path")
        if not url:
            raise RuntimeError(f"lbg: Basic Image entry has no image url: {best!r}")
        return str(url)

    def _resolve_sku(self, spec: SandboxSpec) -> str:
        cat = ["-c", "gpu"] if spec.gpu_count > 0 else []
        proc = self._lbg(["sdbx", "machine", "list", *cat, "--json"], timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"lbg sdbx machine list failed: {self._text(proc, 'stderr')}")
        machines = _as_list(_loads(self._text(proc)))
        if not machines:
            raise RuntimeError("lbg: no SKUs returned by `sdbx machine list`")

        candidates: list[tuple[float, float, int, str]] = []
        for m in machines:
            sku = _first(m, "sku_name", "skuName", "name")
            if not sku:
                continue
            cpu = _num(_first(m, "cpu", "cpuCount", "cpu_count"))
            mem = _mem_gb(_first(m, "memory", "memoryGB", "memory_gb", "mem"))
            gpu = int(_num(_first(m, "gpu_count", "gpuCount", "gpu") or 0))
            if cpu >= spec.cpu_count and mem >= spec.memory_gb and gpu >= spec.gpu_count:
                candidates.append((cpu, mem, gpu, str(sku)))

        if candidates:
            # smallest satisfying SKU: fewest GPUs, then CPU, then memory.
            candidates.sort(key=lambda t: (t[2], t[0], t[1]))
            return candidates[0][3]

        # Nothing satisfies the request: fall back to the largest available so
        # the run proceeds rather than hard-failing on sizing.
        biggest = max(
            machines,
            key=lambda m: (
                int(_num(_first(m, "gpu_count", "gpuCount", "gpu") or 0)),
                _num(_first(m, "cpu", "cpuCount", "cpu_count")),
            ),
        )
        sku = _first(biggest, "sku_name", "skuName", "name")
        if not sku:
            raise RuntimeError("lbg: could not resolve any SKU name")
        return str(sku)

    def _ensure_template(self, image_url: str, sku_name: str, extra_disk_gb: int) -> str:
        digest = hashlib.sha1(
            f"{image_url}|{sku_name}|{extra_disk_gb}".encode("utf-8")
        ).hexdigest()[:12]
        name = f"zero-{digest}"

        try:
            proc = self._lbg(["sdbx", "template", "ls", "--json"], timeout=60)
            if proc.returncode == 0:
                for tpl in _as_list(_loads(self._text(proc))):
                    if _first(tpl, "name", "templateName") == name:
                        return name
        except (OSError, subprocess.SubprocessError):
            pass

        args = ["sdbx", "template", "create", "--name", name,
                "--image", image_url, "--sku-name", sku_name, "--json"]
        if extra_disk_gb > 0:
            args += ["--extra-ephemeral-storage-gb", str(extra_disk_gb)]
        proc = self._lbg(args, timeout=300)
        if proc.returncode != 0:
            # A concurrent create may have won the race; accept if it now exists.
            check = self._lbg(["sdbx", "template", "ls", "--json"], timeout=60)
            if check.returncode == 0:
                for tpl in _as_list(_loads(self._text(check))):
                    if _first(tpl, "name", "templateName") == name:
                        return name
            raise RuntimeError(f"lbg sdbx template create failed: {self._text(proc, 'stderr')}")
        return name

    # -- SandboxProvider interface ----------------------------------------
    def create_sandbox(self, spec: SandboxSpec) -> SandboxHandle:
        if not self._cfg.bohrium_key:
            raise RuntimeError(
                "lbg backend selected but no Bohrium access key found "
                "(set BOHRIUM_ACCESS_KEY or bohrium_key in the dotenv file)"
            )
        image = self._pick_base_image(spec)
        sku = self._resolve_sku(spec)
        template = self._ensure_template(image, sku, self._cfg.lbg_extra_disk_gb)

        rid = self._create_with_retry(template)
        self._remote[spec.sandbox_id] = str(rid)
        self._specs[spec.sandbox_id] = spec
        try:
            self._configure_pip_mirror(spec.sandbox_id)
            self._install_trisol_cli(spec.sandbox_id)
            self._install_playground_cli(spec.sandbox_id)
            self._ensure_standard_dirs(spec.sandbox_id)
        except Exception:
            # Dependency bootstrap is part of sandbox readiness. Do not leave
            # a billed sandbox behind when a required CLI cannot be installed.
            self.destroy(spec.sandbox_id)
            raise
        return SandboxHandle(
            sandbox_id=spec.sandbox_id, backend=self.name, workspace_path=_WORKSPACE,
        )

    def _ensure_standard_dirs(self, sandbox_id: str) -> None:
        """Create Harbor / zero path conventions that stock images often omit."""
        try:
            self.exec(
                sandbox_id,
                "mkdir -p /workspace /app/outputs && chmod 777 /workspace /app /app/outputs",
                timeout=60,
            )
        except Exception:  # noqa: BLE001 - path bootstrap must never block create
            pass

    def _configure_pip_mirror(self, sandbox_id: str) -> None:
        """Bake a domestic pip mirror into the fresh sandbox so every later
        ``pip install`` is fast instead of stalling on the default pypi.org.

        Written at the user level (``~/.pip/pip.conf``), which overrides any
        image-level ``/etc/pip.conf``. Best-effort: failures never block a run.
        """
        url = (self._cfg.pip_index_url or "").strip()
        if not url:
            return
        host = urlsplit(url).hostname or ""
        body = f"[global]\\nindex-url = {url}\\ntimeout = 120\\n"
        if host:
            body += f"trusted-host = {host}\\n"
        cmd = f"mkdir -p ~/.pip && printf '{body}' > ~/.pip/pip.conf"
        try:
            self.exec(sandbox_id, cmd, timeout=60)
        except Exception:  # noqa: BLE001 - mirror setup must never break create
            pass

    def _install_trisol_cli(self, sandbox_id: str) -> None:
        """Install and verify the Trisol system CLI inside an LBG sandbox."""
        install_url = (self._cfg.trisol_install_url or "").strip()
        if not install_url.startswith("https://"):
            raise RuntimeError(
                f"ZERO_TRISOL_INSTALL_URL must be an https URL, got {install_url!r}"
            )
        install = self.exec(
            sandbox_id,
            "command -v trisol >/dev/null 2>&1 || "
            f"(curl -fsSL {shlex.quote(install_url)} | bash)",
            timeout=300,
        )
        if not install.ok:
            raise RuntimeError(
                f"Trisol installation failed in {sandbox_id}: "
                f"{(install.stderr or install.stdout)[-500:]}"
            )
        verify = self.exec(sandbox_id, "trisol version", timeout=60)
        if not verify.ok or "trisol" not in (verify.stdout or "").lower():
            raise RuntimeError(
                f"Trisol verification failed in {sandbox_id}: "
                f"{(verify.stderr or verify.stdout)[-500:]}"
            )

    def _install_playground_cli(self, sandbox_id: str) -> None:
        """Install playground CLI inside the sandbox so the Researcher can use
        ``playground task download`` / ``playground data pull`` directly.

        Trisol is installed and verified separately as a required dependency;
        this method installs Node.js and the Playground wrapper.
        Best-effort: silently skipped on any failure.
        """
        try:
            # 1. Node.js rootless into ~/.local
            self.exec(
                sandbox_id,
                "mkdir -p ~/.local && "
                "curl -fsSL https://nodejs.org/dist/v22.19.0/node-v22.19.0-linux-x64.tar.xz "
                "| tar xJ -C ~/.local --strip-components=1",
                timeout=300,
            )
        except Exception:  # noqa: BLE001
            return
        try:
            # Playground delegates its asset transfer to the verified Trisol
            # binary installed during sandbox bootstrap.
            self.exec(
                sandbox_id,
                "/home/user/.local/bin/npm install -g "
                "http://nwjs1473070.bohrium.tech:50003/packages/paper2arm-playground-cli-0.1.21.tgz",
                timeout=300,
            )
        except Exception:  # noqa: BLE001
            pass

    def _create_with_retry(self, template: str, attempts: int = 3) -> str:
        """Launch a sandbox, tolerating transient gateway timeouts.

        A 502/deadline-exceeded on create may still have created the sandbox
        server-side (image cold-pull outran the gateway). Before each retry we
        check ``sdbx list`` for a sandbox already on this template and adopt it,
        so retries never accumulate orphan (billed) sandboxes.
        """
        args = ["sdbx", "create", template,
                "--timeout", str(self._cfg.lbg_sandbox_timeout), "--json"]
        if self._cfg.lbg_project_id:
            args += ["--project-id", self._cfg.lbg_project_id]

        last = ""
        for attempt in range(1, attempts + 1):
            proc = self._lbg(args, timeout=900)
            if proc.returncode == 0:
                payload = _loads(self._text(proc)) or {}
                rid = _first(payload, "sandboxID", "sandbox_id", "sandboxId", "id")
                if rid:
                    return str(rid)
                last = f"create returned no sandbox id: {self._text(proc)[:300]}"
            else:
                last = self._error_message(proc)

            # Whether it timed out or reported transient, a sandbox may exist now.
            adopted = self._find_sandbox_for_template(template)
            if adopted:
                return adopted
            if attempt < attempts and (proc.returncode == 0 or self._is_transient(proc)):
                time.sleep(6 * attempt)  # let the image cache warm before retrying
                continue
            break
        raise RuntimeError(f"lbg sdbx create failed after {attempts} attempt(s): {last}")

    def _find_sandbox_for_template(self, template: str) -> Optional[str]:
        try:
            proc = self._lbg(["sdbx", "list", "--json"], timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        for sb in _as_list(_loads(self._text(proc))):
            tpl = _first(sb, "templateID", "templateId", "template", "templateName")
            if tpl != template:
                continue
            state = str(_first(sb, "state", "status") or "").lower()
            if state in ("", "running", "ready", "active", "healthy", "pending", "creating", "starting"):
                rid = _first(sb, "sandboxID", "sandbox_id", "sandboxId", "id")
                if rid:
                    return str(rid)
        return None

    def exec(self, sandbox_id: str, command: str, timeout: int = 600,
             workdir: Optional[str] = None, env: Optional[dict[str, str]] = None) -> ExecResult:
        rid = self._rid(sandbox_id)
        # Default to root: Harbor tasks need /app and /workspace under /, which
        # the non-root uid=1001 user cannot create on stock Bohrium images.
        args = ["sdbx", "exec", "--user", "root", "--timeout", str(timeout)]
        if workdir:
            args += ["--cwd", workdir]
        merged_env: dict[str, str] = dict(env or {})
        # Pass playground credentials so the Researcher can use playground CLI
        # inside the sandbox without extra login steps.
        playground_token = (
            os.environ.get("PLAYGROUND_TOKEN", "").strip()
            or os.environ.get("PLAYGROUND_KEY", "").strip()
        )
        if playground_token:
            merged_env.setdefault("PLAYGROUND_TOKEN", playground_token)
        for key in (
            "PLAYGROUND_API_BASE", "TRISOL_TOKEN", "TRISOL_API_URL", "TRISOL_TEAM",
        ):
            val = os.environ.get(key, "").strip()
            if val:
                merged_env.setdefault(key, val)
        # Prepend rootless Node.js / playground bin paths.
        if "PATH" not in merged_env:
            merged_env["PATH"] = (
                "/home/user/.local/bin:/home/user/.local/lib/node_modules/.bin"
                ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            )
        for k, v in merged_env.items():
            args += ["--env", f"{k}={v}"]
        args += ["--json", rid, command]
        try:
            proc = self._lbg(args, timeout=timeout + 120)
        except subprocess.TimeoutExpired:
            return ExecResult(124, "", f"timeout after {timeout}s")
        return self._parse_exec(proc)

    def _parse_exec(self, proc: subprocess.CompletedProcess) -> ExecResult:
        out, err = self._text(proc, "stdout"), self._text(proc, "stderr")
        payload = _loads(out)
        if isinstance(payload, dict):
            code = _first(payload, "exit_code", "exitCode", "code", "returncode", "return_code")
            stdout = _first(payload, "stdout", "stdOut", "output", "out")
            stderr = _first(payload, "stderr", "stdErr", "error", "err")
            return ExecResult(
                int(code) if code is not None else proc.returncode,
                str(stdout) if stdout is not None else "",
                str(stderr) if stderr is not None else (err if proc.returncode else ""),
            )
        # Not JSON-wrapped: treat the CLI's own streams as the result.
        return ExecResult(proc.returncode, out, err)

    def put_file(self, sandbox_id: str, path: str, content: bytes) -> None:
        rid = self._rid(sandbox_id)
        target = path if path.startswith("/") else f"{_WORKSPACE}/{path}"
        parent = str(PurePosixPath(target).parent)
        fd, src = tempfile.mkstemp(prefix="zero-lbg-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            self.exec(sandbox_id, f"mkdir -p {shlex.quote(parent)}", timeout=60)
            proc = self._lbg(
                ["sdbx", "files", "write", "--source", src, rid, target, "--json"], timeout=300,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"lbg sdbx files write failed: {self._text(proc, 'stderr')}")
        finally:
            os.unlink(src)

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        rid = self._rid(sandbox_id)
        target = path if path.startswith("/") else f"{_WORKSPACE}/{path}"
        fd, dst = tempfile.mkstemp(prefix="zero-lbg-")
        os.close(fd)
        try:
            proc = self._lbg(
                ["sdbx", "files", "read", rid, target, "--format", "bytes", "--output", dst],
                timeout=300,
            )
            if proc.returncode != 0:
                # Surface as FileNotFoundError so callers (e.g. conclusion read)
                # can treat a missing file as "not there" rather than a hard error.
                raise FileNotFoundError(f"lbg sdbx files read failed for {target}: "
                                        f"{self._text(proc, 'stderr')}")
            return Path(dst).read_bytes()
        finally:
            os.unlink(dst)

    def mount(self, sandbox_id: str, mount: MountSpec) -> str:
        rid = self._rid(sandbox_id)  # noqa: F841 - validates the sandbox exists
        ref = mount.ref
        target = ref.target_path()
        if ref.source and ref.source.startswith("trisol://"):
            parsed = urlsplit(ref.source)
            parts = parsed.path.strip("/").split("/")
            query = parse_qs(parsed.query)
            asset_id = parts[0]
            version = parts[1] if len(parts) > 1 else ref.version
            team = (query.get("team") or [""])[0]
            team_flag = f" -t {shlex.quote(team)}" if team else ""
            prefix = f"trisol --no-input --no-color{team_flag}"
            if ref.kind == "dataset":
                splits = query.get("split", [])
                if not splits:
                    raise RuntimeError(f"Trisol dataset URI has no splits: {ref.source}")
                commands = [
                    f"{prefix} dataset download {shlex.quote(asset_id)} {shlex.quote(version)} "
                    f"{shlex.quote(split)} --output {shlex.quote(target + '/')}"
                    for split in splits
                ]
                dl = " && ".join(commands)
            else:
                name = (query.get("name") or [asset_id])[0]
                dl = (f"{prefix} model download {shlex.quote(name + ':' + version)} "
                      f"--output {shlex.quote(target + '/')}")
            self.exec(sandbox_id, f"mkdir -p {shlex.quote(target)}", timeout=60)
            result = self.exec(sandbox_id, dl, timeout=7200)
            if not result.ok:
                raise RuntimeError(f"lbg Trisol mount failed for {ref.uri()}: {result.stderr[:400]}")
            return target

        rev = ref.version if ref.version and ref.version.lower() != "latest" else ""
        rev_flag = f" --revision {shlex.quote(rev)}" if rev else ""
        if ref.kind == "dataset":
            dl = (f"huggingface-cli download --repo-type dataset "
                  f"{shlex.quote(ref.name)}{rev_flag} --local-dir {shlex.quote(target)}")
        else:
            dl = (f"huggingface-cli download {shlex.quote(ref.name)}{rev_flag} "
                  f"--local-dir {shlex.quote(target)}")

        self.exec(sandbox_id, _PROXY_ON, timeout=120)
        try:
            self.exec(sandbox_id, f"mkdir -p {shlex.quote(target)}", timeout=60)
            self.exec(sandbox_id, "python -m pip install -q -U 'huggingface_hub[cli]'", timeout=600)
            res = self.exec(sandbox_id, dl, timeout=3600)
            if not res.ok:
                raise RuntimeError(f"lbg mount download failed for {ref.uri()}: {res.stderr[:400]}")
        finally:
            self.exec(sandbox_id, _PROXY_OFF, timeout=120)
        return target

    def snapshot(self, sandbox_id: str) -> str:
        """Submit an async ``lbg sdbx image commit`` and return a tracking digest.

        The backend call only ever returns a commit ``id`` (image build/push
        runs for minutes in the background), so this stays non-blocking and
        cheap to call from a hot path like ``publish_manifest`` -- it never
        waits for ``status`` to reach ``success``. Resolve the final
        ``imageUrl`` later with :meth:`wait_for_image` (e.g. from an offline
        "save this task's environment" step), keyed off the commit id embedded
        in the returned digest (``lbg:commit:<id>``).
        """
        rid = self._rid(sandbox_id)
        project_id = self._cfg.lbg_project_id
        if project_id:
            # LBG image names accept lowercase letters, digits and hyphens
            # only.  Logical Zero sandbox ids contain uppercase workflow
            # names and underscores (for example ``WF-7__...``), so passing
            # them through verbatim makes every commit fail validation and
            # silently fall back to a reproducibility digest.  Keep the name
            # short as well: some registry backends enforce a 63-character
            # component limit.
            raw_name = re.sub(r"[^a-z0-9-]+", "-", f"zero-{sandbox_id}".lower()).strip("-")
            identity = hashlib.sha256(sandbox_id.encode("utf-8")).hexdigest()[:8]
            name = f"{raw_name[:36].rstrip('-')}-{identity}-{int(time.time())}"
            try:
                proc = self._lbg(
                    ["sdbx", "image", "commit",
                     "--sandbox-id", rid, "--name", name, "--project-id", project_id,
                     "--json"],
                    timeout=120,
                )
                if proc.returncode == 0:
                    payload = _loads(self._text(proc)) or {}
                    # ``lbg ... --json`` wraps most commands as
                    # ``{"request": {...}, "response": {...}}``; unwrap it if
                    # present so ``response.id`` (and other command envelopes)
                    # resolve the same way.
                    body = payload.get("response", payload) if isinstance(payload, dict) else payload
                    commit_id = _first(body, "id", "commitId", "commit_id") if isinstance(body, dict) else None
                    if commit_id:
                        return f"lbg:commit:{commit_id}"
                # Non-transient failure (e.g. bad name/quota) -- fall through to
                # the pip-freeze digest below rather than raising, so a snapshot
                # never blocks environment readiness.
                if os.environ.get("ZERO_LBG_DEBUG_SNAPSHOT"):
                    print(f"[lbg snapshot debug] rc={proc.returncode} "
                          f"stdout={self._text(proc)!r} stderr={self._text(proc, 'stderr')!r}")
            except (OSError, subprocess.SubprocessError) as exc:
                if os.environ.get("ZERO_LBG_DEBUG_SNAPSHOT"):
                    print(f"[lbg snapshot debug] exception={exc!r}")
        # Fallback: no project id configured (backend requires one for
        # ``image commit``), or the commit call itself failed. Use a
        # reproducibility digest from the installed package set instead.
        freeze = self.exec(sandbox_id, "pip freeze", timeout=120)
        digest = hashlib.sha256(freeze.stdout.encode("utf-8")).hexdigest()[:16]
        return f"lbg:{digest}"

    def wait_for_image(self, commit_id: str, timeout: int = 1800,
                        poll_interval: int = 15) -> dict[str, Any]:
        """Poll ``lbg sdbx image get <commit_id>`` until a terminal status.

        Returns the raw commit record (``status``, ``imageUrl``/``errorMsg``
        once terminal). Not called from the hot path -- meant for an offline
        "materialize this run's environment as a reusable image" step, since a
        single commit's kaniko build can legitimately take up to the backend's
        own ~1800s timeout.
        """
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        while time.time() < deadline:
            proc = self._lbg(["sdbx", "image", "get", str(commit_id), "--json"], timeout=60)
            if proc.returncode == 0:
                payload = _loads(self._text(proc)) or {}
                body = payload.get("response", payload) if isinstance(payload, dict) else payload
                if isinstance(body, dict):
                    last = body
                    status = _num(_first(body, "status"))
                    if status in (2, 3):  # success | failed
                        return last
            time.sleep(poll_interval)
        last.setdefault("status", -1)
        last.setdefault("errorMsg", f"timed out after {timeout}s waiting for commit {commit_id}")
        return last

    def destroy(self, sandbox_id: str) -> None:
        rid = self._remote.get(sandbox_id)
        if rid:
            try:
                self._lbg(["sdbx", "kill", rid, "--force", "--json"], timeout=120)
            except (OSError, subprocess.SubprocessError):
                pass
        self._remote.pop(sandbox_id, None)
        self._specs.pop(sandbox_id, None)

    def info(self, sandbox_id: str) -> SandboxInfo:
        spec = self._specs.get(sandbox_id)
        cpu = spec.cpu_count if spec else 0
        mem = float(spec.memory_gb) if spec else 0.0
        gpu = spec.gpu_count if spec else 0
        running = False
        rid = self._remote.get(sandbox_id)
        if rid:
            try:
                proc = self._lbg(["sdbx", "describe", rid, "--json"], timeout=60)
                if proc.returncode == 0:
                    payload = _loads(self._text(proc)) or {}
                    if isinstance(payload, dict):
                        state = str(_first(payload, "state", "status") or "").lower()
                        running = state in ("running", "ready", "active", "healthy") or bool(
                            _first(payload, "running")
                        )
                        cpu = int(_num(_first(payload, "cpuCount", "cpu_count", "cpu") or cpu))
                        mem_mb = _num(_first(payload, "memoryMB", "memory_mb") or 0)
                        if mem_mb:
                            mem = mem_mb / 1024
            except (OSError, subprocess.SubprocessError):
                pass
        return SandboxInfo(
            sandbox_id=sandbox_id,
            running=running,
            cpu_count=cpu,
            memory_gb=round(mem, 1),
            gpu_count=gpu,
            disk_free_gb=0.0,
        )
