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
import collections
import enum
import gc
import inspect
import itertools
import logging
import math
import traceback
import types
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Generator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime

import anyio
import pytest
from service_support import (
    ALICE,
    MOST_BUSY_TURNS_BEFORE_THE_RUN_NEVER_SETTLES,
    QUIET,
    SONGMAKER,
    STRAY_CANCELLATION_CANARY,
    BrokenClock,
    ClientSocket,
    Drive,
    GatedRunner,
    ScriptedRunner,
    StepClock,
    StrayCancelledRunner,
    TokenMint,
    alice_admitted_run,
    client_leaves_mid_run,
    client_takes_nothing_until_dropped,
    driven,
    failure_lines,
    get_run,
    in_loop,
    instant,
    post_run,
    quarantine_on,
    reaches_text,
    respond_through_the_app,
    run_headers,
    served,
    service_config,
    stray_cancellation,
    supervision_fails,
    tool_call,
)
from starlette.requests import ClientDisconnect

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
from agent_service.run_liveness import (
    FailedPart,
    LoopClock,
    RunFailures,
    RunStepCancelled,
    log_failure,
)
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
                tool_call("c1"),
                ToolResultEvent(tool_use_id="c1", content="ok"),
                tool_call("c2"),
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
        _assert_recorded(drive.status(), RunState.RUNNING, events=8, at=0)

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
        alice_admitted_run(run_id),
        None,
        SONGMAKER,
        RunConfig(),
        LoopClock(),
        records,
        quarantine=quarantine_on(),
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
        drive.runner.release(tool_call("c", tool))
        drive.took(await anext(drive.frames))
        await drive.frames.aclose()

        _assert_recorded(
            drive.status(), state, code=code, events=2, at=0, lost=ClientLoss.DISCONNECTED
        )
        assert drive.types == ["run_started", "tool_call"]


async def _answer_arrives_behind_a_client_that_is_then_dropped(drive: Drive) -> None:
    drive.took(await anext(drive.frames))
    await drive.run_to(20)
    drive.runner.release(FINAL)
    await drive.run_to(45)


async def _client_leaves_after_the_answer_was_queued(drive: Drive) -> None:
    drive.took(await anext(drive.frames))
    drive.runner.release(FINAL)
    await drive.clock.parked()


