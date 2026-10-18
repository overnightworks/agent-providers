"""The anchored child repeats its kill and count until the group is gone, within a bound.

Pinned on a virtual clock for the schedule, the result, the errors and the cancellation, and
once on the real loop clock with a descendant that outlives the first count.
"""

from __future__ import annotations

import asyncio
import math
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from anchored_kernel import CensusRecorder, EventLog, GroupSignalCall, GroupSignalModel
from anchored_leaders import LEADER_AND_GRANDCHILD_WAITING, Leaders, bounded, open_door
from anchored_support import (
    GONE,
    RUNNING,
    SIGKILLED,
    TEST_WAIT_SECONDS,
    ChildCleanup,
    Exited,
    OwnedChild,
    pidfd_open,
    wait_for_exits,
)

from agent_providers import spawn
from agent_providers.procfs import GroupCensus
from agent_providers.spawn import (
    GROUP_CENSUS_INTERVAL_SECONDS,
    AnchoredChild,
    AnchorStateError,
    ChildIdentityLost,
)

pytestmark = pytest.mark.usefixtures("anchored_door_ends_clean")

ORIGIN = 1000.0
INTERVAL = GROUP_CENSUS_INTERVAL_SECONDS
UNAVAILABLE = [
    census for census in GroupCensus if census not in {GroupCensus.EMPTY, GroupCensus.LIVE}
]
LIVE = GroupCensus.LIVE
EMPTY = GroupCensus.EMPTY


class VirtualCensusClock:
    def __init__(self) -> None:
        self.instant = ORIGIN
        self.waits: list[float] = []
        self.reads = 0
        self.overrun = 0.0
        self.wake_at: float | None = None
        self.park_at: int | None = None
        self.parked = asyncio.Event()

    def now(self) -> float:
        self.reads += 1
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant += seconds

    async def wait_until(self, instant: float) -> None:
        self.waits.append(instant)
        if self.park_at == len(self.waits):
            self.parked.set()
            await asyncio.Event().wait()
        elif self.wake_at is not None:
            self.instant = self.wake_at
        else:
            self.instant = max(self.instant, instant) + self.overrun
        await asyncio.sleep(0)


@dataclass(frozen=True)
class Count:
    instant: float | None
    thread: str


@dataclass
class Rig:
    runner: asyncio.Runner
    child: AnchoredChild
    owned: OwnedChild
    cleanup: ChildCleanup
    clock: VirtualCensusClock | None
    census_calls: CensusRecorder
    model: GroupSignalModel
    log: EventLog = field(default_factory=EventLog)
    counts: list[Count] = field(default_factory=list)
    on_count: Callable[[int], None] | None = None
    on_kill: Callable[[int], None] | None = None

    def count_hook(self) -> None:
        self.counts.append(
            Count(
                None if self.clock is None else self.clock.instant, threading.current_thread().name
            )
        )
        if self.on_count is not None:
            self.on_count(len(self.counts))

    def kill_hook(self, call: GroupSignalCall) -> None:
        self.log.record("kill")
        if self.on_kill is not None:
            self.on_kill(self.log.kinds.count("kill"))

    def answer(self, *answers: GroupCensus) -> None:
        self.census_calls.answers.extend(answers)

    def run(self, scenario) -> object:
        return self.runner.run(bounded(scenario))

    @property
    def count_instants(self) -> list[float]:
        return [count.instant for count in self.counts]

    async def finish(self) -> None:
        assert await self.child.exited() == SIGKILLED
        assert self.cleanup.state(self.owned) == Exited(SIGKILLED)
        self.child.release()
        assert self.cleanup.state(self.owned) == GONE
        assert self.model.violations == []


@pytest.fixture
def open_rig(
    door_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    census_calls: CensusRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Rig]:
    def open_rig(
        *,
        clock: VirtualCensusClock | None = None,
        script: str | None = None,
        kill_failure: OSError | None = None,
    ) -> Rig:
        description = leaders.describe() if script is None else leaders.describe(script=script)
        opened = open_door(door_runner, child_cleanup, description)
        model = GroupSignalModel(failure=kill_failure, during=lambda call: rig.kill_hook(call))
        model.bind(opened.owned.pid)
        model.install(monkeypatch)
        rig = Rig(
            door_runner, opened.child, opened.owned, child_cleanup, clock, census_calls, model
        )
        census_calls.log = rig.log
        census_calls.during = rig.count_hook
        if clock is not None:
            monkeypatch.setattr(spawn, "_census_clock", lambda: clock)
        return rig

    return open_rig


