"""When a run's frames go out: the heartbeat, the idle timeout and the maximum duration.

``run_events`` decides what each frame says; this module decides when frames
are produced and when a run is over (decision record 0001, H6). Three parties
cooperate, each a task created with ``asyncio.create_task``:

* the *pump* owns the runner's generator: it takes one step when the
  supervisor asks, and it alone cancels and closes the generator;
* the *supervisor* waits for the runner and for the clock, writes every frame
  through the one ``FrameWriter``, and puts it into a one-slot queue;
* the stream (``SupervisedRun.frames``) forwards the queued frames.

The supervisor never waits for the client without also watching the maximum
duration, so a client that stops reading cannot keep a runner alive. A client
which takes no frame for ``stall_timeout_s`` is dropped sooner: the runner is
stopped once and the stream ends without a terminal. Every
ending stops the runner first and queues the terminal only after the
runner's close has finished. The shielded wait for that close mirrors the
provider core's reap of a streaming process
(``agent_providers.claude.provider``) without importing it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import traceback
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from types import TracebackType
from typing import Final, Protocol
from uuid import UUID

import anyio

from agent_client.wire import ClientLoss, FailureCode
from agent_providers.events import StreamEvent
from agent_service.config import RunConfig

log = logging.getLogger(__name__)

IDLE_TIMEOUT_MESSAGE: Final = "The run was silent for too long."
LIMIT_EXCEEDED_MESSAGE: Final = "The run reached its maximum duration."


def log_failure(logger: logging.Logger, event: str, failure: BaseException) -> None:
    """Log ``event`` at ERROR with the failure's class and the stack positions it passed.

    Each position is ``file:line:function``, the file by its name: from the
    failure's traceback, or for a ``RunStepCancelled`` the positions its
    cancellation passed. The failure's message, notes and chained exceptions
    are never logged, because any of them may carry a run's provider text.
    """
    positions = (
        failure.positions
        if isinstance(failure, RunStepCancelled)
        else _stack_positions(failure.__traceback__)
    )
    logger.error("%s: %s at %s", event, type(failure).__name__, ", ".join(positions))


def _stack_positions(stack: TracebackType | None) -> tuple[str, ...]:
    """The ``file:line:function`` positions a traceback passed, detached from its frames."""
    return tuple(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in traceback.StackSummary.extract(traceback.walk_tb(stack), lookup_lines=False)
    )


class RunClock(Protocol):
    """The one clock every timer of a run reads, so tests can move it by hand."""

    def __call__(self) -> float: ...

    async def wait_until(self, deadline: float) -> None: ...


class LoopClock:
    """The running event loop's monotonic time."""

    def __call__(self) -> float:
        return asyncio.get_running_loop().time()

    async def wait_until(self, deadline: float) -> None:
        await asyncio.sleep(max(0.0, deadline - self()))


@dataclass(frozen=True)
class RunnerYielded:
    event: StreamEvent


@dataclass(frozen=True)
class RunnerExhausted:
    """The runner ended without a final answer."""


@dataclass(frozen=True)
class RunnerRaised:
    error: Exception


RunnerOutcome = RunnerYielded | RunnerExhausted | RunnerRaised


class FrameWriter(Protocol):
    """The one writer of a run's frames and of its record; ``run_events`` implements it."""

    ended: bool

    def heartbeat(self) -> bytes: ...

    def failed(self, code: FailureCode, message: str) -> bytes: ...

    def frame_of(self, outcome: RunnerOutcome) -> bytes | None:
        """The frame of a runner outcome, or nothing for an event that says nothing."""
        ...

    def run_over(self) -> None: ...

    def client_lost(self, loss: ClientLoss) -> None: ...


class Action(Enum):
    EVENT = auto()
    LIMIT = auto()
    IDLE = auto()
    HEARTBEAT = auto()
    DROP = auto()
    WAIT = auto()


