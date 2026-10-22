"""One subscription run of any provider's CLI, in its own directories.

``astream_cli_run`` makes the run's directories, starts the adapter's child on
:func:`anchored_turn_child`, writes its stdin, turns each stdout line into events
through the adapter's decoder and drains stderr unkept. The group is confirmed
gone and the tree's removal attempted before the ending is yielded; a group that
cannot be confirmed raises ``ContainmentLost`` instead of an answer. A decoder
judges what its CLI announced at the start as a :class:`RunSurface` through
:func:`examine_announcement`, and a refused announcement ends the run with
``RunSurfaceRefused``. :func:`read_line` and :func:`settle_drain` are the one
stdout line reader and the one stderr-drain settlement, which every stream entry
reads and settles through. Nothing here names a provider or reads installed
configuration.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from pathlib import Path
from typing import NoReturn, Protocol

from agent_runtime.anchored_turn import CliProcessPool, anchored_turn_child
from agent_runtime.config import PrivateWorkingRoot
from agent_runtime.errors import raise_without_chain
from agent_runtime.events import (
    AssistantTextEvent,
    FinalEvent,
    StreamEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_runtime.process import run_directories
from agent_runtime.spawn import ChildProcess

log = logging.getLogger(__name__)

StepEvent = AssistantTextEvent | ToolCallEvent | ToolResultEvent

_STDERR_CHUNK_BYTES = 64 * 1024


class CliStreamLineTooLong(Exception):
    """A CLI output line exceeded its limit; it carries none of the line."""


class RelayState(Enum):
    ABSENT = "absent"
    CONNECTED = "connected"
    NOT_CONNECTED = "not_connected"


@dataclass(frozen=True)
class RelayAccess:
    """One run's relay as its adapter attaches it; the secret stays out of `repr`.

    Trusted only as this run's own `Relay.start` built it: the CLI sends the
    `Authorization` header before init, so a host that builds one itself owns the
    trust in its destination (ADR A1b-5r).
    """

    url: str
    secret: str = field(repr=False)


def relay_authorization(secret: str) -> str:
    return f"Bearer {secret}"


@dataclass(frozen=True)
class RunSurface:
    """What a run's CLI announced at its start, classified; it holds no CLI string."""

    foreign_credential: bool
    tool_beside_relay: bool
    server_beside_relay: bool
    relay: RelayState


class SurfaceVerdict(StrEnum):
    ACCEPTED = "accepted"
    FOREIGN_CREDENTIAL = "a credential source beside the profile's login"
    TOOL_BESIDE_RELAY = "a tool the run's relay does not serve"
    SERVER_BESIDE_RELAY = "an MCP server other than the run's relay"
    RELAY_NOT_CONNECTED = "a run's relay that is not connected"
    UNREADABLE = "an unreadable start announcement"
    NOT_ANNOUNCED = "an event before the start announcement"


class RunSurfaceRefused(Exception):
    """A run's start announcement was refused; it carries only the verdict."""

    def __init__(self, verdict: SurfaceVerdict) -> None:
        super().__init__(f"The CLI's start announcement was refused: {verdict.value}.")
        self.verdict = verdict


def verdict_of(surface: RunSurface, *, relay_attached: bool) -> SurfaceVerdict:
    if surface.foreign_credential:
        return SurfaceVerdict.FOREIGN_CREDENTIAL
    if surface.tool_beside_relay:
        return SurfaceVerdict.TOOL_BESIDE_RELAY
    if surface.server_beside_relay or (
        not relay_attached and surface.relay is not RelayState.ABSENT
    ):
        return SurfaceVerdict.SERVER_BESIDE_RELAY
    if relay_attached and surface.relay is not RelayState.CONNECTED:
        return SurfaceVerdict.RELAY_NOT_CONNECTED
    return SurfaceVerdict.ACCEPTED


def refuse_announcement(verdict: SurfaceVerdict) -> NoReturn:
    log.warning("A CLI run's start announcement was refused (%s)", verdict.name)
    raise_without_chain(RunSurfaceRefused(verdict))


def examine_announcement(surface: RunSurface, *, relay_attached: bool) -> None:
    verdict = verdict_of(surface, relay_attached=relay_attached)
    if verdict is not SurfaceVerdict.ACCEPTED:
        refuse_announcement(verdict)


class CliLineDecoder(Protocol):
    def events_of(self, line: bytes) -> Sequence[StepEvent]: ...

    def ending(self, returncode: int) -> FinalEvent: ...


@dataclass(frozen=True)
class CliRunStart:
    child: ChildProcess
    stdin: bytes = field(repr=False)
    decoder: CliLineDecoder


class CliRunAdapter(Protocol):
    @property
    def pool(self) -> CliProcessPool: ...

    @property
    def line_limit_bytes(self) -> int: ...

    def run_files(self) -> Mapping[str, bytes]: ...

    def start(self, home: Path, working_directory: Path) -> CliRunStart: ...


async def drain_without_keeping(stream: asyncio.StreamReader) -> int:
    """Drain a subprocess pipe without retaining its potentially sensitive body."""
    total = 0
    while True:
        chunk = await stream.read(_STDERR_CHUNK_BYTES)
        if not chunk:
            return total
        total += len(chunk)


async def settle_drain(drain: asyncio.Task[int]) -> None:
    drain.cancel()
    await asyncio.gather(drain, return_exceptions=True)


async def read_line(stdout: asyncio.StreamReader) -> bytes:
    try:
        return await stdout.readline()
    except ValueError:
        raise_without_chain(CliStreamLineTooLong("A CLI output line exceeded its limit."))


async def astream_cli_run(
    root: PrivateWorkingRoot, adapter: CliRunAdapter
) -> AsyncIterator[StreamEvent]:
    """Stream one run's events; the ending comes last, after the reap and the removal attempt."""
    with run_directories(root, adapter.run_files()) as directories:
        start = adapter.start(directories.home, directories.working_directory)
        pool = adapter.pool
        stream_buffer_limit = adapter.line_limit_bytes
        async with anchored_turn_child(
            pool,
            start.child,
            home_reservation=directories.reserve(),
            stream_buffer_limit=stream_buffer_limit,
        ) as proc:
            drain = asyncio.create_task(drain_without_keeping(proc.stderr))
            try:
                if start.stdin:
                    proc.stdin.write(start.stdin)
                    await proc.stdin.drain()
                proc.stdin.close()
                while line := await read_line(proc.stdout):
                    for event in start.decoder.events_of(line):
                        yield event
                returncode = await proc.exited()
            finally:
                await settle_drain(drain)
    yield start.decoder.ending(returncode)
