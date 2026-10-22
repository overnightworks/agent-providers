"""Releasing an anchored leader reaps a published exit once and never a running leader."""

from __future__ import annotations

import gc
import logging
import os
import signal
import subprocess
import threading
import warnings
import weakref
from collections.abc import Callable

import pytest
from anchored_kernel import (
    CONSUME_STOP_REPORT,
    OBSERVE_EXIT,
    WATCHER_THREAD_PREFIX,
    KernelModel,
    PassThroughKernel,
    RecordingKernel,
    WaitidCall,
    install_kernel,
    join_watcher,
)
from anchored_leaders import (
    SPAWN_LOGGER,
    ExitProbe,
    InstrumentedLock,
    Leaders,
    Releaser,
    Started,
    spawn_warnings,
    start_bound,
)
from anchored_support import GONE, ChildCleanup, Exited, OwnedChild
from bounds import TEST_WAIT_SECONDS
from provider_test_support import call_within_deadline

from agent_runtime import spawn
from agent_runtime.spawn import AnchorState, AnchorStateError, ChildIdentityLost

EXIT_CODE = 3


def release_within_bound(started: Started) -> None:
    call_within_deadline(started.leader.release, seconds=TEST_WAIT_SECONDS)


def names_of(calls: list[WaitidCall]) -> list[str]:
    return [call.name for call in calls]


def assert_a_destroyed_handle_reaps_nothing(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    owned: OwnedChild,
    probe: ExitProbe,
    dropped: weakref.ReferenceType[subprocess.Popen],
    expected_state: object,
) -> None:
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        probe.bind(None)
        gc.collect()
        leaders.start_bare()

    assert dropped() is None
    assert [str(w.message) for w in emitted if issubclass(w.category, ResourceWarning)] == []
    assert child_cleanup.state(owned) == expected_state


