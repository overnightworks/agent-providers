"""A run's record: written as its events are accepted, ended when its runner is closed.

Decision record 0001, A1a2 ruling, items 7 to 13. The scenarios of the first
rules drive ``run_frames`` with a ``StepClock`` and a ``GatedRunner`` in a
``SteppedLoop`` and read the store through ``Drive.status``. Rule 11 drives the
app's ASGI behaviour with a hand-built scope, so its response is the real one and
the test holds no reference to the run's generator. The served cases use a real
socket.
"""

from __future__ import annotations

import asyncio
import enum
import gc
import logging
import math
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime

import anyio
import pytest
from service_support import (
    ALICE,
    SONGMAKER,
    BrokenClock,
    ClientSocket,
    Drive,
    GatedRunner,
    ScriptedRunner,
    StepClock,
    TokenMint,
    alice_admitted_run,
    driven,
    get_run,
    in_loop,
    instant,
    post_run,
    reaches_text,
    respond_through_the_app,
    run_headers,
    served,
    service_config,
)
from starlette.requests import ClientDisconnect

from agent_client.run_stream import ViolationKind
from agent_client.wire import (
    ClientLoss,
    FailureCode,
    RejectionCode,
    RunStarted,
    RunState,
    RunStatus,
)
from agent_providers.events import (
    AssistantTextEvent,
    FinalEvent,
    StreamEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_service.app import create_app
from agent_service.config import RunConfig
from agent_service.identity import Principal
from agent_service.run_events import run_frames
from agent_service.run_liveness import LoopClock
from agent_service.run_records import RunRecorder, RunRecords
from agent_service.runners import Runners

TEXT = AssistantTextEvent(text="Let me read the song.")
FINAL = FinalEvent(text="Done.")
READ_SCRIPT = (
    TEXT,
    ToolCallEvent(tool_use_id="call-1", name="read_song", input={"song_id": 7}),
    ToolResultEvent(tool_use_id="call-1", content="Verse one"),
    FINAL,
)
EXHAUSTED = None
RECORDING_FAILURE_CANARY = "RECORDING_FAILURE_CANARY"
ALL_THE_WAY_TO_THE_MAXIMUM = 600
STALL_PAST_THE_MAXIMUM = RunConfig(stall_timeout_s=590, idle_timeout_s=595)


def _call(tool_use_id: str, name: str = "read_song") -> ToolCallEvent:
    return ToolCallEvent(tool_use_id=tool_use_id, name=name, input={})


def _release(runner: GatedRunner, script: tuple[StreamEvent | Exception | None, ...]) -> None:
    for item in script:
        runner.exhaust() if item is EXHAUSTED else runner.release(item)


def _assert_recorded(
    status: RunStatus | None,
    state: RunState,
    *,
    events: int,
    at: float,
    code: FailureCode | None = None,
    lost: ClientLoss | None = None,
) -> None:
    assert status is not None
    ended_at: datetime | None = None if state is RunState.RUNNING else instant(at)
    assert (status.state, status.failure_code, status.events, status.client_lost) == (
        state,
        code,
        events,
        lost,
    )
    assert status.ended_at == ended_at


@pytest.mark.parametrize(
    ("runs", "client_reads", "script", "released_at", "state", "code", "events", "ended_at"),
    [
        pytest.param(
            RunConfig(), True, READ_SCRIPT, 5, RunState.SUCCEEDED, None, 5, 5, id="succeeded"
        ),
        pytest.param(
            RunConfig(),
            True,
            (TEXT, RuntimeError("the CLI died")),
            5,
            RunState.FAILED,
            FailureCode.PROVIDER_ERROR,
            3,
            5,
            id="provider-raised",
        ),
        pytest.param(
            RunConfig(),
            True,
            (TEXT, EXHAUSTED),
            5,
            RunState.FAILED,
            FailureCode.PROVIDER_ERROR,
            3,
            5,
            id="provider-exhausted",
        ),
        pytest.param(
            RunConfig(),
            True,
            (ToolCallEvent(tool_use_id="c", name="edit_song", input={"score": math.nan}),),
            5,
            RunState.FAILED,
            FailureCode.PROVIDER_ERROR,
            2,
            5,
            id="unsendable-event-is-not-counted",
        ),
        pytest.param(
            RunConfig(max_tool_calls=1),
            True,
            (
                _call("c1"),
                ToolResultEvent(tool_use_id="c1", content="ok"),
                _call("c2"),
            ),
            5,
            RunState.FAILED,
            FailureCode.LIMIT_EXCEEDED,
            4,
            5,
            id="over-the-tool-call-budget",
        ),
        pytest.param(
            RunConfig(max_bytes=10),
            True,
            (TEXT,),
            5,
            RunState.FAILED,
            FailureCode.LIMIT_EXCEEDED,
            2,
            5,
            id="over-the-byte-budget",
        ),
        pytest.param(
            STALL_PAST_THE_MAXIMUM,
            False,
            (TEXT,),
            20,
            RunState.FAILED,
            FailureCode.LIMIT_EXCEEDED,
            4,
            600,
            id="maximum-with-a-frame-pending-in-the-slot",
        ),
        pytest.param(
            RunConfig(),
            True,
            (),
            0,
            RunState.FAILED,
            FailureCode.IDLE_TIMEOUT,
            9,
            120,
            id="idle-timeout",
        ),
    ],
)
@in_loop
async def test_each_ending_of_a_run_is_recorded_at_the_instant_its_runner_is_closed(
    runs, client_reads, script, released_at, state, code, events, ended_at
):
    async with driven(runs=runs, read=client_reads) as drive:
        if not client_reads:
            drive.took(await anext(drive.frames))
        await drive.run_to(released_at)
        _release(drive.runner, script)
        await drive.run_to(ALL_THE_WAY_TO_THE_MAXIMUM)

        _assert_recorded(drive.status(), state, code=code, events=events, at=ended_at)
        assert drive.runner.closes == 1


@in_loop
async def test_a_stalled_run_reads_running_with_the_events_it_accepted():
    async with driven(read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(16)

        _assert_recorded(drive.status(), RunState.RUNNING, events=2, at=0)


@in_loop
async def test_a_stream_closed_before_its_first_step_leaves_no_record_and_never_runs_the_runner():
    async with driven(read=False) as drive:
        await drive.frames.aclose()

        assert drive.status() is None
        assert drive.runner.steps == 0


@in_loop
async def test_the_answer_of_a_stalled_client_is_recorded_when_it_arrives_not_when_it_is_dropped():
    async with driven(read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(20)
        drive.runner.release(FINAL)
        await drive.run_to(20)
        _assert_recorded(drive.status(), RunState.SUCCEEDED, events=3, at=20)

        await drive.run_to(45)
        _assert_recorded(
            drive.status(), RunState.SUCCEEDED, events=3, at=20, lost=ClientLoss.DROPPED
        )


@in_loop
async def test_a_run_ended_by_the_idle_timeout_reads_running_until_its_runner_is_closed():
    runner = GatedRunner()
    runner.hold_close()
    async with driven(runner) as drive:
        await drive.run_to(125)
        _assert_recorded(drive.status(), RunState.RUNNING, events=9, at=0)

        drive.clock.advance_to(130)
        runner.release_close()
        await drive.finished()

        _assert_recorded(
            drive.status(),
            RunState.FAILED,
            code=FailureCode.IDLE_TIMEOUT,
            events=9,
            at=130,
        )


def _frames_of_a_run_without_a_runner(
    records: RunRecords,
) -> tuple[uuid.UUID, AsyncGenerator[bytes, None]]:
    run_id = uuid.uuid4()
    frames = run_frames(
        alice_admitted_run(run_id), None, SONGMAKER, RunConfig(), LoopClock(), records
    )
    return run_id, frames


def test_a_run_without_a_runner_is_over_before_its_first_frame_is_handed_out():
    records = RunRecords()

    async def scenario() -> RunStatus | None:
        run_id, frames = _frames_of_a_run_without_a_runner(records)
        await anext(frames)
        status = records.read(run_id, "songmaker", ALICE)
        await frames.aclose()
        return status

    status = asyncio.run(scenario())

    assert status is not None
    assert (status.state, status.failure_code, status.events) == (
        RunState.FAILED,
        FailureCode.PROVIDER_UNAVAILABLE,
        2,
    )
    assert status.client_lost is None


def test_a_run_without_a_runner_closed_at_its_first_frame_keeps_its_ending_and_records_the_loss():
    records = RunRecords()

    async def scenario() -> RunStatus | None:
        run_id, frames = _frames_of_a_run_without_a_runner(records)
        await anext(frames)
        await frames.aclose()
        return records.read(run_id, "songmaker", ALICE)

    status = asyncio.run(scenario())

    assert status is not None
    assert (status.state, status.failure_code, status.client_lost) == (
        RunState.FAILED,
        FailureCode.PROVIDER_UNAVAILABLE,
        ClientLoss.DISCONNECTED,
    )


@pytest.mark.parametrize(
    ("tool", "state", "code"),
    [
        pytest.param("read_song", RunState.CANCELLED, None, id="read-only-call"),
        pytest.param("edit_song", RunState.FAILED, FailureCode.INTERRUPTED, id="side-effecting"),
    ],
)
@in_loop
async def test_a_client_leaving_after_a_tool_call_ends_the_run_by_the_effect_of_its_calls(
    tool, state, code
):
    async with driven(read=False) as drive:
        drive.took(await anext(drive.frames))
        drive.runner.release(_call("c", tool))
        drive.took(await anext(drive.frames))
        await drive.frames.aclose()

        _assert_recorded(
            drive.status(), state, code=code, events=2, at=0, lost=ClientLoss.DISCONNECTED
        )
        assert drive.types == ["run_started", "tool_call"]


async def _client_leaves_mid_run(drive: Drive) -> None:
    drive.took(await anext(drive.frames))


async def _client_takes_nothing_until_dropped(drive: Drive) -> None:
    drive.took(await anext(drive.frames))
    await drive.run_to(45)


async def _answer_arrives_behind_a_client_that_is_then_dropped(drive: Drive) -> None:
    drive.took(await anext(drive.frames))
    await drive.run_to(20)
    drive.runner.release(FINAL)
    await drive.run_to(45)


async def _client_leaves_after_the_answer_was_queued(drive: Drive) -> None:
    drive.took(await anext(drive.frames))
    drive.runner.release(FINAL)
    await drive.clock.parked()


async def _supervision_fails(drive: Drive) -> None:
    drive.took(await anext(drive.frames))
    with pytest.raises(RuntimeError, match="the clock broke"):
        await anext(drive.frames)
    assert drive.end_of_stream() == [ViolationKind.TRUNCATED]


@pytest.mark.parametrize(
    ("clock", "scenario", "state", "events", "lost", "ended_at"),
    [
        pytest.param(
            StepClock,
            _client_leaves_mid_run,
            RunState.CANCELLED,
            1,
            ClientLoss.DISCONNECTED,
            0,
            id="client-leaves-while-the-run-is-going",
        ),
        pytest.param(
            StepClock,
            _client_takes_nothing_until_dropped,
            RunState.CANCELLED,
            2,
            ClientLoss.DROPPED,
            45,
            id="client-dropped-with-nothing-decided",
        ),
        pytest.param(
            StepClock,
            _answer_arrives_behind_a_client_that_is_then_dropped,
            RunState.SUCCEEDED,
            3,
            ClientLoss.DROPPED,
            20,
            id="client-dropped-while-a-decided-ending-waits",
        ),
        pytest.param(
            StepClock,
            _client_leaves_after_the_answer_was_queued,
            RunState.SUCCEEDED,
            2,
            ClientLoss.DISCONNECTED,
            0,
            id="client-leaves-after-the-run-is-over",
        ),
        pytest.param(
            BrokenClock,
            _supervision_fails,
            RunState.CANCELLED,
            1,
            ClientLoss.DROPPED,
            0,
            id="supervision-fails",
        ),
    ],
)
@in_loop
async def test_the_first_cause_of_losing_the_client_stays_however_often_the_stream_is_closed(
    clock, scenario, state, events, lost, ended_at
):
    async with driven(clock=clock(), read=False) as drive:
        await scenario(drive)
        for _ in range(2):
            await drive.frames.aclose()

            _assert_recorded(drive.status(), state, events=events, at=ended_at, lost=lost)


@in_loop
async def test_a_client_leaving_during_the_held_close_of_a_drop_stays_dropped():
    runner = GatedRunner()
    runner.hold_close()
    async with driven(runner, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(45)
        closing = asyncio.create_task(drive.frames.aclose())
        await drive.clock.parked()
        _assert_recorded(drive.status(), RunState.RUNNING, events=2, at=0, lost=ClientLoss.DROPPED)

        runner.release_close()
        await closing

        _assert_recorded(
            drive.status(), RunState.CANCELLED, events=2, at=45, lost=ClientLoss.DROPPED
        )


@in_loop
async def test_a_close_that_raises_during_a_drop_still_records_the_ending_and_logs_once(caplog):
    runner = GatedRunner()
    runner.close_error = OSError("the process would not die")
    async with driven(runner, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(45)
        await drive.frames.aclose()

    _assert_recorded(drive.status(), RunState.CANCELLED, events=2, at=45, lost=ClientLoss.DROPPED)
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert [str(drive.run_id) in error.getMessage() for error in errors] == [True]


class _WatchedRecords(RunRecords):
    """The store of one app, which also remembers which runs began in it."""

    def __init__(self, clock: StepClock) -> None:
        super().__init__(lambda: instant(clock.now))
        self.begun: list[uuid.UUID] = []

    def begin(self, started: RunStarted, app: str, principal: Principal) -> RunRecorder:
        self.begun.append(started.run_id)
        return super().begin(started, app, principal)


@asynccontextmanager
async def _answering(
    client: ClientSocket,
    monkeypatch: pytest.MonkeyPatch,
    runner: GatedRunner | None = None,
) -> AsyncIterator[tuple[Drive, _WatchedRecords, asyncio.Task[None]]]:
    clock = StepClock()
    records = _WatchedRecords(clock)
    async with driven(runner, clock=clock, records=records, read=False) as drive:
        response = await respond_through_the_app(drive, client, monkeypatch)
        yield drive, records, response


def _status_of_the_run(drive: Drive, records: _WatchedRecords) -> RunStatus | None:
    return drive.status(records.begun[0])


def _assert_closed_cleanly(drive: Drive) -> None:
    assert drive.runner.closes == 1
    assert asyncio.all_tasks() == {asyncio.current_task()}


@in_loop
async def test_a_client_leaving_while_the_run_waits_for_its_next_frame_closes_the_run(monkeypatch):
    client = ClientSocket()
    async with _answering(client, monkeypatch) as (drive, records, response):
        await drive.run_to(5)
        client.leave()
        await response

        _assert_recorded(
            _status_of_the_run(drive, records),
            RunState.CANCELLED,
            events=1,
            at=5,
            lost=ClientLoss.DISCONNECTED,
        )
        _assert_closed_cleanly(drive)


@in_loop
async def test_a_client_leaving_behind_a_blocked_send_closes_the_run_with_the_generator_paused(
    monkeypatch,
):
    client = ClientSocket(blocks_the_second_chunk=True)
    async with _answering(client, monkeypatch) as (drive, records, response):
        await drive.run_to(15)
        client.leave()
        await response

        _assert_recorded(
            _status_of_the_run(drive, records),
            RunState.CANCELLED,
            events=2,
            at=15,
            lost=ClientLoss.DISCONNECTED,
        )
        _assert_closed_cleanly(drive)


@in_loop
async def test_a_response_whose_start_never_went_out_leaves_no_run_behind(monkeypatch):
    client = ClientSocket(suspends_the_start=True, leaves_at_once=True)
    async with _answering(client, monkeypatch) as (drive, records, response):
        await response

        assert records.begun == []
        assert (drive.runner.steps, drive.runner.closes) == (0, 0)
        assert asyncio.all_tasks() == {asyncio.current_task()}


@in_loop
async def test_a_client_gone_at_the_first_receive_cuts_the_run_at_its_first_wait(monkeypatch):
    client = ClientSocket(leaves_at_once=True)
    async with _answering(client, monkeypatch) as (drive, records, response):
        await response

        _assert_recorded(
            _status_of_the_run(drive, records),
            RunState.CANCELLED,
            events=1,
            at=0,
            lost=ClientLoss.DISCONNECTED,
        )
        _assert_closed_cleanly(drive)


@in_loop
async def test_a_send_failing_on_a_modern_asgi_server_closes_the_run_and_disconnects(monkeypatch):
    client = ClientSocket(spec_version="2.4", second_chunk_raises=OSError("broken pipe"))
    async with _answering(client, monkeypatch) as (drive, records, response):
        await drive.run_to(14)
        _assert_recorded(_status_of_the_run(drive, records), RunState.RUNNING, events=1, at=0)

        drive.clock.advance_to(15)
        with pytest.raises(ClientDisconnect):
            await response

        _assert_recorded(
            _status_of_the_run(drive, records),
            RunState.CANCELLED,
            events=2,
            at=15,
            lost=ClientLoss.DISCONNECTED,
        )
        _assert_closed_cleanly(drive)


@in_loop
async def test_an_error_from_send_closes_the_run_and_reaches_the_caller_unchanged(monkeypatch):
    client = ClientSocket(second_chunk_raises=RuntimeError("the transport broke"))
    async with _answering(client, monkeypatch) as (drive, records, response):
        drive.clock.advance_to(15)
        with pytest.raises(RuntimeError, match="the transport broke"):
            await response

        _assert_recorded(
            _status_of_the_run(drive, records),
            RunState.CANCELLED,
            events=2,
            at=15,
            lost=ClientLoss.DISCONNECTED,
        )
        _assert_closed_cleanly(drive)


async def _client_leaves(client: ClientSocket, response: asyncio.Task[None]) -> None:
    client.leave()


async def _response_is_cancelled(client: ClientSocket, response: asyncio.Task[None]) -> None:
    response.cancel()


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@in_loop
async def test_a_response_cancelled_again_during_the_held_close_still_waits_for_it(
    monkeypatch, spec_version
):
    runner = GatedRunner()
    runner.hold_close()
    client = ClientSocket(spec_version=spec_version, blocks_the_second_chunk=True)
    async with _answering(client, monkeypatch, runner) as (drive, records, response):
        await drive.run_to(15)
        response.cancel()
        await drive.clock.parked()
        assert not response.done()

        for _ in range(3):
            response.cancel()
            await asyncio.sleep(0)
            await drive.clock.parked()
            assert not response.done()

        drive.clock.advance_to(20)
        runner.release_close()
        with pytest.raises(asyncio.CancelledError):
            await response

        _assert_recorded(
            _status_of_the_run(drive, records),
            RunState.CANCELLED,
            events=2,
            at=20,
            lost=ClientLoss.DISCONNECTED,
        )
        _assert_closed_cleanly(drive)


@pytest.mark.parametrize(
    ("spec_version", "ends_the_response", "outcome"),
    [
        pytest.param("2.3", _client_leaves, None, id="disconnect-below-spec-2.4"),
        pytest.param(
            "2.3", _response_is_cancelled, asyncio.CancelledError, id="cancelled-below-2.4"
        ),
        pytest.param(
            "2.4", _response_is_cancelled, asyncio.CancelledError, id="cancelled-from-2.4"
        ),
    ],
)
@in_loop
async def test_a_response_that_ends_waits_for_the_held_close_of_its_run_before_it_returns(
    monkeypatch, spec_version, ends_the_response, outcome
):
    runner = GatedRunner()
    runner.hold_close()
    client = ClientSocket(spec_version=spec_version, blocks_the_second_chunk=True)
    async with _answering(client, monkeypatch, runner) as (drive, records, response):
        await drive.run_to(15)
        await ends_the_response(client, response)
        await drive.clock.parked()

        assert not response.done()
        _assert_recorded(
            _status_of_the_run(drive, records),
            RunState.RUNNING,
            events=2,
            at=0,
            lost=ClientLoss.DISCONNECTED,
        )

        drive.clock.advance_to(20)
        runner.release_close()
        if outcome is None:
            await response
        else:
            with pytest.raises(outcome):
                await response

        _assert_recorded(
            _status_of_the_run(drive, records),
            RunState.CANCELLED,
            events=2,
            at=20,
            lost=ClientLoss.DISCONNECTED,
        )
        _assert_closed_cleanly(drive)


@in_loop
async def test_a_run_whose_terminal_was_accepted_is_recorded_when_the_held_close_is_released():
    runner = GatedRunner()
    runner.hold_close()
    async with driven(runner, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(5)
        runner.release(FINAL)
        await drive.run_to(6)
        closing = asyncio.create_task(drive.frames.aclose())
        await drive.clock.parked()

        _assert_recorded(
            drive.status(), RunState.RUNNING, events=2, at=0, lost=ClientLoss.DISCONNECTED
        )

        drive.clock.advance_to(8)
        runner.release_close()
        await closing

        _assert_recorded(
            drive.status(), RunState.SUCCEEDED, events=2, at=8, lost=ClientLoss.DISCONNECTED
        )
        drive.assert_nothing_left()


@in_loop
async def test_a_closer_cut_by_its_scope_still_waits_for_the_held_close_of_the_run():
    runner = GatedRunner()
    runner.hold_close()
    async with driven(runner, read=False) as drive:
        drive.took(await anext(drive.frames))
        scope = anyio.CancelScope()

        async def close_inside_the_scope() -> None:
            with scope:
                await drive.frames.aclose()

        closing = asyncio.create_task(close_inside_the_scope())
        await drive.clock.parked()
        scope.cancel()
        await drive.clock.parked()
        assert not closing.done()

        runner.release_close()
        await closing

        assert drive.status().state is RunState.CANCELLED
        drive.assert_nothing_left()


class _RecorderWhoseEndingFails:
    def __init__(self, recorder: RunRecorder, before_failing: Callable[[], None]) -> None:
        self._recorder = recorder
        self._before_failing = before_failing

    def accepted(self, event) -> None:
        self._recorder.accepted(event)

    def lost(self, reason: ClientLoss) -> None:
        self._recorder.lost(reason)

    def over(self, fallback=None) -> None:
        self._before_failing()
        raise RuntimeError(RECORDING_FAILURE_CANARY)


class _RecordsWhoseEndingFails(RunRecords):
    def __init__(self, before_failing: Callable[[], None] = lambda: None) -> None:
        super().__init__()
        self._before_failing = before_failing

    def begin(self, started, app, principal):
        return _RecorderWhoseEndingFails(
            super().begin(started, app, principal), self._before_failing
        )


@in_loop
async def test_a_recording_defect_surfaces_as_a_supervision_failure():
    async with driven(records=_RecordsWhoseEndingFails(), read=False) as drive:
        drive.took(await anext(drive.frames))
        drive.runner.release(FINAL)

        with pytest.raises(RuntimeError, match=RECORDING_FAILURE_CANARY):
            await anext(drive.frames)


class _CancelsTheCloser(enum.Enum):
    NEVER = enum.auto()
    WHILE_THE_CLOSE_IS_HELD = enum.auto()
    WHILE_THE_RECORD_ENDS = enum.auto()


async def _close_a_run(
    clock: StepClock, cancels: _CancelsTheCloser, *, cleanup_fails: bool
) -> tuple[str, uuid.UUID]:
    """Close a run whose close is held (or whose record ending fails); how its closer ended."""
    closers: list[asyncio.Task[None]] = []
    cancels_while_the_record_ends = cancels is _CancelsTheCloser.WHILE_THE_RECORD_ENDS
    records = (
        _RecordsWhoseEndingFails(
            lambda: closers[0].cancel() if cancels_while_the_record_ends else None
        )
        if cleanup_fails
        else None
    )
    runner = GatedRunner()
    holds_close = not cancels_while_the_record_ends
    if holds_close:
        runner.hold_close()
    async with driven(runner, clock=clock, records=records, read=False) as drive:
        drive.took(await anext(drive.frames))
        if holds_close:
            await drive.clock.parked()
        closers.append(asyncio.create_task(drive.frames.aclose()))
        await drive.clock.parked()
        if cancels is _CancelsTheCloser.WHILE_THE_CLOSE_IS_HELD:
            closers[0].cancel()
            await drive.clock.parked()
        runner.release_close()
        await asyncio.wait(closers)
    return _outcome_of(closers[0]), drive.run_id


def _outcome_of(task: asyncio.Task[None]) -> str:
    if task.cancelled():
        return "cancelled"
    failure = task.exception()
    return type(failure).__name__ if failure else "returned"


@pytest.mark.parametrize(
    ("clock", "cancels", "cleanup_fails", "propagates", "logged"),
    [
        pytest.param(
            StepClock,
            _CancelsTheCloser.NEVER,
            True,
            "RuntimeError",
            [],
            id="cleanup-failure-propagates",
        ),
        pytest.param(
            StepClock,
            _CancelsTheCloser.WHILE_THE_RECORD_ENDS,
            True,
            "cancelled",
            ["The cleanup of run {} failed: RuntimeError"],
            id="cancelled-while-the-record-ends",
        ),
        pytest.param(
            StepClock,
            _CancelsTheCloser.WHILE_THE_CLOSE_IS_HELD,
            True,
            "cancelled",
            ["The cleanup of run {} failed: RuntimeError"],
            id="cancelled-while-the-close-is-held-and-the-cleanup-fails",
        ),
        pytest.param(
            BrokenClock,
            _CancelsTheCloser.WHILE_THE_CLOSE_IS_HELD,
            False,
            "cancelled",
            ["The supervision of run {} failed: RuntimeError"],
            id="cancelled-after-the-supervision-failed",
        ),
    ],
)
@in_loop
async def test_a_failure_that_does_not_propagate_is_logged_by_class_and_never_unretrieved(
    caplog, clock, cancels, cleanup_fails, propagates, logged
):
    caplog.set_level(logging.DEBUG)
    unretrieved: list[dict[str, object]] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: unretrieved.append(context)
    )

    outcome, run_id = await _close_a_run(clock(), cancels, cleanup_fails=cleanup_fails)

    await asyncio.sleep(0)
    gc.collect()
    assert unretrieved == []
    assert outcome == propagates
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert [record.getMessage() for record in errors] == [line.format(run_id) for line in logged]
    assert all(record.exc_info is None for record in errors)
    assert RECORDING_FAILURE_CANARY not in caplog.text


@in_loop
async def test_a_dropped_run_stays_dropped_when_its_client_then_leaves(monkeypatch):
    client = ClientSocket(blocks_the_second_chunk=True)
    async with _answering(client, monkeypatch) as (drive, records, response):
        await drive.run_to(60)
        client.leave()
        await response

        _assert_recorded(
            _status_of_the_run(drive, records),
            RunState.CANCELLED,
            events=3,
            at=60,
            lost=ClientLoss.DROPPED,
        )
        _assert_closed_cleanly(drive)


def _canary_events() -> tuple[StreamEvent, ...]:
    return (
        AssistantTextEvent(text="canary-text"),
        ToolCallEvent(tool_use_id="c", name="edit_song", input={"title": "canary-input"}),
        ToolResultEvent(tool_use_id="c", content="canary-result"),
        FinalEvent(text="canary-final"),
    )


CANARIES = ("canary-text", "canary-input", "canary-result", "canary-final")
MINT = TokenMint()


def _headers(user: str = "user-alice") -> dict[str, str]:
    return run_headers(MINT.workload(), MINT.user(sub=user))


def _served_app(records: RunRecords | None = None, events: tuple[StreamEvent, ...] = READ_SCRIPT):
    return create_app(
        service_config(),
        runners=Runners(cli=ScriptedRunner(events)),
        records=records,
        fetch_key_set=MINT.fetch_key_set,
    )


def test_a_served_run_is_read_by_its_owner_alone_and_forgotten_by_a_restart():
    with served(_served_app()) as base_url:
        streamed = post_run(base_url, _headers())
        run_id = str(uuid.UUID(streamed.text.split('"run_id":"', 1)[1][:36]))
        alice = get_run(base_url, _headers(), run_id)
        bob = get_run(base_url, _headers("user-bob"), run_id)
    with served(_served_app()) as restarted_url:
        after_restart = get_run(restarted_url, _headers(), run_id)

    assert streamed.content.endswith(b"\n\n")
    status = RunStatus.model_validate_json(alice.content)
    assert (status.state, status.events, status.client_lost) == (RunState.SUCCEEDED, 5, None)
    assert [bob.status_code, after_restart.status_code] == [404, 404]
    assert RejectionCode.NOT_FOUND in bob.text


def test_no_text_of_a_run_is_in_its_status_its_logs_or_the_store(caplog):
    caplog.set_level(logging.DEBUG)
    records = RunRecords()
    with served(_served_app(records, _canary_events())) as base_url:
        streamed = post_run(base_url, _headers())
        run_id = str(uuid.UUID(streamed.text.split('"run_id":"', 1)[1][:36]))
        status_bytes = get_run(base_url, _headers(), run_id).content

    assert all(canary in streamed.text for canary in CANARIES)
    assert not any(canary.encode() in status_bytes for canary in CANARIES)
    assert not any(canary in caplog.text for canary in CANARIES)
    assert not reaches_text(records, CANARIES)
