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
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

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