@dataclass(frozen=True)
class Deadlines:
    """The instants a run is waiting for.

    ``idle`` is None while no step waits on the runner, ``stall`` while the
    client's slot is vacant.
    """

    heartbeat: float
    maximum: float
    idle: float | None = None
    stall: float | None = None

    @property
    def next(self) -> float:
        dues = (self.heartbeat, self.maximum, self.idle, self.stall)
        return min(due for due in dues if due is not None)


def decide(now: float, event_ready: bool, deadlines: Deadlines) -> Action:
    """What to do at ``now``: drop a stalled client, else an event, the maximum, idle, the beat."""
    if deadlines.stall is not None and now >= deadlines.stall:
        return Action.DROP
    if event_ready:
        return Action.EVENT
    if now >= deadlines.maximum:
        return Action.LIMIT
    if deadlines.idle is not None and now >= deadlines.idle:
        return Action.IDLE
    if now >= deadlines.heartbeat:
        return Action.HEARTBEAT
    return Action.WAIT


class _FrameSlot:
    """The one frame waiting for the client; closing it ends the client's stream."""

    def __init__(self) -> None:
        self._frame: bytes | None = None
        self._closed = False
        self._for_client = asyncio.Event()
        self._vacated = asyncio.Event()
        self._vacated.set()

    @property
    def vacant(self) -> bool:
        return self._frame is None

    def fill(self, frame: bytes) -> None:
        self._frame = frame
        self._vacated.clear()
        self._for_client.set()

    def close(self) -> None:
        self._closed = True
        self._for_client.set()

    async def wait_vacant(self) -> None:
        await self._vacated.wait()

    async def take(self) -> bytes | None:
        await self._for_client.wait()
        frame, self._frame = self._frame, None
        if not self._closed:
            self._for_client.clear()
        self._vacated.set()
        return frame


async def _finished(task: asyncio.Task[object]) -> asyncio.CancelledError | None:
    """Wait until ``task`` is done, however often the waiter is cancelled meanwhile.

    Returns the first cancellation of the waiter, for the caller to raise once
    it has retrieved the task's outcome. The task is never cancelled by this
    wait, and AnyIO's level cancellation (which Starlette uses on a client's
    disconnect) cannot cut it short.
    """
    interrupted: asyncio.CancelledError | None = None
    while not task.done():
        try:
            with anyio.CancelScope(shield=True):
                await asyncio.wait({task})
        except asyncio.CancelledError as cancelled:  # NOSONAR(S7497) returned once done (rule 10)
            interrupted = interrupted or cancelled
    return interrupted


class FailedPart(Enum):
    """Which part of a run's ending a failure belongs to (decision record 0001, A1a2 item 8)."""

    SUPERVISION = "supervision"
    CLEANUP = "cleanup"


class RunStepCancelled(Exception):
    """A step of a run's ending was cancelled although no cancellation was requested.

    It stands in for that step's ``CancelledError``, because an ASGI server
    below spec 2.4 swallows a ``CancelledError`` the app raises, and the
    failure would vanish. It keeps the stack positions the cancellation
    passed, never its frames, message or chain.
    """

    def __init__(self, part: FailedPart, cancelled: asyncio.CancelledError) -> None:
        super().__init__(f"A step of the run's {part.value} was cancelled without a request")
        self.part = part
        self.positions = _stack_positions(cancelled.__traceback__)


@dataclass(frozen=True)
class _Failure:
    part: FailedPart
    error: BaseException


@dataclass(frozen=True)
class _Exit:
    """What leaving a run raises, if anything, and the failures it logs, in order."""

    raised: BaseException | None
    logged: tuple[_Failure, ...]


