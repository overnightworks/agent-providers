"""A client that leaves its one waiting frame untaken is dropped; its runner stops once.

Decision record 0001, H6 (A1a3 piece 3). Every scenario drives ``run_frames`` with a
``StepClock`` and a ``GatedRunner`` in a ``SteppedLoop``, so nothing sleeps and no
socket is involved. ``Drive(read=False)`` is a client that takes only the frames the
test asks for. The over-the-socket proof is not in CI (decision record, item 21).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid

import pytest
from service_support import (
    QUIET,
    ClientSocket,
    Drive,
    GatedRunner,
    StepClock,
    driven,
    in_loop,
    respond_through_the_app,
)

from agent_runtime_client.run_stream import ViolationKind
from agent_runtime_client.sse import RunStreamDecoder
from agent_runtime_client.wire import Succeeded
from agent_runtime.events import AssistantTextEvent, FinalEvent, ToolCallEvent
from agent_runtime_service.config import RunConfig

TEXT = AssistantTextEvent(text="Still here.")
FINAL = FinalEvent(text="Done.")
ENDINGS_WITH_A_STALL_WINDOW_OF_THEIR_OWN = RunConfig(
    heartbeat_interval_s=15, stall_timeout_s=50, idle_timeout_s=50, max_duration_s=60
)


def _warnings_naming(
    caplog: pytest.LogCaptureFixture, run_id: uuid.UUID
) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and str(run_id) in record.getMessage()
    ]


@in_loop
async def test_a_client_taking_nothing_after_the_start_is_dropped_one_window_after_the_first_beat():
    async with driven(read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(44)
        assert drive.runner.closes == 0
        await drive.run_to(45)
        assert (drive.runner.cancellations, drive.runner.closes) == (1, 1)
        await drive.drain()

    assert drive.types == ["run_started", "heartbeat"]
    assert drive.end_of_stream() == [ViolationKind.TRUNCATED]


@in_loop
async def test_a_client_taking_each_frame_inside_its_window_is_not_dropped():
    async with driven() as drive:
        await drive.run_to(99)
        drive.stall()
        drive.runner.release(TEXT)
        drive.runner.release(TEXT)
        await drive.run_to(128)
        assert drive.runner.closes == 0
        drive.resume()
        await drive.run_to(130)
        drive.runner.release(FINAL)
        await drive.finished()

    assert isinstance(drive.terminal[1], Succeeded)


@in_loop
async def test_a_dropped_client_gets_the_frame_in_the_slot_and_nothing_queued_after_it(caplog):
    async with driven(read=False) as drive:
        drive.took(await anext(drive.frames))
        drive.runner.release(TEXT)
        drive.runner.release(AssistantTextEvent(text="Held."))
        await drive.run_to(29)
        assert drive.runner.closes == 0
        await drive.run_to(30)
        assert drive.runner.closes == 1
        await drive.run_to(40)
        await drive.drain()

    assert drive.types == ["run_started", "assistant_text"]
    assert drive.seqs == [0, 1]
    assert drive.end_of_stream() == [ViolationKind.TRUNCATED]
    assert len(_warnings_naming(caplog, drive.run_id)) == 1


@pytest.mark.parametrize(
    ("runs", "releases", "dropped_at"),
    [
        pytest.param(RunConfig(stall_timeout_s=120), [], 135, id="idle-timeout"),
        pytest.param(
            ENDINGS_WITH_A_STALL_WINDOW_OF_THEIR_OWN, [(40, QUIET)], 65, id="limit-exceeded"
        ),
        pytest.param(RunConfig(), [(20, FINAL)], 45, id="succeeded"),
        pytest.param(RunConfig(), [(20, RuntimeError("boom"))], 45, id="provider-error"),
    ],
)
@in_loop
async def test_an_ending_waiting_behind_a_stalled_client_is_dropped_without_a_terminal(
    runs, releases, dropped_at
):
    async with driven(runs=runs, read=False) as drive:
        drive.took(await anext(drive.frames))
        for instant, event in releases:
            await drive.run_to(instant)
            drive.runner.release(event)
        await drive.run_to(dropped_at - 1)
        assert asyncio.all_tasks() - {asyncio.current_task()}
        await drive.run_to(dropped_at)
        assert drive.runner.closes == 1
        await drive.drain()

    assert drive.types == ["run_started", "heartbeat"]
    assert drive.end_of_stream() == [ViolationKind.TRUNCATED]
    drive.assert_nothing_left()


@in_loop
async def test_each_frame_an_ending_queues_gets_a_stall_window_of_its_own():
    async with driven(runs=ENDINGS_WITH_A_STALL_WINDOW_OF_THEIR_OWN, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(20)
        drive.runner.release(TEXT)
        await drive.run_to(60)
        assert drive.runner.closes == 1
        drive.clock.advance_to(62)
        drive.took(await anext(drive.frames))
        assert await drive.clock.parked() == 112
        await drive.run_to(100)
        assert drive.runner.closes == 1
        await drive.run_to(112)
        await drive.drain()

    assert drive.types == ["run_started", "heartbeat", "assistant_text"]
    assert drive.end_of_stream() == [ViolationKind.TRUNCATED]
    drive.assert_nothing_left()


@in_loop
async def test_a_dropped_client_is_not_sent_the_event_the_runner_yielded_at_the_stall_instant():
    async with driven(read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(44)
        drive.runner.release(ToolCallEvent(tool_use_id="late", name="read_song", input={}))
        drive.clock.advance_to(45)
        await drive.clock.parked()
        assert (drive.runner.steps, drive.runner.closes) == (1, 1)
        await drive.drain()

    assert drive.types == ["run_started", "heartbeat"]


class _ClockPassingWhileHandling(StepClock):
    """A clock that jumps to an instant on the first reading a named method makes."""

    def __init__(self, method: str, instant: float) -> None:
        super().__init__()
        self._method = method
        self._instant = instant
        self.jumped = False

    def __call__(self) -> float:
        reader = sys._getframe(1).f_code.co_name
        if reader == self._method and not self.jumped:
            self.now = self._instant
            self.jumped = True
        return super().__call__()


@in_loop
async def test_no_runner_step_is_asked_for_after_the_stall_instant_passed_while_handling_an_event():
    clock = _ClockPassingWhileHandling("_handle_outcome", 45)
    async with driven(clock=clock, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(44)
        steps_waiting = drive.runner.steps
        drive.runner.release(QUIET)
        await drive.clock.parked()
        assert clock.jumped, "the clock never jumped: no reading was made while handling the event"
        assert (drive.runner.steps, drive.runner.closes) == (steps_waiting, 1)
        await drive.drain()

    assert drive.types == ["run_started", "heartbeat"]


async def _read_at_a_pace_of(seconds: float, drive: Drive) -> None:
    async for frame in drive.frames:
        drive.took(frame)
        await drive.clock.wait_until(drive.clock.now + seconds)


@in_loop
async def test_a_client_taking_a_frame_every_29_seconds_keeps_its_chatty_run():
    async with driven(read=False) as drive:
        reader = asyncio.create_task(_read_at_a_pace_of(29, drive))
        for _ in range(9):
            drive.runner.release(TEXT)
        drive.runner.release(FINAL)
        await drive.run_to_end()
        await reader

    assert drive.types.count("assistant_text") == 9
    assert isinstance(drive.terminal[1], Succeeded)
    drive.assert_nothing_left()


@pytest.mark.parametrize(
    ("closing_while", "cancellations"),
    [("close-held", 1), ("close-held", 3), ("close-done", 0)],
)
@in_loop
async def test_closing_the_stream_of_a_dropped_client_waits_for_the_one_close(
    closing_while, cancellations
):
    runner = GatedRunner()
    runner.hold_close()
    async with driven(runner, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(45)
        if closing_while == "close-done":
            runner.release_close()
            await drive.clock.parked()
        closing = asyncio.create_task(drive.frames.aclose())
        for _ in range(cancellations):
            await drive.clock.parked()
            closing.cancel()
        await drive.clock.parked()
        assert closing.done() == (closing_while == "close-done")
        runner.release_close()
        await asyncio.gather(closing, return_exceptions=True)

    assert (runner.closes, runner.cancellations) == (1, 1)
    drive.assert_nothing_left()


@in_loop
async def test_the_pump_and_supervisor_are_gone_once_the_dropped_runner_is_closed():
    runner = GatedRunner()
    runner.hold_close()
    async with driven(runner, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(45)
        assert asyncio.all_tasks() - {asyncio.current_task()}
        runner.release_close()
        await drive.clock.parked()
        assert not asyncio.all_tasks() - {asyncio.current_task()}
        await drive.drain()

    drive.assert_nothing_left()


@in_loop
async def test_a_run_behind_a_blocked_starlette_send_is_dropped_at_the_stall_deadline(
    caplog, monkeypatch
):
    client = ClientSocket(blocks_the_second_chunk=True)
    async with driven(read=False) as drive:
        response = await respond_through_the_app(drive, client, monkeypatch)
        await drive.run_to(59)
        assert drive.runner.closes == 0
        await drive.run_to(60)
        assert drive.runner.closes == 1
        assert len(_warnings_naming(caplog, client.run_id)) == 1
        client.released.set()
        await response

    assert client.messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    decoder = RunStreamDecoder()
    assert [event.type for event in decoder.feed(client.body)] == [
        "run_started",
        "heartbeat",
        "heartbeat",
    ]
    assert [violation.kind for violation in decoder.close()] == [ViolationKind.TRUNCATED]
