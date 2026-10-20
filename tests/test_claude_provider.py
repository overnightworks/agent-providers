"""Tests for the Claude provider — API + CLI backends."""

from __future__ import annotations

import asyncio
import concurrent.futures
import gc
import json
import logging
import signal
import stat
import sys
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum, auto
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import provider_test_support
import pytest
from provider_test_support import (
    A_RUNNER_FAILURE_CANARY,
    A_STREAMED_LINE,
    CLAUDE_MODEL_CATALOG_STDOUT,
    DESTINATION_FAULTS,
    MCP_TOOL_NAMES,
    SAMPLE_CLAUDE_SETUP_TOKEN,
    SAMPLE_TOOL_CATALOG,
    SECRET_ENV_KEYS,
    ClaudeCliSpawns,
    EventGate,
    Hold,
    RunnerFailureWatch,
    ScenarioDeadline,
    ScriptedClaudeChild,
    a_child_that_outlives_sigkill,
    a_claude_cli_child,
    a_credential_destination_that_fails,
    assert_claude_token_child,
    call_within_deadline,
    confirm_the_retained_group_gone,
    drive_turn,
    exception_chain,
    fail_to_start_the_runner_thread,
    fake_cli_process,
    held_claude_slots,
    install_claude_cli_children,
    install_cli_door,
    lines_that_fail,
    operations,
    override_claude_runtime,
    override_provider_runtime,
    override_turn_runtime,
    reap_without_waiting,
    recorded_census_workers,
    reset_cli_process_pool_for_test,
    run_bounded,
    scenario_holds,
    set_claude_process_limit,
    traceback_frames_hold,
    write_claude_token_file,
)
from pydantic import SecretStr

from agent_providers import anchored_turn, lost_groups, process, spawn
from agent_providers.claude import provider
from agent_providers.claude.provider import (
    CLAUDE_CLI_BINARY_NOT_FOUND_DETAIL,
    CLAUDE_CLI_LOGIN_NOT_CONFIGURED_DETAIL,
    CLAUDE_CLI_UNAVAILABLE_DETAIL,
    ClaudeResponse,
    CliLoginNotConfiguredError,
    CliToolSurfaceError,
    CliUnavailableError,
    UnavailableError,
    _acall_cli,
    _AttachedMcpServer,
    _call_api,
    _call_cli,
    _cli_arguments,
    _find_claude_binary,
    _mcp_cli_arguments,
    _parse_announced_surface,
    acall_claude,
    acall_claude_with_mcp_stream,
    averify_no_builtin_cli_tools,
    call_claude,
    clear_cli_login_status_cache,
    clear_cli_tool_surface_cache,
    clear_client_cache,
    cli_login_status,
    cli_model_catalog,
    is_available,
    parse_json_response,
    verify_cli_tool_surface,
    verify_no_builtin_cli_tools,
)
from agent_providers.config import (
    McpServerSpec,
    ProviderNotConfiguredError,
    current_claude_runtime,
    current_config,
    current_turn_config,
    reset_config,
)
from agent_providers.constants import (
    CLAUDE_CLI_COMPLETION_TIMEOUT_SECONDS,
    CLAUDE_OAUTH_TOKEN_VARIABLE,
)
from agent_providers.dispatch import call_provider_once
from agent_providers.errors import (
    ContainmentLost,
    ProviderUnavailableError,
    RetainedGroup,
    SafeRouteReasonCode,
)
from agent_providers.events import AssistantTextEvent, FinalEvent, StreamEvent
from agent_providers.grok import transport as grok_transport
from agent_providers.process import CliRun, CliRunOutcome, CliRunReason
from agent_providers.procfs import GroupCensus
from agent_providers.secret_file import MAX_SECRET_FILE_BYTES
from agent_providers.spawn import (
    AnchorStateError,
    ChildIdentityLost,
    ChildProcess,
    KillUntilGoneResult,
)
from agent_providers.tool_loop import ToolOutcome, stream_tool_loop

ROUTE_FAILED = SafeRouteReasonCode.ROUTE_FAILED

# The exact string a tool turn command line must carry, written out rather
# than derived from the code under test.
SAMPLE_ALLOWED_TOOLS = "mcp__notes__*"

failure_watch = provider_test_support.failure_watch


def _configured_mcp_server():
    """The MCP server a host installs for the provider layer."""
    return current_turn_config().mcp_server


@pytest.fixture(autouse=True)
def killpg_signals():
    """Record the signals a reap sends; the group is already gone, so no real pid is signalled.

    Uses ``patch`` rather than ``monkeypatch`` for the reason the conftest autouse fixtures do.
    """
    signals: list[tuple[int, int]] = []

    def record_and_find_the_group_gone(pid: int, signal_number: int) -> None:
        signals.append((pid, signal_number))
        raise ProcessLookupError

    with patch.object(provider.os, "killpg", record_and_find_the_group_gone):
        yield signals


@pytest.fixture(autouse=True)
def _clear_claude_clients():
    clear_client_cache()
    clear_cli_login_status_cache()
    clear_cli_tool_surface_cache()
    reset_cli_process_pool_for_test()
    yield
    clear_client_cache()
    clear_cli_login_status_cache()
    clear_cli_tool_surface_cache()
    reset_cli_process_pool_for_test()


def _run_with_clock(coroutine, clock: dict[str, float]):
    loop = asyncio.new_event_loop()
    loop.time = lambda: clock["now"]
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


class _IncrementingMonotonicClock:
    def __init__(self, start: float, step: float) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        current = self.now
        self.now += self.step
        return current


@pytest.fixture
def incrementing_monotonic_clock(monkeypatch: pytest.MonkeyPatch) -> _IncrementingMonotonicClock:
    clock = _IncrementingMonotonicClock(start=100.0, step=0.01)
    monkeypatch.setattr(provider.time, "monotonic", clock)
    return clock


def _leaked_secret_env_values() -> dict[str, str]:
    """A value per SECRET_ENV_KEYS entry, shaped so DSN-parsing settings
    modules imported by other fixtures during teardown don't choke on it."""
    values = dict.fromkeys(SECRET_ENV_KEYS, "leaked-value")
    values["DATABASE_URL"] = "postgresql://leaked:leaked@leaked-host/leaked"
    values["REDIS_URL"] = "redis://leaked-host:6379/0"
    return values


# ── call_claude routing ─────────────────────────────────────────────


def test_call_claude_routes_to_api_with_key() -> None:
    resp = ClaudeResponse(text="hi")
    with patch("agent_providers.claude.provider._call_api", return_value=resp) as mock:
        result = call_claude("hello", api_key="sk-test", model="claude-sonnet-4-6")
    mock.assert_called_once()
    assert result.text == "hi"


def test_call_claude_routes_to_cli_without_key() -> None:
    resp = ClaudeResponse(text="yo")
    with patch("agent_providers.claude.provider._call_cli", return_value=resp) as mock:
        result = call_claude("hello")
    mock.assert_called_once()
    assert result.text == "yo"


# ── acall_claude routing ──────────────────────────────────────────


def test_acall_claude_routes_to_api_with_key() -> None:
    import asyncio
    from unittest.mock import AsyncMock

    resp = ClaudeResponse(text="async hi")
    mock = AsyncMock(return_value=resp)
    with patch("agent_providers.claude.provider._acall_api", mock):
        result = asyncio.run(acall_claude("hello", api_key="sk-test", model="claude-sonnet-4-6"))
    mock.assert_called_once()
    assert result.text == "async hi"


def test_acall_claude_routes_to_cli_without_key() -> None:
    import asyncio
    from unittest.mock import AsyncMock

    resp = ClaudeResponse(text="async yo")
    mock = AsyncMock(return_value=resp)
    with patch("agent_providers.claude.provider._acall_cli", mock):
        result = asyncio.run(acall_claude("hello"))
    mock.assert_called_once()
    assert result.text == "async yo"


# ── is_available ────────────────────────────────────────────────────


def test_is_available_with_api_key() -> None:
    assert is_available(api_key="sk-test") is True


def test_is_available_with_cli_binary() -> None:
    with patch(
        "agent_providers.claude.provider._find_claude_binary",
        return_value=Path("/usr/bin/claude"),
    ):
        assert is_available(api_key=None) is True


def test_is_available_neither() -> None:
    with patch("agent_providers.claude.provider._find_claude_binary", return_value=None):
        assert is_available(api_key=None) is False


# ── cli_login_status ───────────────────────────────────────────────


def test_cli_login_status_delegates_to_the_shared_runner() -> None:
    runner_status = provider.CliLogin(logged_in=True, auth_method="claude.ai")
    with (
        patch(
            "agent_providers.claude.provider._find_claude_binary",
            return_value=Path("/mounted/claude"),
        ),
        patch(
            "agent_providers.claude.provider.claude_cli_login",
            return_value=runner_status,
        ) as login,
    ):
        status = cli_login_status()

    assert status is runner_status
    login.assert_called_once_with(Path("/mounted/claude"))


def test_cli_login_status_without_a_binary_delegates_the_unavailable_probe() -> None:
    logged_out = provider.CliLogin(logged_in=False, auth_method=None)
    with (
        patch("agent_providers.claude.provider._find_claude_binary", return_value=None),
        patch(
            "agent_providers.claude.provider.claude_cli_login",
            return_value=logged_out,
        ) as login,
    ):
        status = cli_login_status()

    assert status is logged_out
    login.assert_called_once_with(None)


def test_clearing_the_provider_login_cache_delegates_to_the_runner() -> None:
    with patch(
        "agent_providers.claude.provider.clear_claude_cli_login_cache",
    ) as clear:
        clear_cli_login_status_cache()

    clear.assert_called_once()


# ── cli_model_catalog ───────────────────────────────────────────


def _catalog_run(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
    *,
    complete: bool = True,
) -> CliRun:
    return CliRun(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        complete=complete,
    )


AVAILABLE_LINE = "Usage: /model <name>. Available: sonnet, opus.\n"


def _catalog_read_from(stdout: str) -> provider.ClaudeCliModelCatalog:
    with (
        patch(
            "agent_providers.claude.provider._find_claude_binary",
            return_value=Path("/usr/bin/claude"),
        ),
        patch(
            "agent_providers.claude.provider.run_claude_catalog_cli",
            return_value=_catalog_run(stdout),
        ),
    ):
        return cli_model_catalog()


