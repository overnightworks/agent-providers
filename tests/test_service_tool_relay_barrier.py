"""The execution barrier: only a call the service accepted is forwarded to the app.

The CLI side is the real ``Relay`` with ``McpRelayOwner`` over raw sockets. The runner side is
``BarrierRunner``, the fake of the CLI runner's loop, under the real ``run_frames``.
"""

import asyncio
import gc
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from typing import Any

import pytest
from capability_support import canary_token, capability_carrying
from mcp_app_fake import (
    CERTIFICATE_HOST,
    WAIT_SECONDS,
    McpAppFake,
    ScriptedApp,
    serving,
)
from relay_support import (
    RUN_ID,
    Delivery,
    RawClient,
    close_bounded,
    json_rpc,
    port_of,
    post_message,
    yield_until,
)
from service_support import (
    SONGMAKER,
    alice_admitted_run,
    decoded_events,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
)

from agent_client.tools import ToolEffect
from agent_client.wire import Failed, FailureCode, RunStarted, ToolCall
from agent_providers.events import ToolCallEvent
from agent_service.app_tools import open_app_tools
from agent_service.config import RunConfig
from agent_service.mcp_wire import (
    CallToolParams,
    Reply,
    RequestId,
    ToolDefinition,
    ToolOutcome,
)
from agent_service.run_events import RUNNER_FAILED_MESSAGE, TOO_MANY_TOOL_CALLS_MESSAGE, run_frames
from agent_service.run_liveness import LoopClock
from agent_service.run_records import RunRecords
from agent_service.runners import AdmittedRun
from agent_service.tool_relay import (
    MAX_PENDING_TOOL_CALLS,
    MAX_RELAY_CONNECTIONS,
    AppTools,
    McpRelayOwner,
    Relay,
    ToolCallBarrier,
    ToolCallsMisused,
)

SUPPORTED = "2025-11-25"
CLAIMED = ("edit_song", "read_song", "list_songs")
OFFERED = ("edit_song", "read_song", "delete_song")
REFUSAL = {"code": -32602, "message": "Invalid params"}
UPSTREAM_TEXT = "ran edit_song"
SETTLE_TURNS = 3


class UpstreamBroke(Exception):
    pass