def _exit_of(
    failures: Sequence[_Failure],
    *,
    cancellation: asyncio.CancelledError | None,
    body_failure: BaseException | None,
    cancel_requested: bool,
) -> _Exit:
    """The one decision of what leaving a run raises; every other failure is logged once.

    ``failures`` are every failure the run's steps met, in the order they
    happened; a failure met twice counts once, at its first part. A
    cancellation is delivered when a cancellation request is on the leaving
    task (``cancel_requested``); a ``CancelledError`` a step raised without
    one is that step's failure, recorded among ``failures`` as ``RunStepCancelled``.

    1. A delivered cancellation always wins: one caught while the run was
       torn down is raised, and the body's outcome is left to propagate
       while a cancellation request is on the task. That covers a
       cancellation that arrived in the body, and a close (``GeneratorExit``)
       by a closer whose cancellation is on its way out.
    2. Otherwise the first cleanup failure is raised. A ``CancelledError``
       of the body without a request is the body's outcome, so a cleanup
       failure is raised over it too.
    3. Otherwise the first supervision failure is raised, unless the body
       raised: then the body's outcome stands.
    """
    distinct: list[_Failure] = []
    for failure in failures:
        if all(failure.error is not known.error for known in distinct):
            distinct.append(failure)
    if cancellation is not None:
        return _Exit(cancellation, tuple(distinct))
    if cancel_requested:
        return _Exit(None, tuple(distinct))
    cleanup = [failure for failure in distinct if failure.part is FailedPart.CLEANUP]
    if cleanup:
        raised = cleanup[0]
    elif distinct and body_failure is None:
        raised = distinct[0]
    else:
        return _Exit(None, tuple(distinct))
    return _Exit(raised.error, tuple(failure for failure in distinct if failure is not raised))


class RunFailures:
    """The failures one run's ending met, in the order they happened.

    No step of a run's ending raises its failure past this collection: each
    one is recorded here, and leaving the run raises or logs what ``_exit_of``
    decides.
    """

    def __init__(self, run_id: UUID) -> None:
        self._run_id = run_id
        self._failures: list[_Failure] = []
        self._converted: dict[
            int, tuple[weakref.ref[asyncio.CancelledError], RunStepCancelled]
        ] = {}

    def attempt(self, part: FailedPart, step: Callable[[], None]) -> None:
        """Take ``step``, recording its failure instead of raising it.

        A ``CancelledError`` is the step's failure too, unless a cancellation
        request is on the task: then it is delivered, and it propagates.
        """
        try:
            step()
        except Exception as failure:
            self.add(part, failure)
        except asyncio.CancelledError as cancelled:
            if _cancel_requested(asyncio.current_task()):
                raise
            self.add(part, cancelled)

    def add(self, part: FailedPart, failure: BaseException) -> BaseException:
        """Record ``failure`` under ``part``, a stray cancellation as ``RunStepCancelled``.

        Returns the failure as recorded, for a step that raises it on. A
        cancellation recorded again is the same ``RunStepCancelled``, so that
        it still counts once, at its first part.
        """
        if isinstance(failure, asyncio.CancelledError):
            failure = self._converted_once(part, failure)
        self._failures.append(_Failure(part, failure))
        return failure

    def _converted_once(
        self, part: FailedPart, cancelled: asyncio.CancelledError
    ) -> RunStepCancelled:
        """The one ``RunStepCancelled`` of ``cancelled``, told apart by identity alone.

        The stray is held weakly and made inert once converted: a failure that
        later propagates reaches this collection through its traceback, and a
        second failure raised while the stray was handled links it as its
        context, so the stray's frames must not live on through either.
        """
        known = self._converted.get(id(cancelled))
        if known is not None and known[0]() is cancelled:
            return known[1]
        converted = RunStepCancelled(part, cancelled)
        traceback.clear_frames(cancelled.__traceback__)
        cancelled.__traceback__ = None
        self._converted[id(cancelled)] = (weakref.ref(cancelled), converted)
        return converted

    @property
    def met_any(self) -> bool:
        """Whether a step has failed yet."""
        return bool(self._failures)

    def raised(self, cancellation: asyncio.CancelledError | None) -> BaseException | None:
        """What a stop of the runner raises: the decided failure, nothing logged yet."""
        return _exit_of(
            self._failures, cancellation=cancellation, body_failure=None, cancel_requested=False
        ).raised

    def leave(
        self,
        *,
        cancellation: asyncio.CancelledError | None,
        body_failure: BaseException | None,
    ) -> None:
        """Log every failure the decision does not raise, then raise the one it does."""
        decided = _exit_of(
            self._failures,
            cancellation=cancellation,
            body_failure=body_failure,
            cancel_requested=_cancel_requested(asyncio.current_task()),
        )
        for failure in decided.logged:
            log_failure(
                log, f"The {failure.part.value} of run {self._run_id} failed", failure.error
            )
        if decided.raised is not None:
            raise decided.raised

    @contextmanager
    def leaving(self) -> Iterator[None]:
        """Leave a run that has no supervisor by the same decision."""
        body_failure: BaseException | None = None
        try:
            yield
        except BaseException as failure:
            body_failure = failure
            raise
        finally:
            self.leave(cancellation=None, body_failure=body_failure)


