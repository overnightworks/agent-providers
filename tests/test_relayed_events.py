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
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import pytest
from bounds import TEST_WAIT_SECONDS
from capability_support import canary_token, capability_carrying
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
    CapabilityReflected,
    McpRelayOwner,
    Relay,
    ToolCallBarrier,
    UnofferedToolCalled,
)

CANARY = canary_token()
TEXT = AssistantTextEvent(text="hi")
FINAL = FinalEvent(text="done")
EDIT_NOTE = CallToolParams(name="edit_note", arguments={})
GROUP = RetainedGroup(pgid=4242, generation=1)


class UpstreamBroke(Exception):
    pass


class RecordingApp:
    """An ``AppTools`` fake that records the names it was called with and answers, at once or
    once ``release`` opened its gate."""

    def __init__(self, answer: str | None = None, *, gated: bool = False) -> None:
        self.called: list[str] = []
        self._answer = answer
        self._gate = asyncio.Event()
        if not gated:
            self._gate.set()

    def release(self) -> None:
        self._gate.set()

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        self.called.append(name)
        await self._gate.wait()
        return ToolOutcome(self._answer or f"ran {name}", is_error=False)


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
    return barrier_class(RUN_ID, app, capability_carrying(CANARY, tools=("edit_note",)))


class Arrivals:
    """Calls arriving at a barrier the way the relay delivers them."""

    def __init__(self, barrier: ToolCallBarrier) -> None:
        self._barrier = barrier
        self._calls: list[asyncio.Task[object]] = []

    def arrive(self, count: int) -> None:
        for request_id in range(len(self._calls) + 1, len(self._calls) + count + 1):
            self._calls.append(asyncio.ensure_future(self._barrier.call(request_id, EDIT_NOTE)))


async def taken(events: RelayedEvents, count: int) -> list[StreamEvent]:
    return [await asyncio.wait_for(anext(events), TEST_WAIT_SECONDS) for _ in range(count)]


def shown(events: list[StreamEvent]) -> list[tuple[str, str]]:
    return [
        (event.type, getattr(event, "tool_use_id", None) or getattr(event, "text", ""))
        for event in events
    ]


LOOP_PASSES_A_HELD_FINAL_IS_WATCHED = 20


class QueryCountingCalls:
    """The barrier as the owner's port, counting how often the merger asked it a question."""

    def __init__(self, barrier: ToolCallBarrier) -> None:
        self.questions = 0
        self._barrier = barrier

    async def next_event(self):
        return await self._barrier.next_event()

    def accept(self, event) -> None:
        self._barrier.accept(event)

    def has_ready_event(self) -> bool:
        self.questions += 1
        return self._barrier.has_ready_event()

    def has_running_forward(self) -> bool:
        self.questions += 1
        return self._barrier.has_running_forward()


async def loop_passes(count: int) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


def drive(scenario: Awaitable[None]) -> None:
    asyncio.run(asyncio.wait_for(scenario, TEST_WAIT_SECONDS * 3))


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
            ("tool_result", "call-1"),
            ("assistant_text", "hi"),
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

        assert (called_before_the_ask, app.called) == ([], ["edit_note"])
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
        assert app.called == ["edit_note"] * 3
        barrier.stop()
        await events.close()

    drive(scenario())


def test_a_final_ready_as_the_one_result_is_queued_waits_for_that_result() -> None:
    async def scenario() -> None:
        app, core = RecordingApp(), ScriptedCore()
        barrier = a_barrier(app)
        Arrivals(barrier).arrive(1)
        events = RelayedEvents(core.stream(), barrier)
        yielded = await taken(events, 1)

        next_event = asyncio.ensure_future(anext(events))
        core.feed(FINAL)
        yielded.append(await asyncio.wait_for(next_event, TEST_WAIT_SECONDS))
        yielded.extend(await taken(events, 1))

        assert shown(yielded) == [
            ("tool_call", "call-1"),
            ("tool_result", "call-1"),
            ("final", "done"),
        ]
        barrier.stop()
        await events.close()

    drive(scenario())


def after_hops(hops: int, action: Callable[[], object]) -> None:
    """Run ``action`` from a loop callback ``hops`` loop passes from now."""
    loop = asyncio.get_running_loop()
    if hops == 0:
        loop.call_soon(action)
    else:
        loop.call_soon(after_hops, hops - 1, action)


async def delivered_before_the_final(events: RelayedEvents) -> list[StreamEvent]:
    """Asked for in the caller's own coroutine, so what the barrier holds when the final is
    returned is what it holds when the caller goes on."""
    delivered: list[StreamEvent] = []
    while not isinstance(event := await anext(events), FinalEvent):
        delivered.append(event)
    return delivered


