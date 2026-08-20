from types import SimpleNamespace

import pytest

from zero.sandbox.base import ExecResult
from zero.sandbox.lbg_provider import LbgProvider


def _provider(results: list[ExecResult]) -> tuple[LbgProvider, list[str]]:
    provider = object.__new__(LbgProvider)
    provider._cfg = SimpleNamespace(
        trisol_install_url="https://trisol.dp.tech/install.sh"
    )
    commands: list[str] = []

    def execute(_sandbox_id: str, command: str, timeout: int = 0) -> ExecResult:
        commands.append(command)
        return results.pop(0)

    provider.exec = execute
    return provider, commands


def test_install_trisol_cli_installs_and_verifies() -> None:
    provider, commands = _provider([
        ExecResult(0, "installed", ""),
        ExecResult(0, "trisol version v0.5.13", ""),
    ])

    provider._install_trisol_cli("sandbox-1")

    assert "https://trisol.dp.tech/install.sh" in commands[0]
    assert commands[1] == "trisol version"


def test_install_trisol_cli_fails_closed() -> None:
    provider, _ = _provider([ExecResult(1, "", "network unavailable")])

    with pytest.raises(RuntimeError, match="Trisol installation failed"):
        provider._install_trisol_cli("sandbox-1")


def test_install_trisol_cli_requires_https() -> None:
    provider, _ = _provider([])
    provider._cfg.trisol_install_url = "http://unsafe.example/install.sh"

    with pytest.raises(RuntimeError, match="must be an https URL"):
        provider._install_trisol_cli("sandbox-1")
