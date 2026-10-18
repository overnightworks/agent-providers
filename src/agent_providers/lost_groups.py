"""Lost process groups retained by name, and the one attempt that may free their place.

``retain_lost_group`` stores a leader whose group could not be confirmed empty and
returns the :class:`~agent_providers.errors.RetainedGroup` that names that retention.
It closes nothing and signals nothing. ``confirm_group_gone`` makes one attempt per call.

**The attempt.** The group is looked up by its whole value, pgid and generation, so an
old retention never reaches a newer one with the same pgid. An unknown or cleaned-up
group gives ``True`` and nothing is signalled. Otherwise one SIGKILL and one ``/proc``
count go through the leader's anchor, in the attempt's own worker thread, never on a loop
thread. Only an ``EMPTY`` count with a committed exit leads on, on the calling loop's
thread and in this order, each at most once: the leader's ``release()``, the slot's
release, the entry's removal, then ``True``. Any other count, or an exit not yet
committed, gives ``False`` and releases nothing; the next attempt kills and counts again.
The attempt's exit evidence is the committed ``returncode``, not an awaited ``exited()``,
which waits forever once the opening loop has closed. ``release_leader_if_gone`` is the one
decider of the leader's release, for this attempt and for the turn's reap alike.

**One attempt at a time.** A call for a group that has an attempt returns ``False`` at
once and signals nothing. The attempt has up to two holders, the call and its census
worker, and ends when the last one leaves. The worker leaves under the registry lock from
its own thread, before it publishes, so the exclusion ends without any loop. The registry
lock is held only for lookups, holder changes, flag writes, insertion and removal; never
across an ``await``, a call into the child, ``release_slot`` or the worker's start.

**Errors** propagate unchanged, keep the entry and never give ``True``. A
``ChildIdentityLost`` is terminal: the anchor is lost, every later attempt raises it
again without a kernel call, and the place stays held until the process exits. After a
failed ``kill_and_census`` or ``release()`` the next attempt starts again at the kill;
after a failed ``release_slot`` it resumes at the slot's release, without a signal. A
failed worker start propagates its error and ends the exclusion at once when the starter
won the start claim; otherwise the claimed worker ends it.

**Cancellation** propagates at once and releases nothing. The worker finishes its kill and
count and its outcome is dropped; until it has, every call for the group returns ``False``.

**The loop.** ``release()`` and ``release_slot`` run on the thread of the loop that awaits
the call. Like every ``AnchoredChild`` call outside the leader's own, the call belongs on
the loop that opened the child, or on any loop after that one closed. The registry holds
no loop-bound state.

**A group's validity.** A ``RetainedGroup`` is valid only in the process that minted it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import threading
from collections.abc import Callable
from dataclasses import dataclass

from agent_providers.errors import RetainedGroup
from agent_providers.process import ClaimedStartThread
from agent_providers.procfs import GroupCensus
from agent_providers.spawn import AnchoredChild


@dataclass
class _Attempt:
    holders: int = 1


@dataclass
class _LostGroup:
    child: AnchoredChild
    release_slot: Callable[[], None]
    leader_released: bool = False
    attempt: _Attempt | None = None


_registry_lock = threading.Lock()
_lost_groups: dict[RetainedGroup, _LostGroup] = {}
_generations = itertools.count(1)


def retain_lost_group(child: AnchoredChild, release_slot: Callable[[], None]) -> RetainedGroup:
    pid = child.pid
    with _registry_lock:
        group = RetainedGroup(pid, next(_generations))
        _lost_groups[group] = _LostGroup(child, release_slot)
    return group


async def confirm_group_gone(group: RetainedGroup) -> bool:
    entry, attempt = _begin_attempt(group)
    if entry is None:
        return True
    if attempt is None:
        return False
    try:
        if not entry.leader_released and not await _release_leader_if_empty(entry, attempt):
            return False
        entry.release_slot()
        _remove(group)
        return True
    finally:
        _leave(entry, attempt)


def _begin_attempt(group: RetainedGroup) -> tuple[_LostGroup | None, _Attempt | None]:
    with _registry_lock:
        entry = _lost_groups.get(group)
        if entry is None or entry.attempt is not None:
            return entry, None
        entry.attempt = _Attempt()
        return entry, entry.attempt


def _leave(entry: _LostGroup, attempt: _Attempt) -> None:
    with _registry_lock:
        attempt.holders -= 1
        if attempt.holders == 0:
            entry.attempt = None


def _remove(group: RetainedGroup) -> None:
    with _registry_lock:
        del _lost_groups[group]


def release_leader_if_gone(child: AnchoredChild, *, counted_empty: bool, exit_known: bool) -> bool:
    if not (counted_empty and exit_known):
        return False
    child.release()
    return True


async def _release_leader_if_empty(entry: _LostGroup, attempt: _Attempt) -> bool:
    census = await _kill_and_count_off_the_loop(entry, attempt)
    if not release_leader_if_gone(
        entry.child,
        counted_empty=census is GroupCensus.EMPTY,
        exit_known=entry.child.returncode is not None,
    ):
        return False
    with _registry_lock:
        entry.leader_released = True
    return True


async def _kill_and_count_off_the_loop(entry: _LostGroup, attempt: _Attempt) -> GroupCensus:
    outcome: concurrent.futures.Future[GroupCensus] = concurrent.futures.Future()
    outcome.set_running_or_notify_cancel()
    worker = ClaimedStartThread(target=_kill_and_count, args=(entry, attempt, outcome), daemon=True)
    with _registry_lock:
        attempt.holders += 1
    try:
        worker.start()
    except BaseException:
        if worker.take_start_claim():
            _leave(entry, attempt)
        raise
    return await asyncio.wrap_future(outcome)


def _kill_and_count(
    entry: _LostGroup,
    attempt: _Attempt,
    outcome: concurrent.futures.Future[GroupCensus],
) -> None:
    try:
        census = entry.child.kill_and_census()
    except BaseException as error:  # NOSONAR(S5754) the outcome carries it to the awaiting call
        _leave(entry, attempt)
        outcome.set_exception(error)
        return
    _leave(entry, attempt)
    outcome.set_result(census)
