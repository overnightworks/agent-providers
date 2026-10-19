"""The blocking door starts an anchored leader for a caller's own thread.

The streams, the exit wait, the release and the door's rollback are pinned on real children.
``kill_until_gone`` is pinned on a virtual clock for its schedule, result and errors, and once
on the real clock with an acknowledged lingering descendant.
"""

from __future__ import annotations

import contextlib
import math
import os
import signal
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from anchored_kernel import (
    CensusRecorder,
    EventLog,
    GroupSignalCall,
    GroupSignalModel,
    KernelModel,
    install_kernel,
)
from anchored_leaders import Leaders
from anchored_support import GONE, SIGKILLED, ChildCleanup, Exited, OwnedChild
from bounds import TEST_WAIT_SECONDS
from descendant_support import DescendantRow, Handshake, lingering_descendant, process_identity
from provider_test_support import make_the_first_parent_close_of_a_door_fail

from agent_providers import spawn
from agent_providers.procfs import GroupCensus
from agent_providers.spawn import (
    GROUP_CENSUS_INTERVAL_SECONDS,
    AnchoredLeader,
    AnchorStateError,
    BlockingAnchoredChild,
    ChildIdentityLost,
    ChildProcess,
    StderrPolicy,
    closed_environment,
    open_blocking_anchored_pipes,
)

pytestmark = pytest.mark.usefixtures("anchored_door_ends_clean")

ORIGIN = 1000.0
INTERVAL = GROUP_CENSUS_INTERVAL_SECONDS
LIVE = GroupCensus.LIVE
EMPTY = GroupCensus.EMPTY
UNAVAILABLE = [census for census in GroupCensus if census not in {EMPTY, LIVE}]
EXIT_STATUS = 3
ECHOES_STDIN_THEN_EXITS_WITH_THREE = (
    "import sys\n"
    "sys.stdout.write(sys.stdin.read())\n"
    "sys.stdout.flush()\n"
    "sys.stderr.write('problem')\n"
    f"sys.exit({EXIT_STATUS})\n"
)
NAMES_ITS_STDERR = (
    "import os, sys\nsys.stdout.write(os.readlink('/proc/self/fd/2'))\nsys.stdin.read()\n"
)


class VirtualBlockingClock:
    def __init__(self) -> None:
        self.instant = ORIGIN
        self.waits: list[float] = []
        self.reads = 0
        self.overrun = 0.0
        self.wake_at: float | None = None

    def now(self) -> float:
        self.reads += 1
        return self.instant

    def advance(self, seconds: float) -> None:
        self.instant += seconds

    def wait_until(self, instant: float) -> None:
        self.waits.append(instant)
        if self.wake_at is not None:
            self.instant = self.wake_at
        else:
            self.instant = max(self.instant, instant) + self.overrun


def open_door(
    child_cleanup: ChildCleanup, description: ChildProcess, stderr: StderrPolicy = "capture",
) -> tuple[BlockingAnchoredChild, OwnedChild]:
    captured = child_cleanup.capture(
        lambda: open_blocking_anchored_pipes(description, stderr=stderr)
    )
    (owned,) = captured.children
    return captured.value, owned


@dataclass
class Rig:
    child: BlockingAnchoredChild
    owned: OwnedChild
    cleanup: ChildCleanup
    clock: VirtualBlockingClock | None
    census_calls: CensusRecorder
    model: GroupSignalModel
    log: EventLog = field(default_factory=EventLog)
    counts: list[float | None] = field(default_factory=list)
    on_count: Callable[[int], None] | None = None

    def count_hook(self) -> None:
        self.counts.append(None if self.clock is None else self.clock.instant)
        if self.on_count is not None:
            self.on_count(len(self.counts))

    def kill_hook(self, call: GroupSignalCall) -> None:
        self.log.record("kill")

    def answer(self, *answers: GroupCensus) -> None:
        self.census_calls.answers.extend(answers)

    def finish(self) -> None:
        assert self.child.wait_for_exit(TEST_WAIT_SECONDS)
        assert self.child.returncode == SIGKILLED
        assert self.cleanup.state(self.owned) == Exited(SIGKILLED)
        self.child.release()
        assert self.cleanup.state(self.owned) == GONE
        assert self.model.violations == []


@pytest.fixture
def open_rig(
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    census_calls: CensusRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Rig]:
    def open_rig(*, clock: VirtualBlockingClock | None = None) -> Rig:
        child, owned = open_door(child_cleanup, leaders.describe())
        model = GroupSignalModel(during=lambda call: rig.kill_hook(call))
        model.bind(owned.pid)
        model.install(monkeypatch)
        rig = Rig(child, owned, child_cleanup, clock, census_calls, model)
        census_calls.log = rig.log
        census_calls.during = rig.count_hook
        if clock is not None:
            monkeypatch.setattr(spawn, "_blocking_census_clock", lambda: clock)
        return rig

    return open_rig


