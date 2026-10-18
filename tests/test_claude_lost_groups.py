"""A lost group is retained by name; one attempt per call may confirm it gone and free its place."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import logging
import os
import pickle
import signal
import threading
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from anchored_kernel import (
    KernelModel,
    WatcherStarts,
    install_kernel,
    join_watcher,
    recorded_watcher_starts,
)
from anchored_leaders import (
    ALL_PHASES,
    LEADER_WITH_GRANDCHILD,
    Leaders,
    Opened,
    bounded,
    open_door,
    spawn_warnings,
)
from anchored_support import (
    GONE,
    SIGKILLED,
    TEST_WAIT_SECONDS,
    ChildCleanup,
    pidfd_open,
    pidfd_send_signal,
    wait_for_exits,
)
from provider_test_support import (
    Hold,
    Recorded,
    ScriptedAnchoredChild,
    call_within_deadline,
    operations,
    start_then_raise,
)

from agent_providers.claude import lost_groups
from agent_providers.claude.lost_groups import confirm_group_gone, retain_lost_group
from agent_providers.claude.provider import UnavailableError
from agent_providers.errors import (
    ContainmentLost,
    ProviderError,
    RetainedGroup,
)
from agent_providers.process import ClaimedStartThread
from agent_providers.procfs import GroupCensus
from agent_providers.spawn import AnchorStateError, ChildIdentityLost

PID = 4242
KILL, RELEASE, SLOT = "kill", "release", "slot"
UNAVAILABLE_COUNTS = [
    count for count in GroupCensus if count not in (GroupCensus.EMPTY, GroupCensus.LIVE)
]


@pytest.fixture(autouse=True)
def lost_group_rows_end_clean(
    thread_failures: list[threading.ExceptHookArgs],
    caplog: pytest.LogCaptureFixture,
    expected_spawn_warnings: list[str],
) -> Iterator[None]:
    with caplog.at_level(logging.WARNING):
        yield
    assert thread_failures == []
    assert spawn_warnings(caplog, ALL_PHASES) == expected_spawn_warnings
    assert [
        record.getMessage()
        for phase in ALL_PHASES
        for record in caplog.get_records(phase)
        if record.name == lost_groups.__name__ and record.levelno >= logging.WARNING
    ] == []


@pytest.fixture
def log() -> list[Recorded]:
    return []


@dataclass(frozen=True)
class Retained:
    group: RetainedGroup
    child: ScriptedAnchoredChild


def slot_releases(
    log: list[Recorded], pid: int, *, fail_once: Exception | None = None
) -> Callable[[], None]:
    failures = deque([fail_once] if fail_once is not None else [])

    def release_slot() -> None:
        log.append(Recorded(SLOT, pid, threading.current_thread()))
        if failures:
            raise failures.popleft()

    return release_slot


def retain(
    log: list[Recorded],
    pid: int = PID,
    *,
    slot_failure: Exception | None = None,
    **child_options,
) -> Retained:
    child = ScriptedAnchoredChild(pid, log, **child_options)
    group = retain_lost_group(child, slot_releases(log, pid, fail_once=slot_failure))
    return Retained(group, child)


def confirm(group: RetainedGroup) -> bool:
    return call_within_deadline(asyncio.run, confirm_group_gone(group))


def trail(log: list[Recorded], pid: int = PID) -> list[str]:
    return [operation for operation, recorded_pid in operations(log) if recorded_pid == pid]


def join_ended(thread: threading.Thread) -> None:
    thread.join(TEST_WAIT_SECONDS)
    assert not thread.is_alive(), "the thread did not end within the bound"


@pytest.mark.parametrize("already_confirmed", [False, True], ids=["unknown", "cleaned_up"])
def test_an_unknown_or_cleaned_up_group_is_gone_and_nothing_is_signalled(
    log: list[Recorded], already_confirmed: bool
) -> None:
    retained = retain(log, returncode=3)
    group = retained.group
    if already_confirmed:
        assert confirm(group) is True
    else:
        group = RetainedGroup(PID, retained.group.generation + 10**6)
    before = operations(log)

    assert confirm(group) is True
    assert operations(log)[len(before) :] == []
    assert trail(log) == ([KILL, RELEASE, SLOT] if already_confirmed else [])


def test_a_live_group_stays_retained_until_a_later_attempt_finds_it_empty(
    log: list[Recorded],
) -> None:
    retained = retain(log, censuses=[GroupCensus.LIVE, GroupCensus.EMPTY], returncode=3)

    assert confirm(retained.group) is False
    assert trail(log) == [KILL]
    assert confirm(retained.group) is True
    assert trail(log) == [KILL, KILL, RELEASE, SLOT]
    assert confirm(retained.group) is True
    assert trail(log) == [KILL, KILL, RELEASE, SLOT]


@pytest.mark.parametrize("unavailable", UNAVAILABLE_COUNTS, ids=lambda count: count.value)
def test_an_uncountable_group_is_not_confirmed_and_releases_nothing(
    log: list[Recorded], unavailable: GroupCensus
) -> None:
    retained = retain(log, censuses=[unavailable, GroupCensus.EMPTY], returncode=3)

    assert confirm(retained.group) is False
    assert trail(log) == [KILL]
    assert confirm(retained.group) is True
    assert trail(log) == [KILL, KILL, RELEASE, SLOT]


def test_an_empty_count_before_the_exit_is_published_releases_nothing(
    log: list[Recorded],
) -> None:
    retained = retain(log, censuses=[GroupCensus.EMPTY])

    assert confirm(retained.group) is False
    assert trail(log) == [KILL]
    retained.child.returncode = SIGKILLED
    assert confirm(retained.group) is True
    assert trail(log) == [KILL, KILL, RELEASE, SLOT]


@pytest.mark.parametrize(
    "confirmed_before_the_newer_retention", [True, False], ids=["sequential", "together"]
)
def test_an_old_groups_attempt_never_signals_a_newer_group_with_the_same_pgid(
    log: list[Recorded], confirmed_before_the_newer_retention: bool
) -> None:
    older = retain(log, returncode=3)
    if confirmed_before_the_newer_retention:
        assert confirm(older.group) is True
    newer = retain(log, censuses=[GroupCensus.LIVE, GroupCensus.EMPTY], returncode=3)
    if not confirmed_before_the_newer_retention:
        assert confirm(older.group) is True
    assert confirm(older.group) is True

    assert older.group.pgid == newer.group.pgid
    assert older.group.generation != newer.group.generation
    assert trail(log) == [KILL, RELEASE, SLOT]
    assert confirm(newer.group) is False
    assert confirm(newer.group) is True
    assert trail(log) == [KILL, RELEASE, SLOT, KILL, KILL, RELEASE, SLOT]


LOST_AT_THE_KILL = ChildIdentityLost("lost at the kill")
LOST_AT_THE_RELEASE = ChildIdentityLost("lost at the release")
KILL_REFUSED = PermissionError("kill refused")
RELEASE_REFUSED = AnchorStateError("not reaped")


@pytest.mark.parametrize(
    ("failure", "script", "expected_trail"),
    [
        (LOST_AT_THE_KILL, {"censuses": [LOST_AT_THE_KILL]}, [KILL]),
        (LOST_AT_THE_RELEASE, {"release_failures": [LOST_AT_THE_RELEASE]}, [KILL, RELEASE]),
    ],
    ids=["lost_at_the_kill", "lost_at_the_release"],
)
def test_a_lost_anchor_raises_forever_and_is_never_confirmed(
    log: list[Recorded], failure: Exception, script: dict, expected_trail: list[str]
) -> None:
    retained = retain(log, returncode=3, **script)

    for _ in range(3):
        with pytest.raises(ChildIdentityLost) as raised:
            confirm(retained.group)
        assert raised.value is failure
    assert trail(log) == expected_trail


@pytest.mark.parametrize(
    ("failure", "script", "expected_trail"),
    [
        (
            KILL_REFUSED,
            {"censuses": [KILL_REFUSED, GroupCensus.EMPTY]},
            [KILL, KILL, RELEASE, SLOT],
        ),
        (
            RELEASE_REFUSED,
            {"release_failures": [RELEASE_REFUSED]},
            [KILL, RELEASE, KILL, RELEASE, SLOT],
        ),
    ],
    ids=["a_failed_kill", "a_failed_release"],
)
def test_a_transient_failure_propagates_and_the_next_attempt_starts_again_at_the_kill(
    log: list[Recorded], failure: Exception, script: dict, expected_trail: list[str]
) -> None:
    retained = retain(log, returncode=3, **script)

    with pytest.raises(type(failure)) as raised:
        confirm(retained.group)
    assert raised.value is failure
    assert SLOT not in trail(log)

    assert confirm(retained.group) is True
    assert trail(log) == expected_trail


def test_a_failed_slot_release_resumes_without_a_second_kill(log: list[Recorded]) -> None:
    slot_failure = OSError("the home could not be removed")
    retained = retain(log, returncode=3, slot_failure=slot_failure)

    with pytest.raises(OSError) as raised:
        confirm(retained.group)
    assert raised.value is slot_failure
    assert trail(log) == [KILL, RELEASE, SLOT]

    assert confirm(retained.group) is True
    assert trail(log) == [KILL, RELEASE, SLOT, SLOT]
    assert confirm(retained.group) is True
    assert trail(log) == [KILL, RELEASE, SLOT, SLOT]


def test_one_attempt_runs_at_a_time_with_the_kill_off_the_loop_and_the_cleanup_on_it(
    log: list[Recorded],
) -> None:
    hold = Hold()
    retained = retain(log, returncode=3, hold=hold)

    async def scenario() -> tuple[threading.Thread, bool, bool]:
        first = asyncio.ensure_future(confirm_group_gone(retained.group))
        try:
            assert await asyncio.to_thread(hold.entered.wait, TEST_WAIT_SECONDS)
            second = await confirm_group_gone(retained.group)
        finally:
            hold.proceed.set()
        return threading.current_thread(), second, await first

    loop_thread, second, first = call_within_deadline(asyncio.run, scenario())

    assert (first, second) == (True, False)
    assert trail(log) == [KILL, RELEASE, SLOT]
    threads = {recorded.operation: recorded.thread for recorded in log}
    assert threads[KILL] is not loop_thread
    assert threads[RELEASE] is loop_thread
    assert threads[SLOT] is loop_thread


def test_a_cancelled_attempt_keeps_the_group_excluded_until_its_census_call_has_returned(
    log: list[Recorded],
) -> None:
    hold = Hold()
    retained = retain(log, hold=hold, censuses=[GroupCensus.EMPTY], returncode=SIGKILLED)

    async def cancel_the_first_and_call_again() -> bool:
        first = asyncio.ensure_future(confirm_group_gone(retained.group))
        assert await asyncio.to_thread(hold.entered.wait, TEST_WAIT_SECONDS)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        return await confirm_group_gone(retained.group)

    try:
        assert call_within_deadline(asyncio.run, cancel_the_first_and_call_again()) is False
        assert confirm(retained.group) is False
        assert trail(log) == [KILL]
    finally:
        hold.proceed.set()
        if log:
            join_ended(log[0].thread)

    assert confirm(retained.group) is True
    assert trail(log) == [KILL, KILL, RELEASE, SLOT]


def test_no_registry_lock_is_held_around_a_callout(log: list[Recorded]) -> None:
    another_retentions: list[RetainedGroup] = []

    def retain_another() -> None:
        pid = PID + 1 + len(another_retentions)
        another = ScriptedAnchoredChild(pid, log)
        another_retentions.append(retain_lost_group(another, lambda: None))

    release_slot_with_retention = slot_releases(log, PID)
    child = ScriptedAnchoredChild(PID, log, returncode=3, during=retain_another)
    group = retain_lost_group(child, lambda: (retain_another(), release_slot_with_retention())[1])

    assert confirm(group) is True
    generations = {retained.generation for retained in another_retentions}
    assert len(another_retentions) == 3
    assert len(generations) == 3
    assert min(generations) > group.generation


def test_containment_lost_names_only_the_group_and_a_retained_group_is_a_value() -> None:
    group = RetainedGroup(pgid=4242, generation=7731)
    error = ContainmentLost(group)

    assert isinstance(error, Exception)
    assert not isinstance(error, (ProviderError, UnavailableError))
    assert vars(error) == {"group": group}
    assert str(error) == "process group 4242: its end after SIGKILL is not confirmed"
    assert "7731" not in str(error)
    for rebuilt in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):  # noqa: S301
        assert rebuilt.group == group
    with pytest.raises(dataclasses.FrozenInstanceError):
        group.pgid = 1  # type: ignore[misc]
    assert {group: "kept"}[RetainedGroup(4242, 7731)] == "kept"


def test_a_generation_is_never_reused_for_the_same_pgid(log: list[Recorded]) -> None:
    first = retain(log, returncode=3)
    second = retain(log, returncode=3)
    assert confirm(first.group) is True
    third = retain(log, returncode=3)

    generations = [retained.group.generation for retained in (first, second, third)]
    assert generations == sorted(set(generations))
    assert [retained.group.pgid for retained in (first, second, third)] == [PID] * 3


@pytest.mark.parametrize(
    ("worker_claims_first", "expected_trail"),
    [(False, [KILL, RELEASE, SLOT]), (True, [KILL, KILL, RELEASE, SLOT])],
    ids=["starter_claims_first", "worker_claims_first"],
)
def test_a_failed_worker_start_keeps_the_group_excluded_exactly_while_a_claimed_worker_runs(
    monkeypatch: pytest.MonkeyPatch,
    log: list[Recorded],
    worker_claims_first: bool,
    expected_trail: list[str],
) -> None:
    hold = Hold() if worker_claims_first else None
    retained = retain(log, returncode=3, hold=hold)
    cannot_start = RuntimeError("can't start new thread")
    workers: list[threading.Thread] = []
    gate = threading.Event()
    with monkeypatch.context() as failing:
        real_start = threading.Thread.start

        def keep_the_worker(thread: threading.Thread) -> None:
            if isinstance(thread, ClaimedStartThread):
                workers.append(thread)
            real_start(thread)

        failing.setattr(threading.Thread, "start", keep_the_worker)
        try:
            gate = start_then_raise(
                failing,
                cannot_start,
                target=lost_groups._kill_and_count,
                thread_claims_first=worker_claims_first,
            )
            with pytest.raises(RuntimeError) as raised:
                confirm(retained.group)
            assert raised.value is cannot_start
            if hold is None:
                assert trail(log) == []
                gate.set()
            else:
                assert hold.entered.wait(TEST_WAIT_SECONDS)
                assert confirm(retained.group) is False
                assert trail(log) == [KILL]
                hold.proceed.set()
        finally:
            gate.set()
            if hold is not None:
                hold.proceed.set()
            for worker in workers:
                if worker.ident is not None:
                    join_ended(worker)

    assert confirm(retained.group) is True
    assert trail(log) == expected_trail


@pytest.fixture
def own_runner() -> Iterator[asyncio.Runner]:
    with asyncio.Runner() as runner:
        runner.get_loop()
        yield runner


def open_watched_door(
    runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    monkeypatch: pytest.MonkeyPatch,
    **description,
) -> tuple[Opened, WatcherStarts]:
    with recorded_watcher_starts(monkeypatch) as watchers:
        return open_door(runner, child_cleanup, leaders.describe(**description)), watchers


def hold_the_grandchild_stdin_open(
    runner: asyncio.Runner, child_cleanup: ChildCleanup, opened: Opened
) -> int:
    pipe = opened.child.stdin.transport.get_extra_info("pipe")
    child_cleanup.close_at_cleanup(os.dup(pipe.fileno()))
    grandchild = int(runner.run(bounded(opened.child.stdout.readline())))
    pidfd = pidfd_open(grandchild)
    child_cleanup.close_at_cleanup(pidfd)
    return pidfd


@pytest.mark.parametrize(
    "script",
    [LEADER_WITH_GRANDCHILD, None],
    ids=["a_sigterm_ignoring_grandchild", "a_running_leader"],
)
def test_a_real_retained_group_ends_dead_reaped_and_released_once(
    own_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    monkeypatch: pytest.MonkeyPatch,
    log: list[Recorded],
    script: str | None,
) -> None:
    description = {} if script is None else {"script": script}
    opened, watchers = open_watched_door(
        own_runner, child_cleanup, leaders, monkeypatch, **description
    )
    child, owned = opened.child, opened.owned
    grandchild_pidfd = None
    try:
        if script is not None:
            grandchild_pidfd = hold_the_grandchild_stdin_open(own_runner, child_cleanup, opened)
            assert own_runner.run(bounded(child.exited())) == 3
        group = retain_lost_group(child, slot_releases(log, owned.pid))

        first = own_runner.run(bounded(confirm_group_gone(group)))

        if grandchild_pidfd is not None:
            assert wait_for_exits([grandchild_pidfd], TEST_WAIT_SECONDS) == {grandchild_pidfd}
        else:
            assert own_runner.run(bounded(child.exited())) == SIGKILLED
        if first is False:
            child_cleanup.wait_exited(owned)
        join_watcher(watchers.thread(owned.pid))
        if first is False:
            assert own_runner.run(bounded(confirm_group_gone(group))) is True

        assert child_cleanup.state(owned) is GONE
        assert trail(log, owned.pid) == [SLOT]
        assert own_runner.run(bounded(confirm_group_gone(group))) is True
    finally:
        if grandchild_pidfd is not None and not wait_for_exits([grandchild_pidfd], 0):
            with contextlib.suppress(ProcessLookupError):
                pidfd_send_signal(grandchild_pidfd, signal.SIGKILL)


def test_a_real_lost_leader_is_never_confirmed_and_never_frees_its_place(
    own_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    monkeypatch: pytest.MonkeyPatch,
    log: list[Recorded],
) -> None:
    model = KernelModel(loses_at="wait")
    install_kernel(monkeypatch, model)
    opened = open_door(own_runner, child_cleanup, leaders.describe())
    exited = bounded(opened.child.exited())
    with pytest.raises(ChildIdentityLost):
        own_runner.run(exited)
    group = retain_lost_group(opened.child, slot_releases(log, opened.owned.pid))

    for _ in range(2):
        confirmation = bounded(confirm_group_gone(group))
        with pytest.raises(ChildIdentityLost):
            own_runner.run(confirmation)

    assert operations(log) == []
    assert model.violations == []
    with pytest.raises(ChildIdentityLost):
        opened.child.release()
    assert child_cleanup.wait_exited(opened.owned) == 0


def test_a_group_is_confirmed_and_cleaned_up_from_a_new_loop_after_its_opening_loop_closed(
    own_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    monkeypatch: pytest.MonkeyPatch,
    expected_spawn_warnings: list[str],
    log: list[Recorded],
) -> None:
    opened, watchers = open_watched_door(own_runner, child_cleanup, leaders, monkeypatch)
    child, owned = opened.child, opened.owned
    group = retain_lost_group(child, slot_releases(log, owned.pid))
    expected_spawn_warnings.append(f"on_exit of anchored leader {owned.pid} raised RuntimeError")
    own_runner.close()

    first = confirm(group)

    if first is False:
        child_cleanup.wait_exited(owned)
    join_watcher(watchers.thread(owned.pid))
    if first is False:
        assert confirm(group) is True
    assert confirm(group) is True
    assert child.returncode == SIGKILLED
    assert child_cleanup.state(owned) is GONE
    assert trail(log, owned.pid) == [SLOT]
