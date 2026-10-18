"""A run's tool set: the app's client, the barrier and the relay, opened once and closed once."""

import asyncio
import logging
import ssl
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack
from typing import Any, Final, Self

from agent_client.capability import McpOrigin
from agent_providers.outlasting import outlast, wait_outlasting_cancellation
from agent_service.app_tools import open_app_tools
from agent_service.capability import Capability
from agent_service.run_liveness import (
    RunClock,
    log_failure,
    unfinished_by,
)
from agent_service.tool_relay import (
    McpRelayOwner,
    Relay,
    RelayAccess,
    ToolCallBarrier,
)

log = logging.getLogger(__name__)

UPSTREAM_CLOSE_SECONDS: Final = 2.0
UPSTREAM_ABORT_SECONDS: Final = 0.5

_overrun_tasks: set[asyncio.Task[None]] = set()


class UpstreamCleanupIncomplete(Exception):
    """A step of the close ended with its task still running; the task was cancelled once."""

    def __init__(self) -> None:
        super().__init__("the upstream outlived its bound")


def _retire(task: asyncio.Task[None]) -> None:
    _overrun_tasks.discard(task)
    if not task.cancelled():
        task.exception()


def _ending_of(task: asyncio.Task[None]) -> BaseException | None:
    try:
        return task.exception()
    except asyncio.CancelledError as cancelled:  # NOSONAR(S7497) log needs its positions
        return cancelled


async def _failure_of(step: Coroutine[Any, Any, None], name: str) -> BaseException | None:
    """Run ``step`` as the task ``name``, wait for it to end and return what it ended with."""
    task = asyncio.get_running_loop().create_task(step, name=name)
    await asyncio.wait({task})
    return _ending_of(task)


async def _within(
    clock: RunClock, seconds: float, step: Coroutine[Any, Any, None], name: str
) -> BaseException | None:
    """Run ``step`` as the task ``name`` and wait for it until ``seconds`` on ``clock`` passed.

    Returns what the step ended with, or raises ``UpstreamCleanupIncomplete`` when it did not
    end. At the deadline the task is cancelled once and never awaited again: its unwinding, even
    a cleanup that absorbs the cancellation, cannot delay the caller. The task stays referenced
    until it ends and its outcome is retrieved then.
    """
    deadline = clock() + seconds
    task = asyncio.get_running_loop().create_task(step, name=name)
    if await unfinished_by(clock, deadline, {task}, f"{name}-deadline"):
        _overrun_tasks.add(task)
        task.add_done_callback(_retire)
        task.cancel()
        raise UpstreamCleanupIncomplete
    return _ending_of(task)


async def _logged_within(
    clock: RunClock, seconds: float, step: Coroutine[Any, Any, None], name: str, event: str
) -> None:
    try:
        failure = await _within(clock, seconds, step, name)
    except UpstreamCleanupIncomplete as overrun:
        failure = overrun
    if failure is not None:
        log_failure(log, event, failure)


async def _leave_client(run_id: str, clock: RunClock, upstream: AsyncExitStack) -> None:
    await _logged_within(
        clock,
        UPSTREAM_ABORT_SECONDS,
        upstream.aclose(),
        f"upstream:{run_id}:client",
        f"Leaving the app's client of run {run_id} failed",
    )


