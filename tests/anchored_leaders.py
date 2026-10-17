"""The leader harness of the anchored tests: the exit callback probe and the started leader."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from anchored_kernel import join_watcher, watcher_thread
from anchored_support import TEST_WAIT_SECONDS, ChildCleanup, OwnedChild
from test_zombie_anchor import EXIT_WITH_LINE

from agent_providers import spawn
from agent_providers.spawn import AnchoredLeader, AnchorState, ChildProcess, closed_environment

SPAWN_LOGGER = "agent_providers.spawn"
class ExitProbe:
    """The ``on_exit`` callback: counts its calls and reads the handle from inside them."""

    def __init__(self, *, failure: Exception | None = None, held: bool = False) -> None:
        self.seen = threading.Event()
        self.release = threading.Event()
        if not held:
            self.release.set()
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
        for record in caplog.records
        if record.name == SPAWN_LOGGER and record.levelno == logging.WARNING
    ]