class GatedApp:
    """An ``AppTools`` fake that records each call, holds it until released, then answers."""

    def __init__(self, failure: BaseException | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.received: list[Mapping[str, Any]] = []
        self._failure = failure
        self._gate = asyncio.Event()

    def forget_failure(self) -> None:
        self._failure = None

    def release(self) -> None:
        self._gate.set()

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        self.calls.append((name, dict(arguments)))
        self.received.append(arguments)
        await self._gate.wait()
        if self._failure is not None:
            raise self._failure
        return ToolOutcome(f"ran {name}", is_error=False)

    async def forwarded(self, count: int) -> None:
        await yield_until(lambda: len(self.calls) == count)


class EnteringCalls:
    """The barrier as the owner's port, recording which ids entered ``call`` in which order."""

    def __init__(self, barrier: ToolCallBarrier) -> None:
        self.entered: list[RequestId] = []
        self._barrier = barrier

    async def call(self, request_id: RequestId, params: CallToolParams) -> Reply:
        self.entered.append(request_id)
        return await self._barrier.call(request_id, params)

    def unoffered(self) -> None:
        self._barrier.unoffered()


class BarrierRunner:
    """The fake of the CLI runner's loop: hand out, yield, accept once resumed, then stop."""

    def __init__(self, barrier: ToolCallBarrier) -> None:
        self.asks = 0
        self._barrier = barrier

    async def run(self, admitted: AdmittedRun) -> AsyncIterator[ToolCallEvent]:
        try:
            while True:
                self.asks += 1
                event = await self._barrier.next_event()
                yield event
                self._barrier.accept(event)
        finally:
            self._barrier.stop()
            await self._barrier.settle()


@dataclass(frozen=True)
class Rig:
    relay: Relay
    barrier: ToolCallBarrier
    entries: EnteringCalls
    app: AppTools

    async def request(
        self,
        request_id: RequestId,
        name: str = "edit_song",
        arguments: Mapping[str, Any] | None = None,
        client: RawClient | None = None,
    ) -> RawClient:
        connected = client or await RawClient.connect(port_of(self.relay))
        params = {"name": name, "arguments": dict(arguments or {})}
        await connected.send(
            post_message(self.relay, json_rpc("tools/call", request_id, params), SUPPORTED)
        )
        return connected

    async def pending(
        self,
        request_id: RequestId,
        name: str = "edit_song",
        arguments: Mapping[str, Any] | None = None,
        client: RawClient | None = None,
    ) -> RawClient:
        entered = len(self.entries.entered)
        connected = await self.request(request_id, name, arguments, client)
        await yield_until(lambda: len(self.entries.entered) > entered)
        return connected

    async def hand_out_and_accept(self) -> ToolCallEvent:
        event = await self.barrier.next_event()
        self.barrier.accept(event)
        return event


def offered(names: Sequence[str]) -> list[ToolDefinition]:
    return [
        ToolDefinition.model_validate({"name": name, "inputSchema": {"type": "object"}})
        for name in names
    ]


@asynccontextmanager
async def gated_app(
    failure: Callable[[], BaseException] | None = None,
) -> AsyncIterator[GatedApp]:
    yield GatedApp(failure() if failure else None)


def drive_barrier(
    scenario: Callable[[Rig], Awaitable[None]],
    app_scope: AbstractAsyncContextManager[AppTools] | None = None,
) -> None:
    async def main() -> None:
        async with app_scope or gated_app() as app:
            barrier = ToolCallBarrier(RUN_ID, app)
            entries = EnteringCalls(barrier)
            owner = McpRelayOwner(
                capability_carrying(canary_token(), tools=CLAIMED), offered(OFFERED), entries
            )
            relay = await Relay.start(RUN_ID, owner, LoopClock())
            try:
                async with asyncio.timeout(WAIT_SECONDS * 3):
                    await scenario(Rig(relay, barrier, entries, app))
            finally:
                barrier.stop()
                if isinstance(app, GatedApp):
                    app.release()
                await barrier.settle()
                await close_bounded(relay)

    asyncio.run(main())


async def reply_of(client: RawClient) -> dict[str, Any]:
    answer = await client.answer()
    assert answer.status == HTTPStatus.OK
    return json.loads(answer.body)


def refused(request_id: RequestId) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": REFUSAL}


def result_of(request_id: RequestId, text: str = UPSTREAM_TEXT) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }


async def waiting_next_event(barrier: ToolCallBarrier) -> asyncio.Task[ToolCallEvent]:
    asking = asyncio.Event()

    async def ask() -> ToolCallEvent:
        asking.set()
        return await barrier.next_event()

    task = asyncio.create_task(ask())
    async with asyncio.timeout(WAIT_SECONDS):
        await asking.wait()
    return task


def arrive(
    barrier: ToolCallBarrier, params: CallToolParams, request_id: RequestId = 1
) -> asyncio.Task[Reply]:
    """A call that has run to its first suspension when this returns."""
    return asyncio.Task(
        barrier.call(request_id, params), loop=asyncio.get_running_loop(), eager_start=True
    )


def cancel_relay_connections() -> None:
    for task in asyncio.all_tasks():
        if task.get_name().startswith(f"relay:{RUN_ID}:conn-"):
            task.cancel()


def streamed(runner: BarrierRunner, runs: RunConfig | None = None) -> asyncio.Task[bytes]:
    frames = run_frames(
        alice_admitted_run(uuid.uuid4()),
        runner,
        SONGMAKER,
        runs or RunConfig(),
        LoopClock(),
        RunRecords(),
    )

    async def collected() -> bytes:
        return b"".join([frame async for frame in frames])

    return asyncio.create_task(collected())


async def settled_stream(task: asyncio.Task[bytes]) -> list[Any]:
    async with asyncio.timeout(WAIT_SECONDS):
        return decoded_events(await task)


