"""The anchored async door hands an async caller a leader that only its own handle reaps."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from anchored_kernel import KernelModel, install_kernel, join_watcher, watcher_thread
from anchored_leaders import Leaders, spawn_warnings
from anchored_support import GONE, TEST_WAIT_SECONDS, ChildCleanup, Exited, OwnedChild
from test_anchored_child import watcher_names
from test_zombie_anchor import SIGKILLED

from agent_providers import spawn
from agent_providers.spawn import (
    AnchoredChild,
    AnchorStateError,
    ChildIdentityLost,
    ChildProcess,
)

PIPE_CAPACITY = 2**16
MEGABYTE = 2**20
ECHO_LINE_TO_BOTH_STREAMS = (
    "import sys\n"
    "line = sys.stdin.readline()\n"
    "sys.stdout.write(line)\n"
    "sys.stdout.flush()\n"
    "sys.stderr.write('err:' + line)\n"
    "sys.stderr.flush()\n"
    "code = sys.stdin.readline()\n"
    "sys.exit(int(code) if code.strip() else 0)\n"
)
STDERR_FIRST_THEN_ECHO_STDIN = (
    "import sys\n"
    "sys.stderr.write('e' * 2**20)\n"
    "sys.stderr.flush()\n"
    "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    "sys.stdout.flush()\n"
    "sys.exit(3)\n"
)
CLOSE_STDIN_AT_ONCE = (
    "import os, sys\nos.close(0)\nsys.stdout.write('closed\\n')\nsys.stdout.flush()\nsys.exit(4)\n"
)
COUNT_STDIN = (
    "import sys\n"
    "sys.stdout.write(f'{len(sys.stdin.buffer.read())}\\n')\n"
    "sys.stdout.flush()\n"
    "sys.exit(3)\n"
)
BLOCK_ON_STDIN = "import sys\nsys.stdin.read()\n"
TWO_LINES_THEN_EXIT_LINE = (
    "import sys\n"
    "sys.stdout.write('a' * {first} + '\\n')\n"
    "sys.stdout.write('b' * {second} + '\\n')\n"
    "sys.stdout.flush()\n"
    "code = sys.stdin.readline()\n"
    "sys.exit(int(code) if code.strip() else 0)\n"
)


class InjectedStop(BaseException):
    """Neither an Exception nor an interrupt asyncio's task step would hand to the loop."""


@dataclass(frozen=True)
class Opened:
    child: AnchoredChild
    owned: OwnedChild


@dataclass(frozen=True)
class DoorFailure:
    exception: BaseException
    descriptors_in_handler: frozenset[int]


def descriptors() -> frozenset[int]:
    listed = os.listdir("/proc/self/fd")
    return frozenset(int(name) for name in listed if os.path.exists(f"/proc/self/fd/{name}"))


async def bounded[Result](awaitable: Awaitable[Result]) -> Result:
    async with asyncio.timeout(TEST_WAIT_SECONDS):
        return await awaitable


ALL_PHASES = ("call", "teardown")


def asyncio_errors(
    caplog: pytest.LogCaptureFixture, phases: tuple[str, ...] = ("call",)
) -> list[str]:
    return [
        record.getMessage()
        for phase in phases
        for record in caplog.get_records(phase)  # type: ignore[arg-type]
        if record.name == "asyncio" and record.levelno >= logging.ERROR
    ]


def open_door(
    runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    child: ChildProcess,
    *,
    limit: int | None = None,
) -> Opened:
    captured = child_cleanup.capture(
        lambda: runner.run(bounded(spawn.open_anchored_pipes(child, stream_buffer_limit=limit)))
    )
    (owned,) = captured.children
    return Opened(captured.value, owned)


def failed_door(
    runner: asyncio.Runner, child_cleanup: ChildCleanup, child: ChildProcess
) -> DoorFailure:
    recorded: list[DoorFailure] = []

    async def attempt() -> None:
        try:
            await spawn.open_anchored_pipes(child)
        except BaseException as failure:
            recorded.append(DoorFailure(failure, descriptors()))
            raise

    with pytest.raises((asyncio.CancelledError, FileNotFoundError, InjectedStop)):
        child_cleanup.capture(lambda: runner.run(bounded(attempt())))
    (failure,) = recorded
    return failure


async def send_line(child: AnchoredChild, line: bytes) -> None:
    child.stdin.write(line)
    await child.stdin.drain()


async def exited_at_once(child: AnchoredChild) -> int:
    async with asyncio.timeout(0):
        return await child.exited()


def pipe_bytes_still_readable(holder: int) -> int:
    total = 0
    while chunk := os.read(holder, PIPE_CAPACITY):
        total += len(chunk)
    return total


