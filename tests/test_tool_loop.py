"""Contract tests for the transport-independent co-writer tool loop."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from typing import Any

import pytest
from provider_test_support import A_CANARY, formatted_log_text
from pydantic import SecretStr

from agent_providers import tool_loop
from agent_providers.errors import TextToolCallRejectedError
from agent_providers.events import (
    AssistantTextEvent,
    FinalEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_providers.text_tool_protocol import TextToolRejection, TextToolRejectionRule
from agent_providers.tool_loop import (
    FinalText,
    InitialTurn,
    TextDelta,
    ToolCall,
    ToolCallBatch,
    ToolLoopLimitError,
    ToolOutcome,
    ToolResultBatch,
    TransportResponse,
    stream_tool_loop,
)

A_TOOL_FAILURE_MESSAGE = "Co-Writer tool failed."
A_CORRELATION_ID = "job-42"


class _FakeTransport:
    def __init__(self, responses: list[list[TransportResponse | Exception]]) -> None:
        self._responses = responses
        self.messages: list[InitialTurn | ToolResultBatch] = []
        self.closed = False

    async def stream(
        self, message: InitialTurn | ToolResultBatch,
    ) -> AsyncIterator[TransportResponse]:
        self.messages.append(message)
        for response in self._responses.pop(0):
            if isinstance(response, Exception):
                raise response
            yield response

    async def aclose(self) -> None:
        self.closed = True


async def _events(
    transport: _FakeTransport,
    executor,
    correlation_id: str | None = None,
) -> list[object]:
    return [
        event
        async for event in stream_tool_loop(
            provider="grok",
            route="api",
            system="system",
            messages=[{"role": "user", "content": "hello"}],
            transport=transport,
            executor=executor,
            tool_failure_message=A_TOOL_FAILURE_MESSAGE,
            correlation_id=correlation_id,
        )
    ]


def test_passes_the_complete_tool_result_batch_to_the_transport() -> None:
    first = ToolCallBatch((
        ToolCall("call-1", "first", {"position": 1}),
        ToolCall("call-2", "second", {"position": 2}),
    ))
    transport = _FakeTransport([[TextDelta("draft "), first], [FinalText("done")]])
    executed: list[str] = []

    def executor(name: str, arguments: dict[str, Any]) -> ToolOutcome:
        executed.append(name)
        return ToolOutcome(f"{name}:{arguments['position']}", False)

    events = asyncio.run(_events(transport, executor))

    assert executed == ["first", "second"]

    assert transport.messages == [
        InitialTurn("system", [{"role": "user", "content": "hello"}]),
        ToolResultBatch((
            tool_loop.ToolResult("call-1", "first:1", False),
            tool_loop.ToolResult("call-2", "second:2", False),
        )),
    ]
    assert events == [
        AssistantTextEvent(text="draft "),
        ToolCallEvent(tool_use_id="call-1", name="first", input={"position": 1}),
        ToolResultEvent(tool_use_id="call-1", content="first:1", is_error=False),
        ToolCallEvent(tool_use_id="call-2", name="second", input={"position": 2}),
        ToolResultEvent(tool_use_id="call-2", content="second:2", is_error=False),
        AssistantTextEvent(text="done"),
        FinalEvent(text="draft done"),
    ]
    assert transport.closed


def test_allows_eight_tool_rounds_then_one_final_response() -> None:
    calls = [
        [ToolCallBatch((ToolCall(f"call-{index}", "read", {}),))]
        for index in range(tool_loop.MAX_TOOL_ROUNDS)
    ]
    transport = _FakeTransport([*calls, [FinalText("complete")]])

    events = asyncio.run(
        _events(transport, lambda _name, _arguments: ToolOutcome("ok", False)),
    )

    assert sum(isinstance(event, ToolCallEvent) for event in events) == 8
    assert events[-1] == FinalEvent(text="complete")


def test_rejects_a_ninth_tool_round_without_executing_it(monkeypatch) -> None:
    monkeypatch.setattr(tool_loop, "MAX_TOOL_ROUNDS", 0)
    transport = _FakeTransport([[
        ToolCallBatch((ToolCall("call-9", "read", {}),)),
    ]])
    executed = False

    def executor(_name: str, _arguments: dict[str, Any]) -> ToolOutcome:
        nonlocal executed
        executed = True
        return ToolOutcome("unreachable", False)

    events = _events(transport, executor)
    with pytest.raises(ToolLoopLimitError):
        asyncio.run(events)

    assert not executed
    assert transport.closed


def test_executor_failure_returns_a_named_error_to_the_model(caplog) -> None:
    transport = _FakeTransport([
        [ToolCallBatch((ToolCall("call-1", "write", {"lyrics": "secret"}),))],
        [FinalText()],
    ])

    def executor(_name: str, _arguments: dict[str, Any]) -> ToolOutcome:
        raise RuntimeError("secret")

    caplog.set_level("INFO", logger="agent_providers.tool_loop")
    events = asyncio.run(_events(transport, executor))

    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text
    assert [
        record.levelname
        for record in caplog.records
        if "raised" in record.getMessage()
    ] == ["ERROR"]

    assert events == [
        ToolCallEvent(tool_use_id="call-1", name="write", input={"lyrics": "secret"}),
        ToolResultEvent(
            tool_use_id="call-1", content="Co-Writer tool failed.", is_error=True,
        ),
        FinalEvent(text=""),
    ]
    assert transport.messages[1] == ToolResultBatch((
        tool_loop.ToolResult("call-1", "Co-Writer tool failed.", True),
    ))


def test_aclose_stops_the_active_transport_without_a_follow_up_round() -> None:
    class BlockingTransport(_FakeTransport):
        async def stream(
            self, message: InitialTurn | ToolResultBatch,
        ) -> AsyncIterator[TransportResponse]:
            self.messages.append(message)
            yield TextDelta("partial")
            await asyncio.Future()

    transport = BlockingTransport([])

    async def close_after_first_delta() -> None:
        turn = stream_tool_loop(
            provider="grok",
            route="cli",
            system="system",
            messages=[],
            transport=transport,
            executor=lambda _name, _arguments: ToolOutcome("unused", False),
            tool_failure_message=A_TOOL_FAILURE_MESSAGE,
        )
        assert await anext(turn) == AssistantTextEvent(text="partial")
        await turn.aclose()

    asyncio.run(close_after_first_delta())

    assert transport.closed
    assert len(transport.messages) == 1


def test_logs_never_include_tool_input_or_result(caplog) -> None:
    lyrics = "private lyrics"
    song_id = "song-private"
    call_json = '{"lyrics":"private lyrics","song_id":"song-private"}'
    transport = _FakeTransport([
        [ToolCallBatch((ToolCall("safe-call", "update_song_lyrics", {
            "lyrics": lyrics,
            "song_id": song_id,
        }),))],
        [FinalText()],
    ])
    caplog.set_level("INFO", logger="agent_providers.tool_loop")

    asyncio.run(
        _events(transport, lambda _name, _arguments: ToolOutcome(call_json, True)),
    )

    assert "provider=grok" in caplog.text
    assert "route=api" in caplog.text
    assert "tool=update_song_lyrics" in caplog.text
    for forbidden in (lyrics, song_id, call_json):
        assert forbidden not in caplog.text


def test_every_event_of_a_turn_carries_the_hosts_correlation_id() -> None:
    transport = _FakeTransport([
        [TextDelta("draft "), ToolCallBatch((ToolCall("call-1", "read", {}),))],
        [FinalText("done")],
    ])

    events = asyncio.run(_events(
        transport,
        lambda _name, _arguments: ToolOutcome("ok", False),
        correlation_id=A_CORRELATION_ID,
    ))

    assert [event.correlation_id for event in events] == [A_CORRELATION_ID] * len(events)


def test_the_host_names_the_sentence_a_crashed_tool_reports() -> None:
    """The failure text belongs to the host's error vocabulary, not to this loop."""
    transport = _FakeTransport([
        [ToolCallBatch((ToolCall("call-1", "write", {}),))],
        [FinalText()],
    ])
    host_message = "This workshop could not run that tool."

    def executor(_name: str, _arguments: dict[str, Any]) -> ToolOutcome:
        raise RuntimeError("secret")

    async def turn() -> list[object]:
        return [
            event
            async for event in stream_tool_loop(
                provider="grok",
                route="api",
                system="system",
                messages=[],
                transport=transport,
                executor=executor,
                tool_failure_message=host_message,
            )
        ]

    events = asyncio.run(turn())

    assert events[1] == ToolResultEvent(
        tool_use_id="call-1", content=host_message, is_error=True,
    )