def _cancel_requested(task: asyncio.Task[object] | None) -> bool:
    """Whether a cancellation request is on ``task``: a cancellation it meets is delivered."""
    return task is not None and task.cancelling() > 0


def _failure_of(task: asyncio.Task[object]) -> BaseException | None:
    """The finished task's failure, retrieved so that the loop never reports it as forgotten.

    A task that ended cancelled with a cancellation request on it was shut
    down, which is no failure. One that ended cancelled with no request on
    it failed with that cancellation, whoever called ``cancel()`` before.
    """
    if task.cancelled() and _cancel_requested(task):
        return None
    try:
        return task.exception()
    except asyncio.CancelledError as cancelled:
        return cancelled


async def _stopped(
    tasks: list[asyncio.Task[object]],
) -> tuple[asyncio.CancelledError | None, list[BaseException]]:
    """Cancel ``tasks`` and wait until each is done; the waiter's cancellation, their failures."""
    for task in tasks:
        task.cancel()
    interrupted: asyncio.CancelledError | None = None
    for task in tasks:
        waiter_cancelled = await _finished(task)
        interrupted = interrupted or waiter_cancelled
    failures = [failure for task in tasks if (failure := _failure_of(task)) is not None]
    return interrupted, failures


async def _one_step(events: AsyncGenerator[StreamEvent, None]) -> RunnerOutcome:
    try:
        return RunnerYielded(await anext(events))
    except StopAsyncIteration:
        return RunnerExhausted()
    except Exception as error:
        return RunnerRaised(error)


