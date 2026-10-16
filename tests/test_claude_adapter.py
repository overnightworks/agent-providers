"""Behavior of the Claude adapters: cancellation and API turn failures."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from agent_providers.claude import adapter as claude_adapter
from agent_providers.errors import ProviderUnavailableError, SafeRouteReasonCode
from agent_providers.events import AssistantTextEvent, StreamEvent
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
