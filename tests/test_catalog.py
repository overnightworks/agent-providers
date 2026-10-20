"""What the model catalog lists, driven through fake CLIs configured as the binaries.

Pins the contract of ``list_provider_models`` (#14, E1-E4 and E6): every route
lists the names a turn on it can take as its model, never an empty list, and a
CLI route starts its CLI once per listing.
"""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import json
import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from provider_test_support import (
    A_CODEX_LOGIN_WITH_INVALID_UTF8,
    CLAUDE_MODEL_CATALOG_STDOUT,
    CLI_RUNTIME_OVERRIDES,
    GROK_LOGGED_IN,
    SOUND_TOKEN_FILES,
    TOKEN_CANARY,
    UNSOUND_TOKEN_FILES,
    a_codex_login,
    a_fifo,
    assert_claude_run_switches,
    call_within_deadline,
    fake_cli_executable,
    fake_cli_process,
    install_cli_door,
    override_claude_runtime,
    override_codex_runtime,
    override_provider_runtime,
)
from pydantic import SecretStr

from agent_providers import catalog, process
from agent_providers.catalog import (
    ProviderCliUnavailable,
    ProviderNeedsKey,
    ProviderNotLoggedIn,
    ProviderReady,
    ProviderRoute,
    ProviderRouteReadinessState,
    ProviderSetupMethod,
    list_provider_models,
    probe_provider_route,
    provider_configuration,
)
from agent_providers.config import ProviderNotConfiguredError, current_claude_runtime
from agent_providers.constants import CLAUDE_OAUTH_TOKEN_VARIABLE, ProviderName
from agent_providers.errors import (
    ProviderModelCatalogUnavailableError,
    ProviderUnavailableError,
    SafeRouteReasonCode,
)
from agent_providers.process import (
    AgentCliUnavailableError,
    CliEnvironmentCause,
    CliEnvironmentUnavailableError,
    CliLoginUnusableError,
    clear_agent_cli_caches,
    codex_cli_login,
    codex_cli_login_is_usable,
    codex_cli_model_catalog,
    grok_cli_status,
)
from agent_providers.spawn import ChildProcess

CLAUDE_MODEL_CATALOG_ALIASES = sorted([
    "sonnet", "opus", "haiku", "fable", "best", "sonnet[1m]", "opus[1m]",
    "fable[1m]", "opusplan", "default",
])
CLAUDE_LOGGED_IN = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
CODEX_LOGGED_IN = "Logged in using ChatGPT"
CLAUDE_LOGGED_OUT = json.dumps({"loggedIn": False})
CLAUDE_MODEL_COMMAND = ("-p", "/model", "--no-session-persistence")
CLAUDE_MODEL_CATALOG_NAMING_NO_MODEL = "Usage: /model <name>. Available: or a full model ID.\n"
CLAUDE_STATUS_COMMAND = ("auth", "status")
CODEX_MODEL_COMMAND = ("debug", "models")
GROK_LOGGED_OUT = "You are not authenticated.\n\nAvailable models:\n  * grok-4.6 (default)\n"
GROK_LOGGED_IN_LISTING_NO_MODEL = "You are logged in with grok.com.\n"
GROK_WITHOUT_A_LOGIN_MARKER = "Session ready.\n"
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
    CLI_RUNTIME_OVERRIDES[provider](binary=str(binary))


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


def test_a_ready_claude_cli_route_names_no_default_though_the_cli_states_one(
    tmp_path: Path,
) -> None:
    calls = tmp_path / "calls"
    _configure_cli(tmp_path, "claude", {
        CLAUDE_STATUS_COMMAND: CLAUDE_LOGGED_IN,
        CLAUDE_MODEL_COMMAND: CLAUDE_MODEL_CATALOG_STDOUT,
    }, calls_log=calls)

    snapshot = probe_provider_route("claude", ProviderRoute.CLI)

    assert snapshot.readiness is ProviderRouteReadinessState.READY
    assert list(snapshot.models) == CLAUDE_MODEL_CATALOG_ALIASES
    assert snapshot.catalogue_failure is None
    assert snapshot.default_model_label is None
    assert calls.read_text().splitlines() == ["auth status", " ".join(CLAUDE_MODEL_COMMAND)]


