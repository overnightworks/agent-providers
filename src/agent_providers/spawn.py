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
on any child, ``SIGCHLD`` set to ``SIG_IGN`` (which the start refuses),
``SA_NOCLDWAIT``, the deprecated
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
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import IO, Final, Literal, cast

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
    """A child or run was specified with a value its contract refuses."""


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
    environment: Mapping[str, str] = field(repr=False)
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


def _reap_exited(pid: int) -> tuple[int, int]:
    return os.waitpid(pid, os.WNOHANG)


def _reap_after_kill(pid: int) -> tuple[int, int]:
    return os.waitpid(pid, 0)


def _lifecycle_lock() -> threading.Lock:
    return threading.Lock()


def _ownership_gate() -> threading.Lock:
    return threading.Lock()


class _WatcherOwnership(Enum):
    PENDING = "pending"
    OWNED = "owned"
    ABANDONED = "abandoned"


class _WatcherHandoff:
    """The start's one decision about its watcher, which the watcher awaits before any pid call.

    The gate is shut at creation and opens only through :meth:`decide`, so a watcher
    that never reads a decision never touches the pid.
    """

    __slots__ = ("_gate", "_ownership")

    def __init__(self) -> None:
        self._ownership = _WatcherOwnership.PENDING
        self._gate = _ownership_gate()
        self._gate.acquire()

    def decide(self, ownership: _WatcherOwnership) -> None:
        self._ownership = ownership
        self._gate.release()

    def await_decision(self) -> _WatcherOwnership:
        self._gate.acquire()
        return self._ownership


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

    ``release()`` is the one final reap. It reaps only a leader whose exit the
    watcher already published (EXITED), never a running or stopped one, so no
    wait of the watcher can follow the reap: the watcher calls the kernel only
    while RUNNING and its commit is its last call. The exit is published by the
    commit and announced by ``on_exit`` after it, so ``release()`` is admitted
    from the commit on, including from inside ``on_exit``. The reap and the
    commit that follows it happen under the lifecycle lock, so concurrent
    releases reap once. RELEASED writes the reaped code into ``Popen.returncode``
    and a loss writes the sentinel, both through the one lock-held write, so a
    destroyed handle reaps nothing.

    ``done_with_pid`` is a conservative certificate. True certifies that the
    handle is RELEASED or LOST and its watcher ended, so it will never act on its
    pid again. False certifies nothing: it never waits, so a held lifecycle lock
    reads false even for a handle that is already done.

    ``signal_group`` sends a signal to the leader's process group inside the
    lifecycle lock, only while the leader is anchored (RUNNING or EXITED), so no
    ``release()`` can free the number between the admission and the signal. After
    RELEASED or LOST it refuses without a kernel call. ``ESRCH`` means that
    something else reaped the leader: it is committed as a loss and raises
    :class:`ChildIdentityLost`. Any other ``OSError`` propagates unchanged and
    leaves the handle as it was. It waits for nothing and never reaps.

    A loss committed by ``signal_group`` can overtake a stop report the watcher
    holds, so after each stop report the watcher reads the state under the lock
    and ends without a warning, a consume or a further wait unless it is still
    RUNNING.

    The watcher acts on its pid only once the start handed it ownership. A start
    whose watcher launch raised abandons the watcher first; an abandoned watcher
    ends without touching the pid.
    """

    def __init__(self, popen: subprocess.Popen[bytes], on_exit: Callable[[], None]) -> None:
        self.pid = popen.pid
        self._popen = popen
        self._on_exit = on_exit
        self._lock = _lifecycle_lock()
        self._status = LEADER_STARTED
        self._stop_warned = False
        self._handoff = _WatcherHandoff()
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
            terminal = self._status.state not in _ANCHORED_STATES
        finally:
            self._lock.release()
        return terminal and not self._watcher.is_alive()

    def _begin_watching(self) -> None:
        self._watcher.start()

    def _watch(self) -> None:
        if self._handoff.await_decision() is not _WatcherOwnership.OWNED:
            return
        try:
            while True:
                observation = exit_observation(*_wait_for_exit(self.pid))
                if isinstance(observation, LeaderExited):
                    break
                if self.state is not AnchorState.RUNNING:
                    return
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

    def release(self) -> None:
        with self._lock:
            if not admit_release(self._status.state):
                return
            if self._status.state is AnchorState.RUNNING:
                raise AnchorStateError("the leader's exit is not published; it cannot be released")
            try:
                event: LeaderEvent = reap_outcome(*_reap_exited(self.pid))
            except ChildProcessError:
                event = LeaderGone()
            self._apply(event)
            if isinstance(event, LeaderNotReaped):
                raise AnchorStateError("the kernel did not reap the exited leader")
            if isinstance(event, LeaderGone):
                raise ChildIdentityLost("the leader was reaped elsewhere; nothing is left")

    def signal_group(self, sig: signal.Signals) -> None:
        with self._lock:
            admit_group_signal(self._status.state)
            try:
                os.killpg(self.pid, sig)
                return
            except ProcessLookupError:
                transition = self._apply(LeaderGone())
        self._announce_when(transition)
        raise ChildIdentityLost("the leader was reaped elsewhere; its process group is gone")

    def _kill_and_reap(self) -> LeaderEvent:
        with self._lock:
            try:
                os.killpg(self.pid, signal.SIGKILL)
                event: LeaderEvent = self._reap_until_terminal()
            except (ProcessLookupError, ChildProcessError):
                event = LeaderGone()
            self._apply(event)
        return event

    def _reap_until_terminal(self) -> LeaderReaped:
        while True:
            outcome = reap_outcome(*_reap_after_kill(self.pid))
            if isinstance(outcome, LeaderReaped):
                return outcome

    def _apply(self, event: LeaderEvent) -> LeaderTransition:
        transition = advance(self._status, event)
        state = transition.status.state
        if state is AnchorState.LOST:
            self._popen.returncode = _IDENTITY_LOST
        elif state is AnchorState.RELEASED:
            self._popen.returncode = transition.status.returncode
        self._status = transition.status
        return transition

    def _commit(self, event: LeaderEvent) -> None:
        with self._lock:
            transition = self._apply(event)
        self._announce_when(transition)

    def _announce_when(self, transition: LeaderTransition) -> None:
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
    ``Popen`` error unchanged and starts no thread. An ignored ``SIGCHLD`` is
    refused with :class:`AnchorStateError` before any child exists, because the
    kernel would reap the leader at its exit.

    Any raise from the watcher's launch, whether or not a thread exists, abandons
    the watcher, kills the leader's group, reaps the leader through its own pid
    and re-raises the error unchanged; a loss during that rollback raises
    :class:`ChildIdentityLost` from the error. The rollback's blocking ``waitpid``
    has no guaranteed elapsed-time bound, because a child in an uninterruptible
    kernel wait ends only when that wait ends. A resource failure such as
    ``MemoryError`` inside the ownership handoff or the rollback, like an
    asynchronous interruption, can leave the child and a parked watcher until the
    process ends; the host then stops using the library and ends the process.
    """
    if signal.getsignal(signal.SIGCHLD) is signal.SIG_IGN:
        raise AnchorStateError("SIGCHLD is ignored; the kernel would reap the leader at its exit")
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
    try:
        leader._begin_watching()
    except BaseException as launch_failure:
        leader._handoff.decide(_WatcherOwnership.ABANDONED)
        if isinstance(leader._kill_and_reap(), LeaderGone):
            raise ChildIdentityLost(
                "the leader was lost while its start was rolled back"
            ) from launch_failure
        raise
    else:
        leader._handoff.decide(_WatcherOwnership.OWNED)
    return leader


