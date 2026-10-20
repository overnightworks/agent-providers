"""The two entry points a host calls to run a turn and to ask for an image."""

from __future__ import annotations

import importlib
import inspect
import json
import os
import pkgutil
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from provider_test_support import (
    A_CODEX_LOGIN_WITH_INVALID_UTF8,
    CLAUDE_MODEL_CATALOG_STDOUT,
    a_claude_cli_child,
    a_codex_login,
    call_within_deadline,
    drive_turn,
    fake_cli_executable,
    install_claude_cli_children,
    override_codex_runtime,
    override_provider_runtime,
    override_turn_runtime,
    remove_the_codex_turn_block_with_no_login_and_no_binary,
)
from pydantic import SecretStr

import agent_providers
from agent_providers import dispatch
from agent_providers.catalog import ProviderRoute, ProviderRouteReadinessState, probe_provider_route
from agent_providers.claude import adapter as claude_adapter
from agent_providers.claude import provider as claude_provider
from agent_providers.claude.provider import ClaudeResponse
from agent_providers.config import (
    ProviderNotConfiguredError,
    ProviderRuntimeConfig,
    ProviderRuntimeNotConfiguredError,
    TurnRuntimeConfig,
    TurnRuntimeNotConfiguredError,
    current_config,
    reset_config,
)
from agent_providers.constants import CLAUDE_NO_SESSION_PERSISTENCE_FLAG
from agent_providers.dispatch import ImageCapability, call_provider_once, image_capability
from agent_providers.errors import (
    ProviderUnavailableError,
    SafeRouteReasonCode,
)
from agent_providers.events import FinalEvent


def test_stream_turn_refuses_an_unknown_provider_before_any_route_runs(monkeypatch):
    routes_started: list[str] = []

    def start_route(**arguments):
        routes_started.append(arguments["provider"])
        raise ProviderUnavailableError(arguments["provider"], "route started")

    monkeypatch.setattr(dispatch, "_stream_for_route", start_route)

    turn = drive_turn("bob")

    assert isinstance(turn.error, ProviderUnavailableError)
    assert turn.error.reason.code is SafeRouteReasonCode.ROUTE_FAILED
    assert turn.events == []
    assert turn.executed == []
    assert routes_started == []


@pytest.mark.parametrize(
    ("provider", "remove_block", "block"),
    [
        pytest.param(
            "claude", lambda: override_provider_runtime(claude=None), "claude", id="claude-block",
        ),
        pytest.param(
            "grok", lambda: override_provider_runtime(grok=None), "grok", id="grok-block",
        ),
        pytest.param(
            "grok", lambda: override_turn_runtime(grok=None), "turns.grok", id="turns-grok-block",
        ),
        pytest.param(
            "codex", lambda: override_provider_runtime(codex=None), "codex", id="codex-block",
        ),
        pytest.param(
            "codex",
            remove_the_codex_turn_block_with_no_login_and_no_binary,
            "turns.codex",
            id="turns-codex-block",
        ),
    ],
)
def test_a_turn_without_the_providers_block_raises_the_named_wiring_error(
    provider, remove_block, block, spawned_children,
):
    remove_block()

    turn = drive_turn(provider)

    assert type(turn.error) is ProviderNotConfiguredError
    assert (turn.error.provider, turn.error.block) == (provider, block)
    assert turn.events == []
    assert spawned_children == []


@pytest.mark.parametrize(
    ("remove_block", "block"),
    [
        pytest.param(lambda: override_provider_runtime(codex=None), "codex", id="codex-block"),
        pytest.param(
            remove_the_codex_turn_block_with_no_login_and_no_binary,
            "turns.codex",
            id="turns-codex-block",
        ),
    ],
)
def test_codex_image_capability_without_its_block_raises_the_named_wiring_error(
    remove_block, block, spawned_children,
):
    remove_block()

    with pytest.raises(ProviderNotConfiguredError) as refused:
        image_capability("codex", ProviderRoute.CLI)

    assert (refused.value.provider, refused.value.block) == ("codex", block)
    assert spawned_children == []


@pytest.mark.parametrize("entry_point", ["stream_turn", "call_provider_once"])
def test_codex_on_the_api_route_runs_without_a_codex_block_given_an_api_key(
    entry_point, monkeypatch,
):
    async def api_turn(**_arguments):
        yield FinalEvent(text="answer")

    monkeypatch.setattr(dispatch, "stream_openai_compatible_turn", api_turn)
    monkeypatch.setattr(dispatch, "call_openai_compatible_once", lambda **_arguments: "answer")
    override_provider_runtime(codex=None, openai_api_key=SecretStr("sk-test"))

    if entry_point == "stream_turn":
        turn = drive_turn("codex", route=ProviderRoute.API)
        assert turn.error is None
        assert [event.text for event in turn.events] == ["answer"]
    else:
        answer = call_provider_once(provider="codex", model="gpt-5.6", prompt="hi", timeout=1)
        assert answer == "answer"


