"""The one owner of what a waiter that must outlast its own cancellation raises.

A close that has to finish whatever cancels its caller is awaited behind an
AnyIO shield (a scope cancelled by an ASGI server) and behind the completion
of a task (a direct ``Task.cancel()``).  Three functions decide, once, which
cancellation such a waiter has and in what order it is delivered: the
outcome of the task, then the first direct cancellation, then AnyIO's level
one.
"""

from __future__ import annotations

import asyncio
from types import TracebackType

import anyio
import anyio.lowlevel


async def wait_outlasting_cancellation(task: asyncio.Task[object]) -> asyncio.CancelledError | None:
    """Wait until ``task`` is done, however often the waiter is cancelled meanwhile.

    Returns the waiter's first direct cancellation, for the caller to raise
    once it has retrieved the task's outcome.  The task is never cancelled by
    this wait, and AnyIO's level cancellation cannot cut it short.  Only the
    task's completion is observed: its failure, or its cancellation while the
    waiter has no request, stays on the task for the caller to classify.
    """
    completion = asyncio.ensure_future(asyncio.wait({task}))
    interrupted: asyncio.CancelledError | None = None
    while not completion.done():
        with _Kept() as kept, anyio.CancelScope(shield=True):
            await asyncio.shield(completion)
        interrupted = interrupted or kept.cancellation
    return interrupted


class _Kept:
    """Keep the cancellation raised in its block, for the caller to raise later."""

    cancellation: asyncio.CancelledError | None = None

    def __enter__(self) -> _Kept:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_traceback: TracebackType | None,
    ) -> bool:
        if isinstance(exc, asyncio.CancelledError):
            self.cancellation = exc
            return True
        return False


async def level_cancellation() -> asyncio.CancelledError | None:
    """Deliver and return the AnyIO level cancellation pending on the calling task, if any."""
    with _Kept() as kept:
        await anyio.lowlevel.checkpoint_if_cancelled()
    return kept.cancellation


async def outlast[T](task: asyncio.Task[T]) -> T:
    """Wait for ``task``; raise its exception, else its first direct, else the level cancellation.

    Returns the task's result when nothing cancelled the caller.  A direct
    cancellation that the task's exception replaces is not retracted: no
    ``uncancel()``, because the requester of a structured cancellation
    retracts its own request.
    """
    direct = await wait_outlasting_cancellation(task)
    result = task.result()
    if direct is not None:
        raise direct
    if (level := await level_cancellation()) is not None:
        raise level
    return result
