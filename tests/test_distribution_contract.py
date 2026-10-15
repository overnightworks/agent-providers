"""The two distributions ship as one release and share one tool contract."""

from __future__ import annotations

import tomllib
from pathlib import Path

import agent_client.tools
import agent_providers
import agent_providers.tools

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_CLIENT_DISTRIBUTION = "overnightworks-agent-client"


def _project_table(pyproject: Path) -> dict:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]


def _exact_pin(requirements: list[str], distribution: str) -> str:
    pins = [
        requirement.removeprefix(f"{distribution}==")
        for requirement in requirements
        if requirement.startswith(f"{distribution}==")
    ]
    assert len(pins) == 1, f"core must pin {distribution} exactly once with =="
    return pins[0]


def test_core_client_and_pin_carry_one_lockstep_version() -> None:
    core = _project_table(_REPOSITORY_ROOT / "pyproject.toml")
    client = _project_table(_REPOSITORY_ROOT / "packages" / "agent-client" / "pyproject.toml")

    versions = {
        core["version"],
        client["version"],
        agent_providers.__version__,
        _exact_pin(core["dependencies"], _CLIENT_DISTRIBUTION),
    }

    assert len(versions) == 1, f"version literals disagree: {sorted(versions)}"


def test_core_re_exports_the_client_tool_declaration_itself() -> None:
    assert agent_providers.tools.ToolDeclaration is agent_client.tools.ToolDeclaration