class SupervisedRun:
    """A runner's events as the queued frames of one run, until the run is over.

    Use it as an async context manager around ``frames()``. Leaving it stops
    the runner exactly once and returns only after the runner's close has
    finished.
    """

    def __init__(
        self,
        run_id: UUID,
        wire: FrameWriter,
        events: AsyncGenerator[StreamEvent, None],
        runs: RunConfig,
        clock: RunClock,
        started_at: float,
        failures: RunFailures,
    ) -> None:
        self._run_id = run_id
        self._wire = wire
        self._events = events
        self._runs = runs
        self._clock = clock
        self._slot = _FrameSlot()
        self._step_wanted = asyncio.Event()
        self._outcome_ready = asyncio.Event()
        self._outcome: RunnerOutcome | None = None
        self._stopping = False
        self._started_at = started_at
        self._maximum_due = started_at + runs.max_duration_s
        self._heartbeat_due = started_at + runs.heartbeat_interval_s
        self._idle_due: float | None = None
        self._stall_due: float | None = None
        self._pump: asyncio.Task[None]
        self._supervisor: asyncio.Task[None]
        self._cleanup: asyncio.Task[None] | None = None
        self._failures = failures
        self._drop_reported = False

    async def __aenter__(self) -> SupervisedRun:
        self._pump = asyncio.create_task(self._pump_runner())
        self._supervisor = asyncio.create_task(self._supervise())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> None:
        """Leave once the runner is closed, raising or logging what ``_exit_of`` decides."""
        self._supervisor.cancel()
        waited = await _finished(self._supervisor)
        reaped = await self._reaped()
        if (failure := _failure_of(self._supervisor)) is not None:
            self._failures.add(FailedPart.SUPERVISION, failure)
        if (failure := _failure_of(self._reaping())) is not None:
            self._failures.add(FailedPart.CLEANUP, failure)
        self._failures.leave(cancellation=waited or reaped, body_failure=exc)

    async def frames(self) -> AsyncIterator[bytes]:
        while (frame := await self._slot.take()) is not None:
            yield frame

    async def _pump_runner(self) -> None:
        try:
            while not self._stopping:
                await self._step_wanted.wait()
                self._step_wanted.clear()
                outcome = await _one_step(self._events)
                if isinstance(outcome, RunnerRaised) and self._stopping:
                    raise outcome.error
                self._outcome = outcome
                self._outcome_ready.set()
        finally:
            await self._events.aclose()

    async def _supervise(self) -> None:
        try:
            await self._drive()
        except Exception as failure:
            self._supervision_failed(failure)
        except asyncio.CancelledError as cancelled:
            if _cancel_requested(asyncio.current_task()):
                raise
            self._supervision_failed(cancelled)
        finally:
            try:
                await self._stop()
            finally:
                self._slot.close()

    async def _drive(self) -> None:
        self._request_step(self._started_at)
        while True:
            match await self._next_action():
                case Action.EVENT:
                    if not await self._handle_outcome():
                        return
                case Action.HEARTBEAT:
                    self._beat()
                case Action.DROP:
                    await self._drop()
                    return
                case Action.IDLE:
                    await self._end(self._idle_timeout())
                    return
                case Action.LIMIT:
                    await self._end(self._limit_exceeded())
                    return

    def _idle_timeout(self) -> bytes:
        return self._wire.failed(FailureCode.IDLE_TIMEOUT, IDLE_TIMEOUT_MESSAGE)

    def _limit_exceeded(self) -> bytes:
        return self._wire.failed(FailureCode.LIMIT_EXCEEDED, LIMIT_EXCEEDED_MESSAGE)

    @property
    def _deadlines(self) -> Deadlines:
        stall = None if self._slot.vacant else self._stall_due
        return Deadlines(self._heartbeat_due, self._maximum_due, self._idle_due, stall)

    @property
    def _blocked_deadlines(self) -> Deadlines:
        """What a supervisor waiting for a vacant slot watches: the maximum and the stall."""
        return Deadlines(math.inf, self._maximum_due, None, self._deadlines.stall)

    @property
    def _ending_deadlines(self) -> Deadlines:
        """What a supervisor queuing an ending watches: the stall alone, the rest is past."""
        return Deadlines(math.inf, math.inf, None, self._deadlines.stall)

    async def _next_action(self) -> Action:
        while True:
            deadlines = self._deadlines
            action = decide(self._clock(), self._outcome_ready.is_set(), deadlines)
            if action is not Action.WAIT:
                return action
            await self._wait_for(self._outcome_ready.wait(), deadlines.next)

    async def _handle_outcome(self) -> bool:
        """Queue the frame of the runner's event; False once the run has ended."""
        assert self._outcome is not None
        outcome, self._outcome = self._outcome, None
        self._idle_due = None
        self._outcome_ready.clear()
        frame = self._wire.frame_of(outcome)
        if self._wire.ended:
            assert frame is not None
            await self._end(frame)
            return False
        if frame is not None and not await self._deliver(frame):
            return False
        match decide(self._clock(), False, self._blocked_deadlines):
            case Action.DROP:
                await self._drop()
                return False
            case Action.LIMIT:
                await self._end(self._limit_exceeded())
                return False
        self._request_step(self._clock())
        return True

    def _request_step(self, since: float) -> None:
        self._idle_due = since + self._runs.idle_timeout_s
        self._step_wanted.set()

    def _beat(self) -> None:
        if self._slot.vacant:
            self._queue(self._wire.heartbeat())
        else:
            self._heartbeat_due = self._clock() + self._runs.heartbeat_interval_s

    def _fill(self, frame: bytes) -> None:
        self._slot.fill(frame)
        self._stall_due = self._clock() + self._runs.stall_timeout_s

    def _queue(self, frame: bytes) -> None:
        self._fill(frame)
        self._heartbeat_due = self._clock() + self._runs.heartbeat_interval_s

    async def _deliver(self, frame: bytes) -> bool:
        """Queue a frame once the client has taken the last one; False if the run ended first."""
        while not self._slot.vacant:
            deadlines = self._blocked_deadlines
            match decide(self._clock(), False, deadlines):
                case Action.DROP:
                    await self._drop()
                    return False
                case Action.LIMIT:
                    await self._end(self._limit_exceeded(), pending=frame)
                    return False
            await self._wait_for(self._slot.wait_vacant(), deadlines.next)
        self._queue(frame)
        return True

    async def _end(self, terminal: bytes, pending: bytes | None = None) -> None:
        """Stop the runner, then queue the frame already encoded before the terminal, then it."""
        await self._stop()
        for frame in (pending, terminal):
            if frame is not None and not await self._fill_when_vacant(frame):
                return

    async def _fill_when_vacant(self, frame: bytes) -> bool:
        """Fill the slot once the client took the last frame; False if it was dropped."""
        while not self._slot.vacant:
            deadlines = self._ending_deadlines
            if decide(self._clock(), False, deadlines) is Action.DROP:
                await self._drop()
                return False
            await self._wait_for(self._slot.wait_vacant(), deadlines.next)
        self._fill(frame)
        return True

    async def _drop(self) -> None:
        log.warning("The client of run %s stopped taking frames and was dropped", self._run_id)
        self._report_drop()
        await self._stop()

    def _supervision_failed(self, failure: BaseException) -> None:
        self._failures.add(FailedPart.SUPERVISION, failure)
        self._report_drop()

    def _report_drop(self) -> None:
        """Latch ``dropped`` once, whether the client was dropped or the supervision failed."""
        if not self._drop_reported:
            self._drop_reported = True
            self._failures.attempt(
                FailedPart.SUPERVISION, lambda: self._wire.client_lost(ClientLoss.DROPPED)
            )

    async def _wait_for(self, trigger: Coroutine[object, object, object], deadline: float) -> None:
        tasks = [
            asyncio.create_task(trigger),
            asyncio.create_task(self._clock.wait_until(deadline)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            interrupted, stopped = await _stopped(tasks)
            failures = [self._failures.add(FailedPart.SUPERVISION, failure) for failure in stopped]
        if interrupted is not None:
            raise interrupted
        if failures:
            raise failures[0]

    async def _stop(self) -> None:
        """Stop the runner once, then raise what ``_exit_of`` says ends the stop."""
        raised = self._failures.raised(await self._reaped())
        if raised is not None:
            raise raised

    def _reaping(self) -> asyncio.Task[None]:
        """The one task that stops the runner and ends the record, started on first use."""
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._reap())
        return self._cleanup

    async def _reaped(self) -> asyncio.CancelledError | None:
        return await _finished(self._reaping())

    async def _reap(self) -> None:
        self._stopping = True
        self._pump.cancel()
        await asyncio.wait({self._pump})
        closing_failure = _failure_of(self._pump)
        if closing_failure is not None:
            log_failure(log, f"Closing the runner of run {self._run_id} failed", closing_failure)
        self._failures.attempt(FailedPart.CLEANUP, self._wire.run_over)
