"""One subscription run of any provider's CLI, in its own directories.

``astream_cli_run`` makes the run's directories, starts the adapter's child on
:func:`anchored_turn_child`, writes its stdin, turns each stdout line into events
through the adapter's decoder and drains stderr unkept. The group is confirmed
gone and the tree's removal attempted before the ending is yielded; a group that
cannot be confirmed raises ``ContainmentLost`` instead of an answer. Nothing here
names a provider or reads installed configuration.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from agent_providers.anchored_turn import CliProcessPool, anchored_turn_child
from agent_providers.config import PrivateWorkingRoot
from agent_providers.errors import raise_without_chain
from agent_providers.events import (
    AssistantTextEvent,
    FinalEvent,
    StreamEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_providers.process import run_directories
from agent_providers.spawn import ChildProcess

StepEvent = AssistantTextEvent | ToolCallEvent | ToolResultEvent

_STDERR_CHUNK_BYTES = 64 * 1024


class CliStreamLineTooLong(Exception):
    """A CLI output line exceeded its limit; it carries none of the line."""


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


async def _settle(drain: asyncio.Task[int]) -> None:
    drain.cancel()
    await asyncio.gather(drain, return_exceptions=True)


async def _next_line(stdout: asyncio.StreamReader) -> bytes:
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
        async with anchored_turn_child(
            adapter.pool,
            start.child,
            home_reservation=directories.reserve(),
            stream_buffer_limit=adapter.line_limit_bytes,
        ) as proc:
            drain = asyncio.create_task(drain_without_keeping(proc.stderr))
            try:
                if start.stdin:
                    proc.stdin.write(start.stdin)
                    await proc.stdin.drain()
                proc.stdin.close()
                while line := await _next_line(proc.stdout):
                    for event in start.decoder.events_of(line):
                        yield event
                returncode = await proc.exited()
            finally:
                await _settle(drain)
    yield start.decoder.ending(returncode)