type _ParentTransport = asyncio.ReadTransport | asyncio.WriteTransport


class _DoorPipes:
    """The descriptors and transports of one door, each with exactly one closer.

    A parent end lives in an unbuffered file object that this class closes, so a
    close never flushes or blocks and never leaves the closing to a transport's
    later loop turn. A child end is closed by :meth:`close_child_ends` once the
    start returned, or by :meth:`close_parent_side`'s caller on a raise.
    """

    def __init__(self) -> None:
        self._parent_files: list[IO[bytes]] = []
        self._unwrapped_parent_ends: list[int] = []
        self._child_ends: list[int] = []
        self._transports: list[_ParentTransport] = []
        self._write_transport: asyncio.WriteTransport | None = None

    def open_pipe(self, *, parent_reads: bool) -> tuple[IO[bytes], int]:
        """Create one pipe whose both ends the next cleanup closes.

        Both descriptors are registered in the statement after ``os.pipe()``
        returns, so any later synchronous failure closes them; only an
        asynchronous interruption between the two statements can leak them, which
        is the host's Interruptions obligation.
        """
        read_end, write_end = os.pipe()
        parent_end, child_end = (read_end, write_end) if parent_reads else (write_end, read_end)
        self._child_ends.append(child_end)
        self._unwrapped_parent_ends.append(parent_end)
        parent_file = os.fdopen(parent_end, "rb" if parent_reads else "wb", buffering=0)
        self._unwrapped_parent_ends.remove(parent_end)
        self._parent_files.append(parent_file)
        return parent_file, child_end

    async def attach_reader(
        self, loop: asyncio.AbstractEventLoop, pipe: IO[bytes], stream_buffer_limit: int | None
    ) -> asyncio.StreamReader:
        buffer_limit = {} if stream_buffer_limit is None else {"limit": stream_buffer_limit}
        reader = asyncio.StreamReader(loop=loop, **buffer_limit)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
        self._transports.append(transport)
        return reader

    async def attach_writer(
        self, loop: asyncio.AbstractEventLoop, pipe: IO[bytes]
    ) -> asyncio.StreamWriter:
        protocol = asyncio.StreamReaderProtocol(None, loop=loop)
        transport, _ = await loop.connect_write_pipe(lambda: protocol, pipe)
        self._transports.append(transport)
        self._write_transport = transport
        return asyncio.StreamWriter(transport, protocol, None, loop)

    def close_child_ends(self) -> None:
        child_ends, self._child_ends = self._child_ends, []
        for child_end in child_ends:
            os.close(child_end)

    def close_parent_side(self, loop: asyncio.AbstractEventLoop) -> None:
        transports, self._transports = self._transports, []
        parent_files, self._parent_files = self._parent_files, []
        unwrapped_ends, self._unwrapped_parent_ends = self._unwrapped_parent_ends, []
        for unwrapped_end in unwrapped_ends:
            os.close(unwrapped_end)
        if not loop.is_closed():
            for transport in transports:
                self._close_transport(transport)
        for parent_file in parent_files:
            parent_file.close()

    def _close_transport(self, transport: _ParentTransport) -> None:
        if transport is not self._write_transport:
            if not transport.is_closing():
                transport.close()
        elif not transport.is_closing() or transport.get_write_buffer_size() > 0:
            transport.abort()


