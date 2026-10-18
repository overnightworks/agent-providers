"""One CLI turn's child on an anchor that holds a pool slot, and its reap.

The pool bounds the CLI processes a host runs at once, shared by probes and real
turns. ``anchored_turn_child`` reserves a slot, opens the child on its anchor and,
whatever ends the turn, reaps the child behind a shield before the slot is freed.
A leader that cannot be shown gone is retained by :mod:`agent_providers.lost_groups`
and keeps its slot.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from functools import partial

import anyio

from agent_providers import process
from agent_providers.constants import (
    CLAUDE_CLI_SIGTERM_GRACE_SECONDS,
    CLAUDE_CLI_ZOMBIE_REAP_TIMEOUT_SECONDS,
)
from agent_providers.errors import ContainmentLost
from agent_providers.lost_groups import retain_lost_group
from agent_providers.spawn import (
    AnchoredChild,
    AnchorStateError,
    ChildIdentityLost,
    ChildProcess,
    open_anchored_pipes,
)

log = logging.getLogger(__name__)


class CliProcessPoolSaturated(Exception):
    """The pool was at its limit, so no child was started."""


@dataclass(eq=False)
class CliProcessReservation:
    """One pool slot, and the home reservation that is released with it."""

    pool: CliProcessPool
    home_reservation: process.PrivateHomeReservation | None = None


class CliProcessPool:
    """The process-wide slots of one CLI, shared by probes and real turns.

    Its own lock, since probes touch it from plain threads as well as the event
    loop.
    """

    def __init__(self, *, limit: int, name: str) -> None:
        self.limit = limit
        self.name = name
        self._lock = threading.Lock()
        self._reservations: set[CliProcessReservation] = set()

    def limit_message(self) -> str:
        return (
            f"{self.name} process pool is at its concurrency limit "
            f"({self.limit}); refusing to start another"
        )

    def reserve(
        self,
        home_reservation: process.PrivateHomeReservation | None = None,
    ) -> CliProcessReservation | None:
        """Reserve one slot before spawning, or ``None`` when the pool is full."""
        reservation = CliProcessReservation(pool=self, home_reservation=home_reservation)
        with self._lock:
            pool_is_full = len(self._reservations) >= self.limit
            if not pool_is_full:
                self._reservations.add(reservation)
        if pool_is_full:
            log.error(self.limit_message())
            return None
        return reservation

    def release(self, reservation: CliProcessReservation) -> None:
        with self._lock:
            self._reservations.discard(reservation)
        if reservation.home_reservation is not None:
            reservation.home_reservation.release()


async def _reap_behind_shield(child: AnchoredChild, reservation: CliProcessReservation) -> None:
    """Run the one bounded reap of ``child`` to completion, whatever cancels the caller.

    The reap runs in its own task, started once, and is awaited behind an
    AnyIO shield (ASGI cancel scopes) and ``asyncio.shield`` (direct
    ``Task.cancel()``).  Every direct cancellation that arrives meanwhile is
    absorbed until the task is done.  The reap's own exception then wins over
    any cancellation; otherwise the cancellation is delivered afterwards.
    """
    reap_task = asyncio.create_task(_reap_process_group(child, reservation))
    try:
        with anyio.CancelScope(shield=True):
            await asyncio.shield(reap_task)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await _outlast_direct_cancellations(reap_task)
        raise
    await anyio.lowlevel.checkpoint_if_cancelled()


async def _outlast_direct_cancellations(reap_task: asyncio.Task[None]) -> None:
    while not reap_task.done():
        with suppress(asyncio.CancelledError):
            await asyncio.shield(reap_task)
    reap_task.result()


async def _reap_process_group(child: AnchoredChild, reservation: CliProcessReservation) -> None:
    """End the turn's leader through its anchor and free its pool slot, or retain it.

    A leader whose exit is published is released without a signal. A running one
    gets SIGTERM, then SIGKILL after the grace, and is released once its exit is
    published. The slot and the home are freed only after ``release()``. A leader
    that has not exited by the bound after SIGKILL, whose anchor was lost, or
    that the kernel did not reap is retained instead, nothing is freed, and
    ``ContainmentLost`` names the retention: a slot freed beside a group whose end
    nobody knows would put a new run next to it.
    """
    try:
        if child.returncode is None and not await _end_running_leader(child):
            raise _retain_unreleased_leader(child, reservation)
        child.release()
    except (ChildIdentityLost, AnchorStateError) as failure:
        raise _retain_unreleased_leader(child, reservation) from failure
    reservation.pool.release(reservation)


async def _end_running_leader(child: AnchoredChild) -> bool:
    child.signal_group(signal.SIGTERM)
    if await _exit_published_within(child, CLAUDE_CLI_SIGTERM_GRACE_SECONDS):
        return True
    child.signal_group(signal.SIGKILL)
    return await _exit_published_within(child, CLAUDE_CLI_ZOMBIE_REAP_TIMEOUT_SECONDS)


async def _exit_published_within(child: AnchoredChild, seconds: float) -> bool:
    try:
        async with asyncio.timeout(seconds):
            await child.exited()
    except TimeoutError:
        return False
    return True


def _retain_unreleased_leader(
    child: AnchoredChild, reservation: CliProcessReservation
) -> ContainmentLost:
    group = retain_lost_group(child, partial(reservation.pool.release, reservation))
    return ContainmentLost(group)


@asynccontextmanager
async def anchored_turn_child(
    pool: CliProcessPool,
    child: ChildProcess,
    *,
    home_reservation: process.PrivateHomeReservation,
    stream_buffer_limit: int | None = None,
) -> AsyncIterator[AnchoredChild]:
    """Run one async turn's child on an anchor that holds a pool slot.

    The reservation never leaves this frame and nothing lies between the start
    and the ``yield``. Whatever ends the body, the reap runs behind its shield,
    so its own exception wins over the body's and a cancellation is delivered
    after it. A missing binary propagates as the ``FileNotFoundError`` of the spawn.
    """
    reservation = pool.reserve(home_reservation)
    if reservation is None:
        home_reservation.release()
        raise CliProcessPoolSaturated(pool.limit_message())
    try:
        anchored = await open_anchored_pipes(child, stream_buffer_limit=stream_buffer_limit)
    except BaseException:
        pool.release(reservation)
        raise
    try:
        yield anchored
    finally:
        await _reap_behind_shield(anchored, reservation)
