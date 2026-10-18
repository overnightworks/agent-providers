"""The async Claude turns against a real leader: the real door, the real reap, the real kernel.

The leader is a shell script that starts no further process. No row here enters
``killpg_forbidden``, because a group signal must reach the real anchor.
"""

from __future__ import annotations

import contextlib
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from anchored_kernel import join_watcher, recorded_watcher_starts
from anchored_leaders import spawn_warnings
from anchored_support import GONE, ChildCleanup
from provider_test_support import held_claude_slots, run_bounded

from agent_providers.claude import provider
from agent_providers.events import FinalEvent, StreamEvent

READS_STDIN_AND_ANSWERS_ONCE = "cat >/dev/null\nprintf '%s' '{\"result\":\"ok\"}'\n"
READS_STDIN_AND_STREAMS_A_RESULT = (
    'cat >/dev/null\nprintf \'%s\\n\' \'{"type":"result","result":"ok"}\'\n'
)
WAITS_FOR_A_LINE_THAT_NEVER_COMES = "read line\n"


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
    leader_running(WAITS_FOR_A_LINE_THAT_NEVER_COMES)

    async def turn() -> Exception:
        try:
            await provider.acall_claude_with_mcp("hi", user_id="u-1", timeout_seconds=0)
        except provider.UnavailableError as timed_out:
            return timed_out
        raise AssertionError("the turn did not time out")

    timed_out = drive_the_turn(turn, leader_exits_with=-signal.SIGTERM)
    assert "timed out" in str(timed_out)
