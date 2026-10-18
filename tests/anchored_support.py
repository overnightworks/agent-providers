"""The session zombie anchor: tests hold real children through pidfds and never by number.

A pidfd names one process, not a number: once that process is reaped, ``waitid(P_PIDFD)``
on it raises ``ChildProcessError`` and a signal raises ``ESRCH``, and a later process with
the same number has another pidfd. Code that acts only through pidfds cannot reach another
process, whoever reaped its child and whenever the number is reused. This module names no
numbered process operation and calls no method of a ``Popen`` (the lint in
``test_zombie_anchor`` holds it to that); it only writes a ``Popen``'s ``returncode``.

``os.pidfd_open`` is missing from some interpreters, so the two pidfd calls go through the
typed ``syscall`` binding on every interpreter and local runs prove the path CI runs.
"""

from __future__ import annotations

import ctypes
import enum
import errno
import gc
import math
import os
import platform
import select
import signal
import subprocess
import sys
import time
import weakref
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from unittest import mock

from agent_providers.spawn import ChildProcess

P_PIDFD = 3
TEST_WAIT_SECONDS = 10
SIGKILLED = -9
DISARMED = object()
IMPORT_ROOT_PRELUDE = "import os, sys\nsys.path[:0] = sys.argv[1].split(os.pathsep)\n"

_PIDFD_SYSCALL_NUMBERS = {
    "x86_64": (434, 424),
    "aarch64": (434, 424),
}

_syscall = ctypes.CDLL(None, use_errno=True).syscall
_syscall.restype = ctypes.c_long
_syscall.argtypes = [ctypes.c_long]


def absolute_import_roots() -> str:
    """The absolute import roots an isolated interpreter takes through the prelude."""
    roots = [entry for entry in sys.path if entry]
    assert all(os.path.isabs(entry) for entry in roots), "an import root is relative"
    return os.pathsep.join(roots)


def _pidfd_syscall_numbers() -> tuple[int, int]:
    machine = platform.machine()
    if machine not in _PIDFD_SYSCALL_NUMBERS:
        raise OSError(errno.ENOSYS, f"no pidfd syscall numbers known for machine {machine!r}")
    return _PIDFD_SYSCALL_NUMBERS[machine]


def _checked(result: int) -> int:
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def pidfd_open(pid: int) -> int:
    open_number, _ = _pidfd_syscall_numbers()
    return _checked(_syscall(open_number, ctypes.c_long(pid), ctypes.c_long(0)))


def pidfd_send_signal(pidfd: int, signal_number: int) -> None:
    _, send_number = _pidfd_syscall_numbers()
    _checked(
        _syscall(
            send_number,
            ctypes.c_long(pidfd),
            ctypes.c_long(signal_number),
            ctypes.c_void_p(None),
            ctypes.c_long(0),
        )
    )


_waitid = os.waitid


def wait_for_exits(pidfds: Iterable[int], timeout: float) -> set[int]:
    """Return the pidfds readable within ``timeout``; readable never means unreaped."""
    pending = set(pidfds)
    poller = select.poll()
    for pidfd in pending:
        poller.register(pidfd, select.POLLIN)
    readable: set[int] = set()
    deadline = time.monotonic() + timeout
    while pending:
        remaining = deadline - time.monotonic()
        for pidfd, _ in poller.poll(max(0, math.ceil(remaining * 1000))):
            readable.add(pidfd)
            pending.discard(pidfd)
            poller.unregister(pidfd)
        if remaining <= 0:
            break
    return readable


@runtime_checkable
class PidUser(Protocol):
    """A handle that may act on a child's pid; ``done_with_pid`` never blocks.

    True certifies that the handle will never act on its pid again. False certifies
    nothing (lock contention can read false for a handle that is already done).
    """

    @property
    def done_with_pid(self) -> bool: ...


class Recipient(enum.Enum):
    NOBODY = "nobody"
    UNREPORTED = "unreported"


