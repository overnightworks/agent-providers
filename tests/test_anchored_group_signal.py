"""A group signal and a kill-then-count reach an anchored leader's group only under its lock."""

from __future__ import annotations

import os
import select
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from anchored_kernel import (
    OBSERVE_EXIT,
    CensusRecorder,
    EventLog,
    GroupSignalCall,
    GroupSignalModel,
    KernelModel,
    PassThroughKernel,
    install_kernel,
    join_watcher,
)
from anchored_leaders import (
    LEADER_AND_GRANDCHILD_WAITING,
    LEADER_WITH_GRANDCHILD,
    InstrumentedLock,
    Leaders,
    Releaser,
    Started,
    spawn_warnings,
    start_bound,
)
from anchored_support import (
    GONE,
    IMPORT_ROOT_PRELUDE,
    RUNNING,
    ChildCleanup,
    Exited,
    ProcessState,
    absolute_import_roots,
    pidfd_open,
    wait_for_exits,
)
from bounds import TEST_WAIT_SECONDS
from provider_test_support import call_within_deadline

from agent_runtime import procfs, spawn
from agent_runtime.procfs import GroupCensus, RealProcView, group_census
from agent_runtime.spawn import (
    AnchorState,
    AnchorStateError,
    ChildIdentityLost,
    ChildProcess,
    closed_environment,
)

EXIT_CODE = 3

LEAVER = (
    "import os, sys\n"
    "def say(text):\n"
    "    os.write(1, (text + '\\n').encode())\n"
    "def serve(group):\n"
    "    while True:\n"
    "        command = os.read(0, 64).strip()\n"
    "        if command == b'ping':\n"
    "            say('pong')\n"
    "        elif command == b'join':\n"
    "            os.setpgid(0, group)\n"
    "            say('joined')\n"
    "        elif command == b'handoff':\n"
    "            if os.fork() != 0:\n"
    "                os._exit(0)\n"
    "            say('successor ' + str(os.getpid()))\n"
    "        else:\n"
    "            os._exit(0)\n"
    "if os.fork() == 0:\n"
    "    group = os.getpgrp()\n"
    "    {leave}\n"
    "    say('left ' + str(os.getpid()))\n"
    "    serve(group)\n"
    "sys.exit(3)\n"
)

LeaderEnd = Callable[
    [Leaders, ChildCleanup, pytest.MonkeyPatch], tuple[Started, ProcessState | Exited]
]

