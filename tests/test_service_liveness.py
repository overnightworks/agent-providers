"""A run that goes quiet or runs too long ends with a typed failure, whatever its client does.

Decision record 0001, H6 (A1a3 piece 1). Every scenario drives ``run_frames``
with a ``StepClock`` the test advances from one parked deadline to the next and
a ``GatedRunner`` whose events the test releases, so nothing sleeps.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

import pytest
from service_support import (
    GatedRunner,
    ScriptedRunner,
    StepClock,
    TokenMint,
    decoded_events,
    post_run,
    run_body,
    run_headers,
    served,
    service_config,
)

from agent_client.run_stream import StreamViolation
from agent_client.sse import RunStreamDecoder
from agent_client.wire import (
    Failed,
    FailureCode,
    Heartbeat,
    RunEvent,
    RunRequest,
    RunStarted,
    Succeeded,
)
from agent_providers.events import AssistantTextEvent, FinalEvent, ToolCallEvent
from agent_service.app import create_app
from agent_service.config import RunConfig
from agent_service.identity import Principal
from agent_service.routing import Cli
from agent_service.run_events import run_frames
from agent_service.run_liveness import (
    IDLE_TIMEOUT_MESSAGE,
    Action,
    Deadlines,
    decide,
)
from agent_service.runners import AdmittedRun, Runners

SONGMAKER = service_config().apps["songmaker"]
QUIET = AssistantTextEvent(text="")
TEXT = AssistantTextEvent(text="Still here.")
FINAL = FinalEvent(text="Done.")


def in_loop(scenario: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    @functools.wraps(scenario)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(scenario(*args, **kwargs))

    return run


class Drive:
    """One run read by a client task that a test can stall, on a clock the test moves."""

    def __init__(
        self,
        runner: GatedRunner,
        runs: RunConfig,
        clock: StepClock,
        *,
        read: bool = True,
    ) -> None:
        self.runner = runner
        self.clock = clock
        self.run_id = uuid.uuid4()
        admitted = AdmittedRun(
            run_id=self.run_id,
            app="songmaker",
            principal=Principal("https://keycloak.test/realms/agent-test", "user-alice"),
            request=RunRequest.model_validate(run_body()),
            route=Cli("claude-main"),
        )
        self.frames = run_frames(admitted, runner, SONGMAKER, runs, clock)
        self.events: list[tuple[float, RunEvent]] = []
        self._decoder = RunStreamDecoder()
        self._may_read = asyncio.Event()
        self._may_read.set()
        self.reader = asyncio.create_task(self._read()) if read else None

    async def _read(self) -> None:
        while True:
            await self._may_read.wait()
            try:
                frame = await anext(self.frames)
            except StopAsyncIteration:
                return
            self.took(frame)

    def took(self, frame: bytes) -> None:
        for item in self._decoder.feed(frame):
            assert not isinstance(item, StreamViolation), item
            self.events.append((self.clock.now, item))
            self.runner.log.append(f"taken:{item.type}")

    def stall(self) -> None:
        self._may_read.clear()

    def resume(self) -> None:
        self._may_read.set()

    @property
    def types(self) -> list[str]:
        return [event.type for _, event in self.events]

    @property
    def seqs(self) -> list[int]:
        return [event.seq for _, event in self.events]

    @property
    def terminal(self) -> tuple[float, RunEvent]:
        return self.events[-1]

    async def run_to(self, instant: float) -> None:
        while (deadline := await self.clock.parked()) is not None and deadline <= instant:
            self.clock.advance_to(deadline)
        self.clock.advance_to(instant)
        await self.clock.parked()

    async def run_to_end(self) -> list[float]:
        """Step from deadline to deadline until the run is over; the deadlines it waited for."""
        deadlines = []
        while (deadline := await self.clock.parked()) is not None:
            deadlines.append(deadline)
            self.clock.advance_to(deadline)
        return deadlines

    async def speak_quietly_every(self, seconds: int, until: int) -> None:
        for instant in range(seconds, until + 1, seconds):
            await self.run_to(instant)
            self.runner.release(QUIET)

    async def finished(self) -> None:
        assert self.reader is not None
        await self.reader
        self.assert_nothing_left()

    def assert_nothing_left(self) -> None:
        assert asyncio.all_tasks() == {asyncio.current_task()}
        assert self.runner.closes == 1


@asynccontextmanager
async def driven(
    runner: GatedRunner | None = None,
    runs: RunConfig | None = None,
    clock: StepClock | None = None,
    *,
    read: bool = True,
) -> AsyncIterator[Drive]:
    drive = Drive(runner or GatedRunner(), runs or RunConfig(), clock or StepClock(), read=read)
    try:
        yield drive
    finally:
        drive.runner.release_close()
        if drive.reader is not None:
            drive.reader.cancel()
        await asyncio.gather(*([drive.reader] if drive.reader else []), return_exceptions=True)
        await drive.frames.aclose()


def _close_steps(runner: GatedRunner) -> list[str]:
    return [entry for entry in runner.log if not entry.startswith("taken:")]


def _failed_at(drive: Drive, instant: float, code: FailureCode) -> Failed:
    at, terminal = drive.terminal
    assert isinstance(terminal, Failed), terminal
    assert (at, terminal.code) == (instant, code)
    return terminal


@pytest.mark.parametrize(
    ("now", "event_ready", "deadlines", "expected"),
    [
        pytest.param(600, True, Deadlines(615, 600), Action.EVENT, id="event-at-the-maximum"),
        pytest.param(120, True, Deadlines(120, 600, 120), Action.EVENT, id="event-at-idle"),
        pytest.param(600, False, Deadlines(615, 600, 600), Action.LIMIT, id="maximum-beats-idle"),
        pytest.param(600, False, Deadlines(600, 600), Action.LIMIT, id="maximum-beats-heartbeat"),
        pytest.param(120, False, Deadlines(120, 600, 120), Action.IDLE, id="idle-beats-heartbeat"),
        pytest.param(15, False, Deadlines(15, 600, 120), Action.HEARTBEAT, id="heartbeat-due"),
        pytest.param(120, False, Deadlines(120, 600), Action.HEARTBEAT, id="no-idle-no-step"),
        pytest.param(14, False, Deadlines(15, 600, 120), Action.WAIT, id="nothing-due"),
    ],
)
def test_what_falls_due_at_one_instant_is_handled_in_a_fixed_order(
    now, event_ready, deadlines, expected
):
    assert decide(now, event_ready, deadlines) is expected


@in_loop
async def test_a_silent_run_beats_every_interval_then_ends_idle_timeout():
    async with driven() as drive:
        deadlines = await drive.run_to_end()
        await drive.finished()

    assert deadlines == [15, 30, 45, 60, 75, 90, 105, 120]
    assert drive.types == ["run_started", *["heartbeat"] * 7, "failed"]
    assert drive.seqs == list(range(9))
    assert [at for at, _ in drive.events[1:]] == deadlines
    assert _failed_at(drive, 120, FailureCode.IDLE_TIMEOUT).message == IDLE_TIMEOUT_MESSAGE


@in_loop
async def test_the_announced_cadence_is_the_configured_cadence():
    async with driven(runs=RunConfig(heartbeat_interval_s=20)) as drive:
        await drive.run_to(40)

    started = drive.events[0][1]
    assert isinstance(started, RunStarted)
    assert started.heartbeat_interval_s == 20
    assert [(at, e.type) for at, e in drive.events[1:]] == [(20, "heartbeat"), (40, "heartbeat")]


@in_loop
async def test_the_deadlines_count_from_run_started_however_late_the_client_asks_for_more():
    async with driven(read=False) as drive:
        drive.took(await anext(drive.frames))
        drive.clock.advance_to(5)
        first_beat = asyncio.create_task(anext(drive.frames))

        assert await drive.clock.parked() == 15
        drive.clock.advance_to(15)
        drive.took(await first_beat)

    assert [at for at, _ in drive.events] == [0, 15]


def test_the_run_started_of_a_served_run_announces_the_configured_cadence():
    mint = TokenMint()
    app = create_app(
        service_config(runs={"heartbeat_interval_s": 20}),
        runners=Runners(cli=ScriptedRunner([FINAL])),
        fetch_key_set=mint.fetch_key_set,
    )
    with served(app) as base_url:
        response = post_run(base_url, run_headers(mint.workload(), mint.user()))

    started = decoded_events(response.content)[0]
    assert isinstance(started, RunStarted)
    assert started.heartbeat_interval_s == 20


@in_loop
async def test_a_run_that_speaks_late_is_still_stopped_at_the_maximum_from_its_start():
    async with driven() as drive:
        await drive.run_to(50)
        for instant in range(50, 600, 10):
            await drive.run_to(instant)
            drive.runner.release(TEXT)
        await drive.run_to(600)
        await drive.finished()

    _failed_at(drive, 600, FailureCode.LIMIT_EXCEEDED)
    assert drive.runner.cancellations == 1


@in_loop
async def test_a_run_that_streams_text_often_enough_gets_no_heartbeat_and_succeeds():
    async with driven() as drive:
        for instant in range(10, 590, 10):
            await drive.run_to(instant)
            drive.runner.release(TEXT)
        await drive.run_to(585)
        drive.runner.release(FINAL)
        await drive.finished()

    assert "heartbeat" not in drive.types
    assert isinstance(drive.terminal[1], Succeeded)


@in_loop
async def test_a_client_that_stopped_reading_finds_no_pile_of_heartbeats():
    async with driven() as drive:
        await drive.run_to(0)
        drive.stall()
        await drive.run_to(75)
        drive.resume()
        await drive.run_to(76)

        assert [at for at, event in drive.events if isinstance(event, Heartbeat)] == [15, 75]
        assert drive.seqs == [0, 1, 2]
        drive.stall()


@pytest.mark.parametrize("spoken", [QUIET, TEXT], ids=["empty-fragment", "text"])
@in_loop
async def test_a_runner_that_speaks_within_the_idle_window_is_not_idle(spoken):
    async with driven() as drive:
        for instant in (100, 200, 300):
            await drive.run_to(instant)
            drive.runner.release(spoken)
        await drive.run_to(400)
        drive.runner.release(FINAL)
        await drive.finished()

    assert isinstance(drive.terminal[1], Succeeded)


@in_loop
async def test_a_client_that_stalls_does_not_make_a_busy_runner_idle():
    async with driven() as drive:
        await drive.run_to(0)
        drive.stall()
        for index in range(5):
            drive.runner.release(AssistantTextEvent(text=f"line {index}"))
        await drive.run_to(200)
        assert drive.runner.cancellations == 0
        drive.resume()
        await drive.run_to_end()
        await drive.finished()

    assert drive.types.count("assistant_text") == 5
    _failed_at(drive, 320, FailureCode.IDLE_TIMEOUT)


@in_loop
async def test_a_runner_cancelled_at_the_maximum_is_stopped_even_when_the_client_does_not_read():
    async with driven() as drive:
        await drive.run_to(0)
        drive.stall()
        await drive.speak_quietly_every(100, 500)
        await drive.run_to(600)

        assert (drive.runner.cancellations, drive.runner.closes) == (1, 1)
        assert "failed" not in drive.types
        drive.resume()
        await drive.finished()

    assert drive.types == ["run_started", "heartbeat", "heartbeat", "failed"]
    assert drive.seqs == [0, 1, 2, 3]
    _failed_at(drive, 600, FailureCode.LIMIT_EXCEEDED)


@in_loop
async def test_a_frame_waiting_for_a_stalled_client_goes_out_before_the_limit_terminal():
    async with driven() as drive:
        await drive.run_to(0)
        drive.stall()
        await drive.speak_quietly_every(100, 400)
        await drive.run_to(500)
        drive.runner.release(TEXT)
        await drive.run_to(600)
        assert drive.runner.closes == 1
        drive.resume()
        await drive.finished()

    assert drive.types == ["run_started", "heartbeat", "heartbeat", "assistant_text", "failed"]
    assert drive.seqs == [0, 1, 2, 3, 4]


@in_loop
async def test_a_run_that_succeeds_just_before_the_maximum_is_left_alone():
    async with driven() as drive:
        await drive.speak_quietly_every(100, 500)
        await drive.run_to(599)
        drive.runner.release(FINAL)
        await drive.finished()

    assert isinstance(drive.terminal[1], Succeeded)
    assert drive.runner.cancellations == 0


@in_loop
async def test_a_heartbeat_tick_never_cancels_or_restarts_the_runners_step():
    async with driven() as drive:
        await drive.run_to(40)
        assert (drive.runner.steps, drive.runner.cancellations) == (1, 0)
        drive.runner.release(TEXT)
        await drive.run_to(41)
        drive.runner.release(FINAL)
        await drive.finished()

    assert drive.types == ["run_started", "heartbeat", "heartbeat", "assistant_text", "succeeded"]
    assert drive.seqs[3] == 3
    assert drive.runner.cancellations == 0


@pytest.mark.parametrize(
    ("last_quiet", "event_at_600", "expected_tail"),
    [
        pytest.param(500, FINAL, ["succeeded"], id="final-answer-at-the-maximum"),
        pytest.param(500, TEXT, ["assistant_text", "failed"], id="text-at-the-maximum"),
        pytest.param(480, None, ["failed"], id="idle-and-maximum-coincide"),
    ],
)
@in_loop
async def test_an_event_already_yielded_at_the_maximum_is_handled_before_the_maximum(
    last_quiet, event_at_600, expected_tail
):
    async with driven() as drive:
        await drive.speak_quietly_every(100, 400)
        await drive.run_to(last_quiet)
        drive.runner.release(QUIET)
        await drive.run_to(599)
        if event_at_600 is not None:
            drive.runner.release(event_at_600)
        drive.clock.advance_to(600)
        await drive.finished()

    assert drive.types[-len(expected_tail) :] == expected_tail
    if expected_tail[-1] == "failed":
        _failed_at(drive, 600, FailureCode.LIMIT_EXCEEDED)
    else:
        assert drive.terminal[0] == 600


@in_loop
async def test_no_further_runner_step_is_asked_for_once_the_maximum_has_arrived():
    async with driven() as drive:
        await drive.speak_quietly_every(100, 500)
        await drive.run_to(599)
        steps_waiting = drive.runner.steps
        drive.runner.release(TEXT)
        drive.clock.advance_to(600)
        await drive.finished()

    _failed_at(drive, 600, FailureCode.LIMIT_EXCEEDED)
    assert drive.runner.steps == steps_waiting


@pytest.mark.parametrize("ending", ["succeeded", "provider_error", "idle_timeout", "limit"])
@in_loop
async def test_the_runner_is_closed_before_the_terminal_is_taken(ending):
    async with driven() as drive:
        if ending == "succeeded":
            drive.runner.release(FINAL)
        elif ending == "provider_error":
            drive.runner.release(RuntimeError("boom"))
        elif ending == "limit":
            await drive.speak_quietly_every(100, 500)
            await drive.run_to(600)
        await drive.run_to_end()
        await drive.finished()

    terminal_type = "succeeded" if ending == "succeeded" else "failed"
    assert drive.runner.log.index("closed") < drive.runner.log.index(f"taken:{terminal_type}")
    assert drive.types.count(terminal_type) == 1
    assert drive.types[-1] == terminal_type


@pytest.mark.parametrize(
    ("last_quiet", "due", "code"),
    [(0, 120, FailureCode.IDLE_TIMEOUT), (500, 600, FailureCode.LIMIT_EXCEEDED)],
    ids=["idle-timeout", "limit-exceeded"],
)
@in_loop
async def test_the_terminal_waits_for_the_runners_close_to_finish(last_quiet, due, code):
    runner = GatedRunner()
    runner.hold_close()
    async with driven(runner) as drive:
        await drive.speak_quietly_every(100, last_quiet)
        await drive.run_to(due)

        assert (runner.cancellations, runner.closes) == (1, 1)
        assert "failed" not in drive.types
        runner.release_close()
        await drive.finished()

    _failed_at(drive, due, code)
    assert runner.log[-3:] == ["closing", "closed", "taken:failed"]


@pytest.mark.parametrize("cancellations", [1, 2, 3])
@in_loop
async def test_a_consumer_cancelled_while_the_close_runs_returns_only_after_the_close(
    cancellations,
):
    runner = GatedRunner()
    runner.hold_close()
    async with driven(runner) as drive:
        await drive.run_to(120)
        for _ in range(cancellations):
            drive.reader.cancel()
            await drive.clock.parked()
        assert not drive.reader.done()
        assert _close_steps(runner) == ["closing"]
        runner.release_close()
        with pytest.raises(asyncio.CancelledError):
            await drive.reader

    assert (runner.closes, runner.cancellations) == (1, 1)
    assert _close_steps(runner) == ["closing", "closed"]
    drive.assert_nothing_left()


@in_loop
async def test_closing_the_stream_returns_only_after_the_runners_close_has_finished():
    runner = GatedRunner()
    runner.hold_close()
    async with driven(runner, read=False) as drive:
        drive.took(await anext(drive.frames))
        first_beat = asyncio.create_task(anext(drive.frames))
        await drive.run_to(15)
        drive.took(await first_beat)
        await drive.run_to(120)
        assert _close_steps(runner) == ["closing"]

        closing = asyncio.create_task(drive.frames.aclose())
        await drive.clock.parked()
        assert not closing.done()
        runner.release_close()
        await closing

    assert (runner.closes, runner.cancellations) == (1, 1)
    assert _close_steps(runner) == ["closing", "closed"]
    drive.assert_nothing_left()


@pytest.mark.parametrize("ending", ["idle_timeout", "succeeded"])
@in_loop
async def test_a_runner_whose_close_fails_is_logged_and_the_terminal_is_unchanged(
    ending, caplog
):
    runner = GatedRunner()
    runner.close_error = OSError("reap failed")
    caplog.set_level(logging.ERROR)
    async with driven(runner) as drive:
        if ending == "succeeded":
            runner.release(FINAL)
        await drive.run_to_end()
        await drive.finished()

    if ending == "idle_timeout":
        _failed_at(drive, 120, FailureCode.IDLE_TIMEOUT)
    else:
        assert isinstance(drive.terminal[1], Succeeded)
    failures = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(failures) == 1
    assert str(drive.run_id) in failures[0].getMessage()
    assert isinstance(failures[0].exc_info[1], OSError)


class _BrokenClock(StepClock):
    async def wait_until(self, deadline: float) -> None:
        raise RuntimeError("the clock broke")


@in_loop
async def test_a_failure_in_the_supervision_stops_the_runner_and_cuts_the_stream():
    async with driven(clock=_BrokenClock(), read=False) as drive:
        drive.took(await anext(drive.frames))
        with pytest.raises(RuntimeError, match="the clock broke"):
            await anext(drive.frames)

    assert drive.types == ["run_started"]
    drive.assert_nothing_left()


@in_loop
async def test_a_side_effecting_call_followed_by_silence_still_ends_idle_timeout():
    async with driven() as drive:
        drive.runner.release(ToolCallEvent(tool_use_id="call-1", name="edit_song", input={}))
        await drive.run_to_end()
        await drive.finished()

    assert drive.types[1] == "tool_call"
    assert _failed_at(drive, 120, FailureCode.IDLE_TIMEOUT).message == IDLE_TIMEOUT_MESSAGE
