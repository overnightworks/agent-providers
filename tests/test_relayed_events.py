"""The merged event source of a tool run: the barrier's events go first, and only once the
service asked for the next, and a containment loss in the cleanup always leaves the runner.

The core is a scripted generator, the barrier and the relay are the real ones, and the
cleanup cases run behind ``CliRunner.run`` with the real ``RunTools``.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import ssl
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import pytest
from capability_support import canary_token, capability_carrying
from mcp_app_fake import WAIT_SECONDS
from relay_support import RUN_ID, yield_until
from service_support import alice_admitted_run, failure_lines, service_config

import agent_service.cli_runner as cli_runner_module
from agent_providers.errors import ContainmentLost, RetainedGroup
from agent_providers.events import (
    AssistantTextEvent,
    FinalEvent,
    StreamEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_service.cli_runner import CliRunner
from agent_service.mcp_wire import CallToolParams, ToolDefinition, ToolOutcome
from agent_service.relayed_events import RelayedEvents
from agent_service.run_liveness import LoopClock
from agent_service.run_tools import RunTools
from agent_service.tool_relay import (
    McpRelayOwner,
    Relay,
    ToolCallBarrier,
    UnofferedToolCalled,
)

CANARY = canary_token()
TEXT = AssistantTextEvent(text="hi")
FINAL = FinalEvent(text="done")
EDIT_SONG = CallToolParams(name="edit_song", arguments={})
GROUP = RetainedGroup(pgid=4242, generation=1)


class UpstreamBroke(Exception):
    pass


class RecordingApp:
    """An ``AppTools`` fake that answers at once and records the names it was called with."""

    def __init__(self) -> None:
        self.called: list[str] = []

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        self.called.append(name)
        return ToolOutcome(f"ran {name}", is_error=False)


class ScriptedCore:
    """A core generator fed by the test, with the ends the cleanup cases need.

    ``lost_when_cancelled`` is raised by a step cancelled while the core waits;
    ``lost_when_closed`` by the close of a core suspended at an event; ``hold_close`` keeps that
    close open until ``release_close`` is set.
    """

    def __init__(
        self,
        *,
        lost_when_cancelled: ContainmentLost | None = None,
        lost_when_closed: ContainmentLost | None = None,
        hold_close: bool = False,
    ) -> None:
        self._feed: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        self._lost_when_cancelled = lost_when_cancelled
        self._lost_when_closed = lost_when_closed
        self._hold_close = hold_close
        self.waiting = asyncio.Event()
        self.holding = asyncio.Event()
        self.release_close = asyncio.Event()
        self.closes = 0

    def feed(self, *events: StreamEvent) -> None:
        for event in events:
            self._feed.put_nowait(event)

    def end(self) -> None:
        self._feed.put_nowait(None)

    async def stream(self) -> AsyncGenerator[StreamEvent, None]:
        try:
            while True:
                self.waiting.set()
                event = await self._feed.get()
                self.waiting.clear()
                if event is None:
                    return
                yield event
        except asyncio.CancelledError:
            if self._lost_when_cancelled is not None:
                raise self._lost_when_cancelled from None
            raise
        except GeneratorExit:
            self.closes += 1
            if self._hold_close:
                self.holding.set()
                await self.release_close.wait()
            if self._lost_when_closed is not None:
                raise self._lost_when_closed from None
            raise


class BarrierBroken(ToolCallBarrier):
    """A barrier whose pending ``next_event`` fails with its own class when it is cancelled."""

    async def next_event(self) -> ToolCallEvent | ToolResultEvent:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise UpstreamBroke from None
        raise AssertionError("unreachable")


def a_barrier(app: RecordingApp, barrier_class: type[ToolCallBarrier] = ToolCallBarrier):
    return barrier_class(RUN_ID, app, capability_carrying(CANARY, tools=("edit_song",)))


class Arrivals:
    """Calls arriving at a barrier the way the relay delivers them."""

    def __init__(self, barrier: ToolCallBarrier) -> None:
        self._barrier = barrier
        self._calls: list[asyncio.Task[object]] = []

    def arrive(self, count: int) -> None:
        for request_id in range(len(self._calls) + 1, len(self._calls) + count + 1):
            self._calls.append(asyncio.ensure_future(self._barrier.call(request_id, EDIT_SONG)))


async def taken(events: RelayedEvents, count: int) -> list[StreamEvent]:
    return [await asyncio.wait_for(anext(events), WAIT_SECONDS) for _ in range(count)]


def shown(events: list[StreamEvent]) -> list[tuple[str, str]]:
    return [
        (event.type, getattr(event, "tool_use_id", None) or getattr(event, "text", ""))
        for event in events
    ]


def drive(scenario: Awaitable[None]) -> None:
    asyncio.run(asyncio.wait_for(scenario, WAIT_SECONDS * 3))


def test_a_ready_relay_event_goes_before_a_ready_core_event_and_none_is_lost() -> None:
    async def scenario() -> None:
        app, core = RecordingApp(), ScriptedCore()
        barrier = a_barrier(app)
        Arrivals(barrier).arrive(1)
        core.feed(TEXT)
        events = RelayedEvents(core.stream(), barrier)

        yielded = await taken(events, 3)

        assert shown(yielded) == [
            ("tool_call", "call-1"),
            ("assistant_text", "hi"),
            ("tool_result", "call-1"),
        ]
        barrier.stop()
        await events.close()

    drive(scenario())


def test_a_relay_event_is_accepted_only_when_the_service_asks_for_the_next() -> None:
    async def scenario() -> None:
        app, core = RecordingApp(), ScriptedCore()
        barrier = a_barrier(app)
        Arrivals(barrier).arrive(1)
        events = RelayedEvents(core.stream(), barrier)

        await taken(events, 1)
        await barrier.settle()
        called_before_the_ask = list(app.called)
        await taken(events, 1)

        assert (called_before_the_ask, app.called) == ([], ["edit_song"])
        barrier.stop()
        await events.close()

    drive(scenario())


def test_a_relay_event_is_never_accepted_when_the_run_is_closed_at_the_yield() -> None:
    async def scenario() -> None:
        app, core = RecordingApp(), ScriptedCore()
        barrier = a_barrier(app)
        Arrivals(barrier).arrive(1)
        events = RelayedEvents(core.stream(), barrier)

        await taken(events, 1)
        barrier.stop()
        await events.close()
        await barrier.settle()

        assert app.called == []

    drive(scenario())


def test_a_ready_final_waits_for_every_queued_barrier_event() -> None:
    async def scenario() -> None:
        app, core = RecordingApp(), ScriptedCore()
        barrier = a_barrier(app)
        Arrivals(barrier).arrive(3)
        core.feed(FINAL)
        events = RelayedEvents(core.stream(), barrier)

        yielded = await taken(events, 7)

        assert shown(yielded) == [
            *[("tool_call", f"call-{number}") for number in (1, 2, 3)],
            *[("tool_result", f"call-{number}") for number in (1, 2, 3)],
            ("final", "done"),
        ]
        assert app.called == ["edit_song"] * 3
        barrier.stop()
        await events.close()

    drive(scenario())


def test_the_clis_own_tool_lines_are_dropped_from_the_merged_stream() -> None:
    async def scenario() -> None:
        core = ScriptedCore()
        core.feed(
            ToolCallEvent(tool_use_id="toolu_1", name="mcp__relay__edit_song", input={}),
            ToolResultEvent(tool_use_id="toolu_1", content="ok"),
            TEXT,
        )
        core.end()
        events = RelayedEvents(core.stream(), None)

        assert shown([event async for event in events]) == [("assistant_text", "hi")]

    drive(scenario())


def failures_logged(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [kind for _event, kind, _function in failure_lines(caplog)]


@pytest.mark.parametrize(
    ("barrier_class", "logged"),
    [
        pytest.param(ToolCallBarrier, [], id="its-own-stop-is-not-reported"),
        pytest.param(BarrierBroken, ["UpstreamBroke"], id="any-other-class-is-logged-once"),
    ],
)
def test_a_barrier_step_pending_at_the_close_leaves_no_unretrieved_failure(
    caplog: pytest.LogCaptureFixture, barrier_class: type[ToolCallBarrier], logged: list[str]
) -> None:
    async def scenario() -> None:
        unretrieved: list[Mapping[str, Any]] = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: unretrieved.append(ctx))
        core = ScriptedCore()
        barrier = a_barrier(RecordingApp(), barrier_class)
        events = RelayedEvents(core.stream(), barrier)
        asking = asyncio.ensure_future(anext(events))
        await core.waiting.wait()
        await asyncio.sleep(0)
        asking.cancel()
        await asyncio.gather(asking, return_exceptions=True)

        barrier.stop()
        await events.close()
        del asking
        gc.collect()

        assert (failures_logged(caplog), unretrieved) == (logged, [])

    caplog.set_level(logging.ERROR)
    drive(scenario())


@asynccontextmanager
async def real_tools(
    barrier_class: type[ToolCallBarrier] = ToolCallBarrier,
) -> AsyncIterator[RunTools]:
    capability = capability_carrying(CANARY, tools=("edit_song",))
    barrier = barrier_class(RUN_ID, RecordingApp(), capability)
    offered = [ToolDefinition.model_validate({"name": "edit_song", "inputSchema": {}})]
    relay = await Relay.start(RUN_ID, McpRelayOwner(capability, offered, barrier), LoopClock())
    yield RunTools(RUN_ID, LoopClock(), relay, barrier, AsyncExitStack())


def a_runner_behind(
    monkeypatch: pytest.MonkeyPatch, core: ScriptedCore, tools: RunTools
) -> tuple[AsyncGenerator[StreamEvent, None], int]:
    async def open_the_scripted_tools(*_arguments: object) -> RunTools:
        return tools

    monkeypatch.setattr(cli_runner_module.RunTools, "open", open_the_scripted_tools)
    monkeypatch.setattr(cli_runner_module, "astream_cli_run", lambda _root, _adapter: core.stream())
    runner = CliRunner(
        service_config(), tls_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), clock=LoopClock()
    )
    admitted = replace(
        alice_admitted_run(uuid.uuid4()), capability=capability_carrying(CANARY, ("edit_song",))
    )
    return runner.run(admitted), urlsplit(tools.access.url).port or 0


@pytest.mark.parametrize(
    ("core_kwargs", "barrier_class", "ends_cancelled", "logged"),
    [
        pytest.param(
            {"lost_when_cancelled": ContainmentLost(GROUP)}, ToolCallBarrier, True, [], id="step"
        ),
        pytest.param(
            {"lost_when_closed": ContainmentLost(GROUP)}, ToolCallBarrier, False, [], id="close"
        ),
        pytest.param(
            {"lost_when_closed": ContainmentLost(GROUP)},
            BarrierBroken,
            False,
            ["UpstreamBroke"],
            id="competing-barrier-failure",
        ),
    ],
)
def test_a_containment_loss_survives_the_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    core_kwargs: dict[str, ContainmentLost],
    barrier_class: type[ToolCallBarrier],
    ends_cancelled: bool,
    logged: list[str],
) -> None:
    async def scenario() -> None:
        core = ScriptedCore(**core_kwargs)
        async with real_tools(barrier_class) as tools:
            events, _port = a_runner_behind(monkeypatch, core, tools)
            if not ends_cancelled:
                core.feed(TEXT)
                await anext(events)
                await asyncio.sleep(0)
            asking = asyncio.ensure_future(anext(events)) if ends_cancelled else None
            if asking is not None:
                await core.waiting.wait()
                await asyncio.sleep(0)
                asking.cancel()
            closing = asking if asking is not None else asyncio.ensure_future(events.aclose())

            with pytest.raises(ContainmentLost) as lost:
                await closing

            assert (lost.value.group, failures_logged(caplog)) == (GROUP, logged)

    caplog.set_level(logging.ERROR)
    drive(scenario())


def test_a_cancellation_during_a_held_cleanup_waits_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        core = ScriptedCore(hold_close=True)
        async with real_tools() as tools:
            events, port = a_runner_behind(monkeypatch, core, tools)
            core.feed(TEXT)
            await anext(events)
            closing = asyncio.ensure_future(events.aclose())
            await asyncio.wait_for(core.holding.wait(), WAIT_SECONDS)

            closing.cancel()
            await yield_until(lambda: closing.cancelling() > 0)
            await asyncio.sleep(0)
            still_held = not closing.done()
            core.release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await closing

            with pytest.raises(ConnectionRefusedError):
                await asyncio.open_connection("127.0.0.1", port)
            assert (still_held, core.closes) == (True, 1)

    drive(scenario())


def test_a_barrier_failure_reaches_the_service_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        core = ScriptedCore()
        async with real_tools() as tools:
            events, _port = a_runner_behind(monkeypatch, core, tools)
            tools.calls.unoffered()

            with pytest.raises(UnofferedToolCalled):
                await anext(events)

    drive(scenario())