@pytest.fixture
def clock() -> VirtualCensusClock:
    return VirtualCensusClock()


@pytest.fixture
def virtual_rig(open_rig: Callable[..., Rig], clock: VirtualCensusClock) -> Rig:
    rig = open_rig(clock=clock)
    rig.census_calls.answer = LIVE
    return rig


def kill_until_gone_then_finish(rig: Rig, within: float):
    async def scenario():
        result = await rig.child.kill_until_gone(within=within)
        await rig.finish()
        return result

    return rig.run(scenario())


def test_the_schedule_spaces_attempts_from_each_counts_end_and_stops_before_the_deadline(
    virtual_rig: Rig, clock: VirtualCensusClock
) -> None:
    virtual_rig.on_count = lambda ordinal: clock.advance(0.04) if ordinal == 1 else None

    result = kill_until_gone_then_finish(virtual_rig, 0.22)

    assert virtual_rig.count_instants == pytest.approx([ORIGIN, ORIGIN + 0.04 + INTERVAL])
    assert clock.waits == pytest.approx([ORIGIN + 0.04 + INTERVAL])
    assert (result.census, result.ended_in_time, result.confirmed) == (LIVE, True, False)
    assert virtual_rig.log.kinds == ["kill", "count"] * 2
    loop_thread = threading.main_thread().name
    assert {count.thread for count in virtual_rig.counts}.isdisjoint({loop_thread})


def test_the_first_empty_count_in_time_confirms_and_ends_the_call(
    virtual_rig: Rig, clock: VirtualCensusClock
) -> None:
    virtual_rig.answer(LIVE, LIVE, EMPTY)

    result = kill_until_gone_then_finish(virtual_rig, 10)

    assert (result.census, result.ended_in_time, result.confirmed) == (EMPTY, True, True)
    assert len(virtual_rig.counts) == 3
    assert len(clock.waits) == 2


@pytest.mark.parametrize("unavailable", UNAVAILABLE, ids=lambda census: census.value)
def test_an_unavailable_count_ends_the_call_unchanged_and_never_confirms(
    unavailable: GroupCensus, virtual_rig: Rig, clock: VirtualCensusClock
) -> None:
    virtual_rig.answer(LIVE, unavailable)

    result = kill_until_gone_then_finish(virtual_rig, 10)

    assert result.census is unavailable
    assert result.confirmed is False
    assert len(virtual_rig.counts) == 2
    assert len(clock.waits) == 1


@pytest.mark.parametrize(
    ("within", "answer"),
    [(0, LIVE), (0, EMPTY), (INTERVAL, LIVE)],
    ids=["zero-bound-live", "zero-bound-empty", "bound-equal-to-the-interval-live"],
)
def test_no_subsequent_attempt_starts_when_none_can_start_before_the_deadline(
    within: float, answer: GroupCensus, virtual_rig: Rig, clock: VirtualCensusClock
) -> None:
    virtual_rig.census_calls.answer = answer

    result = kill_until_gone_then_finish(virtual_rig, within)

    assert virtual_rig.log.kinds == ["kill", "count"]
    assert clock.waits == []
    assert result.census is answer
    assert result.ended_in_time is True
    assert result.confirmed is (answer is EMPTY)


@pytest.mark.parametrize(
    ("within", "expected"),
    [
        (-0.1, ValueError),
        (math.nan, ValueError),
        (math.inf, ValueError),
        (-math.inf, ValueError),
        ("0.1", TypeError),
        (None, TypeError),
    ],
    ids=["negative", "nan", "inf", "negative-inf", "string", "none"],
)
def test_a_refused_bound_signals_and_counts_nothing_before_any_clock_read(
    within: object, expected: type[Exception], virtual_rig: Rig, clock: VirtualCensusClock
) -> None:
    async def scenario() -> None:
        with pytest.raises(expected):
            await virtual_rig.child.kill_until_gone(within=within)
        assert virtual_rig.model.calls == []
        assert virtual_rig.census_calls == []
        assert clock.reads == 0
        assert virtual_rig.cleanup.state(virtual_rig.owned) == RUNNING
        virtual_rig.child.signal_group(signal.SIGKILL)
        await virtual_rig.finish()

    virtual_rig.run(scenario())