@pytest.mark.parametrize("token_file", (*SOUND_TOKEN_FILES, *UNSOUND_TOKEN_FILES))
def test_the_claude_cli_listing_gets_no_login_and_comes_whatever_the_token_file_holds(
    token_file, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    source = current_claude_runtime().token_file
    token_file(source, tmp_path)
    _configure_cli(tmp_path, "claude", {})
    caplog.set_level(logging.DEBUG)
    started: list[tuple[ChildProcess, list[Path]]] = []

    def door(child: ChildProcess, *, stderr):
        started.append((child, list(Path(child.environment["HOME"]).iterdir())))
        return fake_cli_process(CLAUDE_MODEL_CATALOG_STDOUT.encode())

    install_cli_door(monkeypatch, door)

    listed = list_provider_models("claude", ProviderRoute.CLI)

    [(child, entries_at_the_start)] = started
    assert listed == CLAUDE_MODEL_CATALOG_ALIASES
    assert child.arguments == CLAUDE_MODEL_COMMAND
    assert_claude_run_switches(child)
    assert CLAUDE_OAUTH_TOKEN_VARIABLE not in child.environment
    assert entries_at_the_start == []
    assert TOKEN_CANARY not in caplog.text


def _stub_a_ready_api_listing(
    monkeypatch: pytest.MonkeyPatch, key_setting: str, listed_id: str,
) -> None:
    override_provider_runtime(**{key_setting: SecretStr("a-key")})
    monkeypatch.setattr("agent_providers.catalog._anthropic_sdk_available", lambda: True)
    monkeypatch.setattr(httpx, "get", lambda url, **_: httpx.Response(
        200, json={"data": [{"id": listed_id}]}, request=httpx.Request("GET", url),
    ))


def _a_ready_cli_route(provider: str):
    def arrange(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> ProviderRoute:
        _configure_cli(tmp_path, provider, SIGNED_IN_CLI_ANSWERS[provider])
        return ProviderRoute.CLI
    return arrange


def _a_ready_api_route(provider: str, key_setting: str, listed_id: str):
    def arrange(_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProviderRoute:
        _stub_a_ready_api_listing(monkeypatch, key_setting, listed_id)
        return ProviderRoute.API
    return arrange


def _a_logged_out_claude_cli_route(
    tmp_path: Path, _monkeypatch: pytest.MonkeyPatch,
) -> ProviderRoute:
    _configure_cli(tmp_path, "claude", {
        CLAUDE_STATUS_COMMAND: CLAUDE_LOGGED_OUT,
        CLAUDE_MODEL_COMMAND: CLAUDE_MODEL_CATALOG_STDOUT,
    })
    return ProviderRoute.CLI


def _a_claude_cli_route_with_a_broken_catalogue(
    tmp_path: Path, _monkeypatch: pytest.MonkeyPatch,
) -> ProviderRoute:
    _configure_cli(tmp_path, "claude", {
        CLAUDE_STATUS_COMMAND: CLAUDE_LOGGED_IN,
        CLAUDE_MODEL_COMMAND: CLAUDE_MODEL_CATALOG_NAMING_NO_MODEL,
    })
    return ProviderRoute.CLI


def _a_claude_cli_route_without_a_binary(
    tmp_path: Path, _monkeypatch: pytest.MonkeyPatch,
) -> ProviderRoute:
    override_claude_runtime(binary="missing-cli")
    override_provider_runtime(cli_binary_search_path=(tmp_path,))
    return ProviderRoute.CLI


_READY = ProviderRouteReadinessState.READY
_DISTURBED = ProviderRouteReadinessState.DISTURBED

ROUTES_WITHOUT_A_STATED_DEFAULT = [
    pytest.param(_a_ready_cli_route("codex"), "codex", _READY, id="codex-cli"),
    pytest.param(_a_ready_cli_route("grok"), "grok", _READY, id="grok-cli"),
    pytest.param(
        _a_logged_out_claude_cli_route, "claude", ProviderRouteReadinessState.NOT_CONFIGURED,
        id="claude-logged-out",
    ),
    pytest.param(
        _a_claude_cli_route_without_a_binary, "claude", _DISTURBED,
        id="claude-binary-gone",
    ),
    pytest.param(
        _a_claude_cli_route_with_a_broken_catalogue, "claude", _DISTURBED,
        id="claude-broken-catalogue",
    ),
    pytest.param(
        _a_ready_api_route("claude", "anthropic_api_key", "claude-opus-5"), "claude", _READY,
        id="claude-api",
    ),
    pytest.param(
        _a_ready_api_route("grok", "xai_api_key", "grok-4.6"), "grok", _READY, id="grok-api",
    ),
    pytest.param(
        _a_ready_api_route("codex", "openai_api_key", "gpt-5.6"), "codex", _READY,
        id="codex-api",
    ),
]


@pytest.mark.parametrize(("arrange", "provider", "readiness"), ROUTES_WITHOUT_A_STATED_DEFAULT)
def test_a_route_without_a_stated_default_names_none(
    arrange, provider: str, readiness: ProviderRouteReadinessState,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = arrange(tmp_path, monkeypatch)

    snapshot = probe_provider_route(provider, route)

    assert snapshot.readiness is readiness
    assert snapshot.default_model_label is None


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

    assert starts.read_text().splitlines() == [" ".join(CLAUDE_MODEL_COMMAND)]


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
    ("claude", "anthropic_api_key", "ANTHROPIC_API_KEY"),
    ("grok", "xai_api_key", "XAI_API_KEY"),
    ("codex", "openai_api_key", "OPENAI_API_KEY"),
])
def test_an_api_key_makes_a_provider_ready_without_its_cli_block(
    provider: str, key_setting: str, environment_key: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "_anthropic_sdk_available", lambda: True)
    override_provider_runtime(
        claude=None, grok=None, codex=None, **{key_setting: SecretStr("a-key")},
    )

    configuration = provider_configuration(provider, frozenset())

    assert isinstance(configuration, ProviderReady)
    assert configuration.method is ProviderSetupMethod.API_KEY
    assert configuration.environment_key == environment_key


@pytest.mark.parametrize(
    ("provider", "ask_catalog"),
    [
        pytest.param("claude", lambda: provider_configuration("claude", frozenset()), id="claude"),
        pytest.param("grok", lambda: provider_configuration("grok", frozenset()), id="grok"),
        pytest.param(
            "grok", lambda: probe_provider_route("grok", ProviderRoute.CLI), id="grok-probe",
        ),
        pytest.param(
            "grok", lambda: list_provider_models("grok", ProviderRoute.CLI), id="grok-models",
        ),
        pytest.param("grok", grok_cli_status, id="grok-status"),
        pytest.param("codex", lambda: provider_configuration("codex", frozenset()), id="codex"),
        pytest.param(
            "codex", lambda: probe_provider_route("codex", ProviderRoute.CLI), id="codex-probe",
        ),
        pytest.param(
            "codex", lambda: list_provider_models("codex", ProviderRoute.CLI), id="codex-models",
        ),
        pytest.param("codex", codex_cli_login, id="codex-login"),
        pytest.param("codex", codex_cli_login_is_usable, id="codex-login-usable"),
        pytest.param("codex", codex_cli_model_catalog, id="codex-model-catalog"),
    ],
)
def test_a_host_without_a_providers_block_is_refused_not_reported_as_logged_out(
    provider: str, ask_catalog: Callable[[], object], spawned_children: list[str],
) -> None:
    override_provider_runtime(**{provider: None})

    with pytest.raises(ProviderNotConfiguredError) as refused:
        ask_catalog()

    assert (refused.value.provider, refused.value.block) == (provider, provider)
    assert spawned_children == []


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
def test_a_signed_in_cli_route_is_disturbed_once_its_binary_is_gone(
    provider: str, tmp_path: Path,
) -> None:
    fake_cli_executable(tmp_path, SIGNED_IN_CLI_ANSWERS[provider])
    override_provider_runtime(cli_binary_search_path=(tmp_path,))
    CLI_RUNTIME_OVERRIDES[provider](binary="fake-cli")
    with_binary = probe_provider_route(provider, ProviderRoute.CLI)
    (tmp_path / "fake-cli").unlink()
    clear_agent_cli_caches()

    without_binary = probe_provider_route(provider, ProviderRoute.CLI)

    assert with_binary.readiness is ProviderRouteReadinessState.READY
    assert without_binary.readiness is ProviderRouteReadinessState.DISTURBED
    assert without_binary.reason.code is SafeRouteReasonCode.CLI_UNAVAILABLE


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
    assert snapshot.reason.code is SafeRouteReasonCode.CLI_UNAVAILABLE


LOGIN_FAULT_ROUTES = [
    pytest.param(
        "claude", {CLAUDE_STATUS_COMMAND: CLAUDE_LOGGED_OUT}, id="claude-logged-out",
    ),
    pytest.param("grok", {("models",): GROK_LOGGED_OUT}, id="grok-logged-out"),
    pytest.param(
        "grok", {("models",): GROK_WITHOUT_A_LOGIN_MARKER}, id="grok-status-without-a-marker",
    ),
    pytest.param(
        "codex", {("login", "status"): "Session ready.\n"}, id="codex-status-without-a-marker",
    ),
]


@pytest.mark.parametrize(("provider", "answers"), LOGIN_FAULT_ROUTES)
def test_a_cli_route_without_a_login_is_not_configured(
    tmp_path: Path, provider: str, answers: dict[tuple[str, ...], str],
) -> None:
    _configure_cli(tmp_path, provider, answers)

    snapshot = probe_provider_route(provider, ProviderRoute.CLI)

    assert snapshot.readiness is ProviderRouteReadinessState.NOT_CONFIGURED
    assert snapshot.reason.code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED


def test_a_grok_cli_route_that_is_signed_in_and_lists_no_model_is_a_catalogue_failure(
    tmp_path: Path,
) -> None:
    _configure_cli(tmp_path, "grok", {("models",): GROK_LOGGED_IN_LISTING_NO_MODEL})

    snapshot = probe_provider_route("grok", ProviderRoute.CLI)

    assert snapshot.readiness is ProviderRouteReadinessState.DISTURBED
    assert snapshot.reason.code is SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR


FAULTS_BY_MEANING = [
    pytest.param(
        CliEnvironmentUnavailableError(
            "no binary", cause=CliEnvironmentCause.BINARY_MISSING,
        ),
        SafeRouteReasonCode.CLI_UNAVAILABLE,
        id="environment",
    ),
    pytest.param(
        CliLoginUnusableError("no login"), SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED,
        id="login",
    ),
    pytest.param(
        AgentCliUnavailableError("no model"), SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR,
        id="base",
    ),
]


@pytest.mark.parametrize(
    ("provider", "probe_name"),
    [("grok", "grok_cli_status"), ("codex", "codex_cli_model_catalog")],
)
@pytest.mark.parametrize(("fault", "expected_code"), FAULTS_BY_MEANING)
def test_a_cli_listing_names_the_meaning_of_the_fault_that_stopped_it(
    monkeypatch: pytest.MonkeyPatch, provider: str, probe_name: str,
    fault: AgentCliUnavailableError, expected_code: SafeRouteReasonCode,
) -> None:
    def fail() -> None:
        raise fault

    monkeypatch.setattr(f"agent_providers.catalog.{probe_name}", fail)

    with pytest.raises(ProviderModelCatalogUnavailableError) as failure:
        list_provider_models(provider, ProviderRoute.CLI)

    assert failure.value.reason.code is expected_code


GROK_LISTING_OUTCOMES = [
    pytest.param(
        GROK_LOGGED_OUT, SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED, id="logged-out",
    ),
    pytest.param(
        GROK_LOGGED_IN_OFFERING_ONLY_DEEPSEEK, SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR,
        id="signed-in-offering-only-a-non-chat-model",
    ),
    pytest.param(
        None, SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED,
        id="status-run-exits-non-zero",
    ),
]


@pytest.mark.parametrize(("status_output", "expected_code"), GROK_LISTING_OUTCOMES)
def test_the_grok_cli_listing_reads_the_login_its_status_run_reports(
    tmp_path: Path, status_output: str | None, expected_code: SafeRouteReasonCode,
) -> None:
    answers = {} if status_output is None else {("models",): status_output}
    _configure_cli(tmp_path, "grok", answers)

    with pytest.raises(ProviderModelCatalogUnavailableError) as failure:
        list_provider_models("grok", ProviderRoute.CLI)

    assert failure.value.reason.code is expected_code


def test_the_grok_cli_listing_reads_a_status_run_that_did_not_complete_as_logged_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_cli(tmp_path, "grok", {})
    monkeypatch.setattr(
        process, "run_catalog_cli",
        lambda *_arguments, **_keywords: process.CliRun(0, GROK_LOGGED_IN, "", complete=False),
    )

    with pytest.raises(ProviderModelCatalogUnavailableError) as failure:
        list_provider_models("grok", ProviderRoute.CLI)

    assert failure.value.reason.code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED


def test_the_grok_cli_listing_reads_a_probe_that_outlives_its_caller_budget_as_logged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def hanging_probe() -> None:
        release.wait(timeout=1)

    monkeypatch.setattr(process, "_grok_status_probe", process.CachedProbe(hanging_probe))
    monkeypatch.setattr(process, "CLI_PROBE_CALLER_TIMEOUT_SECONDS", 0.05)
    try:
        with pytest.raises(ProviderModelCatalogUnavailableError) as failure:
            list_provider_models("grok", ProviderRoute.CLI)
    finally:
        release.set()

    assert failure.value.reason.code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED


def test_the_codex_cli_listing_needs_no_login(tmp_path: Path) -> None:
    _configure_cli(tmp_path, "codex", {CODEX_MODEL_COMMAND: _codex_models(("gpt-5.6", "list", 1))})
    current_codex_login = tmp_path / "no-such-codex-login.json"
    override_codex_runtime(token_file=current_codex_login)

    assert list_provider_models("codex", ProviderRoute.CLI) == ["gpt-5.6"]


def _cli_root_removed(tmp_path: Path) -> None:
    removed_root = tmp_path / "removed-root"
    removed_root.mkdir(mode=0o700)
    override_provider_runtime(cli_working_directory_root=removed_root)
    removed_root.rmdir()


def _claude_binary_removed(tmp_path: Path) -> None:
    override_provider_runtime(cli_binary_search_path=(tmp_path,))
    override_claude_runtime(binary="absent-claude-cli")


CLAUDE_LISTING_FAULTS = [
    pytest.param(
        _cli_root_removed, SafeRouteReasonCode.CLI_UNAVAILABLE,
        ProviderRouteReadinessState.DISTURBED, id="home-not-made",
    ),
]


@pytest.mark.parametrize(
    ("make_fault", "expected_code"),
    [pytest.param(*fault.values[:2], id=fault.id) for fault in CLAUDE_LISTING_FAULTS],
)
def test_the_claude_cli_listing_names_the_meaning_of_the_fault_that_stopped_it(
    tmp_path: Path, make_fault, expected_code: SafeRouteReasonCode,
) -> None:
    _configure_cli(tmp_path, "claude", {CLAUDE_MODEL_COMMAND: CLAUDE_MODEL_CATALOG_STDOUT})
    make_fault(tmp_path)

    with pytest.raises(ProviderModelCatalogUnavailableError) as failure:
        list_provider_models("claude", ProviderRoute.CLI)

    assert failure.value.reason.code is expected_code


def test_the_claude_cli_listing_without_a_binary_is_unavailable(tmp_path: Path) -> None:
    _claude_binary_removed(tmp_path)

    with pytest.raises(ProviderModelCatalogUnavailableError) as failure:
        list_provider_models("claude", ProviderRoute.CLI)

    assert failure.value.reason.code is SafeRouteReasonCode.CLI_UNAVAILABLE


@pytest.mark.parametrize(
    ("make_fault", "expected_code", "expected_readiness"), CLAUDE_LISTING_FAULTS,
)
def test_a_claude_route_that_was_signed_in_reports_the_fault_that_broke_its_listing(
    tmp_path: Path, make_fault, expected_code: SafeRouteReasonCode,
    expected_readiness: ProviderRouteReadinessState,
) -> None:
    _configure_cli(tmp_path, "claude", {
        CLAUDE_STATUS_COMMAND: CLAUDE_LOGGED_IN,
        CLAUDE_MODEL_COMMAND: CLAUDE_MODEL_CATALOG_STDOUT,
    })
    assert probe_provider_route("claude", ProviderRoute.CLI).readiness is (
        ProviderRouteReadinessState.READY
    )
    make_fault(tmp_path)

    snapshot = probe_provider_route("claude", ProviderRoute.CLI)

    assert snapshot.readiness is expected_readiness
    assert snapshot.reason.code is expected_code


A_CODEX_LOGIN_WITHOUT_ITS_ID_TOKEN = b'{"auth_mode":"chatgpt","tokens":{"access_token":"a"}}'


def _codex_setup(tmp_path: Path, document: bytes) -> object:
    auth_file = tmp_path / "codex-auth.json"
    auth_file.write_bytes(document)
    override_codex_runtime(token_file=auth_file)
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


class _Credential(enum.Enum):
    VALID = "valid"
    ABSENT = "absent"
    WRONG_TYPED = "wrong-typed"
    NOT_UTF8 = "not-utf8"


class _Binary(enum.Enum):
    RUNNABLE = "runnable"
    NOT_EXECUTABLE = "not-executable"
    MISSING = "missing"


class _Outcome(enum.Enum):
    CLI_UNAVAILABLE = "cli-unavailable"
    CLI_READY = "cli-ready"
    NOT_LOGGED_IN = "not-logged-in"
    API_READY = "api-ready"
    NEEDS_KEY = "needs-key"


@dataclasses.dataclass(frozen=True)
class _CliSetupInstall:
    provider: str
    api_key_setting: str
    method: ProviderSetupMethod
    environment_key: str
    credentials: dict[_Credential, bytes | None]


CLI_SETUP_INSTALLS = [
    pytest.param(
        _CliSetupInstall(
            "grok", "xai_api_key",
            ProviderSetupMethod.GROK_CLI, "XAI_API_KEY",
            {
                _Credential.VALID: b'{"realm": {"key": "synthetic-grok-key"}}',
                _Credential.ABSENT: None,
                _Credential.WRONG_TYPED: b'{"realm": {"key": 3}}',
                _Credential.NOT_UTF8: b"\xff\xfe",
            },
        ),
        id="grok",
    ),
    pytest.param(
        _CliSetupInstall(
            "codex", "openai_api_key",
            ProviderSetupMethod.CODEX_CLI, "OPENAI_API_KEY",
            {
                _Credential.VALID: a_codex_login(),
                _Credential.ABSENT: None,
                _Credential.WRONG_TYPED: a_codex_login(tokens={"account_id": 7}),
                _Credential.NOT_UTF8: A_CODEX_LOGIN_WITH_INVALID_UTF8,
            },
        ),
        id="codex",
    ),
]


def _a_cli_install(
    tmp_path: Path, install: _CliSetupInstall, credential: _Credential, binary: _Binary,
    *, api_key: bool = False,
) -> Path:
    auth_file = tmp_path / "auth.json"
    document = install.credentials[credential]
    if document is not None:
        auth_file.write_bytes(document)
    calls_log = tmp_path / "calls"
    if binary is _Binary.RUNNABLE:
        fake_cli_executable(tmp_path, {}, calls_log=calls_log)
    if binary is _Binary.NOT_EXECUTABLE:
        (tmp_path / "fake-cli").write_text("#!/bin/sh\n")
    override_provider_runtime(
        cli_binary_search_path=(tmp_path,),
        **{install.api_key_setting: SecretStr("a-key") if api_key else None},
    )
    CLI_RUNTIME_OVERRIDES[install.provider](
        token_file=auth_file,
        binary="absent-cli" if binary is _Binary.MISSING else "fake-cli",
    )
    return calls_log


@pytest.mark.parametrize("install", CLI_SETUP_INSTALLS)
@pytest.mark.parametrize(
    ("credential", "binary", "api_key", "outcome", "warned_class"),
    [
        pytest.param(
            _Credential.VALID, _Binary.MISSING, False,
            _Outcome.CLI_UNAVAILABLE, "CliEnvironmentUnavailableError",
            id="a-credential-without-its-binary",
        ),
        pytest.param(
            _Credential.VALID, _Binary.NOT_EXECUTABLE, False,
            _Outcome.CLI_UNAVAILABLE, "CliEnvironmentUnavailableError",
            id="a-credential-with-a-binary-that-cannot-execute",
        ),
        pytest.param(
            _Credential.VALID, _Binary.RUNNABLE, False, _Outcome.CLI_READY, None,
            id="a-runnable-cli",
        ),
        pytest.param(
            _Credential.ABSENT, _Binary.MISSING, False, _Outcome.NOT_LOGGED_IN, None,
            id="no-credential",
        ),
        pytest.param(
            _Credential.WRONG_TYPED, _Binary.MISSING, False,
            _Outcome.NOT_LOGGED_IN, "CliLoginUnusableError",
            id="a-wrong-typed-credential",
        ),
        pytest.param(
            _Credential.NOT_UTF8, _Binary.MISSING, False,
            _Outcome.NOT_LOGGED_IN, None, id="a-credential-of-invalid-utf8",
        ),
        pytest.param(
            _Credential.VALID, _Binary.MISSING, True,
            _Outcome.API_READY, None, id="an-api-key-without-the-binary",
        ),
    ],
)
def test_cli_setup_tells_whether_the_cli_can_run_here(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, install: _CliSetupInstall,
    credential: _Credential, binary: _Binary, api_key: bool,
    outcome: _Outcome, warned_class: str | None,
) -> None:
    caplog.set_level("WARNING", logger="agent_providers.catalog")
    calls_log = _a_cli_install(tmp_path, install, credential, binary, api_key=api_key)

    configuration = provider_configuration(install.provider, frozenset({install.method}))

    assert configuration == {
        _Outcome.CLI_UNAVAILABLE: ProviderCliUnavailable(
            install.provider, CliEnvironmentCause.BINARY_MISSING, install.environment_key,
        ),
        _Outcome.CLI_READY: ProviderReady(install.provider, install.method),
        _Outcome.NOT_LOGGED_IN: ProviderNotLoggedIn(install.provider, install.environment_key),
        _Outcome.API_READY: ProviderReady(
            install.provider, ProviderSetupMethod.API_KEY, install.environment_key,
        ),
    }[outcome]
    if warned_class:
        assert f"{install.provider} CLI probe unavailable: {warned_class}" in caplog.text
    assert not calls_log.exists()


@pytest.mark.parametrize("install", CLI_SETUP_INSTALLS)
@pytest.mark.parametrize(
    ("binary", "expected"),
    [
        pytest.param(
            _Binary.RUNNABLE, _Outcome.NEEDS_KEY,
            id="a-signed-in-cli-the-surface-does-not-accept",
        ),
        pytest.param(_Binary.MISSING, _Outcome.CLI_UNAVAILABLE, id="the-same-without-its-binary"),
    ],
)
def test_cli_setup_for_a_surface_that_excludes_the_cli_names_the_binary_fault_first(
    tmp_path: Path, install: _CliSetupInstall, binary: _Binary, expected: _Outcome,
) -> None:
    _a_cli_install(tmp_path, install, _Credential.VALID, binary)

    configuration = provider_configuration(install.provider, frozenset())

    assert configuration == {
        _Outcome.NEEDS_KEY: ProviderNeedsKey(
            install.provider, install.method, install.environment_key,
        ),
        _Outcome.CLI_UNAVAILABLE: ProviderCliUnavailable(
            install.provider, CliEnvironmentCause.BINARY_MISSING, install.environment_key,
        ),
    }[expected]


@contextlib.contextmanager
def _claude_binary_missing(tmp_path: Path) -> Iterator[None]:
    _claude_binary_removed(tmp_path)
    yield


@contextlib.contextmanager
def _claude_cli_root_removed(tmp_path: Path) -> Iterator[None]:
    _configure_cli(tmp_path, "claude", {CLAUDE_STATUS_COMMAND: CLAUDE_LOGGED_IN})
    _cli_root_removed(tmp_path)
    yield


CLAUDE_ENVIRONMENT_FAULTS = [
    pytest.param(
        _claude_binary_missing, CliEnvironmentCause.BINARY_MISSING,
        "CliEnvironmentUnavailableError", id="binary-missing",
    ),
    pytest.param(
        _claude_cli_root_removed, CliEnvironmentCause.HOME_NOT_CREATED,
        "CliEnvironmentUnavailableError", id="root-removed",
    ),
]


@pytest.mark.parametrize(("fault", "cause", "named_class"), CLAUDE_ENVIRONMENT_FAULTS)
def test_claude_setup_without_a_key_says_the_cli_cannot_run_when_the_environment_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, fault, cause: CliEnvironmentCause,
    named_class: str,
) -> None:
    caplog.set_level("WARNING", logger="agent_providers.catalog")

    with fault(tmp_path):
        configuration = provider_configuration("claude", frozenset())

    assert configuration == ProviderCliUnavailable("claude", cause, "ANTHROPIC_API_KEY")
    assert f"claude CLI probe unavailable: {named_class}" in caplog.text


@pytest.mark.parametrize(("fault", "cause", "named_class"), CLAUDE_ENVIRONMENT_FAULTS)
def test_claude_setup_with_a_key_is_ready_whatever_the_cli_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault, cause: CliEnvironmentCause,
    named_class: str,
) -> None:
    monkeypatch.setattr("agent_providers.catalog._anthropic_sdk_available", lambda: True)
    override_provider_runtime(anthropic_api_key=SecretStr("a-key"))

    with fault(tmp_path):
        configuration = provider_configuration("claude", frozenset())

    assert configuration == ProviderReady(
        "claude", ProviderSetupMethod.API_KEY, "ANTHROPIC_API_KEY",
    )


def test_a_wrong_typed_codex_login_is_named_in_the_setup_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="agent_providers.catalog")

    configuration = _codex_setup(tmp_path, a_codex_login(tokens={"account_id": 7}))

    assert isinstance(configuration, ProviderNotLoggedIn)
    assert "codex CLI probe unavailable: CliLoginUnusableError" in caplog.text


def test_codex_setup_does_not_follow_a_link_to_a_complete_login(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(a_codex_login())
    link = tmp_path / "codex-auth.json"
    link.symlink_to(target)
    override_codex_runtime(token_file=link)

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
    override_provider_runtime(cli_binary_search_path=(tmp_path,))
    override_codex_runtime(binary="fake-cli", token_file=make_source(tmp_path))

    snapshot = call_within_deadline(probe_provider_route, "codex", ProviderRoute.CLI)

    assert snapshot.readiness is ProviderRouteReadinessState.NOT_CONFIGURED
    assert snapshot.reason.code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED
    assert not calls_log.exists()
