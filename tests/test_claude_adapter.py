"""Behavior of the Claude adapters: cancellation and API turn failures."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from provider_test_support import (
    a_child_that_outlives_sigkill,
    a_credential_destination_that_fails,
    confirm_the_retained_group_gone,
    install_claude_cli_children,
    killpg_forbidden,
    override_provider_runtime,
    reap_without_waiting,
    recorded_census_workers,
    run_bounded,
)

from agent_providers.claude import adapter as claude_adapter
from agent_providers.claude import provider as claude_provider
from agent_providers.claude.provider import (
    CLAUDE_CLI_LOGIN_NOT_CONFIGURED_DETAIL,
    CLAUDE_CLI_UNAVAILABLE_DETAIL,
    CliLoginNotConfiguredError,
    CliLoginRefusedError,
    CliToolSurfaceError,
    CliUnavailableError,
    UnavailableError,
)
from agent_providers.config import current_config
from agent_providers.errors import ContainmentLost, ProviderUnavailableError, SafeRouteReasonCode
from agent_providers.events import AssistantTextEvent, StreamEvent
from agent_providers.process import CliEnvironmentUnavailableError, CliLoginUnusableError
from agent_providers.tool_loop import ToolOutcome


class _TrackingProviderStream(AsyncIterator[StreamEvent]):
    def __init__(self) -> None:
        self.aclose_calls = 0
        self._has_yielded = False

    def __aiter__(self) -> _TrackingProviderStream:
        return self

    async def __anext__(self) -> StreamEvent:
        if not self._has_yielded:
            self._has_yielded = True
            return AssistantTextEvent(text="partial")
        await asyncio.Future()

    async def aclose(self) -> None:
        self.aclose_calls += 1


def test_closing_claude_adapter_stream_closes_provider_stream(monkeypatch) -> None:
    provider_stream = _TrackingProviderStream()
    monkeypatch.setattr(
        claude_adapter,
        "acall_claude_with_mcp_stream",
        lambda **_kwargs: provider_stream,
    )

    async def _close_stream() -> None:
        stream = claude_adapter.stream_claude_turn(
            user_id="user-1",
            system="system",
            model="claude-test",
            messages=[],
        )
        assert await anext(stream) == AssistantTextEvent(text="partial")
        await stream.aclose()

    asyncio.run(_close_stream())

    assert provider_stream.aclose_calls == 1


A_STREAMED_LINE = (
    b'{"type":"assistant","message":{"content":[{"type":"text","text":"partial"}]}}\n'
)


@pytest.mark.parametrize("ending", ["iteration_fails", "closed_after_the_first_event"])
def test_a_containment_loss_passes_the_adapter_unchanged(
    monkeypatch: pytest.MonkeyPatch, ending: str,
) -> None:
    reap_without_waiting(monkeypatch)
    child = a_child_that_outlives_sigkill(stream_lines=[A_STREAMED_LINE], stdout_stalls=True)
    install_claude_cli_children(monkeypatch, child)
    if ending == "iteration_fails":
        monkeypatch.setattr(claude_provider, "_iter_lines", _lines_that_fail)

    async def end_the_turn() -> ContainmentLost:
        stream = claude_adapter.stream_claude_turn(
            user_id="user-1", system="system", model="claude-test", messages=[],
        )
        try:
            if ending == "closed_after_the_first_event":
                assert await anext(stream) == AssistantTextEvent(text="partial")
                await stream.aclose()
            else:
                await anext(stream)
        except ContainmentLost as lost:
            return lost
        finally:
            await stream.aclose()
        pytest.fail("the turn ended without a containment loss")

    with killpg_forbidden(), recorded_census_workers(monkeypatch) as census_workers:
        lost = run_bounded(end_the_turn())
        try:
            assert lost.group.pgid == child.pid
            assert lost.__cause__ is None
        finally:
            assert run_bounded(confirm_the_retained_group_gone(child, lost.group))
            census_workers.join_all()


def _lines_that_fail(_stdout: object, _timeout_seconds: int) -> AsyncIterator[bytes]:
    raise RuntimeError("the stream broke")


@pytest.mark.parametrize("entry_point", ["stream_claude_turn", "call_claude_once"])
def test_claude_adapter_refuses_an_empty_model_before_any_child_or_client_exists(
    entry_point, monkeypatch,
) -> None:
    sdk = SimpleNamespace(Anthropic=lambda **_kwargs: pytest.fail("no client may be built"))
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    monkeypatch.setattr(
        claude_provider, "run_capturing", lambda *_a, **_k: pytest.fail("no child may start"),
    )
    install_claude_cli_children(monkeypatch)

    async def drive() -> None:
        if entry_point == "call_claude_once":
            claude_adapter.call_claude_once(model="", prompt="hi", timeout=1)
            return
        async for _event in claude_adapter.stream_claude_turn(
            user_id="user-1", system="system", model="", messages=[],
        ):
            pytest.fail("no event may be yielded")

    turn = drive()
    with pytest.raises(ProviderUnavailableError) as refused:
        asyncio.run(turn)

    assert refused.value.reason.code is SafeRouteReasonCode.ROUTE_FAILED


class _SdkClientThatFails:
    def __init__(self, **_client_options: object) -> None:
        pass

    async def __aenter__(self) -> _SdkClientThatFails:
        raise RuntimeError("sdk exploded")

    async def __aexit__(self, *_exc_info: object) -> None:
        return None


def _a_missing_anthropic_sdk(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)


def _an_sdk_whose_client_raises_a_non_provider_error(monkeypatch) -> None:
    sdk = SimpleNamespace(
        AsyncAnthropic=_SdkClientThatFails,
        APIError=type("APIError", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "anthropic", sdk)


@pytest.mark.parametrize(
    ("break_the_sdk", "safe_code"),
    [
        (_a_missing_anthropic_sdk, SafeRouteReasonCode.API_HTTP_ERROR),
        (_an_sdk_whose_client_raises_a_non_provider_error, SafeRouteReasonCode.API_PROTOCOL_ERROR),
    ],
    ids=["sdk-not-installed", "sdk-client-raises"],
)
def test_claude_api_turn_fails_with_a_safe_code_when_the_sdk_is_unusable(
    monkeypatch, break_the_sdk, safe_code
) -> None:
    break_the_sdk(monkeypatch)

    def executor(_name: str, _arguments: dict) -> ToolOutcome:
        return ToolOutcome("unused", False)

    async def _drive_turn() -> None:
        async for _event in claude_adapter.stream_claude_api_turn(
            api_key="key",
            system="system",
            model="claude-test",
            messages=[{"role": "user", "content": "hi"}],
            executor=executor,
            tool_schemas=[],
        ):
            pass

    turn = _drive_turn()
    with pytest.raises(ProviderUnavailableError) as failure:
        asyncio.run(turn)

    assert failure.value.reason.code is safe_code


@pytest.mark.parametrize(
    ("provider_error", "safe_code"),
    [
        (CliUnavailableError("gone"), SafeRouteReasonCode.CLI_UNAVAILABLE),
        (CliLoginNotConfiguredError("signed out"), SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED),
        (CliLoginRefusedError("refused"), SafeRouteReasonCode.CLI_AUTH_REJECTED),
        (CliToolSurfaceError("surface"), SafeRouteReasonCode.TOOL_EXECUTION_FAILED),
        (UnavailableError("other"), SafeRouteReasonCode.CLI_PROTOCOL_ERROR),
    ],
    ids=[
        "cli-unavailable",
        "login-not-configured",
        "login-refused",
        "tool-surface",
        "other-api-error",
    ],
)
def test_claude_cli_turn_reports_a_provider_failure_with_its_safe_code(
    monkeypatch, provider_error, safe_code
) -> None:
    async def failing_stream(**_kwargs: object) -> AsyncIterator[StreamEvent]:
        raise provider_error
        yield

    monkeypatch.setattr(claude_adapter, "acall_claude_with_mcp_stream", failing_stream)

    async def _drive_turn() -> None:
        async for _event in claude_adapter.stream_claude_turn(
            user_id="user-1", system="system", model="claude-test", messages=[],
        ):
            pass

    turn = _drive_turn()

    with pytest.raises(ProviderUnavailableError) as failure:
        asyncio.run(turn)

    assert failure.value.reason.code is safe_code


async def _drive_real_claude_turn() -> None:
    async for _event in claude_adapter.stream_claude_turn(
        user_id="user-1", system="system", model="claude-test", messages=[],
    ):
        pass


def _a_credential_that(fault: str, tmp_path) -> None:
    source = current_config().claude_cli_auth_file
    source.unlink()
    if fault == "linked":
        target = tmp_path / "elsewhere.json"
        target.write_text("{}")
        source.symlink_to(target)
    if fault == "oversize":
        source.write_bytes(b"x" * (256 * 1024 + 1))


@pytest.mark.parametrize("fault", ("missing", "linked", "oversize"))
def test_claude_cli_turn_with_an_unusable_credential_reports_it_as_not_signed_in(
    fault: str, tmp_path
) -> None:
    _a_credential_that(fault, tmp_path)

    driving = _drive_real_claude_turn()

    with pytest.raises(ProviderUnavailableError) as failure:
        asyncio.run(driving)

    assert failure.value.reason.code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED
    cause = failure.value.__cause__
    assert type(cause) is CliLoginNotConfiguredError
    assert str(cause) == CLAUDE_CLI_LOGIN_NOT_CONFIGURED_DETAIL
    assert isinstance(cause.__cause__, CliLoginUnusableError)


@contextlib.contextmanager
def _a_working_root_that_was_removed(tmp_path) -> Iterator[Path]:
    root = tmp_path / "removed-root"
    root.mkdir(mode=0o700)
    override_provider_runtime(cli_working_directory_root=root)
    root.rmdir()
    yield root


@contextlib.contextmanager
def _a_credential_copy_that_cannot_be_written(tmp_path) -> Iterator[Path]:
    root = tmp_path / "working-root"
    root.mkdir(mode=0o700)
    override_provider_runtime(cli_working_directory_root=root)
    with a_credential_destination_that_fails("write"):
        yield root


@pytest.mark.parametrize(
    "environment_fault",
    (_a_working_root_that_was_removed, _a_credential_copy_that_cannot_be_written),
    ids=("home-not-made", "credential-copy-not-written"),
)
def test_claude_cli_turn_that_cannot_run_here_reports_the_cli_unavailable(
    tmp_path, environment_fault,
) -> None:
    with environment_fault(tmp_path) as root:
        driving = _drive_real_claude_turn()

        with pytest.raises(ProviderUnavailableError) as failure:
            asyncio.run(driving)

    assert not root.exists() or not any(root.iterdir())
    assert failure.value.reason.code is SafeRouteReasonCode.CLI_UNAVAILABLE
    cause = failure.value.__cause__
    assert type(cause) is CliUnavailableError
    assert str(cause) == CLAUDE_CLI_UNAVAILABLE_DETAIL
    assert isinstance(cause.__cause__, CliEnvironmentUnavailableError)
