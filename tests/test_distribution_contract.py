"""The three distributions ship as one release and share one tool contract."""

from __future__ import annotations

import tomllib
from pathlib import Path

import agent_runtime
import agent_runtime.errors
import agent_runtime.tools
import agent_runtime_client.errors
import agent_runtime_client.tools

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_CLIENT_DISTRIBUTION = "overnightworks-agent-runtime-client"
_CORE_DISTRIBUTION = "overnightworks-agent-runtime"


def _project_table(pyproject: Path) -> dict:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]


def _exact_pin(requirements: list[str], distribution: str) -> str:
    pins = [
        requirement.removeprefix(f"{distribution}==")
        for requirement in requirements
        if requirement.startswith(f"{distribution}==")
    ]
    assert len(pins) == 1, f"{distribution} must be pinned exactly once with =="
    return pins[0]


def test_core_client_service_and_pins_carry_one_lockstep_version() -> None:
    core = _project_table(_REPOSITORY_ROOT / "pyproject.toml")
    client = _project_table(_REPOSITORY_ROOT / "packages" / "client" / "pyproject.toml")
    service = _project_table(_REPOSITORY_ROOT / "packages" / "service" / "pyproject.toml")

    versions = {
        core["version"],
        client["version"],
        service["version"],
        agent_runtime.__version__,
        _exact_pin(core["dependencies"], _CLIENT_DISTRIBUTION),
        _exact_pin(service["dependencies"], _CLIENT_DISTRIBUTION),
        _exact_pin(service["dependencies"], _CORE_DISTRIBUTION),
    }

    assert len(versions) == 1, f"version literals disagree: {sorted(versions)}"


def test_core_re_exports_the_client_tool_declaration_itself() -> None:
    assert agent_runtime.tools.ToolDeclaration is agent_runtime_client.tools.ToolDeclaration


def test_core_re_exports_the_client_raise_without_chain_itself() -> None:
    assert (
        agent_runtime.errors.raise_without_chain is agent_runtime_client.errors.raise_without_chain
    )