@pytest.fixture
def clock() -> VirtualBlockingClock:
    return VirtualBlockingClock()


@pytest.fixture
def virtual_rig(open_rig: Callable[..., Rig], clock: VirtualBlockingClock) -> Rig:
    rig = open_rig(clock=clock)
    rig.census_calls.answer = LIVE
    return rig


def test_the_door_carries_the_streams_and_holds_the_exited_leader_until_its_release(
    leaders: Leaders, child_cleanup: ChildCleanup,
) -> None:
    child, owned = open_door(
        child_cleanup, leaders.describe(script=ECHOES_STDIN_THEN_EXITS_WITH_THREE),
    )

    child.stdin.write(b"hello")
    child.stdin.close()
    echoed = child.stdout.read()
    problem = child.stderr.read()
    exit_published = child.wait_for_exit(TEST_WAIT_SECONDS)
    held_state = process_identity(child.pid)

    assert (echoed, problem) == (b"hello", b"problem")
    assert exit_published
    assert child.returncode == EXIT_STATUS
    assert held_state is not None
    assert held_state[0] == "Z"
    child.release()
    assert all(stream.closed for stream in (child.stdin, child.stdout, child.stderr))
    assert child_cleanup.state(owned) == GONE
    child.release()


def test_a_lost_leader_is_no_published_exit_and_is_released_to_nobody(
    leaders: Leaders, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_kernel(monkeypatch, KernelModel(loses_at="wait"))
    child, _ = open_door(child_cleanup, leaders.describe())

    exit_published = child.wait_for_exit(TEST_WAIT_SECONDS)
    with pytest.raises(ChildIdentityLost):
        child.release()

    assert exit_published is False
    assert child.returncode is None
    assert child.stdout.closed


def test_a_devnull_stderr_is_no_stream_and_the_childs_stderr_is_the_null_device(
    leaders: Leaders, child_cleanup: ChildCleanup,
) -> None:
    child, _owned = open_door(child_cleanup, leaders.describe(script=NAMES_ITS_STDERR), "devnull")

    child.stdin.close()
    named = child.stdout.read()
    assert child.wait_for_exit(TEST_WAIT_SECONDS)
    child.release()

    assert child.stderr is None
    assert named == os.devnull.encode()


def _a_missing_binary(monkeypatch: pytest.MonkeyPatch) -> contextlib.AbstractContextManager:
    return contextlib.nullcontext()


@contextlib.contextmanager
def _sigchld_ignored() -> Iterator[None]:
    previous = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGCHLD, previous)


def _an_ignored_sigchld(monkeypatch: pytest.MonkeyPatch) -> contextlib.AbstractContextManager:
    return _sigchld_ignored()


A_WATCHER_LAUNCH_FAILURE = RuntimeError("the watcher could not be launched")


def _a_watcher_launch_that_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> contextlib.AbstractContextManager:
    launch = AnchoredLeader._begin_watching

    def launch_then_raise(leader: AnchoredLeader) -> None:
        launch(leader)
        raise A_WATCHER_LAUNCH_FAILURE

    monkeypatch.setattr(AnchoredLeader, "_begin_watching", launch_then_raise)
    return contextlib.nullcontext()


def _open_descriptors() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


@pytest.mark.parametrize("stderr", ["capture", "devnull"])
@pytest.mark.parametrize(
    ("binary", "arrange", "raised", "child_started"),
    [
        pytest.param(
            Path("/nonexistent/agent-cli"), _a_missing_binary, FileNotFoundError, False,
            id="a-missing-binary",
        ),
        pytest.param(
            None, _an_ignored_sigchld, AnchorStateError, False, id="an-ignored-sigchld",
        ),
        pytest.param(
            None,
            _a_watcher_launch_that_raises,
            RuntimeError,
            True,
            id="a-watcher-launch-that-raises",
        ),
    ],
)
def test_a_door_that_fails_leaves_no_descriptor_thread_or_running_child(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    binary: Path | None,
    arrange: Callable[[pytest.MonkeyPatch], contextlib.AbstractContextManager],
    raised: type[Exception],
    child_started: bool,
    stderr: StderrPolicy,
) -> None:
    description = leaders.describe(binary=binary)
    descriptors_before = _open_descriptors()

    with arrange(monkeypatch), pytest.raises(raised) as refusal:
        child_cleanup.capture(lambda: open_blocking_anchored_pipes(description, stderr=stderr))
    for watcher in [t for t in threading.enumerate() if t.name.startswith("child-exit:")]:
        watcher.join(TEST_WAIT_SECONDS)

    held_by_the_cleanup = {str(owned.pidfd) for owned in child_cleanup.listed()}
    assert _open_descriptors() - held_by_the_cleanup == descriptors_before
    assert [t for t in threading.enumerate() if t.name.startswith("child-exit:")] == []
    assert len(child_cleanup.listed()) == child_started
    if child_started:
        assert refusal.value is A_WATCHER_LAUNCH_FAILURE
        (owned,) = child_cleanup.listed()
        assert child_cleanup.state(owned) == GONE