def test_a_refused_second_response_writes_one_line_with_its_round_and_correlation_id(caplog):
    rejection = TextToolRejection(
        rule=TextToolRejectionRule.UNKNOWN_TOOL,
        offset=23,
        length=90,
        excerpt_start=0,
        excerpt=SecretStr(A_CANARY),
    )
    refused = TextToolCallRejectedError("grok", "cli", rejection)
    transport = _FakeTransport([
        [ToolCallBatch((ToolCall("call-1", "read", {}),))],
        [refused],
    ])
    caplog.set_level(logging.DEBUG)

    turn = _events(transport, lambda _name, _arguments: ToolOutcome("ok", False), A_CORRELATION_ID)
    with pytest.raises(TextToolCallRejectedError) as raised:
        asyncio.run(turn)

    assert raised.value is refused
    assert transport.closed
    [line] = [r for r in caplog.records if "rejected" in r.getMessage()]
    assert (line.name, line.levelno, line.exc_info) == (
        "agent_providers.tool_loop", logging.WARNING, None,
    )
    assert line.getMessage() == (
        "Tool call rejected provider=grok route=api round=1 correlation_id=job-42 "
        "code=text_tool_response_invalid rule=unknown_tool offset=23 length=90"
    )
    assert A_CANARY not in formatted_log_text(caplog.records)


