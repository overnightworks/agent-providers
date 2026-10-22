"""``agent_runtime.outlasting``: what a waiter that must outlast its cancellation raises."""

from __future__ import annotations

import asyncio
import enum

import anyio
import pytest
from service_support import in_loop

from agent_runtime.outlasting import level_cancellation, outlast, wait_outlasting_cancellation


class _Gate:
    """A task that ends only when released, so a test can cancel its waiter meanwhile."""

    def __init__(self, failure: BaseException | None = None) -> None:
        self._released = asyncio.Event()
        self._failure = failure
        self.cancelled_by_the_wait = False
        self.task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> str:
        try:
            await self._released.wait()
        except asyncio.CancelledError:
            self.cancelled_by_the_wait = True
            raise
        if self._failure is not None:
            raise self._failure
        return "done"

    def release(self) -> None:
        self._released.set()


async def _parked() -> None:
    await asyncio.get_running_loop().settled()


class _Cut:
    """How the waiter is cancelled while it waits: directly, by its AnyIO scope, or both."""

    def __init__(self, directly: int, by_level: bool) -> None:
        self.directly = directly
        self.by_level = by_level

    def apply(self, waiter: asyncio.Task[object], scope: anyio.CancelScope) -> None:
        for _ in range(self.directly):
            waiter.cancel()
        if self.by_level:
            scope.cancel()


NO_CUT = _Cut(0, False)
DIRECT_ONCE = _Cut(1, False)
DIRECT_TWICE = _Cut(2, False)
LEVEL = _Cut(0, True)
DIRECT_THEN_LEVEL = _Cut(1, True)


async def _outlasting(gate: _Gate, cut: _Cut) -> tuple[str, object, bool, bool]:
    """Outlast ``gate`` cut as ``cut`` says; what the waiter ended with, and what ran after."""
    scope = anyio.CancelScope()
    after_the_wait: list[bool] = []

    async def waiter() -> object:
        with scope:
            result = await outlast(gate.task)
            after_the_wait.append(True)
            return result
        return None

    waiting = asyncio.get_running_loop().create_task(waiter())
    await _parked()
    cut.apply(waiting, scope)
    await _parked()
    assert not waiting.done()
    gate.release()
    outcomes = await asyncio.gather(waiting, return_exceptions=True)
    ended = outcomes[0]
    kind = type(ended).__name__ if isinstance(ended, BaseException) else "returned"
    return kind, ended, scope.cancelled_caught, bool(after_the_wait)


@pytest.mark.parametrize(
    ("cut", "kind", "caught_by_scope", "ran_on"),
    [
        pytest.param(NO_CUT, "returned", False, True, id="not-cancelled-returns-the-result"),
        pytest.param(DIRECT_ONCE, "CancelledError", False, False, id="cancelled-directly-once"),
        pytest.param(DIRECT_TWICE, "CancelledError", False, False, id="cancelled-directly-twice"),
        pytest.param(LEVEL, "returned", True, False, id="cut-by-level-is-caught-by-the-scope"),
        pytest.param(DIRECT_THEN_LEVEL, "CancelledError", False, False, id="direct-then-level"),
    ],
)
@in_loop
async def test_outlast_waits_for_the_task_and_then_raises_the_callers_first_cancellation(
    cut, kind, caught_by_scope, ran_on
):
    gate = _Gate()

    ended_as, _ended, scope_caught, after_the_wait = await _outlasting(gate, cut)

    assert (ended_as, scope_caught, after_the_wait) == (kind, caught_by_scope, ran_on)
    assert gate.task.result() == "done"
    assert gate.cancelled_by_the_wait is False


@pytest.mark.parametrize("cut", [NO_CUT, DIRECT_ONCE, LEVEL], ids=["uncut", "direct", "level"])
@in_loop
async def test_outlast_raises_the_tasks_own_exception_over_any_cancellation(cut):
    failure = RuntimeError("the task broke")
    gate = _Gate(failure)

    ended_as, ended, scope_caught, _after = await _outlasting(gate, cut)

    assert ended is failure
    assert (ended_as, scope_caught) == ("RuntimeError", False)


@in_loop
async def test_the_first_direct_cancellation_is_the_one_outlast_raises():
    gate = _Gate()
    first_seen: list[asyncio.CancelledError] = []

    async def waiter() -> None:
        try:
            await outlast(gate.task)
        except asyncio.CancelledError as cancelled:
            first_seen.append(cancelled)
            raise

    waiting = asyncio.get_running_loop().create_task(waiter())
    await _parked()
    waiting.cancel("first")
    await _parked()
    waiting.cancel("second")
    await _parked()
    gate.release()
    await asyncio.gather(waiting, return_exceptions=True)

    assert first_seen[0].args == ("first",)


class _Where(enum.Enum):
    """Where level_cancellation is asked: in a cancelled scope, in a live one, or in a raw task."""

    CANCELLED_SCOPE = enum.auto()
    LIVE_SCOPE = enum.auto()
    RAW_TASK_FROM_A_CANCELLED_SCOPE = enum.auto()


@pytest.mark.parametrize(
    ("where", "delivered", "caught"),
    [
        pytest.param(_Where.CANCELLED_SCOPE, True, True, id="own-cancelled-scope"),
        pytest.param(_Where.LIVE_SCOPE, False, False, id="live-scope"),
        pytest.param(_Where.RAW_TASK_FROM_A_CANCELLED_SCOPE, False, False, id="raw-task"),
    ],
)
@in_loop
async def test_a_level_cancellation_is_taken_only_from_the_callers_own_cancelled_scope(
    where, delivered, caught
):
    scope = anyio.CancelScope()
    taken: list[asyncio.CancelledError | None] = []
    raw: list[asyncio.Task[None]] = []

    async def ask() -> None:
        taken.append(await level_cancellation())

    with scope:
        if where is not _Where.LIVE_SCOPE:
            scope.cancel()
        if where is _Where.RAW_TASK_FROM_A_CANCELLED_SCOPE:
            raw.append(asyncio.get_running_loop().create_task(ask()))
        else:
            await ask()
            if taken[0] is not None:
                raise taken[0]
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*raw)

    assert (taken[0] is not None, scope.cancelled_caught) == (delivered, caught)


@pytest.mark.parametrize("fails", [True, False], ids=["raises", "is-cancelled"])
@in_loop
async def test_the_wait_returns_nothing_for_a_failed_or_cancelled_task_and_leaves_its_outcome_on_it(
    fails,
):
    failure = RuntimeError("the task broke")
    gate = _Gate(failure)
    waiting = asyncio.get_running_loop().create_task(wait_outlasting_cancellation(gate.task))
    await _parked()
    if fails:
        gate.release()
    else:
        gate.task.cancel()

    returned = await waiting

    assert returned is None
    assert waiting.cancelling() == 0
    if fails:
        assert gate.task.exception() is failure
    else:
        assert gate.task.cancelled()
