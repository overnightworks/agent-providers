"""A containment loss the runner's pump met decides the run's terminal after the stop.

Decision record 0001, H6 and A1a3 rules 9 and 11 (A1b-3f0). A run's ending is
decided when it happens, but its terminal is encoded, accepted and recorded only
once the runner's close has finished, and a ``ContainmentLost`` the pump met at a
step or at the close makes that terminal, or the fallback of a cut stream,
``failed: provider_error``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial

import anyio
import pytest
from service_support import (
    CUT_BY_ITS_SCOPE,
    BrokenClock,
    Drive,
    GatedRunner,
    StepClock,
    client_leaves_mid_run,
    client_takes_nothing_until_dropped,
    closed_inside,
    driven,
    failure_lines,
    in_loop,
    instant,
    supervision_fails,
    tool_call,
)

from agent_client.run_stream import ViolationKind
from agent_client.wire import (
    ClientLoss,
    Failed,
    FailureCode,
    RunState,
    RunStatus,
    Succeeded,
)
from agent_providers.errors import ContainmentLost, RetainedGroup
from agent_providers.events import AssistantTextEvent, FinalEvent, StreamEvent
from agent_service.config import RunConfig
from agent_service.run_events import INVALID_EVENT_MESSAGE, RUNNER_FAILED_MESSAGE
from agent_service.run_liveness import (
    RunFailures,
    RunnerOutcome,
    RunnerYielded,
    SupervisedRun,
    containment_losses,
)

FIRST = RetainedGroup(pgid=4242, generation=1)
SECOND = RetainedGroup(pgid=4343, generation=2)
FINAL = FinalEvent(text="Done.")
PROVIDER_CANARY = "GATE_SYNTHETIC_CANARY"
TERMINAL_TYPES = frozenset({"succeeded", "failed", "cancelled"})


def _final_with_assigned_payload() -> FinalEvent:
    final = FinalEvent(text="Done.")
    final.text = {"provider_payload": PROVIDER_CANARY}
    return final


def _lost(group: RetainedGroup = FIRST) -> ContainmentLost:
    return ContainmentLost(group)


def _carrying(*, cause: BaseException | None = None, context: BaseException | None = None):
    carrier = RuntimeError("carrier")
    carrier.__cause__ = cause
    carrier.__context__ = context
    return carrier


def _cyclic() -> BaseException:
    carrier, loss = RuntimeError("carrier"), _lost()
    carrier.__cause__ = loss
    loss.__context__ = carrier
    return carrier


@pytest.mark.parametrize(
    ("error", "groups"),
    [
        pytest.param(_lost(), (FIRST,), id="bare"),
        pytest.param(_carrying(cause=_lost()), (FIRST,), id="as-cause"),
        pytest.param(_carrying(context=_lost()), (FIRST,), id="as-context"),
        pytest.param(
            ExceptionGroup("outer", [ExceptionGroup("inner", [_lost()])]),
            (FIRST,),
            id="in-a-nested-exception-group",
        ),
        pytest.param(
            BaseExceptionGroup("cleanup", [asyncio.CancelledError(), _lost()]),
            (FIRST,),
            id="beside-a-cancellation",
        ),
        pytest.param(
            ExceptionGroup("two", [_lost(SECOND), _lost(FIRST)]),
            (SECOND, FIRST),
            id="two-distinct-groups-in-the-order-found",
        ),
        pytest.param(ExceptionGroup("twice", [_lost(), _lost()]), (FIRST,), id="one-group-twice"),
        pytest.param(_cyclic(), (FIRST,), id="a-cyclic-chain"),
        pytest.param(ValueError("no loss"), (), id="no-loss"),
    ],
)
def test_the_losses_of_an_error_are_its_distinct_groups_in_the_order_found(error, groups):
    assert containment_losses(error) == groups


class _RecordingWriter:
    """A ``FrameWriter`` that decides on a terminal and records every ``run_over``."""

    def __init__(self) -> None:
        self.ended = False
        self.overs: list[tuple[RetainedGroup, ...]] = []

    def heartbeat(self) -> bytes:
        return b"heartbeat"

    def decide_failure(self, code: FailureCode, message: str) -> None:
        self.ended = True

    def frame_of(self, outcome: RunnerOutcome) -> bytes | None:
        self.ended = not isinstance(outcome, RunnerYielded) or isinstance(outcome.event, FinalEvent)
        return None if self.ended else b"frame"

    def hand_over(self, losses: tuple[RetainedGroup, ...]) -> None:
        pass

    def commit_terminal(self, losses: tuple[RetainedGroup, ...]) -> None:
        pass

    def run_over(self, losses: tuple[RetainedGroup, ...]) -> None:
        self.overs.append(losses)

    def terminal_frame(self) -> bytes:
        return b"terminal"

    def client_lost(self, loss: ClientLoss) -> None:
        pass


class _TwoPointRunner(AsyncGenerator[StreamEvent, None]):
    """A runner that fails at its one step and again at its close, whatever a generator would do."""

    def __init__(self, step: StreamEvent | BaseException, close: BaseException | None) -> None:
        self._step = step
        self._close = close

    async def asend(self, value: None) -> StreamEvent:
        if isinstance(self._step, BaseException):
            raise self._step
        return self._step

    async def athrow(self, *args, **kwargs) -> StreamEvent:
        raise NotImplementedError

    async def aclose(self) -> None:
        if self._close is not None:
            raise self._close


@pytest.mark.parametrize(
    ("step", "close", "groups"),
    [
        pytest.param(
            ExceptionGroup("step", [_lost(FIRST), _lost(FIRST)]),
            _lost(SECOND),
            (FIRST, SECOND),
            id="a-step-group-twice-then-a-close-loss",
        ),
        pytest.param(
            _lost(FIRST),
            ExceptionGroup("close", [_lost(SECOND), _lost(FIRST)]),
            (FIRST, SECOND),
            id="a-step-loss-then-a-close-group-repeating-it",
        ),
        pytest.param(
            _carrying(context=_lost(FIRST)),
            _lost(FIRST),
            (FIRST,),
            id="a-carried-step-loss-met-again-at-the-close",
        ),
        pytest.param(FINAL, _lost(SECOND), (SECOND,), id="a-final-answer-then-a-close-loss"),
        pytest.param(_lost(FIRST), None, (FIRST,), id="a-step-loss-and-a-clean-close"),
        pytest.param(RuntimeError("plain"), None, (), id="no-loss"),
    ],
)
@in_loop
async def test_the_pump_hands_every_distinct_group_of_the_step_and_the_close_to_run_over_once(
    step, close, groups
):
    writer = _RecordingWriter()
    run_id = uuid.uuid4()
    supervised = SupervisedRun(
        run_id,
        writer,
        _TwoPointRunner(step, close),
        RunConfig(),
        StepClock(),
        0.0,
        RunFailures(run_id),
    )

    async with supervised:
        frames = [frame async for frame in supervised.frames()]

    assert frames == [b"terminal"]
    assert writer.overs == [groups]


def _assert_ended_by_the_lost_runner(drive: Drive) -> None:
    terminal = drive.terminal[1]
    assert isinstance(terminal, Failed), terminal
    assert (terminal.code, terminal.message) == (FailureCode.PROVIDER_ERROR, RUNNER_FAILED_MESSAGE)
    assert [type_ for type_ in drive.types if type_ in TERMINAL_TYPES] == ["failed"]
    assert drive.seqs == list(range(len(drive.seqs)))


def _assert_recorded(
    status: RunStatus | None,
    state: RunState,
    code: FailureCode | None,
    *,
    events: int,
    at: float | None,
    lost: ClientLoss | None = None,
) -> None:
    assert status is not None
    ended_at: datetime | None = None if at is None else instant(at)
    assert (status.state, status.failure_code, status.events, status.client_lost) == (
        state,
        code,
        events,
        lost,
    )
    assert status.ended_at == ended_at


def _failure_classes(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(event.split(" of run ")[0], kind) for event, kind, _ in failure_lines(caplog)]


@pytest.mark.parametrize(
    ("exhausts", "marker"),
    [
        pytest.param(False, "closed", id="a-step-raises-the-loss"),
        pytest.param(True, "close raised", id="an-exhausting-runner-whose-cleanup-raises-it"),
    ],
)
@in_loop
async def test_a_loss_at_a_step_ends_the_run_provider_error_after_the_close(
    exhausts, marker, caplog
):
    runner = GatedRunner()
    runner.hold_close()
    caplog.set_level(logging.ERROR)
    async with driven(runner) as drive:
        if exhausts:
            runner.close_error = _lost()
            runner.exhaust()
        else:
            runner.release(_lost())
        await drive.clock.parked()
        _assert_recorded(drive.status(), RunState.RUNNING, None, events=1, at=None)

        runner.release_close()
        await drive.finished()

    _assert_ended_by_the_lost_runner(drive)
    assert runner.log.index(marker) < runner.log.index("taken:failed")
    _assert_recorded(drive.status(), RunState.FAILED, FailureCode.PROVIDER_ERROR, events=2, at=0)
    assert _failure_classes(caplog) == []


@in_loop
async def test_a_closer_cut_by_its_scope_is_cancelled_after_the_place_passed_to_the_quarantine():
    runner = GatedRunner()
    runner.close_error = _lost()
    runner.hold_close()
    async with driven(runner, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.clock.parked()
        scope = anyio.CancelScope()
        closing = asyncio.create_task(closed_inside(scope, drive.frames.aclose()))
        await drive.clock.parked()
        scope.cancel()
        await drive.clock.parked()
        assert not closing.done()

        runner.release_close()

        assert await closing is CUT_BY_ITS_SCOPE
        _assert_recorded(
            drive.status(),
            RunState.FAILED,
            FailureCode.PROVIDER_ERROR,
            events=1,
            at=0,
            lost=ClientLoss.DISCONNECTED,
        )
        assert drive.slot_is_full()
        await drive.quarantine_clock.step_to_next_deadline()
        assert not drive.slot_is_full()


@pytest.mark.parametrize(
    ("runs", "final"),
    [
        pytest.param(RunConfig(), FINAL, id="a-final-answer"),
        pytest.param(RunConfig(max_bytes=10), FINAL, id="a-final-answer-over-the-byte-budget"),
        pytest.param(RunConfig(), FinalEvent(text="\ud800"), id="an-unsendable-final-answer"),
        pytest.param(RunConfig(), _final_with_assigned_payload(), id="an-invalid-final-answer"),
    ],
)
@in_loop
async def test_a_decided_ending_followed_by_a_close_loss_is_replaced_by_provider_error(
    runs, final, caplog
):
    runner = GatedRunner()
    runner.close_error = _lost()
    caplog.set_level(logging.ERROR)
    async with driven(runner, runs) as drive:
        runner.release(final)
        await drive.run_to_end()
        await drive.finished()

    _assert_ended_by_the_lost_runner(drive)
    assert runner.log.index("close raised") < runner.log.index("taken:failed")
    _assert_recorded(drive.status(), RunState.FAILED, FailureCode.PROVIDER_ERROR, events=2, at=0)
    assert _failure_classes(caplog) == []


@in_loop
async def test_an_idle_timeout_whose_stopping_step_raises_a_loss_ends_provider_error():
    runner = GatedRunner()
    runner.close_error = _lost()
    async with driven(runner) as drive:
        await drive.run_to_end()
        await drive.finished()

    _assert_ended_by_the_lost_runner(drive)
    assert runner.log.index("close raised") < runner.log.index("taken:failed")
    _assert_recorded(drive.status(), RunState.FAILED, FailureCode.PROVIDER_ERROR, events=9, at=120)


@in_loop
async def test_the_maximum_behind_a_pending_frame_sends_the_frame_then_provider_error_on_a_loss():
    runner = GatedRunner()
    runner.close_error = _lost()
    runs = RunConfig(stall_timeout_s=590, idle_timeout_s=595)
    async with driven(runner, runs, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(20)
        runner.release(AssistantTextEvent(text="Still here."))
        await drive.run_to(600)
        await drive.drain()

    assert drive.types == ["run_started", "heartbeat", "assistant_text", "failed"]
    _assert_ended_by_the_lost_runner(drive)
    _assert_recorded(drive.status(), RunState.FAILED, FailureCode.PROVIDER_ERROR, events=4, at=600)


@dataclass(frozen=True)
class _Observed:
    state: RunState
    code: FailureCode | None
    events: int
    client_lost: ClientLoss | None
    ended_at: datetime | None
    types: tuple[str, ...]
    end_of_stream: tuple[ViolationKind, ...]


async def _client_leaves_after(tool: str, drive: Drive) -> None:
    drive.took(await anext(drive.frames))
    drive.runner.release(tool_call("c", tool))
    drive.took(await anext(drive.frames))


async def _client_is_dropped_after_a_side_effecting_call(drive: Drive) -> None:
    await _client_leaves_after("edit_song", drive)
    await drive.run_to(45)


async def _supervision_fails_after_a_side_effecting_call(drive: Drive) -> None:
    await _client_leaves_after("edit_song", drive)
    assert isinstance(drive.clock, BrokenClock)
    drive.clock.arm()
    drive.clock.advance_to(15)
    with pytest.raises(RuntimeError, match="the clock broke"):
        await drive.drain()


async def _observed(
    clock: Callable[[], StepClock],
    scenario: Callable[[Drive], Awaitable[None]],
    close_error: BaseException | None,
) -> _Observed:
    runner = GatedRunner()
    runner.close_error = close_error
    async with driven(runner, clock=clock(), read=False) as drive:
        await scenario(drive)
        await drive.clock.parked()
        await drive.frames.aclose()
        status = drive.status()
        assert status is not None
        return _Observed(
            status.state,
            status.failure_code,
            status.events,
            status.client_lost,
            status.ended_at,
            tuple(drive.types),
            tuple(drive.end_of_stream()),
        )


@pytest.mark.parametrize(
    ("clock", "scenario", "control_state", "control_code", "client_lost"),
    [
        pytest.param(
            StepClock,
            client_leaves_mid_run,
            RunState.CANCELLED,
            None,
            ClientLoss.DISCONNECTED,
            id="disconnect-with-no-call",
        ),
        pytest.param(
            StepClock,
            partial(_client_leaves_after, "read_song"),
            RunState.CANCELLED,
            None,
            ClientLoss.DISCONNECTED,
            id="disconnect-after-a-read-only-call",
        ),
        pytest.param(
            StepClock,
            partial(_client_leaves_after, "edit_song"),
            RunState.FAILED,
            FailureCode.INTERRUPTED,
            ClientLoss.DISCONNECTED,
            id="disconnect-after-a-side-effecting-call",
        ),
        pytest.param(
            StepClock,
            client_takes_nothing_until_dropped,
            RunState.CANCELLED,
            None,
            ClientLoss.DROPPED,
            id="drop-with-nothing-decided",
        ),
        pytest.param(
            StepClock,
            _client_is_dropped_after_a_side_effecting_call,
            RunState.FAILED,
            FailureCode.INTERRUPTED,
            ClientLoss.DROPPED,
            id="drop-after-a-side-effecting-call",
        ),
        pytest.param(
            BrokenClock,
            supervision_fails,
            RunState.CANCELLED,
            None,
            ClientLoss.DROPPED,
            id="supervision-failure",
        ),
        pytest.param(
            partial(BrokenClock, armed=False),
            _supervision_fails_after_a_side_effecting_call,
            RunState.FAILED,
            FailureCode.INTERRUPTED,
            ClientLoss.DROPPED,
            id="supervision-failure-after-a-side-effecting-call",
        ),
    ],
)
@in_loop
async def test_a_loss_replaces_the_fallback_of_a_cut_run_without_a_frame_or_a_counted_event(
    clock, scenario, control_state, control_code, client_lost
):
    control = await _observed(clock, scenario, None)
    lossy = await _observed(clock, scenario, _lost())

    assert (control.state, control.code, control.client_lost) == (
        control_state,
        control_code,
        client_lost,
    )
    assert (lossy.state, lossy.code) == (RunState.FAILED, FailureCode.PROVIDER_ERROR)
    assert replace(lossy, state=control.state, code=control.code) == control
    assert TERMINAL_TYPES.isdisjoint(lossy.types)


@in_loop
async def test_a_client_leaving_behind_a_decided_answer_still_ends_provider_error_on_a_loss():
    runner = GatedRunner()
    runner.hold_close()
    async with driven(runner, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.run_to(5)
        runner.release(FINAL)
        await drive.run_to(6)
        closing = asyncio.create_task(drive.frames.aclose())
        await drive.clock.parked()
        runner.close_error = _lost()

        drive.clock.advance_to(8)
        runner.release_close()
        await closing

        _assert_recorded(
            drive.status(),
            RunState.FAILED,
            FailureCode.PROVIDER_ERROR,
            events=2,
            at=8,
            lost=ClientLoss.DISCONNECTED,
        )


@pytest.mark.parametrize(
    ("ending", "kind"),
    [
        pytest.param("step", "RuntimeError", id="a-step-raises-an-error-carrying-a-loss"),
        pytest.param("close", "ExceptionGroup", id="a-close-raises-a-group-holding-a-loss"),
    ],
)
@in_loop
async def test_a_loss_carried_by_another_error_counts_like_a_bare_one(ending, kind, caplog):
    runner = GatedRunner()
    caplog.set_level(logging.ERROR)
    async with driven(runner) as drive:
        if ending == "step":
            runner.release(_carrying(context=_lost()))
        else:
            runner.close_error = ExceptionGroup("close", [_lost()])
            runner.release(FINAL)
        await drive.run_to_end()
        await drive.finished()

    _assert_ended_by_the_lost_runner(drive)
    assert [line[1] for line in failure_lines(caplog)] == [kind]


@dataclass(frozen=True)
class _StepRaised:
    types: list[str]
    terminal_seq: int
    outcome: list
    failure_lines: list[tuple]
    code: FailureCode


async def _observed_when_a_step_raises(members: list[BaseException], caplog) -> _StepRaised:
    runner = GatedRunner()
    async with driven(runner) as drive:
        runner.release(BaseExceptionGroup("cleanup", members))
        await drive.run_to_end()
        outcome = await asyncio.gather(drive.reader, return_exceptions=True)
        drive.assert_nothing_left()
    terminal = drive.terminal[1]
    assert isinstance(terminal, Failed)
    return _StepRaised(
        types=drive.types[:-1],
        terminal_seq=drive.terminal[0],
        outcome=outcome,
        failure_lines=[(line[1], line[2]) for line in failure_lines(caplog)],
        code=terminal.code,
    )


@in_loop
async def test_a_loss_beside_a_cancellation_escaping_a_step_ends_provider_error_and_nothing_else(
    caplog,
):
    caplog.set_level(logging.ERROR)
    control = await _observed_when_a_step_raises([asyncio.CancelledError()], caplog)
    caplog.clear()
    lossy = await _observed_when_a_step_raises([asyncio.CancelledError(), _lost()], caplog)

    assert control.code is FailureCode.IDLE_TIMEOUT
    assert lossy.code is FailureCode.PROVIDER_ERROR
    assert replace(lossy, code=control.code) == control


@in_loop
async def test_a_final_answer_invalid_at_the_decision_ends_provider_error_without_provider_text(
    caplog,
):
    runner = GatedRunner()
    caplog.set_level(logging.DEBUG)
    async with driven(runner) as drive:
        runner.release(_final_with_assigned_payload())
        await drive.run_to_end()
        await drive.finished()

    terminal = drive.terminal[1]
    assert isinstance(terminal, Failed)
    assert (terminal.code, terminal.message) == (FailureCode.PROVIDER_ERROR, INVALID_EVENT_MESSAGE)
    _assert_recorded(drive.status(), RunState.FAILED, FailureCode.PROVIDER_ERROR, events=2, at=0)
    visible = [repr(drive.terminal), repr(drive.status()), caplog.text]
    assert [PROVIDER_CANARY in text for text in visible] == [False, False, False]


class _AnswerBlankedAtClose(GatedRunner):
    async def run(self, admitted) -> AsyncGenerator[StreamEvent, None]:
        final = FinalEvent(text="original answer")
        try:
            yield final
        finally:
            final.text = ""
            await self._closed()


@in_loop
async def test_a_final_answer_altered_by_the_runner_while_closing_still_succeeds_unchanged():
    async with driven(_AnswerBlankedAtClose()) as drive:
        await drive.run_to_end()
        await drive.finished()

    terminal = drive.terminal[1]
    assert isinstance(terminal, Succeeded)
    assert terminal.text == "original answer"
    _assert_recorded(drive.status(), RunState.SUCCEEDED, None, events=2, at=0)


@in_loop
async def test_a_decided_terminal_is_committed_only_when_the_held_close_is_released():
    runner = GatedRunner()
    runner.hold_close()
    async with driven(runner) as drive:
        await drive.run_to(5)
        runner.release(FINAL)
        await drive.run_to(6)
        _assert_recorded(drive.status(), RunState.RUNNING, None, events=1, at=None)
        assert drive.types == ["run_started"]

        drive.clock.advance_to(8)
        runner.release_close()
        await drive.finished()

    _assert_recorded(drive.status(), RunState.SUCCEEDED, None, events=2, at=8)