EDIT_SONG = CallToolParams(name="edit_song", arguments={"text": "a"})


def app_calls(fake: McpAppFake) -> list[dict[str, Any]]:
    return [
        request.message["params"] for request in fake.requests if request.rpc_method == "tools/call"
    ]


def test_a_call_over_the_budget_is_never_run_in_the_app(authority, caplog) -> None:  # A1
    with serving(ScriptedApp(), authority.credentials_for(CERTIFICATE_HOST)) as fake:

        async def scenario(rig: Rig) -> None:
            first = await rig.pending(1, "edit_song", {"text": "a"})
            second = await rig.pending(2, "edit_song", {"text": "b"})
            assert rig.entries.entered == [1, 2]
            runner = BarrierRunner(rig.barrier)

            events = await settled_stream(streamed(runner, RunConfig(max_tool_calls=1)))

            started, called, failed = events
            assert isinstance(started, RunStarted)
            assert isinstance(called, ToolCall)
            assert (called.tool_use_id, called.name, called.input) == (
                "call-1",
                "edit_song",
                {"text": "a"},
            )
            assert called.effect is ToolEffect.SIDE_EFFECTING
            assert isinstance(failed, Failed)
            assert (failed.code, failed.message) == (
                FailureCode.LIMIT_EXCEEDED,
                TOO_MANY_TOOL_CALLS_MESSAGE,
            )
            assert await reply_of(second) == refused(2)
            assert await reply_of(first) == result_of(1, "edit_song")
            assert app_calls(fake) == [{"name": "edit_song", "arguments": {"text": "a"}}]

        drive_barrier(
            scenario,
            open_app_tools(fake.origin, capability_carrying("t"), authority.client_context()),
        )
    assert failure_lines(caplog) == []


async def another_call_is_accepted(rig: Rig) -> None:
    app = rig.app
    assert isinstance(app, GatedApp)
    first_client = await rig.pending(1, "edit_song", {"text": "a"})
    await rig.pending(2, "edit_song", {"text": "b"})
    first = await rig.barrier.next_event()
    another = first.model_copy(update={"tool_use_id": "call-2"})

    with pytest.raises(ToolCallsMisused):
        rig.barrier.accept(another)
    with pytest.raises(ToolCallsMisused):
        await rig.barrier.next_event()
    assert app.calls == []

    rig.barrier.accept(first)
    await app.forwarded(1)
    with pytest.raises(ToolCallsMisused):
        rig.barrier.accept(first)
    second = await rig.barrier.next_event()
    assert second.tool_use_id == "call-2"

    app.release()
    await rig.barrier.settle()
    assert await reply_of(first_client) == result_of(1)
    assert app.calls == [("edit_song", {"text": "a"})]


async def accepted_after_its_cancellation_was_requested(rig: Rig) -> None:
    assert isinstance(rig.app, GatedApp)
    call = arrive(rig.barrier, EDIT_SONG)
    event = await rig.barrier.next_event()
    call.cancel()

    with pytest.raises(ToolCallsMisused):
        rig.barrier.accept(event)
    await asyncio.gather(call, return_exceptions=True)
    assert rig.app.calls == []


async def handed_out_call_ended_before_the_next_arrives(rig: Rig) -> None:
    first = arrive(rig.barrier, EDIT_SONG, 1)
    assert (await rig.barrier.next_event()).tool_use_id == "call-1"
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    arrive(rig.barrier, EDIT_SONG, 2)

    async with asyncio.timeout(WAIT_SECONDS):
        assert (await rig.barrier.next_event()).tool_use_id == "call-2"


async def asked_by_two_waiters_at_once(rig: Rig) -> None:
    asking = [asyncio.create_task(rig.barrier.next_event()) for _ in range(2)]
    await asyncio.sleep(0)
    calls = [arrive(rig.barrier, EDIT_SONG, request_id) for request_id in (1, 2)]

    outcomes = await asyncio.gather(*asking, return_exceptions=True)

    assert [isinstance(outcome, ToolCallsMisused) for outcome in outcomes].count(True) == 1
    rig.barrier.stop()
    replies = await asyncio.gather(*calls)
    assert [reply.error.code for reply in replies] == [REFUSAL["code"]] * 2