def test_a_one_shot_without_the_claude_block_raises_the_named_wiring_error_unwrapped(
    spawned_children,
):
    override_provider_runtime(claude=None)

    with pytest.raises(ProviderNotConfiguredError) as refused:
        call_provider_once(provider="claude", model=None, prompt="hi", timeout=1)

    assert (refused.value.provider, refused.value.block) == ("claude", "claude")
    assert spawned_children == []


@pytest.mark.parametrize("entry_point", ["stream_turn", "call_provider_once"])
def test_a_provider_call_before_configure_raises_the_unconfigured_error_unwrapped(
    entry_point, spawned_children,
):
    reset_config()

    if entry_point == "stream_turn":
        error = drive_turn("claude").error
    else:
        with pytest.raises(ProviderRuntimeNotConfiguredError) as refused:
            call_provider_once(provider="claude", model=None, prompt="hi", timeout=1)
        error = refused.value

    assert type(error) is ProviderRuntimeNotConfiguredError
    assert spawned_children == []


def test_a_turn_without_the_turn_runtime_raises_the_wiring_error_unwrapped(spawned_children):
    override_provider_runtime(turns=None)

    turn = drive_turn("grok")

    assert type(turn.error) is TurnRuntimeNotConfiguredError
    assert turn.events == []
    assert spawned_children == []


def test_a_failing_route_logs_the_failed_turn_by_provider_route_and_class(monkeypatch, caplog):
    def fail_route(**_arguments):
        raise RuntimeError("synthetic-confidential-detail")

    monkeypatch.setattr(dispatch, "_stream_for_route", fail_route)
    caplog.set_level("WARNING", logger="agent_providers.dispatch")

    drive_turn("grok")

    assert "Turn failed provider=grok route=cli class=RuntimeError" in caplog.text
    assert "synthetic-confidential-detail" not in caplog.text


def test_a_failing_one_shot_completion_logs_its_provider_and_class(monkeypatch, caplog):
    def fail_connection(_provider):
        raise RuntimeError("synthetic-confidential-detail")

    monkeypatch.setattr(dispatch, "_api_connection", fail_connection)
    caplog.set_level("WARNING", logger="agent_providers.dispatch")

    with pytest.raises(ProviderUnavailableError):
        call_provider_once(provider="grok", model="any-model", prompt="hi", timeout=1)

    assert "One-shot completion failed provider=grok class=RuntimeError" in caplog.text
    assert "synthetic-confidential-detail" not in caplog.text


def test_call_provider_once_refuses_an_unknown_provider():
    with pytest.raises(ProviderUnavailableError) as refusal:
        call_provider_once(provider="bob", model="any-model", prompt="hi", timeout=1)

    assert refusal.value.reason.code is SafeRouteReasonCode.ROUTE_FAILED


_PROVIDER_ROUTES = [
    (provider, route)
    for provider in ("claude", "grok", "codex")
    for route in (ProviderRoute.CLI, ProviderRoute.API)
]
_ROUTES_WITHOUT_A_MODEL_DEFAULT = [
    pair for pair in _PROVIDER_ROUTES if pair[1] is ProviderRoute.API
]


@pytest.fixture
def started_routes(monkeypatch) -> list[str]:
    started: list[str] = []

    def start_route(**arguments):
        started.append(arguments["provider"])
        raise AssertionError("no route may start")

    monkeypatch.setattr(dispatch, "_stream_for_route", start_route)
    return started


@pytest.fixture
def claude_cli_turn_models(monkeypatch) -> list[str | None]:
    """The model each Claude CLI tool turn was started with."""
    models: list[str | None] = []

    async def finish_turn(*, model, **_arguments):
        models.append(model)
        yield FinalEvent(text="done")

    monkeypatch.setattr(claude_adapter, "acall_claude_with_mcp_stream", finish_turn)
    return models


def _refusal_of(call) -> ProviderUnavailableError:
    with pytest.raises(ProviderUnavailableError) as refusal:
        call()
    assert refusal.value.reason.code is SafeRouteReasonCode.ROUTE_FAILED
    return refusal.value


@pytest.mark.parametrize("chosen_model", ["opus", None])
def test_stream_turn_passes_the_claude_model_only_when_one_is_chosen(
    chosen_model, claude_cli_turn_models,
):
    turn = drive_turn("claude", model=chosen_model)

    assert turn.error is None
    assert [event.text for event in turn.events] == ["done"]
    assert claude_cli_turn_models == [chosen_model]


