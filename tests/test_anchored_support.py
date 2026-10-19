"""The test child owner names every child by a pidfd, kills first, never reaps behind a handle."""

from __future__ import annotations

import errno
import fcntl
import gc
import os
import re
import signal
import subprocess
import sys
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import anchored_support
import pytest
from anchored_kernel import watcher_thread
from anchored_support import (
    GONE,
    RUNNING,
    SIGKILLED,
    ChildCleanup,
    ChildErrorKind,
    CleanupFailed,
    Exited,
    FakeHandle,
    IdentityUnavailable,
    OwnedChild,
    ZombieAnchor,
    child_cleanup_scope,
    popen_bare,
    state,
)
from bounds import TEST_WAIT_SECONDS
from test_zombie_anchor import EXIT_WITH_LINE

from agent_providers.spawn import ChildProcess, closed_environment

SHORT_BOUND = 0.2


@dataclass(frozen=True)
class ScriptChild:
    owned: OwnedChild
    stdin: int


class ScriptChildren:
    """Starts children that exit with the integer line written to their stdin, or 0 on EOF."""

    def __init__(self, tmp_path: Path, cleanup: ChildCleanup) -> None:
        self._tmp_path = tmp_path
        self._cleanup = cleanup

    def _describe(self, binary: Path | None = None) -> ChildProcess:
        return ChildProcess(
            binary=binary or Path(sys.executable),
            arguments=("-I", "-c", EXIT_WITH_LINE),
            environment=closed_environment(self._tmp_path),
            working_directory=self._tmp_path,
        )

    def _open_stdin(self) -> tuple[int, int]:
        reader, writer = os.pipe()
        self._cleanup.close_at_cleanup(writer)
        return reader, writer

    def start(self, binary: Path | None = None) -> ScriptChild:
        reader, writer = self._open_stdin()
        try:
            owned = self._cleanup.start_bare_child(
                self._describe(binary),
                stdin=reader,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            os.close(reader)
        return ScriptChild(owned, writer)

    def start_then(self, outcome: Callable[[], object]) -> Callable[[], object]:
        def create_a_child_then_report() -> object:
            reader, _ = self._open_stdin()
            try:
                popen_bare(
                    self._describe(),
                    stdin=reader,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            finally:
                os.close(reader)
            return outcome()

        return create_a_child_then_report


@pytest.fixture
def scripts(tmp_path: Path, child_cleanup: ChildCleanup) -> ScriptChildren:
    return ScriptChildren(tmp_path, child_cleanup)


def open_descriptors() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def refuse_with(code: int) -> Callable[..., None]:
    def refuse(*_args) -> None:
        raise OSError(code, os.strerror(code))

    return refuse


def raise_start_failure() -> None:
    raise RuntimeError("start failed after creating its child")


def error_kinds(failure: pytest.ExceptionInfo[CleanupFailed]) -> list[ChildErrorKind]:
    return [error.kind for error in failure.value.errors]


def test_capture_names_the_process_it_created(
    child_cleanup: ChildCleanup, scripts: ScriptChildren
) -> None:
    started = scripts.start()

    assert child_cleanup.state(started.owned) is RUNNING
    assert fcntl.fcntl(started.owned.pidfd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    os.write(started.stdin, b"0\n")
    assert child_cleanup.wait_exited(started.owned) == 0
    assert child_cleanup.state(started.owned) == Exited(0)


def test_a_creation_that_fails_lists_and_opens_nothing(
    child_cleanup: ChildCleanup, scripts: ScriptChildren, tmp_path: Path
) -> None:
    before = open_descriptors()

    with pytest.raises(FileNotFoundError):
        scripts.start(binary=tmp_path / "missing")
    child_cleanup.cleanup()

    assert open_descriptors() == before
    assert child_cleanup.listed() == ()


def test_cleanup_after_a_failed_assertion_ends_every_child_and_closes_registered_ends(
    child_cleanup: ChildCleanup, scripts: ScriptChildren
) -> None:
    running = scripts.start().owned
    exited_started = scripts.start()
    os.write(exited_started.stdin, b"3\n")
    assert child_cleanup.wait_exited(exited_started.owned) == 3
    stopped = scripts.start().owned
    child_cleanup.send_signal(stopped, signal.SIGSTOP)
    kept_read_end, registered_write_end = os.pipe()
    child_cleanup.close_at_cleanup(registered_write_end)
    os.set_blocking(kept_read_end, False)
    try:
        with pytest.raises(AssertionError):
            assert child_cleanup.state(running) is GONE

        child_cleanup.cleanup()
        returncodes = [o.popen.returncode for o in (running, exited_started.owned, stopped)]
        child_cleanup.cleanup()

        assert returncodes == [SIGKILLED, 3, SIGKILLED]
        assert [o.popen.returncode for o in (running, exited_started.owned, stopped)] == returncodes
        assert os.read(kept_read_end, 1) == b""
    finally:
        os.close(kept_read_end)


def test_a_child_not_observed_dead_at_the_bound_is_reported_and_collected_by_the_next_cleanup(
    child_cleanup: ChildCleanup, scripts: ScriptChildren, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = scripts.start().owned

    with monkeypatch.context() as unobserved:
        unobserved.setattr(anchored_support, "wait_for_exits", lambda _pidfds, _timeout: set())
        with pytest.raises(CleanupFailed) as failure:
            child_cleanup.cleanup(bound=SHORT_BOUND)

    assert error_kinds(failure) == [ChildErrorKind.NOT_DEAD_AT_THE_BOUND]
    assert failure.value.errors[0].pid == owned.pid
    assert owned.popen.returncode is None
    assert child_cleanup.wait_exited(owned) == SIGKILLED
    child_cleanup.cleanup()
    assert owned.popen.returncode == SIGKILLED


def test_a_refused_kill_is_reported_even_though_the_child_is_still_stopped_and_retried(
    child_cleanup: ChildCleanup, scripts: ScriptChildren, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = scripts.start().owned
    child_cleanup.send_signal(owned, signal.SIGSTOP)

    with monkeypatch.context() as refused:
        refused.setattr(anchored_support, "pidfd_send_signal", refuse_with(errno.EPERM))
        with pytest.raises(CleanupFailed) as failure:
            child_cleanup.cleanup(bound=SHORT_BOUND)

    assert error_kinds(failure) == [
        ChildErrorKind.KILL_REFUSED,
        ChildErrorKind.NOT_DEAD_AT_THE_BOUND,
    ]
    assert {error.pid for error in failure.value.errors} == {owned.pid}
    assert child_cleanup.state(owned) is RUNNING
    assert owned.popen.returncode is None
    child_cleanup.cleanup()
    assert owned.popen.returncode == SIGKILLED


def test_drop_popen_refuses_an_armed_popen_and_cleanup_still_reaps_it(
    child_cleanup: ChildCleanup, scripts: ScriptChildren
) -> None:
    started = scripts.start()
    os.write(started.stdin, b"3\n")
    assert child_cleanup.wait_exited(started.owned) == 3

    with pytest.raises(AssertionError):
        child_cleanup.drop_popen(started.owned)
    child_cleanup.cleanup()

    assert started.owned.popen.returncode == 3


def test_drop_popen_hands_over_a_disarmed_popen_and_keeps_the_process(
    child_cleanup: ChildCleanup, scripts: ScriptChildren
) -> None:
    started = scripts.start()
    os.write(started.stdin, b"3\n")
    assert child_cleanup.wait_exited(started.owned) == 3
    started.owned.popen.returncode = object()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        dropped = child_cleanup.drop_popen(started.owned)
        gc.collect()
        scripts.start()
        gc.collect()

    assert dropped() is None
    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == []
    assert child_cleanup.state(started.owned) == Exited(3)
    child_cleanup.cleanup()
    assert started.owned.resolved


@pytest.mark.parametrize(
    ("binding", "code", "step"),
    [
        ("pidfd_open", errno.ENOSYS, "pidfd_open"),
        ("pidfd_send_signal", errno.EPERM, "pidfd_send_signal"),
        ("_waitid", errno.EINVAL, "waitid"),
    ],
)
def test_a_refused_pidfd_call_fails_the_construction_before_any_child_exists(
    zombie_anchor: ZombieAnchor,
    monkeypatch: pytest.MonkeyPatch,
    binding: str,
    code: int,
    step: str,
) -> None:
    before = open_descriptors()
    monkeypatch.setattr(anchored_support, binding, refuse_with(code))

    with pytest.raises(IdentityUnavailable, match=step):
        ChildCleanup(zombie_anchor)

    assert open_descriptors() == before


def test_a_pidfd_refused_after_the_child_exists_is_reported_and_the_child_ends_by_its_stdin(
    child_cleanup: ChildCleanup, scripts: ScriptChildren, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(anchored_support, "pidfd_open", refuse_with(errno.EMFILE))

    with pytest.raises(IdentityUnavailable) as refusal:
        scripts.start()
    monkeypatch.undo()
    popen = refusal.value.popen

    assert re.search(rf"\bpid {popen.pid}:", str(refusal.value))
    assert popen.poll() is None
    assert child_cleanup.listed() == ()
    child_cleanup.cleanup()
    assert popen.wait(timeout=TEST_WAIT_SECONDS) == 0


def test_a_dead_child_whose_handle_holds_the_number_stays_an_unreaped_zombie_until_it_certifies(
    child_cleanup: ChildCleanup, scripts: ScriptChildren
) -> None:
    handle = FakeHandle(done_with_pid=False)
    captured = child_cleanup.capture(scripts.start_then(lambda: handle))
    (owned,) = captured.children

    child_cleanup.cleanup()

    assert child_cleanup.state(owned) == Exited(SIGKILLED)
    assert owned.popen.returncode is None
    handle.done_with_pid = True
    child_cleanup.cleanup()
    assert owned.popen.returncode == SIGKILLED
    assert owned.resolved


def test_a_dead_child_whose_handle_is_unreachable_is_reaped_at_once(
    child_cleanup: ChildCleanup, scripts: ScriptChildren
) -> None:
    captured = child_cleanup.capture(scripts.start_then(FakeHandle))
    (owned,) = captured.children
    del captured
    gc.collect()

    child_cleanup.cleanup()

    assert owned.popen.returncode == SIGKILLED
    assert owned.resolved


def test_the_hand_over_keeps_a_deferred_child_until_its_handle_lets_go(
    tmp_path: Path, zombie_anchor: ZombieAnchor
) -> None:
    anchor = ZombieAnchor()
    try:
        with child_cleanup_scope(anchor) as cleanup:
            scripts = ScriptChildren(tmp_path, cleanup)
            failing_start = scripts.start_then(raise_start_failure)
            with pytest.raises(RuntimeError):
                cleanup.capture(failing_start)
            captured = cleanup.capture(scripts.start_then(lambda: FakeHandle(done_with_pid=False)))
            (unreported,) = cleanup.listed()[:1]
            (reported,) = captured.children

            cleanup.cleanup()
            assert [state(o) for o in (unreported, reported)] == [Exited(SIGKILLED)] * 2
            assert [o.popen.returncode for o in (unreported, reported)] == [None, None]

            cleanup.hand_over()
            assert cleanup.listed() == ()
            with pytest.raises(AssertionError):
                cleanup.state(unreported)
            cleanup.cleanup()
            assert [state(o) for o in (unreported, reported)] == [Exited(SIGKILLED)] * 2

            del captured
            gc.collect()
            anchor.finalize()

            assert [o.popen.returncode for o in (unreported, reported)] == [SIGKILLED] * 2
    finally:
        anchor.hand_over(zombie_anchor)


def test_leaving_the_cleanup_scope_ends_children_closes_descriptors_and_defers_to_the_anchor(
    tmp_path: Path, zombie_anchor: ZombieAnchor
) -> None:
    anchor = ZombieAnchor()
    try:
        before = open_descriptors()
        with child_cleanup_scope(anchor) as cleanup:
            scripts = ScriptChildren(tmp_path, cleanup)
            running = scripts.start()
            captured = cleanup.capture(scripts.start_then(lambda: FakeHandle(done_with_pid=False)))
            (deferred,) = captured.children

        assert running.owned.resolved
        assert running.owned.popen.returncode == SIGKILLED
        assert cleanup.listed() == ()
        assert state(deferred) == Exited(SIGKILLED)
        assert deferred.popen.returncode is None
        assert open_descriptors() == before | {str(deferred.pidfd)}
        del captured
        gc.collect()
        anchor.finalize()
        assert deferred.popen.returncode == SIGKILLED
    finally:
        anchor.hand_over(zombie_anchor)


def test_leaving_the_cleanup_scope_by_an_exception_still_ends_children_and_closes_descriptors(
    tmp_path: Path, zombie_anchor: ZombieAnchor
) -> None:
    class TestBodyFailed(Exception):
        pass

    started = SimpleNamespace()

    def leave_scope_by_exception() -> None:
        with child_cleanup_scope(anchor) as started.cleanup:
            scripts = ScriptChildren(tmp_path, started.cleanup)
            started.running = scripts.start()
            deferring_start = scripts.start_then(lambda: FakeHandle(done_with_pid=False))
            started.captured = started.cleanup.capture(deferring_start)
            raise TestBodyFailed

    anchor = ZombieAnchor()
    try:
        before = open_descriptors()
        with pytest.raises(TestBodyFailed):
            leave_scope_by_exception()
        running, captured, cleanup = (
            vars(started).pop(name) for name in ("running", "captured", "cleanup")
        )
        (deferred,) = captured.children

        assert running.owned.resolved
        assert running.owned.popen.returncode == SIGKILLED
        assert cleanup.listed() == ()
        assert state(deferred) == Exited(SIGKILLED)
        assert open_descriptors() == before | {str(deferred.pidfd)}
        del captured
        gc.collect()
        anchor.finalize()
        assert deferred.popen.returncode == SIGKILLED
    finally:
        anchor.hand_over(zombie_anchor)


def test_wait_stopped_rejects_an_exit_report_and_leaves_the_child_unreaped(
    child_cleanup: ChildCleanup, scripts: ScriptChildren
) -> None:
    started = scripts.start()
    os.write(started.stdin, b"3\n")
    assert child_cleanup.wait_exited(started.owned) == 3

    with pytest.raises(AssertionError):
        child_cleanup.wait_stopped(started.owned)

    assert child_cleanup.state(started.owned) == Exited(3)
    assert child_cleanup.wait_exited(started.owned) == 3


def test_watcher_thread_fails_loudly_when_no_watcher_exists() -> None:
    pid_without_a_watcher = os.getpid()

    with pytest.raises(AssertionError):
        watcher_thread(pid_without_a_watcher)