@contextlib.contextmanager
def reading_end_of_stdin_held_open(pid: int) -> Iterator[int]:
    holder = os.open(f"/proc/{pid}/fd/0", os.O_RDONLY | os.O_NONBLOCK)
    try:
        yield holder
    finally:
        os.close(holder)


@pytest.fixture
def door_runner(child_cleanup: ChildCleanup) -> Iterator[asyncio.Runner]:
    with asyncio.Runner() as runner:
        runner.get_loop()
        yield runner


@pytest.fixture
def descriptors_before_the_door(door_runner: asyncio.Runner) -> frozenset[int]:
    return descriptors()


@pytest.fixture
def expected_spawn_warnings() -> list[str]:
    return []


@pytest.fixture(autouse=True)
def every_row_ends_clean(
    thread_failures: list[threading.ExceptHookArgs],
    caplog: pytest.LogCaptureFixture,
    expected_spawn_warnings: list[str],
) -> Iterator[None]:
    with caplog.at_level(logging.WARNING):
        yield
    assert thread_failures == []
    assert asyncio_errors(caplog, ALL_PHASES) == []
    assert spawn_warnings(caplog, ALL_PHASES) == expected_spawn_warnings


def test_the_door_serves_streams_and_keeps_the_exited_leader_anchored_until_release(
    door_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    descriptors_before_the_door: frozenset[int],
) -> None:
    opened = open_door(
        door_runner, child_cleanup, leaders.describe(script=ECHO_LINE_TO_BOTH_STREAMS)
    )
    child = opened.child
    watcher = watcher_thread(opened.owned.pid)

    async def scenario() -> None:
        with pytest.raises(AnchorStateError):
            child.release()
        await send_line(child, b"ping\n")
        assert await child.stdout.readline() == b"ping\n"
        assert await child.stderr.readline() == b"err:ping\n"
        await send_line(child, b"3\n")
        assert await child.exited() == 3
        assert await child.stdout.read() == b""
        assert child_cleanup.state(opened.owned) == Exited(3)
        assert not child.done_with_pid
        child.stdin.close()
        await child.stdin.wait_closed()
        child.release()
        join_watcher(watcher)
        assert child.done_with_pid
        assert child_cleanup.state(opened.owned) == GONE
        child.release()

    door_runner.run(bounded(scenario()))

    assert child.returncode == 3
    assert descriptors() - descriptors_before_the_door - {opened.owned.pidfd} == frozenset()


@pytest.mark.parametrize(
    ("script", "given", "expected_streams", "expected_code"),
    [
        pytest.param(
            STDERR_FIRST_THEN_ECHO_STDIN,
            b"x" * MEGABYTE,
            (b"x" * MEGABYTE, b"e" * MEGABYTE),
            3,
            id="output_before_input_under_backpressure",
        ),
        pytest.param(
            CLOSE_STDIN_AT_ONCE,
            b"x" * MEGABYTE,
            (b"closed\n", b""),
            4,
            id="stdin_closed_by_the_child_at_once",
        ),
        pytest.param(COUNT_STDIN, None, (b"0\n", b""), 3, id="no_input_still_gives_eof"),
    ],
)
def test_communicate_returns_both_streams_and_leaves_the_leader_unreaped(
    door_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    script: str,
    given: bytes | None,
    expected_streams: tuple[bytes, bytes],
    expected_code: int,
) -> None:
    opened = open_door(door_runner, child_cleanup, leaders.describe(script=script))

    streams = door_runner.run(bounded(opened.child.communicate(given)))

    assert streams == expected_streams
    assert child_cleanup.state(opened.owned) == Exited(expected_code)
    opened.child.release()
    assert child_cleanup.state(opened.owned) == GONE


@pytest.mark.parametrize("pending_bytes", [MEGABYTE, 0], ids=["output_pending", "nothing_pending"])
def test_release_discards_stdin_output_that_is_still_buffered(
    door_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    descriptors_before_the_door: frozenset[int],
    pending_bytes: int,
) -> None:
    opened = open_door(door_runner, child_cleanup, leaders.describe())
    child = opened.child

    async def hold_stdin_and_release() -> tuple[int, int, int, frozenset[int]]:
        with reading_end_of_stdin_held_open(child.pid) as holder:
            await send_line(child, b"3\n")
            await child.exited()
            child.stdin.write(b"x" * pending_bytes)
            child.stdin.close()
            buffered_before = child.stdin.transport.get_write_buffer_size()
            assert child.stdin.transport.is_closing()
            child.release()
            buffered_after = child.stdin.transport.get_write_buffer_size()
            held_bytes = pipe_bytes_still_readable(holder)
        remaining = descriptors()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return buffered_before, buffered_after, held_bytes, remaining

    buffered_before, buffered_after, held_bytes, remaining = door_runner.run(
        bounded(hold_stdin_and_release())
    )

    assert (buffered_before > 0) == (pending_bytes > 0)
    assert buffered_after == 0
    assert held_bytes <= PIPE_CAPACITY
    assert remaining - descriptors_before_the_door - {opened.owned.pidfd} == frozenset()