def test_a_failed_tool_reaches_the_model_as_its_own_error_outcome(caplog) -> None:
    transport = _FakeTransport([
        [ToolCallBatch((ToolCall("call-1", "read", {}),))],
        [FinalText()],
    ])
    caplog.set_level(logging.DEBUG)

    events = asyncio.run(
        _events(transport, lambda _name, _arguments: ToolOutcome("not found", True)),
    )

    assert events == [
        ToolCallEvent(tool_use_id="call-1", name="read", input={}),
        ToolResultEvent(tool_use_id="call-1", content="not found", is_error=True),
        FinalEvent(text=""),
    ]
    assert transport.messages[1] == ToolResultBatch((
        tool_loop.ToolResult("call-1", "not found", True),
    ))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_the_executor_runs_on_the_thread_and_loop_driving_the_turn() -> None:
    transport = _FakeTransport([
        [ToolCallBatch((ToolCall("call-1", "read", {}),))],
        [FinalText()],
    ])
    seen: list[tuple[int, asyncio.AbstractEventLoop]] = []

    def executor(_name: str, _arguments: dict[str, Any]) -> ToolOutcome:
        seen.append((threading.get_ident(), asyncio.get_running_loop()))
        return ToolOutcome("ok", False)

    async def turn() -> tuple[int, asyncio.AbstractEventLoop]:
        await _events(transport, executor)
        return threading.get_ident(), asyncio.get_running_loop()

    assert asyncio.run(turn()) == seen[0]


@pytest.mark.parametrize("is_error", [True, False])
def test_the_per_call_record_carries_the_documented_fields(caplog, is_error) -> None:
    transport = _FakeTransport([
        [ToolCallBatch((ToolCall("call-7", "read", {}),))],
        [FinalText()],
    ])
    caplog.set_level("INFO", logger="agent_providers.tool_loop")

    asyncio.run(_events(
        transport,
        lambda _name, _arguments: ToolOutcome("content", is_error),
        A_CORRELATION_ID,
    ))

    [record] = [r for r in caplog.records if "call_id=" in r.getMessage()]
    fields = dict(part.split("=", 1) for part in record.getMessage().split()[2:])
    assert fields | {"duration_ms": "0"} == {
        "provider": "grok",
        "route": "api",
        "round": "1",
        "call_id": "call-7",
        "correlation_id": A_CORRELATION_ID,
        "duration_ms": "0",
        "tool": "read",
        "is_error": str(is_error),
    }
    assert fields["duration_ms"].isdigit()