def test_a_release_reaps_the_published_exit_once_and_a_dropped_handle_reaps_nothing(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = PassThroughKernel()
    with monkeypatch.context() as patch:
        install_kernel(patch, kernel)
        started = start_bound(leaders, kernel)
        started.write_exit_line(EXIT_CODE)
        leaders.await_exit_seen(started)

        release_within_bound(started)

        assert child_cleanup.state(started.owned) == GONE
        assert started.leader.state is AnchorState.RELEASED
        assert started.leader.returncode == EXIT_CODE
        assert started.owned.popen.returncode == EXIT_CODE
        assert started.owned.popen.poll() == EXIT_CODE

        release_within_bound(started)

        join_watcher(started.watcher)
        assert thread_failures == []
        assert names_of(kernel.calls).count("waitpid") == 1
        assert kernel.violations == []
    assert started.leader.done_with_pid is True

    owned, probe = started.owned, started.probe
    dropped = child_cleanup.drop_popen(owned)
    del started
    assert_a_destroyed_handle_reaps_nothing(leaders, child_cleanup, owned, probe, dropped, GONE)


@pytest.mark.parametrize(
    ("stopped", "exit_published", "answer", "waitpids_before_the_refusal"),
    [
        pytest.param(False, False, None, 0, id="a-running-leader"),
        pytest.param(True, False, None, 0, id="a-stopped-leader"),
        pytest.param(False, True, (0, 0), 1, id="a-kernel-that-reaps-nothing"),
    ],
)
def test_a_refused_release_leaves_the_leader_for_a_later_release(
    stopped: bool,
    exit_published: bool,
    answer: tuple[int, int] | None,
    waitpids_before_the_refusal: int,
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = PassThroughKernel(waitpid_answer=answer)
    with monkeypatch.context() as patch:
        install_kernel(patch, kernel)
        started = start_bound(leaders, kernel)
        if stopped:
            child_cleanup.send_signal(started.owned, signal.SIGSTOP)
            child_cleanup.wait_stopped(started.owned)
        if exit_published:
            started.write_exit_line(EXIT_CODE)
            leaders.await_exit_seen(started)

        with pytest.raises(AnchorStateError):
            release_within_bound(started)

        assert names_of(kernel.calls).count("waitpid") == waitpids_before_the_refusal
        published = [(AnchorState.EXITED, EXIT_CODE)] if exit_published else []
        assert started.probe.observed == published
        assert started.leader.state is (
            AnchorState.EXITED if exit_published else AnchorState.RUNNING
        )
        if exit_published:
            assert child_cleanup.state(started.owned) == Exited(EXIT_CODE)

        if stopped:
            child_cleanup.send_signal(started.owned, signal.SIGCONT)
        if not exit_published:
            started.write_exit_line(EXIT_CODE)
            leaders.await_exit_seen(started)
        release_within_bound(started)

        assert started.leader.state is AnchorState.RELEASED
        assert child_cleanup.state(started.owned) == GONE
        join_watcher(started.watcher)
        assert thread_failures == []
        assert kernel.violations == []


def assert_the_watcher_never_followed_the_reap(
    kernel: RecordingKernel, watcher_options: list[int]
) -> None:
    assert names_of(kernel.calls) == ["waitid"] * len(watcher_options) + ["waitpid"]
    assert [call.options for call in kernel.calls[:-1]] == watcher_options
    assert kernel.violations == []


@pytest.mark.parametrize(
    ("kernel", "await_hold", "watcher_options"),
    [
        pytest.param(
            lambda: PassThroughKernel(hold_before_call=1),
            lambda kernel: kernel.await_calls(1, "waitid"),
            [OBSERVE_EXIT],
            id="a-hold-before-the-first-wait",
        ),
        pytest.param(
            lambda: PassThroughKernel(hold_after_call=1),
            lambda kernel: kernel.await_reached(),
            [OBSERVE_EXIT],
            id="a-hold-between-the-wait-and-the-commit",
        ),
        pytest.param(
            lambda: KernelModel(stops=1, hold_before_call=2),
            lambda kernel: kernel.await_calls(2, "waitid"),
            [OBSERVE_EXIT, CONSUME_STOP_REPORT, OBSERVE_EXIT],
            id="a-hold-before-the-stop-consumption",
        ),
    ],
)
def test_a_release_while_the_watcher_waits_makes_no_kernel_call(
    kernel: Callable[[], RecordingKernel],
    await_hold: Callable[[RecordingKernel], None],
    watcher_options: list[int],
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = kernel()
    with monkeypatch.context() as patch:
        install_kernel(patch, model)
        started = start_bound(leaders, model)
        started.write_exit_line(EXIT_CODE)
        assert child_cleanup.wait_exited(started.owned) == EXIT_CODE
        try:
            await_hold(model)

            with pytest.raises(AnchorStateError):
                release_within_bound(started)

            assert "waitpid" not in names_of(model.calls)
            assert child_cleanup.state(started.owned) == Exited(EXIT_CODE)
            assert started.probe.observed == []
        finally:
            model.open_hold()
        leaders.await_exit_seen(started)

        assert started.leader.state is AnchorState.EXITED
        assert started.probe.observed == [(AnchorState.EXITED, EXIT_CODE)]
        release_within_bound(started)

        assert child_cleanup.state(started.owned) == GONE
        join_watcher(started.watcher)
        assert thread_failures == []
        assert_the_watcher_never_followed_the_reap(model, watcher_options)


def test_a_release_during_the_exit_callback_leaves_the_live_watcher_no_call_to_make(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = PassThroughKernel()
    with monkeypatch.context() as patch:
        install_kernel(patch, kernel)
        started = start_bound(leaders, kernel, ExitProbe(held=True))
        started.write_exit_line(EXIT_CODE)
        try:
            assert started.probe.seen.wait(TEST_WAIT_SECONDS), "on_exit was not called in time"

            release_within_bound(started)

            assert started.watcher.is_alive()
            assert started.leader.state is AnchorState.RELEASED
            assert child_cleanup.state(started.owned) == GONE
        finally:
            started.probe.release.set()
        join_watcher(started.watcher)

        assert thread_failures == []
        assert started.probe.observed == [(AnchorState.EXITED, EXIT_CODE)]
        assert_the_watcher_never_followed_the_reap(kernel, [OBSERVE_EXIT])


def test_a_loss_the_kernel_reports_to_a_release_leaves_the_real_zombie_intact_and_disarms_popen(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = PassThroughKernel(waitpid_answer=ChildProcessError("scripted loss"))
    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger=SPAWN_LOGGER):
        install_kernel(patch, kernel)
        started = start_bound(leaders, kernel, ExitProbe(held=True))
        started.write_exit_line(EXIT_CODE)
        try:
            assert started.probe.seen.wait(TEST_WAIT_SECONDS), "on_exit was not called in time"

            with pytest.raises(ChildIdentityLost):
                release_within_bound(started)

            assert started.leader.state is AnchorState.LOST
            assert started.leader.returncode == EXIT_CODE
            disarmed = started.owned.popen.returncode
            assert disarmed is not None
            assert not isinstance(disarmed, int)
        finally:
            started.probe.release.set()
        join_watcher(started.watcher)

        assert started.probe.observed == [(AnchorState.EXITED, EXIT_CODE)]
        assert spawn_warnings(caplog) == []
        assert thread_failures == []
        with pytest.raises(ChildIdentityLost):
            release_within_bound(started)
        assert names_of(kernel.calls).count("waitpid") == 1
        assert started.leader.done_with_pid is True
        assert kernel.violations == []
        assert child_cleanup.state(started.owned) == Exited(EXIT_CODE)

    owned, probe = started.owned, started.probe
    dropped = child_cleanup.drop_popen(owned)
    del started
    assert_a_destroyed_handle_reaps_nothing(
        leaders, child_cleanup, owned, probe, dropped, Exited(EXIT_CODE)
    )


def test_an_exit_callback_may_release_its_own_leader(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = leaders.start(ExitProbe(then=lambda leader: leader.release()))

    with caplog.at_level(logging.WARNING, logger=SPAWN_LOGGER):
        started.write_exit_line(EXIT_CODE)
        leaders.await_exit_seen(started)

    assert started.leader.state is AnchorState.RELEASED
    assert started.leader.returncode == EXIT_CODE
    assert started.probe.observed == [(AnchorState.EXITED, EXIT_CODE)]
    assert spawn_warnings(caplog) == []
    assert thread_failures == []
    assert child_cleanup.state(started.owned) == GONE


def exited_leader_with_a_held_release(
    patch: pytest.MonkeyPatch, leaders: Leaders
) -> tuple[PassThroughKernel, Started]:
    kernel = PassThroughKernel(hold_before_waitpid=1)
    install_kernel(patch, kernel)
    started = start_bound(leaders, kernel)
    started.write_exit_line(EXIT_CODE)
    leaders.await_exit_seen(started)
    return kernel, started


def test_two_releasers_of_one_exited_leader_reap_once(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = InstrumentedLock()
    with monkeypatch.context() as patch:
        patch.setattr(spawn, "_lifecycle_lock", lambda: lock)
        kernel, started = exited_leader_with_a_held_release(patch, leaders)
        releasers = [Releaser(started.leader), Releaser(started.leader)]
        try:
            kernel.await_calls(1, "waitpid")
            lock.await_waiting(1)
        finally:
            kernel.open_hold()
        for releaser in releasers:
            releaser.join()

        join_watcher(started.watcher)
        assert thread_failures == []
        assert [releaser.error for releaser in releasers] == [None, None]
        assert names_of(kernel.calls).count("waitpid") == 1
        assert kernel.violations == []
    assert started.leader.state is AnchorState.RELEASED
    assert started.leader.returncode == EXIT_CODE
    assert child_cleanup.state(started.owned) == GONE


def test_a_cleanup_during_a_held_release_keeps_the_zombie_for_the_late_reap(
    leaders: Leaders,
    child_cleanup: ChildCleanup,
    thread_failures: list[threading.ExceptHookArgs],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        kernel, started = exited_leader_with_a_held_release(patch, leaders)
        releaser = Releaser(started.leader)
        try:
            kernel.await_calls(1, "waitpid")

            assert (
                call_within_deadline(
                    lambda: started.leader.done_with_pid, seconds=TEST_WAIT_SECONDS
                )
                is False
            )
            child_cleanup.cleanup()

            assert child_cleanup.state(started.owned) == Exited(EXIT_CODE)
        finally:
            kernel.open_hold()
        releaser.join()

        join_watcher(started.watcher)
        assert thread_failures == []
        assert releaser.error is None
        assert child_cleanup.state(started.owned) == GONE
        assert started.leader.state is AnchorState.RELEASED
        assert started.leader.returncode == EXIT_CODE
        assert started.owned.popen.returncode == EXIT_CODE
        assert started.leader.done_with_pid is True
        assert names_of(kernel.calls).count("waitpid") == 1
        assert kernel.violations == []
        child_cleanup.cleanup()


@pytest.mark.parametrize("loses_at", ["wait", "consume"])
def test_a_waitpid_after_a_scripted_loss_is_refused_and_never_forwarded(loses_at: str) -> None:
    a_pid_no_process_can_hold = 2**22 + 1
    kernel = KernelModel(stops=1, loses_at=loses_at)
    kernel.bind(a_pid_no_process_can_hold)
    options = OBSERVE_EXIT if loses_at == "wait" else CONSUME_STOP_REPORT
    losing_outcome: list[BaseException | None] = []

    def lose_the_wait() -> None:
        try:
            kernel(os.P_PID, a_pid_no_process_can_hold, options)
            losing_outcome.append(None)
        except Exception as raised:
            losing_outcome.append(raised)

    losing_call = threading.Thread(
        target=lose_the_wait,
        name=f"{WATCHER_THREAD_PREFIX}{a_pid_no_process_can_hold}",
    )
    losing_call.start()
    losing_call.join(TEST_WAIT_SECONDS)

    assert not losing_call.is_alive()
    assert [type(outcome) for outcome in losing_outcome] == [ChildProcessError]

    with pytest.raises(ChildProcessError):
        kernel.waitpid(a_pid_no_process_can_hold, os.WNOHANG)

    assert kernel.violations == [
        f"a call named pid {a_pid_no_process_can_hold} after it was reaped"
    ]
