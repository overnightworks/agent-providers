"""The descendant handshake the real-leader rows share, and the script that starts it."""

from __future__ import annotations

import contextlib
import os
import select
import shlex
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from anchored_support import (
    TEST_WAIT_SECONDS,
    ChildCleanup,
    pidfd_open,
    pidfd_send_signal,
    wait_for_exits,
)

CONSUMES_STDIN = "while IFS= read -r line; do :; done\n"
WAITS_WITHOUT_READING_STDIN = "exec sleep 60\n"

DESCENDANT_PROGRAM = """\
import os
import signal
import sys

identity, ack, ready, escapes = sys.argv[1:5]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
null = os.open(os.devnull, os.O_RDWR)
for stream in (0, 1, 2):
    os.dup2(null, stream)
os.closerange(3, 1 << 16)
with open("/proc/self/stat") as stat:
    start_time = stat.read().rsplit(")", 1)[1].split()[19]
os.write(os.open(identity, os.O_WRONLY), f"{os.getpid()} {start_time}\\n".encode())
if not os.read(os.open(ack, os.O_RDONLY), 1):
    sys.exit(1)
if escapes == "1":
    os.setsid()
os.write(os.open(ready, os.O_WRONLY), b"ready\\n")
while True:
    signal.pause()
"""


@dataclass(frozen=True)
class Handshake:
    program: Path
    identity: Path
    ack: Path
    ready: Path


def open_handshake(directory: Path) -> Handshake:
    paths = Handshake(*(directory / name for name in ("descendant.py", "identity", "ack", "ready")))
    paths.program.write_text(DESCENDANT_PROGRAM)
    for fifo in (paths.identity, paths.ack, paths.ready):
        os.mkfifo(fifo)
    return paths


def lingering_descendant(
    answer: str, *, exit_code: int, blocks: bool, escapes: bool, handshake: Handshake,
) -> str:
    arguments = " ".join(
        shlex.quote(str(argument))
        for argument in (
            sys.executable,
            "-I",
            handshake.program,
            handshake.identity,
            handshake.ack,
            handshake.ready,
            int(escapes),
        )
    )
    starts_the_descendant = f"{arguments} </dev/null >/dev/null 2>&1 &\n"
    waits_for_its_handshake = f"read -r _ < {shlex.quote(str(handshake.ready))}\n"
    answers = CONSUMES_STDIN + answer + f"exit {exit_code}\n"
    return starts_the_descendant + waits_for_its_handshake + (
        WAITS_WITHOUT_READING_STDIN if blocks else answers
    )


def process_identity(pid: int) -> tuple[str, str] | None:
    """The state and start time of ``pid``, or ``None`` when the kernel lists no such process."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            fields = stat.read().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return fields[0], fields[19]


def process_start_time(pid: int) -> str | None:
    """The start time of ``pid``, or ``None`` when the kernel lists no such process."""
    identity = process_identity(pid)
    return None if identity is None else identity[1]


class DescendantRow:
    """The handshake of a lingering descendant, done on the calling thread.

    The descendant is named by a validated pidfd before it is acknowledged, so it can neither
    escape the leader's group unseen nor outlive the row unowned. Leaving the row kills it by
    that pidfd. ``run_beside`` runs a blocking call on a worker thread while this thread does
    the handshake, and reads the descendant's death at the call's return, before any kill.
    """

    def __init__(self, handshake: Handshake, child_cleanup: ChildCleanup) -> None:
        self._child_cleanup = child_cleanup
        self._identity_fd = os.open(handshake.identity, os.O_RDONLY | os.O_NONBLOCK)
        self._ack_fd = os.open(handshake.ack, os.O_RDWR)
        child_cleanup.close_at_cleanup(self._identity_fd)
        child_cleanup.close_at_cleanup(self._ack_fd)
        self._pid: int | None = None
        self._pidfd: int | None = None
        self.dead_at_return: bool | None = None

    def __enter__(self) -> DescendantRow:
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.kill_the_descendant()

    def own_the_descendant(self) -> None:
        pid, start_time = self._identity_line().split()
        self._pid = int(pid)
        identity = process_identity(self._pid)
        assert identity is not None
        assert identity[1] == start_time, "the pid names another process"
        pidfd = pidfd_open(self._pid)
        self._child_cleanup.close_at_cleanup(pidfd)
        self._pidfd = pidfd
        os.write(self._ack_fd, b"x")

    def _identity_line(self) -> str:
        readable, _, _ = select.select([self._identity_fd], [], [], TEST_WAIT_SECONDS)
        assert readable, "the descendant never published its identity"
        return os.read(self._identity_fd, 100).decode()

    def descendant_is_dead(self) -> bool:
        assert self._pidfd is not None
        return self._pidfd in wait_for_exits([self._pidfd], 0)

    def kill_the_descendant(self) -> None:
        if self._pidfd is None:
            return
        with contextlib.suppress(ProcessLookupError):
            pidfd_send_signal(self._pidfd, signal.SIGKILL)
        assert self._pidfd in wait_for_exits([self._pidfd], TEST_WAIT_SECONDS)

    def run_beside[Result](self, call: Callable[[], Result]) -> Result:
        answers: list[Result] = []
        failures: list[BaseException] = []

        def run() -> None:
            try:
                answers.append(call())
            except Exception as failure:
                failures.append(failure)

        worker = threading.Thread(target=run, name="descendant-row-call")
        worker.start()
        try:
            self.own_the_descendant()
            worker.join(2 * TEST_WAIT_SECONDS)
            assert not worker.is_alive(), "the call did not return within the bound"
            self.dead_at_return = self.descendant_is_dead()
        finally:
            self.kill_the_descendant()
            worker.join(TEST_WAIT_SECONDS)
        if failures:
            raise failures[0]
        return answers[0]