async def the_calls_taken_then_the_final_fed_and_the_forwards_released_after_hops(
    app: RecordingApp,
    core: ScriptedCore,
    events: RelayedEvents,
    count: int,
    feed_and_release_hops: tuple[int, int],
) -> None:
    feed_hops, release_hops = feed_and_release_hops
    await taken(events, count)
    after_hops(feed_hops, lambda: core.feed(FINAL))
    after_hops(release_hops, app.release)


@pytest.mark.parametrize("hops", [(0, 1), (1, 1), (2, 2), (3, 3)])
@pytest.mark.parametrize("count", [1, 2, 3])
def test_a_final_is_handed_on_only_after_the_results_of_the_forwards_it_would_overtake(
    count: int, hops: tuple[int, int]
) -> None:
    async def scenario() -> None:
        app, core = RecordingApp(gated=True), ScriptedCore()
        barrier = a_barrier(app)
        Arrivals(barrier).arrive(count)
        events = RelayedEvents(core.stream(), barrier)
        await the_calls_taken_then_the_final_fed_and_the_forwards_released_after_hops(
            app, core, events, count, hops
        )

        delivered = await delivered_before_the_final(events)

        assert (shown(delivered), barrier.has_ready_event(), barrier.has_running_forward()) == (
            [("tool_result", f"call-{number}") for number in range(1, count + 1)],
            False,
            False,
        )
        barrier.stop()
        await events.close()

    drive(scenario())


@pytest.mark.parametrize("hops", [(1, 1), (2, 2), (3, 3)])
def test_a_final_is_not_handed_on_while_a_reflected_result_is_pending(
    hops: tuple[int, int],
) -> None:
    async def scenario() -> None:
        app, core = RecordingApp(CANARY, gated=True), ScriptedCore()
        barrier = a_barrier(app)
        Arrivals(barrier).arrive(1)
        events = RelayedEvents(core.stream(), barrier)
        await the_calls_taken_then_the_final_fed_and_the_forwards_released_after_hops(
            app, core, events, 1, hops
        )

        with pytest.raises(CapabilityReflected):
            await delivered_before_the_final(events)

        barrier.stop()
        await events.close()

    drive(scenario())


@pytest.mark.parametrize("how", ["notification", "request-cancelled"])
def test_a_final_is_handed_on_when_the_one_ready_call_is_given_up_before_the_reader_runs(
    how: str,
) -> None:
    async def scenario() -> None:
        app, core = RecordingApp(), ScriptedCore()
        barrier = a_barrier(app)
        events = RelayedEvents(core.stream(), barrier)
        arrived: list[asyncio.Future[object]] = []

        def give_up() -> None:
            if how == "notification":
                barrier.cancel(1)
            else:
                arrived[0].cancel()

        def the_call_arrives_and_is_given_up_later() -> None:
            after_hops(1, give_up)
            arrived.append(asyncio.ensure_future(barrier.call(1, EDIT_NOTE)))

        def the_final_is_fed_and_the_call_arrives_one_pass_later() -> None:
            core.feed(FINAL)
            after_hops(0, the_call_arrives_and_is_given_up_later)

        after_hops(3, the_final_is_fed_and_the_call_arrives_one_pass_later)

        assert await anext(events) == FINAL
        assert app.called == []
        barrier.stop()
        await events.close()

    drive(scenario())


def test_a_final_waits_for_the_result_of_a_forward_still_running() -> None:
    async def scenario() -> None:
        app, core = RecordingApp(gated=True), ScriptedCore()
        barrier = a_barrier(app)
        Arrivals(barrier).arrive(1)
        events = RelayedEvents(core.stream(), barrier)
        await taken(events, 1)
        core.feed(FINAL)
        next_event = asyncio.ensure_future(anext(events))
        await yield_until(lambda: app.called == ["edit_note"])

        await loop_passes(LOOP_PASSES_A_HELD_FINAL_IS_WATCHED)

        assert not next_event.done()
        app.release()
        yielded = [await asyncio.wait_for(next_event, TEST_WAIT_SECONDS), *await taken(events, 1)]

        assert shown(yielded) == [("tool_result", "call-1"), ("final", "done")]
        barrier.stop()
        await events.close()

    drive(scenario())


