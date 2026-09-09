"""The configuration a host installs, cut along the two ways into the library."""

from __future__ import annotations

from pathlib import Path

import pytest
from provider_test_support import override_provider_runtime

from agent_providers.config import (
    ProviderRuntimeConfig,
    TurnRuntimeNotConfiguredError,
    current_config,
    current_turn_config,
)
from agent_providers.process import grok_cli_status


def _catalog_only_runtime(tmp_path: Path) -> ProviderRuntimeConfig:
    return ProviderRuntimeConfig(
        claude_cli_binary="/bin/sh",
        grok_cli_binary="/bin/sh",
        codex_cli_binary="/bin/sh",
        cli_binary_search_path=(Path("/usr/bin"),),
        claude_cli_auth_file=tmp_path / "claude.json",
        grok_cli_auth_file=tmp_path / "grok.json",
        codex_cli_auth_file=tmp_path / "codex.json",
        cli_working_directory_root=tmp_path,
    )


def test_a_catalog_host_configures_without_inventing_a_turn_fact(tmp_path: Path) -> None:
    """A host that only asks what a provider offers has no chat model to name."""
    catalog_only = _catalog_only_runtime(tmp_path)
    override_provider_runtime(**catalog_only.model_dump())

    assert current_config().turns is None
    assert grok_cli_status().login.logged_in is False


def test_a_turn_under_a_catalog_only_configuration_fails_loudly(tmp_path: Path) -> None:
    override_provider_runtime(**_catalog_only_runtime(tmp_path).model_dump())

    with pytest.raises(TurnRuntimeNotConfiguredError, match="catalog only"):
        current_turn_config()
