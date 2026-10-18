"""A run's tool set: opened after discovery and closed in five bounded, ordered steps.

Every row drives ``RunTools`` over the real relay on sockets and the real ``open_app_tools``
against ``McpAppFake`` over TLS, on a ``SteppedLoop`` whose ``StepClock`` is the run's clock.
A row runs its closer as its own task under a watchdog that never cancels it, so a close
that waits for a deadline or for the peer fails the row instead of hanging it.
"""

import asyncio
import contextlib
import errno
import gc
import json
import ssl
import threading
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, cast

import anyio
import httpcore._backends.anyio as httpcore_anyio
import httpx
import pytest
from capability_support import canary_token, capability_carrying
from mcp_app_fake import (
    CERTIFICATE_HOST,
    WAIT_SECONDS,
    FakeResponse,
    McpAppFake,
    Responder,
    ScriptedApp,
    rpc_result,
    serving,
    tool_listing,
)
from relay_support import (
    RUN_ID,
    RawClient,
    SuppressingOwner,
    json_rpc,
    parked_at,
    port_of,
    post,
    post_message,
    relay_tasks,
    yield_until,
)
from service_support import (
    StepClock,
    SteppedLoop,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
)

from agent_providers.errors import ContainmentLost, RetainedGroup
from agent_service.app_tools import UpstreamRefused, open_app_tools
from agent_service.mcp_wire import CallToolParams
from agent_service.run_tools import (
    UPSTREAM_ABORT_SECONDS,
    UPSTREAM_CLOSE_SECONDS,
    RunTools,
)
from agent_service.tool_relay import (
    RELAY_CLOSE_SECONDS,
    Relay,
    RepeatedToolOffer,
    ToolCallBarrier,
)

SUPPORTED = "2025-11-25"
TOKEN = canary_token()
CAPABILITY = capability_carrying(TOKEN, tools=("edit_song",))
OFFERED = ["edit_song", "delete_song"]
CALLS_EVENT = f"Closing the upstream calls of run {RUN_ID} failed"
CLIENT_EVENT = f"Leaving the app's client of run {RUN_ID} failed"
RELAY_EVENT = f"Closing the relay of run {RUN_ID} failed"
HEAD_BYTES = 40
REFUSAL = {"code": -32602, "message": "Invalid params"}
CLEAN_CLOSE_SECONDS = 0.0
PARKS_OF_EVERY_STEP = (
    UPSTREAM_CLOSE_SECONDS,
    UPSTREAM_CLOSE_SECONDS + UPSTREAM_ABORT_SECONDS,
    UPSTREAM_CLOSE_SECONDS + UPSTREAM_ABORT_SECONDS + RELAY_CLOSE_SECONDS,
)