@pytest.mark.parametrize(
    "scenario",
    [
        another_call_is_accepted,
        accepted_after_its_cancellation_was_requested,
        handed_out_call_ended_before_the_next_arrives,
        asked_by_two_waiters_at_once,
    ],
)
def test_accepting_the_handed_out_call_cannot_forward_another(
    scenario: Callable[[Rig], Awaitable[None]],
) -> None:  # A2
    drive_barrier(scenario)


def test_calls_are_handed_out_in_arrival_order_and_identical_ones_stay_distinct() -> None:  # A3
    arrivals = [
        ("edit_song", {"text": "x"}),
        ("read_song", {"id": 3}),
        ("edit_song", {"text": "x"}),
    ]

    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, GatedApp)
        rig.app.release()
        for request_id, (name, arguments) in enumerate(arrivals, start=1):
            await rig.pending(request_id, name, arguments)

        for number, (name, arguments) in enumerate(arrivals, start=1):
            assert len(rig.app.calls) == number - 1
            event = await rig.hand_out_and_accept()
            assert (event.tool_use_id, event.name, event.input) == (
                f"call-{number}",
                name,
                arguments,
            )
            await rig.app.forwarded(number)

        assert rig.app.calls == arrivals

    drive_barrier(scenario)


def test_the_handed_out_input_is_a_copy_of_the_arguments_the_app_receives() -> None:  # A3
    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, GatedApp)
        rig.app.release()
        await rig.pending(1, "edit_song", {"nested": {"text": "a"}})
        event = await rig.barrier.next_event()

        event.input["nested"]["text"] = "changed"
        rig.barrier.accept(event)
        await rig.app.forwarded(1)

        assert rig.app.received == [{"nested": {"text": "a"}}]

    drive_barrier(scenario)


def test_a_duplicate_pending_id_is_refused_and_mints_no_call() -> None:  # A4
    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, GatedApp)
        first = await rig.pending(7, "edit_song", {"text": "a"})
        duplicate = await rig.request(7, "edit_song", {"text": "b"})

        assert await reply_of(duplicate) == refused(7)

        await rig.pending(8, "edit_song", {"text": "c"})
        handed_out = [await rig.hand_out_and_accept(), await rig.barrier.next_event()]
        assert [event.tool_use_id for event in handed_out] == ["call-1", "call-2"]
        rig.app.release()
        await rig.barrier.settle()
        assert await reply_of(first) == result_of(7)
        assert rig.app.calls == [("edit_song", {"text": "a"})]

    drive_barrier(scenario)


def test_a_reused_id_on_a_new_connection_after_initialize_is_a_new_call() -> None:  # A5
    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, GatedApp)
        rig.app.release()
        first = await rig.pending(2, "edit_song", {"text": "a"})
        await rig.hand_out_and_accept()
        assert await reply_of(first) == result_of(2)

        fresh = await RawClient.connect(port_of(rig.relay))
        await fresh.send(
            post_message(rig.relay, json_rpc("initialize", 1, {"protocolVersion": SUPPORTED}))
        )
        assert (await fresh.answer()).status == HTTPStatus.OK
        await rig.pending(2, "edit_song", {"text": "a"}, client=fresh)

        assert len(rig.app.calls) == 1
        event = await rig.hand_out_and_accept()
        assert event.tool_use_id == "call-2"
        await rig.app.forwarded(2)
        assert await reply_of(fresh) == result_of(2)

    drive_barrier(scenario)