def test_a_cancelled_communicate_under_backpressure_still_closes_stdin(
    door_runner: asyncio.Runner, child_cleanup: ChildCleanup, leaders: Leaders
) -> None:
    opened = open_door(door_runner, child_cleanup, leaders.describe(script=BLOCK_ON_STDIN))
    child = opened.child

    async def scenario() -> bool:
        task = asyncio.create_task(child.communicate(b"x" * MEGABYTE))
        while child.stdin.transport.get_write_buffer_size() == 0:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        closing = child.stdin.transport.is_closing()
        child.signal_group(signal.SIGKILL)
        await child.exited()
        child.release()
        return closing

    assert door_runner.run(bounded(scenario()))


def test_a_pipe_whose_file_object_cannot_be_made_leaves_no_descriptor(
    door_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    monkeypatch: pytest.MonkeyPatch,
    descriptors_before_the_door: frozenset[int],
) -> None:
    def fdopen_that_fails(*args: object, **kwargs: object) -> None:
        raise InjectedStop

    monkeypatch.setattr(os, "fdopen", fdopen_that_fails)

    failure = failed_door(door_runner, child_cleanup, leaders.describe())

    assert isinstance(failure.exception, InjectedStop)
    assert child_cleanup.listed() == ()
    assert failure.descriptors_in_handler - descriptors_before_the_door == frozenset()


@pytest.mark.parametrize(
    ("limit", "short_line", "long_line"),
    [
        pytest.param(None, 32 * 1024, 128 * 1024, id="asyncio_default_limit"),
        pytest.param(1024, 512, 4096, id="forwarded_limit"),
    ],
)
def test_the_stream_limit_is_asyncios_unless_one_is_given(
    door_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    limit: int | None,
    short_line: int,
    long_line: int,
) -> None:
    script = TWO_LINES_THEN_EXIT_LINE.format(first=short_line, second=long_line)
    opened = open_door(door_runner, child_cleanup, leaders.describe(script=script), limit=limit)
    child = opened.child

    async def scenario() -> bytes:
        short = await child.stdout.readline()
        with pytest.raises(ValueError):
            await child.stdout.readline()
        await send_line(child, b"3\n")
        assert await child.exited() == 3
        child.release()
        return short

    assert door_runner.run(bounded(scenario())) == b"a" * short_line + b"\n"


@pytest.mark.parametrize("cancelled_attachment", [1, 2, 3], ids=["stdout", "stderr", "stdin"])
def test_a_cancelled_attachment_leaves_no_child_and_no_descriptor(
    door_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    descriptors_before_the_door: frozenset[int],
    cancelled_attachment: int,
) -> None:
    loop = door_runner.get_loop()
    attachments = 0

    def cancelling_at_the_chosen_attachment(
        real: Callable[..., Awaitable[object]],
    ) -> Callable[..., Awaitable[object]]:
        async def attach(*args: object) -> object:
            nonlocal attachments
            attachments += 1
            if attachments == cancelled_attachment:
                cancel_until_done(asyncio.current_task())
            return await real(*args)

        return attach

    def cancel_until_done(task: asyncio.Task | None) -> None:
        assert task is not None
        if not task.done():
            task.cancel()
            loop.call_soon(cancel_until_done, task)

    for method in ("connect_read_pipe", "connect_write_pipe"):
        real = getattr(loop, method)
        monkeypatch.setattr(loop, method, cancelling_at_the_chosen_attachment(real))

    failure = failed_door(door_runner, child_cleanup, leaders.describe())
    door_runner.run(asyncio.sleep(0))

    assert isinstance(failure.exception, asyncio.CancelledError)
    assert child_cleanup.listed() == ()
    assert failure.descriptors_in_handler - descriptors_before_the_door == frozenset()
    assert asyncio_errors(caplog) == []


def test_exited_serves_every_waiter_after_a_cancelled_or_timed_out_one(
    door_runner: asyncio.Runner, child_cleanup: ChildCleanup, leaders: Leaders
) -> None:
    opened = open_door(door_runner, child_cleanup, leaders.describe())
    child = opened.child

    async def scenario() -> tuple[list[int], int]:
        with pytest.raises(TimeoutError):
            await exited_at_once(child)
        cancelled_waiter = asyncio.create_task(child.exited())
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        await send_line(child, b"3\n")
        concurrent = await asyncio.gather(child.exited(), child.exited())
        later = await child.exited()
        child.release()
        return concurrent, later

    assert door_runner.run(bounded(scenario())) == ([3, 3], 3)