@pytest.mark.parametrize(
    ("provider", "route", "model"),
    [(provider, route, None) for provider, route in _ROUTES_WITHOUT_A_MODEL_DEFAULT]
    + [(provider, route, "") for provider, route in _PROVIDER_ROUTES],
)
def test_stream_turn_refuses_a_model_that_runs_nothing(provider, route, model, started_routes):
    turn = drive_turn(provider, route=route, model=model)

    assert turn.error.reason.code is SafeRouteReasonCode.ROUTE_FAILED
    assert (turn.error.provider, turn.error.route) == (provider, route)
    assert turn.events == []
    assert started_routes == []


@pytest.fixture
def one_shot_claude_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def answer(prompt, **arguments):
        calls.append(arguments)
        return ClaudeResponse(text="answer")

    monkeypatch.setattr(claude_adapter, "call_claude", answer)
    return calls


def test_one_shot_without_a_key_runs_the_claude_cli_default(one_shot_claude_calls):
    answer = call_provider_once(provider="claude", model=None, prompt="hi", timeout=1)

    assert answer == "answer"
    assert [(call["api_key"], call["model"]) for call in one_shot_claude_calls] == [(None, None)]


@pytest.fixture
def claude_only_host(tmp_path: Path) -> Path:
    claude_only = ProviderRuntimeConfig(
        cli_binary_search_path=(tmp_path,),
        cli_working_directory_root=tmp_path,
        claude=current_config().claude.model_copy(update={"binary": "fake-cli"}),
        turns=TurnRuntimeConfig(mcp_server=None),
    )
    fake_cli_executable(tmp_path, {
        ("auth", "status"): json.dumps({"loggedIn": True, "authMethod": "claude.ai"}),
        ("-p", "/model", CLAUDE_NO_SESSION_PERSISTENCE_FLAG): CLAUDE_MODEL_CATALOG_STDOUT,
    })
    override_provider_runtime(**claude_only.model_dump())
    return tmp_path


@pytest.fixture
def claude_children_that_answer(monkeypatch) -> list:
    def answer(child, _stream_buffer_limit):
        if "stream-json" in child.arguments:
            return a_claude_cli_child(stream_lines=[b'{"type":"result","result":"answer"}\n'])
        return a_claude_cli_child(communicate=(b'{"result":"answer"}', b""))

    claude_binary = Path("/usr/bin/claude")
    monkeypatch.setattr(
        claude_provider, "verify_no_builtin_cli_tools", lambda *, deadline=None: claude_binary,
    )
    for async_gate in ("averify_no_builtin_cli_tools", "verify_cli_tool_surface"):
        monkeypatch.setattr(claude_provider, async_gate, AsyncMock(return_value=claude_binary))
    monkeypatch.setattr(
        claude_provider,
        "run_capturing",
        lambda child, **_kwargs: MagicMock(returncode=0, stdout='{"result":"answer"}', stderr=""),
    )
    return install_claude_cli_children(monkeypatch, factory=answer).children


@pytest.mark.usefixtures("claude_only_host")
def test_a_claude_only_host_holds_no_codex_or_grok_value_and_reports_the_claude_catalog():
    installed = current_config()
    snapshot = probe_provider_route("claude", ProviderRoute.CLI)

    assert (installed.codex, installed.grok) == (None, None)
    assert (installed.turns.codex, installed.turns.grok) == (None, None)
    assert snapshot.readiness is ProviderRouteReadinessState.READY


@pytest.mark.usefixtures("claude_only_host", "claude_children_that_answer")
def test_a_claude_only_host_runs_a_one_shot_down_to_the_spawn_boundary():
    answer = call_provider_once(provider="claude", model=None, prompt="hi", timeout=1)

    assert answer == "answer"


@pytest.mark.usefixtures("claude_only_host", "claude_children_that_answer")
def test_a_claude_only_host_streams_a_turn_down_to_the_spawn_boundary():
    turn = drive_turn("claude")

    assert turn.error is None
    assert [event.text for event in turn.events if isinstance(event, FinalEvent)] == ["answer"]


def test_one_shot_refuses_a_missing_model_on_claude_api_with_a_key(monkeypatch):
    override_provider_runtime(anthropic_api_key=SecretStr("sk-test"))
    sdk = MagicMock(APITimeoutError=Exception)
    monkeypatch.setitem(sys.modules, "anthropic", sdk)

    refusal = _refusal_of(
        lambda: call_provider_once(provider="claude", model=None, prompt="hi", timeout=1),
    )

    assert refusal.route == "api"
    sdk.Anthropic.assert_not_called()


