"""Configuration for the capture gateway.

The three upstream essentials — endpoint / model / api_key — are resolved from
(in priority order):

1. Explicit values (CLI flags), passed to ``Config.resolve``.
2. Environment variables ``CAPGW_ENDPOINT`` / ``CAPGW_MODEL`` / ``CAPGW_API_KEY``.
3. An ``--env-file`` (e.g. the project's ``llm.env``) mapping
   ``LLM_BASE_URL`` -> endpoint, ``LLM_PRO`` -> model, ``LLM_API_KEY`` -> api_key.

One running instance fronts exactly one upstream, so every captured record is
tagged with ``upstream_endpoint`` + ``model_used`` and different services are
naturally separable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


# Mapping from env-file keys to our three essentials.
_ENV_FILE_KEYS = {
    "endpoint": ("LLM_BASE_URL",),
    "model": ("LLM_PRO", "LLM_MODEL"),
    "api_key": ("LLM_API_KEY",),
}


def parse_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file.

    Supports ``export KEY=VALUE``, ``#`` comments, blank lines, and optional
    surrounding single/double quotes. Does not do shell expansion.
    """
    env_path = Path(path)
    if not env_path.is_file():
        raise ConfigError(f"env file not found: {env_path}")

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


@dataclass
class Config:
    """Resolved runtime configuration."""

    endpoint: str
    model: str
    api_key: str
    host: str = "0.0.0.0"
    port: int = 8900
    out_dir: str = "./captures"
    # Optional run name. When set, captures are stored as numbered JSON files
    # under captures/<name>/<name>_<NNNNNN>.json instead of per-session JSONL.
    name: Optional[str] = None
    # Path (relative to endpoint) used to reach the OpenAI-compatible chat API.
    chat_completions_path: str = "/v1/chat/completions"
    request_timeout: float = 600.0

    @property
    def chat_completions_url(self) -> str:
        base = self.endpoint.rstrip("/")
        path = self.chat_completions_path
        # Allow endpoint to already include the full chat path.
        if base.endswith("/chat/completions"):
            return base
        # Avoid a doubled version prefix when the endpoint already ends in /v1
        # (or similar) and the path also starts with it.
        if base.endswith("/v1") and path.startswith("/v1"):
            path = path[len("/v1"):]
        return base + path

    @classmethod
    def resolve(
        cls,
        *,
        endpoint: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        env_file: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        out_dir: Optional[str] = None,
        name: Optional[str] = None,
        chat_completions_path: Optional[str] = None,
        request_timeout: Optional[float] = None,
    ) -> "Config":
        env_file_values: dict[str, str] = {}
        if env_file:
            env_file_values = parse_env_file(env_file)

        def pick(explicit: Optional[str], env_key: str, file_keys: tuple[str, ...]) -> Optional[str]:
            if explicit:
                return explicit
            from_env = os.environ.get(env_key)
            if from_env:
                return from_env
            for fk in file_keys:
                if env_file_values.get(fk):
                    return env_file_values[fk]
            return None

        resolved_endpoint = pick(endpoint, "CAPGW_ENDPOINT", _ENV_FILE_KEYS["endpoint"])
        resolved_model = pick(model, "CAPGW_MODEL", _ENV_FILE_KEYS["model"])
        resolved_api_key = pick(api_key, "CAPGW_API_KEY", _ENV_FILE_KEYS["api_key"])

        missing = [
            name
            for name, value in (
                ("endpoint", resolved_endpoint),
                ("model", resolved_model),
                ("api_key", resolved_api_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required config: "
                + ", ".join(missing)
                + ". Provide via CLI flags (--endpoint/--model/--api-key), "
                + "env vars (CAPGW_ENDPOINT/CAPGW_MODEL/CAPGW_API_KEY), "
                + "or --env-file (LLM_BASE_URL/LLM_PRO/LLM_API_KEY)."
            )

        return cls(
            endpoint=resolved_endpoint,  # type: ignore[arg-type]
            model=resolved_model,  # type: ignore[arg-type]
            api_key=resolved_api_key,  # type: ignore[arg-type]
            host=host or os.environ.get("CAPGW_HOST", "0.0.0.0"),
            port=port or int(os.environ.get("CAPGW_PORT", "8900")),
            out_dir=out_dir or os.environ.get("CAPGW_OUT_DIR", "./captures"),
            name=name or os.environ.get("CAPGW_NAME") or None,
            chat_completions_path=chat_completions_path
            or os.environ.get("CAPGW_CHAT_PATH", "/v1/chat/completions"),
            request_timeout=request_timeout
            or float(os.environ.get("CAPGW_TIMEOUT", "600")),
        )

    def redacted(self) -> dict[str, object]:
        """Config snapshot safe for logging (api_key masked)."""
        masked = "***" if not self.api_key else self.api_key[:4] + "..." + self.api_key[-2:]
        return {
            "endpoint": self.endpoint,
            "model": self.model,
            "api_key": masked,
            "host": self.host,
            "port": self.port,
            "out_dir": self.out_dir,
            "name": self.name,
            "chat_completions_url": self.chat_completions_url,
        }
