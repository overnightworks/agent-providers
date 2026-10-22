"""The execution barrier: only a call the service accepted is forwarded to the app.

The CLI side is the real ``Relay`` with ``McpRelayOwner`` over raw sockets. The runner side is
``BarrierRunner``, the fake of the CLI runner's loop, under the real ``run_frames``.
"""

import asyncio
import contextlib
import gc
import json
import logging
import re
import uuid
import weakref
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from http import HTTPStatus
from typing import Any

import pytest
from bounds import TEST_WAIT_SECONDS, within_the_bound
from capability_support import canary_token, capability_carrying
from mcp_app_fake import CERTIFICATE_HOST, McpAppFake, ScriptedApp, serving
from relay_support import (
    RUN_ID,
    Delivery,
    HoldingSteppedLoop,
    RawClient,
    RecordingApp,
    RelayReads,
    RelayWrites,
    close_bounded,
    count_retrievals,
    holding_loop,
    json_rpc,
    port_of,
    post_message,
    relay_tasks,
    release_held_wakeups,
    task_named,
    yield_until,
)
from service_support import (
    alice_admitted_run,
    decoded_events,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    quarantine_on,
)

from agent_runtime_client.mcp_wire import (
    CallToolParams,
    JsonRpcError,
    Reply,
    RequestId,
    ToolDefinition,
    ToolOutcome,
    tool_result_response,
)
from agent_runtime_client.tools import ToolEffect
from agent_runtime_client.wire import (
    Failed,
    FailureCode,
    RunStarted,
    Succeeded,
    ToolCall,
    ToolResult,
)
from agent_runtime.events import FinalEvent, ToolCallEvent, ToolResultEvent
from agent_runtime_service.app_tools import UpstreamCallLost, open_app_tools
from agent_runtime_service.config import RunConfig
from agent_runtime_service.run_events import (
    RUNNER_FAILED_MESSAGE,
    TOO_MANY_TOOL_CALLS_MESSAGE,
    TOO_MUCH_DATA_MESSAGE,
    run_frames,
)
from agent_runtime_service.run_liveness import LoopClock
from agent_runtime_service.run_records import RunRecords
from agent_runtime_service.runners import AdmittedRun
from agent_runtime_service.tool_relay import (
    CALL_FAILED_TEXT,
    CALL_GIVEN_UP_TEXT,
    MAX_PENDING_TOOL_CALLS,
    MAX_RELAY_CONNECTIONS,
    UNANSWERED,
    AppTools,
    CapabilityReflected,
    McpRelayOwner,
    Relay,
    ToolCallBarrier,
    ToolCallsMisused,
    Unanswered,
)

SUPPORTED = "2025-11-25"
CLAIMED = ("edit_song", "read_song")
OFFERED = ("edit_song", "read_song", "delete_song")
REFUSAL = {"code": -32602, "message": "Invalid params"}
UPSTREAM_TEXT = "ran edit_song"
UPSTREAM_FAILURE_MARKER = "upstream-failure-marker-5d1c"
OVERSIZED_MARKER = "oversized-result-marker-0c4e7b19"
CANARY = canary_token()
CANARY_SIGNATURE = CANARY.rsplit(".", 1)[-1]
ANSWER_OF_CALL_1 = f"relay:{RUN_ID}:conn-1:answer"


class UpstreamBroke(Exception):
    pass


class EnteringCalls:
    """The barrier as the owner's port, recording which ids entered ``call`` in which order."""

    def __init__(self, barrier: ToolCallBarrier) -> None:
        self.entered: list[RequestId] = []
        self._barrier = barrier

    async def call(self, request_id: RequestId, params: CallToolParams) -> Reply | Unanswered:
        self.entered.append(request_id)
        return await self._barrier.call(request_id, params)

    def cancel(self, request_id: RequestId) -> None:
        self._barrier.cancel(request_id)

    def unoffered(self) -> None:
        self._barrier.unoffered()

    def reflected(self) -> None:
        self._barrier.reflected()


class BarrierRunner:
    """The fake of the CLI runner's loop: hand out, yield, accept once resumed, then stop.

    ``accept_gate`` holds each acceptance of a call until the test opens it, and
    ``final_after`` ends the run with a final event once that many results were accepted.
    """

    def __init__(
        self,
        barrier: ToolCallBarrier,
        accept_gate: asyncio.Event | None = None,
        final_after: int | None = None,
    ) -> None:
        self.asks = 0
        self.at_gate = asyncio.Event()
        self._barrier = barrier
        self._accept_gate = accept_gate
        self._final_after = final_after

    async def run(
        self, admitted: AdmittedRun
    ) -> AsyncIterator[ToolCallEvent | ToolResultEvent | FinalEvent]:
        accepted_results = 0
        try:
            while True:
                self.asks += 1
                event = await self._barrier.next_event()
                yield event
                if self._accept_gate is not None and isinstance(event, ToolCallEvent):
                    self.at_gate.set()
                    await self._accept_gate.wait()
                self._barrier.accept(event)
                if isinstance(event, ToolResultEvent):
                    accepted_results += 1
                if accepted_results == self._final_after:
                    yield FinalEvent(text="done")
                    return
        finally:
            self._barrier.stop()
            async with asyncio.timeout(TEST_WAIT_SECONDS):
                await self._barrier.settle()


@dataclass(frozen=True)
class Rig:
    """The relay, barrier and app of one scenario, keeping every connection it opened.

    A collected client closes its socket, and the relay ends a call whose connection closed.
    """

    relay: Relay
    barrier: ToolCallBarrier
    entries: EnteringCalls
    app: AppTools
    connected: list[RawClient] = field(default_factory=list)

    async def request(
        self,
        request_id: RequestId,
        name: str = "edit_song",
        arguments: Mapping[str, Any] | None = None,
        client: RawClient | None = None,
    ) -> RawClient:
        if client is None:
            client = await RawClient.connect(port_of(self.relay))
            self.connected.append(client)
        params = {"name": name, "arguments": dict(arguments or {})}
        await client.send(
            post_message(self.relay, json_rpc("tools/call", request_id, params), SUPPORTED)
        )
        return client

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

    async def cancel(self, request_id: RequestId, client: RawClient | None = None) -> RawClient:
        connected = client or await RawClient.connect(port_of(self.relay))
        body = json_rpc("notifications/cancelled", None, {"requestId": request_id})
        await connected.send(post_message(self.relay, body, SUPPORTED))
        assert (await connected.answer()).status == HTTPStatus.ACCEPTED
        return connected

    async def hand_out_and_accept(self) -> ToolCallEvent:
        event = await self.barrier.next_event()
        assert isinstance(event, ToolCallEvent)
        self.barrier.accept(event)
        return event

    async def accept_result(self, tool_use_id: str) -> ToolResultEvent:
        result = await self.barrier.next_event()
        assert isinstance(result, ToolResultEvent)
        assert result.tool_use_id == tool_use_id
        self.barrier.accept(result)
        return result

    @property
    def fake(self) -> RecordingApp:
        assert isinstance(self.app, RecordingApp)
        return self.app


def offered(names: Sequence[str]) -> list[ToolDefinition]:
    return [
        ToolDefinition.model_validate({"name": name, "inputSchema": {"type": "object"}})
        for name in names
    ]


@asynccontextmanager
async def gated_app(
    failure: Callable[[], BaseException] | None = None, outcome: ToolOutcome | None = None
) -> AsyncIterator[RecordingApp]:
    yield RecordingApp(outcome, failure() if failure else None, gated=True)


