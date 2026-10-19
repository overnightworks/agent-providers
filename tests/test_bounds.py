import asyncio

import bounds
import pytest
from bounds import within_the_bound
from relay_support import close_bounded


async def settle_after_a_loop_turn[T](value: T, cancelled: list[bool] | None = None) -> T:
    settled = asyncio.Event()
    asyncio.get_running_loop().call_soon(settled.set)
    try:
        await settled.wait()
    except asyncio.CancelledError:
        if cancelled is not None:
            cancelled.append(True)
        raise
    return value


class RelayClosingAfterALoopTurn:
    async def close(self) -> None:
        await settle_after_a_loop_turn(None)


@pytest.fixture
def no_time_to_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bounds, "TEST_WAIT_SECONDS", 0)


def test_the_helper_returns_what_the_awaitable_returns() -> None:
    assert asyncio.run(within_the_bound(settle_after_a_loop_turn("the item"))) == "the item"


@pytest.mark.usefixtures("no_time_to_wait")
def test_an_awaitable_still_running_at_the_bound_ends_in_a_timeout() -> None:
    with pytest.raises(TimeoutError):
        asyncio.run(within_the_bound(settle_after_a_loop_turn("late")))


@pytest.mark.usefixtures("no_time_to_wait")
def test_a_timed_out_awaitable_is_cancelled() -> None:
    cancelled: list[bool] = []

    with pytest.raises(TimeoutError):
        asyncio.run(within_the_bound(settle_after_a_loop_turn("late", cancelled)))

    assert cancelled == [True]


def test_the_awaitables_own_error_arrives_unchanged() -> None:
    async def failing() -> None:
        raise ValueError("the awaitable's own error")

    with pytest.raises(ValueError, match="the awaitable's own error"):
        asyncio.run(within_the_bound(failing()))


def test_patching_the_one_bound_moves_the_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    async def wait_with_the_bound_as_patched(patched_bound: int) -> str:
        monkeypatch.setattr(bounds, "TEST_WAIT_SECONDS", patched_bound)
        return await within_the_bound(settle_after_a_loop_turn("settled"))

    assert asyncio.run(wait_with_the_bound_as_patched(bounds.TEST_WAIT_SECONDS)) == "settled"
    with pytest.raises(TimeoutError):
        asyncio.run(wait_with_the_bound_as_patched(0))


@pytest.mark.usefixtures("no_time_to_wait")
def test_the_relay_close_honours_the_one_bound() -> None:
    with pytest.raises(TimeoutError):
        asyncio.run(close_bounded(RelayClosingAfterALoopTurn()))