def test_closing_the_streams_closes_every_descriptor_though_one_close_raises(
    leaders: Leaders, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptors_before = _open_descriptors()
    fault = make_the_first_parent_close_of_a_door_fail(monkeypatch)
    child, _owned = open_door(child_cleanup, leaders.describe())

    with pytest.raises(OSError) as raised:
        child.close_streams()

    held_by_the_cleanup = {str(owned.pidfd) for owned in child_cleanup.listed()}
    assert raised.value is fault.failures[0]
    assert all(stream.closed for stream in (child.stdin, child.stdout, child.stderr))
    assert _open_descriptors() - held_by_the_cleanup == descriptors_before


def test_a_running_leader_is_refused_its_release_and_keeps_its_streams(
    virtual_rig: Rig,
) -> None:
    with pytest.raises(AnchorStateError):
        virtual_rig.child.release()
    streams_stay_open = [
        stream.closed for stream in (virtual_rig.child.stdin, virtual_rig.child.stdout)
    ]
    virtual_rig.child.signal_group(signal.SIGKILL)

    virtual_rig.finish()

    assert streams_stay_open == [False, False]


def test_a_call_after_the_release_is_refused_without_a_kill_a_count_or_a_wait(
    virtual_rig: Rig, clock: VirtualBlockingClock,
) -> None:
    virtual_rig.child.signal_group(signal.SIGKILL)
    virtual_rig.finish()
    signals_before = len(virtual_rig.model.calls)

    with pytest.raises(AnchorStateError):
        virtual_rig.child.kill_until_gone(within=10)

    assert len(virtual_rig.model.calls) == signals_before
    assert virtual_rig.census_calls == []
    assert clock.waits == []


def kill_until_gone_then_finish(rig: Rig, within: float):
    result = rig.child.kill_until_gone(within=within)
    rig.finish()
    return result


def test_the_first_empty_count_in_time_confirms_and_ends_the_call(
    virtual_rig: Rig, clock: VirtualBlockingClock,
) -> None:
    virtual_rig.answer(LIVE, LIVE, EMPTY)

    result = kill_until_gone_then_finish(virtual_rig, 10)

    assert (result.census, result.ended_in_time, result.confirmed) == (EMPTY, True, True)
    assert len(virtual_rig.counts) == 3
    assert len(clock.waits) == 2


def test_the_schedule_spaces_attempts_from_each_counts_end_and_stops_before_the_deadline(
    virtual_rig: Rig, clock: VirtualBlockingClock,
) -> None:
    virtual_rig.on_count = lambda ordinal: clock.advance(0.04) if ordinal == 1 else None

    result = kill_until_gone_then_finish(virtual_rig, 0.22)

    assert virtual_rig.counts == pytest.approx([ORIGIN, ORIGIN + 0.04 + INTERVAL])
    assert clock.waits == pytest.approx([ORIGIN + 0.04 + INTERVAL])
    assert (result.census, result.ended_in_time, result.confirmed) == (LIVE, True, False)
    assert virtual_rig.log.kinds == ["kill", "count"] * 2


@pytest.mark.parametrize(
    ("within", "answers", "ends_late_at_count"),
    [
        pytest.param(0, (LIVE,), 1, id="a-zero-bound-and-a-count-that-ends-after-it"),
        pytest.param(0.25, (LIVE, EMPTY), 2, id="an-empty-count-that-ends-after-the-bound"),
        pytest.param(0.25, (LIVE, LIVE), 2, id="a-live-count-that-ends-after-the-bound"),
    ],
)
def test_a_count_that_ends_past_the_bound_never_confirms_an_empty_one_included(
    within: float,
    answers: tuple[GroupCensus, ...],
    ends_late_at_count: int,
    virtual_rig: Rig,
    clock: VirtualBlockingClock,
) -> None:
    virtual_rig.answer(*answers)
    virtual_rig.on_count = lambda ordinal: (
        clock.advance(0.5) if ordinal == ends_late_at_count else None
    )

    result = kill_until_gone_then_finish(virtual_rig, within)

    assert (result.census, result.ended_in_time, result.confirmed) == (
        answers[-1], False, False,
    )
    assert len(virtual_rig.counts) == ends_late_at_count
    assert len(clock.waits) == ends_late_at_count - 1


@pytest.mark.parametrize(
    ("within", "expected"),
    [
        pytest.param(math.nan, ValueError, id="nan"),
        pytest.param(math.inf, ValueError, id="inf"),
        pytest.param(-1, ValueError, id="negative"),
        pytest.param("0.1", TypeError, id="a-string"),
    ],
)
def test_a_refused_bound_signals_and_counts_nothing_before_any_clock_read(
    within: object, expected: type[Exception], virtual_rig: Rig, clock: VirtualBlockingClock,
) -> None:
    with pytest.raises(expected):
        virtual_rig.child.kill_until_gone(within=within)

    assert virtual_rig.model.calls == []
    assert virtual_rig.census_calls == []
    assert clock.reads == 0
    virtual_rig.child.signal_group(signal.SIGKILL)
    virtual_rig.finish()


@pytest.mark.parametrize("unavailable", UNAVAILABLE, ids=lambda census: census.value)
def test_an_unavailable_count_ends_the_call_unchanged_and_never_confirms(
    unavailable: GroupCensus, virtual_rig: Rig, clock: VirtualBlockingClock,
) -> None:
    virtual_rig.answer(LIVE, unavailable)

    result = kill_until_gone_then_finish(virtual_rig, 10)

    assert result.census is unavailable
    assert result.confirmed is False
    assert len(virtual_rig.counts) == 2
    assert len(clock.waits) == 1


@dataclass(frozen=True)
class Wake:
    overrun: float = 0.0
    at_deadline: bool = False


@pytest.mark.parametrize(
    ("wake", "second_count_instant"),
    [
        pytest.param(Wake(overrun=0.06), ORIGIN + 0.16, id="overrun-before-the-deadline"),
        pytest.param(Wake(overrun=0.2), None, id="overrun-past-the-deadline"),
        pytest.param(Wake(at_deadline=True), None, id="wake-on-the-deadline"),
    ],
)
def test_a_late_wait_never_starts_an_attempt_at_or_after_the_deadline(
    wake: Wake,
    second_count_instant: float | None,
    virtual_rig: Rig,
    clock: VirtualBlockingClock,
) -> None:
    within = 0.25
    clock.overrun = wake.overrun
    clock.wake_at = ORIGIN + within if wake.at_deadline else None

    result = kill_until_gone_then_finish(virtual_rig, within)

    expected_instants = [ORIGIN] + ([] if second_count_instant is None else [second_count_instant])
    assert virtual_rig.counts == pytest.approx(expected_instants)
    assert clock.waits == pytest.approx([ORIGIN + INTERVAL])
    assert (result.census, result.ended_in_time, result.confirmed) == (LIVE, True, False)


def test_a_count_failure_propagates_at_once_without_a_further_attempt(
    virtual_rig: Rig, clock: VirtualBlockingClock,
) -> None:
    failure = RuntimeError("the second count failed")

    def fail_the_second_count(ordinal: int) -> None:
        if ordinal == 2:
            raise failure

    virtual_rig.on_count = fail_the_second_count

    with pytest.raises(RuntimeError) as raised:
        virtual_rig.child.kill_until_gone(within=10)
    virtual_rig.finish()

    assert raised.value is failure
    assert virtual_rig.log.kinds == ["kill", "count"] * 2
    assert clock.waits == pytest.approx([ORIGIN + INTERVAL])


def test_the_real_clock_confirms_with_a_lingering_descendant_dead_at_the_return(
    tmp_path: Path, handshake: Handshake, child_cleanup: ChildCleanup,
) -> None:
    script = lingering_descendant(
        "", exit_code=0, blocks=False, escapes=False, handshake=handshake,
    )
    description = ChildProcess(
        binary=Path("/bin/sh"),
        arguments=("-c", script),
        environment=closed_environment(tmp_path),
        working_directory=tmp_path,
    )
    child, owned = open_door(child_cleanup, description, "devnull")

    with DescendantRow(handshake, child_cleanup) as row:
        row.own_the_descendant()
        child.stdin.close()
        assert child.wait_for_exit(TEST_WAIT_SECONDS)
        result = child.kill_until_gone(within=TEST_WAIT_SECONDS)
        descendant_dead_at_return = row.descendant_is_dead()
    child.release()

    assert result.confirmed is True
    assert descendant_dead_at_return
    assert child_cleanup.state(owned) == GONE
