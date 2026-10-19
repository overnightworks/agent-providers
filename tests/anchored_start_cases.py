"""S6: a start whose watcher launch raises leaves no child, and no watcher touches the pid.

The module name matches no ``python_files`` pattern, so ``pytest tests`` never collects it. Only
``test_anchored_start`` names it, one row at a time, on the command line of a disposable
interpreter. Running it directly would run S6 in that pytest's own process, where a failed
cleanup leaves threads and patches behind.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

import pytest
from anchored_kernel import (
    WATCHER_THREAD_PREFIX,
    GroupSignalCall,
    GroupSignalModel,
    PassThroughKernel,
    WaitidCall,
    WaitpidAnswer,
    install_kernel,
)
from anchored_leaders import ExitProbe, InstrumentedLock, Leaders, spawn_warnings
from anchored_support import GONE, ChildCleanup, OwnedChild, kill_children
from bounds import TEST_WAIT_SECONDS

from agent_providers import spawn
from agent_providers.spawn import ChildIdentityLost

START_WORKER_NAME = "bounded-start"
STOP_STATUS = 0x57F
KILLED = -9


class InjectedInterruption(BaseException):
    """Skips ``Thread.start``'s ``except Exception``, so only the watcher leaves ``_limbo``."""


class Bindable(Protocol):
    def bind(self, leader_pid: int) -> None: ...


class RecordingGate(InstrumentedLock):
    def __init__(self) -> None:
        super().__init__()
        self.released_by: list[str] = []

    def release(self) -> None:
        self.released_by.append(threading.current_thread().name)
        super().release()


class StopReportingKernel(PassThroughKernel):
    """Answers the first reap of the bound leader with a stop report naming that leader."""

    def bind(self, leader_pid: int) -> None:
        super().bind(leader_pid)
        self.answer_next_waitpid((leader_pid, STOP_STATUS))


class GateFactory:
    def __init__(self) -> None:
        self.created: list[RecordingGate] = []

    def __call__(self) -> RecordingGate:
        gate = RecordingGate()
        self.created.append(gate)
        return gate

    @property
    def gate(self) -> RecordingGate:
        (only,) = self.created
        return only


