"""``astream_cli_run`` runs any adapter's CLI in its own directories and ends only after the reap.

The real rows start a ``/bin/sh`` leader through the real door in a module-local pool; the
scripted rows answer the door with a scripted child and use the process-wide Claude pool.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import logging
import os
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from anchored_leaders import spawn_warnings
from anchored_support import GONE, TEST_WAIT_SECONDS, ChildCleanup
from descendant_support import DescendantRow, Handshake, lingering_descendant
from provider_test_support import (
    REAL_REAP_BOUNDS,
    ScriptedClaudeChild,
    a_child_that_outlives_sigkill,
    a_claude_cli_child,
    a_private_root,
    confirm_the_retained_group_gone,
    drive_real_children,
    drive_scripted,
    held_claude_slots,
    held_slots,
    the_only_tree,
)

from agent_providers import lost_groups
from agent_providers.anchored_turn import CliProcessPool, CliProcessPoolSaturated
from agent_providers.claude.provider import CLAUDE_CLI_PROCESS_POOL
from agent_providers.cli_run import (
    CliRunStart,
    CliStreamLineTooLong,
    RelayState,
    RunSurface,
    RunSurfaceRefused,
    StepEvent,
    SurfaceVerdict,
    astream_cli_run,
    examine_announcement,
    verdict_of,
)
from agent_providers.config import PrivateWorkingRoot, reset_config
from agent_providers.errors import ContainmentLost
from agent_providers.events import (
    AssistantTextEvent,
    FinalEvent,
    StreamEvent,
    ToolCallEvent,
)
from agent_providers.procfs import GroupCensus
from agent_providers.spawn import ChildProcess, KillUntilGoneResult, closed_environment

SHELL = Path("/bin/sh")
ABSENT_BINARY = Path("/nonexistent/cli-binary")
LINE_LIMIT_BYTES = 1024
NON_ASCII_PAYLOAD = "grüße, 日本語\nzweite Zeile\n\nletzte".encode()
STDERR_BYTES = 1024 * 1024
MARKER = "marker-4c1f9a"
SAYS_THEN_CALLS_THEN_SAYS = "echo say:a; echo call:1; echo say:b\n"
LEADER_ENDS_AFTER_SAYING = "echo say:a\n"
LEADER_BLOCKS_AFTER_SAYING = "echo say:a; exec sleep 60\n"
ONE_LINE_ONLY = [b"say:a\n"]


class ScriptRefused(Exception):
    """The decoder's closed failure at a ``fail`` line."""


class StartRefused(Exception):
    """The adapter's own failure while building the run."""


def text(value: str) -> AssistantTextEvent:
    return AssistantTextEvent(text=value)


def ended(returncode: int) -> FinalEvent:
    return FinalEvent(text=f"Final rc={returncode}")


READS_IN_ORDER = [
    text("a"),
    ToolCallEvent(tool_use_id="1", name="tool", input={}),
    text("b"),
    ended(0),
]


class LineScriptDecoder:
    def __init__(self, refusal: Exception) -> None:
        self._refusal = refusal
        self.returncodes: list[int] = []

    def events_of(self, line: bytes) -> list[StepEvent]:
        kind, _, value = line.decode().strip().partition(":")
        if kind == "say":
            return [text(value)]
        if kind == "call":
            return [ToolCallEvent(tool_use_id=value, name="tool", input={})]
        if kind == "fail":
            raise self._refusal
        return []

    def ending(self, returncode: int) -> FinalEvent:
        self.returncodes.append(returncode)
        return ended(returncode)