def test_a_call_over_the_pending_bound_is_refused_and_never_forwarded() -> None:  # A6
    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, GatedApp)
        assert MAX_PENDING_TOOL_CALLS == MAX_RELAY_CONNECTIONS - 1
        for request_id in range(1, MAX_PENDING_TOOL_CALLS + 1):
            await rig.pending(request_id, "edit_song", {"n": request_id})
        extra = await rig.request(99, "edit_song", {"n": 99})

        assert await reply_of(extra) == refused(99)
        assert not extra.has_ended()
        assert rig.entries.entered == [1, 2, 3, 99]
        first = await rig.hand_out_and_accept()
        rig.app.release()
        await rig.app.forwarded(1)
        await rig.barrier.settle()

        await rig.pending(100, "edit_song", {"n": 100}, client=extra)
        later = [first.tool_use_id]
        for _ in range(MAX_PENDING_TOOL_CALLS):
            later.append((await rig.hand_out_and_accept()).tool_use_id)
        assert later == ["call-1", "call-2", "call-3", "call-4"]

    drive_barrier(scenario)


@pytest.mark.parametrize("name", ["delete_song", "list_songs"])
def test_a_name_never_offered_is_refused_and_ends_the_run_as_a_provider_error(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:  # A7
    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, GatedApp)
        runner = BarrierRunner(rig.barrier)
        stream = streamed(runner)
        await yield_until(lambda: runner.asks == 1)

        client = await rig.request(1, name, {"text": "a"})

        assert await reply_of(client) == refused(1)
        *_, failed = await settled_stream(stream)
        assert isinstance(failed, Failed)
        assert (failed.code, failed.message) == (FailureCode.PROVIDER_ERROR, RUNNER_FAILED_MESSAGE)
        assert rig.entries.entered == []
        assert rig.app.calls == []

    drive_barrier(scenario)

    assert [kind for _, kind, _ in failure_lines(caplog)] == ["UnofferedToolCalled"]
    assert name not in caplog.text


async def calls_pending_when_stopped(rig: Rig) -> None:
    assert isinstance(rig.app, GatedApp)
    forwarded = await rig.pending(1, "edit_song", {"n": 1})
    handed_out = await rig.pending(2, "edit_song", {"n": 2})
    waiting = await rig.pending(3, "edit_song", {"n": 3})
    await rig.hand_out_and_accept()
    await rig.app.forwarded(1)
    event = await rig.barrier.next_event()

    rig.barrier.stop()

    assert await reply_of(handed_out) == refused(2)
    assert await reply_of(waiting) == refused(3)
    assert await reply_of(await rig.request(4, "edit_song", {"n": 4})) == refused(4)
    with pytest.raises(ToolCallsMisused):
        rig.barrier.accept(event)
    rig.app.release()
    assert await reply_of(forwarded) == result_of(1)
    assert rig.app.calls == [("edit_song", {"n": 1})]


async def idle_when_stopped(rig: Rig) -> None:
    waiting = await waiting_next_event(rig.barrier)

    rig.barrier.stop()

    with pytest.raises(ToolCallsMisused):
        await waiting


async def stopped_while_a_cancellation_is_requested(rig: Rig) -> None:
    first = arrive(rig.barrier, EDIT_SONG, 1)
    second = arrive(rig.barrier, EDIT_SONG, 2)
    first.cancel()

    rig.barrier.stop()

    async with asyncio.timeout(WAIT_SECONDS):
        assert (await second).error.code == REFUSAL["code"]
    await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize(
    "scenario",
    [calls_pending_when_stopped, idle_when_stopped, stopped_while_a_cancellation_is_requested],
)
def test_stop_refuses_waiting_calls_and_wakes_the_runner_while_the_relay_is_open(
    scenario: Callable[[Rig], Awaitable[None]],
) -> None:  # A8a
    drive_barrier(scenario)


async def handed_out_when_closed(rig: Rig) -> None:
    event = await rig.barrier.next_event()
    await close_bounded(rig.relay)
    with pytest.raises(ToolCallsMisused):
        rig.barrier.accept(event)


async def waiting_when_closed(rig: Rig) -> None:
    await close_bounded(rig.relay)
    next_event = await waiting_next_event(rig.barrier)
    await asyncio.sleep(0)
    assert not next_event.done()
    next_event.cancel()