def drive_barrier(
    scenario: Callable[[Rig], Awaitable[None]],
    app_scope: AbstractAsyncContextManager[AppTools] | None = None,
    loop_factory: Callable[[], asyncio.AbstractEventLoop] | None = None,
) -> None:
    async def main() -> None:
        async with app_scope or gated_app() as app:
            capability = capability_carrying(CANARY, tools=CLAIMED)
            barrier = ToolCallBarrier(RUN_ID, app, capability)
            entries = EnteringCalls(barrier)
            owner = McpRelayOwner(capability, offered(OFFERED), entries)
            relay = await Relay.start(RUN_ID, owner, LoopClock())
            try:
                async with asyncio.timeout(TEST_WAIT_SECONDS * 3):
                    await scenario(Rig(relay, barrier, entries, app))
            finally:
                try:
                    try:
                        barrier.stop()
                    finally:
                        if isinstance(app, RecordingApp):
                            app.release()
                        release_held_wakeups()
                        async with asyncio.timeout(TEST_WAIT_SECONDS):
                            await barrier.settle()
                finally:
                    await close_bounded(relay)

    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(main())


async def reply_of(client: RawClient) -> dict[str, Any]:
    answer = await client.answer()
    assert answer.status == HTTPStatus.OK
    return json.loads(answer.body)


def refused(request_id: RequestId) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": REFUSAL}


def result_of(
    request_id: RequestId, text: str = UPSTREAM_TEXT, is_error: bool = False
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
    }


async def waiting_next_event(
    barrier: ToolCallBarrier,
) -> asyncio.Task[ToolCallEvent | ToolResultEvent]:
    asking = asyncio.Event()

    async def ask() -> ToolCallEvent | ToolResultEvent:
        asking.set()
        return await barrier.next_event()

    task = asyncio.create_task(ask())
    async with asyncio.timeout(TEST_WAIT_SECONDS):
        await asking.wait()
    return task


def arrive(
    barrier: ToolCallBarrier, params: CallToolParams, request_id: RequestId = 1
) -> asyncio.Task[Reply | Unanswered]:
    """A call that has run to its first suspension when this returns."""
    return asyncio.Task(
        barrier.call(request_id, params), loop=asyncio.get_running_loop(), eager_start=True
    )


def upstream_task(number: int) -> asyncio.Task[ToolOutcome]:
    name = f"upstream:{RUN_ID}:call-{number}"
    return next(task for task in asyncio.all_tasks() if task.get_name() == name)


def upstream_task_names() -> list[str]:
    return sorted(
        name for task in asyncio.all_tasks() if (name := task.get_name()).startswith("upstream:")
    )


async def ending_of(call: asyncio.Task[Reply | Unanswered]) -> object:
    (ending,) = await asyncio.gather(call, return_exceptions=True)
    return ending


def upstream_answer(request_id: RequestId) -> Reply:
    return tool_result_response(request_id, ToolOutcome(UPSTREAM_TEXT, is_error=False))


def capture_loop_reports() -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    asyncio.get_running_loop().set_exception_handler(lambda _, context: reports.append(context))
    return reports


@dataclass(frozen=True)
class SettledState:
    logged_classes: list[str]
    retrievals: int


def settling_with_evidence(
    rig: Rig, caplog: pytest.LogCaptureFixture, retrievals: Counter[str]
) -> asyncio.Task[SettledState]:
    """A ``settle()`` that has started; its task reads the evidence at ``settle()``'s return."""

    async def settle_and_observe() -> SettledState:
        await rig.barrier.settle()
        return SettledState(
            [kind for _, kind, _ in failure_lines(caplog)], retrievals[f"upstream:{RUN_ID}:call-1"]
        )

    return asyncio.Task(settle_and_observe(), loop=asyncio.get_running_loop(), eager_start=True)


async def closed_without_unretrieved_reports(rig: Rig, reports: list[dict[str, Any]]) -> None:
    rig.fake.forget_failure()
    await close_bounded(rig.relay)
    gc.collect()
    assert reports == []


def cancel_relay_tasks(suffix: str) -> None:
    for task in asyncio.all_tasks():
        if re.fullmatch(rf"relay:{RUN_ID}:conn-\d+{suffix}", task.get_name()):
            task.cancel()


def cancel_relay_connections() -> None:
    cancel_relay_tasks("")


def cancel_owner_answers() -> None:
    cancel_relay_tasks(":answer")


def streamed(runner: BarrierRunner, runs: RunConfig | None = None) -> asyncio.Task[bytes]:
    frames = run_frames(
        alice_admitted_run(uuid.uuid4()),
        runner,
        runs or RunConfig(),
        LoopClock(),
        RunRecords(),
        quarantine=quarantine_on(),
    )

    async def collected() -> bytes:
        return b"".join([frame async for frame in frames])

    return asyncio.create_task(collected())


async def settled_stream(task: asyncio.Task[bytes]) -> list[Any]:
    return decoded_events(await within_the_bound(task))


EDIT_SONG = CallToolParams(name="edit_song", arguments={"text": "a"})
GIVEN_UP_RESULT = ToolResultEvent(tool_use_id="call-1", content=CALL_GIVEN_UP_TEXT, is_error=True)


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
            assert await reply_of(first) == refused(1)
            assert app_calls(fake) == [{"name": "edit_song", "arguments": {"text": "a"}}]

        drive_barrier(
            scenario,
            open_app_tools(fake.origin, capability_carrying("t"), authority.client_context()),
        )
    assert failure_lines(caplog) == []


async def another_call_is_accepted(rig: Rig) -> None:
    app = rig.app
    assert isinstance(app, RecordingApp)
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
    rig.barrier.cancel(2)
    rig.barrier.accept(second)
    assert await rig.accept_result("call-2") == ToolResultEvent(
        tool_use_id="call-2", content=CALL_GIVEN_UP_TEXT, is_error=True
    )
    assert await rig.accept_result("call-1") == ToolResultEvent(
        tool_use_id="call-1", content=UPSTREAM_TEXT, is_error=False
    )
    assert await reply_of(first_client) == result_of(1)
    assert app.calls == [("edit_song", {"text": "a"})]


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
        asked_by_two_waiters_at_once,
    ],
)
def test_accepting_the_handed_out_call_cannot_forward_another(
    scenario: Callable[[Rig], Awaitable[None]],
) -> None:  # A2
    drive_barrier(scenario)


@dataclass(frozen=True)
class GiveUp:
    apply: Callable[[ToolCallBarrier, asyncio.Task[Reply | Unanswered]], None]
    unanswered: bool


CANCEL = GiveUp(lambda barrier, call: barrier.cancel(1), unanswered=True)
TASK_CANCELLATION = GiveUp(lambda barrier, call: call.cancel(), unanswered=False)


def saturated(rig: Rig) -> list[asyncio.Task[Reply | Unanswered]]:
    return [arrive(rig.barrier, EDIT_SONG, n) for n in range(1, MAX_PENDING_TOOL_CALLS + 1)]


async def ended_as_given_up_and_admits_a_fourth(
    rig: Rig, first: asyncio.Task[Reply | Unanswered], give_up: GiveUp
) -> None:
    ending = await ending_of(first)
    if give_up.unanswered:
        assert ending is UNANSWERED
    else:
        assert isinstance(ending, asyncio.CancelledError)
    assert not arrive(rig.barrier, EDIT_SONG, MAX_PENDING_TOOL_CALLS + 1).done()


async def waiting_call_given_up(rig: Rig, give_up: GiveUp) -> None:
    calls = saturated(rig)

    give_up.apply(rig.barrier, calls[0])
    following = await rig.barrier.next_event()

    assert following.tool_use_id == "call-2"
    assert isinstance(following, ToolCallEvent)
    rig.barrier.accept(following)
    assert (await rig.barrier.next_event()).tool_use_id == "call-3"
    await ended_as_given_up_and_admits_a_fourth(rig, calls[0], give_up)
    assert rig.fake.calls == [("edit_song", {"text": "a"})]


