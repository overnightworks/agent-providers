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
from provider_test_support import (
    A_CODEX_LOGIN_WITH_INVALID_UTF8,
    CLAUDE_MODEL_CATALOG_STDOUT,
    GROK_LOGGED_IN,
    a_codex_login,
    a_fifo,
    call_within_deadline,
    fake_cli_executable,
    override_provider_runtime,
)
from pydantic import SecretStr

from agent_providers.catalog import (
    ProviderNotLoggedIn,
    ProviderReady,
    ProviderRoute,
    ProviderRouteReadinessState,
    ProviderSetupMethod,
    list_provider_models,
    probe_provider_route,
    provider_configuration,
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
CODEX_LOGGED_IN = "Logged in using ChatGPT"
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


def test_the_provider_names_are_public_in_their_documented_order() -> None:
    assert [(member.name, member.value) for member in ProviderName] == [
        ("CLAUDE", "claude"), ("GROK", "grok"), ("CODEX", "codex"),
    ]


_UNKNOWN_PROVIDER_CALLS = {
    "list-cli": lambda: list_provider_models("bob", ProviderRoute.CLI),
    "list-api": lambda: list_provider_models("bob", ProviderRoute.API),
    "probe": lambda: probe_provider_route("bob", ProviderRoute.API),
    "configuration": lambda: provider_configuration("bob", frozenset()),
}


@pytest.mark.parametrize(
    "ask", _UNKNOWN_PROVIDER_CALLS.values(), ids=_UNKNOWN_PROVIDER_CALLS.keys(),
)
def test_asking_the_catalog_about_an_unknown_provider_is_refused_by_name(ask) -> None:
    with pytest.raises(ProviderUnavailableError) as refusal:
        ask()

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


SIGNED_IN_CLI_ANSWERS = {
    "claude": {
        CLAUDE_STATUS_COMMAND: CLAUDE_LOGGED_IN,
        CLAUDE_MODEL_COMMAND: CLAUDE_MODEL_CATALOG_STDOUT,
    },
    "grok": {("models",): GROK_LOGGED_IN},
    "codex": {
        ("login", "status"): CODEX_LOGGED_IN,
        CODEX_MODEL_COMMAND: _codex_models(("gpt-5.6", "list", 10)),
    },
}


@pytest.mark.parametrize("provider", ["claude", "grok", "codex"])
def test_a_signed_in_cli_route_is_not_configured_once_its_binary_is_gone(
    provider: str, tmp_path: Path,
) -> None:
    fake_cli_executable(tmp_path, SIGNED_IN_CLI_ANSWERS[provider])
    override_provider_runtime(
        cli_binary_search_path=(tmp_path,), **{f"{provider}_cli_binary": "fake-cli"},
    )
    with_binary = probe_provider_route(provider, ProviderRoute.CLI)
    (tmp_path / "fake-cli").unlink()
    clear_agent_cli_caches()

    without_binary = probe_provider_route(provider, ProviderRoute.CLI)

    assert with_binary.readiness is ProviderRouteReadinessState.READY
    assert without_binary.readiness is ProviderRouteReadinessState.NOT_CONFIGURED
    assert without_binary.reason.code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED


@pytest.mark.parametrize("provider", ["claude", "grok", "codex"])
def test_a_cli_route_whose_private_home_cannot_be_made_is_disturbed(
    provider: str, tmp_path: Path,
) -> None:
    removed_root = tmp_path / "removed-root"
    removed_root.mkdir(mode=0o700)
    override_provider_runtime(cli_working_directory_root=removed_root)
    removed_root.rmdir()
    clear_agent_cli_caches()

    snapshot = probe_provider_route(provider, ProviderRoute.CLI)

    assert snapshot.readiness is ProviderRouteReadinessState.DISTURBED
    assert snapshot.reason.code is SafeRouteReasonCode.CLI_BINARY_UNAVAILABLE


A_CODEX_LOGIN_WITHOUT_ITS_ID_TOKEN = b'{"auth_mode":"chatgpt","tokens":{"access_token":"a"}}'


def _codex_setup(tmp_path: Path, document: bytes) -> object:
    auth_file = tmp_path / "codex-auth.json"
    auth_file.write_bytes(document)
    override_provider_runtime(codex_cli_auth_file=auth_file)
    return provider_configuration("codex", frozenset({ProviderSetupMethod.CODEX_CLI}))


def test_codex_setup_is_ready_through_a_complete_login(tmp_path: Path) -> None:
    configuration = _codex_setup(tmp_path, a_codex_login())

    assert isinstance(configuration, ProviderReady)
    assert configuration.method is ProviderSetupMethod.CODEX_CLI


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(A_CODEX_LOGIN_WITHOUT_ITS_ID_TOKEN, id="only-an-access-token"),
        pytest.param(a_codex_login(tokens={"account_id": 7}), id="numeric-account-id"),
        pytest.param(A_CODEX_LOGIN_WITH_INVALID_UTF8, id="invalid-encoding"),
    ],
)
def test_codex_setup_needs_a_usable_login(tmp_path: Path, document: bytes) -> None:
    assert isinstance(_codex_setup(tmp_path, document), ProviderNotLoggedIn)


def test_an_unreadable_codex_login_leaves_the_api_key_in_charge(tmp_path: Path) -> None:
    override_provider_runtime(openai_api_key=SecretStr("a-key"))

    configuration = _codex_setup(tmp_path, A_CODEX_LOGIN_WITH_INVALID_UTF8)

    assert isinstance(configuration, ProviderReady)
    assert configuration.method is ProviderSetupMethod.API_KEY


def test_a_wrong_typed_codex_login_is_named_in_the_setup_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="agent_providers.catalog")

    _codex_setup(tmp_path, a_codex_login(tokens={"account_id": 7}))

    assert "codex CLI probe unavailable: CliLoginUnusableError" in caplog.text


def test_codex_setup_does_not_follow_a_link_to_a_complete_login(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(a_codex_login())
    link = tmp_path / "codex-auth.json"
    link.symlink_to(target)
    override_provider_runtime(codex_cli_auth_file=link)

    configuration = provider_configuration("codex", frozenset({ProviderSetupMethod.CODEX_CLI}))

    assert isinstance(configuration, ProviderNotLoggedIn)


@pytest.mark.parametrize(
    "make_source",
    [a_fifo, lambda _directory: Path("/dev/null")],
    ids=("fifo", "device"),
)
def test_the_codex_login_probe_refuses_a_source_that_is_not_a_plain_file(
    tmp_path: Path, make_source,
) -> None:
    calls_log = tmp_path / "calls"
    fake_cli_executable(tmp_path, {("login", "status"): CODEX_LOGGED_IN}, calls_log=calls_log)
    override_provider_runtime(
        cli_binary_search_path=(tmp_path,), codex_cli_binary="fake-cli",
        codex_cli_auth_file=make_source(tmp_path),
    )

    snapshot = call_within_deadline(probe_provider_route, "codex", ProviderRoute.CLI)

    assert snapshot.readiness is ProviderRouteReadinessState.DISTURBED
    assert snapshot.reason.code is SafeRouteReasonCode.CLI_BINARY_UNAVAILABLE
    assert not calls_log.exists()