class BoundedLifecycleLock:
    """A lifecycle lock whose blocking acquire fails by name instead of waiting past the bound."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, blocking: bool = True) -> bool:
        if not blocking:
            return self._lock.acquire(blocking=False)
        if not self._lock.acquire(timeout=TEST_WAIT_SECONDS):
            raise AssertionError(
                f"{threading.current_thread().name} waited for the lifecycle lock past the bound"
            )
        return True

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exception: object) -> None:
        self.release()


class LaunchSeam:
    """The native launch ``Thread.start`` reads at call time, answered for the watcher only."""

    def __init__(self, models: Iterable[Bindable]) -> None:
        self._models = tuple(models)
        self._answer: Callable[[threading.Thread, Callable[[], object]], None] | None = None
        self.launches: list[threading.Thread] = []

    def refuse(self, error: BaseException) -> None:
        def answer(watcher: threading.Thread, launch: Callable[[], object]) -> None:
            raise error

        self._answer = answer

    def raise_after_launch(self, error: BaseException, before_raise: Callable[[], None]) -> None:
        def answer(watcher: threading.Thread, launch: Callable[[], object]) -> None:
            launch()
            self.launches.append(watcher)
            before_raise()
            raise error

        self._answer = answer

    def install(self, patch: pytest.MonkeyPatch) -> None:
        launcher = (
            "_start_joinable_thread"
            if hasattr(threading, "_start_joinable_thread")
            else "_start_new_thread"
        )
        real_launch = getattr(threading, launcher)

        def launch(bootstrap, *args, **kwargs):
            watcher = bootstrap.__self__
            if not watcher.name.startswith(WATCHER_THREAD_PREFIX):
                return real_launch(bootstrap, *args, **kwargs)
            assert self._answer is not None, "the seam was given no answer"
            leader_pid = int(watcher.name.removeprefix(WATCHER_THREAD_PREFIX))
            for model in self._models:
                model.bind(leader_pid)
            return self._answer(watcher, lambda: real_launch(bootstrap, *args, **kwargs))

        patch.setattr(threading, launcher, launch)

    @staticmethod
    def await_ready(watcher: threading.Thread) -> None:
        assert watcher._started.wait(TEST_WAIT_SECONDS), f"{watcher.name} was not ready in time"

    def unfinished_launches(self) -> list[threading.Thread]:
        return [
            watcher
            for watcher in self.launches
            if not watcher._started.is_set() or watcher.is_alive()
        ]


class BoundedStartup:
    """One start in a named daemon worker, with a cleanup that names every failure it finds."""

    def __init__(
        self,
        leaders: Leaders,
        child_cleanup: ChildCleanup,
        seam: LaunchSeam,
        gates: GateFactory,
        probe: ExitProbe,
    ) -> None:
        self._leaders = leaders
        self._child_cleanup = child_cleanup
        self._seam = seam
        self._gates = gates
        self._probe = probe
        self._listed_before = child_cleanup.listed()
        self._started_leader: object | None = None
        self._failure: BaseException | None = None
        self._worker = threading.Thread(target=self._run, name=START_WORKER_NAME, daemon=True)

    def __enter__(self) -> BoundedStartup:
        self._reader, writer = os.pipe()
        self._child_cleanup.close_at_cleanup(writer)
        self._worker.start()
        return self

    def _run(self) -> None:
        try:
            captured = self._child_cleanup.capture(
                lambda: spawn.start_anchored_leader(
                    self._leaders.describe(),
                    stdin=self._reader,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    on_exit=self._probe,
                )
            )
            self._started_leader = captured.value
        except BaseException as failure:
            self._failure = failure
        finally:
            os.close(self._reader)

    def outcome(self) -> BaseException:
        self._worker.join(TEST_WAIT_SECONDS)
        assert not self._worker.is_alive(), "the start did not finish within the bound"
        assert self._failure is not None, "the start returned a handle instead of raising"
        return self._failure

    @property
    def child(self) -> OwnedChild:
        (only,) = self._attempt_children()
        return only

    def _attempt_children(self) -> list[OwnedChild]:
        return [
            owned
            for owned in self._child_cleanup.listed()
            if all(owned is not before for before in self._listed_before)
        ]

    def __exit__(self, exception_type, exception, traceback) -> None:
        failures: list[str] = []
        self._kill_what_may_still_run(exception is not None, failures)
        self._open_the_gate_the_start_left_shut(failures)
        self._join_the_worker(failures)
        self._join_the_launches(failures)
        if failures:
            raise AssertionError("; ".join(failures))

    def _kill_what_may_still_run(self, block_raised: bool, failures: list[str]) -> None:
        if block_raised or self._worker.is_alive() or self._seam.unfinished_launches():
            failures.extend(
                f"pid {error.pid}: {error.kind.value}"
                for error in kill_children(self._attempt_children())
            )

    def _open_the_gate_the_start_left_shut(self, failures: list[str]) -> None:
        if not self._seam.launches:
            return
        for gate in self._gates.created:
            if not gate.released_by:
                failures.append("the start left its watcher undecided")
                try:
                    gate.release()
                except RuntimeError as decided_meanwhile:
                    failures.append(
                        f"the gate was decided while the exit opened it: {decided_meanwhile}"
                    )

    def _join_the_worker(self, failures: list[str]) -> None:
        self._worker.join(TEST_WAIT_SECONDS)
        if self._worker.is_alive():
            failures.append("the start worker was still alive at the bound")
        if isinstance(self._failure, AssertionError):
            failures.append(str(self._failure))

    def _join_the_launches(self, failures: list[str]) -> None:
        for watcher in self._seam.launches:
            try:
                self._seam.await_ready(watcher)
            except AssertionError as not_ready:
                failures.append(str(not_ready))
                continue
            watcher.join(TEST_WAIT_SECONDS)
            if watcher.is_alive():
                failures.append(f"{watcher.name} was still alive at the bound")


@dataclass
class StartCell:
    leaders: Leaders
    child_cleanup: ChildCleanup
    seam: LaunchSeam
    gates: GateFactory
    kernel: PassThroughKernel
    killer: GroupSignalModel
    probe: ExitProbe

    def start(self) -> BoundedStartup:
        return BoundedStartup(self.leaders, self.child_cleanup, self.seam, self.gates, self.probe)

    @property
    def gate(self) -> RecordingGate:
        return self.gates.gate

    def rolled_back_by_the_start(self, startup: BoundedStartup, reaps: int) -> None:
        pid = startup.child.pid
        assert self.gate.released_by == [START_WORKER_NAME]
        assert self.killer.calls == [GroupSignalCall(START_WORKER_NAME, pid, signal.SIGKILL)]
        assert self.kernel.calls == [
            WaitidCall(START_WORKER_NAME, pid, 0, "waitpid") for _ in range(reaps)
        ]

    def left_no_trace(self, caplog: pytest.LogCaptureFixture) -> None:
        assert self.kernel.violations == []
        assert self.killer.violations == []
        assert not self.probe.seen.is_set()
        assert spawn_warnings(caplog) == []


def assert_abandoned_before_the_kill(gates: GateFactory, call: GroupSignalCall) -> None:
    assert gates.gate.released_by == [call.thread], "the kill came before the decision"


@pytest.fixture
def start_cell(
    monkeypatch: pytest.MonkeyPatch, leaders: Leaders, child_cleanup: ChildCleanup
) -> Callable[..., StartCell]:
    def build(
        *,
        waitpid_answer: WaitpidAnswer | None = None,
        stop_report: bool = False,
        kill_failure: OSError | None = None,
    ) -> StartCell:
        kernel_type = StopReportingKernel if stop_report else PassThroughKernel
        kernel = kernel_type(reap_options=0, waitpid_answer=waitpid_answer)
        gates = GateFactory()
        killer = GroupSignalModel(
            failure=kill_failure, during=lambda call: assert_abandoned_before_the_kill(gates, call)
        )
        cell = StartCell(
            leaders, child_cleanup, LaunchSeam([kernel, killer]), gates, kernel, killer, ExitProbe()
        )
        install_kernel(monkeypatch, kernel)
        killer.install(monkeypatch)
        monkeypatch.setattr(spawn, "_ownership_gate", gates)
        monkeypatch.setattr(spawn, "_lifecycle_lock", BoundedLifecycleLock)
        cell.seam.install(monkeypatch)
        return cell

    return build


def test_a_refused_watcher_launch_leaves_no_child(
    start_cell, thread_failures, caplog: pytest.LogCaptureFixture
) -> None:
    cell = start_cell()
    refusal = RuntimeError("can't start new thread")
    cell.seam.refuse(refusal)

    with cell.start() as startup:
        assert startup.outcome() is refusal
        cell.rolled_back_by_the_start(startup, reaps=1)
        assert cell.seam.launches == []
        assert cell.gate.waiting == []
        assert not [t for t in threading.enumerate() if t.name.startswith(WATCHER_THREAD_PREFIX)]
        assert cell.child_cleanup.state(startup.child) is GONE
        assert startup.child.popen.returncode == KILLED

    cell.left_no_trace(caplog)
    assert thread_failures == []


def test_a_watcher_launched_before_the_start_raised_never_touches_the_pid(
    start_cell, thread_failures, caplog: pytest.LogCaptureFixture
) -> None:
    cell = start_cell()
    interruption = InjectedInterruption()
    cell.seam.raise_after_launch(interruption, before_raise=lambda: cell.gate.await_waiting(1))

    with cell.start() as startup:
        assert startup.outcome() is interruption
        (watcher,) = cell.seam.launches
        assert watcher.name == f"{WATCHER_THREAD_PREFIX}{startup.child.pid}"
        assert cell.gate.waiting == [watcher.name]
        cell.seam.await_ready(watcher)
        watcher.join(TEST_WAIT_SECONDS)
        assert not watcher.is_alive()
        cell.rolled_back_by_the_start(startup, reaps=1)
        assert cell.child_cleanup.state(startup.child) is GONE
        assert startup.child.popen.returncode == KILLED

    cell.left_no_trace(caplog)
    assert thread_failures == []


LOSSES = {
    "kill-meets-esrch": {"kill_failure": ProcessLookupError()},
    "reap-meets-echild": {"waitpid_answer": ChildProcessError("scripted loss")},
}


@pytest.mark.parametrize("losses", LOSSES.values(), ids=LOSSES.keys())
def test_a_loss_during_the_rollback_raises_identity_lost_from_the_launch_error(
    start_cell, thread_failures, caplog: pytest.LogCaptureFixture, losses: dict
) -> None:
    cell = start_cell(**losses)
    refusal = RuntimeError("can't start new thread")
    cell.seam.refuse(refusal)

    with cell.start() as startup:
        lost = startup.outcome()
        assert isinstance(lost, ChildIdentityLost)
        assert lost.__cause__ is refusal
        reaps = 0 if "kill_failure" in losses else 1
        cell.rolled_back_by_the_start(startup, reaps=reaps)
        popen_returncode = startup.child.popen.returncode
        assert popen_returncode is not None
        assert not isinstance(popen_returncode, int)

    cell.left_no_trace(caplog)
    assert thread_failures == []


def test_a_stop_report_at_the_rollback_reap_is_followed_by_the_exit_report(
    start_cell, thread_failures, caplog: pytest.LogCaptureFixture
) -> None:
    cell = start_cell(stop_report=True)
    refusal = RuntimeError("can't start new thread")
    cell.seam.refuse(refusal)

    with cell.start() as startup:
        assert startup.outcome() is refusal
        cell.rolled_back_by_the_start(startup, reaps=2)
        assert cell.child_cleanup.state(startup.child) is GONE
        assert startup.child.popen.returncode == KILLED

    cell.left_no_trace(caplog)
    assert thread_failures == []