@dataclass(frozen=True)
class Wake:
    overrun: float = 0.0
    at_deadline: bool = False


@pytest.mark.parametrize(
    ("wake", "second_count_instant"),
    [
        (Wake(overrun=0.06), ORIGIN + 0.16),
        (Wake(overrun=0.2), None),
        (Wake(at_deadline=True), None),
    ],
    ids=["overrun-before-the-deadline", "overrun-past-the-deadline", "wake-on-the-deadline"],
)
def test_a_late_wait_never_starts_an_attempt_at_or_after_the_deadline(
    wake: Wake, second_count_instant: float | None, virtual_rig: Rig, clock: VirtualCensusClock
) -> None:
    within = 0.25
    clock.overrun = wake.overrun
    clock.wake_at = ORIGIN + within if wake.at_deadline else None

    result = kill_until_gone_then_finish(virtual_rig, within)

    expected_instants = [ORIGIN] + ([] if second_count_instant is None else [second_count_instant])
    assert virtual_rig.count_instants == pytest.approx(expected_instants)
    assert clock.waits == pytest.approx([ORIGIN + INTERVAL])
    assert (result.census, result.ended_in_time, result.confirmed) == (LIVE, True, False)


@pytest.mark.parametrize("answer", [LIVE, EMPTY], ids=["live", "empty"])
def test_a_count_that_ends_past_the_bound_never_confirms_an_empty_one_included(
    answer: GroupCensus, virtual_rig: Rig, clock: VirtualCensusClock
) -> None:
    virtual_rig.answer(LIVE, answer)
    virtual_rig.on_count = lambda ordinal: clock.advance(0.5) if ordinal == 2 else None

    result = kill_until_gone_then_finish(virtual_rig, 0.25)

    assert (result.census, result.ended_in_time, result.confirmed) == (answer, False, False)
    assert len(virtual_rig.counts) == 2
    assert clock.waits == pytest.approx([ORIGIN + INTERVAL])


def test_a_call_after_the_release_is_refused_without_a_kill_a_count_or_a_wait(
    virtual_rig: Rig, clock: VirtualCensusClock
) -> None:
    async def scenario() -> None:
        virtual_rig.child.signal_group(signal.SIGKILL)
        await virtual_rig.finish()
        signals_before = len(virtual_rig.model.calls)
        with pytest.raises(AnchorStateError):
            await virtual_rig.child.kill_until_gone(within=10)
        assert len(virtual_rig.model.calls) == signals_before
        assert virtual_rig.census_calls == []
        assert clock.waits == []

    virtual_rig.run(scenario())


def test_a_loss_met_by_the_first_kill_ends_the_call_and_is_not_retried(
    open_rig: Callable[..., Rig], clock: VirtualCensusClock
) -> None:
    rig = open_rig(clock=clock, kill_failure=ProcessLookupError())

    async def scenario() -> None:
        with pytest.raises(ChildIdentityLost):
            await rig.child.kill_until_gone(within=10)
        with pytest.raises(ChildIdentityLost):
            await rig.child.exited()
        assert rig.child.returncode is None
        with pytest.raises(ChildIdentityLost):
            rig.child.release()

    rig.run(scenario())

    assert rig.log.kinds == ["kill"]
    assert rig.census_calls == []
    assert clock.waits == []
    assert rig.cleanup.wait_exited(rig.owned) == 0


