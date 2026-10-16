"""What the model catalog lists, driven through fake CLIs configured as the binaries.

Pins the contract of ``list_provider_models`` (#14, E1-E4 and E6): every route
lists the names a turn on it can take as its model, never an empty list, and a
CLI route starts its CLI once per listing.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from provider_test_support import (
    CLAUDE_MODEL_CATALOG_STDOUT,
    GROK_LOGGED_IN,
    fake_cli_executable,
    override_provider_runtime,
)

from agent_providers.catalog import (
    ProviderReady,
    ProviderSetupMethod,
    provider_configuration,
    ProviderRoute,
    ProviderRouteReadinessState,
    list_provider_models,
    probe_provider_route,
)
from agent_providers.constants import ProviderName
from agent_providers.errors import (
    ProviderModelCatalogUnavailableError,
    ProviderUnavailableError,
    SafeRouteReasonCode,
)
from agent_providers.process import clear_agent_cli_caches

CLAUDE_MODEL_CATALOG_ALIASES = sorted([
    "sonnet", "opus", "haiku", "fable", "best", "sonnet[1m]", "opus[1m]",
    "fable[1m]", "opusplan", "default",
])
CLAUDE_LOGGED_IN = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
CLAUDE_LOGGED_OUT = json.dumps({"loggedIn": False})
CLAUDE_MODEL_COMMAND = ("-p", "/model")
CLAUDE_MODEL_CATALOG_NAMING_NO_MODEL = "Usage: /model <name>. Available: or a full model ID.\n"
CLAUDE_STATUS_COMMAND = ("auth", "status")
CODEX_MODEL_COMMAND = ("debug", "models")
GROK_LOGGED_IN_OFFERING_ONLY_DEEPSEEK = (
    "You are logged in with grok.com.\n\nAvailable models:\n  * deepseek-v4-flash (default)\n"
)


@pytest.fixture(autouse=True)
def _fresh_cli_probes():
    clear_agent_cli_caches()
    yield
    clear_agent_cli_caches()


def _codex_models(*slugs_with_visibility: tuple[str, str, int]) -> str:
    return json.dumps({"models": [
        {"slug": slug, "visibility": visibility, "priority": priority}
        for slug, visibility, priority in slugs_with_visibility
    ]})


def _configure_cli(
    tmp_path: Path,
    provider: str,
    answers: dict[tuple[str, ...], str],
    calls_log: Path | None = None,
) -> None:
    binary = fake_cli_executable(tmp_path, answers, calls_log=calls_log)
    override_provider_runtime(**{f"{provider}_cli_binary": str(binary)})


CLI_OFFERS = [
    pytest.param(
        "claude", {CLAUDE_MODEL_COMMAND: CLAUDE_MODEL_CATALOG_STDOUT},
        CLAUDE_MODEL_CATALOG_ALIASES, id="claude-aliases-without-the-full-id-clause",
    ),
    pytest.param(
        "grok", {("models",): GROK_LOGGED_IN},
        ["grok-4.5", "grok-4.6"], id="grok-chat-models-sorted",
    ),
    pytest.param(
        "codex",
        {CODEX_MODEL_COMMAND: _codex_models(
            ("gpt-5.5", "list", 20), ("gpt-hidden", "hide", 1), ("gpt-5.6", "list", 10),
        )},
        ["gpt-5.6", "gpt-5.5"], id="codex-visible-slugs-in-priority-order",
    ),
]

CLI_OFFERS_NOTHING_USABLE = [
    pytest.param(
        "claude", {CLAUDE_MODEL_COMMAND: CLAUDE_MODEL_CATALOG_NAMING_NO_MODEL},
        id="claude-prints-only-the-full-id-clause",
    ),
    pytest.param(
        "claude", {CLAUDE_MODEL_COMMAND: "Current model: `Opus 5 (1M context)`\n"},
        id="claude-prints-no-available-line",
    ),
    pytest.param(
        "claude", {},
        id="claude-exits-non-zero",
    ),
    pytest.param(
        "grok", {("models",): GROK_LOGGED_IN_OFFERING_ONLY_DEEPSEEK},
        id="grok-offers-only-a-model-without-the-chat-prefix",
    ),
    pytest.param(
        "codex", {CODEX_MODEL_COMMAND: _codex_models(("gpt-hidden", "hide", 1))},
        id="codex-hides-every-slug",
    ),
]


@pytest.mark.parametrize(("provider", "answers", "expected_names"), CLI_OFFERS)
def test_each_cli_route_lists_the_names_its_cli_offers(
    tmp_path: Path, provider: str, answers: dict[tuple[str, ...], str], expected_names: list[str],
) -> None:
    _configure_cli(tmp_path, provider, answers)

    assert list_provider_models(provider, ProviderRoute.CLI) == expected_names


@pytest.mark.parametrize(("provider", "answers"), CLI_OFFERS_NOTHING_USABLE)
def test_a_cli_that_offers_no_usable_name_is_a_catalogue_failure(
    tmp_path: Path, provider: str, answers: dict[tuple[str, ...], str],
) -> None:
    _configure_cli(tmp_path, provider, answers)

    with pytest.raises(ProviderModelCatalogUnavailableError) as failure:
        list_provider_models(provider, ProviderRoute.CLI)

    assert failure.value.reason.code is SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR


def test_a_ready_claude_cli_route_carries_its_aliases(tmp_path: Path) -> None:
    _configure_cli(tmp_path, "claude", {
        CLAUDE_STATUS_COMMAND: CLAUDE_LOGGED_IN,
        CLAUDE_MODEL_COMMAND: CLAUDE_MODEL_CATALOG_STDOUT,
    })

    snapshot = probe_provider_route("claude", ProviderRoute.CLI)

    assert snapshot.readiness is ProviderRouteReadinessState.READY
    assert list(snapshot.models) == CLAUDE_MODEL_CATALOG_ALIASES
    assert snapshot.catalogue_failure is None


def test_a_claude_cli_route_with_a_broken_catalogue_is_disturbed_and_lists_nothing(
    tmp_path: Path,
) -> None:
    _configure_cli(tmp_path, "claude", {
        CLAUDE_STATUS_COMMAND: CLAUDE_LOGGED_IN,
        CLAUDE_MODEL_COMMAND: CLAUDE_MODEL_CATALOG_NAMING_NO_MODEL,
    })

    snapshot = probe_provider_route("claude", ProviderRoute.CLI)

    assert snapshot.readiness is ProviderRouteReadinessState.DISTURBED
    assert snapshot.models == ()
    assert snapshot.catalogue_failure.message == "Model catalogue response was invalid."


def test_a_logged_out_claude_cli_route_is_not_configured_and_lists_nothing(
    tmp_path: Path,
) -> None:
    _configure_cli(tmp_path, "claude", {
        CLAUDE_STATUS_COMMAND: CLAUDE_LOGGED_OUT,
        CLAUDE_MODEL_COMMAND: CLAUDE_MODEL_CATALOG_STDOUT,
    })

    snapshot = probe_provider_route("claude", ProviderRoute.CLI)

    assert snapshot.readiness is ProviderRouteReadinessState.NOT_CONFIGURED
    assert snapshot.models == ()


def test_listing_the_claude_cli_starts_it_once(tmp_path: Path) -> None:
    starts = tmp_path / "starts"
    _configure_cli(
        tmp_path, "claude", {CLAUDE_MODEL_COMMAND: CLAUDE_MODEL_CATALOG_STDOUT}, calls_log=starts,
    )

    list_provider_models("claude", ProviderRoute.CLI)

    assert starts.read_text().splitlines() == ["-p /model"]


@pytest.mark.parametrize(("stored", "is_provider"), [
    ("claude", True), ("grok", True), ("codex", True), ("bob", False), ("", False),
])
def test_a_stored_provider_string_is_found_by_value(stored: str, is_provider: bool) -> None:
    assert (stored in ProviderName) is is_provider


@pytest.mark.parametrize("route", [ProviderRoute.CLI, ProviderRoute.API])
def test_listing_models_for_an_unknown_provider_is_refused_by_name(route: ProviderRoute) -> None:
    with pytest.raises(ProviderUnavailableError) as refusal:
        list_provider_models("bob", route)

    assert str(refusal.value) == "Unknown provider 'bob'"


API_ROUTES = [
    pytest.param("claude", "anthropic_api_key", ["claude-opus-5", "text-embedding"],
                 ["claude-opus-5"], id="claude"),
    pytest.param("grok", "xai_api_key", ["grok-4.6", "grok-4.6-image"],
                 ["grok-4.6"], id="grok"),
    pytest.param("codex", "openai_api_key", ["gpt-5.6", "whisper-1"],
                 ["gpt-5.6"], id="codex"),
]


@pytest.mark.parametrize(("provider", "key_setting", "offered", "expected"), API_ROUTES)
def test_each_api_route_lists_the_chat_models_its_api_offers(
    monkeypatch: pytest.MonkeyPatch, provider: str, key_setting: str,
    offered: list[str], expected: list[str],
) -> None:
    override_provider_runtime(**{key_setting: SecretStr("a-key")})
    monkeypatch.setattr(httpx, "get", lambda url, **_: httpx.Response(
        200, json={"data": [{"id": model_id} for model_id in offered]},
        request=httpx.Request("GET", url),
    ))

    assert list_provider_models(provider, ProviderRoute.API) == expected


@pytest.mark.parametrize(("provider", "key_setting", "environment_key"), [
    ("grok", "xai_api_key", "XAI_API_KEY"),
    ("codex", "openai_api_key", "OPENAI_API_KEY"),
])
def test_a_provider_with_an_api_key_is_ready_through_the_api_key(
    provider: str, key_setting: str, environment_key: str,
) -> None:
    override_provider_runtime(**{key_setting: SecretStr("a-key")})

    configuration = provider_configuration(provider, frozenset())

    assert isinstance(configuration, ProviderReady)
    assert configuration.method is ProviderSetupMethod.API_KEY
    assert configuration.environment_key == environment_key


@pytest.mark.parametrize("provider", ["claude", "grok", "codex"])
def test_a_cli_route_without_a_binary_is_not_configured(provider: str) -> None:
    snapshot = probe_provider_route(provider, ProviderRoute.CLI)

    assert snapshot.readiness is ProviderRouteReadinessState.NOT_CONFIGURED