def refused(request_id: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": REFUSAL}


def call_result(request: Any) -> FakeResponse:
    return rpc_result(request, {"content": [{"type": "text", "text": "done"}]})


def held_before_the_head(gate: threading.Event) -> Mapping[str, Responder]:
    def respond(request: Any) -> FakeResponse:
        gate.wait(WAIT_SECONDS)
        return call_result(request)

    return {"tools/call": respond}


def held_after_the_head(_gate: threading.Event) -> Mapping[str, Responder]:
    def respond(request: Any) -> FakeResponse:
        answer = call_result(request)
        return FakeResponse(body=answer.body[:HEAD_BYTES], held_back=answer.body[HEAD_BYTES:])

    return {"tools/call": respond}


def task_names(prefix: str) -> list[str]:
    return sorted(
        name for task in asyncio.all_tasks() if (name := task.get_name()).startswith(prefix)
    )


def injected_reap(
    *,
    gate: asyncio.Event | None = None,
    raises: BaseException | None = None,
    on_entry: Callable[[], Awaitable[None]] | None = None,
) -> Callable[[], Awaitable[None]]:
    async def reap() -> None:
        if on_entry is not None:
            await on_entry()
        if gate is not None:
            await gate.wait()
        if raises is not None:
            raise raises

    return reap


class Stall:
    """Absorbs every cancellation of the target's first call until released, then runs it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, owner: type, name: str) -> None:
        self.gate = asyncio.Event()
        self.pending = False
        self.original_ran = False
        self.cancelling = 0
        self._claimed = False
        original = getattr(owner, name)

        async def stalled(*args: Any, **kwargs: Any) -> Any:
            if self._claimed:
                return await original(*args, **kwargs)
            self._claimed = True
            self.pending = True
            while not self.gate.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await self.gate.wait()
            self.pending = False
            self.cancelling = cast(asyncio.Task[Any], asyncio.current_task()).cancelling()
            self.original_ran = True
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, name, stalled)


class ListenerRecorder:
    """Wraps ``asyncio.start_server``: records each listener, or fails with ``failure``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.servers: list[asyncio.Server] = []
        self.ports: list[int] = []
        self.failure: OSError | None = None
        original = asyncio.start_server

        async def recording(*args: Any, **kwargs: Any) -> asyncio.Server:
            if self.failure is not None:
                raise self.failure
            server = await original(*args, **kwargs)
            self.servers.append(server)
            self.ports.append(server.sockets[0].getsockname()[1])
            return server

        monkeypatch.setattr(asyncio, "start_server", recording)

    @property
    def serving(self) -> list[asyncio.Server]:
        return [server for server in self.servers if server.is_serving()]


def failing_transport_close(monkeypatch: pytest.MonkeyPatch) -> None:
    original = httpx.AsyncHTTPTransport.aclose
    request = httpx.Request(
        "POST", "https://localhost/mcp", headers={"Authorization": f"Bearer {TOKEN}"}
    )

    async def failing(transport: httpx.AsyncHTTPTransport) -> None:
        await original(transport)
        raise httpx.CloseError("the close failed", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "aclose", failing)


def recording_client_exits(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    upstream_tasks_at_exit: list[list[str]] = []
    original = httpx.AsyncClient.aclose

    async def recording(client: httpx.AsyncClient) -> None:
        upstream_tasks_at_exit.append(
            [name for name in task_names(f"upstream:{RUN_ID}:") if not name.endswith("-deadline")]
        )
        await original(client)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", recording)
    return upstream_tasks_at_exit


class Scene:
    def __init__(
        self,
        fake: McpAppFake,
        context: ssl.SSLContext,
        app_gate: threading.Event,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self.fake = fake
        self.context = context
        self.app_gate = app_gate
        self.monkeypatch = monkeypatch
        self.caplog = caplog
        self.clock = StepClock()
        self.listeners = ListenerRecorder(monkeypatch)
        self.stalls: list[Stall] = []
        self._releases: list[Callable[[], None]] = []

    async def open(self) -> RunTools:
        return await RunTools.open(RUN_ID, self.fake.origin, CAPABILITY, self.context, self.clock)

    def stall(self, owner: type, name: str) -> Stall:
        stall = Stall(self.monkeypatch, owner, name)
        self.stalls.append(stall)
        return stall

    def on_release(self, release: Callable[[], None]) -> None:
        self._releases.append(release)

    def app_calls(self) -> list[dict[str, Any]]:
        return [
            request.message["params"]
            for request in self.fake.requests
            if request.rpc_method == "tools/call"
        ]

    async def release_and_drain(self) -> None:
        self.app_gate.set()
        self.fake.tail_released.set()
        for stall in self.stalls:
            stall.gate.set()
        for release in self._releases:
            release()
        current = asyncio.current_task()
        leftovers = {
            task
            for task in asyncio.all_tasks()
            if task is not current
            and task.get_name().startswith((f"upstream:{RUN_ID}", f"relay:{RUN_ID}", "run-tools:"))
        }
        if leftovers:
            await asyncio.wait(leftovers, timeout=WAIT_SECONDS)

    def logged(self) -> list[tuple[str, str]]:
        gc.collect()
        return [(event, kind) for event, kind, _ in failure_lines(self.caplog)]

    async def app_saw_every_connection_end(self) -> None:
        await yield_until(lambda: self.fake.ended_connections == self.fake.accepted_connections)

    async def assert_nothing_left(self) -> None:
        assert relay_tasks() == []
        assert task_names(f"upstream:{RUN_ID}:") == []
        assert self.listeners.serving == []
        for port in self.listeners.ports:
            with pytest.raises(ConnectionRefusedError):
                await RawClient.connect(port)


def run_scene(
    scenario: Callable[[Scene], Awaitable[None]],
    authority: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    app_holds: Callable[[threading.Event], Mapping[str, Responder]] = lambda _gate: {},
    pages: list[dict[str, Any]] | None = None,
) -> None:
    gate = threading.Event()
    app = ScriptedApp(
        pages=pages or [tool_listing(OFFERED)],
        overrides=app_holds(gate),
    )
    with serving(app, authority.credentials_for(CERTIFICATE_HOST), keep_alive=True) as fake:
        scene = Scene(fake, authority.client_context(), gate, monkeypatch, caplog)

        async def main() -> None:
            try:
                await scenario(scene)
            finally:
                await scene.release_and_drain()

        with asyncio.Runner(loop_factory=SteppedLoop) as runner:
            runner.run(main())


async def finished(task: asyncio.Task[Any]) -> None:
    done, _ = await asyncio.wait({task}, timeout=WAIT_SECONDS)
    assert task in done


async def ending_of(task: asyncio.Task[Any]) -> object:
    await finished(task)
    (ending,) = await asyncio.gather(task, return_exceptions=True)
    return ending


async def through_parks(scene: Scene, parks: tuple[float, ...]) -> None:
    for instant in parks:
        await parked_at(scene.clock, instant)
        scene.clock.advance_to(instant)


def start_close(tools: RunTools, reap: Callable[[], Awaitable[None]]) -> asyncio.Task[None]:
    return asyncio.get_running_loop().create_task(tools.close(reap), name="test:closer")


def cli_call(tools: RunTools, request_id: int) -> bytes:
    params = {"name": "edit_song", "arguments": {}}
    body = json_rpc("tools/call", request_id, params)
    return post_message(cast(Relay, tools), body, SUPPORTED)


async def reply_of(client: RawClient) -> dict[str, Any]:
    return json.loads((await client.answer()).body)


async def forwarded_from_the_cli(scene: Scene, tools: RunTools) -> tuple[RawClient, RawClient]:
    port = port_of(cast(Relay, tools))
    first, idle = await RawClient.connect(port), await RawClient.connect(port)
    await first.send(cli_call(tools, 1))
    event = await tools.calls.next_event()
    tools.calls.accept(event)
    await yield_until(lambda: scene.app_calls() != [])
    return first, idle


class Stalls(Enum):
    FORWARD_AND_SETTLE = auto()
    CLIENT = auto()
    EVERY_STEP = auto()


PARKS = {
    Stalls.FORWARD_AND_SETTLE: (UPSTREAM_CLOSE_SECONDS,),
    Stalls.CLIENT: (UPSTREAM_ABORT_SECONDS,),
    Stalls.EVERY_STEP: PARKS_OF_EVERY_STEP,
}
STALLED_LINES = {
    Stalls.FORWARD_AND_SETTLE: [(CALLS_EVENT, "UpstreamCleanupIncomplete")],
    Stalls.CLIENT: [(CLIENT_EVENT, "UpstreamCleanupIncomplete")],
    Stalls.EVERY_STEP: [
        (CALLS_EVENT, "UpstreamCleanupIncomplete"),
        (CLIENT_EVENT, "UpstreamCleanupIncomplete"),
        (RELAY_EVENT, "RelayCleanupIncomplete"),
    ],
}


async def put_a_forwarded_call_on_the_barrier(scene: Scene, barrier: ToolCallBarrier) -> None:
    asyncio.get_running_loop().create_task(
        barrier.call(1, CallToolParams(name="edit_song", arguments={}))
    )
    barrier.accept(await barrier.next_event())
    await yield_until(lambda: scene.app_calls() != [])


async def stalled_tools(scene: Scene, plan: Stalls) -> RunTools:
    if plan is not Stalls.EVERY_STEP:
        tools = await scene.open()
        if plan is Stalls.FORWARD_AND_SETTLE:
            scene.stall(httpcore_anyio.AnyIOStream, "aclose")
            scene.stall(ToolCallBarrier, "settle")
            await put_a_forwarded_call_on_the_barrier(scene, tools.calls)
        else:
            scene.stall(httpcore_anyio.AnyIOStream, "aclose")
        return tools
    upstream = AsyncExitStack()
    session = await upstream.enter_async_context(
        open_app_tools(scene.fake.origin, CAPABILITY, scene.context)
    )
    barrier = ToolCallBarrier(RUN_ID, session, CAPABILITY)
    owner = SuppressingOwner()
    scene.on_release(owner.release)
    relay = await Relay.start(RUN_ID, owner, scene.clock)
    holding = await RawClient.connect(port_of(relay))
    await holding.send(post(relay))
    await owner.wait_entered()
    scene.stall(httpcore_anyio.AnyIOStream, "aclose")
    scene.stall(httpx.AsyncHTTPTransport, "aclose")
    scene.stall(ToolCallBarrier, "settle")
    await put_a_forwarded_call_on_the_barrier(scene, barrier)
    return RunTools(RUN_ID, scene.clock, relay, barrier, upstream)


@pytest.mark.parametrize("app_holds", [held_before_the_head, held_after_the_head])
def test_the_close_stops_before_the_reap_and_needs_neither_clock_time_nor_the_peer(
    authority, monkeypatch, caplog, app_holds
):
    async def scenario(scene: Scene) -> None:
        tools = await scene.open()
        client_exits = recording_client_exits(scene.monkeypatch)
        first, idle = await forwarded_from_the_cli(scene, tools)
        port = port_of(cast(Relay, tools))
        seen_at_reap: list[list[str]] = []

        async def at_the_reap() -> None:
            with pytest.raises(ConnectionRefusedError):
                await RawClient.connect(port)
            await idle.send(cli_call(tools, 2))
            assert await reply_of(idle) == refused(2)
            assert await reply_of(first) == refused(1)
            seen_at_reap.append(task_names(f"upstream:{RUN_ID}:") + task_names(f"relay:{RUN_ID}:"))

        closer = start_close(tools, injected_reap(on_entry=at_the_reap))
        assert await ending_of(closer) is None

        (running_at_reap,) = seen_at_reap
        assert {
            f"upstream:{RUN_ID}:call-1",
            f"relay:{RUN_ID}:conn-1",
            f"relay:{RUN_ID}:conn-2",
        } <= set(running_at_reap)
        assert client_exits == [[f"upstream:{RUN_ID}:client"]]
        assert scene.clock.now == CLEAN_CLOSE_SECONDS
        assert not scene.app_gate.is_set()
        await scene.assert_nothing_left()
        await scene.release_and_drain()
        assert len(scene.app_calls()) == 1
        await scene.app_saw_every_connection_end()
        assert scene.logged() == []

    run_scene(scenario, authority, monkeypatch, caplog, app_holds)


@pytest.mark.parametrize("plan", list(Stalls))
def test_a_stalled_cleanup_ends_its_step_at_its_own_deadline_and_still_closes_after_it(
    authority, monkeypatch, caplog, plan
):
    async def scenario(scene: Scene) -> None:
        tools = await stalled_tools(scene, plan)
        closer = start_close(tools, injected_reap())

        await through_parks(scene, PARKS[plan])

        assert await ending_of(closer) is None
        assert scene.clock.now == PARKS[plan][-1]
        assert [stall.pending for stall in scene.stalls] == [True] * len(scene.stalls)
        await scene.release_and_drain()
        assert [stall.cancelling for stall in scene.stalls] == [1] * len(scene.stalls)
        assert all(stall.original_ran for stall in scene.stalls)
        await scene.assert_nothing_left()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == STALLED_LINES[plan]

    run_scene(scenario, authority, monkeypatch, caplog, held_before_the_head)


def a_loss() -> ContainmentLost:
    return ContainmentLost(RetainedGroup(4242, 7))


def test_a_containment_loss_is_raised_after_the_last_step_when_the_steps_are_clean(
    authority, monkeypatch, caplog
):
    loss = a_loss()

    async def scenario(scene: Scene) -> None:
        tools = await scene.open()
        await forwarded_from_the_cli(scene, tools)
        closer = start_close(tools, injected_reap(raises=loss))

        assert await ending_of(closer) is loss
        assert scene.clock.now == CLEAN_CLOSE_SECONDS
        await scene.assert_nothing_left()
        await scene.release_and_drain()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == []

    run_scene(scenario, authority, monkeypatch, caplog, held_before_the_head)


def test_a_containment_loss_is_raised_after_the_last_step_when_every_step_stalls(
    authority, monkeypatch, caplog
):
    loss = a_loss()

    async def scenario(scene: Scene) -> None:
        tools = await stalled_tools(scene, Stalls.EVERY_STEP)
        closer = start_close(tools, injected_reap(raises=loss))

        await through_parks(scene, PARKS[Stalls.EVERY_STEP])

        assert await ending_of(closer) is loss
        assert scene.logged() == STALLED_LINES[Stalls.EVERY_STEP]

    run_scene(scenario, authority, monkeypatch, caplog, held_before_the_head)


class Cancellation(Enum):
    DIRECT = auto()
    LEVEL = auto()


@pytest.mark.parametrize("cancellation", list(Cancellation))
@pytest.mark.parametrize("raises_loss", [False, True])
def test_a_callers_cancellation_never_cuts_the_close_short_and_is_delivered_after_it(
    authority, monkeypatch, caplog, cancellation, raises_loss
):
    loss = a_loss()

    async def scenario(scene: Scene) -> None:
        tools = await scene.open()
        await forwarded_from_the_cli(scene, tools)
        in_the_reap, reap_gate = asyncio.Event(), asyncio.Event()
        reached_after_close = asyncio.Event()
        scope = anyio.CancelScope()

        async def entered() -> None:
            in_the_reap.set()

        reap = injected_reap(gate=reap_gate, raises=loss if raises_loss else None, on_entry=entered)

        async def closing_in_scope() -> None:
            with scope:
                await tools.close(reap)
                reached_after_close.set()

        closer = asyncio.get_running_loop().create_task(
            closing_in_scope() if cancellation is Cancellation.LEVEL else tools.close(reap),
            name="test:closer",
        )
        await in_the_reap.wait()
        if cancellation is Cancellation.LEVEL:
            scope.cancel()
        else:
            closer.cancel()
        await cast(SteppedLoop, asyncio.get_running_loop()).settled()
        assert not closer.done()
        reap_gate.set()

        ending = await ending_of(closer)

        if raises_loss:
            assert ending is loss
            assert scope.cancelled_caught is False
        elif cancellation is Cancellation.DIRECT:
            assert isinstance(ending, asyncio.CancelledError)
        else:
            assert ending is None
            assert scope.cancelled_caught
            assert not reached_after_close.is_set()
        await scene.assert_nothing_left()
        await scene.release_and_drain()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == []

    run_scene(scenario, authority, monkeypatch, caplog, held_before_the_head)


def test_a_failing_step_is_logged_by_class_and_leaks_no_token(authority, monkeypatch, caplog):
    async def scenario(scene: Scene) -> None:
        tools = await scene.open()
        failing_transport_close(scene.monkeypatch)
        closer = start_close(tools, injected_reap())

        assert await ending_of(closer) is None
        await scene.assert_nothing_left()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == [(CLIENT_EVENT, "UpstreamCallLost")]
        signature = TOKEN.rsplit(".", 1)[-1]
        assert [secret in caplog.text for secret in (TOKEN, signature)] == [False, False]

    run_scene(scenario, authority, monkeypatch, caplog)


def test_open_builds_the_tool_set_after_discovery(authority, monkeypatch, caplog):
    def listing_held(gate: threading.Event) -> Mapping[str, Responder]:
        def respond(request: Any) -> FakeResponse:
            gate.wait(WAIT_SECONDS)
            return rpc_result(request, tool_listing(OFFERED))

        return {"tools/list": respond}

    async def scenario(scene: Scene) -> None:
        opening = asyncio.get_running_loop().create_task(scene.open(), name="test:opener")
        await yield_until(lambda: any(r.rpc_method == "tools/list" for r in scene.fake.requests))
        assert scene.listeners.servers == []
        scene.app_gate.set()

        await finished(opening)
        tools = opening.result()

        assert len(scene.listeners.serving) == 1
        client = await RawClient.connect(port_of(cast(Relay, tools)))
        await client.send(post_message(cast(Relay, tools), json_rpc("tools/list", 1), SUPPORTED))
        listed = await reply_of(client)
        assert [tool["name"] for tool in listed["result"]["tools"]] == ["edit_song"]
        await client.send(cli_call(tools, 2))
        assert (await tools.calls.next_event()).name == "edit_song"
        closer = start_close(tools, injected_reap())
        assert await ending_of(closer) is None
        await scene.assert_nothing_left()
        await scene.app_saw_every_connection_end()

    run_scene(scenario, authority, monkeypatch, caplog, listing_held)


@dataclass(frozen=True)
class FailedOpen:
    expected: type[BaseException]
    lines: list[tuple[str, str]]
    refuses_listing: bool = False
    offers_twice: bool = False
    fails_transport_close: bool = False
    stalls_cleanup: bool = False


FAILED_OPENS = {
    "the listing is refused": FailedOpen(UpstreamRefused, [], refuses_listing=True),
    "an allowed tool is offered twice and the client's exit fails": FailedOpen(
        RepeatedToolOffer,
        [(CLIENT_EVENT, "UpstreamCallLost")],
        offers_twice=True,
        fails_transport_close=True,
    ),
    "the relay cannot listen and the client's cleanup stalls": FailedOpen(
        OSError, [(CLIENT_EVENT, "UpstreamCleanupIncomplete")], stalls_cleanup=True
    ),
}


@pytest.mark.parametrize("case", FAILED_OPENS.values(), ids=FAILED_OPENS.keys())
def test_a_failed_open_leaves_the_client_and_keeps_its_own_failure(
    authority, monkeypatch, caplog, case
):
    def refusing_the_listing(_gate: threading.Event) -> Mapping[str, Responder]:
        refusal = FakeResponse(status=500, content_type=None)
        return {"tools/list": lambda _request: refusal} if case.refuses_listing else {}

    async def scenario(scene: Scene) -> None:
        address_in_use = OSError(errno.EADDRINUSE, "in use")
        if case.stalls_cleanup:
            scene.listeners.failure = address_in_use
            stall = scene.stall(httpcore_anyio.AnyIOStream, "aclose")
        if case.fails_transport_close:
            failing_transport_close(scene.monkeypatch)
        opening = asyncio.get_running_loop().create_task(scene.open(), name="test:opener")

        if case.stalls_cleanup:
            await through_parks(scene, (UPSTREAM_ABORT_SECONDS,))
        ending = await ending_of(opening)

        assert isinstance(ending, case.expected)
        if case.stalls_cleanup:
            assert ending is address_in_use
            assert stall.pending
            await scene.release_and_drain()
            assert stall.original_ran
        assert scene.listeners.serving == []
        await scene.release_and_drain()
        await scene.assert_nothing_left()
        await scene.app_saw_every_connection_end()
        assert scene.logged() == case.lines

    pages = [tool_listing(["edit_song", "edit_song"])] if case.offers_twice else None
    run_scene(scenario, authority, monkeypatch, caplog, refusing_the_listing, pages)
