"""The place of a run whose process group is not confirmed gone (decision record 0001, line 20).

When a run's runner met a ``ContainmentLost``, the provider core still holds the
group's leader and its slot until ``lost_groups.confirm_group_gone`` answers
``True``. ``Quarantine`` takes over the run's admission place at the run's one end
and asks again at a fixed interval, for every group the runner met. It frees
exactly that place once every one of them answered ``True``, never earlier, and
never gives up. It signals and counts no group itself: the core's confirmation does.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final
from uuid import UUID

from agent_runtime.errors import RetainedGroup
from agent_runtime_service.admission import Lease
from agent_runtime_service.run_liveness import RunClock

log = logging.getLogger(__name__)

QUARANTINE_RETRY_SECONDS: Final = 5.0

GroupConfirmation = Callable[[RetainedGroup], Awaitable[bool]]


class Quarantine:
    def __init__(self, clock: RunClock, confirm: GroupConfirmation) -> None:
        self._clock = clock
        self._confirm = confirm
        self._retries: dict[UUID, asyncio.Task[None]] = {}

    def hold(self, run_id: UUID, lease: Lease | None, groups: tuple[RetainedGroup, ...]) -> None:
        """Keep the run's place taken until every group is confirmed gone.

        A run without a lease has no place, but its groups are still retried.
        """
        if run_id in self._retries:
            raise ValueError(f"The place of run {run_id} is already in quarantine.")
        held = None if lease is None else lease.transfer()
        retry = asyncio.create_task(
            self._free_when_gone(run_id, held, groups), name=f"quarantine:{run_id}"
        )
        self._retries[run_id] = retry
        retry.add_done_callback(lambda _: self._retries.pop(run_id, None))
        log.error(
            "Not confirmed gone after SIGKILL: process %s %s of run %s;"
            " the run's place stays taken until then",
            "group" if len(groups) == 1 else "groups",
            ", ".join(str(group.pgid) for group in groups),
            run_id,
        )

    async def _free_when_gone(
        self, run_id: UUID, lease: Lease | None, groups: tuple[RetainedGroup, ...]
    ) -> None:
        unconfirmed = list(groups)
        while unconfirmed:
            await self._clock.wait_until(self._clock() + QUARANTINE_RETRY_SECONDS)
            unconfirmed = [group for group in unconfirmed if not await self._confirmed(group)]
        if lease is not None:
            lease.release()
        log.info("Every process group of run %s is gone; its place is free", run_id)

    async def _confirmed(self, group: RetainedGroup) -> bool:
        """An attempt that raises counts as not confirmed, as the core's host contract says."""
        try:
            return await self._confirm(group)
        except Exception:
            return False