class RunTools:
    """The tool set of one run, opened by ``open`` and closed once by ``close``.

    ``open`` enters ``open_app_tools``, lists the app's tools, builds the ``ToolCallBarrier``
    and the ``McpRelayOwner`` and starts the relay; ``access`` and ``calls`` are what the
    runner uses. A failure after the client was entered, a cancellation included, leaves the
    client within ``UPSTREAM_ABORT_SECONDS`` and propagates as the same object: no cancellation
    of the opener, direct or AnyIO's level one, cuts that leaving short or replaces the failure.

    ``close(reap)`` is one attempt of five steps, in this order:

    1. the relay stops listening and the barrier stops, with no await between them;
    2. the injected ``reap`` runs to its own bound; its exception is kept, not logged;
    3. the forwards are cancelled once and ``settle()`` is awaited within
       ``UPSTREAM_CLOSE_SECONDS``;
    4. the app's client is left within ``UPSTREAM_ABORT_SECONDS``;
    5. the relay's connections are closed within ``RELAY_CLOSE_SECONDS``.

    Each deadline is read on the run's ``RunClock`` when its step starts, so an early end gives
    nothing to a later step and an overrun takes nothing from it. The bound is the owner's wait
    for the step's task, never a cancel scope: at the deadline the task is cancelled once and
    not awaited again, so a cleanup that absorbs cancellation delays only itself. A step that
    overruns (``UpstreamCleanupIncomplete``, ``RelayCleanupIncomplete``) or ends with any
    exception, ``CancelledError`` and other ``BaseException`` included, is logged once by class
    and the close goes on. A returned close means each step's bounded attempt
    finished, not that a stalled cleanup's socket is closed.

    Only the reap's exception, of any class, is raised, as the same object, after step 5 and
    never logged; steps 3 to 5 run whatever the reap raised. ``KeyboardInterrupt`` and
    ``SystemExit`` are not contained, for any step or the reap: asyncio propagates them out of
    the event loop and the process ends as README "Interruptions" says.
    A caller's cancellation, direct or AnyIO's level one, never cuts the attempt short: the
    close waits for the attempt, raises its exception, otherwise the direct cancellation,
    otherwise delivers the level cancellation, otherwise returns. ``close`` is called once.
    """

    def __init__(
        self,
        run_id: str,
        clock: RunClock,
        relay: Relay,
        barrier: ToolCallBarrier,
        upstream: AsyncExitStack,
    ) -> None:
        self._run_id = run_id
        self._clock = clock
        self._relay = relay
        self._barrier = barrier
        self._upstream = upstream

    @classmethod
    async def open(
        cls,
        run_id: str,
        origin: McpOrigin,
        capability: Capability,
        tls_context: ssl.SSLContext,
        clock: RunClock,
    ) -> Self:
        upstream = AsyncExitStack()
        session = await upstream.enter_async_context(
            open_app_tools(origin, capability, tls_context)
        )
        try:
            offered = await session.list_tools()
            barrier = ToolCallBarrier(run_id, session, capability)
            relay = await Relay.start(run_id, McpRelayOwner(capability, offered, barrier), clock)
        except BaseException:
            leaving = asyncio.get_running_loop().create_task(
                _leave_client(run_id, clock, upstream), name=f"run-tools:{run_id}:failed-open"
            )
            await wait_outlasting_cancellation(leaving)
            leaving.result()
            raise
        return cls(run_id, clock, relay, barrier, upstream)

    @property
    def access(self) -> RelayAccess:
        return self._relay.access

    @property
    def calls(self) -> ToolCallBarrier:
        return self._barrier

    async def close(self, reap: Callable[[], Awaitable[None]]) -> None:
        await outlast(
            asyncio.get_running_loop().create_task(
                self._close_once(reap), name=f"run-tools:{self._run_id}:close"
            )
        )

    async def _close_once(self, reap: Callable[[], Awaitable[None]]) -> None:
        self._relay.stop_listening()
        self._barrier.stop()
        try:
            await reap()
        finally:
            await self._close_upstream_calls()
            await _leave_client(self._run_id, self._clock, self._upstream)
            await self._close_relay()

    async def _close_upstream_calls(self) -> None:
        self._barrier.abort_forwards()
        await _logged_within(
            self._clock,
            UPSTREAM_CLOSE_SECONDS,
            self._barrier.settle(),
            f"upstream:{self._run_id}:settle",
            f"Closing the upstream calls of run {self._run_id} failed",
        )

    async def _close_relay(self) -> None:
        failure = await _failure_of(self._relay.close(), f"run-tools:{self._run_id}:relay-close")
        if failure is not None:
            log_failure(log, f"Closing the relay of run {self._run_id} failed", failure)
