"""The one owner of child-process creation in this library.

Every child is described by one immutable :class:`ChildProcess`: an absolute
binary, an explicit working directory and the child's *complete* environment.
Nothing is inherited and nothing is merged onto a host baseline. An optional
set of extras on an inherited environment is a denylist seen from the other
side, and a denylist is what this owner exists to remove: the host's real
``HOME`` reaches an agent CLI's credential directory with the parent's rights.

``HOME`` must be set rather than merely absent, because an agent CLI with no
``HOME`` falls back to the passwd home — the very directory a closed
environment is meant to keep out of reach.

A closed environment is process configuration, not access control. It stops
discovery by convention (``HOME``, ``PATH``, ``CODEX_HOME``, ``GROK_HOME``,
``XDG_*``); it does not stop an absolute ``open()``, traversal out of the
working directory, or the network. UID, GID, umask, resource limits,
namespaces and mounts are inherited whatever this module does, and
``start_new_session`` moves only the session and process group. A host that
needs containment adds a sandbox around this spawn.

An anchored leader is a child whose exit nothing but its own handle observes
and, from the release on, reaps. The anchor: an exited, unreaped leader keeps
its pid, pgid and sid from being reused, which is what lets a later group
signal reach only this run.

The host obligation: the anchor holds only while nothing else reaps this
child. Forbidden are ``waitpid(-1)``, ``os.wait()``, ``wait3`` and ``wait4``
on any child, ``SIGCHLD`` set to ``SIG_IGN``, ``SA_NOCLDWAIT``, the deprecated
``FastChildWatcher`` of Python 3.12 and 3.13, and any ptrace request of the
service. Per-pid asyncio watchers do not consume this unregistered child. A
foreign reap after a successful observation can invalidate the anchor without
timely detection; :class:`ChildIdentityLost` is a best-effort signal, not a
guarantee.

The theorem interval: the anchor claims the number, not the census. A group
SIGKILL ``K`` reaches the members present during its walk. A reached process
can no longer attach a new child by fork or clone, but it can still finish an
in-flight ``setpgid``, ``setsid`` or ``exec``, and an eligible parent can move
such a child. So membership can change from ``K``'s walk through the end of
any later scan, and a later scan's "no live member" is read with the anchor
intact and ``/proc`` unmasked.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, cast

log = logging.getLogger(__name__)

HOME_VARIABLE: Final = "HOME"
SEARCH_PATH_VARIABLE: Final = "PATH"
TEMPORARY_DIRECTORY_VARIABLE: Final = "TMPDIR"

# Names whose value is a property of the machine, not of the operator's
# account: without them an agent CLI cannot resolve its own helpers, speak the
# deployment's locale, or reach the network through its proxy and CA store.
# Naming them is what makes the child environment an allowlist; their values
# come from the host process because only the host knows them.
INHERITED_MACHINE_VARIABLES: Final[tuple[str, ...]] = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
)

StderrPolicy = Literal["capture", "devnull"]

type PipedProcess = subprocess.Popen[bytes]
type AsyncPipedProcess = asyncio.subprocess.Process
PipedProcessTimeout = subprocess.TimeoutExpired


class ChildProcessSpecificationError(ValueError):
    """A child was described without an absolute binary, a cwd, or a HOME."""


def closed_environment(home: Path, **provider_variables: str) -> dict[str, str]:
    """Build a child's complete environment around one private home.

    The result is exactly ``HOME``, ``TMPDIR`` (the same private home, so the
    child's scratch files go with it), ``PATH``, the machine variables this
    host actually sets, and the provider's own variables. A caller cannot extend an
    inherited base, because there is none.
    """
    environment = {
        name: os.environ[name] for name in INHERITED_MACHINE_VARIABLES if os.environ.get(name)
    }
    environment[SEARCH_PATH_VARIABLE] = os.environ.get(SEARCH_PATH_VARIABLE) or os.defpath
    environment[HOME_VARIABLE] = str(home)
    environment[TEMPORARY_DIRECTORY_VARIABLE] = str(home)
    environment.update(provider_variables)
    return environment


@dataclass(frozen=True)
class ChildProcess:
    """The complete description of one child: binary, arguments, env, cwd."""

    binary: Path
    arguments: tuple[str, ...]
    environment: Mapping[str, str]
    working_directory: Path

    def __post_init__(self) -> None:
        if not self.binary.is_absolute():
            raise ChildProcessSpecificationError(
                f"A child process needs an absolute binary, not {self.binary}",
            )
        if not self.working_directory.is_absolute():
            raise ChildProcessSpecificationError(
                f"A child process needs an absolute working directory, "
                f"not {self.working_directory}",
            )
        if not self.environment.get(HOME_VARIABLE):
            raise ChildProcessSpecificationError(
                "A child process environment must set HOME: an unset HOME sends the "
                "child to the passwd home",
            )
        object.__setattr__(self, "arguments", tuple(self.arguments))
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    @property
    def command(self) -> tuple[str, ...]:
        return (str(self.binary), *self.arguments)

    def with_arguments(self, arguments: tuple[str, ...]) -> ChildProcess:
        return ChildProcess(
            binary=self.binary,
            arguments=arguments,
            environment=self.environment,
            working_directory=self.working_directory,
        )


def open_pipes(child: ChildProcess, *, stderr: StderrPolicy) -> subprocess.Popen[bytes]:
    """Start one child with pipes on every stream the caller reads.

    Its own session, because the caller holds the handle that reaps that
    whole group.
    """
    return subprocess.Popen(
        child.command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if stderr == "capture" else subprocess.DEVNULL,
        env=dict(child.environment),
        cwd=str(child.working_directory),
        start_new_session=True,
    )


def run_capturing(
    child: ChildProcess,
    *,
    stdin_text: str,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    """Run one child to completion, feeding stdin and capturing both streams.

    Deliberately *not* its own session. A new session is worth taking only
    where someone reaps the group it creates; this door returns no handle and
    its timeout kills the direct child alone, so a new session would strand
    that child's own children instead of leaving them reachable by a
    group-directed signal to this process.
    """
    return subprocess.run(
        child.command,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=dict(child.environment),
        cwd=str(child.working_directory),
        check=False,
    )


async def open_async_pipes(
    child: ChildProcess,
    *,
    stream_buffer_limit: int | None = None,
) -> AsyncPipedProcess:
    """Start one child on the event loop with pipes on every stream.

    Its own session, because every failure path in the calling turn reaps
    that group before it returns.
    """
    buffer_limit = {} if stream_buffer_limit is None else {"limit": stream_buffer_limit}
    return await asyncio.create_subprocess_exec(
        *child.command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(child.environment),
        cwd=str(child.working_directory),
        start_new_session=True,
        **buffer_limit,
    )


class ChildIdentityLost(RuntimeError):
    """Something else reaped the leader, so its number no longer names it."""


class AnchorStateError(RuntimeError):
    """A call the leader's current state does not allow."""


class AnchorState(Enum):
    RUNNING = "running"
    EXITED = "exited"
    RELEASED = "released"
    LOST = "lost"


@dataclass(frozen=True)
class LeaderStatus:
    state: AnchorState
    returncode: int | None


@dataclass(frozen=True)
class LeaderExited:
    returncode: int


@dataclass(frozen=True)
class LeaderStopped:
    pass


@dataclass(frozen=True)
class LeaderReaped:
    returncode: int


@dataclass(frozen=True)
class LeaderNotReaped:
    pass


@dataclass(frozen=True)
class LeaderGone:
    pass


LeaderEvent = LeaderExited | LeaderStopped | LeaderReaped | LeaderNotReaped | LeaderGone


@dataclass(frozen=True)
class LeaderTransition:
    status: LeaderStatus
    announces: bool


LEADER_STARTED: Final = LeaderStatus(AnchorState.RUNNING, None)

_ANCHORED_STATES: Final = frozenset({AnchorState.RUNNING, AnchorState.EXITED})


def exit_observation(si_code: int, si_status: int) -> LeaderExited | LeaderStopped:
    """Classify the report of a non-reaping ``waitid`` on the leader.

    Only an exit, a kill or a dump ends the leader. A tracer sees a traced
    child's stops whatever the wait options, so ``CLD_TRAPPED`` and
    ``CLD_STOPPED`` are stops, not ends. Any other code is not a result of
    this call and raises.
    """
    if si_code == os.CLD_EXITED:
        return LeaderExited(si_status)
    if si_code in (os.CLD_KILLED, os.CLD_DUMPED):
        return LeaderExited(-si_status)
    if si_code in (os.CLD_TRAPPED, os.CLD_STOPPED):
        return LeaderStopped()
    raise ValueError(f"not a waitid report for an exit or a stop: si_code={si_code}")


def reap_outcome(reaped_pid: int, wait_status: int) -> LeaderReaped | LeaderNotReaped:
    """Classify the result of the final ``waitpid`` on the leader.

    Only a status with ``WIFEXITED`` or ``WIFSIGNALED`` reaped the leader; pid
    0, a stop and a continue did not. Any other status raises.
    """
    if reaped_pid == 0:
        return LeaderNotReaped()
    if os.WIFEXITED(wait_status) or os.WIFSIGNALED(wait_status):
        return LeaderReaped(os.waitstatus_to_exitcode(wait_status))
    if os.WIFSTOPPED(wait_status) or os.WIFCONTINUED(wait_status):
        return LeaderNotReaped()
    raise ValueError(f"not a waitpid status: {wait_status:#x}")


def advance(status: LeaderStatus, event: LeaderEvent) -> LeaderTransition:
    """The leader's whole lifecycle: one transition per status and event.

    RELEASED and LOST absorb every event, so a delayed observation cannot
    restore an identity the handle no longer holds. The announcement is true
    exactly when the state leaves RUNNING, so it comes once on every path to
    the end, a loss included. A return code is only ever one the kernel
    reported for this child: a loss from RUNNING has none.
    """
    unchanged = LeaderTransition(status, announces=False)
    match status.state, event:
        case AnchorState.RUNNING, LeaderExited(returncode):
            return LeaderTransition(LeaderStatus(AnchorState.EXITED, returncode), announces=True)
        case AnchorState.RUNNING, LeaderReaped(returncode):
            return LeaderTransition(LeaderStatus(AnchorState.RELEASED, returncode), announces=True)
        case AnchorState.RUNNING, LeaderGone():
            return LeaderTransition(LeaderStatus(AnchorState.LOST, None), announces=True)
        case AnchorState.EXITED, LeaderReaped(returncode):
            return LeaderTransition(LeaderStatus(AnchorState.RELEASED, returncode), announces=False)
        case AnchorState.EXITED, LeaderGone():
            return LeaderTransition(
                LeaderStatus(AnchorState.LOST, status.returncode), announces=False
            )
        case _:
            return unchanged


def admit_group_signal(state: AnchorState) -> None:
    if state is AnchorState.RELEASED:
        raise AnchorStateError(
            "the leader was released; its process group number is no longer held"
        )
    if state is AnchorState.LOST:
        raise ChildIdentityLost(
            "the leader was reaped elsewhere; its process group number is not safe"
        )


def admit_release(state: AnchorState) -> bool:
    if state is AnchorState.LOST:
        raise ChildIdentityLost("the leader was reaped elsewhere; there is nothing left to release")
    return state in _ANCHORED_STATES


_IDENTITY_LOST: Final = object()


def _wait_for_exit(pid: int) -> tuple[int, int]:
    reported = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
    return reported.si_code, reported.si_status


def _consume_stop_report(pid: int) -> tuple[int, int] | None:
    reported = cast("os.waitid_result | None", os.waitid(os.P_PID, pid, os.WSTOPPED | os.WNOHANG))
    if reported is None:
        return None
    return reported.si_code, reported.si_status


class AnchoredLeader:
    """A leader that only this handle observes; its exit leaves an unreaped zombie.

    One ``child-exit:<pid>`` thread waits without reaping. Its commit of the exit
    (or of a loss) is its last act on the pid, so whatever reaps later finds the
    watcher gone. Every status change goes through :func:`advance` under one lock;
    ``on_exit`` is called after the commit, outside the lock, exactly when the
    transition announces.

    A loss writes a private sentinel into ``Popen.returncode``. ``Popen``'s
    destructor and ``subprocess._cleanup`` poll only while it is ``None``, so a
    lost handle never lets them wait on a number that may name another process.
    Dropping a handle that is still RUNNING or EXITED is a caller defect: CPython
    then reaps the leader as an ordinary child.

    ``done_with_pid`` is a conservative certificate. True certifies that the
    handle is RELEASED or LOST and its watcher ended, so it will never act on its
    pid again. False certifies nothing: it never waits, so a held lifecycle lock
    reads false even for a handle that is already done.
    """

    def __init__(self, popen: subprocess.Popen[bytes], on_exit: Callable[[], None]) -> None:
        self.pid = popen.pid
        self._popen = popen
        self._on_exit = on_exit
        self._lock = threading.Lock()
        self._status = LEADER_STARTED
        self._stop_warned = False
        self._watcher = threading.Thread(
            target=self._watch, name=f"child-exit:{self.pid}", daemon=True
        )

    @property
    def state(self) -> AnchorState:
        with self._lock:
            return self._status.state

    @property
    def returncode(self) -> int | None:
        with self._lock:
            return self._status.returncode

    @property
    def done_with_pid(self) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        try:
            terminal = self._status.state in (AnchorState.RELEASED, AnchorState.LOST)
        finally:
            self._lock.release()
        return terminal and not self._watcher.is_alive()

    def _begin_watching(self) -> None:
        self._watcher.start()

    def _watch(self) -> None:
        try:
            while True:
                observation = exit_observation(*_wait_for_exit(self.pid))
                if isinstance(observation, LeaderExited):
                    break
                self._warn_once_that_the_leader_is_stopped()
                _consume_stop_report(self.pid)
        except ChildProcessError:
            self._commit(LeaderGone())
            return
        self._commit(observation)

    def _warn_once_that_the_leader_is_stopped(self) -> None:
        if self._stop_warned:
            return
        self._stop_warned = True
        log.warning(
            "anchored leader %d is stopped or traced; it stays anchored and "
            "SIGTERM is not delivered while it is stopped",
            self.pid,
        )

    def _commit(self, event: LeaderEvent) -> None:
        with self._lock:
            transition = advance(self._status, event)
            if transition.status.state is AnchorState.LOST:
                self._popen.returncode = _IDENTITY_LOST
            self._status = transition.status
        if transition.announces:
            self._announce()

    def _announce(self) -> None:
        try:
            self._on_exit()
        except Exception as failure:
            log.warning("on_exit of anchored leader %d raised %s", self.pid, type(failure).__name__)


def start_anchored_leader(
    child: ChildProcess,
    *,
    stdin: int,
    stdout: int,
    stderr: int,
    on_exit: Callable[[], None],
) -> AnchoredLeader:
    """Start one child in its own session that no asyncio watcher knows.

    The descriptors are the caller's and stay open. A failed start raises the
    ``Popen`` error unchanged and starts no thread.
    """
    popen = subprocess.Popen(
        child.command,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=dict(child.environment),
        cwd=str(child.working_directory),
        start_new_session=True,
    )
    leader = AnchoredLeader(popen, on_exit)
    leader._begin_watching()
    return leader