@dataclass
class LineScriptAdapter:
    pool: CliProcessPool
    script: str = ""
    stdin: bytes = b""
    files: Mapping[str, bytes] = field(default_factory=dict)
    line_limit_bytes: int = 1024 * 1024
    binary: Path = SHELL
    refusal: Exception = field(default_factory=ScriptRefused)
    start_failure: Exception | None = None
    decoders: list[LineScriptDecoder] = field(default_factory=list)

    def run_files(self) -> Mapping[str, bytes]:
        return self.files

    def start(self, home: Path, working_directory: Path) -> CliRunStart:
        if self.start_failure is not None:
            raise self.start_failure
        decoder = LineScriptDecoder(self.refusal)
        self.decoders.append(decoder)
        child = ChildProcess(
            binary=self.binary,
            arguments=("-c", self.script),
            environment=closed_environment(home),
            working_directory=working_directory,
        )
        return CliRunStart(child, self.stdin, decoder)


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    return a_private_root(tmp_path)


@pytest.fixture
def root(private_root: Path) -> PrivateWorkingRoot:
    return PrivateWorkingRoot(private_root)


@pytest.fixture
def pool() -> CliProcessPool:
    return CliProcessPool(limit=1, name="cli run test", reap=REAL_REAP_BOUNDS)


