"""The anchored leader starts in its own session and is watched, never reaped, by its handle."""

from __future__ import annotations

import gc
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest
from anchored_kernel import (
    CONSUME_STOP_REPORT,
    OBSERVE_EXIT,
    WATCHER_THREAD_PREFIX,
    KernelModel,
    PassThroughKernel,
)
from anchored_support import TEST_WAIT_SECONDS, ChildCleanup, Exited, OwnedChild
from test_zombie_anchor import EXIT_WITH_LINE

from agent_providers import spawn
from agent_providers.spawn import AnchoredLeader, AnchorState, ChildProcess, closed_environment

SPAWN_LOGGER = "agent_providers.spawn"
SIGKILLED = -9


class ExitProbe:
    """The ``on_exit`` callback: counts its calls and reads the handle from inside them."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.seen = threading.Event()
        self.observed: list[tuple[AnchorState, int | None]] = []
        self.leader: AnchoredLeader | None = None
        self._bound = threading.Event()
        self._failure = failure

    def bind(self, leader: AnchoredLeader | None) -> None:
        self.leader = leader
        self._bound.set()

    def __call__(self) -> None:
        assert self._bound.wait(TEST_WAIT_SECONDS), "the handle was never bound"
        assert self.leader is not None
        self.observed.append((self.leader.state, self.leader.returncode))
        self.seen.set()
        if self._failure is not None:
            raise self._failure


@dataclass
class Started:
    leader: AnchoredLeader
    owned: OwnedChild
    probe: ExitProbe
    stdin: int

    def write_exit_line(self, code: int) -> None:
        os.write(self.stdin, f"{code}\n".encode())


class Leaders:
    def __init__(self, tmp_path: Path, cleanup: ChildCleanup) -> None:
        self._tmp_path = tmp_path
        self._cleanup = cleanup

    def describe(self, binary: Path | None = None) -> ChildProcess:
        return ChildProcess(
            binary=binary or Path(sys.executable),
            arguments=("-I", "-c", EXIT_WITH_LINE),
            environment=closed_environment(self._tmp_path),
            working_directory=self._tmp_path,
        )

    def start(self, probe: ExitProbe | None = None) -> Started:
        probe = probe or ExitProbe()
        reader, writer = os.pipe()
        self._cleanup.close_at_cleanup(writer)
        try:
            captured = self._cleanup.capture(
                lambda: spawn.start_anchored_leader(
                    self.describe(),
                    stdin=reader,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    on_exit=probe,
                )
            )
        finally:
            os.close(reader)
        (owned,) = captured.children
        probe.bind(captured.value)
        return Started(captured.value, owned, probe, writer)

    def start_bare(self) -> OwnedChild:
        reader, writer = os.pipe()
        self._cleanup.close_at_cleanup(writer)
        try:
            return self._cleanup.start_bare_child(
                self.describe(),
                stdin=reader,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            os.close(reader)

    def watcher_of(self, started: Started) -> threading.Thread | None:
        name = f"{WATCHER_THREAD_PREFIX}{started.owned.pid}"
        return next((t for t in threading.enumerate() if t.name == name), None)

    def await_exit_seen(self, started: Started) -> None:
        assert started.probe.seen.wait(TEST_WAIT_SECONDS), "on_exit was not called within the bound"
        self.join_watcher(started)

    def join_watcher(self, started: Started) -> None:
        watcher = self.watcher_of(started)
        if watcher is not None:
            watcher.join(TEST_WAIT_SECONDS)
            assert not watcher.is_alive(), "the watcher did not end within the bound"


@pytest.fixture
def leaders(tmp_path: Path, child_cleanup: ChildCleanup) -> Leaders:
    return Leaders(tmp_path, child_cleanup)


@pytest.fixture
def thread_failures(monkeypatch: pytest.MonkeyPatch) -> list[threading.ExceptHookArgs]:
    forwarded: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", forwarded.append)
    return forwarded


def spawn_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == SPAWN_LOGGER and record.levelno == logging.WARNING
    ]


def process_status(pid: int) -> tuple[str, int, int]:
    stat = Path(f"/proc/{pid}/stat").read_text()
    state, _parent, group, session = stat.rpartition(")")[2].split()[:4]
    return state, int(group), int(session)


def await_stopped(pid: int) -> None:
    deadline = time.monotonic() + TEST_WAIT_SECONDS
    while process_status(pid)[0] != "T":
        assert time.monotonic() < deadline, f"pid {pid} did not stop within the bound"


def test_an_exited_leader_stays_an_unreaped_anchor_of_this_process(
    leaders: Leaders, child_cleanup: ChildCleanup
) -> None:
    started = leaders.start()

    started.write_exit_line(3)
    leaders.await_exit_seen(started)

    pid = started.owned.pid
    assert started.probe.observed == [(AnchorState.EXITED, 3)]
    assert child_cleanup.state(started.owned) == Exited(3)
    assert process_status(pid) == ("Z", pid, pid)
    os.killpg(pid, 0)
    assert started.owned.popen.returncode is None
    assert started.leader.done_with_pid is False


def test_a_missing_binary_raises_unchanged_and_starts_no_watcher(
    tmp_path: Path, leaders: Leaders
) -> None:
    threads_before = set(threading.enumerate())
    descriptors_before = set(os.listdir("/proc/self/fd"))
    missing = leaders.describe(tmp_path / "missing-binary")
    on_exit = ExitProbe()

    with pytest.raises(FileNotFoundError):
        spawn.start_anchored_leader(
            missing,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            on_exit=on_exit,
        )

    assert set(threading.enumerate()) == threads_before
    assert set(os.listdir("/proc/self/fd")) == descriptors_before


def test_a_raising_on_exit_is_logged_by_pid_and_class_and_stays_off_the_excepthook(
    leaders: Leaders,
    thread_failures: list[threading.ExceptHookArgs],
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = leaders.start(ExitProbe(failure=RuntimeError("a message that stays out of logs")))

    with caplog.at_level(logging.WARNING, logger=SPAWN_LOGGER):
        started.write_exit_line(3)
        leaders.await_exit_seen(started)

    [warning] = spawn_warnings(caplog)
    assert str(started.owned.pid) in warning
    assert "RuntimeError" in warning
    assert "stays out of logs" not in warning
    assert (started.leader.state, started.leader.returncode) == (AnchorState.EXITED, 3)
    assert thread_failures == []


def test_a_stop_report_is_consumed_by_a_call_that_cannot_reap(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    model = KernelModel(stops=2)
    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger=SPAWN_LOGGER):
        patch.setattr(os, "waitid", model)
        started = leaders.start()
        model.await_calls(5)

        assert [call.options for call in model.calls] == [
            OBSERVE_EXIT,
            CONSUME_STOP_REPORT,
            OBSERVE_EXIT,
            CONSUME_STOP_REPORT,
            OBSERVE_EXIT,
        ]
        assert started.leader.state is AnchorState.RUNNING
        assert started.probe.observed == []
        assert len(spawn_warnings(caplog)) == 1

        started.write_exit_line(0)
        leaders.await_exit_seen(started)

    assert model.violations == []
    assert started.probe.observed == [(AnchorState.EXITED, 0)]
    assert child_cleanup.state(started.owned) == Exited(0)


def test_stopped_leaders_are_consumed_for_their_own_pid_only(
    leaders: Leaders, child_cleanup: ChildCleanup
) -> None:
    first, second = leaders.start(), leaders.start()
    for started in (first, second):
        child_cleanup.send_signal(started.owned, signal.SIGSTOP)
        await_stopped(started.owned.pid)

    report = (os.CLD_STOPPED, signal.SIGSTOP)
    assert spawn._consume_stop_report(first.owned.pid) == report
    assert spawn._consume_stop_report(first.owned.pid) is None
    assert spawn._consume_stop_report(second.owned.pid) == report
    assert [s.leader.state for s in (first, second)] == [AnchorState.RUNNING] * 2

    for started in (first, second):
        child_cleanup.send_signal(started.owned, signal.SIGCONT)
        started.write_exit_line(0)
        leaders.await_exit_seen(started)
        assert (started.leader.state, started.leader.returncode) == (AnchorState.EXITED, 0)


@pytest.mark.parametrize(
    ("stops", "loses_at", "calls_before_the_loss"),
    [
        pytest.param(0, "wait", 1, id="the-observation-wait-reports-the-loss"),
        pytest.param(1, "consume", 2, id="the-stop-consumer-reports-the-loss"),
    ],
)
def test_a_loss_the_kernel_reports_leaves_the_real_zombie_intact_and_disarms_popen(
    stops: int,
    loses_at: str,
    calls_before_the_loss: int,
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = KernelModel(stops=stops, loses_at=loses_at)
    with monkeypatch.context() as patch:
        patch.setattr(os, "waitid", model)
        started = leaders.start()
        started.write_exit_line(0)
        child_cleanup.wait_exited(started.owned)
        leaders.await_exit_seen(started)

    assert started.leader.state is AnchorState.LOST
    assert started.probe.observed == [(AnchorState.LOST, None)]
    assert started.leader.returncode is None
    disarmed = started.owned.popen.returncode
    assert disarmed is not None
    assert not isinstance(disarmed, int)
    assert started.leader.done_with_pid is True
    assert len(model.calls) == calls_before_the_loss
    assert model.violations == []
    assert thread_failures == []
    assert child_cleanup.state(started.owned) == Exited(0)

    owned = started.owned
    dropped = child_cleanup.drop_popen(owned)
    probe = started.probe
    probe.bind(None)
    del started
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        gc.collect()
        leaders.start_bare()

    assert dropped() is None
    assert [str(w.message) for w in emitted if issubclass(w.category, ResourceWarning)] == []
    assert child_cleanup.state(owned) == Exited(0)


@pytest.mark.parametrize("watcher_held_behind_a_hold", [False, True], ids=["running", "held"])
def test_the_owner_keeps_the_zombie_of_a_leader_whose_handle_is_not_done(
    watcher_held_behind_a_hold: bool,
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = PassThroughKernel(hold_before_call=1 if watcher_held_behind_a_hold else None)
    with monkeypatch.context() as patch:
        patch.setattr(os, "waitid", kernel)
        started = leaders.start()
        try:
            with pytest.raises(AssertionError):
                raise AssertionError("an expectation of the cell failed")
            child_cleanup.cleanup()

            assert child_cleanup.state(started.owned) == Exited(SIGKILLED)
        finally:
            kernel.open_hold()
        leaders.await_exit_seen(started)

    watcher = f"{WATCHER_THREAD_PREFIX}{started.owned.pid}"
    assert [(call.thread, call.options) for call in kernel.calls] == [(watcher, OBSERVE_EXIT)]
    assert kernel.violations == []
    assert started.probe.observed == [(AnchorState.EXITED, SIGKILLED)]
    assert started.leader.returncode == SIGKILLED
    assert started.owned.popen.returncode is None
    assert child_cleanup.state(started.owned) == Exited(SIGKILLED)
    assert started.leader.done_with_pid is False
    assert thread_failures == []