def test_a_held_final_waits_on_the_barrier_and_does_not_spin() -> None:
    async def scenario() -> None:
        app, core = RecordingApp(gated=True), ScriptedCore()
        barrier = a_barrier(app)
        Arrivals(barrier).arrive(1)
        calls = QueryCountingCalls(barrier)
        events = RelayedEvents(core.stream(), calls)
        await taken(events, 1)
        core.feed(FINAL)
        held = asyncio.ensure_future(anext(events))
        await yield_until(lambda: app.called == ["edit_note"])

        await loop_passes(LOOP_PASSES_A_HELD_FINAL_IS_WATCHED)
        questions_after_a_few_passes = calls.questions
        await loop_passes(2 * LOOP_PASSES_A_HELD_FINAL_IS_WATCHED)

        assert (held.done(), calls.questions) == (False, questions_after_a_few_passes)
        app.release()
        await asyncio.wait_for(held, TEST_WAIT_SECONDS)
        barrier.stop()
        await events.close()

    drive(scenario())


def test_a_text_event_is_not_held_by_a_forward_still_running() -> None:
    async def scenario() -> None:
        app, core = RecordingApp(gated=True), ScriptedCore()
        barrier = a_barrier(app)
        Arrivals(barrier).arrive(1)
        events = RelayedEvents(core.stream(), barrier)
        await taken(events, 1)
        core.feed(TEXT)

        assert await taken(events, 1) == [TEXT]
        assert barrier.has_running_forward()
        app.release()
        barrier.stop()
        await events.close()

    drive(scenario())


@pytest.mark.parametrize("ending", ["close", "barrier-failure"])
def test_a_held_final_is_released_by_the_close_and_by_a_barrier_failure(ending: str) -> None:
    async def scenario() -> None:
        app, core = RecordingApp(gated=True), ScriptedCore()
        barrier = a_barrier(app)
        Arrivals(barrier).arrive(1)
        events = RelayedEvents(core.stream(), barrier)
        await taken(events, 1)
        core.feed(FINAL)
        held = asyncio.ensure_future(anext(events))
        await yield_until(lambda: app.called == ["edit_note"])
        await loop_passes(LOOP_PASSES_A_HELD_FINAL_IS_WATCHED)
        assert not held.done()

        if ending == "close":
            held.cancel()
            await events.close()
            with pytest.raises(asyncio.CancelledError):
                await held
        else:
            barrier.reflected()
            with pytest.raises(CapabilityReflected):
                await asyncio.wait_for(held, TEST_WAIT_SECONDS)
            await events.close()

        barrier.abort_forwards()
        barrier.stop()
        await barrier.settle()

    drive(scenario())


def test_the_clis_own_tool_lines_are_dropped_from_the_merged_stream() -> None:
    async def scenario() -> None:
        core = ScriptedCore()
        core.feed(
            ToolCallEvent(tool_use_id="toolu_1", name="mcp__relay__edit_note", input={}),
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
        await asyncio.sleep(0)
        await events.close()
        del asking
        gc.collect()

        assert (failures_logged(caplog), unretrieved) == (logged, [])

    caplog.set_level(logging.ERROR)
    drive(scenario())


@asynccontextmanager
async def real_tools(
    barrier_class: type[ToolCallBarrier] = ToolCallBarrier, app: RecordingApp | None = None
) -> AsyncIterator[RunTools]:
    capability = capability_carrying(CANARY, tools=("edit_note",))
    barrier = barrier_class(RUN_ID, app or RecordingApp(), capability)
    offered = [ToolDefinition.model_validate({"name": "edit_note", "inputSchema": {}})]
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
        alice_admitted_run(uuid.uuid4()), capability=capability_carrying(CANARY, ("edit_note",))
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
            await asyncio.wait_for(core.holding.wait(), TEST_WAIT_SECONDS)

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


async def provoke_unoffered(tools: RunTools, events: AsyncGenerator[StreamEvent, None]) -> None:
    tools.calls.unoffered()


async def provoke_reflected(tools: RunTools, events: AsyncGenerator[StreamEvent, None]) -> None:
    Arrivals(tools.calls).arrive(1)
    await asyncio.wait_for(anext(events), TEST_WAIT_SECONDS)


@pytest.mark.parametrize(
    ("failure", "provoke"),
    [(UnofferedToolCalled, provoke_unoffered), (CapabilityReflected, provoke_reflected)],
    ids=["unoffered", "reflected"],
)
def test_a_barrier_failure_reaches_the_service_unchanged(
    monkeypatch: pytest.MonkeyPatch, failure, provoke
) -> None:
    async def scenario() -> None:
        core = ScriptedCore()
        async with real_tools(app=RecordingApp(answer=CANARY)) as tools:
            events, _port = a_runner_behind(monkeypatch, core, tools)
            await provoke(tools, events)

            next_event = asyncio.ensure_future(anext(events))

            with pytest.raises(failure):
                await asyncio.wait_for(next_event, TEST_WAIT_SECONDS)

    drive(scenario())