def test_a_count_failure_propagates_at_once_without_a_further_attempt(
    virtual_rig: Rig, clock: VirtualCensusClock
) -> None:
    failure = RuntimeError("the second count failed")

    def fail_the_second_count(ordinal: int) -> None:
        if ordinal == 2:
            raise failure

    virtual_rig.on_count = fail_the_second_count

    async def scenario() -> None:
        with pytest.raises(RuntimeError) as raised:
            await virtual_rig.child.kill_until_gone(within=10)
        assert raised.value is failure
        await virtual_rig.finish()

    virtual_rig.run(scenario())

    assert virtual_rig.log.kinds == ["kill", "count"] * 2
    assert clock.waits == pytest.approx([ORIGIN + INTERVAL])


def test_a_cancelled_wait_starts_no_further_attempt(
    virtual_rig: Rig, clock: VirtualCensusClock
) -> None:
    clock.park_at = 1

    async def scenario() -> None:
        task = asyncio.ensure_future(virtual_rig.child.kill_until_gone(within=10))
        await clock.parked.wait()
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=TEST_WAIT_SECONDS / 4)
        assert task in done
        assert task.cancelled()
        clock.advance(1)
        for _ in range(5):
            await asyncio.sleep(0)
        assert virtual_rig.log.kinds == ["kill", "count"]
        await virtual_rig.finish()

    virtual_rig.run(scenario())


@dataclass
class Hold:
    reached: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    attempt_finished: threading.Event = field(default_factory=threading.Event)

    def block(self) -> None:
        self.reached.set()
        self.release.wait(TEST_WAIT_SECONDS)
        self.attempt_finished.set()


@pytest.mark.parametrize("held", ["count", "kill"])
def test_a_cancelled_attempt_still_commits_the_loss_it_met_and_starts_nothing_more(
    held: str, open_rig: Callable[..., Rig], clock: VirtualCensusClock
) -> None:
    kill_is_lost = held == "kill"
    rig = open_rig(clock=clock, kill_failure=ProcessLookupError() if kill_is_lost else None)
    rig.census_calls.answer = LIVE
    hold = Hold()
    if kill_is_lost:
        rig.on_kill = lambda ordinal: hold.block()
    else:
        rig.on_count = lambda ordinal: hold.block()

    async def scenario() -> None:
        task = asyncio.ensure_future(rig.child.kill_until_gone(within=10))
        try:
            assert await asyncio.to_thread(hold.reached.wait, TEST_WAIT_SECONDS)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=TEST_WAIT_SECONDS / 4)
            assert task in done
            assert task.cancelled()
        finally:
            hold.release.set()
            task.cancel()
            await asyncio.wait({task}, timeout=TEST_WAIT_SECONDS / 4)
        assert await asyncio.to_thread(hold.attempt_finished.wait, TEST_WAIT_SECONDS)
        if kill_is_lost:
            with pytest.raises(ChildIdentityLost):
                await rig.child.exited()
            assert rig.child.returncode is None
            with pytest.raises(ChildIdentityLost):
                rig.child.release()
        else:
            await rig.finish()

    rig.run(scenario())

    assert clock.waits == []
    assert rig.log.kinds == (["kill"] if kill_is_lost else ["kill", "count"])
    if kill_is_lost:
        assert rig.cleanup.wait_exited(rig.owned) == 0


def test_the_real_clock_confirms_with_a_lingering_descendant_dead_at_the_return(
    open_rig: Callable[..., Rig],
    child_cleanup: ChildCleanup,
) -> None:
    rig = open_rig(script=LEADER_AND_GRANDCHILD_WAITING)
    rig.answer(LIVE)

    async def scenario() -> None:
        grandchild = int((await rig.child.stdout.readline()).decode())
        pidfd = pidfd_open(grandchild)
        child_cleanup.close_at_cleanup(pidfd)
        result = await rig.child.kill_until_gone(within=TEST_WAIT_SECONDS / 2)
        assert result.confirmed is True
        assert wait_for_exits([pidfd], 0) == {pidfd}
        assert await rig.child.exited() == SIGKILLED
        assert child_cleanup.state(rig.owned) == Exited(SIGKILLED)
        rig.child.release()
        assert child_cleanup.state(rig.owned) == GONE

    rig.run(scenario())

    assert len(rig.census_calls) >= 2
    assert rig.log.kinds == ["kill", "count"] * len(rig.census_calls)
    assert rig.model.violations == []