@pytest.mark.parametrize("provider", ["grok", "codex"])
@pytest.mark.parametrize("model", [None, ""])
def test_one_shot_refuses_a_model_that_runs_nothing_on_the_api(provider, model, monkeypatch):
    monkeypatch.setattr(
        dispatch, "_api_connection", lambda _provider: pytest.fail("no HTTP call may start"),
    )

    _refusal_of(lambda: call_provider_once(provider=provider, model=model, prompt="hi", timeout=1))


def test_one_shot_refuses_an_empty_claude_model_before_any_child_runs(one_shot_claude_calls):
    _refusal_of(lambda: call_provider_once(provider="claude", model="", prompt="hi", timeout=1))

    assert one_shot_claude_calls == []


def test_stream_turn_takes_only_keyword_arguments():
    parameters = inspect.signature(dispatch.stream_turn).parameters.values()

    assert {parameter.kind for parameter in parameters} == {inspect.Parameter.KEYWORD_ONLY}


def test_image_capability_says_a_grok_cli_route_carries_no_image_tool():
    capability = image_capability("grok", ProviderRoute.CLI)

    assert isinstance(capability, ImageCapability)
    assert capability.carries_image_tool is False
    assert capability.failure.code is SafeRouteReasonCode.NO_IMAGE_TOOL



def _codex_image_failure(monkeypatch, auth_file: Path):
    monkeypatch.setattr(dispatch, "codex_image_turn_is_available", lambda: True)
    override_codex_runtime(token_file=auth_file)
    return call_within_deadline(image_capability, "codex", ProviderRoute.CLI).failure


@pytest.mark.parametrize(
    ("document", "failure_code"),
    [
        pytest.param(a_codex_login(), None, id="complete"),
        pytest.param(
            b'{"auth_mode":"chatgpt","tokens":{"access_token":"a"}}',
            SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED, id="only-an-access-token",
        ),
        pytest.param(b"{}", SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED, id="tokens-absent"),
        pytest.param(
            a_codex_login(tokens={"account_id": 7}),
            SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED, id="numeric-account-id",
        ),
        pytest.param(
            A_CODEX_LOGIN_WITH_INVALID_UTF8, SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED,
            id="invalid-encoding",
        ),
    ],
)
def test_codex_image_preflight_needs_a_usable_login(
    monkeypatch, tmp_path: Path, document: bytes, failure_code: SafeRouteReasonCode | None,
):
    auth_file = tmp_path / "auth.json"
    auth_file.write_bytes(document)

    failure = _codex_image_failure(monkeypatch, auth_file)

    assert (failure.code if failure else None) is failure_code


def test_codex_image_preflight_refuses_a_fifo_login_without_waiting(monkeypatch, tmp_path: Path):
    auth_file = tmp_path / "auth.json"
    os.mkfifo(auth_file)

    failure = _codex_image_failure(monkeypatch, auth_file)

    assert failure.code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED


def test_codex_image_preflight_says_the_cli_is_unavailable_when_an_image_dependency_is_missing(
    monkeypatch,
):
    monkeypatch.setattr(dispatch, "codex_image_turn_is_available", lambda: False)

    failure = image_capability("codex", ProviderRoute.CLI).failure

    assert failure.code is SafeRouteReasonCode.CLI_UNAVAILABLE


README = Path(__file__).resolve().parents[1] / "README.md"
DOTTED_NAME = re.compile(r"`((?:[a-z_]+\.)+[A-Za-z_]+)`")


def names_table_rows() -> list[tuple[str, str]]:
    names_section = README.read_text().split("### Names and values", 1)[1].split("\n## ", 1)[0]
    table_lines = [line for line in names_section.splitlines() if line.startswith("|")]
    rows = []
    for line in table_lines[2:]:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            raise ValueError(f"Malformed Names-table row: {line!r}")
        rows.append((cells[0], cells[1]))
    return rows


def renamed_public_names() -> list[str]:
    return sorted({
        old
        for before, now in names_table_rows()
        for old in DOTTED_NAME.findall(before)
        if old not in DOTTED_NAME.findall(now)
    })


def package_modules() -> list[object]:
    return [
        importlib.import_module(module_info.name)
        for module_info in pkgutil.walk_packages(agent_providers.__path__, "agent_providers.")
    ] + [agent_providers]


def test_the_names_table_lists_the_renames_this_release_made():
    assert renamed_public_names()
    assert any(now == "`dispatch.stream_turn`" for _, now in names_table_rows())


@pytest.mark.parametrize("old_name", renamed_public_names())
def test_a_renamed_public_name_is_gone_from_every_module_without_an_alias(old_name: str):
    attribute = old_name.rpartition(".")[2]

    modules_still_naming_it = [
        module.__name__ for module in package_modules() if hasattr(module, attribute)
    ]

    assert modules_still_naming_it == []