def test_a_loss_before_the_exit_raises_from_exited_and_release_still_frees_the_child(
    door_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = KernelModel(loses_at="wait")
    install_kernel(monkeypatch, model)
    opened = open_door(door_runner, child_cleanup, leaders.describe())
    child = opened.child

    async def scenario() -> None:
        try:
            with pytest.raises(ChildIdentityLost):
                await child.exited()
            assert child.returncode is None
            with pytest.raises(ChildIdentityLost):
                child.release()
        finally:
            child.stdin.close()

    door_runner.run(bounded(scenario()))

    assert child_cleanup.wait_exited(opened.owned) == 0
    assert child_cleanup.state(opened.owned) == Exited(0)
    assert model.violations == []


@pytest.mark.parametrize(
    "raised_by_start",
    [None, InjectedStop()],
    ids=["missing_binary", "non_exception_stop"],
)
def test_a_start_that_raises_leaves_the_caller_the_error_and_no_descriptor(
    door_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    descriptors_before_the_door: frozenset[int],
    raised_by_start: BaseException | None,
) -> None:
    watchers_before = watcher_names()
    child = leaders.describe(tmp_path / "missing-binary")
    if raised_by_start is not None:

        def start_that_raises(*args: object, **kwargs: object) -> None:
            raise raised_by_start

        monkeypatch.setattr(spawn, "start_anchored_leader", start_that_raises)

    failure = failed_door(door_runner, child_cleanup, child)
    door_runner.run(asyncio.sleep(0))

    if raised_by_start is None:
        assert isinstance(failure.exception, FileNotFoundError)
    else:
        assert failure.exception is raised_by_start
    assert child_cleanup.listed() == ()
    assert watcher_names() <= watchers_before
    assert failure.descriptors_in_handler - descriptors_before_the_door == frozenset()
    assert asyncio_errors(caplog) == []


def test_a_closed_loop_still_lets_the_anchored_child_be_released(
    door_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    expected_spawn_warnings: list[str],
    descriptors_before_the_door: frozenset[int],
) -> None:
    opened = open_door(door_runner, child_cleanup, leaders.describe())
    watcher = watcher_thread(opened.owned.pid)
    expected_spawn_warnings.append(
        f"on_exit of anchored leader {opened.child.pid} raised RuntimeError"
    )

    door_runner.close()
    opened.child.signal_group(signal.SIGKILL)
    child_cleanup.wait_exited(opened.owned)
    join_watcher(watcher)
    opened.child.release()

    assert child_cleanup.state(opened.owned) == GONE
    assert opened.child.returncode == SIGKILLED
    assert descriptors() - descriptors_before_the_door - {opened.owned.pidfd} == frozenset()


def test_asyncios_child_watcher_leaves_the_anchored_leader_unreaped(
    door_runner: asyncio.Runner, child_cleanup: ChildCleanup, leaders: Leaders
) -> None:
    opened = open_door(door_runner, child_cleanup, leaders.describe())

    async def scenario() -> int | None:
        await send_line(opened.child, b"3\n")
        assert await opened.child.exited() == 3
        stock = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", "pass")
        return await stock.wait()

    assert door_runner.run(bounded(scenario())) == 0
    assert child_cleanup.state(opened.owned) == Exited(3)
    opened.child.release()
    assert child_cleanup.state(opened.owned) == GONE


def test_the_handle_is_returned_without_an_await_after_the_start_on_the_loop_thread(
    door_runner: asyncio.Runner,
    child_cleanup: ChildCleanup,
    leaders: Leaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_start = spawn.start_anchored_leader
    started_on: list[tuple[asyncio.AbstractEventLoop | None, int]] = []

    def start_then_request_cancellation(*args: object, **kwargs: object) -> spawn.AnchoredLeader:
        leader = real_start(*args, **kwargs)
        thread = threading.get_ident()
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        started_on.append((running_loop, thread))
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return leader

    monkeypatch.setattr(spawn, "start_anchored_leader", start_then_request_cancellation)
    handed: list[AnchoredChild] = []

    async def caller() -> None:
        handed.append(await spawn.open_anchored_pipes(leaders.describe()))
        await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        child_cleanup.capture(lambda: door_runner.run(bounded(caller())))
    (owned,) = child_cleanup.listed()
    child = handed[0]

    async def finish() -> None:
        await send_line(child, b"3\n")
        assert await child.exited() == 3
        child.release()

    door_runner.run(bounded(finish()))

    assert started_on == [(door_runner.get_loop(), threading.get_ident())]
    assert child_cleanup.state(owned) == GONE
