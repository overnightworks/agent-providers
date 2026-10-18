"""One CLI turn's child on an anchor that holds a pool slot, and its reap.

The pool bounds the CLI processes a host runs at once, shared by probes and real
turns. ``anchored_turn_child`` reserves a slot, opens the child on its anchor and,
whatever ends the turn, reaps the child behind a shield before the slot is freed.
A group that cannot be shown empty is retained by :mod:`agent_providers.lost_groups`
and its leader keeps the slot.
"""

from __future__ import annotations

import asyncio
import logging
import math
import signal
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial

from agent_providers.errors import ContainmentLost
from agent_providers.lost_groups import release_leader_if_gone, retain_lost_group
from agent_providers.outlasting import outlast
from agent_providers.process import HomeReservation
from agent_providers.spawn import AnchoredChild, ChildProcess, open_anchored_pipes

log = logging.getLogger(__name__)


class CliProcessPoolSaturated(Exception):
    """The pool was at its limit, so no child was started."""


@dataclass(eq=False)
class CliProcessReservation:
    """One pool slot, and the home reservation that is released with it."""

    pool: CliProcessPool
    home_reservation: HomeReservation | None = None


@dataclass(frozen=True)
class ReapBounds:
    """How long a turn's reap waits, stated once by the CLI that owns the pool."""

    sigterm_grace_seconds: float
    confirmation_seconds: float

    def __post_init__(self) -> None:
        for field_name in ("sigterm_grace_seconds", "confirmation_seconds"):
            value = getattr(self, field_name)
            if not (math.isfinite(value) and value >= 0):
                raise ValueError(f"{field_name} must be finite and not negative, got {value!r}")


class CliProcessPool:
    """The process-wide slots of one CLI, shared by probes and real turns.

    Its own lock, since probes touch it from plain threads as well as the event
    loop. The probes' reap is the bounded runner's, so only anchored turns read
    ``reap``.
    """

    def __init__(self, *, limit: int, name: str, reap: ReapBounds) -> None:
        self.limit = limit
        self.name = name
        self.reap = reap
        self._lock = threading.Lock()
        self._reservations: set[CliProcessReservation] = set()

    def limit_message(self) -> str:
        return (
            f"{self.name} process pool is at its concurrency limit "
            f"({self.limit}); refusing to start another"
        )

    def reserve(
        self,
        home_reservation: HomeReservation | None = None,
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

    The reap runs in its own task, started once, and ``outlast`` (which owns
    the order) awaits it behind an AnyIO shield and ``asyncio.shield``.  The
    reap's own exception wins over any cancellation; otherwise the caller's
    cancellation is delivered afterwards.
    """
    await outlast(asyncio.create_task(_reap_process_group(child, reservation)))


async def _reap_process_group(child: AnchoredChild, reservation: CliProcessReservation) -> None:
    """Show the turn's process group empty and free its pool slot, or retain it.

    A running leader gets SIGTERM and the grace. Then, on every exit, the group
    is SIGKILLed and counted within the confirmation bound from that first
    SIGKILL. Only a count that found the group empty in time, the leader's
    published exit and ``release()`` free the slot and the home. A live, late or
    uncountable group, an exit that stays unpublished, a lost anchor or any
    other failure of these steps retains the leader instead, nothing is freed,
    and ``ContainmentLost`` names the retention: a slot freed beside a group
    whose end nobody knows would put a new run next to it.
    """
    try:
        group_is_gone = await _confirm_group_gone_and_release(child, reservation.pool.reap)
    except Exception as failure:
        raise _retain_unreleased_leader(child, reservation) from failure
    if not group_is_gone:
        raise _retain_unreleased_leader(child, reservation)
    reservation.pool.release(reservation)


async def _confirm_group_gone_and_release(child: AnchoredChild, bounds: ReapBounds) -> bool:
    if child.returncode is None:
        await _offer_sigterm(child, bounds.sigterm_grace_seconds)
    result = await child.kill_until_gone(within=bounds.confirmation_seconds)
    exit_published = result.confirmed and await _exit_published_within(
        child, bounds.confirmation_seconds
    )
    return release_leader_if_gone(child, counted_empty=result.confirmed, exit_known=exit_published)


async def _offer_sigterm(child: AnchoredChild, grace_seconds: float) -> None:
    child.signal_group(signal.SIGTERM)
    await _exit_published_within(child, grace_seconds)


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
    home_reservation: HomeReservation,
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
