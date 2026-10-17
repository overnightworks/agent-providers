"""A group signal reaches an anchored leader's group only under the lifecycle lock."""

from __future__ import annotations

import os
import select
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from anchored_kernel import (
    OBSERVE_EXIT,
    GroupSignalCall,
    GroupSignalModel,
    KernelModel,
    install_kernel,
    join_watcher,
)
from anchored_leaders import (
    LEADER_WITH_GRANDCHILD,
    InstrumentedLock,
    Leaders,
    Releaser,
    Started,
    start_bound,
)
from anchored_support import (
    GONE,
    RUNNING,
    TEST_WAIT_SECONDS,
    ChildCleanup,
    Exited,
    ProcessState,
    _pidfd_open,
    _wait_for_exits,
)
from provider_test_support import call_within_deadline

from agent_providers import spawn
from agent_providers.procfs import GroupCensus, RealProcView, group_census
from agent_providers.spawn import (
    AnchorState,
    AnchorStateError,
    ChildIdentityLost,
    ChildProcess,
    closed_environment,
)

EXIT_CODE = 3

LeaderEnd = Callable[
    [Leaders, ChildCleanup, pytest.MonkeyPatch], tuple[Started, ProcessState | Exited]
]

SIGCHLD_IGNORED_START = """
import os, subprocess, sys, threading
from pathlib import Path
sys.path[:0] = sys.argv[1].split(os.pathsep)
import signal
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
launched = []
real_popen = subprocess.Popen
subprocess.Popen = lambda *args, **kwargs: launched.append(args) or real_popen(*args, **kwargs)
from agent_providers import spawn
from agent_providers.spawn import AnchorStateError, ChildProcess, closed_environment
child = ChildProcess(
    binary=Path(sys.executable),
    arguments=("-I", "-c", "pass"),
    environment=closed_environment(Path.cwd()),
    working_directory=Path.cwd(),
)
try:
    spawn.start_anchored_leader(
        child, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, on_exit=lambda: None,
    )
except AnchorStateError:
    refused = True
else:
    refused = False
watchers = [t for t in threading.enumerate() if t.name.startswith("child-exit:")]
sys.exit(0 if refused and not launched and not watchers else 2)
"""


def signal_within_bound(started: Started, sig: signal.Signals) -> None:
    call_within_deadline(started.leader.signal_group, sig, seconds=TEST_WAIT_SECONDS)


