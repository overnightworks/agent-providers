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
duration, so a client that stops reading cannot keep a runner alive. Every
ending stops the runner first and queues the terminal only after the
runner's close has finished. The shielded wait for that close mirrors the
provider core's reap of a streaming process
(``agent_providers.claude.provider``) without importing it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final, Protocol
from uuid import UUID

import anyio

from agent_client.wire import FailureCode
from agent_providers.events import StreamEvent
from agent_service.config import RunConfig

log = logging.getLogger(__name__)

IDLE_TIMEOUT_MESSAGE: Final = "The run was silent for too long."
LIMIT_EXCEEDED_MESSAGE: Final = "The run reached its maximum duration."


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
    """The one writer of a run's frames; ``run_events`` implements it."""

    ended: bool

    def heartbeat(self) -> bytes: ...

    def failed(self, code: FailureCode, message: str) -> bytes: ...

    def frame_of(self, outcome: RunnerOutcome) -> bytes | None:
        """The frame of a runner outcome, or nothing for an event that says nothing."""
        ...


class Action(Enum):
    EVENT = auto()
    LIMIT = auto()
    IDLE = auto()
    HEARTBEAT = auto()
    WAIT = auto()


@dataclass(frozen=True)
class Deadlines:
    """The instants a run is waiting for; ``idle`` is None while no step waits on the runner."""

    heartbeat: float
    maximum: float
    idle: float | None = None

    @property
    def next(self) -> float:
        return min(due for due in (self.heartbeat, self.maximum, self.idle) if due is not None)


def decide(now: float, event_ready: bool, deadlines: Deadlines) -> Action:
    """What to do at ``now``: a runner event first, then the maximum, idle, the heartbeat."""
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


async def _finished(task: asyncio.Task[object]) -> None:
    """Wait until ``task`` is done, however often the waiter is cancelled meanwhile.

    The first cancellation is re-raised once the task is done. The task is
    never cancelled by this wait, and AnyIO's level cancellation (which
    Starlette uses on a client's disconnect) cannot cut it short.
    """
    interrupted: asyncio.CancelledError | None = None
    while not task.done():
        try:
            with anyio.CancelScope(shield=True):
                await asyncio.wait({task})
        except asyncio.CancelledError as cancelled:
            interrupted = interrupted or cancelled
    if interrupted is not None:
        raise interrupted


async def _cancelled(tasks: list[asyncio.Task[object]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


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
        self._pump: asyncio.Task[None]
        self._supervisor: asyncio.Task[None]
        self._cleanup: asyncio.Task[None] | None = None

    async def __aenter__(self) -> SupervisedRun:
        self._pump = asyncio.create_task(self._pump_runner())
        self._supervisor = asyncio.create_task(self._supervise())
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._supervisor.cancel()
        try:
            await _finished(self._supervisor)
        finally:
            await self._stop()
        failure = None if self._supervisor.cancelled() else self._supervisor.exception()
        if failure is None:
            return
        if exc_type is None:
            raise failure
        log.error("The supervision of run %s failed", self._run_id, exc_info=failure)

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

    async def _next_action(self) -> Action:
        while True:
            deadlines = Deadlines(self._heartbeat_due, self._maximum_due, self._idle_due)
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

    def _queue(self, frame: bytes) -> None:
        self._slot.fill(frame)
        self._heartbeat_due = self._clock() + self._runs.heartbeat_interval_s

    async def _deliver(self, frame: bytes) -> bool:
        """Queue a frame once the client has taken the last one; False if the maximum came first."""
        while not self._slot.vacant:
            if self._clock() >= self._maximum_due:
                await self._end(self._limit_exceeded(), pending=frame)
                return False
            await self._wait_for(self._slot.wait_vacant(), self._maximum_due)
        self._queue(frame)
        return True

    async def _end(self, terminal: bytes, pending: bytes | None = None) -> None:
        """Stop the runner, then queue the frame already encoded before the terminal, then it."""
        await self._stop()
        for frame in (pending, terminal):
            if frame is not None:
                await self._slot.wait_vacant()
                self._slot.fill(frame)

    async def _wait_for(self, trigger: Coroutine[object, object, object], deadline: float) -> None:
        tasks = [
            asyncio.create_task(trigger),
            asyncio.create_task(self._clock.wait_until(deadline)),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await _cancelled(tasks)
        for task in done:
            task.result()

    async def _stop(self) -> None:
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._reap())
        await _finished(self._cleanup)

    async def _reap(self) -> None:
        self._stopping = True
        self._pump.cancel()
        await asyncio.wait({self._pump})
        closing_failure = None if self._pump.cancelled() else self._pump.exception()
        if closing_failure is not None:
            log.error("Closing the runner of run %s failed", self._run_id, exc_info=closing_failure)
