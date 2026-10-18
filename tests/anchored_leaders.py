"""The leader harness of the anchored tests: the exit callback probe and the started leader."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from anchored_kernel import RecordingKernel, join_watcher, watcher_thread
from anchored_support import TEST_WAIT_SECONDS, ChildCleanup, OwnedChild
from test_zombie_anchor import EXIT_WITH_LINE

from agent_providers import spawn
from agent_providers.spawn import AnchoredLeader, AnchorState, ChildProcess, closed_environment

SPAWN_LOGGER = "agent_providers.spawn"
LEADER_WITH_GRANDCHILD = (
    "import os, signal, sys\n"
    "if os.fork() == 0:\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "    sys.stdout.write(f'{os.getpid()}\\n')\n"
    "    sys.stdout.flush()\n"
    "    sys.stdin.read()\n"
    "    os._exit(0)\n"
    "sys.exit(3)\n"
)


class ExitProbe:
    """The ``on_exit`` callback: counts its calls and reads the handle from inside them."""

    def __init__(
        self,
        *,
        failure: Exception | None = None,
        held: bool = False,
        then: Callable[[AnchoredLeader], None] | None = None,
    ) -> None:
        self.seen = threading.Event()
        self.release = threading.Event()
        if not held:
            self.release.set()
        self.observed: list[tuple[AnchorState, int | None]] = []
        self.leader: AnchoredLeader | None = None
        self._bound = threading.Event()
        self._failure = failure
        self._then = then

    def bind(self, leader: AnchoredLeader | None) -> None:
        self.leader = leader
        self._bound.set()

    def __call__(self) -> None:
        assert self._bound.wait(TEST_WAIT_SECONDS), "the handle was never bound"
        assert self.leader is not None
        self.observed.append((self.leader.state, self.leader.returncode))
        self.seen.set()
        if self._then is not None:
            self._then(self.leader)
        assert self.release.wait(TEST_WAIT_SECONDS), "the callback was never released"
        if self._failure is not None:
            raise self._failure


@dataclass
class Started:
    leader: AnchoredLeader
    owned: OwnedChild
    probe: ExitProbe
    stdin: int
    watcher: threading.Thread

    def write_exit_line(self, code: int) -> None:
        os.write(self.stdin, f"{code}\n".encode())


class Leaders:
    def __init__(self, tmp_path: Path, cleanup: ChildCleanup) -> None:
        self._tmp_path = tmp_path
        self._cleanup = cleanup

    def describe(self, binary: Path | None = None, script: str = EXIT_WITH_LINE) -> ChildProcess:
        return ChildProcess(
            binary=binary or Path(sys.executable),
            arguments=("-I", "-c", script),
            environment=closed_environment(self._tmp_path),
            working_directory=self._tmp_path,
        )

    def start(
        self,
        probe: ExitProbe | None = None,
        *,
        script: str = EXIT_WITH_LINE,
        stdout: int = subprocess.DEVNULL,
    ) -> Started:
        probe = probe or ExitProbe()
        reader, writer = os.pipe()
        self._cleanup.close_at_cleanup(writer)
        try:
            captured = self._cleanup.capture(
                lambda: spawn.start_anchored_leader(
                    self.describe(script=script),
                    stdin=reader,
                    stdout=stdout,
                    stderr=subprocess.DEVNULL,
                    on_exit=probe,
                )
            )
        finally:
            os.close(reader)
        (owned,) = captured.children
        watcher = watcher_thread(owned.pid)
        probe.bind(captured.value)
        return Started(captured.value, owned, probe, writer, watcher)

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

    def await_exit_seen(self, started: Started) -> None:
        assert started.probe.seen.wait(TEST_WAIT_SECONDS), "on_exit was not called within the bound"
        join_watcher(started.watcher)


def spawn_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.get_records("call")
        if record.name == SPAWN_LOGGER and record.levelno == logging.WARNING
    ]


class InstrumentedLock:
    """A lifecycle lock that reports the threads waiting for it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._changed = threading.Condition()
        self._waiting: list[str] = []

    def acquire(self, blocking: bool = True) -> bool:
        if self._lock.acquire(blocking=False):
            return True
        if not blocking:
            return False
        with self._changed:
            self._waiting.append(threading.current_thread().name)
            self._changed.notify_all()
        return self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exception: object) -> None:
        self.release()

    @property
    def waiting(self) -> list[str]:
        with self._changed:
            return list(self._waiting)

    def await_waiting(self, count: int) -> None:
        with self._changed:
            assert self._changed.wait_for(lambda: len(self._waiting) >= count, TEST_WAIT_SECONDS), (
                f"fewer than {count} threads waited for the lock within the bound"
            )


class Releaser:
    """A daemon thread that releases a leader, joined by the bound."""

    def __init__(self, leader: spawn.AnchoredLeader) -> None:
        self.error: BaseException | None = None
        self._leader = leader
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._leader.release()
        except BaseException as failure:
            self.error = failure

    def join(self) -> None:
        self._thread.join(TEST_WAIT_SECONDS)
        assert not self._thread.is_alive(), "a releaser was still blocked at its bound"


def start_bound(
    leaders: Leaders, kernel: RecordingKernel, probe: ExitProbe | None = None
) -> Started:
    started = leaders.start(probe)
    kernel.bind(started.owned.pid)
    return started