async def handed_out_call_given_up(rig: Rig, give_up: GiveUp) -> None:
    calls = saturated(rig)
    event = await rig.barrier.next_event()
    assert isinstance(event, ToolCallEvent)

    give_up.apply(rig.barrier, calls[0])
    rig.barrier.accept(event)

    await ended_as_given_up_and_admits_a_fourth(rig, calls[0], give_up)
    assert rig.fake.calls == []
    assert await rig.barrier.next_event() == GIVEN_UP_RESULT
    with pytest.raises(ToolCallsMisused):
        await rig.barrier.next_event()
    rig.barrier.accept(GIVEN_UP_RESULT)
    assert (await rig.barrier.next_event()).tool_use_id == "call-2"


async def forwarded_call_given_up(rig: Rig, give_up: GiveUp) -> None:
    calls = saturated(rig)
    await rig.hand_out_and_accept()
    await rig.fake.forwarded(1)
    forward = upstream_task(1)

    give_up.apply(rig.barrier, calls[0])

    await ended_as_given_up_and_admits_a_fourth(rig, calls[0], give_up)
    assert not forward.done()
    rig.fake.release()
    await rig.barrier.settle()
    assert (forward.cancelled(), forward.result()) == (False, ToolOutcome(UPSTREAM_TEXT, False))
    assert rig.fake.calls == [("edit_song", {"text": "a"})]


@pytest.mark.parametrize(
    "give_up", [pytest.param(CANCEL, id="cancel"), pytest.param(TASK_CANCELLATION, id="task")]
)
@pytest.mark.parametrize(
    "state", [waiting_call_given_up, handed_out_call_given_up, forwarded_call_given_up]
)
def test_a_given_up_call_ends_by_its_state_at_once_and_frees_its_slot(
    state: Callable[[Rig, GiveUp], Awaitable[None]], give_up: GiveUp
) -> None:
    """C4."""

    async def scenario(rig: Rig) -> None:
        await state(rig, give_up)

    drive_barrier(scenario)


def test_a_cancel_on_the_free_fourth_connection_frees_the_slot_the_next_call_takes() -> None:
    """C5."""

    async def scenario(rig: Rig) -> None:
        clients = [await rig.pending(n, "edit_song", {"n": n}) for n in (1, 2, 3)]
        fourth = await rig.cancel(2)

        await rig.pending(4, "edit_song", {"n": 4}, client=fourth)

        assert await clients[1].delivery() is Delivery.ABSENT
        handed_out = [(await rig.hand_out_and_accept()).tool_use_id for _ in range(3)]
        assert handed_out == ["call-1", "call-3", "call-4"]

    drive_barrier(scenario)


@dataclass(frozen=True)
class Race:
    forwarded: bool
    interfere: Callable[[ToolCallBarrier, asyncio.Task[Reply | Unanswered]], None]
    unanswered: bool


def cancel_when_the_upstream_finishes(
    barrier: ToolCallBarrier, call: asyncio.Task[Reply | Unanswered]
) -> None:
    upstream_task(1).add_done_callback(lambda _: barrier.cancel(1))


def cancel_then_task_cancellation(
    barrier: ToolCallBarrier, call: asyncio.Task[Reply | Unanswered]
) -> None:
    barrier.cancel(1)
    call.cancel()


def task_cancellation_then_cancel(
    barrier: ToolCallBarrier, call: asyncio.Task[Reply | Unanswered]
) -> None:
    call.cancel()
    barrier.cancel(1)


def cancel_then_stop(barrier: ToolCallBarrier, call: asyncio.Task[Reply | Unanswered]) -> None:
    barrier.cancel(1)
    barrier.stop()


def stop_then_cancel(barrier: ToolCallBarrier, call: asyncio.Task[Reply | Unanswered]) -> None:
    barrier.stop()
    barrier.cancel(1)


@pytest.mark.parametrize(
    "race",
    [
        pytest.param(Race(True, cancel_when_the_upstream_finishes, True), id="after-the-upstream"),
        pytest.param(Race(True, cancel_then_task_cancellation, False), id="cancel-then-task"),
        pytest.param(Race(False, cancel_then_stop, True), id="cancel-then-stop"),
        pytest.param(Race(False, stop_then_cancel, True), id="stop-then-cancel"),
        pytest.param(Race(True, task_cancellation_then_cancel, False), id="task-then-cancel"),
    ],
)
def test_a_give_up_before_the_call_returns_leaves_its_request_unanswered(
    race: Race, caplog: pytest.LogCaptureFixture
) -> None:
    """C6."""

    async def scenario(rig: Rig) -> None:
        reports = capture_loop_reports()
        retrievals = count_retrievals()
        call = arrive(rig.barrier, EDIT_SONG)
        if race.forwarded:
            await rig.hand_out_and_accept()
            await rig.fake.forwarded(1)
        settling = settling_with_evidence(rig, caplog, retrievals)

        race.interfere(rig.barrier, call)
        rig.fake.release()
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            ending = await ending_of(call)
            settled = await settling

        if race.unanswered:
            assert ending is UNANSWERED
        else:
            assert isinstance(ending, asyncio.CancelledError)
        assert settled.logged_classes == (["UpstreamBroke"] if race.forwarded else [])
        assert settled.retrievals == int(race.forwarded)
        await closed_without_unretrieved_reports(rig, reports)

    with caplog.at_level(logging.ERROR):
        drive_barrier(scenario, gated_app(UPSTREAM_BROKE))


async def next_event_before_accept(barrier: ToolCallBarrier, event: ToolCallEvent) -> None:
    with pytest.raises(ToolCallsMisused):
        await barrier.next_event()


async def accept_after_stop(barrier: ToolCallBarrier, event: ToolCallEvent) -> None:
    barrier.stop()
    with pytest.raises(ToolCallsMisused):
        barrier.accept(event)


async def next_event_after_accept_and_stop(barrier: ToolCallBarrier, event: ToolCallEvent) -> None:
    barrier.accept(event)
    barrier.stop()
    with pytest.raises(ToolCallsMisused):
        await barrier.next_event()


@pytest.mark.parametrize(
    "misuse", [next_event_before_accept, accept_after_stop, next_event_after_accept_and_stop]
)
def test_the_hand_out_rule_holds_over_a_given_up_handed_out_call(
    misuse: Callable[[ToolCallBarrier, ToolCallEvent], Awaitable[None]],
) -> None:
    """C7."""

    async def scenario(rig: Rig) -> None:
        call = arrive(rig.barrier, EDIT_SONG)
        event = await rig.barrier.next_event()
        assert isinstance(event, ToolCallEvent)
        rig.barrier.cancel(1)

        await misuse(rig.barrier, event)

        await ending_of(call)
        assert rig.fake.calls == []

    drive_barrier(scenario)


async def nothing_arrived(rig: Rig) -> bool:
    return False


async def a_call_waits_to_be_handed_out(rig: Rig) -> bool:
    arrive(rig.barrier, EDIT_SONG)
    return True


async def a_waiting_call_was_given_up(rig: Rig) -> bool:
    arrive(rig.barrier, EDIT_SONG)
    rig.barrier.cancel(1)
    return False


async def a_result_waits_to_be_handed_out(rig: Rig) -> bool:
    arrive(rig.barrier, EDIT_SONG)
    await rig.hand_out_and_accept()
    await rig.fake.forwarded(1)
    rig.fake.release()
    await rig.barrier.settle()
    return True


async def a_result_was_handed_out_and_nothing_follows(rig: Rig) -> bool:
    await a_result_waits_to_be_handed_out(rig)
    await rig.accept_result("call-1")
    return False


async def the_offer_was_exceeded(rig: Rig) -> bool:
    rig.barrier.unoffered()
    return True