@pytest.fixture
def drive(
    pool: CliProcessPool,
    child_cleanup: ChildCleanup,
    thread_failures: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[Callable[[Coroutine[Any, Any, Any]], Any]]:
    yield lambda scenario: drive_real_children(child_cleanup, monkeypatch, scenario)
    assert [child_cleanup.state(owned) is GONE for owned in child_cleanup.listed()] == [
        True for _ in child_cleanup.listed()
    ]
    assert (held_slots(pool), thread_failures, spawn_warnings(caplog)) == (0, [], [])


@asynccontextmanager
async def without_waiting_on_stderr() -> AsyncIterator[None]:
    async with asyncio.timeout(TEST_WAIT_SECONDS / 4) as step_bound:
        yield
    assert not step_bound.expired()


def no_task_left() -> None:
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def collect(
    root: PrivateWorkingRoot,
    adapter: LineScriptAdapter,
    seen: list[StreamEvent],
) -> None:
    async for event in astream_cli_run(root, adapter):
        seen.append(event)


def events_of_run(
    drive: Callable[[Coroutine[Any, Any, Any]], Any],
    root: PrivateWorkingRoot,
    adapter: LineScriptAdapter,
) -> list[StreamEvent]:
    seen: list[StreamEvent] = []
    drive(collect(root, adapter, seen))
    return seen


def exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    return chain


def test_lines_become_events_in_order_and_the_ending_comes_last(
    drive,
    root: PrivateWorkingRoot,
    pool: CliProcessPool,
) -> None:
    adapter = LineScriptAdapter(pool, script=SAYS_THEN_CALLS_THEN_SAYS)

    assert events_of_run(drive, root, adapter) == READS_IN_ORDER


@pytest.mark.parametrize(
    ("stdin", "script", "expected"),
    [
        pytest.param(
            NON_ASCII_PAYLOAD,
            'echo "say:$(sha256sum | cut -d" " -f1)"',
            hashlib.sha256(NON_ASCII_PAYLOAD).hexdigest(),
            id="payload",
        ),
        pytest.param(b"", "cat >/dev/null; echo say:done", "done", id="empty"),
    ],
)
def test_the_stdin_reaches_the_child_whole_and_is_closed(
    drive,
    root: PrivateWorkingRoot,
    pool: CliProcessPool,
    stdin: bytes,
    script: str,
    expected: str,
) -> None:
    adapter = LineScriptAdapter(pool, script=script, stdin=stdin)

    assert events_of_run(drive, root, adapter) == [text(expected), ended(0)]


def test_the_child_runs_in_the_runs_own_directories(
    drive,
    root: PrivateWorkingRoot,
    private_root: Path,
    pool: CliProcessPool,
) -> None:
    script = (
        'echo "say:$(pwd -P)"; echo "say:$HOME"; echo "say:$(ls -A | tr "\\n" ,)"; '
        'echo "say:$(ls -A "$HOME" | tr "\\n" ,)"; echo "say:$(stat -c %a "$HOME/run-file")"\n'
    )
    adapter = LineScriptAdapter(pool, script=script, files={"run-file": b"content"})

    working_directory, home, working_listing, home_listing, mode = (
        event.text for event in events_of_run(drive, root, adapter)[:-1]
    )

    tree = Path(os.path.realpath(private_root)) / Path(home).parent.name
    assert (
        Path(working_directory),
        Path(home),
        working_listing,
        home_listing,
        mode,
        tree.name.startswith("agent-run-"),
    ) == (tree / "work", tree / "home", "", "run-file,", "600", True)


def test_the_group_is_gone_and_the_tree_removed_before_the_ending(
    drive,
    root: PrivateWorkingRoot,
    private_root: Path,
    pool: CliProcessPool,
    handshake: Handshake,
    child_cleanup: ChildCleanup,
) -> None:
    script = lingering_descendant(
        "echo say:a\n",
        exit_code=0,
        blocks=False,
        escapes=False,
        handshake=handshake,
    )
    adapter = LineScriptAdapter(pool, script=script)
    at_the_ending: list[tuple[bool, int, list[Path]]] = []

    async def scenario() -> None:
        async for event in astream_cli_run(root, adapter):
            if isinstance(event, FinalEvent):
                at_the_ending.append(
                    (row.descendant_is_dead(), held_slots(pool), list(private_root.iterdir()))
                )

    with DescendantRow(handshake, child_cleanup) as row:
        row.run_beside(lambda: drive(scenario()))

    assert at_the_ending == [(True, 0, [])]


async def close_after_the_first_event(
    root: PrivateWorkingRoot,
    adapter: LineScriptAdapter,
) -> None:
    events = astream_cli_run(root, adapter)
    assert await anext(events) == text("a")
    await events.aclose()


async def cancel_after_the_first_event(
    root: PrivateWorkingRoot,
    adapter: LineScriptAdapter,
) -> None:
    first_seen = asyncio.Event()

    async def consume() -> None:
        async for _ in astream_cli_run(root, adapter):
            first_seen.set()

    consumer = asyncio.create_task(consume())
    await first_seen.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer


@pytest.mark.parametrize(
    "stop",
    [
        pytest.param(close_after_the_first_event, id="aclose"),
        pytest.param(cancel_after_the_first_event, id="cancelled"),
    ],
)
def test_a_stop_mid_stream_reaps_before_it_returns(
    drive,
    root: PrivateWorkingRoot,
    private_root: Path,
    pool: CliProcessPool,
    child_cleanup: ChildCleanup,
    stop: Callable[[PrivateWorkingRoot, LineScriptAdapter], Coroutine[Any, Any, None]],
) -> None:
    adapter = LineScriptAdapter(pool, script=LEADER_BLOCKS_AFTER_SAYING)

    async def scenario() -> tuple[bool, int, list[Path]]:
        await stop(root, adapter)
        no_task_left()
        leader = child_cleanup.listed()[-1]
        return (
            child_cleanup.state(leader) is GONE,
            held_slots(pool),
            list(private_root.iterdir()),
        )

    assert drive(scenario()) == (True, 0, [])


@pytest.mark.parametrize(
    ("overrides", "expected_error", "pool_is_taken"),
    [
        pytest.param({"binary": ABSENT_BINARY}, FileNotFoundError, False, id="missing-binary"),
        pytest.param({}, CliProcessPoolSaturated, True, id="pool-full"),
        pytest.param({"start_failure": StartRefused()}, StartRefused, False, id="start-refused"),
    ],
)
def test_a_start_that_fails_raises_at_the_first_step_and_leaves_nothing(
    drive,
    root: PrivateWorkingRoot,
    private_root: Path,
    pool: CliProcessPool,
    overrides: dict[str, Any],
    expected_error: type[Exception],
    pool_is_taken: bool,
) -> None:
    adapter = LineScriptAdapter(pool, **overrides)
    taken = pool.reserve() if pool_is_taken else None

    async def scenario() -> tuple[list[Path], list[Path], int]:
        events = astream_cli_run(root, adapter)
        before_the_first_step = list(private_root.iterdir())
        with pytest.raises(expected_error):
            await anext(events)
        return before_the_first_step, list(private_root.iterdir()), held_slots(pool)

    try:
        assert drive(scenario()) == ([], [], 1 if pool_is_taken else 0)
    finally:
        if taken is not None:
            pool.release(taken)


def test_an_over_long_line_ends_the_run_without_its_bytes(
    drive,
    root: PrivateWorkingRoot,
    private_root: Path,
    pool: CliProcessPool,
) -> None:
    script = f"printf %s {MARKER}; head -c {2 * LINE_LIMIT_BYTES} /dev/zero | tr '\\0' x; echo\n"
    adapter = LineScriptAdapter(pool, script=script, line_limit_bytes=LINE_LIMIT_BYTES)

    run = collect(root, adapter, [])

    with pytest.raises(CliStreamLineTooLong) as refusal:
        drive(run)

    assert (
        MARKER in str(refusal.value),
        refusal.value.__cause__,
        refusal.value.__context__,
        list(private_root.iterdir()),
    ) == (False, None, None, [])


def test_a_decoder_refusal_propagates_after_the_reap(
    drive,
    root: PrivateWorkingRoot,
    private_root: Path,
    pool: CliProcessPool,
) -> None:
    refusal = ScriptRefused()
    adapter = LineScriptAdapter(
        pool,
        script="printf 'say:a\\nfail\\nsay:b\\n'\n",
        refusal=refusal,
    )
    seen: list[StreamEvent] = []
    run = collect(root, adapter, seen)

    with pytest.raises(ScriptRefused) as raised:
        drive(run)

    assert (raised.value is refusal, seen, list(private_root.iterdir())) == (
        True,
        [text("a")],
        [],
    )


def test_the_ending_receives_the_leaders_exit_code(
    drive,
    root: PrivateWorkingRoot,
    pool: CliProcessPool,
) -> None:
    adapter = LineScriptAdapter(pool, script="echo say:a; exit 3\n")

    assert events_of_run(drive, root, adapter) == [text("a"), ended(3)]


def test_stderr_is_drained_and_never_kept(
    drive,
    root: PrivateWorkingRoot,
    pool: CliProcessPool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = f"echo {MARKER} >&2; head -c {STDERR_BYTES} /dev/zero >&2; echo say:a\n"
    adapter = LineScriptAdapter(pool, script=script, line_limit_bytes=LINE_LIMIT_BYTES)
    caplog.set_level(logging.DEBUG)

    assert events_of_run(drive, root, adapter) == [text("a"), ended(0)]
    assert MARKER not in caplog.text


def test_a_hundred_runs_each_start_fresh_and_leave_nothing(
    drive,
    root: PrivateWorkingRoot,
    private_root: Path,
    pool: CliProcessPool,
) -> None:
    async def scenario() -> list[list[StreamEvent]]:
        runs: list[list[StreamEvent]] = []
        for number in range(100):
            adapter = LineScriptAdapter(
                pool,
                script='echo "say:$(ls -A "$HOME" | tr "\\n" ,)"\n',
                files={f"file-{number}": b""},
            )
            seen: list[StreamEvent] = []
            await collect(root, adapter, seen)
            runs.append(seen)
        return runs

    assert drive(scenario()) == [[text(f"file-{number},"), ended(0)] for number in range(100)]
    assert (list(private_root.iterdir()), held_slots(pool)) == ([], 0)


def test_a_run_needs_no_installed_configuration(
    drive,
    root: PrivateWorkingRoot,
    pool: CliProcessPool,
) -> None:
    reset_config()
    adapter = LineScriptAdapter(pool, script=SAYS_THEN_CALLS_THEN_SAYS)

    assert events_of_run(drive, root, adapter) == READS_IN_ORDER


async def free_the_retained_group(
    child: ScriptedClaudeChild,
    group: Any,
    private_root: Path,
) -> None:
    assert (held_claude_slots(), the_only_tree(private_root).is_dir()) == (1, True)
    assert not await lost_groups.confirm_group_gone(group)
    assert (held_claude_slots(), the_only_tree(private_root).is_dir()) == (1, True)
    assert await confirm_the_retained_group_gone(child, group)


def test_a_retained_group_keeps_its_tree_until_confirmed(
    root: PrivateWorkingRoot,
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = a_child_that_outlives_sigkill(
        censuses=(GroupCensus.LIVE, GroupCensus.EMPTY),
        stderr_stalls=True,
        stream_lines=ONE_LINE_ONLY,
    )
    adapter = LineScriptAdapter(CLAUDE_CLI_PROCESS_POOL)

    async def scenario() -> None:
        events = astream_cli_run(root, adapter)
        assert await anext(events) == text("a")
        async with without_waiting_on_stderr():
            with pytest.raises(ContainmentLost) as lost:
                await events.aclose()
        no_task_left()
        await free_the_retained_group(child, lost.value.group, private_root)

    drive_scripted(monkeypatch, child, scenario())

    assert (held_claude_slots(), list(private_root.iterdir())) == (0, [])


def test_a_group_unconfirmed_after_a_clean_end_raises_instead_of_the_ending(
    root: PrivateWorkingRoot,
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = a_claude_cli_child(
        stream_lines=ONE_LINE_ONLY,
        returncode=0,
        gone=(KillUntilGoneResult(GroupCensus.LIVE, ended_in_time=True),),
        censuses=(GroupCensus.LIVE, GroupCensus.EMPTY),
        stderr_stalls=True,
    )
    adapter = LineScriptAdapter(CLAUDE_CLI_PROCESS_POOL)

    async def scenario() -> None:
        events = astream_cli_run(root, adapter)
        assert await anext(events) == text("a")
        async with without_waiting_on_stderr():
            with pytest.raises(ContainmentLost) as lost:
                await anext(events)
        no_task_left()
        assert [decoder.returncodes for decoder in adapter.decoders] == [[]]
        await free_the_retained_group(child, lost.value.group, private_root)

    drive_scripted(monkeypatch, child, scenario())

    assert (held_claude_slots(), list(private_root.iterdir())) == (0, [])


def test_a_failed_reap_keeps_the_original_failure_as_its_context(
    root: PrivateWorkingRoot,
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def run_to_the_failed_reap() -> None:
        refusal = ScriptRefused()
        drain_failure = OSError("stderr read failed")
        child = a_child_that_outlives_sigkill(
            censuses=(GroupCensus.LIVE, GroupCensus.EMPTY),
            stream_lines=[b"say:a\n", b"fail\n"],
            stderr_failure=drain_failure,
        )
        adapter = LineScriptAdapter(CLAUDE_CLI_PROCESS_POOL, refusal=refusal)

        async def scenario() -> None:
            events = astream_cli_run(root, adapter)
            assert await anext(events) == text("a")
            await child.stderr_failed.wait()
            async with without_waiting_on_stderr():
                with pytest.raises(ContainmentLost) as lost:
                    await anext(events)
            assert (
                lost.value.__context__ is refusal,
                drain_failure in exception_chain(lost.value),
            ) == (True, False)
            await free_the_retained_group(child, lost.value.group, private_root)

        drive_scripted(monkeypatch, child, scenario())

    run_to_the_failed_reap()
    gc.collect()

    assert (
        held_claude_slots(),
        list(private_root.iterdir()),
        [r for r in caplog.records if r.name == "asyncio" and r.levelno >= logging.ERROR],
    ) == (0, [], [])


def test_a_stdin_write_failure_propagates_after_the_reap(
    root: PrivateWorkingRoot,
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = BrokenPipeError()
    child = a_claude_cli_child(stdin_failure=refused, stderr_stalls=True)
    adapter = LineScriptAdapter(CLAUDE_CLI_PROCESS_POOL, stdin=b"payload")
    seen: list[StreamEvent] = []

    async def scenario() -> None:
        async with without_waiting_on_stderr():
            with pytest.raises(BrokenPipeError) as raised:
                await collect(root, adapter, seen)
        no_task_left()
        assert (raised.value is refused, seen) == (True, [])

    drive_scripted(monkeypatch, child, scenario())

    assert (held_claude_slots(), list(private_root.iterdir())) == (0, [])


def a_surface(relay: RelayState = RelayState.ABSENT, **causes: bool) -> RunSurface:
    flags = {
        "foreign_credential": False,
        "tool_beside_relay": False,
        "server_beside_relay": False,
        **causes,
    }
    return RunSurface(relay=relay, **flags)


@pytest.mark.parametrize(
    ("relay", "relay_attached", "verdict"),
    [
        (RelayState.CONNECTED, True, SurfaceVerdict.ACCEPTED),
        (RelayState.ABSENT, True, SurfaceVerdict.RELAY_NOT_CONNECTED),
        (RelayState.NOT_CONNECTED, True, SurfaceVerdict.RELAY_NOT_CONNECTED),
        (RelayState.ABSENT, False, SurfaceVerdict.ACCEPTED),
        (RelayState.CONNECTED, False, SurfaceVerdict.SERVER_BESIDE_RELAY),
        (RelayState.NOT_CONNECTED, False, SurfaceVerdict.SERVER_BESIDE_RELAY),
    ],
)
def test_the_relay_verdict_follows_whether_a_relay_is_attached(
    relay: RelayState, relay_attached: bool, verdict: SurfaceVerdict
) -> None:
    assert verdict_of(a_surface(relay), relay_attached=relay_attached) == verdict


@pytest.mark.parametrize(
    ("causes", "relay", "verdict"),
    [
        (
            {"foreign_credential": True, "tool_beside_relay": True},
            RelayState.CONNECTED,
            SurfaceVerdict.FOREIGN_CREDENTIAL,
        ),
        (
            {"tool_beside_relay": True, "server_beside_relay": True},
            RelayState.CONNECTED,
            SurfaceVerdict.TOOL_BESIDE_RELAY,
        ),
        (
            {"server_beside_relay": True},
            RelayState.NOT_CONNECTED,
            SurfaceVerdict.SERVER_BESIDE_RELAY,
        ),
    ],
    ids=["credential-over-tool", "tool-over-server", "server-over-relay"],
)
def test_each_verdict_outranks_the_next_in_precedence(
    causes: dict[str, bool], relay: RelayState, verdict: SurfaceVerdict
) -> None:
    assert verdict_of(a_surface(relay, **causes), relay_attached=True) == verdict


def test_a_refused_announcement_raises_its_verdict_alone_and_logs_its_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    refusal: RunSurfaceRefused | None = None

    try:
        raise UnicodeDecodeError("utf-8", MARKER.encode(), 0, 1, "invalid start byte")
    except UnicodeDecodeError:
        try:
            examine_announcement(
                a_surface(RelayState.CONNECTED, tool_beside_relay=True), relay_attached=True
            )
        except RunSurfaceRefused as refused:
            refusal = refused

    assert refusal is not None
    assert (
        refusal.verdict,
        str(refusal),
        refusal.__cause__,
        refusal.__context__,
        MARKER in caplog.text,
        [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING],
    ) == (
        SurfaceVerdict.TOOL_BESIDE_RELAY,
        "The CLI's start announcement was refused: a tool the run's relay does not serve.",
        None,
        None,
        False,
        ["A CLI run's start announcement was refused (TOOL_BESIDE_RELAY)"],
    )


def test_an_accepted_announcement_returns_without_a_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    examine_announcement(a_surface(RelayState.CONNECTED), relay_attached=True)

    assert caplog.records == []