SIGCHLD_IGNORED_START = (
    IMPORT_ROOT_PRELUDE
    + """
import subprocess, threading
from pathlib import Path
import signal
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
launched = []
real_popen = subprocess.Popen
subprocess.Popen = lambda *args, **kwargs: launched.append(args) or real_popen(*args, **kwargs)
from agent_runtime import spawn
from agent_runtime.spawn import AnchorStateError, ChildProcess, closed_environment
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
)


def signal_within_bound(started: Started, sig: signal.Signals) -> None:
    call_within_deadline(started.leader.signal_group, sig, seconds=TEST_WAIT_SECONDS)


@pytest.fixture(autouse=True)
def no_spawn_warning(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    yield
    assert spawn_warnings(caplog) == []


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


def read_line(descriptor: int) -> str:
    line = b""
    while not line.endswith(b"\n"):
        readable, _, _ = select.select([descriptor], [], [], TEST_WAIT_SECONDS)
        assert readable, "the child wrote no line within the bound"
        byte = os.read(descriptor, 1)
        assert byte, "the child closed its output before a full line"
        line += byte
    return line.decode().strip()


def read_pid_line(descriptor: int) -> int:
    return int(read_line(descriptor))


def read_reported_pid(descriptor: int, report: str) -> int:
    label, pid = read_line(descriptor).split()
    assert label == report
    return int(pid)


def kill_and_census_within_bound(started: Started) -> procfs.GroupCensus:
    return call_within_deadline(started.leader.kill_and_census, seconds=TEST_WAIT_SECONDS)


@dataclass(frozen=True)
class GroupCall:
    send: Callable[[spawn.AnchoredLeader], object]
    counts: bool


def group_calls(sig: signal.Signals) -> list[Any]:
    return [
        pytest.param(
            GroupCall(lambda leader: leader.signal_group(sig), counts=False), id="signal-group"
        ),
        pytest.param(
            GroupCall(lambda leader: leader.kill_and_census(), counts=True), id="kill-and-census"
        ),
    ]


def call_within_bound(started: Started, call: GroupCall) -> object:
    return call_within_deadline(call.send, started.leader, seconds=TEST_WAIT_SECONDS)


class LoggedLock(InstrumentedLock):
    def __init__(self, log: EventLog) -> None:
        super().__init__()
        self._log = log

    def acquire(self, blocking: bool = True) -> bool:
        acquired = super().acquire(blocking)
        if acquired:
            self._log.record("acquire")
        return acquired

    def release(self) -> None:
        self._log.record("release")
        super().release()


def test_a_group_signal_after_the_leader_exited_reaches_a_sigterm_ignoring_grandchild(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
) -> None:
    pid_reader, pid_writer = os.pipe()
    child_cleanup.close_at_cleanup(pid_reader)
    started = leaders.start(script=LEADER_WITH_GRANDCHILD, stdout=pid_writer)
    os.close(pid_writer)
    grandchild = pidfd_open(read_pid_line(pid_reader))
    child_cleanup.close_at_cleanup(grandchild)
    leaders.await_exit_seen(started)
    assert started.probe.observed == [(AnchorState.EXITED, EXIT_CODE)]

    signal_within_bound(started, signal.SIGKILL)

    assert grandchild in wait_for_exits([grandchild], TEST_WAIT_SECONDS)
    assert group_census(RealProcView(), started.owned.pid) is GroupCensus.EMPTY
    assert child_cleanup.state(started.owned) == Exited(EXIT_CODE)
    assert started.leader.state is AnchorState.EXITED
    release_within_bound(started)
    assert child_cleanup.state(started.owned) == GONE
    assert thread_failures == []


@pytest.mark.parametrize("call", group_calls(signal.SIGKILL))
def test_a_group_signal_the_kernel_refuses_propagates_and_leaves_the_handle_usable(
    call: GroupCall,
    census_calls: CensusRecorder,
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
            call_within_bound(started, call)
        assert raised.value is refusal
        assert census_calls == []
        assert call_within_deadline(lambda: started.leader.state, seconds=TEST_WAIT_SECONDS) is (
            AnchorState.RUNNING
        )
        assert child_cleanup.state(started.owned) == RUNNING
        assert started.owned.popen.returncode is None
        assert not started.probe.seen.is_set()

        call_within_bound(started, call)
        leaders.await_exit_seen(started)

    assert len(census_calls) == int(call.counts)
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


@pytest.mark.parametrize("call", group_calls(signal.SIGTERM))
@pytest.mark.parametrize(
    ("end", "refusal", "state"),
    [
        pytest.param(a_released_leader, AnchorStateError, AnchorState.RELEASED, id="after-release"),
        pytest.param(a_lost_leader, ChildIdentityLost, AnchorState.LOST, id="after-loss"),
    ],
)
def test_a_group_signal_after_the_end_is_refused_without_a_kernel_call(
    call: GroupCall,
    census_calls: CensusRecorder,
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
            call_within_bound(started, call)

    assert model.calls == []
    assert census_calls == []
    assert call_within_deadline(lambda: started.leader.state, seconds=TEST_WAIT_SECONDS) is state
    assert child_cleanup.state(started.owned) == process
    assert thread_failures == []


def exited_leader(leaders: Leaders) -> Started:
    started = leaders.start()
    started.write_exit_line(EXIT_CODE)
    leaders.await_exit_seen(started)
    return started


def running_leader(leaders: Leaders) -> Started:
    return leaders.start()


@pytest.mark.parametrize("call", group_calls(signal.SIGKILL))
@pytest.mark.parametrize("error", [ProcessLookupError, ChildProcessError])
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
def test_a_group_signal_that_meets_esrch_or_echild_commits_the_loss_and_raises(
    call: GroupCall,
    error: type[OSError],
    census_calls: CensusRecorder,
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
    model = group_signal_model(failure=error())
    with monkeypatch.context() as patch:
        started = anchored_leader(leaders)
        model.bind(started.owned.pid)
        model.install(patch)

        with pytest.raises(ChildIdentityLost):
            call_within_bound(started, call)

    assert census_calls == []
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
    reader, writer = os.pipe()
    child_cleanup.close_at_cleanup(writer)
    isolated = child_cleanup.start_bare_child(
        ChildProcess(
            binary=Path(sys.executable),
            arguments=("-I", "-c", SIGCHLD_IGNORED_START, absolute_import_roots()),
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


def test_a_kill_and_census_runs_in_one_hold_that_keeps_a_release_waiting(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
    group_signal_model: Callable[..., GroupSignalModel],
    census_calls: CensusRecorder,
) -> None:
    log = EventLog()
    lock = LoggedLock(log)
    releasers: list[Releaser] = []
    started_holder: list[Started] = []
    states_inside_the_count: list[ProcessState | Exited] = []
    admit = spawn.admit_group_signal

    def admit_logged(state: AnchorState) -> None:
        log.record("admit")
        admit(state)

    def release_in_the_background() -> None:
        releasers.append(Releaser(started_holder[0].leader))
        lock.await_waiting(1)
        states_inside_the_count.append(child_cleanup.state(started_holder[0].owned))

    census_calls.log = log
    census_calls.during = release_in_the_background
    model = group_signal_model(during=lambda call: log.record("kill"))
    with monkeypatch.context() as patch:
        patch.setattr(spawn, "_lifecycle_lock", lambda: lock)
        started = exited_leader(leaders)
        started_holder.append(started)
        model.bind(started.owned.pid)
        model.install(patch)
        patch.setattr(spawn, "admit_group_signal", admit_logged)
        before = len(log)

        census = kill_and_census_within_bound(started)

        after = len(log)
        releasers[0].join()

    caller = log[before][1]
    interval = log[before:after]
    assert census is GroupCensus.EMPTY
    assert [kind for kind, thread in interval if thread == caller] == [
        "acquire",
        "admit",
        "kill",
        "count",
        "release",
    ]
    caller_release = log.index(("release", caller), before)
    releaser_acquisition = next(
        index
        for index, (kind, thread) in enumerate(log)
        if index >= before and kind == "acquire" and thread != caller
    )
    assert releaser_acquisition > caller_release
    assert states_inside_the_count == [Exited(EXIT_CODE)]
    assert releasers[0].error is None
    assert started.leader.state is AnchorState.RELEASED
    assert child_cleanup.state(started.owned) == GONE
    assert thread_failures == []


@pytest.mark.parametrize("answer", list(GroupCensus))
def test_a_kill_and_census_returns_the_scripted_count_unchanged_after_the_kill(
    answer: GroupCensus,
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
    group_signal_model: Callable[..., GroupSignalModel],
    census_calls: CensusRecorder,
) -> None:
    log = EventLog()
    census_calls.log = log
    census_calls.answer = answer
    model = group_signal_model(during=lambda call: log.record("kill"))
    with monkeypatch.context() as patch:
        started = exited_leader(leaders)
        model.bind(started.owned.pid)
        model.install(patch)

        census = kill_and_census_within_bound(started)

    assert census is answer
    assert log.kinds == ["kill", "count"]
    assert census_calls == [started.owned.pid]
    release_within_bound(started)
    assert thread_failures == []


@pytest.mark.parametrize(
    ("script", "leader_exits_first", "final_code"),
    [
        pytest.param(LEADER_WITH_GRANDCHILD, True, EXIT_CODE, id="leader-exited-first"),
        pytest.param(
            LEADER_AND_GRANDCHILD_WAITING, False, -signal.SIGKILL, id="leader-still-running"
        ),
    ],
)
def test_a_kill_and_census_kills_a_sigterm_ignoring_grandchild_and_leaves_the_leader_unreaped(
    script: str,
    leader_exits_first: bool,
    final_code: int,
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
) -> None:
    pid_reader, pid_writer = os.pipe()
    child_cleanup.close_at_cleanup(pid_reader)
    started = leaders.start(script=script, stdout=pid_writer)
    os.close(pid_writer)
    grandchild = pidfd_open(read_pid_line(pid_reader))
    child_cleanup.close_at_cleanup(grandchild)
    if leader_exits_first:
        leaders.await_exit_seen(started)

    first = kill_and_census_within_bound(started)

    assert first in {GroupCensus.LIVE, GroupCensus.EMPTY}
    assert grandchild in wait_for_exits([grandchild], TEST_WAIT_SECONDS)
    leaders.await_exit_seen(started)
    assert group_census(RealProcView(), started.owned.pid) is GroupCensus.EMPTY
    assert started.leader.state is AnchorState.EXITED
    assert started.leader.returncode == final_code
    assert child_cleanup.state(started.owned) == Exited(final_code)
    assert started.probe.observed == [(AnchorState.EXITED, final_code)]
    release_within_bound(started)
    assert child_cleanup.state(started.owned) == GONE
    assert thread_failures == []


@dataclass
class LeaverRun:
    started: Started
    output: int
    leaver: int

    def tell(self, command: str) -> str:
        os.write(self.started.stdin, f"{command}\n".encode())
        return read_line(self.output)


def watched(pid: int, child_cleanup: ChildCleanup) -> int:
    pidfd = pidfd_open(pid)
    child_cleanup.close_at_cleanup(pidfd)
    return pidfd


def start_leaver(leaders: Leaders, child_cleanup: ChildCleanup, leave: str) -> LeaverRun:
    output, writer = os.pipe()
    child_cleanup.close_at_cleanup(output)
    started = leaders.start(script=LEAVER.format(leave=leave), stdout=writer)
    os.close(writer)
    leaver = watched(read_reported_pid(output, "left"), child_cleanup)
    leaders.await_exit_seen(started)
    return LeaverRun(started, output, leaver)


def assert_gone_within_bound(*pidfds: int) -> None:
    assert set(pidfds) <= wait_for_exits(pidfds, TEST_WAIT_SECONDS)


SETSID = "os.setsid()"
SETPGID = "os.setpgid(0, 0)"


@pytest.mark.parametrize("leave", [SETSID, SETPGID], ids=["setsid", "setpgid"])
def test_a_leaver_outside_the_group_is_neither_counted_nor_killed(
    leave: str,
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
) -> None:
    run = start_leaver(leaders, child_cleanup, leave)

    census = kill_and_census_within_bound(run.started)

    assert census is GroupCensus.EMPTY
    assert run.tell("ping") == "pong"
    os.write(run.started.stdin, b"exit\n")
    assert_gone_within_bound(run.leaver)
    release_within_bound(run.started)
    assert child_cleanup.state(run.started.owned) == GONE
    assert thread_failures == []


def test_a_fork_hand_off_between_two_leavers_changes_no_answer_and_kills_neither(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
) -> None:
    run = start_leaver(leaders, child_cleanup, SETPGID)
    os.write(run.started.stdin, b"handoff\n")

    census = kill_and_census_within_bound(run.started)

    assert census is GroupCensus.EMPTY
    successor = watched(read_reported_pid(run.output, "successor"), child_cleanup)
    assert_gone_within_bound(run.leaver)
    assert run.tell("ping") == "pong"
    os.write(run.started.stdin, b"exit\n")
    assert_gone_within_bound(successor)
    release_within_bound(run.started)
    assert thread_failures == []


def test_a_rejoiner_is_killed_by_the_next_kill_and_census(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    census_calls: CensusRecorder,
) -> None:
    run = start_leaver(leaders, child_cleanup, SETPGID)
    assert kill_and_census_within_bound(run.started) is GroupCensus.EMPTY
    assert run.tell("join") == "joined"

    second = kill_and_census_within_bound(run.started)

    assert second in {GroupCensus.LIVE, GroupCensus.EMPTY}
    assert_gone_within_bound(run.leaver)
    assert kill_and_census_within_bound(run.started) is GroupCensus.EMPTY
    assert census_calls == [run.started.owned.pid] * 3
    release_within_bound(run.started)
    assert child_cleanup.state(run.started.owned) == GONE
    assert thread_failures == []


def test_an_exception_from_the_count_propagates_and_leaves_the_lock_usable(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
    group_signal_model: Callable[..., GroupSignalModel],
    census_calls: CensusRecorder,
) -> None:
    log = EventLog()
    lock = LoggedLock(log)
    failure = RuntimeError("scripted count failure")
    census_calls.failure = failure
    model = group_signal_model()
    with monkeypatch.context() as patch:
        patch.setattr(spawn, "_lifecycle_lock", lambda: lock)
        started = exited_leader(leaders)
        model.bind(started.owned.pid)
        model.install(patch)

        with pytest.raises(RuntimeError) as raised:
            kill_and_census_within_bound(started)

    assert raised.value is failure
    assert log.kinds[-1] == "release"
    assert len(model.calls) == 1
    assert call_within_deadline(lambda: started.leader.state, seconds=TEST_WAIT_SECONDS) is (
        AnchorState.EXITED
    )
    assert call_within_deadline(lambda: started.leader.returncode, seconds=TEST_WAIT_SECONDS) == (
        EXIT_CODE
    )
    release_within_bound(started)
    assert child_cleanup.state(started.owned) == GONE
    assert thread_failures == []


def test_an_empty_count_publishes_nothing_while_the_watcher_is_held(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = PassThroughKernel(hold_after_call=1)
    with monkeypatch.context() as patch:
        install_kernel(patch, kernel)
        started = start_bound(leaders, kernel)
        started.write_exit_line(EXIT_CODE)
        try:
            kernel.await_reached()

            census = kill_and_census_within_bound(started)

            assert census is GroupCensus.EMPTY
            assert started.leader.state is AnchorState.RUNNING
            assert started.probe.observed == []
            assert not started.probe.seen.is_set()
        finally:
            kernel.open_hold()
        leaders.await_exit_seen(started)

        assert started.probe.observed == [(AnchorState.EXITED, EXIT_CODE)]
        assert started.leader.state is AnchorState.EXITED
        release_within_bound(started)

    assert kernel.violations == []
    assert child_cleanup.state(started.owned) == GONE
    assert thread_failures == []