@pytest.mark.parametrize(
    "arrangement",
    [
        nothing_arrived,
        a_call_waits_to_be_handed_out,
        a_waiting_call_was_given_up,
        a_result_waits_to_be_handed_out,
        a_result_was_handed_out_and_nothing_follows,
        the_offer_was_exceeded,
    ],
)
def test_the_barrier_has_a_ready_event_exactly_while_next_event_would_not_wait(
    arrangement: Callable[[Rig], Awaitable[bool]],
) -> None:
    async def scenario(rig: Rig) -> None:
        expected = await arrangement(rig)

        assert rig.barrier.has_ready_event() is expected

    drive_barrier(scenario)


def test_the_barrier_has_a_ready_event_while_a_reflected_capability_is_pending() -> None:
    async def scenario(rig: Rig) -> None:
        await a_result_waits_to_be_handed_out(rig)

        assert rig.barrier.has_ready_event()
        with pytest.raises(CapabilityReflected):
            await rig.barrier.next_event()

    drive_barrier(scenario, gated_app(outcome=ToolOutcome(CANARY_SIGNATURE, False)))


def test_a_reflection_reported_by_the_owner_fails_the_next_event() -> None:
    async def scenario(rig: Rig) -> None:
        waiting = asyncio.ensure_future(rig.barrier.next_event())
        await asyncio.sleep(0)
        assert not waiting.done()

        rig.barrier.reflected()

        with pytest.raises(CapabilityReflected):
            await asyncio.wait_for(waiting, TEST_WAIT_SECONDS)
        assert rig.barrier.has_ready_event()

    drive_barrier(scenario)


async def a_call_waits_without_a_forward(rig: Rig) -> bool:
    await a_call_waits_to_be_handed_out(rig)
    return False


async def a_forward_is_running(rig: Rig) -> bool:
    arrive(rig.barrier, EDIT_SONG)
    await rig.hand_out_and_accept()
    await rig.fake.forwarded(1)
    return True


async def a_forward_whose_result_is_queued(rig: Rig) -> bool:
    await a_result_waits_to_be_handed_out(rig)
    return False


async def a_forward_the_cli_gave_up_on(rig: Rig) -> bool:
    await a_forward_is_running(rig)
    rig.barrier.cancel(1)
    return True


@pytest.mark.parametrize(
    "arrangement",
    [
        nothing_arrived,
        a_call_waits_without_a_forward,
        a_forward_is_running,
        a_forward_the_cli_gave_up_on,
        a_forward_whose_result_is_queued,
    ],
)
def test_the_barrier_has_a_running_forward_from_its_accept_until_its_result_is_queued(
    arrangement: Callable[[Rig], Awaitable[bool]],
) -> None:
    async def scenario(rig: Rig) -> None:
        expected = await arrangement(rig)

        assert rig.barrier.has_running_forward() is expected

    drive_barrier(scenario)


async def an_id_never_seen(rig: Rig) -> None:
    call = arrive(rig.barrier, EDIT_SONG, 1)

    rig.barrier.cancel(99)

    await rig.hand_out_and_accept()
    rig.fake.release()
    await rig.accept_result("call-1")
    assert await call == upstream_answer(1)


async def a_repeated_cancel(rig: Rig) -> None:
    first = arrive(rig.barrier, EDIT_SONG, 1)
    second = arrive(rig.barrier, EDIT_SONG, 2)

    rig.barrier.cancel(1)
    rig.barrier.cancel(1)

    assert await ending_of(first) is UNANSWERED
    await rig.hand_out_and_accept()
    rig.fake.release()
    await rig.accept_result("call-2")
    assert await second == upstream_answer(2)


async def a_refused_duplicate(rig: Rig) -> None:
    original = arrive(rig.barrier, EDIT_SONG, 7)
    duplicate = arrive(rig.barrier, EDIT_SONG, 7)
    assert isinstance(duplicate.result(), JsonRpcError)
    assert duplicate.result().error.code == REFUSAL["code"]

    rig.barrier.cancel(7)

    assert await ending_of(original) is UNANSWERED


async def a_reused_id(rig: Rig) -> None:
    rig.fake.release()
    first = arrive(rig.barrier, EDIT_SONG, 2)
    await rig.hand_out_and_accept()
    await rig.accept_result("call-1")
    assert await first == upstream_answer(2)
    reused = arrive(rig.barrier, EDIT_SONG, 2)

    rig.barrier.cancel(2)

    assert await ending_of(reused) is UNANSWERED
    assert first.result() == upstream_answer(2)


async def an_id_reused_in_the_step_of_its_cancel(rig: Rig) -> None:
    first = arrive(rig.barrier, EDIT_SONG, 1)

    rig.barrier.cancel(1)
    second = arrive(rig.barrier, EDIT_SONG, 1)

    assert not second.done()
    assert await ending_of(first) is UNANSWERED
    rig.barrier.cancel(1)
    assert await ending_of(second) is UNANSWERED


@pytest.mark.parametrize(
    "scenario",
    [
        an_id_never_seen,
        a_repeated_cancel,
        a_refused_duplicate,
        a_reused_id,
        an_id_reused_in_the_step_of_its_cancel,
    ],
)
def test_a_cancel_ends_only_the_pending_call_with_that_id(
    scenario: Callable[[Rig], Awaitable[None]],
) -> None:
    """C8."""
    drive_barrier(scenario)


@pytest.mark.parametrize("ending", ["cancelled_by_the_cli", "given_up_by_task_cancellation"])
def test_a_call_that_ended_before_its_hand_out_leaves_nothing_queued_while_the_runner_is_parked(
    ending: str,
) -> None:
    async def scenario(rig: Rig) -> None:
        admitted = [
            CallToolParams(name="edit_song", arguments={"n": number}) for number in range(1, 6)
        ]
        watchers = [weakref.ref(params) for params in admitted]
        calls = [arrive(rig.barrier, params, number) for number, params in enumerate(admitted, 1)]
        for number, call in enumerate(calls, 1):
            if ending == "cancelled_by_the_cli":
                rig.barrier.cancel(number)
            else:
                call.cancel()
            await ending_of(call)
        del admitted, calls, call
        await asyncio.sleep(0)
        gc.collect()

        assert [watcher() for watcher in watchers] == [None] * len(watchers)

    drive_barrier(scenario)


def test_a_call_cancelled_before_its_accept_streams_an_error_result_and_never_runs() -> None:
    """C9."""

    async def scenario(rig: Rig) -> None:
        gate = asyncio.Event()
        runner = BarrierRunner(rig.barrier, accept_gate=gate, final_after=2)
        stream = streamed(runner, RunConfig(max_tool_calls=2))
        first = await rig.pending(1, "edit_song", {"n": 1})
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            await runner.at_gate.wait()

        second_connection = await rig.cancel(1)
        gate.set()
        await rig.pending(2, "edit_song", {"n": 2}, client=second_connection)
        rig.fake.release()

        started, first_call, given_up, second_call, second_result, succeeded = await settled_stream(
            stream
        )
        assert isinstance(started, RunStarted)
        assert isinstance(first_call, ToolCall)
        assert (first_call.tool_use_id, first_call.name, first_call.effect) == (
            "call-1",
            "edit_song",
            ToolEffect.SIDE_EFFECTING,
        )
        assert isinstance(given_up, ToolResult)
        assert (given_up.tool_use_id, given_up.content, given_up.is_error) == (
            "call-1",
            CALL_GIVEN_UP_TEXT,
            True,
        )
        assert isinstance(second_call, ToolCall)
        assert second_call.tool_use_id == "call-2"
        assert isinstance(second_result, ToolResult)
        assert (second_result.tool_use_id, second_result.content) == ("call-2", UPSTREAM_TEXT)
        assert isinstance(succeeded, Succeeded)
        assert rig.fake.calls == [("edit_song", {"n": 2})]
        assert await first.delivery() is Delivery.ABSENT
        assert await reply_of(second_connection) == result_of(2)

    drive_barrier(scenario)


