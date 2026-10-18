"""The two entry points a host calls to run a turn and to ask for an image."""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from provider_test_support import (
    A_CODEX_LOGIN_WITH_INVALID_UTF8,
    a_codex_login,
    call_within_deadline,
    drive_turn,
    override_provider_runtime,
    override_turn_runtime,
)
from pydantic import SecretStr

import agent_providers
from agent_providers import dispatch
from agent_providers.catalog import ProviderRoute
from agent_providers.claude import adapter as claude_adapter
from agent_providers.claude.provider import ClaudeResponse
from agent_providers.config import ProviderNotConfiguredError, TurnRuntimeNotConfiguredError
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


@pytest.fixture
def spawned_children(monkeypatch) -> list[str]:
    spawned: list[str] = []

    def refuse_spawn(*arguments, **_keywords):
        spawned.append(str(arguments))
        raise AssertionError("no child may be spawned")

    monkeypatch.setattr(subprocess, "Popen", refuse_spawn)
    return spawned


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
    override_provider_runtime(codex_cli_auth_file=auth_file)
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