NOBODY = Recipient.NOBODY
UNREPORTED = Recipient.UNREPORTED


class ProcessState(enum.Enum):
    RUNNING = "running"
    GONE = "gone"


RUNNING = ProcessState.RUNNING
GONE = ProcessState.GONE


@dataclass(frozen=True)
class Exited:
    """An exited, unreaped child; ``code`` is ``Popen``'s encoding (status, or minus the signal)."""

    code: int


class ChildErrorKind(enum.Enum):
    KILL_REFUSED = "kill refused, termination unconfirmed"
    NOT_DEAD_AT_THE_BOUND = "not dead at the bound"
    CALL_FAILED = "call failed"
    REAPED_WHILE_POPEN_WAITED = "reaped while its Popen still waited"
    HANDLE_STILL_HOLDS_PID = "its handle still holds the pid"


@dataclass(frozen=True)
class ChildError:
    pid: int
    kind: ChildErrorKind
    detail: str = ""


class ResolvedChildTouched(RuntimeError):
    """A resolved record was used; its closed pidfd number may now name another child's pidfd."""


class SessionCleanupFailed(Exception):
    def __init__(self, errors: Iterable[ChildError]) -> None:
        self.errors = tuple(errors)
        super().__init__(
            "; ".join(f"pid {e.pid}: {e.kind.value} {e.detail}".rstrip() for e in self.errors)
        )


@dataclass(frozen=True)
class DroppedPopen:
    """What stays of a disarmed ``Popen`` once its owner let it go: nothing left to write."""

    returncode: object = DISARMED


@dataclass
class OwnedChild:
    """A held child: its pid, the pidfd that names it, its ``Popen`` and who else has its number.

    ``name`` is the one place a pidfd is opened for a child and must run the moment ``Popen``
    returns, before the number reached any other code. The pidfd belongs to whoever holds the
    record. Resolving a record closes its pidfd and marks it; a resolved record is never
    touched again because the process may reuse a closed descriptor's number, even for
    another child's pidfd.
    """

    pid: int
    pidfd: int
    popen: subprocess.Popen | DroppedPopen
    recipient: Recipient | weakref.ReferenceType[PidUser]
    resolved: bool = field(default=False)

    @classmethod
    def name(cls, popen: subprocess.Popen, recipient: Recipient | PidUser) -> OwnedChild:
        pidfd = pidfd_open(popen.pid)
        return cls(popen.pid, pidfd, popen, _held_recipient(recipient))

    def declare_recipient(self, recipient: Recipient | PidUser) -> None:
        self.recipient = _held_recipient(recipient)


def _held_recipient(
    recipient: Recipient | PidUser,
) -> Recipient | weakref.ReferenceType[PidUser]:
    if isinstance(recipient, Recipient):
        return recipient
    return weakref.ref(recipient)


def _require_unresolved(children: Iterable[OwnedChild]) -> None:
    for owned in children:
        if owned.resolved:
            raise ResolvedChildTouched(
                f"pid {owned.pid} is resolved; its pidfd number may be reused"
            )