def test_calls_are_handed_out_in_arrival_order_and_identical_ones_stay_distinct() -> None:  # A3
    arrivals = [
        ("edit_song", {"text": "x"}),
        ("read_song", {"id": 3}),
        ("edit_song", {"text": "x"}),
    ]

    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, RecordingApp)
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
        assert isinstance(rig.app, RecordingApp)
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
        assert isinstance(rig.app, RecordingApp)
        first = await rig.pending(7, "edit_song", {"text": "a"})
        duplicate = await rig.request(7, "edit_song", {"text": "b"})

        assert await reply_of(duplicate) == refused(7)

        await rig.pending(8, "edit_song", {"text": "c"})
        handed_out = [await rig.hand_out_and_accept(), await rig.barrier.next_event()]
        assert [event.tool_use_id for event in handed_out] == ["call-1", "call-2"]
        rig.app.release()
        await rig.barrier.settle()
        rig.barrier.cancel(8)
        rig.barrier.accept(handed_out[1])
        await rig.accept_result("call-2")
        await rig.accept_result("call-1")
        assert await reply_of(first) == result_of(7)
        assert rig.app.calls == [("edit_song", {"text": "a"})]

    drive_barrier(scenario)


def test_a_reused_id_on_a_new_connection_after_initialize_is_a_new_call() -> None:  # A5
    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, RecordingApp)
        rig.app.release()
        first = await rig.pending(2, "edit_song", {"text": "a"})
        await rig.hand_out_and_accept()
        await rig.accept_result("call-1")
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
        await rig.accept_result("call-2")
        assert await reply_of(fresh) == result_of(2)

    drive_barrier(scenario)


def test_a_call_over_the_pending_bound_is_refused_and_never_forwarded() -> None:  # A6, P4
    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, RecordingApp)
        assert MAX_PENDING_TOOL_CALLS == MAX_RELAY_CONNECTIONS - 1
        clients = [
            await rig.pending(request_id, "edit_song", {"n": request_id})
            for request_id in range(1, MAX_PENDING_TOOL_CALLS + 1)
        ]
        extra = await rig.request(99, "edit_song", {"n": 99})

        assert await reply_of(extra) == refused(99)
        assert not extra.has_ended()
        assert rig.entries.entered == [1, 2, 3, 99]
        first = await rig.hand_out_and_accept()
        rig.app.release()
        await rig.app.forwarded(1)
        await rig.barrier.settle()
        later = [first.tool_use_id]
        for _ in range(MAX_PENDING_TOOL_CALLS - 1):
            later.append((await rig.hand_out_and_accept()).tool_use_id)
        await rig.barrier.settle()

        await rig.request(100, "edit_song", {"n": 100}, client=extra)
        assert await reply_of(extra) == refused(100)
        await rig.accept_result("call-1")
        assert await reply_of(clients[0]) == result_of(1)
        await rig.pending(101, "edit_song", {"n": 101}, client=extra)
        await rig.accept_result("call-2")
        await rig.accept_result("call-3")
        later.append((await rig.hand_out_and_accept()).tool_use_id)
        assert later == ["call-1", "call-2", "call-3", "call-4"]
        assert rig.entries.entered == [1, 2, 3, 99, 100, 101]

    drive_barrier(scenario)


@pytest.mark.parametrize("name", ["delete_song", "list_songs"])
def test_a_name_never_offered_is_refused_and_ends_the_run_as_a_provider_error(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:  # A7
    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, RecordingApp)
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
    assert isinstance(rig.app, RecordingApp)
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
    assert await reply_of(forwarded) == refused(1)
    rig.app.release()
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

    async with asyncio.timeout(TEST_WAIT_SECONDS):
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
    assert isinstance(event, ToolCallEvent)
    await close_bounded(rig.relay)
    rig.barrier.accept(event)
    assert await rig.barrier.next_event() == GIVEN_UP_RESULT


async def handed_out_when_its_connection_task_is_cancelled(rig: Rig) -> None:
    event = await rig.barrier.next_event()
    assert isinstance(event, ToolCallEvent)
    cancel_relay_connections()
    rig.barrier.accept(event)
    assert await rig.barrier.next_event() == GIVEN_UP_RESULT


async def waiting_when_closed(rig: Rig) -> None:
    await close_bounded(rig.relay)
    next_event = await waiting_next_event(rig.barrier)
    await asyncio.sleep(0)
    assert not next_event.done()
    next_event.cancel()


@pytest.mark.parametrize(
    "closing",
    [handed_out_when_closed, handed_out_when_its_connection_task_is_cancelled, waiting_when_closed],
)
def test_the_relays_close_ends_a_pending_call_so_nothing_of_it_is_forwarded(
    closing: Callable[[Rig], Awaitable[None]],
) -> None:  # A8b
    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, RecordingApp)
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
    retrievals: int
    no_result: bool = False
    cancelled_on_completion: bool = False


UPSTREAM_BROKE = partial(UpstreamBroke, UPSTREAM_FAILURE_MARKER)