@pytest.fixture(autouse=True)
def no_spawn_warning(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    yield


@pytest.fixture
def group_signal_model() -> Iterator[Callable[..., GroupSignalModel]]:
    models: list[GroupSignalModel] = []

    def make(**options: Any) -> GroupSignalModel:
        models.append(GroupSignalModel(**options))
        return models[-1]

    yield make
    assert [model.violations for model in models] == [[] for _ in models]


def release_within_bound(started: Started) -> None:
    call_within_deadline(started.leader.release, seconds=TEST_WAIT_SECONDS)


def read_pid_line(descriptor: int) -> int:
    readable, _, _ = select.select([descriptor], [], [], TEST_WAIT_SECONDS)
    assert readable, "the grandchild wrote no pid within the bound"
    return int(os.read(descriptor, 32))


def test_a_group_signal_after_the_leader_exited_reaches_a_sigterm_ignoring_grandchild(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
) -> None:
    pid_reader, pid_writer = os.pipe()
    child_cleanup.close_at_cleanup(pid_reader)
    started = leaders.start(script=LEADER_WITH_GRANDCHILD, stdout=pid_writer)
    os.close(pid_writer)
    grandchild = _pidfd_open(read_pid_line(pid_reader))
    child_cleanup.close_at_cleanup(grandchild)
    leaders.await_exit_seen(started)
    assert started.probe.observed == [(AnchorState.EXITED, EXIT_CODE)]

    signal_within_bound(started, signal.SIGKILL)

    assert grandchild in _wait_for_exits([grandchild], TEST_WAIT_SECONDS)
    assert group_census(RealProcView(), started.owned.pid) is GroupCensus.EMPTY
    assert child_cleanup.state(started.owned) == Exited(EXIT_CODE)
    assert started.leader.state is AnchorState.EXITED
    release_within_bound(started)
    assert child_cleanup.state(started.owned) == GONE
    assert thread_failures == []


def test_a_group_signal_the_kernel_refuses_propagates_and_leaves_the_handle_usable(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
    group_signal_model: Callable[..., GroupSignalModel],
) -> None:
    refusal = PermissionError()
    model = group_signal_model(failure=refusal)
    with monkeypatch.context() as patch:
        started = leaders.start()
        model.bind(started.owned.pid)
        model.install(patch)

        with pytest.raises(PermissionError) as raised:
            signal_within_bound(started, signal.SIGKILL)
        assert raised.value is refusal
        assert started.leader.state is AnchorState.RUNNING
        assert child_cleanup.state(started.owned) == RUNNING
        assert started.owned.popen.returncode is None
        assert not started.probe.seen.is_set()

        signal_within_bound(started, signal.SIGKILL)
        leaders.await_exit_seen(started)

    assert started.probe.observed == [(AnchorState.EXITED, -signal.SIGKILL)]
    release_within_bound(started)
    assert started.owned.popen.returncode == -signal.SIGKILL
    assert [(call.pgid, call.sig) for call in model.calls] == [
        (started.owned.pid, signal.SIGKILL)
    ] * 2
    assert thread_failures == []


def a_released_leader(
    leaders: Leaders, child_cleanup: ChildCleanup, patch: pytest.MonkeyPatch
) -> tuple[Started, ProcessState | Exited]:
    started = leaders.start()
    started.write_exit_line(EXIT_CODE)
    leaders.await_exit_seen(started)
    release_within_bound(started)
    return started, GONE


def a_lost_leader(
    leaders: Leaders, child_cleanup: ChildCleanup, patch: pytest.MonkeyPatch
) -> tuple[Started, ProcessState | Exited]:
    kernel = KernelModel(loses_at="wait")
    install_kernel(patch, kernel)
    started = start_bound(leaders, kernel)
    started.write_exit_line(0)
    leaders.await_exit_seen(started)
    child_cleanup.wait_exited(started.owned)
    return started, Exited(0)


@pytest.mark.parametrize(
    ("end", "refusal", "state"),
    [
        pytest.param(a_released_leader, AnchorStateError, AnchorState.RELEASED, id="after-release"),
        pytest.param(a_lost_leader, ChildIdentityLost, AnchorState.LOST, id="after-loss"),
    ],
)
def test_a_group_signal_after_the_end_is_refused_without_a_kernel_call(
    end: LeaderEnd,
    refusal: type[Exception],
    state: AnchorState,
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
    group_signal_model: Callable[..., GroupSignalModel],
) -> None:
    model = group_signal_model(forward=False)
    with monkeypatch.context() as patch:
        started, process = end(leaders, child_cleanup, patch)
        model.bind(started.owned.pid)
        model.install(patch)

        with pytest.raises(refusal):
            signal_within_bound(started, signal.SIGTERM)

    assert model.calls == []
    assert started.leader.state is state
    assert child_cleanup.state(started.owned) == process
    assert thread_failures == []


def exited_leader(leaders: Leaders) -> Started:
    started = leaders.start()
    started.write_exit_line(EXIT_CODE)
    leaders.await_exit_seen(started)
    return started


def running_leader(leaders: Leaders) -> Started:
    return leaders.start()


@pytest.mark.parametrize(
    ("anchored_leader", "announced", "kept_returncode", "child_state", "exits_late"),
    [
        pytest.param(
            running_leader,
            [(AnchorState.LOST, None)],
            None,
            RUNNING,
            True,
            id="from-running-announces-the-loss-once",
        ),
        pytest.param(
            exited_leader,
            [(AnchorState.EXITED, EXIT_CODE)],
            EXIT_CODE,
            Exited(EXIT_CODE),
            False,
            id="from-exited-keeps-the-observed-code-and-announces-nothing",
        ),
    ],
)
def test_a_group_signal_that_meets_esrch_commits_the_loss_and_raises(
    anchored_leader: Callable[[Leaders], Started],
    announced: list[tuple[AnchorState, int | None]],
    kept_returncode: int | None,
    child_state: ProcessState | Exited,
    exits_late: bool,
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
    group_signal_model: Callable[..., GroupSignalModel],
) -> None:
    model = group_signal_model(failure=ProcessLookupError())
    with monkeypatch.context() as patch:
        started = anchored_leader(leaders)
        model.bind(started.owned.pid)
        model.install(patch)

        with pytest.raises(ChildIdentityLost):
            signal_within_bound(started, signal.SIGKILL)

    assert started.leader.state is AnchorState.LOST
    assert started.leader.returncode == kept_returncode
    disarmed = started.owned.popen.returncode
    assert disarmed is not None
    assert not isinstance(disarmed, int)
    assert child_cleanup.state(started.owned) == child_state
    if exits_late:
        started.write_exit_line(0)
    join_watcher(started.watcher)
    assert started.probe.observed == announced
    assert started.leader.done_with_pid is True
    assert thread_failures == []


def test_a_stop_report_held_across_a_loss_ends_the_watcher_without_a_warning(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
    group_signal_model: Callable[..., GroupSignalModel],
) -> None:
    kernel = KernelModel(stops=1, hold_after_call=1)
    model = group_signal_model(failure=ProcessLookupError())
    with monkeypatch.context() as patch:
        install_kernel(patch, kernel)
        started = start_bound(leaders, kernel)
        model.bind(started.owned.pid)
        model.install(patch)
        kernel.await_reached()

        with pytest.raises(ChildIdentityLost):
            signal_within_bound(started, signal.SIGKILL)
        kernel.open_hold()
        join_watcher(started.watcher)

    assert started.probe.observed == [(AnchorState.LOST, None)]
    assert [(call.name, call.options) for call in kernel.calls] == [("waitid", OBSERVE_EXIT)]
    assert kernel.violations == []
    assert started.leader.done_with_pid is True
    assert thread_failures == []


def test_a_group_signal_runs_inside_the_lock_that_covers_the_final_reap(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
    group_signal_model: Callable[..., GroupSignalModel],
) -> None:
    lock = InstrumentedLock()
    releasers: list[Releaser] = []
    started_holder: list[Started] = []

    def release_in_the_background(call: GroupSignalCall) -> None:
        releasers.append(Releaser(started_holder[0].leader))
        lock.await_waiting(1)
        assert child_cleanup.state(started_holder[0].owned) == Exited(EXIT_CODE)

    model = group_signal_model(during=release_in_the_background)
    with monkeypatch.context() as patch:
        patch.setattr(spawn, "_lifecycle_lock", lambda: lock)
        started = exited_leader(leaders)
        started_holder.append(started)
        model.bind(started.owned.pid)
        model.install(patch)

        signal_within_bound(started, signal.SIGTERM)
        releasers[0].join()

    assert releasers[0].error is None
    assert started.leader.state is AnchorState.RELEASED
    assert child_cleanup.state(started.owned) == GONE
    assert [(call.pgid, call.sig) for call in model.calls] == [(started.owned.pid, signal.SIGTERM)]
    assert thread_failures == []


def test_an_ignored_sigchld_refuses_the_start_before_any_child_exists(
    child_cleanup: ChildCleanup, tmp_path: Path
) -> None:
    import_roots = [entry for entry in sys.path if entry]
    assert all(os.path.isabs(entry) for entry in import_roots)
    reader, writer = os.pipe()
    child_cleanup.close_at_cleanup(writer)
    isolated = child_cleanup.start_bare_child(
        ChildProcess(
            binary=Path(sys.executable),
            arguments=("-I", "-c", SIGCHLD_IGNORED_START, os.pathsep.join(import_roots)),
            environment=closed_environment(tmp_path),
            working_directory=tmp_path,
        ),
        stdin=reader,
        stdout=None,
        stderr=None,
    )
    os.close(reader)

    assert child_cleanup.wait_exited(isolated) == 0


def test_done_with_pid_reads_without_waiting_for_a_held_lifecycle_lock(
    leaders: Leaders,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = InstrumentedLock()
    with monkeypatch.context() as patch:
        patch.setattr(spawn, "_lifecycle_lock", lambda: lock)
        started = exited_leader(leaders)
        release_within_bound(started)

    def read() -> bool:
        return call_within_deadline(lambda: started.leader.done_with_pid, seconds=TEST_WAIT_SECONDS)

    assert read() is True
    with lock:
        assert read() is False
        assert lock.waiting == []
    assert read() is True
    assert thread_failures == []