class AnchoredChild:
    """The async face of an :class:`AnchoredLeader`: three pipes, an exit event, no reaper.

    Only :func:`open_anchored_pipes` creates one. ``pid``, ``returncode``,
    ``done_with_pid`` and ``signal_group`` are the leader's, unchanged; ``stdin`` is
    a writer whose ``drain()`` and ``wait_closed()`` work as on asyncio's door,
    ``stdout`` and ``stderr`` are readers.

    ``exited()`` returns the leader's code once the exit is published. It
    waits on an event, so a cancelled or timed-out waiter harms nobody and every
    later or concurrent waiter gets the same result; it raises
    :class:`ChildIdentityLost` when the leader was lost while running, because no
    code exists then. ``communicate`` mirrors asyncio's: it writes ``input`` when
    given, drains, ignores ``BrokenPipeError`` and ``ConnectionResetError`` from
    those two, and always closes ``stdin``, while reading both streams to EOF
    concurrently, then awaits ``exited()``. It never reaps.

    The exited leader stays an unreaped zombie, which anchors its process-group
    number, until ``release()``. ``release()`` reaps through the leader, which
    refuses a running one with :class:`AnchorStateError` and then closes nothing,
    because the child may still use its pipes. Once the leader is released or
    lost, whatever its call raised, the pipes are closed once, each transport
    before its file object. On an open loop a reading transport that is not
    closing is closed. The ``stdin`` transport is aborted when it is not closing
    or when it is closing but still holds buffered output, which the abort
    discards; a closing transport with an empty buffer has its one
    ``connection_lost`` queued or done already and is left alone. This relies on
    CPython queuing ``connection_lost`` only once the buffer is empty. On a closed
    loop transports cannot be closed, so only the file objects are closed, buffered
    output is lost with the loop and asyncio's transport destructor warns with its
    own ``ResourceWarning``. A second ``release()`` does nothing.

    The start runs on the event-loop thread, as asyncio's own door runs ``Popen``.
    ``Popen`` blocks that loop until the child has executed its binary or failed,
    and the start's rollback after a watcher-launch error blocks it in ``waitpid``
    until the killed child is gone. Neither wait has a guaranteed elapsed-time
    bound, no other task of the loop runs meanwhile, and an ``asyncio.timeout``
    cannot shorten either.

    Host obligations, each answered by stopping the use of the library and ending
    the process (README, Interruptions): an allocation failure in the leader's
    construction after ``Popen`` leaves a child nobody signals; a resource failure
    in the start's hand-over or rollback, or an asynchronous interruption during
    the start, can leave a parked watcher holding the leader and the child; a
    resource failure or interruption inside the door's own cleanup can leave a
    descriptor or transport of the door open; a resource failure or interruption
    after the start returned and before the handle was returned leaves a running
    leader that no caller holds.
    """

    def __init__(
        self,
        leader: AnchoredLeader,
        exit_published: asyncio.Event,
        pipes: _DoorPipes,
        loop: asyncio.AbstractEventLoop,
        *,
        stdin: asyncio.StreamWriter,
        stdout: asyncio.StreamReader,
        stderr: asyncio.StreamReader,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._leader = leader
        self._exit_published = exit_published
        self._pipes = pipes
        self._loop = loop

    @property
    def pid(self) -> int:
        return self._leader.pid

    @property
    def returncode(self) -> int | None:
        return self._leader.returncode

    @property
    def done_with_pid(self) -> bool:
        return self._leader.done_with_pid

    def signal_group(self, sig: signal.Signals) -> None:
        self._leader.signal_group(sig)

    async def exited(self) -> int:
        await self._exit_published.wait()
        returncode = self._leader.returncode
        if returncode is None:
            raise ChildIdentityLost("the leader was lost before its exit was published")
        return returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        _, stdout, stderr = await asyncio.gather(
            self._feed_stdin(input), self.stdout.read(), self.stderr.read()
        )
        await self.exited()
        return stdout, stderr

    async def _feed_stdin(self, input: bytes | None) -> None:
        try:
            if input is not None:
                self.stdin.write(input)
            await self.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.stdin.close()

    def release(self) -> None:
        try:
            self._leader.release()
        finally:
            if self._leader.state not in _ANCHORED_STATES:
                self._pipes.close_parent_side(self._loop)


async def open_anchored_pipes(
    child: ChildProcess,
    *,
    stream_buffer_limit: int | None = None,
) -> AnchoredChild:
    """Start one child on the event loop with pipes on every stream, anchored.

    All three pipes are attached before the child exists. Those attachments are the
    door's only ``await``; the start then runs synchronously on the loop thread
    and the handle is returned with no ``await`` after it.

    Any ``BaseException`` before the return, cancellation included, is answered
    synchronously and re-raised unchanged: every transport that is not closing is
    closed, every parent file object and every child end is closed, so the door's
    descriptors are closed when the caller's handler runs. That holds when the
    cleanup itself completed and, for a raise from the watcher launch, when the
    start's rollback completed; the residuals outside it are the host obligations
    on :class:`AnchoredChild`, as is the loop-thread wait of the start.
    """
    loop = asyncio.get_running_loop()
    exit_published = asyncio.Event()
    pipes = _DoorPipes()
    try:
        stdout_file, stdout_child_end = pipes.open_pipe(parent_reads=True)
        stderr_file, stderr_child_end = pipes.open_pipe(parent_reads=True)
        stdin_file, stdin_child_end = pipes.open_pipe(parent_reads=False)
        stdout = await pipes.attach_reader(loop, stdout_file, stream_buffer_limit)
        stderr = await pipes.attach_reader(loop, stderr_file, stream_buffer_limit)
        stdin = await pipes.attach_writer(loop, stdin_file)
        leader = start_anchored_leader(
            child,
            stdin=stdin_child_end,
            stdout=stdout_child_end,
            stderr=stderr_child_end,
            on_exit=lambda: loop.call_soon_threadsafe(exit_published.set),
        )
        pipes.close_child_ends()
        return AnchoredChild(
            leader, exit_published, pipes, loop, stdin=stdin, stdout=stdout, stderr=stderr
        )
    except BaseException:
        pipes.close_child_ends()
        pipes.close_parent_side(loop)
        raise