@pytest.mark.parametrize(
    ("stdout", "label", "aliases"),
    (
        (
            CLAUDE_MODEL_CATALOG_STDOUT, "Opus 5 (1M context)",
            (
                "sonnet", "opus", "haiku", "fable", "best", "sonnet[1m]", "opus[1m]",
                "fable[1m]", "opusplan", "default",
            ),
        ),
        (
            "Current model: `Opus 5.5 (default)`\n" + AVAILABLE_LINE,
            "Opus 5.5 (default)", ("sonnet", "opus"),
        ),
        (
            "Current model: `Opus 5` (effort: `high`)\n" + AVAILABLE_LINE,
            "Opus 5", ("sonnet", "opus"),
        ),
    ),
    ids=("fixture-with-effort", "measured-2.1.302-shape", "several-backtick-pairs"),
)
def test_claude_catalog_names_the_current_model(
    stdout: str, label: str, aliases: tuple[str, ...], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    catalog = _catalog_read_from(stdout)

    assert catalog.current_model_label == label
    assert catalog.aliases == aliases
    assert label not in caplog.text


@pytest.mark.parametrize(
    "current_model_text",
    (
        "",
        "Current model: `Opus 5.5 (default)`\nCurrent model: `Sonnet 5`\n",
        "Current model: `Opus 5.5 (default)\n",
        "Current model: ``\n",
        "Current model: `Opus\x1b[31m 5`\n",
        "Note: Current model: `Opus 5`\n",
    ),
    ids=(
        "no-line", "two-lines", "no-closing-backtick", "empty-text", "control-character",
        "marker-mid-line",
    ),
)
def test_claude_catalog_without_one_readable_current_model_has_no_label(
    current_model_text: str, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    catalog = _catalog_read_from(current_model_text + AVAILABLE_LINE)

    assert catalog.current_model_label is None
    assert catalog.aliases == ("sonnet", "opus")
    assert "Opus" not in caplog.text


def test_a_current_model_line_alone_is_not_a_catalog() -> None:
    with pytest.raises(UnavailableError, match="did not contain a parseable"):
        _catalog_read_from("Current model: `Opus 5.5 (default)`\n")


def test_the_model_catalog_timeout_raises_named_error() -> None:
    with (
        patch(
            "agent_providers.claude.provider._find_claude_binary",
            return_value=Path("/usr/bin/claude"),
        ),
        patch(
            "agent_providers.claude.provider.run_claude_catalog_cli",
            return_value=_catalog_run(complete=False),
        ),
    ):
        with pytest.raises(UnavailableError, match="timed out"):
            cli_model_catalog()


def test_the_model_catalog_spawn_failure_raises_named_error() -> None:
    with (
        patch(
            "agent_providers.claude.provider._find_claude_binary",
            return_value=Path("/usr/bin/claude"),
        ),
        patch(
            "agent_providers.claude.provider.run_claude_catalog_cli",
            return_value=None,
        ),
    ):
        with pytest.raises(UnavailableError, match="failed to run"):
            cli_model_catalog()


def test_the_model_catalog_nonzero_exit_raises_named_error() -> None:
    with (
        patch(
            "agent_providers.claude.provider._find_claude_binary",
            return_value=Path("/usr/bin/claude"),
        ),
        patch(
            "agent_providers.claude.provider.run_claude_catalog_cli",
            return_value=_catalog_run("", returncode=1),
        ),
    ):
        with pytest.raises(UnavailableError, match="Claude CLI could not list models"):
            cli_model_catalog()


def test_the_model_catalog_runs_the_closed_catalog_spawn(
    tmp_path, monkeypatch,
) -> None:
    fake = tmp_path / "claude"
    fake.write_bytes(b"")
    spawned: list[spawn.ChildProcess] = []

    def catalog_door(child, **_kw):
        spawned.append(child)
        return fake_cli_process(b"Usage: /model <name>. Available: sonnet, opus.\n")

    install_cli_door(monkeypatch, catalog_door)
    monkeypatch.setenv("CATALOG_ENV_LEAK", "should-not-appear")
    with patch(
        "agent_providers.claude.provider._find_claude_binary",
        return_value=fake,
    ):
        catalog = cli_model_catalog()

    assert catalog.aliases == ("sonnet", "opus")
    assert "CATALOG_ENV_LEAK" not in spawned[0].environment


# ── _call_api ───────────────────────────────────────────────────────


def test_call_api_success() -> None:
    mock_client = MagicMock()
    mock_content = MagicMock()
    mock_content.text = "response text"
    mock_client.messages.create.return_value = MagicMock(content=[mock_content])

    mock_anthropic = MagicMock(APITimeoutError=Exception)
    mock_anthropic.Anthropic.return_value = mock_client

    with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
        result = _call_api("hello", "sk-test", None, "claude-sonnet-4-20250514", 1024)

    assert result.text == "response text"


def test_call_api_with_system_prompt() -> None:
    mock_client = MagicMock()
    mock_content = MagicMock()
    mock_content.text = "ok"
    mock_client.messages.create.return_value = MagicMock(content=[mock_content])

    mock_anthropic = MagicMock(APITimeoutError=Exception)
    mock_anthropic.Anthropic.return_value = mock_client

    with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
        _call_api("hello", "sk-test", "be helpful", "claude-sonnet-4-20250514", 1024)

    kwargs = mock_client.messages.create.call_args[1]
    assert kwargs["system"] == [
        {"type": "text", "text": "be helpful", "cache_control": {"type": "ephemeral"}},
    ]


def test_call_api_empty_response() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(content=[])

    mock_anthropic = MagicMock(APITimeoutError=Exception)
    mock_anthropic.Anthropic.return_value = mock_client

    with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
        result = _call_api("hello", "sk-test", None, "claude-sonnet-4-20250514", 1024)

    assert result.text == ""


def test_call_api_no_anthropic_package() -> None:
    with patch.dict("sys.modules", {"anthropic": None}):
        with pytest.raises(UnavailableError, match="anthropic package not installed"):
            _call_api("hello", "sk-test", None, "claude-sonnet-4-20250514", 1024)


def test_one_shot_api_uses_its_remaining_budget_without_retries(
    incrementing_monotonic_clock,
) -> None:
    mock_client = MagicMock()
    mock_client.with_options.return_value = mock_client
    mock_content = MagicMock(text="completion text")
    mock_client.messages.create.return_value = MagicMock(content=[mock_content])
    mock_anthropic = MagicMock(APITimeoutError=Exception)
    mock_anthropic.Anthropic.return_value = mock_client

    incrementing_monotonic_clock.step = 0.5
    with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
        result = call_claude(
            "hello",
            api_key="sk-test",
            model="claude-sonnet-4-6",
            timeout_seconds=10,
        )

    assert result.text == "completion text"
    assert mock_client.messages.create.call_args.kwargs["model"] == "claude-sonnet-4-6"
    assert mock_client.with_options.call_args.kwargs == {
        "timeout": 9.5,
        "max_retries": 0,
    }


def test_async_one_shot_api_sends_the_chosen_model() -> None:
    async_client = MagicMock()
    async_client.messages.create = AsyncMock(
        return_value=MagicMock(content=[MagicMock(text="async completion")]),
    )
    sdk = MagicMock(APITimeoutError=Exception)
    sdk.AsyncAnthropic.return_value = async_client

    with patch.dict("sys.modules", {"anthropic": sdk}):
        result = asyncio.run(acall_claude("hello", api_key="sk-test", model="claude-sonnet-4-6"))

    assert result.text == "async completion"
    assert async_client.messages.create.call_args.kwargs["model"] == "claude-sonnet-4-6"


def test_one_shot_api_timeout_uses_the_shared_timeout_reason() -> None:
    class APITimeoutError(Exception):
        pass

    mock_client = MagicMock()
    mock_client.with_options.return_value = mock_client
    mock_client.messages.create.side_effect = APITimeoutError()
    mock_anthropic = MagicMock(APITimeoutError=APITimeoutError)
    mock_anthropic.Anthropic.return_value = mock_client

    with (
        patch.dict("sys.modules", {"anthropic": mock_anthropic}),
        patch("agent_providers.claude.provider.time.monotonic", return_value=100.0),
    ):
        with pytest.raises(UnavailableError) as exc:
            _call_api("hello", "sk-test", None, "claude-sonnet-4-6", 1024, deadline=110.0)

    assert str(exc.value) == "deadline_exceeded"


def test_non_one_shot_api_timeout_keeps_the_provider_exception() -> None:
    class APITimeoutError(Exception):
        pass

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = APITimeoutError()
    mock_anthropic = MagicMock(APITimeoutError=APITimeoutError)
    mock_anthropic.Anthropic.return_value = mock_client

    with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
        with pytest.raises(APITimeoutError):
            _call_api("hello", "sk-test", None, "claude-sonnet-4-6", 1024)


def test_one_shot_cli_gives_the_provider_request_the_remaining_budget(
    incrementing_monotonic_clock,
) -> None:
    gate = MagicMock(return_value=Path("/usr/bin/claude"))
    completed = MagicMock(returncode=0, stdout='{"result": "completion text"}', stderr="")

    incrementing_monotonic_clock.step = 2.5
    with (
        patch("agent_providers.claude.provider.verify_no_builtin_cli_tools", gate),
        patch("subprocess.run", return_value=completed) as run,
    ):
        result = call_claude(
            "hello",
            model="claude-sonnet-4-6",
            timeout_seconds=10,
        )

    assert result.text == "completion text"
    gate.assert_called_once_with()
    assert run.call_args.kwargs["timeout"] == 7.5


# ── _call_cli / _acall_cli ──────────────────────────────────────────
#
# Both are gated by verify_no_builtin_cli_tools() / averify_no_builtin_
# cli_tools() before they build a command or spawn anything (Finding 1 of
# the #351 review — this is the one funnel every non-MCP CLI call shares,
# so every host one-shot caller inherits
# the gate without having to call it themselves). The tests below
# that exercise _call_cli's/_acall_cli's OWN behavior (command shape,
# secret scrubbing, error handling) bypass the gate via the fixture below;
# the gate's own wiring and behavior get their own tests further down.


@pytest.fixture
def _no_tool_gate_open(monkeypatch: pytest.MonkeyPatch):
    """Stand in for a CLI that already passed the tool-surface gate,
    resolved to ``/usr/bin/claude`` — so tests here can focus on what
    ``_call_cli``/``_acall_cli`` do with that verified binary."""
    monkeypatch.setattr(
        provider,
        "verify_no_builtin_cli_tools",
        lambda *, deadline=None: Path("/usr/bin/claude"),
    )
    monkeypatch.setattr(
        provider,
        "averify_no_builtin_cli_tools",
        AsyncMock(return_value=Path("/usr/bin/claude")),
    )


def test_call_cli_success(_no_tool_gate_open) -> None:
    json_output = json.dumps({"result": "cli response"})
    mock_proc = MagicMock(returncode=0, stdout=json_output, stderr="")

    with patch("subprocess.run", return_value=mock_proc):
        result = _call_cli("hello")

    assert result.text == "cli response"


@pytest.mark.parametrize(
    "entry_point",
    [
        lambda: call_claude("hello"),
        lambda: asyncio.run(acall_claude("hello")),
        lambda: asyncio.run(provider.acall_claude_with_mcp("hello", user_id="user-1")),
    ],
    ids=["call_claude", "acall_claude", "acall_claude_with_mcp"],
)
def test_error_result_raises_without_its_text_from_every_entry_point(
    _no_tool_gate_open, monkeypatch, caplog, entry_point
) -> None:
    caplog.set_level("DEBUG")
    refused_text = "Not logged in · Please run /login"
    json_output = json.dumps({"is_error": True, "result": refused_text})
    monkeypatch.setattr(
        provider,
        "verify_cli_tool_surface",
        AsyncMock(return_value=Path("/usr/bin/claude")),
    )

    install_claude_cli_children(
        monkeypatch, a_claude_cli_child(communicate=(json_output.encode(), b"")),
    )
    mock_proc = MagicMock(returncode=0, stdout=json_output, stderr="")

    with patch("subprocess.run", return_value=mock_proc):
        with pytest.raises(UnavailableError) as failure:
            entry_point()

    assert type(failure.value) is UnavailableError
    assert str(failure.value) == CLAUDE_CLI_UNAVAILABLE_DETAIL
    assert refused_text not in f"{failure.value!r} {caplog.text}"


def test_call_cli_uses_the_shared_process_pool_and_refuses_at_its_cap(
    _no_tool_gate_open,
    monkeypatch,
) -> None:
    set_claude_process_limit(monkeypatch, 1)
    homes = _record_the_homes_the_provider_opens(monkeypatch)
    run_calls = 0

    def fake_run(*_args: object, **_kwargs: object) -> MagicMock:
        nonlocal run_calls
        run_calls += 1
        with provider.CLAUDE_CLI_PROCESS_POOL._lock:
            assert len(provider.CLAUDE_CLI_PROCESS_POOL._reservations) == 1
        return MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        assert _call_cli("hello").text == "ok"

        live_turn = _reserve_zombie_process()
        with pytest.raises(UnavailableError) as exc:
            _call_cli("one too many")

    assert str(exc.value) == (
        "Claude CLI process pool is at its concurrency limit (1); refusing to start another"
    )
    assert run_calls == 1
    assert not homes[-1].path.exists()
    provider.CLAUDE_CLI_PROCESS_POOL.release(live_turn)


def _a_child_that_returns_with(returncode: int):
    def run_capturing(_child, **_kwargs):
        return MagicMock(returncode=returncode, stdout='{"result": "ok"}', stderr="")

    return run_capturing


def _a_child_that_times_out(_child, **_kwargs):
    raise provider.PipedProcessTimeout("claude", 1)


@pytest.mark.parametrize(
    ("run_capturing", "outcome"),
    [
        pytest.param(_a_child_that_returns_with(0), nullcontext(), id="success"),
        pytest.param(
            _a_child_that_returns_with(1), pytest.raises(UnavailableError), id="non-zero-exit",
        ),
        pytest.param(_a_child_that_times_out, pytest.raises(UnavailableError), id="timeout"),
    ],
)
def test_a_sync_turn_frees_its_slot_and_its_home_once_its_child_has_exited(
    _no_tool_gate_open, monkeypatch, run_capturing, outcome,
) -> None:
    set_claude_process_limit(monkeypatch, 1)
    homes = _record_the_homes_the_provider_opens(monkeypatch)
    during_the_run: list[tuple[int, bool]] = []

    def observed_run(child, **kwargs):
        during_the_run.append((held_claude_slots(), homes[0].path.exists()))
        return run_capturing(child, **kwargs)

    monkeypatch.setattr(provider, "run_capturing", observed_run)

    with outcome:
        _call_cli("hello")

    [home] = homes
    assert during_the_run == [(1, True)]
    assert (held_claude_slots(), home.path.exists()) == (0, False)


@pytest.mark.parametrize(
    ("deadline", "reason"),
    [
        (500.0, "deadline_exceeded"),
        (None, "Claude CLI timed out after 120s"),
    ],
)
def test_call_cli_timeout_names_the_deadline_only_under_a_caller_deadline(
    _no_tool_gate_open,
    monkeypatch: pytest.MonkeyPatch,
    deadline: float | None,
    reason: str,
) -> None:
    def run_capturing(_child, **_kwargs):
        raise provider.PipedProcessTimeout("claude", 1)

    monkeypatch.setattr(provider.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(provider, "run_capturing", run_capturing)

    with pytest.raises(UnavailableError) as exc:
        _call_cli("hello", deadline=deadline)

    assert str(exc.value) == reason


@pytest.mark.parametrize(
    "complete",
    [
        pytest.param(
            lambda: _call_api("hello", "sk-x", None, "claude-sonnet-4-6", 1024, deadline=99.0),
            id="api",
        ),
        pytest.param(lambda: _call_cli("hello", deadline=99.0), id="cli"),
    ],
)
def test_an_already_elapsed_deadline_refuses_the_completion_before_it_starts(
    _no_tool_gate_open,
    monkeypatch: pytest.MonkeyPatch,
    complete,
) -> None:
    completions_started: list[str] = []
    mock_client = MagicMock()
    mock_client.with_options.side_effect = lambda **_kwargs: completions_started.append("api")
    mock_anthropic = MagicMock(Anthropic=MagicMock(return_value=mock_client))
    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
    monkeypatch.setattr(provider.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        provider, "run_capturing", lambda *_a, **_k: completions_started.append("cli")
    )

    with pytest.raises(provider._DeadlineExhausted) as exc:
        complete()

    assert str(exc.value) == "deadline_exceeded"
    assert completions_started == []


def test_call_cli_strips_secrets_from_child_env(
    _no_tool_gate_open,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _leaked_secret_env_values().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", "/usr/bin")
    mock_proc = MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        _call_cli("hello")

    child_env = mock_run.call_args.kwargs["env"]
    for key in SECRET_ENV_KEYS:
        assert key not in child_env
    assert child_env["PATH"] == "/usr/bin"


def test_call_cli_with_system_prompt(_no_tool_gate_open) -> None:
    mock_proc = MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        _call_cli("hello", system="be helpful")

    cmd = mock_run.call_args[0][0]
    assert "--system-prompt" not in cmd
    assert "be helpful" not in cmd
    assert "hello" not in cmd
    assert mock_run.call_args.kwargs["input"] == "be helpful\n\nhello"


def test_acall_cli_keeps_prompt_and_system_out_of_argv(
    _no_tool_gate_open, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = a_claude_cli_child(communicate=(b'{"result":"ok"}', b""))
    spawns = install_claude_cli_children(monkeypatch, proc)

    result = asyncio.run(_acall_cli("secret prompt", system="secret system"))

    assert result.text == "ok"
    command = spawns.children[0].command
    assert "secret prompt" not in command
    assert "secret system" not in command
    proc.communicate.assert_awaited_once_with(b"secret system\n\nsecret prompt")


def test_acall_cli_strips_secrets_from_child_env(
    _no_tool_gate_open,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in _leaked_secret_env_values().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", "/usr/bin")
    spawns = install_claude_cli_children(
        monkeypatch, a_claude_cli_child(communicate=(b'{"result":"ok"}', b"")),
    )

    asyncio.run(_acall_cli("hello"))

    child_env = spawns.children[0].environment
    for key in SECRET_ENV_KEYS:
        assert key not in child_env
    assert child_env["PATH"] == "/usr/bin"


@pytest.mark.parametrize("failure", ["missing", "nonzero"])
def test_acall_cli_surfaces_a_missing_or_failed_binary(
    _no_tool_gate_open,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    monkeypatch.setattr(
        provider, "averify_no_builtin_cli_tools", AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    _install_a_start_that_ends(monkeypatch, failure)

    call = _acall_cli("hello")
    with pytest.raises(
        UnavailableError, match="Claude CLI (binary not found|is unavailable)",
    ) as raised:
        asyncio.run(call)

    assert isinstance(raised.value, CliUnavailableError) is (failure == "missing")


@pytest.mark.parametrize("failure", ["missing", "nonzero"])
def test_tool_turn_cli_surfaces_a_missing_or_failed_binary(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    monkeypatch.setattr(
        provider, "verify_cli_tool_surface", AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    monkeypatch.setattr(provider, "_write_mcp_config", lambda _spec, _user_id: "unused")
    monkeypatch.setattr(provider, "_unlink_quiet", lambda _path: None)
    _install_a_start_that_ends(monkeypatch, failure)

    call = provider.acall_claude_with_mcp("hello", user_id="user-1")
    with pytest.raises(UnavailableError, match="Claude CLI (binary not found|is unavailable)"):
        asyncio.run(call)


def test_call_cli_passes_model(_no_tool_gate_open) -> None:
    mock_proc = MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        _call_cli("hello", model="claude-haiku-4-5-20251001")

    cmd = mock_run.call_args[0][0]
    assert "--model" in cmd
    model_idx = cmd.index("--model")
    assert cmd[model_idx + 1] == "claude-haiku-4-5-20251001"


@pytest.fixture
def spawned_claude_children(_no_tool_gate_open, monkeypatch: pytest.MonkeyPatch) -> list:
    """Every CLI child the Claude provider starts, answered by a fake that exits cleanly."""
    def run_capturing(child, **_kwargs):
        children.append(child)
        return MagicMock(returncode=0, stdout='{"result":"ok"}', stderr="")

    def child_that_exits_cleanly(child, _stream_buffer_limit):
        if "stream-json" in child.arguments:
            return a_claude_cli_child(stream_lines=[b'{"type":"result","result":"ok"}\n'])
        return a_claude_cli_child(communicate=(b'{"result":"ok"}', b""))

    children = install_claude_cli_children(monkeypatch, factory=child_that_exits_cleanly).children
    monkeypatch.setattr(provider, "run_capturing", run_capturing)
    monkeypatch.setattr(
        provider, "verify_cli_tool_surface", AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    return children


@pytest.fixture
def anthropic_sdk() -> MagicMock:
    sdk = MagicMock(APITimeoutError=Exception)
    with patch.dict("sys.modules", {"anthropic": sdk}):
        yield sdk


def _assert_no_anthropic_client_built(sdk: MagicMock) -> None:
    sdk.Anthropic.assert_not_called()
    sdk.AsyncAnthropic.assert_not_called()


def _answer_text(result: object) -> str:
    return result[-1].text if isinstance(result, list) else result.text


def _claude_entry_point(name: str, model: str | None, api_key: str | None = None) -> object:
    if name == "call_claude":
        return call_claude("hi", api_key=api_key, model=model)
    if name == "acall_claude":
        return asyncio.run(acall_claude("hi", api_key=api_key, model=model))
    if name == "acall_claude_with_mcp":
        return asyncio.run(provider.acall_claude_with_mcp("hi", user_id="u-1", model=model))
    stream = acall_claude_with_mcp_stream("hi", user_id="u-1", model=model)
    return asyncio.run(_collect_stream_events(stream))


_CLI_ENTRY_POINTS = [
    "call_claude", "acall_claude", "acall_claude_with_mcp", "acall_claude_with_mcp_stream",
]


@pytest.mark.parametrize("entry_point", _CLI_ENTRY_POINTS)
def test_claude_entry_points_run_the_cli_default_without_a_model(
    entry_point: str, spawned_claude_children: list,
) -> None:
    answer = _claude_entry_point(entry_point, None)

    assert _answer_text(answer) == "ok"
    assert len(spawned_claude_children) == 1
    assert "--model" not in spawned_claude_children[0].arguments


@pytest.mark.parametrize("entry_point", _CLI_ENTRY_POINTS)
def test_claude_entry_points_pass_a_chosen_model_to_the_cli(
    entry_point: str, spawned_claude_children: list,
) -> None:
    answer = _claude_entry_point(entry_point, "opus")

    assert _answer_text(answer) == "ok"
    arguments = spawned_claude_children[0].arguments
    assert arguments[arguments.index("--model") + 1] == "opus"


@pytest.mark.parametrize("entry_point", ["stream_turn", "call_provider_once"])
@pytest.mark.parametrize("chosen_model", [None, "opus"])
def test_dispatch_runs_claude_over_the_cli_with_the_model_only_when_chosen(
    entry_point: str, chosen_model: str | None, spawned_claude_children: list,
) -> None:
    if entry_point == "call_provider_once":
        answer = call_provider_once(
            provider="claude", model=chosen_model, prompt="hi", timeout=5,
        )
    else:
        turn = drive_turn("claude", model=chosen_model)
        assert turn.error is None
        answer = turn.events[-1].text

    assert answer == "ok"
    arguments = spawned_claude_children[0].arguments
    if chosen_model is None:
        assert "--model" not in arguments
    else:
        assert arguments[arguments.index("--model") + 1] == chosen_model


def test_a_catalog_only_host_runs_a_one_shot_on_the_cli_default(
    spawned_claude_children: list,
) -> None:
    override_provider_runtime(turns=None)

    assert call_claude("hi").text == "ok"
    assert "--model" not in spawned_claude_children[0].arguments


async def _drain_stream() -> None:
    async for _event in provider.acall_claude_with_mcp_stream("hi", user_id="u-1"):
        pass


@pytest.mark.parametrize(
    "run_turn",
    [
        pytest.param(lambda: call_claude("hi"), id="completion"),
        pytest.param(lambda: asyncio.run(provider.acall_claude("hi")), id="async-completion"),
        pytest.param(
            lambda: asyncio.run(provider.acall_claude_with_mcp("hi", user_id="u-1")),
            id="tool-turn",
        ),
        pytest.param(lambda: asyncio.run(_drain_stream()), id="streaming-turn"),
    ],
)
def test_a_claude_turn_without_a_claude_block_is_refused_naming_the_provider(
    run_turn: Callable[[], object], spawned_claude_children: list,
) -> None:
    override_provider_runtime(claude=None)

    with pytest.raises(ProviderNotConfiguredError) as refused:
        run_turn()

    assert (refused.value.provider, refused.value.block) == ("claude", "claude")
    assert spawned_claude_children == []


@pytest.mark.parametrize("entry_point", ["call_claude", "acall_claude"])
def test_claude_api_branch_refuses_a_missing_model(
    entry_point: str, spawned_claude_children: list, anthropic_sdk: MagicMock,
) -> None:
    with pytest.raises(ProviderUnavailableError) as refused:
        _claude_entry_point(entry_point, None, api_key="sk-test")

    assert (refused.value.provider, refused.value.route, refused.value.reason.code) == (
        "claude", "api", ROUTE_FAILED,
    )
    _assert_no_anthropic_client_built(anthropic_sdk)
    assert spawned_claude_children == []


@pytest.mark.parametrize(
    ("entry_point", "api_key", "route"),
    [
        ("call_claude", None, "cli"),
        ("call_claude", "sk-test", "api"),
        ("acall_claude", None, "cli"),
        ("acall_claude", "sk-test", "api"),
        ("acall_claude_with_mcp", None, "cli"),
        ("acall_claude_with_mcp_stream", None, "cli"),
    ],
)
def test_claude_entry_points_refuse_an_empty_model(
    entry_point: str,
    api_key: str | None,
    route: str,
    spawned_claude_children: list,
    anthropic_sdk: MagicMock,
) -> None:
    with pytest.raises(ProviderUnavailableError) as refused:
        _claude_entry_point(entry_point, "", api_key=api_key)

    assert (refused.value.provider, refused.value.route, refused.value.reason.code) == (
        "claude", route, ROUTE_FAILED,
    )
    _assert_no_anthropic_client_built(anthropic_sdk)
    assert spawned_claude_children == []


def test_call_cli_plain_text_output(_no_tool_gate_open) -> None:
    mock_proc = MagicMock(returncode=0, stdout="plain text response", stderr="")

    with patch("subprocess.run", return_value=mock_proc):
        result = _call_cli("hello")

    assert result.text == "plain text response"


def test_call_cli_no_binary(monkeypatch) -> None:
    # The autouse conftest fixture stubs the gate out for every other test's
    # safety; undo that here so this test proves the real gate — not a
    # stand-in for it — is what surfaces "no binary" through _call_cli.
    monkeypatch.setattr(provider, "verify_no_builtin_cli_tools", verify_no_builtin_cli_tools)
    with patch("agent_providers.claude.provider._find_claude_binary", return_value=None):
        with pytest.raises(UnavailableError, match="Claude CLI not found"):
            _call_cli("hello")


def test_call_cli_error(_no_tool_gate_open) -> None:
    mock_proc = MagicMock(returncode=1, stdout="", stderr="error message")

    with patch("subprocess.run", return_value=mock_proc):
        with pytest.raises(UnavailableError, match="Claude CLI is unavailable"):
            _call_cli("hello")


def test_call_cli_timeout(_no_tool_gate_open) -> None:
    import subprocess

    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(
            cmd="claude",
            timeout=CLAUDE_CLI_COMPLETION_TIMEOUT_SECONDS,
        ),
    ):
        with pytest.raises(UnavailableError, match="timed out"):
            _call_cli("hello")


def test_call_cli_refuses_a_cli_the_gate_rejects(_no_tool_gate_open, monkeypatch) -> None:
    monkeypatch.setattr(
        provider,
        "verify_no_builtin_cli_tools",
        MagicMock(side_effect=CliToolSurfaceError("Bash")),
    )
    with patch("subprocess.run") as mock_run:
        with pytest.raises(CliToolSurfaceError):
            _call_cli("hello")
    mock_run.assert_not_called()


def test_acall_cli_refuses_a_cli_the_gate_rejects(_no_tool_gate_open, monkeypatch) -> None:
    monkeypatch.setattr(
        provider,
        "averify_no_builtin_cli_tools",
        AsyncMock(side_effect=CliToolSurfaceError("Bash")),
    )
    spawns = install_claude_cli_children(monkeypatch)
    call = _acall_cli("hello")
    with pytest.raises(CliToolSurfaceError):
        asyncio.run(call)
    assert spawns.children == []


def test_healthy_mcp_turn_holds_its_process_pool_reservation_until_it_exits(
    monkeypatch,
) -> None:
    deadline = ScenarioDeadline()
    turn_gate = EventGate(deadline)
    install_claude_cli_children(
        monkeypatch,
        a_claude_cli_child(
            pid=1234, communicate=(b'{"result":"ok"}', b""), communicate_gate=turn_gate,
        ),
    )
    monkeypatch.setattr(
        provider, "verify_cli_tool_surface", AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    monkeypatch.setattr(provider, "_write_mcp_config", lambda _spec, _user_id: "unused")
    monkeypatch.setattr(provider, "_unlink_quiet", lambda _path: None)

    async def run_turn() -> None:
        tasks: list[asyncio.Future[Any]] = []
        async with scenario_holds([turn_gate], tasks, set()):
            turn = asyncio.create_task(provider.acall_claude_with_mcp("hi", user_id="u-1"))
            tasks.append(turn)
            await turn_gate.started.wait()
            with provider.CLAUDE_CLI_PROCESS_POOL._lock:
                assert len(provider.CLAUDE_CLI_PROCESS_POOL._reservations) == 1
            turn_gate.release.set()
            assert (await turn).text == "ok"
            with provider.CLAUDE_CLI_PROCESS_POOL._lock:
                assert not provider.CLAUDE_CLI_PROCESS_POOL._reservations

    run_bounded(run_turn())


def test_mcp_turn_refuses_a_healthy_turn_when_the_shared_process_pool_is_full(
    monkeypatch,
) -> None:
    process_cap = 2
    set_claude_process_limit(monkeypatch, process_cap)
    monkeypatch.setattr(
        provider, "verify_cli_tool_surface", AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    monkeypatch.setattr(provider, "_write_mcp_config", lambda _spec, _user_id: "unused")
    monkeypatch.setattr(provider, "_unlink_quiet", lambda _path: None)
    deadline = ScenarioDeadline()
    release = asyncio.Event()
    gates = [EventGate(deadline, release=release) for _ in range(process_cap + 1)]
    processes = [
        a_claude_cli_child(
            pid=pid,
            communicate=(b'{"result":"ok"}', b""),
            communicate_gate=gate,
        )
        for pid, gate in enumerate(gates, start=1)
    ]
    spawned = install_claude_cli_children(monkeypatch, *processes).children

    async def run_turns() -> None:
        running: list[asyncio.Future[Any]] = []
        async with scenario_holds(gates, running, set()):
            for gate in gates[:process_cap]:
                running.append(
                    asyncio.create_task(provider.acall_claude_with_mcp("hi", user_id="u-1")),
                )
                await gate.started.wait()

            expected = (
                f"Claude CLI process pool is at its concurrency limit ({process_cap}); "
                "refusing to start another"
            )
            with pytest.raises(UnavailableError) as exc:
                await provider.acall_claude_with_mcp("hi", user_id="u-1")
            assert str(exc.value) == expected
            assert len(spawned) == process_cap

            release.set()
            assert [turn.text for turn in await asyncio.gather(*running)] == ["ok"] * process_cap
            assert (await provider.acall_claude_with_mcp("hi", user_id="u-1")).text == "ok"
            assert len(spawned) == process_cap + 1

    run_bounded(run_turns())


def test_a_saturated_pool_refuses_a_turn_with_no_chained_cause_or_context(
    monkeypatch,
) -> None:
    set_claude_process_limit(monkeypatch, 1)
    monkeypatch.setattr(
        provider,
        "verify_cli_tool_surface",
        AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    monkeypatch.setattr(provider, "_write_mcp_config", lambda _spec, _user_id: "unused")
    monkeypatch.setattr(provider, "_unlink_quiet", lambda _path: None)
    install_claude_cli_children(monkeypatch)
    held = provider.CLAUDE_CLI_PROCESS_POOL.reserve()
    assert held is not None

    turn = provider.acall_claude_with_mcp("hi", user_id="u-1")
    with pytest.raises(provider._ClaudeCliProcessPoolSaturated) as refusal:
        asyncio.run(turn)

    assert str(refusal.value) == (
        "Claude CLI process pool is at its concurrency limit (1); refusing to start another"
    )
    assert refusal.value.__cause__ is None
    assert refusal.value.__context__ is None


# ── the child a Claude turn runs as ─────────────────────────────────


def test_a_claude_child_gets_a_private_home_not_the_operators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "value-the-child-must-not-see")
    override_claude_runtime(token_file=write_claude_token_file(tmp_path / "claude-token"))

    login = provider._claude_login()
    home = provider._claude_private_home()
    child = provider._claude_child_process(Path("/usr/bin/claude"), ["-p"], home.path, login)

    assert child.environment["HOME"] == str(home.path)
    assert child.working_directory == home.path
    assert "ANTHROPIC_API_KEY" not in child.environment
    assert child.environment[CLAUDE_OAUTH_TOKEN_VARIABLE] == SAMPLE_CLAUDE_SETUP_TOKEN
    assert child.command == ("/usr/bin/claude", "-p")
    home.close()
    assert not child.working_directory.exists()


# ── the setup token a Claude child starts with ──────────────────────

_TOKEN_CANARY = "sk-ant-oat01-token-canary-5e2b81"
_VERIFIED_CLAUDE_BINARY = Path("/usr/bin/claude")
_AN_EMPTY_TOOL_SURFACE = CliRunOutcome(
    started=True,
    spawn_error=None,
    returncode=0,
    stdout=json.dumps({"type": "system", "subtype": "init", "tools": [], "slash_commands": []})
    + "\n",
    stderr="",
    complete=True,
    became_zombie=False,
    reason=CliRunReason.COMPLETE,
)

type _OnSpawn = Callable[[ChildProcess], None]


def _a_sound_token_file(source: Path, _tmp_path: Path) -> None:
    write_claude_token_file(source, _TOKEN_CANARY)


def _no_token_file(source: Path, _tmp_path: Path) -> None:
    source.unlink()


def _a_token_file_open_to_others(source: Path, _tmp_path: Path) -> None:
    write_claude_token_file(source, _TOKEN_CANARY, mode=0o644)


def _a_link_to_a_token_file_of_mode(mode: int) -> Callable[[Path, Path], None]:
    def link(source: Path, tmp_path: Path) -> None:
        target = write_claude_token_file(tmp_path / "linked-token", _TOKEN_CANARY, mode=mode)
        source.unlink()
        source.symlink_to(target)

    return link


def _a_token_file_over_the_bound(source: Path, _tmp_path: Path) -> None:
    write_claude_token_file(
        source, _TOKEN_CANARY * (MAX_SECRET_FILE_BYTES // len(_TOKEN_CANARY) + 1),
    )


def _a_credentials_document(source: Path, _tmp_path: Path) -> None:
    login = {"accessToken": _TOKEN_CANARY, "refreshToken": _TOKEN_CANARY}
    write_claude_token_file(source, json.dumps({"claudeAiOauth": login}))


SOUND_TOKEN_FILES = (
    pytest.param(_a_sound_token_file, id="0600-token"),
    pytest.param(_a_link_to_a_token_file_of_mode(0o600), id="link-to-0600"),
)
UNSOUND_TOKEN_FILES = (
    pytest.param(_no_token_file, id="missing"),
    pytest.param(_a_token_file_open_to_others, id="0644-token"),
    pytest.param(_a_link_to_a_token_file_of_mode(0o644), id="link-to-0644"),
    pytest.param(_a_token_file_over_the_bound, id="over-16KiB"),
    pytest.param(_a_credentials_document, id="json"),
)


def _async_claude_children_answering(
    monkeypatch: pytest.MonkeyPatch, on_spawn: _OnSpawn, **script: Any,
) -> None:
    def answer(child: ChildProcess, _stream_buffer_limit: int | None) -> ScriptedClaudeChild:
        on_spawn(child)
        return a_claude_cli_child(**script)

    install_claude_cli_children(monkeypatch, factory=answer)


def _start_the_mcp_completion(monkeypatch: pytest.MonkeyPatch, on_spawn: _OnSpawn) -> None:
    _async_claude_children_answering(monkeypatch, on_spawn, communicate=(b'{"result":"ok"}', b""))
    monkeypatch.setattr(
        provider, "verify_cli_tool_surface", AsyncMock(return_value=_VERIFIED_CLAUDE_BINARY),
    )
    assert asyncio.run(provider.acall_claude_with_mcp("hello", user_id="user-1")).text == "ok"


def _start_the_mcp_stream(monkeypatch: pytest.MonkeyPatch, on_spawn: _OnSpawn) -> None:
    _async_claude_children_answering(
        monkeypatch, on_spawn, stream_lines=[b'{"type":"result","result":"ok"}\n'],
    )
    monkeypatch.setattr(
        provider, "verify_cli_tool_surface", AsyncMock(return_value=_VERIFIED_CLAUDE_BINARY),
    )

    async def stream() -> list[StreamEvent]:
        return [event async for event in acall_claude_with_mcp_stream("hello", user_id="user-1")]

    assert asyncio.run(stream())[-1].text == "ok"


def _start_the_tool_surface_probe(monkeypatch: pytest.MonkeyPatch, on_spawn: _OnSpawn) -> None:
    def run_cli_bounded(child: ChildProcess, **kwargs: Any) -> CliRunOutcome:
        on_spawn(child)
        kwargs["on_reaped"](2468, False)
        return _AN_EMPTY_TOOL_SURFACE

    monkeypatch.setattr(provider.process, "run_cli_bounded", run_cli_bounded)
    assert _probe_the_surface_once().tools == ()


def _start_the_sync_completion(monkeypatch: pytest.MonkeyPatch, on_spawn: _OnSpawn) -> None:
    def run_capturing(child: ChildProcess, **_kwargs: Any) -> MagicMock:
        on_spawn(child)
        return MagicMock(returncode=0, stdout='{"result":"ok"}', stderr="")

    monkeypatch.setattr(provider, "run_capturing", run_capturing)
    monkeypatch.setattr(
        provider, "verify_no_builtin_cli_tools", lambda *, deadline=None: _VERIFIED_CLAUDE_BINARY,
    )
    assert call_claude("hello").text == "ok"


def _start_the_async_completion(monkeypatch: pytest.MonkeyPatch, on_spawn: _OnSpawn) -> None:
    _async_claude_children_answering(monkeypatch, on_spawn, communicate=(b'{"result":"ok"}', b""))
    monkeypatch.setattr(
        provider, "averify_no_builtin_cli_tools", AsyncMock(return_value=_VERIFIED_CLAUDE_BINARY),
    )
    assert asyncio.run(acall_claude("hello")).text == "ok"


CLAUDE_CHILD_STARTS = (
    pytest.param(_start_the_mcp_completion, id="mcp-completion"),
    pytest.param(_start_the_mcp_stream, id="mcp-stream"),
    pytest.param(_start_the_tool_surface_probe, id="tool-surface-probe"),
    pytest.param(_start_the_sync_completion, id="call-cli"),
    pytest.param(_start_the_async_completion, id="acall-cli"),
)


@pytest.mark.parametrize("token_file", SOUND_TOKEN_FILES)
@pytest.mark.parametrize("start_child", CLAUDE_CHILD_STARTS)
def test_a_claude_child_gets_the_setup_token_only_in_its_environment_never_a_login_file(
    start_child, token_file, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog,
) -> None:
    source = current_claude_runtime().token_file
    token_file(source, tmp_path)
    caplog.set_level(logging.DEBUG)
    homes: list[Path] = []

    start_child(
        monkeypatch,
        lambda child: homes.append(
            assert_claude_token_child(child, root=tmp_path, token=_TOKEN_CANARY),
        ),
    )

    [home] = homes
    assert not home.exists()
    assert _TOKEN_CANARY not in caplog.text
    assert source.read_text() == f"{_TOKEN_CANARY}\n"


@pytest.mark.parametrize("token_file", UNSOUND_TOKEN_FILES)
@pytest.mark.parametrize("start_child", CLAUDE_CHILD_STARTS)
def test_a_claude_child_with_an_unsound_token_file_is_refused_before_any_home(
    start_child, token_file, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog,
) -> None:
    token_file(current_claude_runtime().token_file, tmp_path)
    caplog.set_level(logging.DEBUG)
    homes = _record_the_homes_the_provider_opens(monkeypatch)
    spawned: list[ChildProcess] = []

    with pytest.raises(CliLoginNotConfiguredError) as refusal:
        start_child(monkeypatch, spawned.append)

    assert (homes, spawned) == ([], [])
    assert str(refusal.value) == CLAUDE_CLI_LOGIN_NOT_CONFIGURED_DETAIL
    assert type(refusal.value.__cause__) is process.CliLoginUnusableError
    assert not any(_TOKEN_CANARY in f"{link}{link!r}" for link in exception_chain(refusal.value))
    assert not traceback_frames_hold(refusal.value, _TOKEN_CANARY)
    assert _TOKEN_CANARY not in caplog.text


@pytest.mark.parametrize("start_child", CLAUDE_CHILD_STARTS)
def test_a_replaced_token_file_reaches_the_next_claude_child(
    start_child, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = current_claude_runtime().token_file
    tokens = ("sk-ant-oat01-first-setup-token", "sk-ant-oat01-second-setup-token")
    seen: list[str] = []

    for token in tokens:
        write_claude_token_file(source, token)
        start_child(
            monkeypatch, lambda child: seen.append(child.environment[CLAUDE_OAUTH_TOKEN_VARIABLE]),
        )

    assert tuple(seen) == tokens


def test_sync_claude_call_removes_its_home_when_the_arguments_fail(
    _no_tool_gate_open,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def refuse_arguments(_model):
        raise RuntimeError("no arguments")

    monkeypatch.setattr(provider, "_cli_arguments", refuse_arguments)
    before = set(tmp_path.iterdir())

    with pytest.raises(RuntimeError, match="no arguments") as held:
        call_claude("hello")

    assert held.value is not None
    assert set(tmp_path.iterdir()) == before


class _RefusedHomeRemoval:
    """Refuse every removal below the CLI root until lifted, recording what it refused."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.refused: set[Path] = set()
        self._root = current_config().cli_working_directory_root
        self._remove_tree = process.shutil.rmtree
        self._lifted = False
        monkeypatch.setattr(process.shutil, "rmtree", self._rmtree)

    def _rmtree(self, path) -> None:
        home = Path(path)
        if self._lifted or home.parent != self._root:
            self._remove_tree(path)
            return
        self.refused.add(home)
        raise PermissionError(13, "denied", str(home))

    def lift(self) -> None:
        self._lifted = True


def _removal_failure_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and record.name == process.__name__
    ]


def _assert_logged_by_class_alone(
    caplog: pytest.LogCaptureFixture, homes: set[Path],
) -> None:
    records = _removal_failure_records(caplog)
    assert records
    for record in records:
        message = record.getMessage()
        assert "PermissionError" in message
        assert record.exc_info is None
        assert not any(str(home) in message for home in homes)


def _sync_claude_call(fails: bool, monkeypatch: pytest.MonkeyPatch) -> object:
    def run_capturing(child, **_kwargs):
        if fails:
            raise spawn.PipedProcessTimeout(list(child.command), 1)
        return MagicMock(returncode=0, stdout='{"result":"ok"}', stderr="")

    monkeypatch.setattr(provider, "run_capturing", run_capturing)
    return call_claude("hello").text


def _cancelled_async_claude_call(fails: bool, monkeypatch: pytest.MonkeyPatch) -> object:
    communicating = EventGate(ScenarioDeadline())

    async def run() -> str:
        install_claude_cli_children(
            monkeypatch,
            a_claude_cli_child(
                communicate=(b'{"result":"ok"}', b""),
                communicate_gate=communicating if fails else None,
            ),
        )
        tasks: list[asyncio.Future[Any]] = []
        async with scenario_holds([communicating], tasks, set()):
            turn = asyncio.create_task(acall_claude("hello"))
            tasks.append(turn)
            if fails:
                await communicating.started.wait()
                turn.cancel()
            return (await turn).text

    return run_bounded(run())


def _mcp_claude_call(fails: bool, monkeypatch: pytest.MonkeyPatch) -> object:
    install_claude_cli_children(
        monkeypatch,
        a_claude_cli_child(communicate=(b'{"result":"ok"}', b""), returncode=1 if fails else 0),
    )
    return asyncio.run(provider.acall_claude_with_mcp("hello", user_id="user-1")).text


def _mcp_claude_stream(fails: bool, monkeypatch: pytest.MonkeyPatch) -> object:
    install_claude_cli_children(
        monkeypatch,
        a_claude_cli_child(
            stream_lines=[b'{"type":"result","result":"ok"}\n'], returncode=1 if fails else 0,
        ),
    )
    stream = acall_claude_with_mcp_stream("hello", user_id="user-1")
    return asyncio.run(_collect_stream_events(stream))[-1].text


def _claude_surface_probe(fails: bool, monkeypatch: pytest.MonkeyPatch) -> object:
    def run_cli_bounded(_child, **kwargs):
        if fails:
            kwargs["on_spawn_failed"]()
            return CliRunOutcome(
                started=False,
                spawn_error="spawn refused",
                returncode=None,
                stdout="",
                stderr="",
                complete=False,
                became_zombie=False,
                reason=CliRunReason.SPAWN_FAILED,
            )
        kwargs["on_reaped"](2468, False)
        return CliRunOutcome(
            started=True,
            spawn_error=None,
            returncode=0,
            stdout=json.dumps({
                "type": "system", "subtype": "init", "tools": [], "slash_commands": [],
            }) + "\n",
            stderr="",
            complete=True,
            became_zombie=False,
            reason=CliRunReason.COMPLETE,
        )

    monkeypatch.setattr(provider.process, "run_cli_bounded", run_cli_bounded)
    return provider._probe_cli_surface_sync(
        Path("/usr/bin/claude"), mcp=None, deadline=time.monotonic() + 5,
    ).tools


def _claude_catalog_probe(fails: bool, monkeypatch: pytest.MonkeyPatch) -> object:
    if fails:
        def broken_probe(*_args, **_kwargs):
            raise RuntimeError("catalog probe broke")

        monkeypatch.setattr(process, "run_catalog_cli", broken_probe)
    install_cli_door(monkeypatch, lambda *_cmd, **_kw: fake_cli_process(b"ok\n"))
    run = process.run_claude_catalog_cli(Path(sys.executable), ("-c", "print('ok')"))
    assert run is not None
    return run.stdout.strip()


def _grok_tool_turn(fails: bool, monkeypatch: pytest.MonkeyPatch) -> object:
    def run_cli_bounded(_child, **kwargs):
        channel = kwargs["stdout_line_channel"]
        for event in (
            {"type": "text", "data": "ok"},
            {"type": "end", "stopReason": "stop", "sessionId": _GROK_SESSION_ID},
        ):
            assert channel._send(json.dumps(event).encode() + b"\n")
        kwargs["on_reaped"](2468, False)
        outcome = CliRunOutcome(
            started=True,
            spawn_error=None,
            returncode=1 if fails else 0,
            stdout="",
            stderr="",
            complete=True,
            became_zombie=False,
            reason=CliRunReason.COMPLETE,
        )
        channel._close(outcome)
        return outcome

    monkeypatch.setattr(grok_transport, "run_cli_bounded", run_cli_bounded)

    async def turn() -> str:
        events = [
            event
            async for event in stream_tool_loop(
                provider="grok",
                route="cli",
                system="system",
                messages=[{"role": "user", "content": "hello"}],
                transport=grok_transport.GrokCliToolTransport(
                    model="grok-test", catalog=SAMPLE_TOOL_CATALOG,
                ),
                executor=lambda _name, _arguments: ToolOutcome("unused", False),
                tool_failure_message="tool failed",
            )
        ]
        return events[-1].text

    return asyncio.run(turn())


_GROK_SESSION_ID = "3e04bf5b-4e1c-4f26-8e1e-2f17c5f6d9cf"

_HOME_ENTRYPOINTS = {
    "claude-sync": (_sync_claude_call, UnavailableError, "ok"),
    "claude-async-cancelled": (_cancelled_async_claude_call, asyncio.CancelledError, "ok"),
    "claude-mcp": (_mcp_claude_call, UnavailableError, "ok"),
    "claude-mcp-stream": (_mcp_claude_stream, UnavailableError, "ok"),
    "claude-surface-probe": (_claude_surface_probe, UnavailableError, ()),
    "claude-catalog-probe": (_claude_catalog_probe, RuntimeError, "ok"),
    "grok-turn": (_grok_tool_turn, ProviderUnavailableError, "ok"),
}


@pytest.fixture
def collection_only_when_forced():
    was_enabled = gc.isenabled()
    gc.disable()
    yield
    if was_enabled:
        gc.enable()


@pytest.mark.parametrize(
    "collected_while_refused",
    (True, False),
    ids=("collected-while-refused", "collected-once-permitted"),
)
@pytest.mark.parametrize("entrypoint", _HOME_ENTRYPOINTS)
def test_a_refused_home_removal_never_replaces_a_failed_calls_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    collection_only_when_forced,
    entrypoint: str,
    collected_while_refused: bool,
) -> None:
    run, failure, _result = _HOME_ENTRYPOINTS[entrypoint]
    removal = _RefusedHomeRemoval(monkeypatch)
    caplog.set_level(logging.ERROR, logger=process.__name__)

    with pytest.raises(failure) as raised:
        run(True, monkeypatch)

    assert removal.refused
    assert all(home.exists() for home in removal.refused)
    _assert_logged_by_class_alone(caplog, removal.refused)
    raised.value.__traceback__ = None
    del raised
    if collected_while_refused:
        gc.collect()
    removal.lift()
    gc.collect()
    if collected_while_refused:
        assert all(home.exists() for home in removal.refused)
        process._sweep_failed_homes_at_exit()
    assert not any(home.exists() for home in removal.refused)


@pytest.mark.parametrize("entrypoint", _HOME_ENTRYPOINTS)
def test_a_refused_home_removal_returns_a_successful_calls_result(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    entrypoint: str,
) -> None:
    run, _failure, result = _HOME_ENTRYPOINTS[entrypoint]
    removal = _RefusedHomeRemoval(monkeypatch)
    caplog.set_level(logging.ERROR, logger=process.__name__)

    assert run(False, monkeypatch) == result

    assert removal.refused
    assert all(home.exists() for home in removal.refused)
    _assert_logged_by_class_alone(caplog, removal.refused)


@pytest.mark.parametrize(
    ("reason", "callback"),
    (
        (CliRunReason.DEADLINE_BEFORE_SPAWN, "on_spawn_failed"),
        (CliRunReason.CLEANUP_OVERRAN, "on_reaped"),
    ),
    ids=("late-spawn-failure", "late-reap"),
)
def test_claude_surface_probe_keeps_its_home_until_late_runner_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reason: CliRunReason,
    callback: str,
) -> None:
    captured: dict[str, object] = {}

    def run_cli_bounded(child, **kwargs):
        captured.update(kwargs)
        captured["home"] = assert_claude_token_child(
            child, root=tmp_path, token=SAMPLE_CLAUDE_SETUP_TOKEN,
        )
        return CliRunOutcome(
            started=reason is CliRunReason.CLEANUP_OVERRAN,
            spawn_error=None,
            returncode=None,
            stdout="",
            stderr="",
            complete=False,
            became_zombie=False,
            reason=reason,
        )

    monkeypatch.setattr(provider.process, "run_cli_bounded", run_cli_bounded)

    binary = Path("/usr/bin/claude")
    deadline = time.monotonic() + 1
    with pytest.raises(UnavailableError):
        provider._probe_cli_surface_sync(binary, mcp=None, deadline=deadline)

    home = captured["home"]
    assert isinstance(home, Path)
    assert home.exists()
    if callback == "on_spawn_failed":
        captured[callback]()
    else:
        captured[callback](2468, True)
    assert not home.exists()


def _probe_the_surface_once() -> provider._AnnouncedSurface:
    return provider._probe_cli_surface_sync(
        Path("/usr/bin/claude"), mcp=None, deadline=time.monotonic() + 1,
    )


def test_a_tool_surface_probe_whose_runner_raises_leaves_its_slot_and_home_to_the_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_claude_process_limit(monkeypatch, 2)
    captured: dict[str, Any] = {}

    def run_cli_bounded(child, **kwargs):
        captured.update(kwargs, home=child.working_directory)
        raise RuntimeError("the claimed start failed")

    monkeypatch.setattr(provider.process, "run_cli_bounded", run_cli_bounded)

    with pytest.raises(RuntimeError, match="the claimed start failed"):
        _probe_the_surface_once()
    after_the_raise = (held_claude_slots(), captured["home"].exists())
    captured["on_reaped"](2468, False)

    assert after_the_raise == (1, True)
    assert (held_claude_slots(), captured["home"].exists()) == (0, False)


def test_a_tool_surface_probe_that_fails_before_the_runner_holds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_claude_process_limit(monkeypatch, 2)
    homes: list[Path] = []
    ran = []

    def refuse_to_build(_binary, _arguments, home, _login):
        homes.append(home)
        raise RuntimeError("the child could not be described")

    monkeypatch.setattr(provider, "_claude_child_process", refuse_to_build)
    monkeypatch.setattr(provider.process, "run_cli_bounded", lambda *_a, **_k: ran.append(True))

    with pytest.raises(RuntimeError, match="could not be described"):
        _probe_the_surface_once()

    assert ran == []
    assert (held_claude_slots(), homes[0].exists()) == (0, False)


# ── _find_claude_binary ─────────────────────────────────────────────


def test_find_binary_on_the_configured_search_path() -> None:
    with patch(
        "agent_providers.claude.provider.resolve_cli_binary",
        return_value=Path("/usr/bin/claude"),
    ):
        assert _find_claude_binary() == Path("/usr/bin/claude")


def _install_vscode_style_binary(root: Path, version: str, *, executable: bool = True) -> Path:
    binary = root / f"anthropic.claude-code-{version}" / "resources" / "native-binary" / "claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh")
    if executable:
        binary.chmod(0o755)
    return binary


def test_find_binary_takes_the_newest_match_from_a_configured_search_location(
    tmp_path: Path,
) -> None:
    _install_vscode_style_binary(tmp_path, "1.0.0")
    newest = _install_vscode_style_binary(tmp_path, "2.0.0")
    override_claude_runtime(
        binary_search_globs=(
            str(tmp_path / "anthropic.claude-code-*" / "resources" / "native-binary" / "claude"),
        ),
    )

    with patch("agent_providers.claude.provider.resolve_cli_binary", return_value=None):
        assert _find_claude_binary() == newest


def test_find_binary_refuses_a_search_location_match_it_cannot_run(tmp_path: Path) -> None:
    """A glob hit is a candidate, not an answer: a child needs a runnable file."""
    _install_vscode_style_binary(tmp_path, "1.0.0", executable=False)
    override_claude_runtime(
        binary_search_globs=(
            str(tmp_path / "anthropic.claude-code-*" / "resources" / "native-binary" / "claude"),
        ),
    )

    with patch("agent_providers.claude.provider.resolve_cli_binary", return_value=None):
        assert _find_claude_binary() is None


def test_find_binary_not_found() -> None:
    override_claude_runtime(binary_search_globs=())

    with patch("agent_providers.claude.provider.resolve_cli_binary", return_value=None):
        assert _find_claude_binary() is None


# ── parse_json_response ─────────────────────────────────────────────


def test_parse_json_response_plain() -> None:
    result = parse_json_response('{"score": 8, "summary": "good"}')
    assert result["score"] == 8


def test_parse_json_response_markdown_wrapped() -> None:
    text = '```json\n{"score": 9}\n```'
    result = parse_json_response(text)
    assert result["score"] == 9


def test_parse_json_response_markdown_no_lang() -> None:
    text = '```\n{"score": 7}\n```'
    result = parse_json_response(text)
    assert result["score"] == 7


# ── tool allowlist ──────────────────────────────────────────────────


def _flag_value(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


def test_mcp_turn_command_offers_no_builtin_tool() -> None:
    cmd = _mcp_cli_arguments("opus", "/tmp/mcp.json", _configured_mcp_server())

    assert _flag_value(cmd, "--tools") == ""
    assert _flag_value(cmd, "--allowedTools") == SAMPLE_ALLOWED_TOOLS
    assert "--disallowedTools" not in cmd
    assert "bypassPermissions" not in cmd


def test_mcp_turn_command_ignores_the_mounted_settings_file() -> None:
    cmd = _mcp_cli_arguments("opus", "/tmp/mcp.json", _configured_mcp_server())

    assert _flag_value(cmd, "--setting-sources") == ""
    assert "--strict-mcp-config" in cmd


def test_mcp_turn_command_disables_slash_commands() -> None:
    cmd = _mcp_cli_arguments("opus", "/tmp/mcp.json", _configured_mcp_server())

    assert "--disable-slash-commands" in cmd


def test_tool_free_command_offers_no_tool_at_all() -> None:
    cmd = _cli_arguments("opus")

    assert _flag_value(cmd, "--tools") == ""
    assert "--allowedTools" not in cmd
    assert "--disable-slash-commands" in cmd


# ── the host declares its MCP server; this module holds no literal ────


def test_the_written_mcp_config_is_the_json_the_cli_expects() -> None:
    """Byte for byte, because the CLI parses this file and the file carries
    the database credentials: key order, separators, the unwrapped secrets
    and the turn's user appended last are all part of the contract."""
    spec = McpServerSpec(
        name="probe-server",
        command="/usr/bin/python3",
        args=("-m", "probe.server"),
        environment={
            "DATABASE_URL": SecretStr("postgresql://u:p@db/x"),
            "REDIS_URL": SecretStr("unused-here"),
        },
        user_id_environment_variable="PROBE_USER_ID",
        config_file_prefix="probe-",
        tool_names=frozenset({"get_note"}),
    )

    assert provider._build_mcp_config(spec, "u-1") == (
        '{"mcpServers": {"probe-server": {"command": "/usr/bin/python3", '
        '"args": ["-m", "probe.server"], "env": {"DATABASE_URL": '
        '"postgresql://u:p@db/x", "REDIS_URL": "unused-here", '
        '"PROBE_USER_ID": "u-1"}}}}'
    )


def test_the_mcp_config_file_is_readable_only_by_its_owner() -> None:
    path = provider._write_mcp_config(_configured_mcp_server(), "u-1")
    try:
        assert Path(path).name.startswith(_configured_mcp_server().config_file_prefix)
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
        assert Path(path).read_text() == provider._build_mcp_config(
            _configured_mcp_server(), "u-1",
        )
    finally:
        Path(path).unlink()


def test_a_deployment_without_an_mcp_server_runs_the_tool_free_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mcp_server=None`` is a valid configuration: the tool turn then
    runs the same command line the one-shot call does — no ``--mcp-config``, no
    ``--allowedTools`` — and writes no config file at all."""
    override_turn_runtime(mcp_server=None)
    monkeypatch.setattr(
        provider, "verify_cli_tool_surface", AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    monkeypatch.setattr(
        provider,
        "_write_mcp_config",
        lambda *_args: pytest.fail("no MCP config may be written without a server"),
    )
    spawns = install_claude_cli_children(
        monkeypatch,
        a_claude_cli_child(pid=1, communicate=(b'{"result":"ok"}', b"")),
    )

    response = asyncio.run(provider.acall_claude_with_mcp("hi", user_id="u-1", model="opus"))

    assert response.text == "ok"
    assert list(spawns.children[0].arguments) == _cli_arguments("opus")
    assert "--mcp-config" not in spawns.children[0].arguments
    assert "--allowedTools" not in spawns.children[0].arguments
    assert held_claude_slots() == 0


def test_a_deployment_without_an_mcp_server_streams_the_tool_free_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override_turn_runtime(mcp_server=None)
    monkeypatch.setattr(
        provider, "verify_cli_tool_surface", AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    monkeypatch.setattr(
        provider,
        "_write_mcp_config",
        lambda *_args: pytest.fail("no MCP config may be written without a server"),
    )
    spawns = install_claude_cli_children(monkeypatch, a_claude_cli_child(pid=1))

    async def one_final_event(_proc, _timeout, _correlation_id):
        yield FinalEvent(text="done")

    monkeypatch.setattr(provider, "_consume_stream", one_final_event)

    async def collect() -> list[StreamEvent]:
        return [
            event
            async for event in acall_claude_with_mcp_stream("hi", user_id="u-1", model="opus")
        ]

    assert [event.text for event in asyncio.run(collect())] == ["done"]
    assert list(spawns.children[0].arguments) == _cli_arguments("opus", stream=True)
    assert "--mcp-config" not in spawns.children[0].arguments
    assert "--allowedTools" not in spawns.children[0].arguments
    assert held_claude_slots() == 0


# ── tool-surface verification ───────────────────────────────────────
#
# Two gates share one probe mechanism (see the block comment above
# ``verify_cli_tool_surface`` in provider.py): the MCP-attached one used by
# the tool turn, expecting exactly the configured server's tools, and the
# no-builtin-tools one used by ``_call_cli``/``_acall_cli``, expecting none
# at all. Both are exercised below.


def _init_line(
    tools: list[str],
    *,
    slash_commands: list[str] | None = None,
    mcp_connected: bool = True,
) -> bytes:
    """A ``system``/``init`` line. ``mcp_connected`` only matters to the
    MCP-attached probe (the no-MCP probe never reads ``mcp_servers`` at
    all) — defaults to a connected host server so every existing
    MCP-attached test keeps proving what it always proved."""
    return (
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "tools": tools,
                "slash_commands": slash_commands or [],
                "mcp_servers": [
                    {"name": "notes", "status": "connected" if mcp_connected else "failed"},
                ],
            }
        ).encode()
        + b"\n"
    )


@pytest.fixture
def claude_binary(tmp_path: Path):
    """A stand-in binary file, so its build identity can be stat()ed."""
    binary = tmp_path / "claude"
    binary.write_bytes(b"cli-build-one")
    with patch(
        "agent_providers.claude.provider._require_claude_binary",
        return_value=binary,
    ):
        yield binary


def _answer_with(monkeypatch, *lines: bytes) -> list[tuple[str, ...]]:
    """Let the next probes read ``lines`` in turn; collect the commands used."""
    commands: list[tuple[str, ...]] = []
    queued = list(lines)

    def fake_door(child, **_kw):
        commands.append(tuple(child.command))
        return fake_cli_process(queued.pop(0))

    install_cli_door(monkeypatch, fake_door)
    return commands


_ALL_SAMPLE_TOOLS = sorted(f"mcp__notes__{name}" for name in MCP_TOOL_NAMES)


def test_tool_surface_accepts_a_cli_offering_exactly_the_configured_mcp_tools(
    claude_binary,
    monkeypatch,
) -> None:
    _answer_with(monkeypatch, _init_line(_ALL_SAMPLE_TOOLS))

    binary = asyncio.run(verify_cli_tool_surface())

    assert binary == claude_binary


def test_tool_surface_rejects_a_cli_offering_an_unlisted_tool(
    claude_binary,
    monkeypatch,
) -> None:
    _answer_with(monkeypatch, _init_line([*_ALL_SAMPLE_TOOLS, "Bash"]))

    probe = verify_cli_tool_surface()
    with pytest.raises(CliToolSurfaceError) as exc:
        asyncio.run(probe)
    assert "Bash" in str(exc.value)


def test_tool_surface_rejects_a_cli_offering_fewer_than_the_configured_tools(
    claude_binary,
    monkeypatch,
) -> None:
    """A drift check that only flags additions would miss the host
    server silently losing a registration too — #351 Finding 2 wants the
    reported set compared exactly, not just checked for extras."""
    _answer_with(monkeypatch, _init_line(_ALL_SAMPLE_TOOLS[:-1]))

    probe = verify_cli_tool_surface()
    with pytest.raises(CliToolSurfaceError) as exc:
        asyncio.run(probe)
    assert "missing tools" in str(exc.value)
    assert _ALL_SAMPLE_TOOLS[-1] in str(exc.value)


def test_tool_surface_rejects_a_cli_that_still_advertises_slash_commands(
    claude_binary,
    monkeypatch,
) -> None:
    _answer_with(
        monkeypatch,
        _init_line(_ALL_SAMPLE_TOOLS, slash_commands=["/compact"]),
    )

    probe = verify_cli_tool_surface()
    with pytest.raises(CliToolSurfaceError) as exc:
        asyncio.run(probe)
    assert "/compact" in str(exc.value)


def test_tool_surface_is_probed_with_the_mcp_turn_restrictions(
    claude_binary,
    monkeypatch,
) -> None:
    commands = _answer_with(monkeypatch, _init_line(_ALL_SAMPLE_TOOLS))

    asyncio.run(verify_cli_tool_surface())

    probe = list(commands[0])
    assert _flag_value(probe, "--tools") == ""
    assert _flag_value(probe, "--setting-sources") == ""
    assert "--strict-mcp-config" in probe
    assert "--disable-slash-commands" in probe
    assert _flag_value(probe, "--allowedTools") == SAMPLE_ALLOWED_TOOLS
    assert "--mcp-config" in probe


def _mounted_build(tmp_path, monkeypatch) -> Path:
    """A mounted ``claude`` symlink into a versioned build, as deployment has it.

    The two command lines can only agree on this build path if the turn
    really carries what the gate resolved and returned, rather than resolving
    the mounted name a second time itself.
    """
    build = tmp_path / "claude-2.1.257"
    build.write_bytes(b"cli-build-one")
    mounted = tmp_path / "claude"
    mounted.symlink_to(build)
    monkeypatch.setattr(provider, "_find_claude_binary", lambda: mounted)
    return build


def _assert_the_turn_runs_what_the_probe_verified(
    probed: list[str],
    executed: list[str],
    build: Path,
    *,
    mcp_attached: bool,
) -> None:
    """A probe that verified a different command line proves nothing about a turn."""
    assert probed[0] == executed[0] == str(build)
    isolation_flags = ["--tools", "--setting-sources"]
    if mcp_attached:
        isolation_flags.append("--allowedTools")
    for flag in isolation_flags:
        assert _flag_value(probed, flag) == _flag_value(executed, flag)
    for flag in ("--strict-mcp-config", "--disable-slash-commands"):
        assert flag in probed
        assert flag in executed
    assert ("--mcp-config" in probed) is mcp_attached
    assert ("--mcp-config" in executed) is mcp_attached
    assert ("--allowedTools" in probed) is mcp_attached
    assert ("--allowedTools" in executed) is mcp_attached


def test_the_mcp_turn_runs_the_same_command_its_probe_verified(
    tmp_path,
    monkeypatch,
) -> None:
    build = _mounted_build(tmp_path, monkeypatch)
    monkeypatch.setattr(provider, "verify_cli_tool_surface", verify_cli_tool_surface)
    probe_commands = _answer_with(monkeypatch, _init_line(_ALL_SAMPLE_TOOLS))
    turn_spawns = install_claude_cli_children(
        monkeypatch, a_claude_cli_child(pid=4242, communicate=(b'{"result": "ok"}', b"")),
    )

    asyncio.run(provider.acall_claude_with_mcp(prompt="hi", user_id="u-1"))

    _assert_the_turn_runs_what_the_probe_verified(
        list(probe_commands[0]), list(turn_spawns.children[0].command), build, mcp_attached=True,
    )


def test_the_streamed_mcp_turn_runs_the_same_command_its_probe_verified(
    tmp_path,
    monkeypatch,
) -> None:
    """The streaming twin of the tool turn carries the same assurance.

    It builds its own command line from the gate's answer, so a future
    caller that re-resolved the mounted binary would ship a turn nobody
    verified.
    """
    build = _mounted_build(tmp_path, monkeypatch)
    monkeypatch.setattr(provider, "verify_cli_tool_surface", verify_cli_tool_surface)
    probe_commands = _answer_with(monkeypatch, _init_line(_ALL_SAMPLE_TOOLS))
    turn_spawns = install_claude_cli_children(
        monkeypatch, a_claude_cli_child(pid=4242),
    )

    async def one_final_event(_proc, _timeout, _correlation_id):
        yield FinalEvent(text="done")

    monkeypatch.setattr(provider, "_consume_stream", one_final_event)

    async def collect() -> list[StreamEvent]:
        return [
            event
            async for event in provider.acall_claude_with_mcp_stream("hi", user_id="u-1")
        ]

    assert [event.text for event in asyncio.run(collect())] == ["done"]
    _assert_the_turn_runs_what_the_probe_verified(
        list(probe_commands[0]), list(turn_spawns.children[0].command), build, mcp_attached=True,
    )


def test_the_tool_free_turn_runs_the_same_command_its_probe_verified(
    tmp_path,
    monkeypatch,
) -> None:
    """``_call_cli`` — the one-shot calls' shared funnel.

    Its gate holds the CLI to no tool at all, so probe and turn must agree on
    the same build and the same isolation flags, and neither may attach an
    MCP server.
    """
    build = _mounted_build(tmp_path, monkeypatch)
    monkeypatch.setattr(provider, "verify_no_builtin_cli_tools", verify_no_builtin_cli_tools)
    probe_commands = _answer_with(monkeypatch, _init_line([]))
    turn = MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")

    with patch("subprocess.run", return_value=turn) as run:
        _call_cli("hello")

    _assert_the_turn_runs_what_the_probe_verified(
        list(probe_commands[0]), list(run.call_args[0][0]), build, mcp_attached=False,
    )


def test_the_async_tool_free_turn_runs_the_same_command_its_probe_verified(
    tmp_path,
    monkeypatch,
) -> None:
    """``_acall_cli`` carries the same assurance as its synchronous twin."""
    build = _mounted_build(tmp_path, monkeypatch)
    monkeypatch.setattr(
        provider, "averify_no_builtin_cli_tools", averify_no_builtin_cli_tools,
    )
    probe_commands = _answer_with(monkeypatch, _init_line([]))
    turn_spawns = install_claude_cli_children(
        monkeypatch, a_claude_cli_child(pid=4242, communicate=(b'{"result":"ok"}', b"")),
    )

    asyncio.run(_acall_cli("hello"))

    _assert_the_turn_runs_what_the_probe_verified(
        list(probe_commands[0]), list(turn_spawns.children[0].command), build, mcp_attached=False,
    )


def test_tool_surface_gate_expects_no_tool_when_no_mcp_server_is_configured(
    claude_binary,
    monkeypatch,
) -> None:
    """Without a configured MCP server the tool turn runs the tool-free
    command line, so its gate must hold the CLI to the tool-free
    expectation — and probe without ``--mcp-config``, which the
    scoring-worker container could not answer anyway."""
    override_turn_runtime(mcp_server=None)
    monkeypatch.setattr(provider, "averify_no_builtin_cli_tools", averify_no_builtin_cli_tools)
    commands = _answer_with(monkeypatch, _init_line([]))

    binary = asyncio.run(verify_cli_tool_surface())

    assert binary == claude_binary
    assert "--mcp-config" not in commands[0]
    assert "--allowedTools" not in commands[0]
    assert provider.claude_cli_tool_surface_health() == "ok"


def test_tool_surface_gate_refuses_any_tool_when_no_mcp_server_is_configured(
    claude_binary,
    monkeypatch,
) -> None:
    override_turn_runtime(mcp_server=None)
    monkeypatch.setattr(provider, "averify_no_builtin_cli_tools", averify_no_builtin_cli_tools)
    _answer_with(monkeypatch, _init_line(["Bash"]))

    probe = verify_cli_tool_surface()
    with pytest.raises(CliToolSurfaceError) as exc:
        asyncio.run(probe)

    assert "Bash" in str(exc.value)
    assert provider.claude_cli_tool_surface_health() == "drift"


def test_the_follower_waits_the_runners_settle_margin_and_its_own_margin_past_the_probe_timeout(
) -> None:
    margin = provider._cleanup_margin_seconds()

    assert margin == process.cli_settle_margin_seconds() + provider._FOLLOWER_WAIT_MARGIN_SECONDS
    assert margin == pytest.approx(13.1)
    assert provider._follower_wait_budget_seconds(15.0) == 15.0 + margin


def _signals_sent(child) -> list[signal.Signals]:
    return [operation[1] for operation in child.operations if operation[0] == "signal_group"]


def test_tool_surface_probe_stops_the_session_it_started(
    claude_binary,
    monkeypatch,
) -> None:
    child = fake_cli_process(_init_line(_ALL_SAMPLE_TOOLS), still_running=True)
    install_cli_door(monkeypatch, lambda *_cmd, **_kw: child)

    asyncio.run(verify_cli_tool_surface())

    assert _signals_sent(child) == [signal.SIGTERM]
    assert child.released


# ── the shared process pool ────────────────────────────────────────


def _reserve_zombie_process() -> anchored_turn.CliProcessReservation:
    reservation = provider.CLAUDE_CLI_PROCESS_POOL.reserve()
    assert reservation is not None
    return reservation


def test_zombie_reap_reservations_have_a_hard_cap(monkeypatch, caplog) -> None:
    caplog.set_level("ERROR")
    set_claude_process_limit(monkeypatch, 2)

    reservations = [_reserve_zombie_process(), _reserve_zombie_process()]
    assert provider.CLAUDE_CLI_PROCESS_POOL.reserve() is None
    assert any("at its concurrency limit (2)" in r.message for r in caplog.records)

    for reservation in reservations:
        provider.CLAUDE_CLI_PROCESS_POOL.release(reservation)
    assert held_claude_slots() == 0


def test_shutdown_tool_surface_background_tasks_cancels_a_probe_runner_task(
    claude_binary,
    monkeypatch,
) -> None:
    probe_started = asyncio.Event()
    probe_cancelled = asyncio.Event()

    async def pending_probe(*_args, **_kwargs) -> provider._AnnouncedSurface:
        probe_started.set()
        try:
            await asyncio.Future()
        finally:
            probe_cancelled.set()

    monkeypatch.setattr(provider, "_probe_cli_surface_async", pending_probe)

    async def _run() -> None:
        waiter = asyncio.create_task(averify_no_builtin_cli_tools())
        await probe_started.wait()
        await provider.shutdown_tool_surface_background_tasks()
        await probe_cancelled.wait()
        with pytest.raises(UnavailableError):
            await waiter

    asyncio.run(asyncio.wait_for(_run(), timeout=2))


def test_tool_surface_is_probed_again_after_the_cli_updates_itself(
    claude_binary,
    monkeypatch,
) -> None:
    _answer_with(
        monkeypatch,
        _init_line(_ALL_SAMPLE_TOOLS),
        _init_line([*_ALL_SAMPLE_TOOLS, "FutureTool"]),
    )

    asyncio.run(verify_cli_tool_surface())
    claude_binary.write_bytes(b"cli-build-two-is-a-different-size")

    probe = verify_cli_tool_surface()
    with pytest.raises(CliToolSurfaceError) as exc:
        asyncio.run(probe)
    assert "FutureTool" in str(exc.value)


def test_tool_surface_is_probed_once_per_cli_build(
    claude_binary,
    monkeypatch,
) -> None:
    """Proves the cache across two *sequential* calls — not concurrency;
    see test_tool_surface_single_flight_serializes_concurrent_probes below
    for that (#351 round 3, Finding 2: a sequential-only stampede test
    proves nothing about two callers racing for a cold cache)."""
    commands = _answer_with(monkeypatch, _init_line(_ALL_SAMPLE_TOOLS))

    asyncio.run(verify_cli_tool_surface())
    asyncio.run(verify_cli_tool_surface())

    assert len(commands) == 1


def test_tool_surface_single_flight_shares_one_successful_probe(
    claude_binary,
    monkeypatch,
) -> None:
    """#351 round 3, Finding 2: two callers racing for the same cold key
    must share one probe. Genuine concurrency via two real asyncio tasks —
    a threading.Event (the spawn now happens on a worker thread — round 6)
    holds the first probe open until both tasks have had a chance to run,
    not a sleep. Covers the success path only — see the fail-closed test
    below for the mutations this one alone cannot catch."""
    calls = 0
    probe_started = threading.Event()
    release_probe = threading.Event()

    def fake_popen(*_cmd, **_kw):
        nonlocal calls
        calls += 1
        probe_started.set()
        release_probe.wait()
        return fake_cli_process(_init_line(_ALL_SAMPLE_TOOLS))

    install_cli_door(monkeypatch, fake_popen)

    async def _race() -> tuple[str, str]:
        first = asyncio.create_task(verify_cli_tool_surface())
        second = asyncio.create_task(verify_cli_tool_surface())
        await asyncio.to_thread(probe_started.wait, 5)
        release_probe.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(asyncio.wait_for(_race(), timeout=5))

    assert calls == 1
    assert first == second == claude_binary


def test_tool_surface_single_flight_waits_for_the_real_result_not_a_placeholder(
    claude_binary,
    monkeypatch,
) -> None:
    """#351 rounds 4-5, Finding 2: two mutations a bare ``calls == 1`` check
    on a *successful* probe cannot catch, both deterministically red here:

    1. A follower that sees the key "in flight" and returns some default
       immediately instead of waiting for the real answer.
    2. A follower that, on discovering the leader's probe *failed*, starts
       its own second probe instead of accepting the shared failure —
       invisible to a success-only test, since ``calls`` would still read 1
       there. The probe here fails outright (malformed output), not just a
       tool mismatch, specifically to exercise that path.

    No sleep loop stands in for "the follower has reached its wait": an
    explicit event, set from inside ``_await_follower_result_async`` right
    before it actually waits, is what the test awaits. The 5s wrapper is a
    deadlock guard only — everything above resolves in well under 100ms.
    """
    calls = 0
    probe_started = threading.Event()
    release_probe = threading.Event()
    follower_waiting = asyncio.Event()

    def fake_popen(*_cmd, **_kw):
        nonlocal calls
        calls += 1
        probe_started.set()
        release_probe.wait()
        return fake_cli_process(b"not valid json\n")

    install_cli_door(monkeypatch, fake_popen)

    real_await_follower = provider._await_follower_result_async

    async def _instrumented_await_follower(build, future, timeout_seconds):
        follower_waiting.set()
        return await real_await_follower(build, future, timeout_seconds)

    monkeypatch.setattr(
        provider,
        "_await_follower_result_async",
        _instrumented_await_follower,
    )

    async def _race() -> list[BaseException]:
        first = asyncio.create_task(verify_cli_tool_surface())
        second = asyncio.create_task(verify_cli_tool_surface())
        await asyncio.to_thread(probe_started.wait, 5)
        await follower_waiting.wait()
        assert not second.done(), (
            "second caller returned before the in-flight probe resolved — "
            "it must wait for the real answer, not assume success"
        )
        release_probe.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    results = asyncio.run(asyncio.wait_for(_race(), timeout=5))

    assert calls == 1, "a follower must not start its own second probe on failure"
    assert all(isinstance(r, UnavailableError) for r in results)
    assert not any(isinstance(r, CliToolSurfaceError) for r in results)


def test_tool_surface_single_flight_shares_a_zombie_failure_across_concurrent_callers(
    claude_binary,
    monkeypatch,
) -> None:
    """A process that outlives SIGKILL turns into a _ZombieProbeError, not
    a hang: single-flight must still resolve *both* concurrent callers to
    that one refusal. The probe itself is stubbed to fail instantly — no
    real timing anywhere, deliberately, since the reap bound is already
    proven at the unit level above; this is only about single-flight
    correctly propagating a zombie failure to every waiter.

    Needs the ``claude_binary`` fixture like its sibling tests: without
    it, ``_require_claude_binary()`` resolves whatever real ``claude``
    happens to be on *this* machine's ``PATH`` — present on a desktop
    with the CLI installed (green), absent on CI (``UnavailableError``
    raised before ``fake_probe`` is ever reached, so ``calls`` stays 0
    and the single-flight claim under test never actually runs). Not a
    real race — the coroutines already register/join the in-flight
    future synchronously before either ever yields — but a hidden
    dependency on real host state, same shape as depending on a real
    service.
    """
    calls = 0

    async def fake_probe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise provider._ZombieProbeError(
            "Claude CLI process group 6161 did not exit within its budget",
        )

    monkeypatch.setattr(provider, "_probe_cli_surface_async", fake_probe)

    async def _race() -> list[BaseException]:
        return await asyncio.gather(
            averify_no_builtin_cli_tools(),
            averify_no_builtin_cli_tools(),
            return_exceptions=True,
        )

    results = asyncio.run(asyncio.wait_for(_race(), timeout=2))

    assert calls == 1
    assert all(isinstance(r, UnavailableError) for r in results)


def test_tool_surface_reports_a_cli_that_vanished_mid_update(
    claude_binary,
    monkeypatch,
) -> None:
    claude_binary.unlink()

    probe = verify_cli_tool_surface()
    with pytest.raises(UnavailableError):
        asyncio.run(probe)


def test_tool_surface_rejects_a_cli_that_announces_nothing(
    claude_binary,
    monkeypatch,
) -> None:
    _answer_with(monkeypatch, b'{"type": "assistant"}\n')

    probe = verify_cli_tool_surface()
    with pytest.raises(UnavailableError):
        asyncio.run(probe)


def test_tool_surface_rejects_an_init_event_with_the_wrong_subtype(
    claude_binary,
    monkeypatch,
) -> None:
    line = (
        json.dumps(
            {
                "type": "system",
                "subtype": "not_init",
                "tools": [],
                "slash_commands": [],
            }
        ).encode()
        + b"\n"
    )
    _answer_with(monkeypatch, line)

    probe = verify_cli_tool_surface()
    with pytest.raises(UnavailableError):
        asyncio.run(probe)


def test_tool_surface_failure_is_cached_across_sequential_calls(
    claude_binary,
    monkeypatch,
) -> None:
    """Sequential calls only — see
    test_tool_surface_single_flight_serializes_concurrent_probes for the
    genuine-concurrency case a "stampede" claim actually needs (#351 round
    3, Finding 2)."""
    commands = _answer_with(monkeypatch, b"not json\n")

    first_probe = verify_cli_tool_surface()
    with pytest.raises(UnavailableError):
        asyncio.run(first_probe)
    second_probe = verify_cli_tool_surface()
    with pytest.raises(UnavailableError):
        asyncio.run(second_probe)

    assert len(commands) == 1


_INIT_MARKER = "INIT-MARKER-5d2e81"


@pytest.mark.parametrize(
    "init_line",
    [
        b"\xff" + json.dumps({"type": "system", "subtype": "init", "note": _INIT_MARKER}).encode(),
        json.dumps({"type": "system", "subtype": "init", "note": _INIT_MARKER}).encode("utf-16"),
    ],
    ids=["non-utf8", "utf16"],
)
def test_an_init_line_that_is_not_utf8_json_is_refused_with_the_closed_init_error(
    init_line, caplog,
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(UnavailableError) as refused:
        _parse_announced_surface(init_line, mcp=None)

    carried = [str(refused.value), repr(refused.value), repr(refused.value.args)]
    assert str(refused.value) == "Claude CLI did not open with the expected session init event"
    assert (refused.value.__cause__, refused.value.__context__) == (None, None)
    assert [_INIT_MARKER in text for text in carried] == [False] * len(carried)
    assert _INIT_MARKER not in caplog.text


def test_a_probe_reads_a_malformed_server_entry_as_not_connected() -> None:
    spec = McpServerSpec(
        name="notes",
        command="/usr/bin/python3",
        args=(),
        environment={},
        user_id_environment_variable="PROBE_USER_ID",
        config_file_prefix="probe-",
        tool_names=frozenset({"get_note"}),
    )
    init = json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "tools": [],
            "slash_commands": [],
            "mcp_servers": [1, {"name": "notes", "status": "connected"}],
        }
    ).encode()

    surface = _parse_announced_surface(init, mcp=_AttachedMcpServer(spec, "/tmp/unused.json"))

    assert surface.mcp_connected is False


def test_tool_surface_failure_cache_expires_so_a_repair_takes_effect(
    claude_binary,
    monkeypatch,
) -> None:
    commands = _answer_with(monkeypatch, b"not json\n", _init_line(_ALL_SAMPLE_TOOLS))
    clock = {"now": time.monotonic()}
    monkeypatch.setattr(provider, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

    first_probe = verify_cli_tool_surface()
    with pytest.raises(UnavailableError):
        _run_with_clock(first_probe, clock)

    clock["now"] += provider.CLAUDE_CLI_TOOL_SURFACE_FAILURE_CACHE_SECONDS + 1
    _run_with_clock(verify_cli_tool_surface(), clock)

    assert len(commands) == 2


def test_tool_surface_treats_a_failed_mcp_connection_as_a_failure_not_a_permanent_verdict(
    claude_binary,
    monkeypatch,
) -> None:
    """#351 round 3, Finding 1: a failed MCP connection reports a valid
    init event with tools=[] — the same shape "every tool genuinely
    missing" has. Confusing the two used to cache the failure forever in
    the success cache, which no repair — not even a later clean probe —
    could ever override. It must instead be a short-lived failure: a
    second call, once that TTL passes, reaches its own, real probe."""
    commands = _answer_with(
        monkeypatch,
        _init_line([], mcp_connected=False),
        _init_line(_ALL_SAMPLE_TOOLS),
    )
    clock = {"now": time.monotonic()}
    monkeypatch.setattr(provider, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

    failed_probe = verify_cli_tool_surface()
    with pytest.raises(UnavailableError) as exc:
        _run_with_clock(failed_probe, clock)
    assert not isinstance(exc.value, CliToolSurfaceError)

    clock["now"] += provider.CLAUDE_CLI_TOOL_SURFACE_FAILURE_CACHE_SECONDS + 1
    binary = _run_with_clock(verify_cli_tool_surface(), clock)

    assert binary == claude_binary
    assert len(commands) == 2


# ── #351 round 7, Finding 4: /health follows the gate's live verdict ─


def test_claude_cli_tool_surface_health_transitions_from_unverified_to_ok(
    claude_binary,
    monkeypatch,
) -> None:
    """/health must not be a frozen boot snapshot — a later successful
    tool turn probe (e.g. the CLI was briefly unreachable at boot but is
    fine by the time someone actually opens a chat) must clear an
    earlier "unverified"."""
    commands = _answer_with(monkeypatch, b"not json\n", _init_line(_ALL_SAMPLE_TOOLS))
    clock = {"now": time.monotonic()}
    monkeypatch.setattr(provider, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

    failed_probe = verify_cli_tool_surface()
    with pytest.raises(UnavailableError):
        _run_with_clock(failed_probe, clock)
    assert provider.claude_cli_tool_surface_health() == "unverified"

    clock["now"] += provider.CLAUDE_CLI_TOOL_SURFACE_FAILURE_CACHE_SECONDS + 1
    _run_with_clock(verify_cli_tool_surface(), clock)

    assert provider.claude_cli_tool_surface_health() == "ok"
    assert len(commands) == 2


def test_claude_cli_tool_surface_health_transitions_from_ok_to_drift_after_a_build_change(
    claude_binary,
    monkeypatch,
) -> None:
    """A later drifted build (after a self-update) must replace an
    earlier "ok" — /health must not go on claiming clean forever just
    because the boot-time check once found it so."""
    commands = _answer_with(
        monkeypatch,
        _init_line(_ALL_SAMPLE_TOOLS),
        _init_line([*_ALL_SAMPLE_TOOLS, "FutureTool"]),
    )

    asyncio.run(verify_cli_tool_surface())
    assert provider.claude_cli_tool_surface_health() == "ok"

    claude_binary.write_bytes(b"a-different-build-entirely")
    probe = verify_cli_tool_surface()
    with pytest.raises(CliToolSurfaceError):
        asyncio.run(probe)

    assert provider.claude_cli_tool_surface_health() == "drift"
    assert len(commands) == 2


def test_claude_cli_tool_surface_health_becomes_unverified_when_a_cached_binary_disappears(
    claude_binary,
    monkeypatch,
) -> None:
    commands = _answer_with(monkeypatch, _init_line(_ALL_SAMPLE_TOOLS))

    asyncio.run(verify_cli_tool_surface())
    assert provider.claude_cli_tool_surface_health() == "ok"
    monkeypatch.setattr(
        provider,
        "_require_claude_binary",
        lambda: (_ for _ in ()).throw(UnavailableError("Claude CLI is unavailable")),
    )

    probe = verify_cli_tool_surface()
    with pytest.raises(UnavailableError, match="unavailable"):
        asyncio.run(probe)

    assert provider.claude_cli_tool_surface_health() == "unverified"
    assert len(commands) == 1


def test_claude_cli_tool_surface_health_becomes_unverified_when_a_cached_binary_is_unreadable(
    claude_binary,
    monkeypatch,
) -> None:
    commands = _answer_with(monkeypatch, _init_line(_ALL_SAMPLE_TOOLS))

    asyncio.run(verify_cli_tool_surface())
    assert provider.claude_cli_tool_surface_health() == "ok"
    monkeypatch.setattr(
        provider,
        "_binary_build",
        lambda _binary: (_ for _ in ()).throw(UnavailableError("Claude CLI is unreadable")),
    )

    probe = verify_cli_tool_surface()
    with pytest.raises(UnavailableError, match="unreadable"):
        asyncio.run(probe)

    assert provider.claude_cli_tool_surface_health() == "unverified"
    assert len(commands) == 1


# ── #351 round 6: unexpected tool/slash command always permanent ─────


def test_tool_surface_unexpected_tool_is_permanent_even_when_mcp_is_disconnected(
    claude_binary,
    monkeypatch,
) -> None:
    """#351 round 6, Finding 2: tools=["Bash"] plus a failed MCP connection
    used to fall through the disconnected-MCP short-failure path (the MCP
    check ran before the surface was ever evaluated) and get cached for
    only ten seconds — a nondeterministic build could then look clean once,
    get approved, and offer Bash again on the real turn. An unexpected
    tool is a permanent mismatch regardless of mcp_connected; only a
    *clean* absence explainable by the connection itself may be
    short-lived."""
    commands = _answer_with(monkeypatch, _init_line(["Bash"], mcp_connected=False))

    probe = verify_cli_tool_surface()
    with pytest.raises(CliToolSurfaceError) as exc:
        asyncio.run(probe)
    assert "Bash" in str(exc.value)

    # A second call, even immediately, must not re-probe — the mismatch is
    # cached forever, not merely for the ordinary ten-second failure TTL.
    probe = verify_cli_tool_surface()
    with pytest.raises(CliToolSurfaceError):
        asyncio.run(probe)
    assert len(commands) == 1


def test_tool_surface_slash_command_is_permanent_even_when_mcp_is_disconnected(
    claude_binary,
    monkeypatch,
) -> None:
    commands = _answer_with(
        monkeypatch,
        _init_line([], slash_commands=["/compact"], mcp_connected=False),
    )

    probe = verify_cli_tool_surface()
    with pytest.raises(CliToolSurfaceError) as exc:
        asyncio.run(probe)
    assert "/compact" in str(exc.value)

    probe = verify_cli_tool_surface()
    with pytest.raises(CliToolSurfaceError):
        asyncio.run(probe)
    assert len(commands) == 1


def test_tool_surface_permanent_mismatch_outlives_the_zombie_failure_ttl(
    claude_binary,
    monkeypatch,
) -> None:
    """A genuine verdict is not just "longer than ten seconds" — it never
    expires. Advancing the clock past even the zombie TTL (the longest one
    this module has) must not trigger a fresh probe."""
    commands = _answer_with(monkeypatch, _init_line([*_ALL_SAMPLE_TOOLS, "Bash"]))
    clock = {"now": time.monotonic()}
    monkeypatch.setattr(provider, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

    first_probe = verify_cli_tool_surface()
    with pytest.raises(CliToolSurfaceError):
        _run_with_clock(first_probe, clock)

    clock["now"] += provider.CLAUDE_CLI_ZOMBIE_FAILURE_CACHE_SECONDS + 1
    second_probe = verify_cli_tool_surface()
    with pytest.raises(CliToolSurfaceError):
        _run_with_clock(second_probe, clock)

    assert len(commands) == 1


def test_tool_surface_is_reprobed_after_a_genuine_symlink_retarget(
    tmp_path,
    monkeypatch,
) -> None:
    """#351 round 6, Finding 8: the 'CLI updates itself' test elsewhere
    overwrites the same file path — a real self-update repoints a symlink
    at a different target file instead. _binary_build resolves the
    symlink, so this must land on a different build key too."""
    target_a = tmp_path / "claude-2.1.257"
    target_a.write_bytes(b"cli-build-one")
    target_b = tmp_path / "claude-2.1.258"
    target_b.write_bytes(b"a-different-cli-build-entirely")
    symlink = tmp_path / "claude"
    symlink.symlink_to(target_a)

    with patch(
        "agent_providers.claude.provider._require_claude_binary",
        return_value=symlink,
    ):
        commands = _answer_with(
            monkeypatch,
            _init_line(_ALL_SAMPLE_TOOLS),
            _init_line([*_ALL_SAMPLE_TOOLS, "FutureTool"]),
        )

        asyncio.run(verify_cli_tool_surface())
        symlink.unlink()
        symlink.symlink_to(target_b)

        probe = verify_cli_tool_surface()
        with pytest.raises(CliToolSurfaceError) as exc:
            asyncio.run(probe)
        assert "FutureTool" in str(exc.value)

    assert len(commands) == 2


# ── #351 round 6, Finding 5: a zombie always wins ─────────────────────


def test_tool_surface_a_clean_read_followed_by_a_zombie_is_not_trusted(
    claude_binary,
    monkeypatch,
) -> None:
    """became_zombie used to count only when the answer phase had already
    failed — a clean init line read successfully, followed by the process
    outliving SIGKILL, produced an unbounded *clean* verdict and turn
    approval. A zombie always wins, regardless of what was read, and
    before parsing, the MCP check, or the verdict ever run."""
    _answer_with(monkeypatch, _init_line(_ALL_SAMPLE_TOOLS))
    monkeypatch.setattr(provider.process, "_reap_process_group", lambda _proc: True)

    probe = verify_cli_tool_surface()
    with pytest.raises(provider._ZombieProbeError):
        asyncio.run(probe)

    with provider._tool_surface_lock:
        assert not provider._tool_surface_verdicts


_A_LIVE_GROUP = KillUntilGoneResult(GroupCensus.LIVE, ended_in_time=True)
_AN_EMPTY_GROUP = KillUntilGoneResult(GroupCensus.EMPTY, ended_in_time=True)


@pytest.mark.parametrize(
    ("groups_counted", "expected_failure", "slots_and_home_at_the_return", "returns_the_binary"),
    (
        ((), nullcontext(), (0, False), True),
        (
            (_A_LIVE_GROUP, _AN_EMPTY_GROUP),
            pytest.raises(provider._ZombieProbeError, match="not confirmed gone after SIGKILL"),
            (1, True),
            False,
        ),
    ),
    ids=("confirmed", "confirmed-only-in-the-background"),
)
def test_a_tool_surface_probe_frees_its_slot_and_home_only_when_the_runner_confirms(
    claude_binary,
    monkeypatch,
    failure_watch,
    groups_counted,
    expected_failure,
    slots_and_home_at_the_return,
    returns_the_binary,
) -> None:
    set_claude_process_limit(monkeypatch, 2)
    hold = Hold()
    child = fake_cli_process(_init_line(_ALL_SAMPLE_TOOLS), gone=groups_counted, hold=hold)
    homes: list[Path] = []

    def door(started_child, *, stderr):
        homes.append(started_child.working_directory)
        return child

    install_cli_door(monkeypatch, door)

    verdict = None
    with expected_failure:
        verdict = asyncio.run(verify_cli_tool_surface())
    [home] = homes
    at_the_return = (held_claude_slots(), home.exists())
    hold.proceed.set()
    failure_watch.wait_for_the_runner_threads_to_end()

    assert at_the_return == slots_and_home_at_the_return
    assert (held_claude_slots(), home.exists()) == (0, False)
    if returns_the_binary:
        assert verdict == claude_binary


def test_tool_surface_a_clean_read_followed_by_a_zombie_gets_the_zombie_ttl(
    claude_binary,
    monkeypatch,
) -> None:
    commands = _answer_with(
        monkeypatch,
        _init_line(_ALL_SAMPLE_TOOLS),
        _init_line(_ALL_SAMPLE_TOOLS),
    )
    monkeypatch.setattr(provider.process, "_reap_process_group", lambda _proc: True)
    clock = {"now": time.monotonic()}
    monkeypatch.setattr(provider, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

    probe = verify_cli_tool_surface()
    with pytest.raises(provider._ZombieProbeError):
        _run_with_clock(probe, clock)

    # The ordinary (short) failure TTL alone must not be enough to retry —
    # this second call is a cache hit, which always re-raises as a plain
    # UnavailableError (the cache carries the message and the TTL choice
    # it already made, not the original exception's exact type).
    clock["now"] += provider.CLAUDE_CLI_TOOL_SURFACE_FAILURE_CACHE_SECONDS + 1
    cached_probe = verify_cli_tool_surface()
    with pytest.raises(
        UnavailableError, match="was not confirmed gone after SIGKILL",
    ):
        _run_with_clock(cached_probe, clock)
    assert len(commands) == 1

    # Past the zombie TTL a fresh probe runs — and this time the process
    # exits cleanly, so it should actually succeed.
    monkeypatch.setattr(provider.process, "_reap_process_group", lambda _proc: False)
    clock["now"] += provider.CLAUDE_CLI_ZOMBIE_FAILURE_CACHE_SECONDS + 1
    _run_with_clock(verify_cli_tool_surface(), clock)
    assert len(commands) == 2


def test_zombie_failure_uses_the_event_loop_clock_when_the_loop_is_offset(
    claude_binary,
    monkeypatch,
) -> None:
    commands = _answer_with(monkeypatch, _init_line(_ALL_SAMPLE_TOOLS))
    monkeypatch.setattr(provider.process, "_reap_process_group", lambda _proc: True)
    loop = asyncio.new_event_loop()
    monotonic = time.monotonic
    monkeypatch.setattr(loop, "time", lambda: monotonic() - 43_000)
    monkeypatch.setattr(provider, "time", SimpleNamespace(monotonic=loop.time))
    monkeypatch.setattr(provider.process.time, "monotonic", loop.time)

    try:
        probe = verify_cli_tool_surface()
        with pytest.raises(provider._ZombieProbeError):
            loop.run_until_complete(probe)
    finally:
        loop.close()

    assert len(commands) == 1


# ── #351 round 6, Finding 3: cleanup and cancellation ─────────────────


def test_tool_surface_probe_normalizes_a_broken_pipe_during_write(
    claude_binary,
    monkeypatch,
) -> None:
    """#351 round 7, Finding 5: a broken pipe during the write must be
    normalized *at the write itself* — caught inline, reaped, and
    reported with its own message — not merely "some UnavailableError,
    eventually" surfacing however long the outer deadline takes to
    exhaust. The previous version of this test only checked the latter,
    which stayed green even without the inline ``except OSError`` around
    the write (confirmed live: removing it, the write's BrokenPipeError
    escapes ``_run``'s try instead of being caught inline, the thread's
    own ``finally`` still puts a result — but with the *generic* "Popen()
    did not return" payload, not the real cause, and pytest only warns
    on the escaped exception rather than failing). A generous timeout
    budget makes "prompt, not deadline-bound" and "the real cause, not
    the generic fallback" both load-bearing assertions here.
    """
    reaped: list[int] = []

    def fake_reap(proc) -> bool:
        reaped.append(proc.pid)
        return False

    def fake_popen(*_cmd, **_kw):
        proc = fake_cli_process(None)
        proc.pid = 9090
        proc._stdin_reader.close()
        return proc

    install_cli_door(monkeypatch, fake_popen)
    monkeypatch.setattr(provider.process, "_reap_process_group", fake_reap)
    monkeypatch.setattr(provider, "CLAUDE_CLI_TOOL_SURFACE_TIMEOUT_SECONDS", 5.0)

    started = time.monotonic()
    probe = verify_cli_tool_surface()
    with pytest.raises(UnavailableError, match="Claude CLI probe failed to run") as exc:
        asyncio.run(probe)
    elapsed = time.monotonic() - started

    assert not isinstance(exc.value, provider._ZombieProbeError)
    assert "Broken pipe" not in str(exc.value)
    assert elapsed < 1.0, "normalized via the deadline instead of the write itself"
    assert reaped == [9090]


def test_tool_surface_probe_rejects_a_newline_free_stream_at_its_read_limit(
    claude_binary,
    monkeypatch,
) -> None:
    _build, key = provider._tool_surface_key(provider._NO_TOOLS_EXPECTED)

    def fake_popen(*_args, **_kwargs) -> MagicMock:
        proc = fake_cli_process(None)

        def write_stream() -> None:
            proc._stdout_writer.write(b"x" * (provider.CLI_OUTPUT_READ_LIMIT_BYTES + 1))
            proc._stdout_writer.close()

        threading.Thread(target=write_stream, daemon=True).start()
        return proc

    install_cli_door(monkeypatch, fake_popen)

    with pytest.raises(UnavailableError, match="exceeded its read limit"):
        verify_no_builtin_cli_tools()

    with provider._tool_surface_lock:
        assert key in provider._tool_surface_failures
        assert key not in provider._tool_surface_verdicts


def test_probe_runner_task_cancellation_gives_waiters_a_normal_error(
    claude_binary,
    monkeypatch,
) -> None:
    """#351 round 6, Finding 3, updated for round 7's redesign: the probe
    now runs as its own independent task (``_run_probe_and_resolve_async``)
    that no single caller's own cancellation can reach — so what *can*
    still be cancelled directly is that task itself (exactly what
    ``shutdown_tool_surface_background_tasks()`` does). Its own
    CancelledError must not become a waiting caller's literal
    CancelledError — that would make an unrelated caller's own task look
    cancelled too. Every waiter gets a normal, catchable UnavailableError.
    """
    probe_started = threading.Event()
    release_runner = threading.Event()

    def fake_popen(*_cmd, **_kw):
        probe_started.set()
        release_runner.wait(999)
        return fake_cli_process(_init_line(_ALL_SAMPLE_TOOLS))

    install_cli_door(monkeypatch, fake_popen)

    async def _race():
        waiter = asyncio.create_task(verify_cli_tool_surface())
        await asyncio.to_thread(probe_started.wait, 5)

        with provider._tool_surface_lock:
            probe_tasks = list(provider._tool_surface_probe_tasks)
        assert len(probe_tasks) == 1
        probe_tasks[0].cancel()

        with pytest.raises(UnavailableError) as exc:
            await waiter
        return exc.value

    try:
        error = asyncio.run(asyncio.wait_for(_race(), timeout=5))
    finally:
        release_runner.set()

    assert not isinstance(error, asyncio.CancelledError)


def test_tool_surface_a_cancelled_leader_does_not_reopen_single_flight(
    claude_binary,
    monkeypatch,
) -> None:
    """#351 round 7, Finding 2: the probe runs as a task independent of
    whoever triggered it (``_run_probe_and_resolve_async``), not inline in
    the triggering caller's own coroutine — cancelling that caller's own
    wait must not remove the in-flight entry while the real probe is
    still running in the background. A third caller landing in exactly
    that window must still find it (or, if the probe finished by then,
    the verdict it already cached) rather than starting a second probe.
    """
    calls = 0
    probe_started = threading.Event()
    release_probe = threading.Event()

    def fake_popen(*_cmd, **_kw):
        nonlocal calls
        calls += 1
        probe_started.set()
        release_probe.wait()
        return fake_cli_process(_init_line(_ALL_SAMPLE_TOOLS))

    install_cli_door(monkeypatch, fake_popen)

    async def _race() -> str:
        first = asyncio.create_task(verify_cli_tool_surface())
        await asyncio.to_thread(probe_started.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        # The real probe (its own independent task, on its own worker
        # thread) is still running here — first's cancellation only
        # interrupted first's own wait on the shared future.
        third = asyncio.create_task(verify_cli_tool_surface())
        release_probe.set()
        return await third

    result = asyncio.run(asyncio.wait_for(_race(), timeout=5))

    assert calls == 1
    assert result == claude_binary


def test_tool_surface_inflight_future_is_resolved_even_when_evaluation_itself_raises(
    claude_binary,
    monkeypatch,
) -> None:
    """#351 round 6, Finding 7: _evaluate_tool_surface used to run outside
    the try/except that resolves the in-flight future — a bug there would
    leave the future (and the dict entry) dangling, degrading every later
    caller for that key to a follower timeout instead of a fresh probe.

    Since round 7 (Finding 2), the probe runs as its own task and every
    caller — including whichever one triggered it — reaches the shared
    future through the same follower-safe path, so what a caller actually
    sees for a bug here is the translated UnavailableError
    (_follower_safe_exception), not the raw RuntimeError; the dict cleanup
    is what this test is really about.
    """
    _answer_with(monkeypatch, _init_line(_ALL_SAMPLE_TOOLS))
    monkeypatch.setattr(
        provider,
        "_evaluate_tool_surface",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    probe = verify_cli_tool_surface()
    with pytest.raises(UnavailableError, match="boom"):
        asyncio.run(probe)

    with provider._tool_surface_lock:
        assert not provider._tool_surface_inflight_async


# ── #351 round 6, Finding 4: the spawn step is off the event loop ────


def test_tool_surface_probe_stays_bounded_even_when_popen_itself_hangs(
    claude_binary,
    monkeypatch,
) -> None:
    """asyncio.create_subprocess_exec() still runs subprocess.Popen()
    synchronously on whichever thread calls it, including the event loop's
    own — so a spawn that hangs there could keep the loop from ever
    running the timer meant to enforce the deadline. The async gate now
    delegates to the sync twin via asyncio.to_thread specifically so a
    hung Popen() only blocks its own worker thread, never the caller."""
    popen_may_return = threading.Event()
    popen_returned = threading.Event()

    def fake_popen(*_cmd, **_kw):
        popen_may_return.wait()
        proc = fake_cli_process(_init_line([]))
        popen_returned.set()
        return proc

    install_cli_door(monkeypatch, fake_popen)
    monkeypatch.setattr(provider, "CLAUDE_CLI_NO_TOOL_SURFACE_TIMEOUT_SECONDS", 0.05)

    try:
        probe = averify_no_builtin_cli_tools()
        bounded_probe = asyncio.wait_for(probe, timeout=3)
        with pytest.raises(UnavailableError) as exc:
            asyncio.run(bounded_probe)
        assert str(exc.value) == "Claude CLI probe did not start within its budget"
    finally:
        popen_may_return.set()
        assert popen_returned.wait(timeout=1)


def test_tool_surface_probe_deadline_includes_the_default_executor_queue(
    claude_binary,
    monkeypatch,
) -> None:
    monkeypatch.setattr(provider, "_cleanup_margin_seconds", lambda: 0)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        work_started = loop.create_future()
        release_work = threading.Event()

        def occupy_executor() -> None:
            loop.call_soon_threadsafe(work_started.set_result, None)
            release_work.wait()

        loop.set_default_executor(executor)
        occupied_work = loop.run_in_executor(None, occupy_executor)
        await work_started
        deadline = loop.time() + 0.05
        try:
            binary = claude_binary
            probe = provider._probe_cli_surface_async(
                binary,
                mcp=None,
                deadline=deadline,
            )
            bounded_probe = asyncio.wait_for(probe, timeout=1)
            with pytest.raises(UnavailableError) as exc:
                await bounded_probe
            assert str(exc.value) == "Claude CLI probe cleanup did not finish within its budget"
        finally:
            release_work.set()
            await occupied_work

    asyncio.run(_run())


def test_delayed_probe_start_is_a_probe_failure_not_a_one_shot_timeout(
    claude_binary,
    monkeypatch,
) -> None:
    spawned: list[object] = []

    monkeypatch.setattr(provider.time, "monotonic", lambda: 100.0)
    install_cli_door(
        monkeypatch,
        lambda *_args, **_kwargs: spawned.append(1),
    )

    binary = claude_binary
    with pytest.raises(UnavailableError) as exc:
        provider._probe_cli_surface_sync(binary, mcp=None, deadline=100.0)

    assert not isinstance(exc.value, provider._DeadlineExhausted)
    assert str(exc.value) == "Claude CLI probe preflight budget was already exhausted"
    assert spawned == []


def test_async_probe_waits_for_cleanup_after_its_answer_budget_is_exhausted(
    monkeypatch,
) -> None:
    worker_started = asyncio.Event()
    cleanup_may_finish = asyncio.Event()

    async def delayed_worker(*_args, **_kwargs) -> object:
        worker_started.set()
        await cleanup_may_finish.wait()
        raise provider._ZombieProbeError("probe process outlived SIGKILL")

    monkeypatch.setattr(provider.asyncio, "to_thread", delayed_worker)

    async def receive_zombie_after_cleanup() -> None:
        loop = asyncio.get_running_loop()
        probe = asyncio.create_task(
            provider._probe_cli_surface_async(
                "claude",
                mcp=None,
                deadline=loop.time(),
            ),
        )
        await asyncio.wait_for(worker_started.wait(), timeout=1)
        assert not probe.done()
        cleanup_may_finish.set()
        with pytest.raises(provider._ZombieProbeError):
            await probe

    asyncio.run(receive_zombie_after_cleanup())


@pytest.mark.parametrize("stdin_blocked", [False, True], ids=["read", "write"])
def test_probe_with_a_stalled_pipe_reaps_and_releases_its_admission(
    claude_binary,
    monkeypatch,
    incrementing_monotonic_clock,
    stdin_blocked: bool,
) -> None:
    spawned = threading.Event()
    pipe_stalled = threading.Event()
    expire_probe = threading.Event()
    reaped = threading.Event()
    processes: list[MagicMock] = []
    failures: list[BaseException] = []

    class StalledPipeSelector:
        """Drive the probe from pipe events instead of scheduler time."""

        def __init__(self) -> None:
            self._registrations: dict[object, SimpleNamespace] = {}

        def __enter__(self) -> StalledPipeSelector:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def register(self, fileobj: object, _events: int, data: str) -> None:
            self._registrations[fileobj] = SimpleNamespace(fileobj=fileobj, data=data)

        def unregister(self, fileobj: object) -> None:
            self._registrations.pop(fileobj)

        def get_map(self) -> dict[object, SimpleNamespace]:
            return self._registrations

        def select(self, timeout: float | None = None) -> list[tuple[SimpleNamespace, int]]:
            del timeout
            stdin = next(
                (item for item in self._registrations.values() if item.data == "stdin"),
                None,
            )
            if stdin is not None and not stdin_blocked:
                return [(stdin, 0)]
            pipe_stalled.set()
            expire_probe.wait()
            return []

    def fake_popen(*_cmd, **_kw) -> MagicMock:
        proc = fake_cli_process(None, stdin_blocked=stdin_blocked)
        processes.append(proc)
        spawned.set()
        return proc

    def fake_reap(_proc: object) -> bool:
        reaped.set()
        return False

    install_cli_door(monkeypatch, fake_popen)
    monkeypatch.setattr(provider.process, "_reap_process_group", fake_reap)
    monkeypatch.setattr(provider.process.selectors, "DefaultSelector", StalledPipeSelector)
    monkeypatch.setattr(provider, "CLAUDE_CLI_NO_TOOL_SURFACE_TIMEOUT_SECONDS", 5.0)
    set_claude_process_limit(monkeypatch, 1)
    incrementing_monotonic_clock.step = 0

    def probe() -> None:
        try:
            verify_no_builtin_cli_tools()
        except BaseException as error:
            failures.append(error)

    probe_thread = threading.Thread(target=probe)
    try:
        probe_thread.start()
        assert pipe_stalled.wait(timeout=1), failures
        assert spawned.is_set()
        incrementing_monotonic_clock.now += provider.CLAUDE_CLI_NO_TOOL_SURFACE_TIMEOUT_SECONDS
        expire_probe.set()
        probe_thread.join()

        assert len(failures) == 1
        assert isinstance(failures[0], UnavailableError)
        assert "did not answer" in str(failures[0])
        assert reaped.is_set()
        reservation = provider.CLAUDE_CLI_PROCESS_POOL.reserve()
        assert reservation is not None
        provider.CLAUDE_CLI_PROCESS_POOL.release(reservation)
    finally:
        expire_probe.set()
        probe_thread.join(timeout=1)
        assert not probe_thread.is_alive()
        for proc in processes:
            proc.stdout.close()
            proc._stdin_reader.close()
            proc._stdout_writer.close()


def test_async_probe_returns_a_zombie_after_cleanup_crosses_its_answer_deadline(
    claude_binary,
    monkeypatch,
) -> None:
    process_spawned = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    processes: list[MagicMock] = []

    def fake_popen(*_cmd, **_kw) -> MagicMock:
        proc = fake_cli_process(None)
        processes.append(proc)
        process_spawned.set()
        return proc

    def fake_reap(_proc: object) -> bool:
        cleanup_started.set()
        assert release_cleanup.wait(timeout=1)
        return True

    install_cli_door(monkeypatch, fake_popen)
    monkeypatch.setattr(provider.process, "_reap_process_group", fake_reap)

    async def verify_after_cleanup() -> None:
        loop = asyncio.get_running_loop()
        probe = asyncio.create_task(
            provider._probe_cli_surface_async(
                claude_binary,
                mcp=None,
                deadline=loop.time() + 0.02,
            )
        )
        assert await asyncio.to_thread(process_spawned.wait, 1)
        assert await asyncio.to_thread(cleanup_started.wait, 1)
        assert not probe.done()
        release_cleanup.set()
        with pytest.raises(provider._ZombieProbeError):
            await probe

    try:
        asyncio.run(verify_after_cleanup())
    finally:
        release_cleanup.set()
        for proc in processes:
            if not proc.stdout.closed:
                proc.stdout.close()
            if not proc._stdin_reader.closed:
                proc._stdin_reader.close()
            if not proc._stdout_writer.closed:
                proc._stdout_writer.close()


def test_probe_reaps_a_process_whose_popen_call_returns_after_the_deadline(
    claude_binary,
    monkeypatch,
) -> None:
    """#351 round 7, Finding 1: reaping used to happen only in the calling
    function, after it successfully read the background thread's result
    — so a Popen() that returned *after* the caller had already given up
    was never reaped by anyone. Reaping now happens inside the thread's
    own finally, so it still runs even though nothing is listening for
    the answer by the time Popen() finally does return.
    """
    popen_may_return = threading.Event()
    reaped = threading.Event()
    reaped_pids: list[int] = []
    probe_threads: list[threading.Thread] = []
    real_thread = provider.process.ClaimedStartThread
    _build, key = provider._tool_surface_key(provider._NO_TOOLS_EXPECTED)

    def fake_popen(*_cmd, **_kw):
        popen_may_return.wait(5)
        return fake_cli_process(_init_line([]))

    def fake_reap(proc) -> bool:
        reaped_pids.append(proc.pid)
        reaped.set()
        return False

    def tracked_thread(*args, **kwargs) -> threading.Thread:
        thread = real_thread(*args, **kwargs)
        probe_threads.append(thread)
        return thread

    install_cli_door(monkeypatch, fake_popen)
    monkeypatch.setattr(provider.process, "_reap_process_group", fake_reap)
    monkeypatch.setattr(provider, "CLAUDE_CLI_NO_TOOL_SURFACE_TIMEOUT_SECONDS", 0.02)
    set_claude_process_limit(monkeypatch, 1)
    monkeypatch.setattr(provider.process, "ClaimedStartThread", tracked_thread)

    started = time.monotonic()
    with pytest.raises(UnavailableError, match="did not start"):
        verify_no_builtin_cli_tools()  # gives up long before Popen() returns
    assert time.monotonic() - started < 1
    with provider._tool_surface_lock:
        assert key in provider._tool_surface_failures

    assert provider.CLAUDE_CLI_PROCESS_POOL.reserve() is None
    popen_may_return.set()  # only now does the "late" spawn actually complete

    assert reaped.wait(5), "the late-returning process was never reaped"
    probe_threads[0].join(timeout=1)
    assert not probe_threads[0].is_alive()
    assert reaped_pids == [4343]


def test_tool_surface_preflight_uses_its_configured_bound(
    claude_binary,
    monkeypatch,
    incrementing_monotonic_clock,
) -> None:
    observed_deadlines: list[float] = []

    def probe(*_args: object, deadline: float, **_kwargs: object) -> object:
        observed_deadlines.append(deadline)
        raise UnavailableError("probe stopped")

    monkeypatch.setattr(provider, "_probe_cli_surface_sync", probe)

    with pytest.raises(UnavailableError, match="probe stopped"):
        verify_no_builtin_cli_tools()

    assert observed_deadlines == [105.0]


def test_one_shot_deadline_keeps_and_caches_a_zombie_probe_failure(
    claude_binary,
    incrementing_monotonic_clock,
) -> None:
    _build, key = provider._tool_surface_key(provider._NO_TOOLS_EXPECTED)

    def zombie_probe(_deadline: float) -> provider._AnnouncedSurface:
        raise provider._ZombieProbeError("probe process outlived SIGKILL")

    with pytest.raises(provider._ZombieProbeError, match="outlived SIGKILL"):
        provider._verify_tool_surface_sync(_build, key, zombie_probe)

    with provider._tool_surface_lock:
        failure = provider._tool_surface_failures[key]
    assert failure.ttl_seconds() == provider.CLAUDE_CLI_ZOMBIE_FAILURE_CACHE_SECONDS


# ── the pool caps new probes too ─────────────────────────────────────


@pytest.mark.parametrize("probe_kind", ("async", "sync"))
def test_a_tool_surface_probe_is_refused_while_retained_groups_fill_the_pool(
    probe_kind: str,
    claude_binary,
    killpg_signals,
    monkeypatch,
) -> None:
    set_claude_process_limit(monkeypatch, 2)
    reap_without_waiting(monkeypatch)
    monkeypatch.setattr(provider, "_write_mcp_config", lambda _spec, _user_id: "unused")
    monkeypatch.setattr(provider, "_unlink_quiet", lambda _path: None)
    spawned: list[object] = []
    install_cli_door(
        monkeypatch,
        lambda *_a, **_kw: spawned.append(1) or MagicMock(),
    )
    children = [
        a_child_that_outlives_sigkill(pid=pid, communicate=asyncio.TimeoutError())
        for pid in (1, 2)
    ]
    install_claude_cli_children(monkeypatch, *children)

    async def refused_probe() -> Path:
        if probe_kind == "async":
            return await averify_no_builtin_cli_tools()
        return verify_no_builtin_cli_tools()

    async def _run() -> None:
        retained: list[RetainedGroup] = []
        try:
            for _ in children:
                with pytest.raises(ContainmentLost) as lost:
                    await provider.acall_claude_with_mcp(prompt="hi", user_id="u-1")
                retained.append(lost.value.group)
            with pytest.raises(UnavailableError) as exc:
                await refused_probe()
            assert str(exc.value) == (
                "Claude CLI process pool is at its concurrency limit (2); refusing to start another"
            )
            assert spawned == []
        finally:
            for child, group in zip(children, retained, strict=False):
                assert await confirm_the_retained_group_gone(child, group)
        assert killpg_signals == []
        commands = _answer_with(monkeypatch, _init_line([]))
        assert await refused_probe() == claude_binary
        assert len(commands) == 1

    with recorded_census_workers(monkeypatch):
        run_bounded(_run())
    assert held_claude_slots() == 0


def test_zombie_reap_reservation_is_reserved_atomically_not_just_counted(monkeypatch) -> None:
    """#351 round 7, Finding 3: the old check-then-claim-later version let
    two concurrent probes both see room at cap-minus-one and both
    proceed. A real reservation closes that: the second concurrent
    attempt at the cap must be refused right at the reservation, not
    discover the gap only later when it happens to become a zombie."""
    set_claude_process_limit(monkeypatch, 1)

    reservation = _reserve_zombie_process()
    assert provider.CLAUDE_CLI_PROCESS_POOL.reserve() is None
    provider.CLAUDE_CLI_PROCESS_POOL.release(reservation)
    next_reservation = provider.CLAUDE_CLI_PROCESS_POOL.reserve()
    assert next_reservation is not None
    provider.CLAUDE_CLI_PROCESS_POOL.release(next_reservation)


def test_clearing_the_tool_surface_cache_keeps_live_processes_in_the_pool(
    monkeypatch,
) -> None:
    set_claude_process_limit(monkeypatch, 1)
    reservation = _reserve_zombie_process()

    clear_cli_tool_surface_cache()

    with provider.CLAUDE_CLI_PROCESS_POOL._lock:
        assert provider.CLAUDE_CLI_PROCESS_POOL._reservations == {reservation}
    assert provider.CLAUDE_CLI_PROCESS_POOL.reserve() is None
    provider.CLAUDE_CLI_PROCESS_POOL.release(reservation)
    with provider.CLAUDE_CLI_PROCESS_POOL._lock:
        assert not provider.CLAUDE_CLI_PROCESS_POOL._reservations


def test_zombie_reap_reservation_has_no_toctou_window_under_real_concurrency(
    monkeypatch,
) -> None:
    """The same property, proven with two genuinely concurrent threads
    racing the same 1-slot pool rather than two sequential calls."""
    set_claude_process_limit(monkeypatch, 1)
    both_may_proceed = threading.Barrier(2)
    results: list[object | None] = []
    results_lock = threading.Lock()

    def _attempt() -> None:
        both_may_proceed.wait(timeout=5)
        reserved = provider.CLAUDE_CLI_PROCESS_POOL.reserve()
        with results_lock:
            results.append(reserved)

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    try:
        assert sum(reservation is not None for reservation in results) == 1
    finally:
        for reservation in results:
            if reservation is not None:
                provider.CLAUDE_CLI_PROCESS_POOL.release(reservation)


def _record_the_homes_the_provider_opens(
    monkeypatch,
) -> list[process.PrivateHome]:
    homes: list[process.PrivateHome] = []
    real_private_home = provider._claude_private_home

    def record_home() -> process.PrivateHome:
        homes.append(real_private_home())
        return homes[-1]

    monkeypatch.setattr(provider, "_claude_private_home", record_home)
    return homes


def test_a_tool_surface_probe_refused_by_a_full_pool_leaves_no_home_behind(monkeypatch) -> None:
    set_claude_process_limit(monkeypatch, 1)
    homes = _record_the_homes_the_provider_opens(monkeypatch)
    held = provider.CLAUDE_CLI_PROCESS_POOL.reserve()
    assert held is not None

    deadline = time.monotonic() + 1
    try:
        with pytest.raises(provider._ClaudeCliProcessPoolSaturated):
            provider._probe_cli_surface_sync(Path("/usr/bin/claude"), mcp=None, deadline=deadline)
        [home] = homes
        assert (held_claude_slots(), home.path.exists()) == (1, False)
    finally:
        provider.CLAUDE_CLI_PROCESS_POOL.release(held)


def test_probe_runner_start_failure_is_a_closed_probe_failure_that_frees_its_reservation(
    monkeypatch, caplog, capfd,
) -> None:
    watch = RunnerFailureWatch(monkeypatch, caplog, capfd)
    set_claude_process_limit(monkeypatch, 1)
    homes = _record_the_homes_the_provider_opens(monkeypatch)
    thread_start_error = RuntimeError(f"thread start failed {A_RUNNER_FAILURE_CANARY}")
    fail_to_start_the_runner_thread(monkeypatch, thread_start_error)

    deadline = time.monotonic() + 1
    with pytest.raises(UnavailableError) as raised:
        provider._probe_cli_surface_sync(Path("/usr/bin/claude"), mcp=None, deadline=deadline)

    assert str(raised.value) == "Claude CLI probe failed to run"
    assert watch.reported_steps() == ["start RuntimeError"]
    [home] = homes
    assert (held_claude_slots(), home.path.exists()) == (0, False)
    watch.assert_nothing_leaked(raised.value)
    assert "thread start failed" not in caplog.text


def _a_probe_runner_that_reports(
    outcome: CliRunOutcome, monkeypatch,
) -> list[process.PrivateHome]:
    homes = _record_the_homes_the_provider_opens(monkeypatch)

    def run_cli_bounded(_child, **kwargs):
        kwargs["on_spawn_failed"]()
        return outcome

    monkeypatch.setattr(provider.process, "run_cli_bounded", run_cli_bounded)
    return homes


@pytest.mark.parametrize(
    "failed_outcome",
    [
        CliRunOutcome(
            started=False,
            spawn_error=OSError(A_RUNNER_FAILURE_CANARY),
            returncode=None,
            stdout="",
            stderr="",
            complete=False,
            became_zombie=False,
            reason=CliRunReason.SPAWN_FAILED,
        ),
        CliRunOutcome(
            started=True,
            spawn_error=None,
            returncode=None,
            stdout="",
            stderr="",
            complete=False,
            became_zombie=False,
            reason=CliRunReason.IO_ERROR,
            io_error=OSError(A_RUNNER_FAILURE_CANARY),
        ),
    ],
    ids=["spawn-failed", "io-error"],
)
def test_a_probe_failure_message_carries_no_runner_exception_text(
    monkeypatch, caplog, capfd, failed_outcome,
) -> None:
    watch = RunnerFailureWatch(monkeypatch, caplog, capfd)
    set_claude_process_limit(monkeypatch, 1)
    homes = _a_probe_runner_that_reports(failed_outcome, monkeypatch)

    deadline = time.monotonic() + 1
    with pytest.raises(UnavailableError) as raised:
        provider._probe_cli_surface_sync(Path("/usr/bin/claude"), mcp=None, deadline=deadline)

    assert str(raised.value) == "Claude CLI probe failed to run"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    reservation = provider.CLAUDE_CLI_PROCESS_POOL.reserve()
    assert reservation is not None
    provider.CLAUDE_CLI_PROCESS_POOL.release(reservation)
    assert not homes[0].path.exists()
    watch.assert_nothing_leaked(raised.value)


def test_a_probe_whose_cleanup_overruns_fails_at_the_settle_instant_not_the_async_budget(
    claude_binary, monkeypatch, caplog, capfd,
) -> None:
    watch = RunnerFailureWatch(monkeypatch, caplog, capfd)
    set_claude_process_limit(monkeypatch, 1)
    monkeypatch.setattr(process, "CLI_TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(process, "CLI_GROUP_CONFIRMATION_SECONDS", 0.2)
    monkeypatch.setattr(process, "CLI_PROBE_CALLER_TIMEOUT_MARGIN_SECONDS", 0.01)
    release_cleanup = threading.Event()
    slot_released = threading.Event()
    real_release = provider.CLAUDE_CLI_PROCESS_POOL.release

    def release_and_signal(reservation) -> None:
        real_release(reservation)
        slot_released.set()

    monkeypatch.setattr(provider.CLAUDE_CLI_PROCESS_POOL, "release", release_and_signal)
    install_cli_door(monkeypatch, lambda *_cmd, **_kw: fake_cli_process(None))
    monkeypatch.setattr(
        provider.process,
        "_reap_process_group",
        lambda _proc: release_cleanup.wait(timeout=2) and False,
    )
    deadline = time.monotonic() + 0.05

    try:
        with pytest.raises(UnavailableError) as raised:
            provider._probe_cli_surface_sync(claude_binary, mcp=None, deadline=deadline)
        failed_at = time.monotonic()

        assert str(raised.value) == "Claude CLI probe cleanup did not finish within its budget"
        assert process.cli_settle_deadline(deadline) <= failed_at
        assert failed_at < process.cli_settle_deadline(deadline) + 0.5
        assert provider.CLAUDE_CLI_PROCESS_POOL.reserve() is None
    finally:
        release_cleanup.set()
    assert slot_released.wait(timeout=2)
    reservation = provider.CLAUDE_CLI_PROCESS_POOL.reserve()
    assert reservation is not None
    provider.CLAUDE_CLI_PROCESS_POOL.release(reservation)
    watch.assert_nothing_leaked()


# ── #351 round 6, Finding 8: the non-streaming MCP entry point ───────


def test_mcp_turn_non_stream_turn_refuses_a_cli_with_an_unverified_tool_surface(
    monkeypatch,
) -> None:
    """The streaming entry point already had this proof; the
    non-streaming acall_claude_with_mcp needs its own — the global
    autouse mock of verify_cli_tool_surface would otherwise hide a
    regression that dropped this call from this specific entry point."""
    monkeypatch.setattr(
        provider,
        "verify_cli_tool_surface",
        AsyncMock(side_effect=CliToolSurfaceError("FutureTool")),
    )
    spawned = install_claude_cli_children(monkeypatch).children

    turn = provider.acall_claude_with_mcp(prompt="hi", user_id="u-1")
    with pytest.raises(CliToolSurfaceError):
        asyncio.run(turn)
    assert spawned == []


def test_mcp_turn_refuses_a_cli_with_an_unverified_tool_surface(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        provider,
        "verify_cli_tool_surface",
        AsyncMock(side_effect=CliToolSurfaceError("FutureTool")),
    )
    spawned = install_claude_cli_children(monkeypatch).children

    async def _turn() -> None:
        async for _ in acall_claude_with_mcp_stream(prompt="hi", user_id="u-1"):
            pass

    turn = _turn()
    with pytest.raises(CliToolSurfaceError):
        asyncio.run(turn)
    assert spawned == []


async def _collect_stream_events(stream) -> list[object]:
    return [event async for event in stream]


def test_public_claude_stream_skips_malformed_cli_output(monkeypatch, caplog) -> None:
    install_claude_cli_children(
        monkeypatch,
        a_claude_cli_child(
            stream_lines=[
                b"not-json\n",
                b'{"type":"assistant","message":{"content":[{"type":"text","text":"draft"}]}}\n',
                b'{"type":"result","result":"final"}\n',
            ],
        ),
    )
    monkeypatch.setattr(
        provider, "verify_cli_tool_surface", AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    monkeypatch.setattr(provider, "_write_mcp_config", lambda _spec, _user_id: "unused")
    monkeypatch.setattr(provider, "_unlink_quiet", lambda _path: None)
    caplog.set_level("WARNING", logger="agent_providers.claude.provider")

    events = asyncio.run(
        _collect_stream_events(
            acall_claude_with_mcp_stream(prompt="hi", user_id="u-1"),
        )
    )

    assert events == [
        AssistantTextEvent(text="draft"),
        FinalEvent(text="final"),
    ]
    assert "malformed JSON" in caplog.text
    assert held_claude_slots() == 0


def test_public_claude_stream_names_a_nonzero_cli_exit(monkeypatch) -> None:
    install_claude_cli_children(monkeypatch, a_claude_cli_child(returncode=2))
    monkeypatch.setattr(
        provider, "verify_cli_tool_surface", AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    monkeypatch.setattr(provider, "_write_mcp_config", lambda _spec, _user_id: "unused")
    monkeypatch.setattr(provider, "_unlink_quiet", lambda _path: None)

    stream = acall_claude_with_mcp_stream(prompt="hi", user_id="u-1")
    events = _collect_stream_events(stream)
    with pytest.raises(UnavailableError, match="unavailable"):
        asyncio.run(events)
    assert held_claude_slots() == 0


def test_public_claude_stream_names_a_missing_binary(monkeypatch) -> None:
    _install_a_start_that_raises(monkeypatch, FileNotFoundError())
    monkeypatch.setattr(
        provider, "verify_cli_tool_surface", AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    monkeypatch.setattr(provider, "_write_mcp_config", lambda _spec, _user_id: "unused")
    monkeypatch.setattr(provider, "_unlink_quiet", lambda _path: None)

    stream = acall_claude_with_mcp_stream(prompt="hi", user_id="u-1")
    events = _collect_stream_events(stream)
    with pytest.raises(CliUnavailableError, match="not found"):
        asyncio.run(events)
    assert held_claude_slots() == 0


def test_stream_reap_completes_before_a_twice_cancelled_closer_returns(monkeypatch) -> None:
    """A disconnect may cancel an ASGI 2.3 stream while it is closing.

    The reaper itself has awaits for SIGTERM/SIGKILL grace and process wait,
    so cancelling the closer must not cancel that work halfway through.
    """
    deadline = ScenarioDeadline()
    reap_gate = EventGate(deadline)
    real_reap = anchored_turn._reap_process_group
    monkeypatch.setattr(
        provider, "verify_cli_tool_surface", AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    monkeypatch.setattr(provider, "_write_mcp_config", lambda _spec, _user_id: "unused")
    monkeypatch.setattr(provider, "_unlink_quiet", lambda _path: None)
    spawns = install_claude_cli_children(monkeypatch, a_claude_cli_child())

    async def _run() -> None:
        reaper_finished = asyncio.Event()

        async def fake_consume(*_args, **_kwargs):
            yield AssistantTextEvent(text="partial")
            await asyncio.Future()

        async def fake_reap(child, reservation) -> None:
            await _reap_after_holds(real_reap, child, reservation, reap_gate)
            reaper_finished.set()

        monkeypatch.setattr(provider, "_consume_stream", fake_consume)
        monkeypatch.setattr(anchored_turn, "_reap_process_group", fake_reap)

        tasks: list[asyncio.Future[Any]] = []
        async with scenario_holds([reap_gate], tasks, set()):
            stream = provider.acall_claude_with_mcp_stream(prompt="hi", user_id="u-1")
            assert (await anext(stream)).text == "partial"

            closer = asyncio.create_task(stream.aclose())
            tasks.append(closer)
            await reap_gate.started.wait()
            for _ in range(2):
                closer.cancel()
                await _after_pending_callbacks()
                assert not closer.done()

            reap_gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await closer
            assert reaper_finished.is_set()
            assert not reap_gate.missed.is_set()
            assert held_claude_slots() == 0
            assert not spawns.children[0].working_directory.exists()

    run_bounded(_run())


def test_closing_a_claude_stream_keeps_its_private_home_until_confirmed_reap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    killpg_signals,
) -> None:
    deadline = ScenarioDeadline()
    reap_gate = EventGate(deadline)
    real_reap = anchored_turn._reap_process_group
    child = a_claude_cli_child(
        returncode=None,
        stream_lines=[
            b'{"type":"assistant","message":{"content":[{"type":"text","text":"partial"}]}}\n',
        ],
    )

    async def run() -> None:
        home_seen: Path | None = None

        def child_started_in_a_private_home(spawned, _stream_buffer_limit):
            nonlocal home_seen
            home_seen = assert_claude_token_child(
                spawned, root=tmp_path, token=SAMPLE_CLAUDE_SETUP_TOKEN,
            )
            return child

        async def reap_process_group(reaped_child, reservation) -> None:
            await _reap_after_holds(real_reap, reaped_child, reservation, reap_gate)

        install_claude_cli_children(monkeypatch, factory=child_started_in_a_private_home)
        monkeypatch.setattr(anchored_turn, "_reap_process_group", reap_process_group)
        monkeypatch.setattr(
            provider,
            "verify_cli_tool_surface",
            AsyncMock(return_value=Path("/usr/bin/claude")),
        )

        tasks: list[asyncio.Future[Any]] = []
        async with scenario_holds([reap_gate], tasks, set()):
            stream = acall_claude_with_mcp_stream("hello", user_id="user-1")
            assert await anext(stream) == AssistantTextEvent(text="partial")
            closer = asyncio.create_task(stream.aclose())
            tasks.append(closer)
            await reap_gate.started.wait()
            assert home_seen is not None
            assert home_seen.exists()

            reap_gate.release.set()
            await closer
            assert not home_seen.exists()

    run_bounded(run())
    assert operations(child.log) == [
        ("SIGTERM", child.pid), ("kill_until_gone", child.pid), ("release", child.pid),
    ]
    assert killpg_signals == []


REAPER_CHECKPOINTS = 5
MOST_SHIELD_CALLS_OF_ONE_REAP = 2


@pytest.mark.parametrize(
    "also_cancelled_directly", [False, True], ids=["scope_only", "scope_and_task_cancel"],
)
def test_stream_reap_does_not_spin_under_anyio_level_cancellation(
    monkeypatch, also_cancelled_directly,
) -> None:
    """An ASGI cancellation scope must let the delayed reap make progress.

    The old asyncio.shield retry loop was immediately cancelled again by
    AnyIO's level cancellation, repeatedly attempting the shielded await
    while the reaper could not finish.  A cancellation shield waits once for
    the child reaper, then propagates cancellation after it has completed.
    """
    deadline = ScenarioDeadline()
    shield_calls = 0
    original_shield = asyncio.shield

    def count_shield(awaitable):
        nonlocal shield_calls
        shield_calls += 1
        return original_shield(awaitable)

    monkeypatch.setattr(asyncio, "shield", count_shield)

    async def _run() -> None:
        reaper_started = anyio.Event()
        finish_reaper = anyio.Event()
        reaper_finished = anyio.Event()
        close_finished = anyio.Event()
        reap_calls = 0
        closing: list[asyncio.Task] = []

        async def fake_reap(_child, _reservation) -> None:
            nonlocal reap_calls
            reap_calls += 1
            reaper_started.set()
            await finish_reaper.wait()
            for _ in range(REAPER_CHECKPOINTS):
                await anyio.lowlevel.checkpoint()
            reaper_finished.set()

        monkeypatch.setattr(anchored_turn, "_reap_process_group", fake_reap)

        async def close_after_cancellation() -> None:
            process = a_claude_cli_child(returncode=None)
            reservation = anchored_turn.CliProcessReservation(pool=provider.CLAUDE_CLI_PROCESS_POOL)
            closing.append(asyncio.current_task())
            with pytest.raises(asyncio.CancelledError):
                await anchored_turn._reap_behind_shield(process, reservation)
            close_finished.set()

        with anyio.fail_after(deadline.remaining()):
            async with anyio.create_task_group() as task_group:
                try:
                    task_group.start_soon(close_after_cancellation)
                    await reaper_started.wait()
                    task_group.cancel_scope.cancel()
                    if also_cancelled_directly:
                        closing[0].cancel()
                finally:
                    finish_reaper.set()
                with anyio.CancelScope(shield=True), anyio.fail_after(deadline.remaining()):
                    await reaper_finished.wait()
                    await close_finished.wait()

        assert reap_calls == 1
        assert shield_calls <= MOST_SHIELD_CALLS_OF_ONE_REAP

    anyio.run(_run)


# ── every exit of a turn goes through one shielded reap ──────────────

A_PROCESS_PID = 2468


class ReapFault(Exception):
    """The reap's own failure, distinct from every turn error."""


async def _after_pending_callbacks() -> None:
    callbacks_ran = asyncio.Event()
    asyncio.get_running_loop().call_soon(callbacks_ran.set)
    await callbacks_ran.wait()


async def _reap_after_holds(
    real_reap: Callable[[object, object], Coroutine[Any, Any, None]],
    child: object,
    reservation: object,
    *holds: EventGate,
) -> None:
    """Hold at each gate, then run the real reap, so a missed hold still frees slot and home."""
    try:
        for hold in holds:
            await hold.pass_through()
    finally:
        await real_reap(child, reservation)


def _install_a_start_that_raises(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> ClaudeCliSpawns:
    def fail_to_start(_child, _stream_buffer_limit):
        raise error

    return install_claude_cli_children(monkeypatch, factory=fail_to_start)


def _install_a_start_that_ends(monkeypatch: pytest.MonkeyPatch, failure: str) -> ClaudeCliSpawns:
    if failure == "missing":
        return _install_a_start_that_raises(monkeypatch, FileNotFoundError())
    return install_claude_cli_children(
        monkeypatch,
        a_claude_cli_child(pid=1, returncode=1, communicate=(b"", b"failed")),
    )


@dataclass
class _ChildInAHome:
    child: ScriptedClaudeChild
    homes: list[Path]

    @property
    def home(self) -> Path:
        return self.homes[0]


def _install_child_in_a_home(
    monkeypatch: pytest.MonkeyPatch,
    build_child: Callable[..., ScriptedClaudeChild] = a_claude_cli_child,
    on_spawn: Callable[[], None] = lambda: None,
    **child_knobs: object,
) -> _ChildInAHome:
    child = build_child(**child_knobs)
    homes: list[Path] = []

    def start(spawned, _stream_buffer_limit) -> ScriptedClaudeChild:
        on_spawn()
        homes.append(Path(spawned.environment["HOME"]))
        return child

    install_claude_cli_children(monkeypatch, factory=start)
    return _ChildInAHome(child, homes)


async def _call_mcp_one_shot() -> ClaudeResponse:
    return await provider.acall_claude_with_mcp("hi", user_id="u-1")


async def _call_cli_one_shot() -> ClaudeResponse:
    return await _acall_cli("hi")


@pytest.fixture(
    params=[_call_mcp_one_shot, _call_cli_one_shot], ids=["mcp_one_shot", "cli_one_shot"],
)
def one_shot(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], object]:
    _write_no_mcp_config_file(monkeypatch)
    return request.param


def _write_no_mcp_config_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider, "_write_mcp_config", lambda _spec, _user_id: "unused")
    monkeypatch.setattr(provider, "_unlink_quiet", lambda _path: None)


def _replace_the_reap(
    monkeypatch: pytest.MonkeyPatch, reap: Callable[[object, object], object],
) -> None:
    monkeypatch.setattr(anchored_turn, "_reap_process_group", reap)


@dataclass
class _ReapRecorder:
    invocations: int = 0


def _record_the_real_reap(
    monkeypatch: pytest.MonkeyPatch, before_reaping: EventGate | None = None,
) -> _ReapRecorder:
    recorder = _ReapRecorder()
    real_reap = anchored_turn._reap_process_group

    async def counting_reap(child, reservation) -> None:
        recorder.invocations += 1
        holds = () if before_reaping is None else (before_reaping,)
        await _reap_after_holds(real_reap, child, reservation, *holds)

    _replace_the_reap(monkeypatch, counting_reap)
    return recorder


def test_a_successful_one_shot_holds_its_slot_and_home_until_the_reap_is_done(
    one_shot, monkeypatch, killpg_signals,
) -> None:
    reap_hold = EventGate(ScenarioDeadline())
    turn_child = _install_child_in_a_home(monkeypatch, communicate=(b'{"result":"ok"}', b""))
    reaps = _record_the_real_reap(monkeypatch, before_reaping=reap_hold)

    async def scenario() -> None:
        tasks: list[asyncio.Future[Any]] = []
        async with scenario_holds([reap_hold], tasks, set()):
            turn = asyncio.create_task(one_shot())
            tasks.append(turn)
            await reap_hold.started.wait()
            assert not turn.done()
            assert held_claude_slots() == 1
            assert turn_child.home.exists()

            reap_hold.release.set()
            assert (await turn).text == "ok"
            assert reaps.invocations == 1
            assert held_claude_slots() == 0
            assert not turn_child.home.exists()

    run_bounded(scenario())
    assert operations(turn_child.child.log) == [
        ("kill_until_gone", turn_child.child.pid), ("release", turn_child.child.pid),
    ]
    assert killpg_signals == []


@pytest.mark.parametrize(
    ("make_turn_failure", "expected_error", "message"),
    [
        (asyncio.TimeoutError, UnavailableError, "timed out"),
        (lambda: RuntimeError("turn broke"), RuntimeError, "turn broke"),
    ],
    ids=["timeout", "error"],
)
def test_a_failed_one_shot_reaps_once_then_releases_its_slot_and_home(
    one_shot, monkeypatch, killpg_signals, make_turn_failure, expected_error, message,
) -> None:
    turn_child = _install_child_in_a_home(
        monkeypatch, pid=A_PROCESS_PID, returncode=None, communicate=make_turn_failure(),
    )

    async def scenario() -> None:
        with pytest.raises(expected_error, match=message):
            await one_shot()
        assert held_claude_slots() == 0
        assert not turn_child.home.exists()

    run_bounded(scenario())
    assert operations(turn_child.child.log) == [
        ("SIGTERM", A_PROCESS_PID), ("kill_until_gone", A_PROCESS_PID), ("release", A_PROCESS_PID),
    ]
    assert killpg_signals == []


def test_a_cancelled_one_shot_receives_its_cancellation_only_after_the_reap(
    one_shot, monkeypatch,
) -> None:
    deadline = ScenarioDeadline()
    turn_gate = EventGate(deadline)
    exit_wait = EventGate(deadline)
    turn_child = _install_child_in_a_home(
        monkeypatch, returncode=None, communicate_gate=turn_gate, exit_gate=exit_wait,
    )

    async def scenario() -> None:
        tasks: list[asyncio.Future[Any]] = []
        async with scenario_holds([turn_gate, exit_wait], tasks, set()):
            turn = asyncio.create_task(one_shot())
            tasks.append(turn)
            await turn_gate.started.wait()
            turn.cancel()
            await exit_wait.started.wait()
            assert not turn.done()
            assert turn_child.home.exists()

            exit_wait.release.set()
            with pytest.raises(asyncio.CancelledError):
                await turn
            assert not turn_child.home.exists()

    run_bounded(scenario())


def test_a_one_shot_cancelled_twice_completes_one_reap_before_delivering_the_cancellation(
    one_shot, monkeypatch,
) -> None:
    deadline = ScenarioDeadline()
    first_stage = EventGate(deadline)
    second_stage = EventGate(deadline)
    real_reap = anchored_turn._reap_process_group
    reaps_completed = 0
    turn_child = _install_child_in_a_home(
        monkeypatch, returncode=None, communicate=RuntimeError("turn broke"),
    )

    async def two_stage_reap(child, reservation) -> None:
        nonlocal reaps_completed
        await _reap_after_holds(real_reap, child, reservation, first_stage, second_stage)
        reaps_completed += 1

    _replace_the_reap(monkeypatch, two_stage_reap)

    async def scenario() -> None:
        tasks: list[asyncio.Future[Any]] = []
        async with scenario_holds([first_stage, second_stage], tasks, set()):
            turn = asyncio.create_task(one_shot())
            tasks.append(turn)
            await first_stage.started.wait()
            turn.cancel()
            await _after_pending_callbacks()
            assert reaps_completed == 0

            first_stage.release.set()
            await second_stage.started.wait()
            turn.cancel()
            await _after_pending_callbacks()
            assert not turn.done()
            assert reaps_completed == 0
            assert turn_child.home.exists()

            second_stage.release.set()
            with pytest.raises(asyncio.CancelledError):
                await turn
            assert reaps_completed == 1
            assert not turn_child.home.exists()

    run_bounded(scenario())


class _CancellationOfTheTurn(Enum):
    NONE = auto()
    WHILE_THE_REAP_IS_HELD = auto()
    IN_THE_LOOP_ITERATION_THE_REAP_FAILS = auto()


@dataclass(frozen=True)
class _ReapFaultBoundary:
    make_turn_failure: Callable[[], BaseException]
    make_reap_fault: Callable[[], BaseException]
    cancellation: _CancellationOfTheTurn
    expected_context: type[BaseException]


def _a_turn_that_broke() -> BaseException:
    return RuntimeError("turn broke")


REAP_FAULT_BOUNDARIES = {
    "timeout": _ReapFaultBoundary(
        asyncio.TimeoutError, ReapFault, _CancellationOfTheTurn.NONE, UnavailableError,
    ),
    "error": _ReapFaultBoundary(
        _a_turn_that_broke, ReapFault, _CancellationOfTheTurn.NONE, RuntimeError,
    ),
    "cancellation": _ReapFaultBoundary(
        _a_turn_that_broke, ReapFault, _CancellationOfTheTurn.WHILE_THE_REAP_IS_HELD,
        RuntimeError,
    ),
    "cancellation_in_the_iteration_the_reap_fails": _ReapFaultBoundary(
        _a_turn_that_broke, ReapFault,
        _CancellationOfTheTurn.IN_THE_LOOP_ITERATION_THE_REAP_FAILS, RuntimeError,
    ),
    "file_not_found": _ReapFaultBoundary(
        asyncio.TimeoutError, FileNotFoundError, _CancellationOfTheTurn.NONE, UnavailableError,
    ),
}


@pytest.mark.parametrize("boundary", REAP_FAULT_BOUNDARIES.values(), ids=REAP_FAULT_BOUNDARIES)
def test_the_reaps_own_exception_wins_over_the_turns_exit(
    one_shot, monkeypatch, boundary,
) -> None:
    reap_gate = EventGate(ScenarioDeadline())
    reap_fault = boundary.make_reap_fault()
    real_reap = anchored_turn._reap_process_group
    _install_child_in_a_home(
        monkeypatch, returncode=None, communicate=boundary.make_turn_failure(),
    )

    async def reap_that_fails(child, reservation) -> None:
        await _reap_after_holds(real_reap, child, reservation, reap_gate)
        raise reap_fault

    _replace_the_reap(monkeypatch, reap_that_fails)

    async def scenario() -> None:
        tasks: list[asyncio.Future[Any]] = []
        asserted_faults: set[asyncio.Future[Any]] = set()
        async with scenario_holds([reap_gate], tasks, asserted_faults):
            turn = asyncio.create_task(one_shot())
            tasks.append(turn)
            await reap_gate.started.wait()
            if boundary.cancellation is _CancellationOfTheTurn.WHILE_THE_REAP_IS_HELD:
                turn.cancel()
                await _after_pending_callbacks()
            reap_gate.release.set()
            if boundary.cancellation is _CancellationOfTheTurn.IN_THE_LOOP_ITERATION_THE_REAP_FAILS:
                turn.cancel()
            with pytest.raises(type(reap_fault)) as caught:
                await turn
            asserted_faults.add(turn)
            assert caught.value is reap_fault
            assert isinstance(caught.value.__context__, boundary.expected_context)

    run_bounded(scenario())


def test_a_stream_that_ends_normally_reaps_once_and_releases_its_slot_and_home(
    monkeypatch, killpg_signals,
) -> None:
    deadline = ScenarioDeadline()
    exit_wait = EventGate(deadline)
    reap_entered = EventGate(deadline)
    turn_child = _install_child_in_a_home(
        monkeypatch,
        returncode=None,
        stream_lines=[b'{"type":"result","subtype":"success","result":"ok"}\n'],
        exit_gate=exit_wait,
    )
    reaps = _record_the_real_reap(monkeypatch, before_reaping=reap_entered)

    async def scenario() -> None:
        tasks: list[asyncio.Future[Any]] = []
        async with scenario_holds([exit_wait, reap_entered], tasks, set()):
            stream = provider.acall_claude_with_mcp_stream("hi", user_id="u-1")
            consuming = asyncio.create_task(_collect_stream_events(stream))
            tasks.append(consuming)
            await exit_wait.started.wait()
            assert not consuming.done()
            assert held_claude_slots() == 1
            assert turn_child.home.exists()

            exit_wait.release.set()
            await reap_entered.started.wait()
            assert not consuming.done()
            assert held_claude_slots() == 1
            assert turn_child.home.exists()

            reap_entered.release.set()
            assert (await consuming)[-1].text == "ok"
            assert reaps.invocations == 1
            assert held_claude_slots() == 0
            assert not turn_child.home.exists()

    run_bounded(scenario())
    assert operations(turn_child.child.log) == [
        ("kill_until_gone", turn_child.child.pid), ("release", turn_child.child.pid),
    ]
    assert killpg_signals == []


async def _stream_to_its_end() -> None:
    async for _event in provider.acall_claude_with_mcp_stream("hi", user_id="u-1"):
        pass


async def _stream_closed_after_its_first_event() -> None:
    stream = provider.acall_claude_with_mcp_stream("hi", user_id="u-1")
    await anext(stream)
    await stream.aclose()


@dataclass(frozen=True)
class _TurnEnding:
    """One way an async turn ends: how it is driven, what its child does, what it raises."""

    drive: Callable[[], Awaitable[object]]
    child_script: dict[str, Any]
    raises: tuple[type[BaseException], str] | None
    exit_of_the_turn: type[BaseException]
    iteration_failure: bool = False


TURN_ENDINGS = {
    "mcp_one_shot_timeout": _TurnEnding(
        _call_mcp_one_shot,
        {"communicate": asyncio.TimeoutError()},
        (UnavailableError, "timed out"),
        UnavailableError,
    ),
    "cli_one_shot_error": _TurnEnding(
        _call_cli_one_shot,
        {"communicate": RuntimeError("turn broke")},
        (RuntimeError, "turn broke"),
        RuntimeError,
    ),
    "stream_iteration_failure": _TurnEnding(
        _stream_to_its_end,
        {"stream_lines": [A_STREAMED_LINE]},
        (RuntimeError, "stream read failed"),
        RuntimeError,
        iteration_failure=True,
    ),
    "stream_closed_after_its_first_event": _TurnEnding(
        _stream_closed_after_its_first_event,
        {"stream_lines": [A_STREAMED_LINE], "stdout_stalls": True},
        None,
        GeneratorExit,
    ),
}


def _prepare_the_turn(
    monkeypatch: pytest.MonkeyPatch,
    ending: _TurnEnding,
    build_child: Callable[..., ScriptedClaudeChild] = a_claude_cli_child,
    on_spawn: Callable[[], None] = lambda: None,
    **child_script: object,
) -> _ChildInAHome:
    _write_no_mcp_config_file(monkeypatch)
    if ending.iteration_failure:
        monkeypatch.setattr(provider, "_iter_lines", lines_that_fail)
    return _install_child_in_a_home(
        monkeypatch,
        build_child,
        on_spawn,
        **{**ending.child_script, **child_script},
    )


async def _end_the_turn_with_its_own_error(ending: _TurnEnding) -> None:
    if ending.raises is None:
        await ending.drive()
        return
    expected_error, message = ending.raises
    with pytest.raises(expected_error, match=message):
        await ending.drive()


SIGKILL_ENDS_THE_LEADER = (
    "mcp_one_shot_timeout",
    "cli_one_shot_error",
    "stream_closed_after_its_first_event",
)


@pytest.mark.parametrize(
    "ending", [TURN_ENDINGS[name] for name in SIGKILL_ENDS_THE_LEADER], ids=SIGKILL_ENDS_THE_LEADER,
)
def test_sigkill_ends_a_leader_that_survives_sigterm_before_its_slot_and_home_are_freed(
    ending: _TurnEnding, monkeypatch, killpg_signals,
) -> None:
    reap_without_waiting(monkeypatch)
    turn = _prepare_the_turn(
        monkeypatch, ending, returncode=None, natural_exit=None, survives={signal.SIGTERM},
    )

    run_bounded(_end_the_turn_with_its_own_error(ending))

    pid = turn.child.pid
    assert operations(turn.child.log) == [
        ("SIGTERM", pid), ("kill_until_gone", pid), ("release", pid),
    ]
    assert held_claude_slots() == 0
    assert not turn.home.exists()
    assert killpg_signals == []


def _provider_records_after(caplog: pytest.LogCaptureFixture, spawned_at: int) -> list[str]:
    return [
        f"{record.levelname} {record.getMessage()}"
        for record in caplog.records[spawned_at:]
        if record.name in {provider.log.name, anchored_turn.log.name}
    ]


@pytest.mark.parametrize("ending", TURN_ENDINGS.values(), ids=TURN_ENDINGS)
def test_a_leader_that_outlives_sigkill_keeps_its_slot_and_home_until_its_group_is_confirmed_gone(
    ending: _TurnEnding, monkeypatch, killpg_signals, caplog,
) -> None:
    reap_without_waiting(monkeypatch)
    records_before_spawn: list[int] = []
    turn = _prepare_the_turn(
        monkeypatch,
        ending,
        a_child_that_outlives_sigkill,
        lambda: records_before_spawn.append(len(caplog.records)),
    )
    child = turn.child
    pid = child.pid
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        with pytest.raises(ContainmentLost) as lost:
            await ending.drive()
        group = lost.value.group
        try:
            assert group.pgid == pid
            assert lost.value.__cause__ is None
            assert isinstance(lost.value.__context__, ending.exit_of_the_turn)
            assert operations(child.log) == [("SIGTERM", pid), ("kill_until_gone", pid)]
            assert held_claude_slots() == 1
            assert turn.home.exists()
            assert _provider_records_after(caplog, records_before_spawn[0]) == []

            reset_config()
            child.publish_exit(-signal.SIGKILL)
            assert await lost_groups.confirm_group_gone(group)
            assert operations(child.log) == [
                ("SIGTERM", pid), ("kill_until_gone", pid), ("kill", pid), ("release", pid),
            ]
            assert held_claude_slots() == 0
            assert not turn.home.exists()
            assert await lost_groups.confirm_group_gone(group)
            assert len(child.log) == 4
        finally:
            assert await confirm_the_retained_group_gone(child, group)

    with recorded_census_workers(monkeypatch):
        run_bounded(scenario())
    assert killpg_signals == []


@dataclass(frozen=True)
class _FailingAnchor:
    """A child whose anchor fails in one step of the reap, and how its retention can end."""

    script: dict[str, Any]
    cause: type[BaseException]
    recoverable: bool


def _lost_anchor() -> ChildIdentityLost:
    return ChildIdentityLost("a foreign reaper broke the anchor")


FAILING_ANCHORS = {
    "signal_group_loses_the_anchor": _FailingAnchor(
        {"returncode": None, "signal_loss": _lost_anchor()}, ChildIdentityLost, False,
    ),
    "exited_loses_the_anchor": _FailingAnchor(
        {"returncode": None, "exit_loss": _lost_anchor()}, ChildIdentityLost, False,
    ),
    "release_loses_the_anchor": _FailingAnchor(
        {"release_failures": [_lost_anchor()]}, ChildIdentityLost, False,
    ),
    "release_finds_the_leader_unreaped": _FailingAnchor(
        {"release_failures": [AnchorStateError("the kernel did not reap the exited leader")]},
        AnchorStateError,
        True,
    ),
}


@pytest.mark.parametrize("failing", FAILING_ANCHORS.values(), ids=FAILING_ANCHORS)
def test_a_failing_anchor_is_retained_with_its_slot_and_home(
    failing: _FailingAnchor, monkeypatch, killpg_signals,
) -> None:
    ending = TURN_ENDINGS["mcp_one_shot_timeout"]
    reap_without_waiting(monkeypatch)
    turn = _prepare_the_turn(monkeypatch, ending, **failing.script)
    child = turn.child
    retentions: list[tuple[RetainedGroup, Callable[[], None]]] = []
    real_retain = anchored_turn.retain_lost_group

    def keep_the_retention(retained_child, release_slot) -> RetainedGroup:
        group = real_retain(retained_child, release_slot)
        retentions.append((group, release_slot))
        return group

    monkeypatch.setattr(anchored_turn, "retain_lost_group", keep_the_retention)

    async def scenario() -> None:
        with pytest.raises(ContainmentLost) as lost:
            await ending.drive()
        try:
            assert type(lost.value.__cause__) is failing.cause
            assert held_claude_slots() == 1
            if failing.recoverable:
                assert await lost_groups.confirm_group_gone(lost.value.group)
                assert [name for name, _pid in operations(child.log)].count("release") == 2
                assert held_claude_slots() == 0
            else:
                for _ in range(2):
                    with pytest.raises(ChildIdentityLost):
                        await lost_groups.confirm_group_gone(lost.value.group)
                assert held_claude_slots() == 1
        finally:
            if failing.recoverable:
                assert await confirm_the_retained_group_gone(child, lost.value.group)
            else:
                for _group, release_slot in retentions:
                    release_slot()

    with recorded_census_workers(monkeypatch):
        run_bounded(scenario())
    assert held_claude_slots() == 0
    if failing.recoverable:
        assert not turn.home.exists()
    assert killpg_signals == []


def test_shutdown_leaves_a_retained_group_as_it_is(
    claude_binary, monkeypatch, killpg_signals,
) -> None:
    ending = TURN_ENDINGS["mcp_one_shot_timeout"]
    reap_without_waiting(monkeypatch)
    turn = _prepare_the_turn(monkeypatch, ending, a_child_that_outlives_sigkill)
    child = turn.child
    probe_started = asyncio.Event()

    async def pending_probe(*_args, **_kwargs) -> provider._AnnouncedSurface:
        probe_started.set()
        await asyncio.Future()

    monkeypatch.setattr(provider, "_probe_cli_surface_async", pending_probe)

    async def scenario() -> None:
        with pytest.raises(ContainmentLost) as lost:
            await ending.drive()
        group = lost.value.group
        try:
            waiter = asyncio.create_task(averify_no_builtin_cli_tools())
            await probe_started.wait()
            log_before_shutdown = list(child.log)

            await provider.shutdown_tool_surface_background_tasks()

            with pytest.raises(UnavailableError):
                await waiter
            assert child.log == log_before_shutdown
            assert held_claude_slots() == 1
            assert turn.home.exists()
        finally:
            assert await confirm_the_retained_group_gone(child, group)
        assert held_claude_slots() == 0
        assert not turn.home.exists()

    with recorded_census_workers(monkeypatch):
        run_bounded(scenario())
    assert killpg_signals == []


# ── no-builtin-tools gate (_call_cli / _acall_cli) ────────────────────


def test_no_builtin_gate_accepts_a_cli_offering_nothing(
    claude_binary,
    monkeypatch,
) -> None:
    commands = _answer_with(monkeypatch, _init_line([]))

    binary = asyncio.run(averify_no_builtin_cli_tools())

    assert binary == claude_binary
    assert "--mcp-config" not in commands[0]
    assert "--allowedTools" not in commands[0]


def test_no_builtin_gate_rejects_a_cli_offering_any_tool(
    claude_binary,
    monkeypatch,
) -> None:
    _answer_with(monkeypatch, _init_line(["Bash"]))

    probe = averify_no_builtin_cli_tools()
    with pytest.raises(CliToolSurfaceError) as exc:
        asyncio.run(probe)
    assert "Bash" in str(exc.value)


def test_no_builtin_gate_rejects_a_cli_still_advertising_slash_commands(
    claude_binary,
    monkeypatch,
) -> None:
    _answer_with(monkeypatch, _init_line([], slash_commands=["/help"]))

    probe = averify_no_builtin_cli_tools()
    with pytest.raises(CliToolSurfaceError) as exc:
        asyncio.run(probe)
    assert "/help" in str(exc.value)


def test_no_builtin_gate_and_mcp_gate_cache_independently(
    claude_binary,
    monkeypatch,
) -> None:
    """Same binary build, different expectation — a tool turn passing
    verify_cli_tool_surface() must not let _call_cli skip its own probe,
    and vice versa; they check different command shapes."""
    commands = _answer_with(
        monkeypatch,
        _init_line(_ALL_SAMPLE_TOOLS),
        _init_line([]),
    )

    asyncio.run(verify_cli_tool_surface())
    asyncio.run(averify_no_builtin_cli_tools())

    assert len(commands) == 2


def test_no_builtin_gate_sync_twin_accepts_a_cli_offering_nothing(
    claude_binary,
    monkeypatch,
) -> None:
    _answer_with(monkeypatch, _init_line([]))

    binary = verify_no_builtin_cli_tools()

    assert binary == claude_binary


def test_no_builtin_gate_sync_twin_rejects_a_cli_offering_any_tool(
    claude_binary,
    monkeypatch,
) -> None:
    _answer_with(monkeypatch, _init_line(["Bash"]))

    with pytest.raises(CliToolSurfaceError) as exc:
        verify_no_builtin_cli_tools()
    assert "Bash" in str(exc.value)


def test_no_builtin_gate_sync_twin_kills_a_still_running_probe(
    claude_binary,
    monkeypatch,
) -> None:
    child = fake_cli_process(_init_line([]), still_running=True)
    install_cli_door(monkeypatch, lambda *_cmd, **_kw: child)

    verify_no_builtin_cli_tools()

    assert _signals_sent(child) == [signal.SIGTERM]
    assert child.released


def test_no_builtin_gate_sync_single_flight_waits_for_the_real_result_not_a_placeholder(
    claude_binary,
    monkeypatch,
) -> None:
    """#351 rounds 4-5, Finding 2, sync side: the same two mutations the
    async test guards against (a placeholder-success follower; a follower
    that starts its own second probe on failure instead of accepting the
    shared one), and the same reason a bare ``calls == 1`` check on a
    *successful* probe cannot catch either. The probe fails outright
    (malformed output) specifically to exercise the second one.

    No timing assumption stands in for "the second caller reached the
    in-flight wait": a real ``threading.Event``, set from inside
    ``_claim_or_join_inflight_sync`` the moment it identifies the caller as
    a follower, is what the test waits on. The remaining timeouts are
    deadlock guards only (generous, never the expected path) — everything
    above resolves in well under 100ms.
    """
    calls = 0
    probe_started = threading.Event()
    release_probe = threading.Event()
    follower_joined = threading.Event()

    def fake_popen(_cmd, **_kw):
        nonlocal calls
        calls += 1
        probe_started.set()
        release_probe.wait()
        return fake_cli_process(b"not valid json\n")

    install_cli_door(monkeypatch, fake_popen)

    real_claim = provider._claim_or_join_inflight_sync

    def _instrumented_claim(key):
        future, is_leader = real_claim(key)
        if not is_leader:
            follower_joined.set()
        return future, is_leader

    monkeypatch.setattr(provider, "_claim_or_join_inflight_sync", _instrumented_claim)

    results: list[object] = [None, None]

    def _call(index: int) -> None:
        try:
            results[index] = verify_no_builtin_cli_tools()
        except Exception as exc:  # noqa: BLE001 - captured, asserted below
            results[index] = exc

    first = threading.Thread(target=_call, args=(0,))
    second = threading.Thread(target=_call, args=(1,))
    first.start()
    assert probe_started.wait(timeout=5), "first caller never reached its probe"
    second.start()
    assert follower_joined.wait(timeout=5), "second caller never reached the in-flight wait"
    assert results[1] is None, (
        "second caller returned before the in-flight probe resolved — "
        "it must wait for the real answer, not assume success"
    )

    release_probe.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert calls == 1, "a follower must not start its own second probe on failure"
    assert all(isinstance(r, UnavailableError) for r in results)
    assert not any(isinstance(r, CliToolSurfaceError) for r in results)


_FLIGHT_BOUND_SECONDS = 5


class _HeldProbes:
    """Tool-surface probes that each wait for their release; the first one fails."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = [threading.Event() for _ in range(3)]
        self._released = [threading.Event() for _ in range(3)]
        self.count = 0
        self.follower_joined = threading.Event()

    def run(self) -> provider._AnnouncedSurface:
        with self._lock:
            index = self.count
            self.count += 1
        self._started[index].set()
        assert self._released[index].wait(timeout=_FLIGHT_BOUND_SECONDS), "probe never released"
        if index == 0:
            raise RuntimeError("the first flight fails without a cached verdict")
        return provider._AnnouncedSurface(tools=(), slash_commands=(), mcp_connected=None)

    def wait_until_started(self, index: int) -> None:
        assert self._started[index].wait(timeout=_FLIGHT_BOUND_SECONDS)

    def release(self, index: int) -> None:
        self._released[index].set()

    def release_all(self) -> None:
        for released in self._released:
            released.set()


def _drive_the_cleared_flight_on_threads(held: _HeldProbes) -> list[object]:
    outcomes: list[object] = [None, None, None]

    def caller(index: int) -> None:
        try:
            outcomes[index] = verify_no_builtin_cli_tools()
        except Exception as exc:  # noqa: BLE001 - captured, asserted by the caller
            outcomes[index] = exc

    threads = [threading.Thread(target=caller, args=(index,), daemon=True) for index in range(3)]
    try:
        threads[0].start()
        held.wait_until_started(0)
        clear_cli_tool_surface_cache()
        threads[1].start()
        held.wait_until_started(1)
        held.release(0)
        threads[0].join(timeout=_FLIGHT_BOUND_SECONDS)
        threads[2].start()
        assert held.follower_joined.wait(timeout=_FLIGHT_BOUND_SECONDS)
        held.release(1)
    finally:
        held.release_all()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=_FLIGHT_BOUND_SECONDS)
    return outcomes


def _drive_the_cleared_flight_on_tasks(held: _HeldProbes) -> list[object]:
    async def scenario() -> list[object]:
        tasks: list[asyncio.Task] = []
        try:
            tasks.append(asyncio.create_task(averify_no_builtin_cli_tools()))
            await asyncio.to_thread(held.wait_until_started, 0)
            clear_cli_tool_surface_cache()
            tasks.append(asyncio.create_task(averify_no_builtin_cli_tools()))
            await asyncio.to_thread(held.wait_until_started, 1)
            held.release(0)
            await asyncio.gather(tasks[0], return_exceptions=True)
            tasks.append(asyncio.create_task(averify_no_builtin_cli_tools()))
            await asyncio.to_thread(held.follower_joined.wait, _FLIGHT_BOUND_SECONDS)
            assert held.follower_joined.is_set()
            held.release(1)
        finally:
            held.release_all()
        return await asyncio.gather(*tasks, return_exceptions=True)

    return asyncio.run(asyncio.wait_for(scenario(), _FLIGHT_BOUND_SECONDS * 2))


@pytest.mark.parametrize(
    ("drive", "claim_name"),
    (
        (_drive_the_cleared_flight_on_threads, "_claim_or_join_inflight_sync"),
        (_drive_the_cleared_flight_on_tasks, "_claim_or_join_inflight_async"),
    ),
    ids=("sync", "async"),
)
def test_a_cleared_tool_surface_flight_does_not_retire_its_replacement(
    claude_binary, monkeypatch, drive, claim_name,
) -> None:
    held = _HeldProbes()

    async def held_async_probe(*_args, **_kwargs) -> provider._AnnouncedSurface:
        return await asyncio.to_thread(held.run)

    monkeypatch.setattr(
        provider, "_probe_cli_surface_sync", lambda *_args, **_kwargs: held.run(),
    )
    monkeypatch.setattr(provider, "_probe_cli_surface_async", held_async_probe)
    real_claim = getattr(provider, claim_name)

    def instrumented_claim(key):
        future, is_leader = real_claim(key)
        if not is_leader:
            held.follower_joined.set()
        return future, is_leader

    monkeypatch.setattr(provider, claim_name, instrumented_claim)

    outcomes = drive(held)

    assert isinstance(outcomes[0], Exception)
    assert outcomes[1:] == [claude_binary, claude_binary]
    assert held.count == 2


def test_no_builtin_gate_sync_and_async_share_one_cache(
    claude_binary,
    monkeypatch,
) -> None:
    """Async and sync gates key their cache on the same (binary build,
    expectation) pair; since round 6 they even spawn through the same
    subprocess.Popen mechanism (the async gate delegates to the sync one
    via asyncio.to_thread), so one probe now answers both outright."""
    commands = _answer_with(monkeypatch, _init_line([]))

    asyncio.run(averify_no_builtin_cli_tools())
    verify_no_builtin_cli_tools()

    assert len(commands) == 1


@pytest.mark.parametrize(
    ("parent", "other"),
    (
        (CliUnavailableError, CliLoginNotConfiguredError),
        (CliLoginNotConfiguredError, CliUnavailableError),
    ),
    ids=("unavailable", "not-configured"),
)
def test_the_claude_cli_faults_are_unavailable_errors_and_not_each_other(parent, other) -> None:
    assert issubclass(parent, UnavailableError)
    assert not issubclass(parent, other)


def _a_claude_binary_missing_at(site: str, monkeypatch: pytest.MonkeyPatch):
    if site == "require":
        monkeypatch.setattr(provider, "_find_claude_binary", lambda: None)
        return cli_model_catalog
    _verify_the_claude_surfaces(monkeypatch)
    _install_a_start_that_raises(monkeypatch, FileNotFoundError())
    return _a_turn_at(site)


def _verify_the_claude_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provider,
        "averify_no_builtin_cli_tools",
        AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    monkeypatch.setattr(
        provider,
        "verify_cli_tool_surface",
        AsyncMock(return_value=Path("/usr/bin/claude")),
    )
    monkeypatch.setattr(provider, "_write_mcp_config", lambda _spec, _user_id: "unused")
    monkeypatch.setattr(provider, "_unlink_quiet", lambda _path: None)


def _a_turn_at(site: str) -> Callable[[], object]:
    if site == "acall-cli":
        return lambda: asyncio.run(_acall_cli("hello"))
    if site == "mcp":
        return lambda: asyncio.run(provider.acall_claude_with_mcp("hi", user_id="u-1"))
    return lambda: asyncio.run(acall_claude_with_mcp_stream(prompt="hi", user_id="u-1").__anext__())


@pytest.mark.parametrize(
    ("site", "detail"),
    (
        ("require", "Claude CLI not found"),
        ("acall-cli", CLAUDE_CLI_BINARY_NOT_FOUND_DETAIL),
        ("mcp", CLAUDE_CLI_BINARY_NOT_FOUND_DETAIL),
        ("mcp-stream", CLAUDE_CLI_BINARY_NOT_FOUND_DETAIL),
    ),
)
def test_a_missing_claude_binary_raises_cli_unavailable(
    site: str, detail: str, _no_tool_gate_open, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _a_claude_binary_missing_at(site, monkeypatch)

    with pytest.raises(CliUnavailableError, match=detail):
        run()


@pytest.mark.parametrize("site", ["acall-cli", "mcp", "mcp-stream"])
def test_a_missing_claude_binary_frees_the_turns_slot_and_home(
    site: str,
    _no_tool_gate_open,
    monkeypatch: pytest.MonkeyPatch,
    killpg_signals,
) -> None:
    _verify_the_claude_surfaces(monkeypatch)
    spawns = _install_a_start_that_raises(monkeypatch, FileNotFoundError())
    turn = _a_turn_at(site)

    with pytest.raises(CliUnavailableError, match=CLAUDE_CLI_BINARY_NOT_FOUND_DETAIL) as raised:
        turn()

    assert isinstance(raised.value.__cause__, FileNotFoundError)
    assert held_claude_slots() == 0
    assert not spawns.children[0].working_directory.exists()
    assert killpg_signals == []


def test_another_start_failure_is_not_a_missing_binary_and_frees_the_turns_slot_and_home(
    monkeypatch: pytest.MonkeyPatch,
    killpg_signals,
) -> None:
    _verify_the_claude_surfaces(monkeypatch)
    spawns = _install_a_start_that_raises(monkeypatch, PermissionError())
    turn = provider.acall_claude_with_mcp("hi", user_id="u-1")

    with pytest.raises(PermissionError):
        asyncio.run(turn)

    assert held_claude_slots() == 0
    assert not spawns.children[0].working_directory.exists()
    assert killpg_signals == []


class _PoolLockProbe:
    """Tries the pool lock once per call without ever waiting on it.

    A callout that runs while the pool lock is held finds the lock taken and
    records that, instead of blocking the thread that would release it.
    """

    def __init__(self) -> None:
        self.found_the_lock_free: list[bool] = []

    def __call__(self) -> None:
        acquired = provider.CLAUDE_CLI_PROCESS_POOL._lock.acquire(blocking=False)
        self.found_the_lock_free.append(acquired)
        if acquired:
            provider.CLAUDE_CLI_PROCESS_POOL._lock.release()


class _PoolLockProbingHandler(logging.Handler):
    def __init__(self, probe: _PoolLockProbe) -> None:
        super().__init__()
        self.probe = probe
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.probe()
        self.messages.append(record.getMessage())


def test_the_pool_lock_is_not_held_while_a_home_is_released() -> None:
    probe = _PoolLockProbe()
    home_reservation = MagicMock(spec_set=["release"])
    home_reservation.release.side_effect = probe
    reservation = anchored_turn.CliProcessReservation(
        pool=provider.CLAUDE_CLI_PROCESS_POOL, home_reservation=home_reservation,
    )
    with provider.CLAUDE_CLI_PROCESS_POOL._lock:
        provider.CLAUDE_CLI_PROCESS_POOL._reservations.add(reservation)
    try:
        call_within_deadline(provider.CLAUDE_CLI_PROCESS_POOL.release, reservation)

        assert probe.found_the_lock_free == [True]
        assert held_claude_slots() == 0
    finally:
        with provider.CLAUDE_CLI_PROCESS_POOL._lock:
            provider.CLAUDE_CLI_PROCESS_POOL._reservations.discard(reservation)


def test_the_pool_lock_is_not_held_while_the_pool_limit_is_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_claude_process_limit(monkeypatch, 1)
    probe = _PoolLockProbe()
    handler = _PoolLockProbingHandler(probe)
    own_reservation = provider.CLAUDE_CLI_PROCESS_POOL.reserve()
    assert own_reservation is not None
    anchored_turn.log.addHandler(handler)
    try:
        refused = call_within_deadline(provider.CLAUDE_CLI_PROCESS_POOL.reserve)

        assert refused is None
        assert handler.messages == [provider.CLAUDE_CLI_PROCESS_POOL.limit_message()]
        assert probe.found_the_lock_free == [True]
    finally:
        anchored_turn.log.removeHandler(handler)
        provider.CLAUDE_CLI_PROCESS_POOL.release(own_reservation)


def _cli_root_removed(tmp_path: Path) -> None:
    root = tmp_path / "removed-root"
    root.mkdir(mode=0o700)
    override_provider_runtime(cli_working_directory_root=root)
    root.rmdir()


def _credential_source_replaced_by_a_link(tmp_path: Path) -> None:
    source = current_claude_runtime().token_file
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    source.unlink()
    source.symlink_to(target)


@pytest.mark.parametrize(
    ("make_fault", "raised_class", "detail"),
    (
        (_cli_root_removed, CliUnavailableError, CLAUDE_CLI_UNAVAILABLE_DETAIL),
        (
            _credential_source_replaced_by_a_link,
            CliLoginNotConfiguredError,
            CLAUDE_CLI_LOGIN_NOT_CONFIGURED_DETAIL,
        ),
    ),
    ids=("home-not-made", "linked-credential"),
)
def test_the_claude_model_listing_raises_only_the_claude_family(
    make_fault, raised_class, detail, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider, "_find_claude_binary", lambda: Path("/usr/bin/claude"))
    make_fault(tmp_path)

    with pytest.raises(UnavailableError) as raised:
        cli_model_catalog()

    assert type(raised.value) is raised_class
    assert str(raised.value) == detail
    assert isinstance(raised.value.__cause__, process.AgentCliUnavailableError)


@pytest.mark.parametrize("fault", DESTINATION_FAULTS)
def test_a_credential_copy_that_cannot_be_written_raises_cli_unavailable(
    fault: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider, "_find_claude_binary", lambda: Path("/usr/bin/claude"))
    with a_credential_destination_that_fails(fault):
        with pytest.raises(UnavailableError) as raised:
            cli_model_catalog()

    assert type(raised.value) is CliUnavailableError
    assert str(raised.value) == CLAUDE_CLI_UNAVAILABLE_DETAIL
    assert isinstance(raised.value.__cause__, process.CliEnvironmentUnavailableError)
    assert raised.value.__cause__.cause is process.CliEnvironmentCause.CREDENTIAL_COPY_NOT_WRITTEN


def _probe_failing_with(error_class, message: str, alive: list[weakref.ref]):
    def fail() -> None:
        error = error_class(message)
        alive.append(weakref.ref(error))
        raise error

    return fail


def _run_to_completion(coroutine_function) -> None:
    asyncio.run(coroutine_function())


def _failure_of(call) -> tuple[type, str, BaseException | None]:
    try:
        call()
    except UnavailableError as exc:
        return type(exc), str(exc), exc.__cause__
    raise AssertionError("the call did not fail")


@pytest.mark.parametrize("probe_kind", ("async", "sync"))
@pytest.mark.parametrize(
    ("error_class", "message"),
    (
        (CliLoginNotConfiguredError, CLAUDE_CLI_LOGIN_NOT_CONFIGURED_DETAIL),
        (CliUnavailableError, CLAUDE_CLI_UNAVAILABLE_DETAIL),
        (provider._ZombieProbeError, "probe process outlived SIGKILL"),
    ),
    ids=("login", "environment", "zombie"),
)
def test_a_failed_tool_surface_probe_replays_its_class_and_message_without_the_exception(
    probe_kind: str, error_class, message: str, claude_binary, monkeypatch: pytest.MonkeyPatch
) -> None:
    alive: list[weakref.ref] = []
    fail = _probe_failing_with(error_class, message, alive)
    probes: list[None] = []

    async def failing_async_probe(*_args, **_kwargs):
        probes.append(None)
        fail()

    def failing_sync_probe(*_args, **_kwargs):
        probes.append(None)
        fail()

    if probe_kind == "async":
        monkeypatch.setattr(provider, "_probe_cli_surface_async", failing_async_probe)
        verify = partial(_run_to_completion, verify_cli_tool_surface)
    else:
        monkeypatch.setattr(provider, "_probe_cli_surface_sync", failing_sync_probe)
        verify = verify_no_builtin_cli_tools

    _failure_of(verify)
    gc.collect()
    replayed = _failure_of(verify)

    assert replayed == (error_class, message, None)
    assert len(probes) == 1
    assert alive[0]() is None
