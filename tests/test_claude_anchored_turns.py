"""The async Claude turns against a real leader: the real door, the real reap, the real kernel.

The leader is a shell script. It starts no further process, except in the rows
whose script builder ``lingering_descendant`` starts one descendant on purpose. No
row here enters ``killpg_forbidden``, because a group signal must reach the real anchor.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import os
import shlex
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from anchored_kernel import WatcherStarts, join_watcher, recorded_watcher_starts
from anchored_leaders import spawn_warnings
from anchored_support import (
    GONE,
    TEST_WAIT_SECONDS,
    ChildCleanup,
    pidfd_open,
    pidfd_send_signal,
    wait_for_exits,
)
from provider_test_support import EventGate, ScenarioDeadline, held_claude_slots, run_bounded

from agent_providers.claude import provider
from agent_providers.events import FinalEvent, StreamEvent

CONSUMES_STDIN = "while IFS= read -r line; do :; done\n"
ANSWERS_ONCE = "printf '%s' '{\"result\":\"ok\"}'\n"
STREAMS_A_RESULT = 'printf \'%s\\n\' \'{"type":"result","result":"ok"}\'\n'
READS_STDIN_AND_ANSWERS_ONCE = CONSUMES_STDIN + ANSWERS_ONCE
READS_STDIN_AND_STREAMS_A_RESULT = CONSUMES_STDIN + STREAMS_A_RESULT
WAITS_WITHOUT_READING_STDIN = "exec sleep 60\n"


@pytest.fixture
def leader_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    def install(script: str) -> None:
        leader = tmp_path / "bin" / "claude"
        leader.parent.mkdir()
        leader.write_text(f"#!/bin/sh\n{script}")
        leader.chmod(0o700)
        for gate in ("averify_no_builtin_cli_tools", "verify_cli_tool_surface"):
            monkeypatch.setattr(provider, gate, AsyncMock(return_value=leader))
        monkeypatch.setattr(provider, "_write_mcp_config", lambda _spec, _user_id: "unused")
        monkeypatch.setattr(provider, "_unlink_quiet", lambda _path: None)

    return install


@pytest.fixture
def drive_the_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_cleanup: ChildCleanup,
    thread_failures: list[Any],
    caplog: pytest.LogCaptureFixture,
) -> Callable[..., Any]:
    def drive(turn: Callable[[], Awaitable[Any]], *, leader_exits_with: int) -> Any:
        with recorded_watcher_starts(monkeypatch) as watchers:
            try:
                result = child_cleanup.capture(lambda: run_bounded(turn())).value
            finally:
                for owned in child_cleanup.listed():
                    join_watcher(watchers.thread(owned.pid))
        (owned,) = child_cleanup.listed()
        assert child_cleanup.state(owned) is GONE
        assert owned.popen.returncode == leader_exits_with
        assert held_claude_slots() == 0
        assert list(tmp_path.glob("agent-cli-claude-turn-*")) == []
        assert thread_failures == []
        assert spawn_warnings(caplog) == []
        return result

    return drive


def test_a_real_cli_one_shot_ends_with_its_leader_reaped_and_its_slot_and_home_freed(
    leader_running,
    drive_the_turn,
) -> None:
    leader_running(READS_STDIN_AND_ANSWERS_ONCE)

    async def turn() -> str:
        return (await provider.acall_claude("hi")).text

    assert drive_the_turn(turn, leader_exits_with=0) == "ok"


async def _last_event_of_the_stream() -> StreamEvent:
    stream = provider.acall_claude_with_mcp_stream("hi", user_id="u-1")
    async with contextlib.aclosing(stream):
        events = [event async for event in stream]
    return events[-1]


def test_a_real_stream_ends_with_its_leader_reaped_and_its_slot_and_home_freed(
    leader_running,
    drive_the_turn,
) -> None:
    leader_running(READS_STDIN_AND_STREAMS_A_RESULT)

    assert drive_the_turn(_last_event_of_the_stream, leader_exits_with=0) == FinalEvent(text="ok")


def test_a_real_timeout_ends_its_leader_by_sigterm_through_the_anchor(
    leader_running,
    drive_the_turn,
) -> None:
    leader_running(WAITS_WITHOUT_READING_STDIN)

    async def turn() -> Exception:
        try:
            await provider.acall_claude_with_mcp("hi", user_id="u-1", timeout_seconds=0)
        except provider.UnavailableError as timed_out:
            return timed_out
        raise AssertionError("the turn did not time out")

    timed_out = drive_the_turn(turn, leader_exits_with=-signal.SIGTERM)
    assert "timed out" in str(timed_out)


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


@pytest.fixture
def handshake(tmp_path: Path) -> Handshake:
    paths = Handshake(*(tmp_path / name for name in ("descendant.py", "identity", "ack", "ready")))
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


class HarnessInterrupted(Exception):
    pass


class Stage(enum.Enum):
    IDENTITY_READ = enum.auto()
    PIDFD_REGISTERED = enum.auto()


class AfterHandshake(enum.Enum):
    AWAIT_THE_TURN = enum.auto()
    CANCEL_THE_TURN = enum.auto()
    EXHAUST_THE_BUDGET = enum.auto()


@dataclass(frozen=True)
class RowOutcome:
    settled: Any
    timed_out: bool


class DescendantRow:
    """One real turn with a lingering descendant whose death the test proves by itself.

    The descendant is named by a validated pidfd before it is acknowledged, so it
    can neither escape the turn's group nor outlive the row unowned.
    """

    def __init__(
        self,
        handshake: Handshake,
        child_cleanup: ChildCleanup,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._handshake = handshake
        self._child_cleanup = child_cleanup
        self._monkeypatch = monkeypatch
        self.execution = ScenarioDeadline()
        self._identity_fd = os.open(handshake.identity, os.O_RDONLY | os.O_NONBLOCK)
        self._ack_fd = os.open(handshake.ack, os.O_RDWR)
        child_cleanup.close_at_cleanup(self._identity_fd)
        child_cleanup.close_at_cleanup(self._ack_fd)
        self._watchers: WatcherStarts | None = None
        self._pid: int | None = None
        self._start_time: str | None = None
        self._pidfd: int | None = None
        self.cleanup_budget_at_start: float | None = None

    def run(
        self,
        turn: Callable[[], Awaitable[Any]],
        *,
        after: AfterHandshake = AfterHandshake.AWAIT_THE_TURN,
        interrupt_at: Stage | None = None,
    ) -> RowOutcome:
        with recorded_watcher_starts(self._monkeypatch) as watchers:
            self._watchers = watchers
            drive = self._drive(turn, after, interrupt_at)
            return self._child_cleanup.capture(lambda: run_bounded(drive)).value

    async def _drive(
        self,
        turn: Callable[[], Awaitable[Any]],
        after: AfterHandshake,
        interrupt_at: Stage | None,
    ) -> RowOutcome:
        turn_task = asyncio.ensure_future(turn())
        timed_out = False
        try:
            await self._own_the_descendant(interrupt_at)
            if after is AfterHandshake.CANCEL_THE_TURN:
                turn_task.cancel()
            if after is AfterHandshake.EXHAUST_THE_BUDGET:
                await asyncio.sleep(self.execution.remaining())
            done, _pending = await asyncio.wait({turn_task}, timeout=self.execution.remaining())
            timed_out = not done
        finally:
            turn_task.cancel()
            (settled,) = await asyncio.gather(turn_task, return_exceptions=True)
        return RowOutcome(settled, timed_out)

    async def _own_the_descendant(self, interrupt_at: Stage | None) -> None:
        identity_line = await self._identity_line()
        pid, self._start_time = identity_line.split()
        self._pid = int(pid)
        if interrupt_at is Stage.IDENTITY_READ:
            raise HarnessInterrupted
        pidfd = pidfd_open(self._pid)
        self._child_cleanup.close_at_cleanup(pidfd)
        self._pidfd = pidfd
        assert process_identity(self._pid) is not None
        assert process_identity(self._pid)[1] == self._start_time, "the pid names another process"
        if interrupt_at is Stage.PIDFD_REGISTERED:
            raise HarnessInterrupted
        os.write(self._ack_fd, b"x")

    async def _identity_line(self) -> str:
        loop = asyncio.get_running_loop()
        published = EventGate(self.execution)
        loop.add_reader(self._identity_fd, published.release.set)
        try:
            await published.pass_through()
        finally:
            loop.remove_reader(self._identity_fd)
        assert not published.missed.is_set(), "the descendant never published its identity"
        return os.read(self._identity_fd, 100).decode()

    def descendant_is_dead(self) -> bool:
        """Dead by its pidfd, or by the recorded identity: gone, a zombie, or another start time."""
        assert self._pid is not None
        if self._pidfd is not None and self._pidfd in wait_for_exits([self._pidfd], 0):
            return True
        identity = process_identity(self._pid)
        return identity is None or identity[0] in "ZX" or identity[1] != self._start_time

    def clean_up(self) -> None:
        cleanup = ScenarioDeadline()
        self.cleanup_budget_at_start = cleanup.remaining()
        if self._pid is not None:
            self._kill_the_descendant(cleanup)
        assert self._watchers is not None
        for owned in self._child_cleanup.listed():
            join_watcher(self._watchers.thread(owned.pid), cleanup.remaining())

    def _kill_the_descendant(self, cleanup: ScenarioDeadline) -> None:
        if self._pidfd is not None:
            with contextlib.suppress(ProcessLookupError):
                pidfd_send_signal(self._pidfd, signal.SIGKILL)
            assert self._pidfd in wait_for_exits([self._pidfd], cleanup.remaining())
            return
        while not self.descendant_is_dead():
            assert cleanup.remaining() > 0, "the descendant outlived the cleanup budget"
            time.sleep(0.01)

    def assert_the_turn_left_nothing(
        self,
        tmp_path: Path,
        thread_failures: list[Any],
        caplog: pytest.LogCaptureFixture,
        *,
        leader_exited_with: int,
    ) -> None:
        (owned,) = self._child_cleanup.listed()
        assert self._child_cleanup.state(owned) is GONE
        assert owned.popen.returncode == leader_exited_with
        assert held_claude_slots() == 0
        assert list(tmp_path.glob("agent-cli-claude-turn-*")) == []
        assert thread_failures == []
        assert spawn_warnings(caplog) == []


@pytest.fixture
def descendant_row(
    handshake: Handshake, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch,
) -> DescendantRow:
    return DescendantRow(handshake, child_cleanup, monkeypatch)


async def _answer_of_a_one_shot() -> str:
    return (await provider.acall_claude("hi")).text


async def _answer_of_an_mcp_one_shot() -> str:
    return (await provider.acall_claude_with_mcp("hi", user_id="u-1")).text


async def _failure_of_a_one_shot() -> BaseException:
    try:
        await provider.acall_claude("hi")
    except Exception as failure:
        return failure
    raise AssertionError("the one-shot did not fail")


@dataclass(frozen=True)
class LingeringTurn:
    entry: Callable[[], Awaitable[Any]]
    answer: str
    exit_code: int
    blocks: bool
    after: AfterHandshake
    leader_exited_with: int
    settled_as: Callable[[Any], bool]


LINGERING_TURNS = {
    "acall_claude_success": LingeringTurn(
        _answer_of_a_one_shot, ANSWERS_ONCE, 0, False, AfterHandshake.AWAIT_THE_TURN, 0,
        lambda settled: settled == "ok",
    ),
    "acall_claude_cli_failure": LingeringTurn(
        _failure_of_a_one_shot, ANSWERS_ONCE, 1, False, AfterHandshake.AWAIT_THE_TURN, 1,
        lambda settled: isinstance(settled, Exception),
    ),
    "acall_claude_with_mcp_success": LingeringTurn(
        _answer_of_an_mcp_one_shot, ANSWERS_ONCE, 0, False, AfterHandshake.AWAIT_THE_TURN, 0,
        lambda settled: settled == "ok",
    ),
    "acall_claude_with_mcp_cancelled": LingeringTurn(
        _answer_of_an_mcp_one_shot, "", 0, True, AfterHandshake.CANCEL_THE_TURN, -signal.SIGTERM,
        lambda settled: isinstance(settled, asyncio.CancelledError),
    ),
    "acall_claude_with_mcp_stream_result": LingeringTurn(
        _last_event_of_the_stream, STREAMS_A_RESULT, 0, False, AfterHandshake.AWAIT_THE_TURN, 0,
        lambda settled: settled == FinalEvent(text="ok"),
    ),
}


@pytest.mark.parametrize("turn", LINGERING_TURNS.values(), ids=LINGERING_TURNS)
def test_a_lingering_descendant_is_dead_when_the_real_turn_ends(
    turn: LingeringTurn,
    leader_running,
    handshake: Handshake,
    descendant_row: DescendantRow,
    tmp_path: Path,
    thread_failures: list[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    leader_running(
        lingering_descendant(
            turn.answer,
            exit_code=turn.exit_code,
            blocks=turn.blocks,
            escapes=False,
            handshake=handshake,
        ),
    )

    try:
        outcome = descendant_row.run(turn.entry, after=turn.after)
        assert not outcome.timed_out
        assert turn.settled_as(outcome.settled), outcome.settled
        assert descendant_row.descendant_is_dead()
    finally:
        descendant_row.clean_up()
    descendant_row.assert_the_turn_left_nothing(
        tmp_path, thread_failures, caplog, leader_exited_with=turn.leader_exited_with,
    )


def test_a_descendant_that_left_the_group_does_not_stop_the_reap(
    leader_running,
    handshake: Handshake,
    descendant_row: DescendantRow,
    tmp_path: Path,
    thread_failures: list[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    leader_running(
        lingering_descendant(
            ANSWERS_ONCE, exit_code=0, blocks=False, escapes=True, handshake=handshake,
        ),
    )

    try:
        outcome = descendant_row.run(_answer_of_a_one_shot)
        assert outcome.settled == "ok"
        assert not descendant_row.descendant_is_dead()
    finally:
        descendant_row.clean_up()
    descendant_row.assert_the_turn_left_nothing(
        tmp_path, thread_failures, caplog, leader_exited_with=0,
    )


INTERRUPTIONS = {
    "after_the_identity_was_read": Stage.IDENTITY_READ,
    "after_the_pidfd_was_registered": Stage.PIDFD_REGISTERED,
}


@pytest.mark.parametrize("stage", INTERRUPTIONS.values(), ids=INTERRUPTIONS)
def test_a_harness_interrupted_before_the_ack_leaves_no_escapee(
    stage: Stage,
    leader_running,
    handshake: Handshake,
    descendant_row: DescendantRow,
    tmp_path: Path,
    thread_failures: list[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    leader_running(
        lingering_descendant(
            ANSWERS_ONCE, exit_code=0, blocks=False, escapes=True, handshake=handshake,
        ),
    )

    try:
        with pytest.raises(HarnessInterrupted):
            descendant_row.run(_answer_of_a_one_shot, interrupt_at=stage)
        assert descendant_row.descendant_is_dead()
    finally:
        descendant_row.clean_up()
    descendant_row.assert_the_turn_left_nothing(
        tmp_path, thread_failures, caplog, leader_exited_with=-signal.SIGTERM,
    )


def test_an_exhausted_execution_budget_still_leaves_the_cleanup_its_own(
    leader_running,
    handshake: Handshake,
    descendant_row: DescendantRow,
    tmp_path: Path,
    thread_failures: list[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    leader_running(
        lingering_descendant("", exit_code=0, blocks=True, escapes=True, handshake=handshake),
    )

    try:
        outcome = descendant_row.run(
            _answer_of_an_mcp_one_shot, after=AfterHandshake.EXHAUST_THE_BUDGET,
        )
        assert outcome.timed_out
        assert not descendant_row.descendant_is_dead()
    finally:
        descendant_row.clean_up()
    assert descendant_row.cleanup_budget_at_start > TEST_WAIT_SECONDS / 2
    descendant_row.assert_the_turn_left_nothing(
        tmp_path, thread_failures, caplog, leader_exited_with=-signal.SIGTERM,
    )