def state(owned: OwnedChild) -> ProcessState | Exited:
    _require_unresolved([owned])
    try:
        observed = _waitid(P_PIDFD, owned.pidfd, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return GONE
    if observed is None:
        return RUNNING
    if observed.si_code == os.CLD_EXITED:
        return Exited(observed.si_status)
    return Exited(-observed.si_status)


def free_to_reap(owned: OwnedChild, *, session_ended: bool) -> bool:
    recipient = owned.recipient
    if recipient is NOBODY:
        return True
    if recipient is UNREPORTED:
        return session_ended
    handle = recipient()
    return handle is None or handle.done_with_pid


def kill_children(children: Iterable[OwnedChild]) -> list[ChildError]:
    """Step one: ``SIGKILL`` through each pidfd; refusals are errors, ``ESRCH`` means reaped."""
    children = list(children)
    _require_unresolved(children)
    errors = []
    for owned in children:
        try:
            pidfd_send_signal(owned.pidfd, signal.SIGKILL)
        except OSError as refusal:
            if refusal.errno != errno.ESRCH:
                errors.append(
                    ChildError(
                        owned.pid,
                        ChildErrorKind.KILL_REFUSED,
                        errno.errorcode.get(refusal.errno or 0, str(refusal.errno)),
                    )
                )
    return errors


def resolve_children(
    children: Iterable[OwnedChild], *, deadline: float, session_ended: bool
) -> list[ChildError]:
    """Step two: wait within ``deadline`` (a ``time.monotonic`` instant), then reap or keep.

    A child is reaped through its pidfd only when free to reap; a zombie its handle may
    still act on is kept, because an unreaped zombie keeps its number from reuse. Whatever is
    not confirmed dead is kept with its pidfd and its ``Popen``.
    """
    children = list(children)
    _require_unresolved(children)
    readable = wait_for_exits(
        [owned.pidfd for owned in children], max(0.0, deadline - time.monotonic())
    )
    errors = []
    for owned in children:
        if owned.pidfd not in readable:
            errors.append(ChildError(owned.pid, ChildErrorKind.NOT_DEAD_AT_THE_BOUND))
            continue
        errors.extend(_resolve_ready_child(owned, session_ended=session_ended))
    return errors


def _resolve_ready_child(owned: OwnedChild, *, session_ended: bool) -> list[ChildError]:
    try:
        observed = state(owned)
        if observed is RUNNING:
            return [ChildError(owned.pid, ChildErrorKind.NOT_DEAD_AT_THE_BOUND)]
        if observed is GONE:
            return _resolve_reaped_elsewhere(owned)
        if not free_to_reap(owned, session_ended=session_ended):
            if session_ended:
                return [ChildError(owned.pid, ChildErrorKind.HANDLE_STILL_HOLDS_PID)]
            return []
        _reap(owned, observed)
    except OSError as failure:
        detail = errno.errorcode.get(failure.errno or 0, str(failure.errno))
        return [ChildError(owned.pid, ChildErrorKind.CALL_FAILED, detail)]
    return []


def _reap(owned: OwnedChild, observed: Exited) -> None:
    if _waitid(P_PIDFD, owned.pidfd, os.WEXITED | os.WNOHANG) is None:
        raise OSError(errno.EAGAIN, "exited child was not reapable")
    if owned.popen.returncode is None:
        owned.popen.returncode = observed.code
    _resolve(owned)


def _resolve_reaped_elsewhere(owned: OwnedChild) -> list[ChildError]:
    errors = []
    if owned.popen.returncode is None:
        owned.popen.returncode = DISARMED
        errors.append(ChildError(owned.pid, ChildErrorKind.REAPED_WHILE_POPEN_WAITED))
    _resolve(owned)
    return errors


def _resolve(owned: OwnedChild) -> None:
    os.close(owned.pidfd)
    owned.resolved = True


class ZombieAnchor:
    """Holds children until the session's end, then kills, reaps what is free and fails loudly.

    It never signals or reaps a process it was not given, never frees a number a handle may
    still act on, and gives every child it holds ``SIGKILL`` through its pidfd and then either
    reaps it through that pidfd or fails the session naming its pid. It never closes the pidfd
    of an unresolved child and never drops a ``Popen`` whose ``returncode`` is ``None``.
    """

    def __init__(self) -> None:
        self._held: list[OwnedChild] = []

    def hold(self, owned: OwnedChild) -> None:
        self._held.append(owned)

    def hand_over(self, to: ZombieAnchor) -> None:
        for owned in self._held:
            to.hold(owned)
        self._held = []

    def finalize(self, bound: float = TEST_WAIT_SECONDS) -> None:
        deadline = time.monotonic() + bound
        gc.collect()
        errors = kill_children(self._held)
        errors.extend(resolve_children(self._held, deadline=deadline, session_ended=True))
        self._held = [owned for owned in self._held if not owned.resolved]
        if errors:
            raise SessionCleanupFailed(errors)


def zombie_anchor_session() -> Iterator[ZombieAnchor]:
    anchor = ZombieAnchor()
    yield anchor
    anchor.finalize()


class FakeHandle:
    """A ``PidUser`` whose ``done_with_pid`` the test sets."""

    def __init__(self, *, done_with_pid: bool = False) -> None:
        self.done_with_pid = done_with_pid


class CleanupFailed(SessionCleanupFailed):
    """A child's kill was refused or its death was not confirmed; the next cleanup retries it."""


class IdentityUnavailable(Exception):
    """A pidfd call was refused; ``popen`` is the child that exists without a pidfd, if any."""

    def __init__(self, message: str, *, popen: subprocess.Popen | None = None) -> None:
        super().__init__(message)
        self.popen = popen


@dataclass(frozen=True)
class Captured:
    value: object
    children: tuple[OwnedChild, ...]


def popen_bare(
    child: ChildProcess, *, stdin: int | None, stdout: int | None, stderr: int | None
) -> subprocess.Popen:
    return subprocess.Popen(
        child.command,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=dict(child.environment),
        cwd=child.working_directory,
        start_new_session=True,
    )


class ChildCleanup:
    """Owns every child a test starts: named by a pidfd at creation, killed first, never reaped
    behind a handle.

    The kill comes first so that a running child ends by the kill and never by an exit line or
    EOF; a pidfd kill reaches only its own process, so it is always safe. Registered raw
    descriptors are closed with ``os.close`` (never a file object, so no flush can wait), then
    the children are waited for within one bound. A dead child is reaped through its pidfd only
    when nobody else may still act on its number; otherwise it stays an unreaped zombie
    (deferred, not an error), because an unreaped zombie keeps its number from reuse and a
    delayed watcher could still wait on a freed one. There is deliberately no operation that
    reaps behind a handle. ``hand_over`` moves whatever is still listed into the session's
    anchor after ``cleanup`` decided deferral while every handle was as reachable as the test
    left it; only the anchor reaps it, once its handle let go. A ``Popen`` whose ``returncode``
    is ``None`` is never dropped, because its destructor would wait on a number; ``drop_popen``
    admits only a disarmed one. A refused pidfd is never answered with the number: the child
    ends with its own lifetime and its ``Popen`` reaps it.
    """

    def __init__(self, anchor: ZombieAnchor) -> None:
        self._anchor = anchor
        self._listed: list[OwnedChild] = []
        self._descriptors: list[int] = []
        _prove_the_pidfd_calls_on_this_process()

    def listed(self) -> tuple[OwnedChild, ...]:
        return tuple(self._listed)

    def capture(self, start: Callable[[], object]) -> Captured:
        created: list[OwnedChild] = []
        real_popen = subprocess.Popen

        def create(*args, **kwargs):
            popen = real_popen(*args, **kwargs)
            try:
                owned = OwnedChild.name(popen, UNREPORTED)
            except OSError as refusal:
                raise IdentityUnavailable(
                    f"pid {popen.pid}: pidfd_open refused: {refusal}", popen=popen
                ) from refusal
            self._listed.append(owned)
            created.append(owned)
            return popen

        with mock.patch("subprocess.Popen", create):
            value = start()
        if isinstance(value, PidUser):
            for owned in created:
                owned.declare_recipient(value)
        return Captured(value, tuple(created))

    def start_bare_child(
        self, child: ChildProcess, *, stdin: int | None, stdout: int | None, stderr: int | None
    ) -> OwnedChild:
        captured = self.capture(
            lambda: popen_bare(child, stdin=stdin, stdout=stdout, stderr=stderr)
        )
        (owned,) = captured.children
        owned.declare_recipient(NOBODY)
        return owned

    def state(self, owned: OwnedChild) -> ProcessState | Exited:
        self._require_listed(owned)
        return state(owned)

    def wait_exited(self, owned: OwnedChild, *, bound: float = TEST_WAIT_SECONDS) -> int:
        self._require_listed(owned)
        if owned.pidfd not in wait_for_exits([owned.pidfd], bound):
            raise AssertionError(f"pid {owned.pid} did not exit within {bound}s")
        observed = state(owned)
        if not isinstance(observed, Exited):
            raise AssertionError(f"pid {owned.pid} is {observed}, not exited")
        return observed.code

    def wait_stopped(self, owned: OwnedChild) -> None:
        """Block until the child reports a stop, without consuming the report.

        Preconditions: a fresh stop request, no competing stop consumer, and no continuation
        before this observation. Under them the stop report is certain to arrive, so there is no
        timer; a report that was already consumed is never reported again and would block forever.
        """
        self._require_listed(owned)
        report = _waitid(P_PIDFD, owned.pidfd, os.WSTOPPED | os.WEXITED | os.WNOWAIT)
        if report.si_code not in (os.CLD_STOPPED, os.CLD_TRAPPED):
            raise AssertionError(f"pid {owned.pid} reported an exit, not a stop")

    def send_signal(self, owned: OwnedChild, signal_number: int) -> None:
        self._require_listed(owned)
        pidfd_send_signal(owned.pidfd, signal_number)

    def drop_popen(self, owned: OwnedChild) -> weakref.ReferenceType[subprocess.Popen]:
        self._require_listed(owned)
        if owned.popen.returncode is None:
            raise AssertionError(f"pid {owned.pid}: an armed Popen is never dropped")
        dropped = weakref.ref(owned.popen)
        owned.popen = DroppedPopen()
        return dropped

    def close_at_cleanup(self, descriptor: int) -> None:
        self._descriptors.append(descriptor)

    def cleanup(self, bound: float = TEST_WAIT_SECONDS) -> None:
        deadline = time.monotonic() + bound
        errors = kill_children(self._listed)
        self._close_descriptors()
        errors.extend(resolve_children(self._listed, deadline=deadline, session_ended=False))
        self._listed = [owned for owned in self._listed if not owned.resolved]
        if errors:
            raise CleanupFailed(errors)

    def hand_over(self) -> None:
        for owned in self._listed:
            self._anchor.hold(owned)
        self._listed = []

    def _close_descriptors(self) -> None:
        descriptors, self._descriptors = self._descriptors, []
        for descriptor in descriptors:
            os.close(descriptor)

    def _require_listed(self, owned: OwnedChild) -> None:
        if not any(listed is owned for listed in self._listed):
            raise AssertionError(f"pid {owned.pid} is not listed: it was resolved or handed over")


def _prove_the_pidfd_calls_on_this_process() -> None:
    try:
        pidfd = pidfd_open(os.getpid())
    except OSError as refusal:
        raise IdentityUnavailable(f"pidfd_open refused: {refusal}") from refusal
    try:
        _prove_signal_and_wait(pidfd)
    finally:
        os.close(pidfd)


def _prove_signal_and_wait(pidfd: int) -> None:
    try:
        pidfd_send_signal(pidfd, 0)
    except OSError as refusal:
        raise IdentityUnavailable(f"pidfd_send_signal refused: {refusal}") from refusal
    try:
        _waitid(P_PIDFD, pidfd, os.WEXITED | os.WNOHANG)
    except ChildProcessError:
        return
    except OSError as refusal:
        raise IdentityUnavailable(f"waitid refused: {refusal}") from refusal
    raise IdentityUnavailable("waitid reported a child for the test process itself")


@contextmanager
def child_cleanup_scope(anchor: ZombieAnchor) -> Iterator[ChildCleanup]:
    cleanup = ChildCleanup(anchor)
    try:
        yield cleanup
    finally:
        try:
            cleanup.cleanup()
        finally:
            cleanup.hand_over()
