"""The suite's one bound for waiting on an awaited event.

A leaf, so every test support family can import it without reaching another.
"""

import asyncio
from collections.abc import Awaitable
from typing import Final

TEST_WAIT_SECONDS: Final = 10


async def within_the_bound[T](awaitable: Awaitable[T]) -> T:
    async with asyncio.timeout(TEST_WAIT_SECONDS):
        return await awaitable