@pytest.mark.parametrize("closing", [handed_out_when_closed, waiting_when_closed])
def test_the_relays_close_ends_a_pending_call_so_nothing_of_it_is_forwarded(
    closing: Callable[[Rig], Awaitable[None]],
) -> None:  # A8b
    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, GatedApp)
        client = await rig.pending(1, "edit_song", {"text": "a"})

        await closing(rig)

        assert rig.app.calls == []
        assert await client.delivery() is Delivery.ABSENT

    drive_barrier(scenario)


@dataclass(frozen=True)
class ForwardEnding:
    request_cancelled: bool
    failure: Callable[[], BaseException] | None
    logged_classes: tuple[str, ...]
    status: HTTPStatus | None
    cancelled_on_completion: bool = False


UPSTREAM_BROKE = partial(UpstreamBroke, "x")


@pytest.mark.parametrize(
    "ending",
    [
        ForwardEnding(True, None, (), None),
        ForwardEnding(True, UPSTREAM_BROKE, ("UpstreamBroke",), None),
        ForwardEnding(False, UPSTREAM_BROKE, ("UpstreamBroke",), HTTPStatus.INTERNAL_SERVER_ERROR),
        ForwardEnding(True, asyncio.CancelledError, (), None),
        ForwardEnding(
            False, UPSTREAM_BROKE, ("UpstreamBroke",), None, cancelled_on_completion=True
        ),
    ],
    ids=[
        "cancelled-success",
        "cancelled-failure",
        "waiting-failure",
        "cancelled-forward",
        "cancelled-on-completion",
    ],
)
def test_a_forwards_outcome_is_retrieved_exactly_once_and_settle_waits_for_it(
    ending: ForwardEnding, caplog: pytest.LogCaptureFixture
) -> None:  # A8c
    reports: list[dict[str, Any]] = []

    async def exercise(rig: Rig) -> None:
        app = rig.app
        assert isinstance(app, GatedApp)
        client = await rig.pending(1, "edit_song", {"text": "a"})
        await rig.hand_out_and_accept()
        await app.forwarded(1)
        if ending.request_cancelled:
            await close_bounded(rig.relay)

        settling = asyncio.create_task(rig.barrier.settle())
        for _ in range(SETTLE_TURNS):
            await asyncio.sleep(0)
        assert not settling.done()

        app.release()
        if ending.cancelled_on_completion:
            await asyncio.sleep(0)
            cancel_relay_connections()
        async with asyncio.timeout(WAIT_SECONDS):
            await settling
        if ending.status is not None:
            answer = await client.answer()
            assert (answer.status, answer.body) == (ending.status, b"")
        assert app.calls == [("edit_song", {"text": "a"})]

    async def scenario(rig: Rig) -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _, context: reports.append(context))
        await exercise(rig)
        assert isinstance(rig.app, GatedApp)
        rig.app.forget_failure()
        await close_bounded(rig.relay)
        gc.collect()
        assert reports == []

    with caplog.at_level(logging.ERROR):
        drive_barrier(scenario, gated_app(ending.failure))
    assert [kind for _, kind, _ in failure_lines(caplog)] == list(ending.logged_classes)
    assert reports == []


@pytest.mark.parametrize(
    "arrival_before_cancel", [False, True], ids=["cancel-then-arrival", "arrival-then-cancel"]
)
def test_a_cancelled_wait_for_the_next_event_loses_and_doubles_no_call(
    arrival_before_cancel: bool,
) -> None:  # A9
    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, GatedApp)
        params = CallToolParams(name="edit_song", arguments={"text": "a"})
        waiting = await waiting_next_event(rig.barrier)
        if arrival_before_cancel:
            arrival = arrive(rig.barrier, params)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        if not arrival_before_cancel:
            arrival = arrive(rig.barrier, params)

        event = await rig.hand_out_and_accept()
        assert event.tool_use_id == "call-1"
        further = await waiting_next_event(rig.barrier)
        assert not further.done()
        further.cancel()

        rig.app.release()
        await rig.barrier.settle()
        async with asyncio.timeout(WAIT_SECONDS):
            assert (await arrival).id == 1
        assert rig.app.calls == [("edit_song", {"text": "a"})]

    drive_barrier(scenario)