@pytest.mark.parametrize(
    "ending",
    [
        ForwardEnding(True, None, (), 1),
        ForwardEnding(True, UPSTREAM_BROKE, ("UpstreamBroke",), 1),
        ForwardEnding(True, asyncio.CancelledError, (), 0, no_result=True),
        ForwardEnding(False, asyncio.CancelledError, (), 0, no_result=True),
        ForwardEnding(False, UPSTREAM_BROKE, ("UpstreamBroke",), 1, cancelled_on_completion=True),
    ],
    ids=[
        "cancelled-success",
        "cancelled-failure",
        "cancelled-forward",
        "cancelled-forward-with-an-active-waiter",
        "cancelled-on-completion",
    ],
)
def test_a_forwards_outcome_is_retrieved_exactly_once_and_settle_waits_for_it(
    ending: ForwardEnding, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:  # A8c, P5
    writes = RelayWrites(monkeypatch)

    async def scenario(rig: Rig) -> None:
        reports = capture_loop_reports()
        retrievals = count_retrievals()
        app = rig.fake
        client = await rig.pending(1, "edit_song", {"text": "a"})
        await rig.hand_out_and_accept()
        await app.forwarded(1)
        if ending.request_cancelled:
            await close_bounded(rig.relay)

        settling = settling_with_evidence(rig, caplog, retrievals)
        assert not settling.done()

        if ending.cancelled_on_completion:
            upstream_task(1).add_done_callback(lambda _: cancel_owner_answers())
        app.release()
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            settled = await settling

        assert settled.logged_classes == list(ending.logged_classes)
        assert settled.retrievals == ending.retrievals
        if ending.cancelled_on_completion or (ending.no_result and not ending.request_cancelled):
            assert await client.delivery() is Delivery.ABSENT
            assert writes.to(client) == b""
        if ending.no_result:
            arrive(rig.barrier, EDIT_SONG, 2)
            assert (await rig.barrier.next_event()).tool_use_id == "call-2"
        assert app.calls == [("edit_song", {"text": "a"})]
        await closed_without_unretrieved_reports(rig, reports)

    with caplog.at_level(logging.ERROR):
        drive_barrier(scenario, gated_app(ending.failure))


@pytest.mark.parametrize(
    "arrival_before_cancel", [False, True], ids=["cancel-then-arrival", "arrival-then-cancel"]
)
def test_a_cancelled_wait_for_the_next_event_loses_and_doubles_no_call(
    arrival_before_cancel: bool,
) -> None:  # A9
    async def scenario(rig: Rig) -> None:
        assert isinstance(rig.app, RecordingApp)
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
        await rig.accept_result("call-1")
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            assert (await arrival).id == 1
        assert rig.app.calls == [("edit_song", {"text": "a"})]

    drive_barrier(scenario)


CONNECTION_OF_CALL_1 = f"relay:{RUN_ID}:conn-1"
WATCH_OF_CALL_1 = f"{CONNECTION_OF_CALL_1}:watch"
PARTIAL_NEXT_HEAD = b"POST /mcp HTTP/1.1\r\nHost: x\r\n"
FORWARDED_CALL_1 = ("edit_song", {"n": 1})


class Call1(Enum):
    WAITING = "waiting"
    HANDED_OUT = "handed out"
    FORWARDED = "forwarded"


async def bring_call_1_to(rig: Rig, state: Call1) -> ToolCallEvent | None:
    if state is Call1.HANDED_OUT:
        event = await rig.barrier.next_event()
        assert isinstance(event, ToolCallEvent)
        return event
    if state is Call1.FORWARDED:
        await rig.hand_out_and_accept()
    return None


async def accept_calls_up_to_call_4(rig: Rig) -> list[str | ToolResultEvent]:
    """The events in the order they came: an accepted call by its id, a result as itself."""
    events: list[str | ToolResultEvent] = []
    while sum(isinstance(event, str) for event in events) < 3:
        event = await rig.barrier.next_event()
        rig.barrier.accept(event)
        events.append(event.tool_use_id if isinstance(event, ToolCallEvent) else event)
    return events


CLI_CLOSES = [
    pytest.param(Call1.HANDED_OUT, RawClient.disconnect, b"", id="handed-out-fin"),
    pytest.param(
        Call1.HANDED_OUT, RawClient.disconnect, PARTIAL_NEXT_HEAD, id="handed-out-bytes-then-fin"
    ),
    pytest.param(Call1.WAITING, RawClient.finish_sending, b"", id="waiting-half-close"),
    pytest.param(Call1.FORWARDED, RawClient.reset, b"", id="forwarded-reset"),
]


@pytest.mark.parametrize(("state", "end", "pipelined"), CLI_CLOSES)
def test_a_call_the_cli_closed_is_never_forwarded_and_frees_its_slot_while_saturated(
    state: Call1,
    end: Callable[[RawClient], None],
    pipelined: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W7."""
    reads = RelayReads(monkeypatch)

    async def scenario(rig: Rig) -> None:
        clients = [await rig.pending(number, "edit_song", {"n": number}) for number in (1, 2, 3)]
        handed_out = await bring_call_1_to(rig, state)
        connection_task = task_named(CONNECTION_OF_CALL_1)
        await clients[0].send(pipelined)
        await reads.until_read(WATCH_OF_CALL_1, len(pipelined))
        end(clients[0])
        await yield_until(lambda: CONNECTION_OF_CALL_1 not in relay_tasks())
        assert not connection_task.cancelled()
        if handed_out is not None:
            rig.barrier.accept(handed_out)
        await rig.pending(4, "edit_song", {"n": 4})

        forwarded = [FORWARDED_CALL_1] if state is Call1.FORWARDED else []
        assert rig.fake.calls == forwarded
        assert upstream_task_names() == ([f"upstream:{RUN_ID}:call-1"] if forwarded else [])
        given_up = [GIVEN_UP_RESULT] if state is Call1.HANDED_OUT else []
        assert await accept_calls_up_to_call_4(rig) == [*given_up, "call-2", "call-3", "call-4"]
        rig.fake.release()
        await rig.barrier.settle()
        assert [call for call in rig.fake.calls if call[1] == {"n": 1}] == forwarded

    drive_barrier(scenario)


def test_a_cancelled_connection_task_ends_cancelled() -> None:
    async def scenario(rig: Rig) -> None:
        await rig.pending(1, "edit_song", {"n": 1})
        connection_task = task_named(CONNECTION_OF_CALL_1)
        connection_task.cancel()
        await yield_until(connection_task.done)
        assert connection_task.cancelled()

    drive_barrier(scenario)


async def hand_out_call_1_with_the_connection_task_held(
    rig: Rig, held: tuple[str, ...]
) -> tuple[RawClient, ToolCallEvent]:
    client = await rig.pending(1, "edit_song", {"n": 1})
    event = await rig.barrier.next_event()
    assert isinstance(event, ToolCallEvent)
    holding_loop().hold_wakeups(*held)
    return client, event


async def assert_given_up_before_the_connection_task_resumed(
    rig: Rig, event: ToolCallEvent
) -> None:
    rig.barrier.accept(event)
    assert upstream_task_names() == []
    assert rig.fake.calls == []
    assert await rig.barrier.next_event() == GIVEN_UP_RESULT


def test_a_call_accepted_after_the_watch_saw_the_close_is_not_forwarded_before_the_listener_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W8 a."""
    writes = RelayWrites(monkeypatch)

    async def scenario(rig: Rig) -> None:
        client, event = await hand_out_call_1_with_the_connection_task_held(
            rig, (CONNECTION_OF_CALL_1,)
        )
        client.disconnect()
        await yield_until(lambda: WATCH_OF_CALL_1 not in relay_tasks())
        assert CONNECTION_OF_CALL_1 in relay_tasks()
        await assert_given_up_before_the_connection_task_resumed(rig, event)
        holding_loop().release_wakeups()
        await yield_until(lambda: relay_tasks() == [])
        assert writes.to(client) == b""

    drive_barrier(scenario, loop_factory=HoldingSteppedLoop)


def test_a_call_accepted_after_the_relays_close_cancelled_its_connection_task_is_not_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W8 b."""
    writes = RelayWrites(monkeypatch)

    async def scenario(rig: Rig) -> None:
        client, event = await hand_out_call_1_with_the_connection_task_held(
            rig, (CONNECTION_OF_CALL_1, WATCH_OF_CALL_1)
        )
        closing = asyncio.create_task(rig.relay.close())
        await yield_until(lambda: task_named(CONNECTION_OF_CALL_1).cancelling() > 0)
        await assert_given_up_before_the_connection_task_resumed(rig, event)
        holding_loop().release_wakeups()
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            await closing
        assert writes.to(client) == b""

    drive_barrier(scenario, loop_factory=HoldingSteppedLoop)


async def forwarded_call_1(rig: Rig) -> RawClient:
    client = await rig.pending(1, "edit_song", {"n": 1})
    await rig.hand_out_and_accept()
    await rig.fake.forwarded(1)
    return client


async def answer_pending_once_idle() -> bool:
    await holding_loop().settled()
    return not task_named(ANSWER_OF_CALL_1).done()


RELEASED_OUTCOMES = [
    pytest.param(ToolOutcome(UPSTREAM_TEXT, False), id="success"),
    pytest.param(ToolOutcome("the song is locked", True), id="upstream-error"),
]


@pytest.mark.parametrize("outcome", RELEASED_OUTCOMES)
def test_a_result_is_released_to_the_cli_only_after_its_acceptance(
    outcome: ToolOutcome, monkeypatch: pytest.MonkeyPatch
) -> None:  # R1
    writes = RelayWrites(monkeypatch)

    async def scenario(rig: Rig) -> None:
        client = await forwarded_call_1(rig)
        rig.fake.release()

        result = await rig.barrier.next_event()

        assert result == ToolResultEvent(
            tool_use_id="call-1", content=outcome.content, is_error=outcome.is_error
        )
        assert await answer_pending_once_idle()
        assert writes.to(client) == b""
        rig.barrier.accept(result)
        assert await reply_of(client) == result_of(1, outcome.content, outcome.is_error)

    drive_barrier(scenario, gated_app(outcome=outcome), loop_factory=HoldingSteppedLoop)


def test_a_result_is_released_only_after_the_service_resumed_the_runner_past_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # R2
    writes = RelayWrites(monkeypatch)

    async def scenario(rig: Rig) -> None:
        frames = run_frames(
            alice_admitted_run(uuid.uuid4()),
            BarrierRunner(rig.barrier, final_after=1),
            RunConfig(),
            LoopClock(),
            RunRecords(),
            quarantine=quarantine_on(),
        )
        async with contextlib.aclosing(frames):
            rig.fake.release()
            taken = [await anext(frames)]
            client = await rig.pending(1, "edit_song", {"n": 1})

            assert await answer_pending_once_idle()
            assert writes.to(client) == b""

            taken.append(await anext(frames))
            assert await reply_of(client) == result_of(1)
            taken.extend([await anext(frames), await anext(frames)])

        started, called, result, succeeded = decoded_events(b"".join(taken))
        assert isinstance(started, RunStarted)
        assert isinstance(called, ToolCall)
        assert isinstance(result, ToolResult)
        assert (result.tool_use_id, result.content, result.is_error) == (
            "call-1",
            UPSTREAM_TEXT,
            False,
        )
        assert isinstance(succeeded, Succeeded)

    drive_barrier(scenario, loop_factory=HoldingSteppedLoop)


def test_a_result_the_run_refuses_is_never_released_and_its_call_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # R3
    writes = RelayWrites(monkeypatch)

    async def scenario(rig: Rig) -> None:
        stream = streamed(BarrierRunner(rig.barrier), RunConfig(max_bytes=2_048))
        rig.fake.release()
        client = await rig.pending(1, "edit_song", {"n": 1})

        started, called, failed = await settled_stream(stream)

        assert isinstance(started, RunStarted)
        assert isinstance(called, ToolCall)
        assert isinstance(failed, Failed)
        assert (failed.code, failed.message) == (
            FailureCode.LIMIT_EXCEEDED,
            TOO_MUCH_DATA_MESSAGE,
        )
        assert await reply_of(client) == refused(1)
        assert OVERSIZED_MARKER.encode() not in writes.to(client)
        assert len(rig.fake.calls) == 1

    oversized = ToolOutcome(OVERSIZED_MARKER * 128, is_error=False)
    drive_barrier(scenario, gated_app(outcome=oversized))


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(ToolOutcome(f"echo {CANARY} end", False), id="whole-token-in-success"),
        pytest.param(ToolOutcome(f"locked {CANARY_SIGNATURE}", True), id="segment-in-error"),
    ],
)
def test_a_result_whose_text_carries_the_capability_never_becomes_an_event_and_fails_the_run(
    outcome: ToolOutcome, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:  # R4 a, b
    writes = RelayWrites(monkeypatch)

    async def scenario(rig: Rig) -> None:
        stream = streamed(BarrierRunner(rig.barrier))
        rig.fake.release()
        client = await rig.pending(1, "edit_song", {"n": 1})

        raw = await within_the_bound(stream)

        started, called, failed = decoded_events(raw)
        assert isinstance(started, RunStarted)
        assert isinstance(called, ToolCall)
        assert isinstance(failed, Failed)
        assert (failed.code, failed.message) == (FailureCode.PROVIDER_ERROR, RUNNER_FAILED_MESSAGE)
        assert await client.delivery() is Delivery.ABSENT
        assert CANARY_SIGNATURE.encode() not in raw
        assert all(CANARY_SIGNATURE.encode() not in writes.to(each) for each in rig.connected)
        assert writes.to(client) == b""

    drive_barrier(scenario, gated_app(outcome=outcome))

    assert [kind for _, kind, _ in failure_lines(caplog)] == ["CapabilityReflected"]
    assert CANARY_SIGNATURE not in caplog.text


def test_a_capability_in_the_result_of_a_call_the_cli_gave_up_on_still_fails_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:  # R4 c
    async def scenario(rig: Rig) -> None:
        await forwarded_call_1(rig)
        rig.barrier.cancel(1)
        rig.fake.release()
        await rig.barrier.settle()

        with pytest.raises(CapabilityReflected):
            await rig.barrier.next_event()

    drive_barrier(scenario, gated_app(outcome=ToolOutcome(CANARY_SIGNATURE, False)))

    assert CANARY_SIGNATURE not in caplog.text


@pytest.mark.parametrize(
    ("failure", "failure_class"),
    [
        pytest.param(UpstreamCallLost, "UpstreamCallLost", id="call-lost"),
        pytest.param(UPSTREAM_BROKE, "UpstreamBroke", id="another-failure"),
    ],
)
def test_an_upstream_failure_is_the_services_error_result_without_the_apps_text(
    failure: Callable[[], BaseException],
    failure_class: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # R5
    writes = RelayWrites(monkeypatch)

    async def scenario(rig: Rig) -> None:
        client = await forwarded_call_1(rig)
        rig.fake.release()

        result = await rig.barrier.next_event()

        assert result == ToolResultEvent(
            tool_use_id="call-1", content=CALL_FAILED_TEXT, is_error=True
        )
        rig.barrier.accept(result)
        assert await reply_of(client) == result_of(1, CALL_FAILED_TEXT, is_error=True)
        assert UPSTREAM_FAILURE_MARKER.encode() not in writes.to(client)
        assert len(rig.fake.calls) == 1

    drive_barrier(scenario, gated_app(failure))

    assert [kind for _, kind, _ in failure_lines(caplog)] == [failure_class]
    assert UPSTREAM_FAILURE_MARKER not in caplog.text


async def cancelled_while_forwarded(rig: Rig) -> ToolResultEvent | None:
    await rig.cancel(1)
    rig.fake.release()
    await rig.barrier.settle()
    return None


async def cancelled_while_the_result_is_queued(rig: Rig) -> ToolResultEvent | None:
    rig.fake.release()
    await holding_loop().settled()
    cancel_owner_answers()
    return None


async def cancelled_while_the_result_is_handed_out(rig: Rig) -> ToolResultEvent | None:
    rig.fake.release()
    result = await rig.barrier.next_event()
    assert isinstance(result, ToolResultEvent)
    await rig.cancel(1)
    return result


@pytest.mark.parametrize(
    "give_up",
    [
        cancelled_while_forwarded,
        cancelled_while_the_result_is_queued,
        cancelled_while_the_result_is_handed_out,
    ],
)
def test_a_call_the_cli_gave_up_on_still_shows_its_result_to_the_app_and_gets_no_reply(
    give_up: Callable[[Rig], Awaitable[ToolResultEvent | None]], monkeypatch: pytest.MonkeyPatch
) -> None:  # R6
    writes = RelayWrites(monkeypatch)

    async def scenario(rig: Rig) -> None:
        retrievals = count_retrievals()
        client = await forwarded_call_1(rig)

        handed_out = await give_up(rig)

        await rig.pending(2, "edit_song", {"n": 2})
        result = handed_out or await rig.barrier.next_event()
        assert result == ToolResultEvent(
            tool_use_id="call-1", content=UPSTREAM_TEXT, is_error=False
        )
        rig.barrier.accept(result)
        assert await client.delivery() is Delivery.ABSENT
        assert writes.to(client) == b""
        assert retrievals[f"upstream:{RUN_ID}:call-1"] == 1
        assert (await rig.barrier.next_event()).tool_use_id == "call-2"

    drive_barrier(scenario, loop_factory=HoldingSteppedLoop)


async def stopped_while_the_forward_runs(
    rig: Rig, writes: RelayWrites, caplog: pytest.LogCaptureFixture, retrievals: Counter[str]
) -> RawClient:
    client = await forwarded_call_1(rig)
    rig.barrier.stop()
    settling = settling_with_evidence(rig, caplog, retrievals)
    await yield_until(lambda: writes.to(client) != b"")
    rig.fake.release()
    async with asyncio.timeout(TEST_WAIT_SECONDS):
        settled = await settling
    assert (settled.logged_classes, settled.retrievals) == (["UpstreamBroke"], 1)
    return client


async def stopped_while_the_result_is_queued(
    rig: Rig, writes: RelayWrites, caplog: pytest.LogCaptureFixture, retrievals: Counter[str]
) -> RawClient:
    client = await forwarded_call_1(rig)
    rig.fake.release()
    await holding_loop().settled()
    rig.barrier.stop()
    return client


async def stopped_while_the_result_is_handed_out(
    rig: Rig, writes: RelayWrites, caplog: pytest.LogCaptureFixture, retrievals: Counter[str]
) -> RawClient:
    client = await forwarded_call_1(rig)
    rig.fake.release()
    result = await rig.barrier.next_event()
    rig.barrier.stop()
    with pytest.raises(ToolCallsMisused):
        rig.barrier.accept(result)
    return client


@pytest.mark.parametrize(
    ("stopping", "failure"),
    [
        pytest.param(stopped_while_the_forward_runs, UPSTREAM_BROKE, id="forward-running"),
        pytest.param(stopped_while_the_result_is_queued, None, id="result-queued"),
        pytest.param(stopped_while_the_result_is_handed_out, None, id="result-handed-out"),
    ],
)
def test_stop_refuses_every_reply_not_yet_released(
    stopping: Callable[..., Awaitable[RawClient]],
    failure: Callable[[], BaseException] | None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # R7
    writes = RelayWrites(monkeypatch)

    async def scenario(rig: Rig) -> None:
        client = await stopping(rig, writes, caplog, count_retrievals())

        assert await reply_of(client) == refused(1)
        written = writes.to(client)
        assert UPSTREAM_TEXT.encode() not in written
        assert UPSTREAM_FAILURE_MARKER.encode() not in written

    with caplog.at_level(logging.ERROR):
        drive_barrier(scenario, gated_app(failure), loop_factory=HoldingSteppedLoop)


def test_the_hand_out_rule_over_a_result_accepts_exactly_the_handed_out_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # R8
    writes = RelayWrites(monkeypatch)

    async def scenario(rig: Rig) -> None:
        client = await rig.pending(1, "edit_song", {"n": 1})
        call_event = await rig.hand_out_and_accept()
        rig.fake.release()
        await rig.barrier.settle()
        await rig.pending(2, "edit_song", {"n": 2})
        result = await rig.barrier.next_event()

        with pytest.raises(ToolCallsMisused):
            await rig.barrier.next_event()
        with pytest.raises(ToolCallsMisused):
            rig.barrier.accept(call_event)
        another_result = ToolResultEvent(
            tool_use_id="call-2", content=UPSTREAM_TEXT, is_error=False
        )
        with pytest.raises(ToolCallsMisused):
            rig.barrier.accept(another_result)
        assert writes.to(client) == b""
        assert len(rig.fake.calls) == 1

        rig.barrier.accept(result)
        assert await reply_of(client) == result_of(1)
        written = writes.to(client)
        with pytest.raises(ToolCallsMisused):
            rig.barrier.accept(result)
        assert writes.to(client) == written
        assert (await rig.barrier.next_event()).tool_use_id == "call-2"

    drive_barrier(scenario)


def calls_through_a_direct_await(
    barrier: ToolCallBarrier,
) -> asyncio.Task[Reply | Unanswered]:
    return asyncio.Task(
        barrier.call(1, EDIT_SONG),
        loop=asyncio.get_running_loop(),
        name="waiter",
        eager_start=True,
    )


def calls_through_gather(barrier: ToolCallBarrier) -> asyncio.Task[Reply | Unanswered]:
    inner = asyncio.Task(
        barrier.call(1, EDIT_SONG),
        loop=asyncio.get_running_loop(),
        name="waiter:call",
        eager_start=True,
    )

    async def gathered() -> Reply | Unanswered:
        (reply,) = await asyncio.gather(inner)
        return reply

    return asyncio.Task(
        gathered(), loop=asyncio.get_running_loop(), name="waiter", eager_start=True
    )


async def given_up_by_a_direct_await(
    rig: Rig, writes: RelayWrites
) -> Callable[[], Awaitable[None]]:
    waiter = calls_through_a_direct_await(rig.barrier)
    event = await rig.barrier.next_event()
    assert isinstance(event, ToolCallEvent)
    holding_loop().hold_wakeups("waiter")
    rig.barrier.accept(event)
    waiter.cancel()

    async def waiter_ends_cancelled() -> None:
        assert isinstance(await ending_of(waiter), asyncio.CancelledError)

    return waiter_ends_cancelled


async def given_up_through_gather(rig: Rig, writes: RelayWrites) -> Callable[[], Awaitable[None]]:
    waiter = calls_through_gather(rig.barrier)
    event = await rig.barrier.next_event()
    assert isinstance(event, ToolCallEvent)
    holding_loop().hold_wakeups("waiter", "waiter:call")
    rig.barrier.accept(event)
    waiter.cancel()

    async def waiter_ends_cancelled() -> None:
        assert isinstance(await ending_of(waiter), asyncio.CancelledError)

    return waiter_ends_cancelled


async def given_up_by_the_relays_close(
    rig: Rig, writes: RelayWrites
) -> Callable[[], Awaitable[None]]:
    client = await forwarded_call_1(rig)
    holding_loop().hold_wakeups(
        CONNECTION_OF_CALL_1, f"{CONNECTION_OF_CALL_1}:answer", WATCH_OF_CALL_1
    )
    closing = asyncio.create_task(rig.relay.close())
    await yield_until(lambda: task_named(CONNECTION_OF_CALL_1).cancelling() > 0)

    async def close_ends_without_a_byte() -> None:
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            await closing
        assert writes.to(client) == b""

    return close_ends_without_a_byte


@pytest.mark.parametrize(
    ("give_up", "failure", "expected_result"),
    [
        pytest.param(
            given_up_by_a_direct_await,
            UPSTREAM_BROKE,
            ToolResultEvent(tool_use_id="call-1", content=CALL_FAILED_TEXT, is_error=True),
            id="direct-await-failure",
        ),
        pytest.param(
            given_up_through_gather,
            None,
            ToolResultEvent(tool_use_id="call-1", content=UPSTREAM_TEXT, is_error=False),
            id="gather-success",
        ),
        pytest.param(
            given_up_by_the_relays_close,
            None,
            ToolResultEvent(tool_use_id="call-1", content=UPSTREAM_TEXT, is_error=False),
            id="relay-close-success",
        ),
    ],
)
def test_a_give_up_recorded_while_its_waiter_has_not_resumed_releases_nothing(
    give_up: Callable[[Rig, RelayWrites], Awaitable[Callable[[], Awaitable[None]]]],
    failure: Callable[[], BaseException] | None,
    expected_result: ToolResultEvent,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # R9
    writes = RelayWrites(monkeypatch)

    async def scenario(rig: Rig) -> None:
        retrievals = count_retrievals()
        waiter_ends = await give_up(rig, writes)
        rig.fake.release()
        await rig.barrier.settle()
        result = await rig.barrier.next_event()
        assert result == expected_result

        rig.barrier.accept(result)

        assert holding_loop().held_wakeups > 0
        holding_loop().release_wakeups()
        await waiter_ends()
        assert retrievals[f"upstream:{RUN_ID}:call-1"] == 1
        arrive(rig.barrier, EDIT_SONG, 2)
        assert (await rig.barrier.next_event()).tool_use_id == "call-2"

    with caplog.at_level(logging.ERROR):
        drive_barrier(scenario, gated_app(failure), loop_factory=HoldingSteppedLoop)

    assert [kind for _, kind, _ in failure_lines(caplog)] == (["UpstreamBroke"] if failure else [])
    assert UPSTREAM_FAILURE_MARKER not in caplog.text