@pytest.mark.parametrize(
    ("clock", "scenario", "state", "events", "lost", "ended_at"),
    [
        pytest.param(
            StepClock,
            client_leaves_mid_run,
            RunState.CANCELLED,
            1,
            ClientLoss.DISCONNECTED,
            0,
            id="client-leaves-while-the-run-is-going",
        ),
        pytest.param(
            StepClock,
            client_takes_nothing_until_dropped,
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
            supervision_fails,
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
    caplog.set_level(logging.DEBUG)
    runner = GatedRunner()
    runner.close_error = OSError(RECORDING_FAILURE_CANARY)
    async with driven(runner, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(45)
        await drive.frames.aclose()

    _assert_recorded(drive.status(), RunState.CANCELLED, events=2, at=45, lost=ClientLoss.DROPPED)
    assert failure_lines(caplog) == [
        (f"Closing the runner of run {drive.run_id} failed", "OSError", "_closed")
    ]
    assert RECORDING_FAILURE_CANARY not in caplog.text


@in_loop
async def test_a_runner_that_raises_is_logged_by_its_class_and_stack_never_by_its_text(caplog):
    caplog.set_level(logging.DEBUG)
    async with driven() as drive:
        drive.runner.release(RuntimeError(RECORDING_FAILURE_CANARY))
        await drive.run_to(ALL_THE_WAY_TO_THE_MAXIMUM)

    assert drive.status().failure_code is FailureCode.PROVIDER_ERROR
    assert failure_lines(caplog) == [
        (f"The runner of run {drive.run_id} failed", "RuntimeError", "run")
    ]
    assert RECORDING_FAILURE_CANARY not in caplog.text


def test_a_failure_is_logged_by_its_class_and_stack_never_by_its_text_notes_or_chain(caplog):
    caplog.set_level(logging.DEBUG)
    try:
        try:
            raise ValueError("canary-context")
        except ValueError:
            raise RuntimeError("canary-message") from KeyError("canary-cause")
    except RuntimeError as failure:
        failure.add_note("canary-note")
        log_failure(logging.getLogger(__name__), "The cleanup of run 7 failed", failure)

    assert failure_lines(caplog) == [
        (
            "The cleanup of run 7 failed",
            "RuntimeError",
            "test_a_failure_is_logged_by_its_class_and_stack_never_by_its_text_notes_or_chain",
        )
    ]
    assert "test_service_run_recording.py:" in caplog.text
    assert "canary" not in caplog.text


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
async def test_a_run_whose_terminal_was_decided_is_recorded_when_the_held_close_is_released():
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
            drive.status(), RunState.RUNNING, events=1, at=0, lost=ClientLoss.DISCONNECTED
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


class _Step(enum.Enum):
    """A step of a run's ending that can fail, with the failure it raises (A1a2 item 8)."""

    CLOCK = "the clock"
    DROP_REPORT = "the drop report"
    DISCONNECT_REPORT = "the disconnect report"
    RECORD_END = "the record's end"

    @property
    def failure(self) -> type[Exception]:
        return _FAILURE_OF[self]

    @property
    def canary(self) -> str:
        return f"CANARY_OF_{self.name}"

    def fails(self) -> Exception:
        return self.failure(self.canary)


class _ClockBroke(Exception):
    pass


class _DropNotRecorded(Exception):
    pass


class _DisconnectNotRecorded(Exception):
    pass


class _EndNotRecorded(Exception):
    pass


_FAILURE_OF = {
    _Step.CLOCK: _ClockBroke,
    _Step.DROP_REPORT: _DropNotRecorded,
    _Step.DISCONNECT_REPORT: _DisconnectNotRecorded,
    _Step.RECORD_END: _EndNotRecorded,
}
_SUPERVISION = "The supervision of run {} failed"
_CLEANUP = "The cleanup of run {} failed"


class _RecorderThatFails:
    """A run's recorder whose failing steps raise a new failure each time they are taken.

    ``taking`` hears of each step just before it is taken.
    """

    def __init__(
        self,
        recorder: RunRecorder,
        failing: frozenset[_Step],
        taking: Callable[[_Step], None],
        breaks: Callable[[_Step], BaseException],
    ) -> None:
        self._recorder = recorder
        self._failing = failing
        self._taking = taking
        self._breaks = breaks

    def accepted(self, event) -> None:
        self._recorder.accepted(event)

    def lost(self, reason: ClientLoss) -> None:
        step = _Step.DROP_REPORT if reason is ClientLoss.DROPPED else _Step.DISCONNECT_REPORT
        self._take(step, lambda: self._recorder.lost(reason))

    def over(self, fallback=None) -> None:
        self._take(_Step.RECORD_END, lambda: self._recorder.over(fallback))

    def _take(self, step: _Step, record: Callable[[], None]) -> None:
        self._taking(step)
        if step in self._failing:
            raise self._breaks(step)
        record()


class _RecordsThatFail(RunRecords):
    def __init__(
        self,
        failing: frozenset[_Step] = frozenset(),
        taking: Callable[[_Step], None] = lambda _step: None,
        breaks: Callable[[_Step], BaseException] = _Step.fails,
    ) -> None:
        super().__init__()
        self._failing = failing
        self._taking = taking
        self._breaks = breaks
        self.started: list[RunStarted] = []

    def begin(self, started, app, principal):
        self.started.append(started)
        return _RecorderThatFails(
            super().begin(started, app, principal), self._failing, self._taking, self._breaks
        )


@in_loop
async def test_a_recording_defect_surfaces_as_a_supervision_failure(caplog):
    records = _RecordsThatFail(frozenset({_Step.RECORD_END}))
    async with driven(records=records, read=False) as drive:
        drive.took(await anext(drive.frames))
        drive.runner.release(FINAL)

        with pytest.raises(_EndNotRecorded, match=_Step.RECORD_END.canary):
            await anext(drive.frames)

    assert failure_lines(caplog) == []


class _Closer(enum.Enum):
    """How the client ends its run: it closes the stream, or it reads it to its end."""

    CLOSES = enum.auto()
    READS = enum.auto()


class _Cancellation(enum.Enum):
    """Where the closer is cancelled, once it waits: never, in the body, or in the teardown."""

    NONE = enum.auto()
    IN_THE_BODY = enum.auto()
    DURING_TEARDOWN = enum.auto()


@dataclass(frozen=True)
class _Ending:
    cancellation: _Cancellation
    closer: _Closer
    holds_close: bool
    failing: frozenset[_Step]
    stray: frozenset[_Step] = frozenset()

    def breaks(self, step: _Step) -> BaseException:
        """The failure of a failing step: its own, or a stray cancellation nobody requested."""
        return _cancelled_unasked(step) if step in self.stray else step.fails()

    @property
    def ends_by_itself(self) -> bool:
        """Whether the run ends before its closer acts: its clock broke, or a stall dropped it."""
        return bool(self.failing & {_Step.CLOCK, _Step.DROP_REPORT})

    @property
    def stalls(self) -> bool:
        """Whether the run ends by a stall: its drop report fails while its clock holds."""
        return _Step.DROP_REPORT in self.failing and _Step.CLOCK not in self.failing

    @property
    def is_possible(self) -> bool:
        """Whether the service can reach this combination; the table drives every one it can.

        * A close is never cancelled in the body: ``aclose`` throws into the
          stream paused at a ``yield``, where nothing awaits.
        * A reader is never cancelled in the teardown: its stream ends only
          once the runner's stop has finished, so it waits in the body.
        * The disconnect report is made only when the stream is cut short: by
          a close, or by a cancellation in the body.
        """
        match self.cancellation:
            case _Cancellation.IN_THE_BODY if self.closer is _Closer.CLOSES:
                return False
            case _Cancellation.DURING_TEARDOWN if self.closer is _Closer.READS:
                return False
        cut_short = self.closer is _Closer.CLOSES or self.cancellation is _Cancellation.IN_THE_BODY
        return cut_short or _Step.DISCONNECT_REPORT not in self.failing

    @property
    def injected(self) -> collections.Counter[str]:
        return collections.Counter(type(self.breaks(step)).__name__ for step in self.failing)


def _every_ending() -> list[pytest.param]:
    subsets = [
        frozenset(subset)
        for size in range(len(_Step) + 1)
        for subset in itertools.combinations(_Step, size)
    ]
    endings = [
        _Ending(cancellation, closer, holds_close, failing)
        for cancellation, closer, holds_close, failing in itertools.product(
            _Cancellation, _Closer, (False, True), subsets
        )
    ]
    return [
        pytest.param(
            ending,
            id="-".join(
                [
                    ending.cancellation.name.lower(),
                    ending.closer.name.lower(),
                    "held" if ending.holds_close else "free",
                    *sorted(step.name.lower() for step in ending.failing),
                ]
            ),
        )
        for ending in endings
        if ending.is_possible
    ]


async def _end_a_run(
    ending: _Ending, *, cancel_at_the_record_end: bool = False
) -> tuple[asyncio.Task[object], bool, Drive]:
    """End a run the way ``ending`` says; its closer, whether a cancellation reached it, the run.

    The run ends by itself first when its clock breaks, or when a failing
    drop report needs a stall: the client takes nothing until it is dropped
    at 45 s. A reader of a run that nothing else ends gets its answer. A
    close cut in its teardown of a run that ended by itself, without a held
    close, comes right after the drop report, while the runner's stop is
    still under way. A reader cut in the body of a run that ended by itself,
    without a held close, is cancelled while the runner stops: one whose
    clock broke waits for its next frame from the first one on and is
    cancelled when the drop report is taken; one whose stall dropped it
    starts reading at the drop report and is cancelled once it waits in the
    stream.
    """
    closers: list[asyncio.Task[object]] = []
    cuts: list[asyncio.Task[None]] = []
    cancelled_by_a_step: list[bool] = []
    dropped = asyncio.Event()
    cut_while_it_stops = (
        ending.cancellation is _Cancellation.IN_THE_BODY
        and ending.ends_by_itself
        and not ending.holds_close
    )

    def taking(step: _Step) -> None:
        if step is _Step.DROP_REPORT:
            dropped.set()
            if cut_while_it_stops and ending.stalls:
                closers.append(asyncio.create_task(drive.drain()))
                cuts.append(asyncio.create_task(cut_once_it_reads(closers[0])))
            elif cut_while_it_stops:
                cancelled_by_a_step.append(drive.frames.ag_running and closers[0].cancel())
        if step is _Step.RECORD_END and cancel_at_the_record_end:
            cancelled_by_a_step.append(closers[0].cancel())

    async def cut_once_it_reads(reader: asyncio.Task[object]) -> None:
        await _spun_until(lambda: drive.frames.ag_running or reader.done())
        cancelled_by_a_step.append(drive.frames.ag_running and reader.cancel())

    records = _RecordsThatFail(ending.failing, taking, ending.breaks)
    clock = (
        BrokenClock(lambda: ending.breaks(_Step.CLOCK))
        if _Step.CLOCK in ending.failing
        else StepClock()
    )
    runner = GatedRunner()
    if ending.holds_close:
        runner.hold_close()
    cut_in_the_teardown = (
        ending.cancellation is _Cancellation.DURING_TEARDOWN and not ending.holds_close
    )
    async with driven(runner, clock=clock, records=records, read=False) as drive:
        drive.took(await anext(drive.frames))
        if cut_in_the_teardown and ending.ends_by_itself:
            if ending.stalls:
                await drive.run_to(44)
                drive.clock.advance_to(45)
            await _spun_until(dropped.is_set)
        elif not cut_while_it_stops or ending.stalls:
            await drive.clock.parked()
            if ending.stalls:
                await drive.run_to(45)
            elif not ending.ends_by_itself and ending.cancellation is _Cancellation.NONE:
                if ending.closer is _Closer.READS:
                    runner.release(FINAL)
        if not closers:
            closing = drive.frames.aclose() if ending.closer is _Closer.CLOSES else drive.drain()
            closers.append(asyncio.create_task(closing))
        if cut_while_it_stops:
            await asyncio.wait(closers + cuts)
        elif cut_in_the_teardown:
            await _spun_until(lambda: drive.frames.ag_running or closers[0].done())
        else:
            await drive.clock.parked()
        delivered = ending.cancellation is not _Cancellation.NONE and closers[0].cancel()
        await drive.clock.parked()
        runner.release_close()
        await asyncio.wait(closers)
    return closers[0], delivered or any(cancelled_by_a_step), drive


async def _spun_until(done: Callable[[], bool]) -> None:
    """Spin the loop until ``done``, failing once the run would never get there."""
    for _turn in range(MOST_BUSY_TURNS_BEFORE_THE_RUN_NEVER_SETTLES):
        if done():
            return
        await asyncio.sleep(0)
    raise AssertionError("the run never got there")


def _propagated(closer: asyncio.Task[object], *, delivered: bool) -> str:
    """What the closer ended with; a cancellation nobody delivered is a raised failure."""
    if closer.cancelled():
        return "cancelled" if delivered else "CancelledError"
    failure = closer.exception()
    return type(failure).__name__ if failure else "returned"


@asynccontextmanager
async def _watched_loop() -> AsyncIterator[list[dict[str, object]]]:
    """What the loop reports as never retrieved, once the scenario's tasks are gone."""
    unretrieved: list[dict[str, object]] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: unretrieved.append(context)
    )
    yield unretrieved
    await asyncio.sleep(0)
    gc.collect()


@pytest.mark.parametrize("ender", [_Step.CLOCK, _Step.DROP_REPORT])
def test_the_ending_table_drives_a_reader_cut_in_the_body_of_every_run_that_ended_by_itself(
    ender,
):
    """Rule 14: the table leaves out no run its broken clock or its stall ended."""
    table = {param.values[0] for param in _every_ending()}
    cut_readers = [
        _Ending(_Cancellation.IN_THE_BODY, _Closer.READS, holds_close, frozenset(others) | {ender})
        for holds_close in (False, True)
        for size in range(len(_Step))
        for others in itertools.combinations(set(_Step) - {ender}, size)
    ]
    assert [ending for ending in cut_readers if ending not in table] == []


@pytest.mark.parametrize("ending", _every_ending())
@in_loop
async def test_every_failure_of_a_run_s_ending_propagates_or_is_logged_once(caplog, ending):
    caplog.set_level(logging.DEBUG)
    async with _watched_loop() as unretrieved:
        closer, cancelled, _drive = await _end_a_run(ending)

    propagated = _propagated(closer, delivered=cancelled)
    assert cancelled is (ending.cancellation is not _Cancellation.NONE)
    if cancelled:
        assert propagated == "cancelled"
    raised = collections.Counter([propagated]) - collections.Counter(["cancelled", "returned"])
    logged = collections.Counter(kind for _event, kind, _raiser in failure_lines(caplog))
    assert raised + logged == ending.injected
    assert unretrieved == []
    assert not [step for step in _Step if step.canary in caplog.text]


@pytest.mark.parametrize(
    ("ending", "propagates", "logged"),
    [
        pytest.param(
            _Ending(
                _Cancellation.NONE, _Closer.CLOSES, True, frozenset({_Step.CLOCK, _Step.RECORD_END})
            ),
            "_EndNotRecorded",
            [(_SUPERVISION, "_ClockBroke")],
            id="cleanup-over-supervision",
        ),
        *(
            pytest.param(
                _Ending(_Cancellation.NONE, _Closer.CLOSES, holds_close, frozenset({_Step.CLOCK})),
                "returned",
                [(_SUPERVISION, "_ClockBroke")],
                id=f"a-close-after-the-supervision-failed-returns-{held}",
            )
            for holds_close, held in ((False, "free"), (True, "held"))
        ),
        pytest.param(
            _Ending(
                _Cancellation.NONE,
                _Closer.READS,
                False,
                frozenset({_Step.CLOCK, _Step.DROP_REPORT, _Step.RECORD_END}),
            ),
            "_EndNotRecorded",
            [(_SUPERVISION, "_ClockBroke"), (_SUPERVISION, "_DropNotRecorded")],
            id="clock-drop-and-end-fail",
        ),
        pytest.param(
            _Ending(
                _Cancellation.IN_THE_BODY,
                _Closer.READS,
                False,
                frozenset({_Step.DISCONNECT_REPORT}),
            ),
            "cancelled",
            [(_CLEANUP, "_DisconnectNotRecorded")],
            id="a-failed-disconnect-report-keeps-the-cancellation",
        ),
        pytest.param(
            _Ending(
                _Cancellation.IN_THE_BODY,
                _Closer.READS,
                False,
                frozenset({_Step.DISCONNECT_REPORT, _Step.RECORD_END}),
            ),
            "cancelled",
            [(_CLEANUP, "_DisconnectNotRecorded"), (_CLEANUP, "_EndNotRecorded")],
            id="a-failed-disconnect-report-and-record-end-keep-the-cancellation",
        ),
        pytest.param(
            _Ending(
                _Cancellation.NONE,
                _Closer.CLOSES,
                True,
                frozenset({_Step.DISCONNECT_REPORT, _Step.RECORD_END}),
            ),
            "_DisconnectNotRecorded",
            [(_CLEANUP, "_EndNotRecorded")],
            id="the-first-cleanup-failure-propagates",
        ),
    ],
)
@in_loop
async def test_which_failure_of_a_run_s_ending_propagates_and_which_are_logged(
    caplog, ending, propagates, logged
):
    caplog.set_level(logging.DEBUG)
    async with _watched_loop() as unretrieved:
        closer, delivered, drive = await _end_a_run(ending)

    assert _propagated(closer, delivered=delivered) == propagates
    assert [(event, kind) for event, kind, _raiser in failure_lines(caplog)] == [
        (event.format(drive.run_id), kind) for event, kind in logged
    ]
    assert unretrieved == []
    assert not [step for step in _Step if step.canary in caplog.text]


@in_loop
async def test_a_closer_cancelled_while_the_record_ends_is_cancelled_and_the_failure_logged(
    caplog,
):
    async with _watched_loop() as unretrieved:
        closer, delivered, drive = await _end_a_run(
            _Ending(_Cancellation.NONE, _Closer.CLOSES, False, frozenset({_Step.RECORD_END})),
            cancel_at_the_record_end=True,
        )

    assert _propagated(closer, delivered=delivered) == "cancelled"
    assert [(event, kind) for event, kind, _raiser in failure_lines(caplog)] == [
        (_CLEANUP.format(drive.run_id), "_EndNotRecorded")
    ]
    assert unretrieved == []


async def _observed_ending(
    ending: _Ending, caplog: pytest.LogCaptureFixture
) -> tuple[str, list[tuple[str, str]], list[str], tuple[RunState, ClientLoss | None] | None]:
    """What the client and the store see of a run ended as ``ending`` says, and what is logged."""
    caplog.clear()
    closer, delivered, drive = await _end_a_run(ending)
    status = drive.status()
    return (
        _propagated(closer, delivered=delivered),
        [
            (event.replace(str(drive.run_id), "{}"), kind)
            for event, kind, _raiser in failure_lines(caplog)
        ],
        drive.types,
        None if status is None else (status.state, status.client_lost),
    )


@pytest.mark.parametrize(
    ("stray", "ending"),
    [
        pytest.param(
            step,
            _Ending(_Cancellation.NONE, closer, False, frozenset({step, *also})),
            id="-".join([step.name.lower(), *(failing.name.lower() for failing in also)]),
        )
        for step, closer, also in (
            (_Step.CLOCK, _Closer.CLOSES, ()),
            (_Step.CLOCK, _Closer.CLOSES, (_Step.RECORD_END,)),
            (_Step.DROP_REPORT, _Closer.READS, ()),
            (_Step.DISCONNECT_REPORT, _Closer.CLOSES, ()),
            (_Step.RECORD_END, _Closer.READS, ()),
            (_Step.RECORD_END, _Closer.CLOSES, (_Step.DISCONNECT_REPORT,)),
        )
    ],
)
@in_loop
async def test_a_stray_cancellation_of_a_step_ends_the_run_as_that_step_s_own_failure(
    caplog, stray, ending
):
    """A ``CancelledError`` that nobody requested is the step's failure (rule 14).

    The run ends exactly as it does when the step raises its own failure:
    the same frames, the same record, the same propagated and logged
    failures, with ``RunStepCancelled`` in the place of the step's own class.
    """
    caplog.set_level(logging.DEBUG)
    async with _watched_loop() as unretrieved:
        own = await _observed_ending(ending, caplog)
        cancelled = await _observed_ending(replace(ending, stray=frozenset({stray})), caplog)

    def as_own(kind: str) -> str:
        return stray.failure.__name__ if kind == RunStepCancelled.__name__ else kind

    propagated, logged, frames, record = cancelled
    logged_as_own = [(event, as_own(kind)) for event, kind in logged]
    renamed = (as_own(propagated), logged_as_own, frames, record)
    assert renamed == own
    assert [propagated, *(kind for _event, kind in logged)].count(RunStepCancelled.__name__) == 1
    assert unretrieved == []
    assert not [step for step in _Step if step.canary in caplog.text]


def _cancelled_unasked(step: _Step) -> asyncio.CancelledError:
    return asyncio.CancelledError(step.canary)


def _reaches_a_frame_of(root: object, function: Callable[..., object]) -> bool:
    """Whether an observer of ``root`` can reach a frame of ``function`` through references."""
    seen: set[int] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        if id(current) in seen or isinstance(current, type | types.ModuleType):
            continue
        seen.add(id(current))
        if isinstance(current, types.FrameType) and current.f_code is function.__code__:
            return True
        pending.extend(gc.get_referents(current))
    return False


def _a_lookup_that_fails() -> None:
    local_value = {"payload": _Step.RECORD_END.canary}
    raise LookupError(local_value["payload"])


def _a_step_cancelled_unasked() -> None:
    local_value = {"payload": _Step.RECORD_END.canary}
    try:
        _a_lookup_that_fails()
    except LookupError as lookup:
        raise _cancelled_unasked(_Step.RECORD_END) from lookup
    raise AssertionError(local_value)


def _cancelled_after_passing_a_step(_step: _Step) -> asyncio.CancelledError:
    with pytest.raises(asyncio.CancelledError) as stray:
        _a_step_cancelled_unasked()
    return stray.value


def _stray_only_at_the_clock(step: _Step) -> BaseException:
    return _cancelled_after_passing_a_step(step) if step is _Step.CLOCK else step.fails()


@pytest.mark.parametrize(
    ("failing", "breaks"),
    [
        pytest.param(
            frozenset({_Step.RECORD_END}),
            _cancelled_after_passing_a_step,
            id="record_end",
        ),
        pytest.param(
            frozenset({_Step.DISCONNECT_REPORT, _Step.RECORD_END}),
            _cancelled_after_passing_a_step,
            id="disconnect-record_end",
        ),
        pytest.param(
            frozenset({_Step.CLOCK, _Step.DROP_REPORT}),
            _stray_only_at_the_clock,
            id="stray_clock-raising_drop_report",
        ),
    ],
)
@in_loop
async def test_a_propagated_stray_cancellation_keeps_no_frame_of_the_step_it_left(failing, breaks):
    """Rule 14: nothing reachable from the failure a close raises keeps the stray's frames."""
    records = _RecordsThatFail(failing, breaks=breaks)
    clock = BrokenClock(lambda: breaks(_Step.CLOCK)) if _Step.CLOCK in failing else StepClock()
    async with driven(GatedRunner(), clock=clock, records=records, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.clock.parked()
        ending = drive.drain() if _Step.CLOCK in failing else drive.frames.aclose()
        closer = asyncio.create_task(ending)
        await asyncio.wait({closer})
    propagated = closer.exception()
    del drive, records, closer
    gc.collect()

    assert type(propagated) is RunStepCancelled
    assert not _reaches_a_frame_of(propagated, _a_step_cancelled_unasked)
    assert not _reaches_a_frame_of(propagated, _a_lookup_that_fails)


@pytest.mark.parametrize(
    "after_its_stop",
    [
        pytest.param(False, id="a-runner-cancelled-unasked"),
        pytest.param(True, id="a-runner-that-withdraws-its-stop"),
    ],
)
@in_loop
async def test_a_failed_record_end_keeps_no_frame_of_how_a_runner_was_cancelled_unasked(
    after_its_stop,
):
    """Rule 14: no frame of the service that holds the runner's stray outlives its step."""
    records = _RecordsThatFail(frozenset({_Step.RECORD_END}))
    runner = StrayCancelledRunner(after_its_stop=after_its_stop)
    async with driven(runner, records=records, read=False) as drive:
        closer = asyncio.create_task(_closed_while_it_waits(drive))
        await asyncio.wait({closer})
    propagated = closer.exception()
    del drive, closer, runner
    gc.collect()

    assert type(propagated) is _EndNotRecorded
    assert not _reaches_a_frame_of(propagated, stray_cancellation)


class _RunnerRaisingThisStray(GatedRunner):
    def __init__(self, stray: asyncio.CancelledError) -> None:
        super().__init__()
        self._stray = stray

    async def run(self, admitted):
        try:
            raise self._stray
            yield QUIET
        finally:
            await self._closed()


@in_loop
async def test_a_runner_cancelled_unasked_is_made_inert_once_its_close_is_logged():
    """Rule 14: the stray the runner's close met keeps neither its traceback nor its chain."""
    stray = asyncio.CancelledError(STRAY_CANCELLATION_CANARY)
    stray.__cause__ = RuntimeError("its cause")
    stray.__context__ = RuntimeError("its context")
    async with driven(_RunnerRaisingThisStray(stray), read=False) as drive:
        closer = asyncio.create_task(_closed_while_it_waits(drive))
        await asyncio.wait({closer})

    assert (stray.__traceback__, stray.__cause__, stray.__context__) == (None, None, None)


def _a_generator_suspended_after_catching_a_cancellation() -> Generator[None, None, None]:
    try:
        raise asyncio.CancelledError
    except asyncio.CancelledError:
        yield
    yield


@in_loop
async def test_recording_a_stray_cancellation_leaves_a_suspended_generator_of_its_traceback_open():
    """Rule 14: cutting a traceback must not close the generators it passed (``clear_frames``)."""
    suspended = _a_generator_suspended_after_catching_a_cancellation()
    next(suspended)
    stray = asyncio.CancelledError()
    stray.__traceback__ = types.TracebackType(
        None, suspended.gi_frame, suspended.gi_frame.f_lasti, suspended.gi_frame.f_lineno
    )

    RunFailures(uuid.uuid4()).add(FailedPart.SUPERVISION, stray)

    assert inspect.getgeneratorstate(suspended) == inspect.GEN_SUSPENDED


def _raised_again_by_a_function_holding_a_payload(
    cancelled: asyncio.CancelledError,
) -> asyncio.CancelledError:
    with pytest.raises(asyncio.CancelledError) as again:
        _raises_holding_a_payload(cancelled)
    return again.value


def _raises_holding_a_payload(cancelled: asyncio.CancelledError) -> None:
    local_value = {"payload": _Step.CLOCK.canary}
    try:
        raise cancelled
    finally:
        del local_value


@in_loop
async def test_a_stray_cancellation_recorded_again_keeps_no_frame_of_what_raised_it_again():
    """Rule 14: a repeated record makes the stray inert again, whoever links it."""
    failures = RunFailures(uuid.uuid4())
    cancelled = _cancelled_unasked(_Step.CLOCK)
    failures.add(FailedPart.SUPERVISION, cancelled)
    failures.add(FailedPart.SUPERVISION, _raised_again_by_a_function_holding_a_payload(cancelled))
    secondary = RuntimeError("secondary")
    secondary.__cause__ = cancelled
    failures.add(FailedPart.SUPERVISION, secondary)
    with pytest.raises(RunStepCancelled) as propagated:
        failures.leave(cancellation=None, body_failure=None)
    del cancelled, secondary
    gc.collect()

    assert not _reaches_a_frame_of(propagated.value, _raises_holding_a_payload)
    assert not _reaches_a_frame_of(failures, _raises_holding_a_payload)


@pytest.mark.parametrize("again", list(FailedPart))
@in_loop
async def test_a_stray_cancellation_recorded_again_stays_one_failure_of_its_first_part(
    caplog, again
):
    failures = RunFailures(uuid.uuid4())
    cancelled = _cancelled_unasked(_Step.CLOCK)
    first = failures.add(FailedPart.SUPERVISION, cancelled)
    second = failures.add(again, cancelled)

    with pytest.raises(RunStepCancelled) as raised:
        failures.leave(cancellation=None, body_failure=None)
    assert second is first
    assert raised.value is first
    assert first.part is FailedPart.SUPERVISION
    assert failure_lines(caplog) == []


class _CancelledLikeAnyOther(asyncio.CancelledError):
    """A cancellation equal to every other of its class, as a subclass may define itself."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _CancelledLikeAnyOther)

    def __hash__(self) -> int:
        return 0


def _raised(cancelled: asyncio.CancelledError) -> asyncio.CancelledError:
    with pytest.raises(asyncio.CancelledError) as raised:
        raise cancelled
    return raised.value


@in_loop
async def test_two_equal_but_distinct_stray_cancellations_stay_two_failures(caplog):
    """Rule 14: the service tells failures apart by identity, never by equality."""
    run_id = uuid.uuid4()
    failures = RunFailures(run_id)
    supervision, cleanup = _raised(_CancelledLikeAnyOther()), _raised(_CancelledLikeAnyOther())
    failures.add(FailedPart.SUPERVISION, supervision)
    second = failures.add(FailedPart.CLEANUP, cleanup)

    with pytest.raises(RunStepCancelled) as raised:
        failures.leave(cancellation=None, body_failure=None)
    assert raised.value is second
    assert second.part is FailedPart.CLEANUP
    assert [(event, kind) for event, kind, _raiser in failure_lines(caplog)] == [
        (_SUPERVISION.format(run_id), RunStepCancelled.__name__)
    ]


@in_loop
async def test_a_stray_cancellation_reusing_a_freed_ones_identity_is_a_failure_of_its_own():
    failures = RunFailures(uuid.uuid4())
    first = _raised(_CancelledLikeAnyOther())
    freed_identity = id(first)
    failures.add(FailedPart.SUPERVISION, first)
    del first
    reuser = _raised(_CancelledLikeAnyOther())
    for _attempt in range(1000):
        if id(reuser) == freed_identity:
            break
        reuser = _raised(_CancelledLikeAnyOther())
    assert id(reuser) == freed_identity, "the allocator did not reuse the freed identity"

    second = failures.add(FailedPart.CLEANUP, reuser)

    assert second.part is FailedPart.CLEANUP


@in_loop
async def test_one_stray_cancellation_met_by_two_steps_of_a_close_propagates_once(caplog):
    caplog.set_level(logging.DEBUG)
    shared = _cancelled_unasked(_Step.DISCONNECT_REPORT)
    records = _RecordsThatFail(
        frozenset({_Step.DISCONNECT_REPORT, _Step.RECORD_END}), breaks=lambda _step: shared
    )
    async with _watched_loop() as unretrieved:
        async with driven(GatedRunner(), records=records, read=False) as drive:
            drive.took(await anext(drive.frames))
            await drive.clock.parked()
            closer = asyncio.create_task(drive.frames.aclose())
            await asyncio.wait({closer})

    assert type(closer.exception()) is RunStepCancelled
    assert failure_lines(caplog) == []
    assert unretrieved == []


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.parametrize(
    ("records", "clock", "runners", "state", "lost"),
    [
        pytest.param(
            lambda: _RecordsThatFail(frozenset({_Step.RECORD_END}), breaks=_cancelled_unasked),
            StepClock,
            None,
            RunState.RUNNING,
            ClientLoss.DROPPED,
            id="record_end",
        ),
        pytest.param(
            _RecordsThatFail,
            lambda: BrokenClock(lambda: _cancelled_unasked(_Step.CLOCK)),
            None,
            RunState.CANCELLED,
            ClientLoss.DROPPED,
            id="clock",
        ),
        pytest.param(
            lambda: _RecordsThatFail(frozenset({_Step.RECORD_END}), breaks=_cancelled_unasked),
            StepClock,
            Runners,
            RunState.RUNNING,
            None,
            id="no_runner-record_end",
        ),
    ],
)
@in_loop
async def test_a_stray_cancellation_reaches_the_asgi_server_as_the_step_s_failure(
    caplog, monkeypatch, spec_version, records, clock, runners, state, lost
):
    """A server below spec 2.4 swallows a ``CancelledError``; a stray one never travels as one."""
    caplog.set_level(logging.DEBUG)
    store = records()
    async with driven(clock=clock(), records=store, read=False) as drive:
        client = ClientSocket(spec_version=spec_version)
        response = await respond_through_the_app(
            drive, client, monkeypatch, runners=runners and runners()
        )
        await drive.clock.parked()
        drive.runner.release(FINAL)
        await asyncio.wait({response})

    assert not response.cancelled()
    assert type(response.exception()) is RunStepCancelled
    propagated = "".join(traceback.format_exception(response.exception()))
    assert not [step for step in _Step if step.canary in propagated]
    assert failure_lines(caplog) == []
    assert not [step for step in _Step if step.canary in caplog.text]
    status = drive.status(store.started[0].run_id)
    assert status is not None
    assert (status.state, status.client_lost) == (state, lost)


def _cancelled_by_its_own_request(step: _Step) -> asyncio.CancelledError:
    """The cancellation of a step that requested the cancellation of its own task."""
    current = asyncio.current_task()
    assert current is not None
    current.cancel()
    return asyncio.CancelledError(step.canary)


async def _clock_cancelled_by_its_own_request() -> None:
    raise _cancelled_by_its_own_request(_Step.CLOCK)


class _ClockWhoseFirstWaitIsCancelled(StepClock):
    def __init__(self, cancelled: Callable[[], Coroutine[object, object, None]]) -> None:
        super().__init__()
        self._cancelled: Callable[[], Coroutine[object, object, None]] | None = cancelled

    async def wait_until(self, deadline: float) -> None:
        if (cancelled := self._cancelled) is not None:
            self._cancelled = None
            await cancelled()
        await super().wait_until(deadline)


async def _answered_and_read(drive: Drive) -> None:
    drive.took(await anext(drive.frames))
    drive.runner.release(FINAL)
    await drive.drain()


async def _dropped_and_read(drive: Drive) -> None:
    await client_takes_nothing_until_dropped(drive)
    await drive.drain()


async def _closed_while_it_waits(drive: Drive) -> None:
    drive.took(await anext(drive.frames))
    await drive.clock.parked()
    await drive.frames.aclose()


_RUNNER_CLOSE = "Closing the runner of run {} failed"


@pytest.mark.parametrize(
    ("runner", "clock", "records", "client", "propagates", "logged"),
    [
        pytest.param(
            GatedRunner,
            lambda: _ClockWhoseFirstWaitIsCancelled(
                lambda: stray_cancellation(after_its_stop=True)
            ),
            RunRecords,
            _answered_and_read,
            RunStepCancelled.__name__,
            [],
            id="a-clock-wait-that-withdraws-its-stop-fails",
        ),
        pytest.param(
            GatedRunner,
            lambda: _ClockWhoseFirstWaitIsCancelled(_clock_cancelled_by_its_own_request),
            RunRecords,
            _answered_and_read,
            "returned",
            [],
            id="a-clock-wait-cancelled-at-its-own-request-is-shut-down",
        ),
        pytest.param(
            GatedRunner,
            StepClock,
            lambda: _RecordsThatFail(
                frozenset({_Step.DROP_REPORT}), breaks=_cancelled_by_its_own_request
            ),
            _dropped_and_read,
            "returned",
            [],
            id="a-supervisor-cancelled-at-its-own-request-is-shut-down",
        ),
        pytest.param(
            lambda: StrayCancelledRunner(after_its_stop=False),
            StepClock,
            RunRecords,
            _closed_while_it_waits,
            "returned",
            [(_RUNNER_CLOSE, "CancelledError")],
            id="a-runner-cancelled-unasked-is-logged",
        ),
        pytest.param(
            lambda: StrayCancelledRunner(after_its_stop=True),
            StepClock,
            RunRecords,
            _closed_while_it_waits,
            "returned",
            [(_RUNNER_CLOSE, "CancelledError")],
            id="a-runner-that-withdraws-its-stop-is-logged",
        ),
    ],
)
@in_loop
async def test_a_finished_task_of_a_run_is_shut_down_only_while_its_cancellation_request_stands(
    caplog, runner, clock, records, client, propagates, logged
):
    """Rule 14: a task's own ``cancelling()`` decides, never whether ``cancel()`` once succeeded."""
    caplog.set_level(logging.DEBUG)
    async with _watched_loop() as unretrieved:
        async with driven(runner(), clock=clock(), records=records(), read=False) as drive:
            closer = asyncio.create_task(client(drive))
            await asyncio.wait({closer})

    assert _propagated(closer, delivered=False) == propagates
    assert [(event, kind) for event, kind, _raiser in failure_lines(caplog)] == [
        (event.format(drive.run_id), kind) for event, kind in logged
    ]
    assert unretrieved == []
    assert STRAY_CANCELLATION_CANARY not in caplog.text
    assert not [step for step in _Step if step.canary in caplog.text]


@pytest.mark.parametrize(
    ("has_runner", "failing", "propagates", "logged"),
    [
        pytest.param(True, frozenset(), "CancelledError", [], id="nothing-failed"),
        pytest.param(
            True, frozenset({_Step.RECORD_END}), "_EndNotRecorded", [], id="the-record-end-failed"
        ),
        pytest.param(
            True,
            frozenset({_Step.DISCONNECT_REPORT, _Step.RECORD_END}),
            "_DisconnectNotRecorded",
            [(_CLEANUP, "_EndNotRecorded")],
            id="both-cleanup-steps-failed",
        ),
        pytest.param(
            False,
            frozenset({_Step.DISCONNECT_REPORT}),
            "_DisconnectNotRecorded",
            [],
            id="no-runner-the-disconnect-report-failed",
        ),
    ],
)
@in_loop
async def test_a_cancellation_thrown_into_the_body_without_a_request_yields_to_a_failed_cleanup(
    caplog, has_runner, failing, propagates, logged
):
    """Rule 14: the body's own ``CancelledError`` is its outcome; a cleanup failure outranks it."""
    caplog.set_level(logging.DEBUG)
    records = _RecordsThatFail(failing)
    async with _watched_loop() as unretrieved, driven(records=records, read=False) as drive:
        run_id, frames = (
            (drive.run_id, drive.frames)
            if has_runner
            else _frames_of_a_run_without_a_runner(records)
        )
        drive.took(await anext(frames))
        await drive.clock.parked()
        closer = asyncio.create_task(frames.athrow(asyncio.CancelledError()))
        await asyncio.wait({closer})

    assert _propagated(closer, delivered=False) == propagates
    assert [(event, kind) for event, kind, _raiser in failure_lines(caplog)] == [
        (event.format(run_id), kind) for event, kind in logged
    ]
    assert unretrieved == []
    assert not [step for step in _Step if step.canary in caplog.text]


@pytest.mark.parametrize("spins", range(8))
@in_loop
async def test_a_clock_failure_racing_the_close_is_logged_once_however_soon_the_close_comes(
    caplog, spins
):
    """The client closes after ``spins`` steps of the loop, not once the run waits for its clock."""
    caplog.set_level(logging.DEBUG)
    broken: list[Exception] = []

    def breaks() -> Exception:
        broken.append(_Step.CLOCK.fails())
        return broken[-1]

    async with _watched_loop() as unretrieved:
        async with driven(GatedRunner(), clock=BrokenClock(breaks), read=False) as drive:
            drive.took(await anext(drive.frames))
            for _ in range(spins):
                await asyncio.sleep(0)
            await drive.frames.aclose()

    assert [(event, kind) for event, kind, _raiser in failure_lines(caplog)] == [
        (_SUPERVISION.format(drive.run_id), "_ClockBroke")
    ] * len(broken)
    assert unretrieved == []
    assert _Step.CLOCK.canary not in caplog.text


class _NoRunnerClient(enum.Enum):
    """What the client of a run without a runner does once it holds the first frame."""

    READS = enum.auto()
    CLOSES = enum.auto()
    IS_CANCELLED = enum.auto()


async def _take_a_run_without_a_runner(
    frames: AsyncGenerator[bytes, None], does: _NoRunnerClient, taken: list[bytes]
) -> None:
    """Take the first frame, then act; the frames are closed on the way out, as a response does."""
    async with aclosing(frames):
        taken.append(await anext(frames))
        match does:
            case _NoRunnerClient.READS:
                taken.extend([frame async for frame in frames])
            case _NoRunnerClient.IS_CANCELLED:
                await asyncio.Event().wait()


@pytest.mark.parametrize(
    ("failing", "does", "propagates", "frames", "logged"),
    [
        (frozenset(), _NoRunnerClient.READS, "returned", 2, []),
        *(
            (frozenset({_Step.RECORD_END, *also}), does, "_EndNotRecorded", 0, [])
            for also in ((), (_Step.DISCONNECT_REPORT,))
            for does in _NoRunnerClient
        ),
        (
            frozenset({_Step.DISCONNECT_REPORT}),
            _NoRunnerClient.CLOSES,
            "_DisconnectNotRecorded",
            1,
            [],
        ),
        (
            frozenset({_Step.DISCONNECT_REPORT}),
            _NoRunnerClient.IS_CANCELLED,
            "cancelled",
            1,
            [(_CLEANUP, "_DisconnectNotRecorded")],
        ),
    ],
    ids=lambda value: (
        "+".join(sorted(step.name.lower() for step in value)) or "nothing"
        if isinstance(value, frozenset)
        else None
    ),
)
@in_loop
async def test_a_run_without_a_runner_ends_its_record_before_it_hands_out_a_frame(
    caplog, failing, does, propagates, frames, logged
):
    """A failed record end leaves no frame to hand out, so no cancellation can reach it."""
    caplog.set_level(logging.DEBUG)
    records = _RecordsThatFail(failing)
    taken: list[bytes] = []
    async with _watched_loop() as unretrieved:
        run_id, run = _frames_of_a_run_without_a_runner(records)
        client = asyncio.create_task(_take_a_run_without_a_runner(run, does, taken))
        await asyncio.sleep(0)
        delivered = does is _NoRunnerClient.IS_CANCELLED and client.cancel()
        await asyncio.wait({client})

    assert _propagated(client, delivered=delivered) == propagates
    assert len(taken) == frames
    assert [(event, kind) for event, kind, _raiser in failure_lines(caplog)] == [
        (event.format(run_id), kind) for event, kind in logged
    ]
    assert unretrieved == []
    assert not [step for step in _Step if step.canary in caplog.text]
    status = records.read(run_id, "songmaker", ALICE)
    assert status is not None
    assert status.state is (RunState.RUNNING if _Step.RECORD_END in failing else RunState.FAILED)


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@in_loop
async def test_a_response_without_a_runner_cancelled_in_its_first_send_is_cancelled(
    caplog, monkeypatch, spec_version
):
    """The failed disconnect report is logged, and the response's cancellation still propagates."""
    caplog.set_level(logging.DEBUG)
    client = ClientSocket(spec_version=spec_version, blocks_the_first_chunk=True)
    records = _RecordsThatFail(frozenset({_Step.DISCONNECT_REPORT}))
    async with driven(records=records, read=False) as drive:
        response = await respond_through_the_app(drive, client, monkeypatch, runners=Runners())
        await drive.clock.parked()
        response.cancel()
        await asyncio.wait({response})

    assert response.cancelled()
    assert [(event, kind) for event, kind, _raiser in failure_lines(caplog)] == [
        (_CLEANUP.format(records.started[0].run_id), "_DisconnectNotRecorded")
    ]
    status = drive.status(records.started[0].run_id)
    assert status is not None
    assert (status.state, status.failure_code) == (
        RunState.FAILED,
        FailureCode.PROVIDER_UNAVAILABLE,
    )


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
