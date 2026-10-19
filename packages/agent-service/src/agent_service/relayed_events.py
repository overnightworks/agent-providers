"""One run's events: the core's text and ending merged with the relay's tool events.

The relay's ``ToolCallBarrier`` is the one source of tool events, so the CLI's own tool lines
the core decodes are dropped. A barrier event is handed out before the core's when both are
ready, and it is accepted only when the service asks for the next event, which it does only
after it checked, counted, recorded and queued the one it was given. A ready ``FinalEvent``
therefore waits until the barrier has nothing ready, a queued event or a failure to raise.
``close`` stops the merged source and lets a containment loss of the core leave, whatever else
the cleanup met.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Final, Protocol

from agent_providers.errors import ContainmentLost
from agent_providers.events import StreamEvent, ToolCallEvent, ToolResultEvent
from agent_service.run_liveness import log_failure
from agent_service.tool_relay import ToolCallsMisused

log = logging.getLogger(__name__)

type RelayEvent = ToolCallEvent | ToolResultEvent


class _CoreEnded:
    """What the core's step returns when its stream is over."""


_CORE_ENDED: Final = _CoreEnded()


class HandedOutCalls(Protocol):
    async def next_event(self) -> RelayEvent: ...

    def accept(self, event: RelayEvent) -> None: ...

    def has_ready_event(self) -> bool: ...


class RelayedEvents:
    def __init__(
        self, core: AsyncGenerator[StreamEvent, None], calls: HandedOutCalls | None
    ) -> None:
        self._core = core
        self._calls = calls
        self._core_step: asyncio.Task[StreamEvent | _CoreEnded] | None = None
        self._relay_step: asyncio.Task[RelayEvent] | None = None
        self._unaccepted: RelayEvent | None = None

    def __aiter__(self) -> RelayedEvents:
        return self

    async def __anext__(self) -> StreamEvent:
        self._accept_the_yielded_event()
        while True:
            await self._wait_for_a_step()
            await self._wait_for_the_barrier_while_it_has_a_ready_event()
            if (relayed := self._finished_relay_event()) is not None:
                return relayed
            if (core_event := self._finished_core_event()) is not None:
                return core_event

    async def close(self) -> None:
        steps = [step for step in (self._core_step, self._relay_step) if step is not None]
        stopped_barrier_step = self._relay_step
        self._core_step = self._relay_step = None
        for step in steps:
            step.cancel()
        if steps:
            await asyncio.wait(steps)
        lost = None
        for step in steps:
            failure = None if step.cancelled() else step.exception()
            if failure is None or (
                step is stopped_barrier_step and isinstance(failure, ToolCallsMisused)
            ):
                continue
            if isinstance(failure, ContainmentLost) and lost is None:
                lost = failure
            else:
                log_failure(log, "A step of the run's event source failed at its close", failure)
        await self._close_core(lost)

    async def _close_core(self, lost: ContainmentLost | None) -> None:
        try:
            await self._core.aclose()
        except ContainmentLost as lost_in_close:
            if lost is not None:
                log_failure(log, "A second containment loss met the run's close", lost_in_close)
            lost = lost or lost_in_close
        except Exception as failure:
            if lost is None:
                raise
            log_failure(log, "Closing the run's core stream failed", failure)
        if lost is not None:
            raise lost

    def _accept_the_yielded_event(self) -> None:
        if self._unaccepted is None or self._calls is None:
            return
        event, self._unaccepted = self._unaccepted, None
        self._calls.accept(event)

    async def _wait_for_a_step(self) -> None:
        if self._core_step is None:
            self._core_step = asyncio.create_task(self._next_core_event())
        if self._relay_step is None and self._calls is not None:
            self._relay_step = asyncio.create_task(self._calls.next_event())
        steps = {step for step in (self._core_step, self._relay_step) if step is not None}
        await asyncio.wait(steps, return_when=asyncio.FIRST_COMPLETED)

    async def _wait_for_the_barrier_while_it_has_a_ready_event(self) -> None:
        """A ready core event is returned only while the barrier has nothing ready.

        The readiness is looked at again on every loop pass, because the ready call can be given
        up before the reader runs; the last look and the return have no await between them, so
        nothing can be queued in between.
        """
        core_ready = self._core_step is not None and self._core_step.done()
        reader = self._relay_step
        while core_ready and reader is not None and not reader.done() and self._barrier_is_ready():
            await asyncio.sleep(0)

    def _barrier_is_ready(self) -> bool:
        return self._calls is not None and self._calls.has_ready_event()

    async def _next_core_event(self) -> StreamEvent | _CoreEnded:
        return await anext(self._core, _CORE_ENDED)

    def _finished_relay_event(self) -> RelayEvent | None:
        step = self._relay_step
        if step is None or not step.done():
            return None
        self._relay_step = None
        self._unaccepted = step.result()
        return self._unaccepted

    def _finished_core_event(self) -> StreamEvent | None:
        step = self._core_step
        if step is None or not step.done():
            return None
        self._core_step = None
        event = step.result()
        if isinstance(event, _CoreEnded):
            raise StopAsyncIteration
        if isinstance(event, ToolCallEvent | ToolResultEvent):
            return None
        return event
