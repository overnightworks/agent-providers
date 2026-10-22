import asyncio
import contextlib
import contextvars
import gc
import json
import re
import socket
import struct
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from http import HTTPStatus
from typing import Any, Protocol
from urllib.parse import urlsplit

import h11
import pytest
from bounds import TEST_WAIT_SECONDS, within_the_bound
from service_support import StepClock, SteppedLoop

from agent_runtime.cli_run import RelayAccess
from agent_runtime_client.mcp_wire import (
    JSON_MEDIA_TYPE,
    PROTOCOL_VERSION_HEADER,
    CallToolParams,
    HttpAnswer,
    JsonRpcResponse,
    Reply,
    RequestId,
    ToolOutcome,
)
from agent_runtime_client.wire import JsonObject
from agent_runtime_service.run_liveness import LoopClock, RunClock
from agent_runtime_service.tool_relay import (
    LOOPBACK_HOST,
    RECEIVE_CHUNK_BYTES,
    Relay,
    RelayOwner,
    RelayRequest,
    Unanswered,
)

RUN_ID = "run-under-test"
_READ_BYTES = 65_536
_CLIENT_REQUEST = h11.Request(method="POST", target="/mcp", headers=[("host", "x")])


OWNER_BODY = b'{"owner":"answer"}'
OWNER_ANSWER = HttpAnswer(HTTPStatus.OK, JSON_MEDIA_TYPE, OWNER_BODY)


class RecordingApp:
    """An ``AppTools`` fake that records each call, answers at once or once ``release`` opened
    its gate, and raises a scripted failure instead of the outcome."""

    def __init__(
        self,
        outcome: ToolOutcome | None = None,
        failure: BaseException | None = None,
        *,
        gated: bool = False,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.received: list[Mapping[str, Any]] = []
        self._outcome = outcome
        self._failure = failure
        self._gate = asyncio.Event()
        if not gated:
            self._gate.set()

    @property
    def called(self) -> list[str]:
        return [name for name, _ in self.calls]

    def forget_failure(self) -> None:
        self._failure = None

    def release(self) -> None:
        self._gate.set()

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        self.calls.append((name, dict(arguments)))
        self.received.append(arguments)
        await self._gate.wait()
        if self._failure is not None:
            raise self._failure
        return self._outcome or ToolOutcome(f"ran {name}", is_error=False)

    async def forwarded(self, count: int) -> None:
        await yield_until(lambda: len(self.calls) == count)


class ScriptedOwner:
    """A fake of the ``RelayOwner`` port: records requests, can gate, fail and be cancelled."""

    def __init__(
        self,
        answers: Sequence[HttpAnswer | Unanswered] = (),
        failure: Exception | None = None,
        *,
        gated: bool = False,
    ) -> None:
        self.requests: list[RelayRequest] = []
        self.entered = asyncio.Event()
        self.cancelled = False
        self._answers = list(answers)
        self._failure = failure
        self._gate = asyncio.Event()
        if not gated:
            self.release()

    def release(self) -> None:
        self._gate.set()

    def hold(self) -> None:
        self._gate.clear()

    async def answer(self, request: RelayRequest) -> HttpAnswer | Unanswered:
        self.requests.append(request)
        self.entered.set()
        await self._wait_for_gate()
        if self._failure is not None:
            raise self._failure
        return self._answers.pop(0) if self._answers else OWNER_ANSWER

    async def _wait_for_gate(self) -> None:
        try:
            await self._gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    @property
    def bodies(self) -> list[bytes]:
        return [request.body for request in self.requests]

    async def wait_entered(self) -> None:
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            await self.entered.wait()


class ScriptedCalls:
    """A fake of the ``ToolCalls`` port: records calls and cancels, can gate, and be cancelled."""

    def __init__(self, replies: Sequence[Reply | Unanswered] = (), *, gated: bool = False) -> None:
        self.calls: list[tuple[RequestId, CallToolParams]] = []
        self.cancelled_ids: list[RequestId] = []
        self.entered = asyncio.Event()
        self.cancelled = False
        self.unoffered_count = 0
        self.reflected_count = 0
        self._replies = list(replies)
        self._gate = asyncio.Event()
        if not gated:
            self.release()

    def release(self) -> None:
        self._gate.set()

    async def call(self, request_id: RequestId, params: CallToolParams) -> Reply | Unanswered:
        self.calls.append((request_id, params))
        self.entered.set()
        try:
            await self._gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return self._replies.pop(0) if self._replies else clean_reply(request_id)

    def cancel(self, request_id: RequestId) -> None:
        self.cancelled_ids.append(request_id)

    def unoffered(self) -> None:
        self.unoffered_count += 1

    def reflected(self) -> None:
        self.reflected_count += 1

    async def wait_entered(self) -> None:
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            await self.entered.wait()


def clean_reply(request_id: RequestId) -> JsonRpcResponse:
    return JsonRpcResponse(id=request_id, result={"content": [{"type": "text", "text": "done"}]})


class SuppressingOwner(ScriptedOwner):
    """Breaks the ``RelayOwner`` contract: swallows every cancellation until released."""

    def __init__(self) -> None:
        super().__init__(gated=True)

    async def _wait_for_gate(self) -> None:
        while not self._gate.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await self._gate.wait()


class AbsorbingOwner(ScriptedOwner):
    """Breaks the ``RelayOwner`` contract once: absorbs the first cancellation, then waits."""

    def __init__(self) -> None:
        super().__init__(gated=True)

    async def _wait_for_gate(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await self._gate.wait()
        await super()._wait_for_gate()


class Delivery(Enum):
    ANSWERED = "answered"
    INTERRUPTED = "interrupted"
    ABSENT = "absent"


class ConnectionEnded(AssertionError):
    def __init__(self, delivery: Delivery) -> None:
        super().__init__(f"connection ended before an answer: {delivery.value}")
        self.delivery = delivery


@dataclass(frozen=True)
class Answer:
    status: int
    headers: dict[str, str]
    body: bytes


class HasRelayAccess(Protocol):
    @property
    def access(self) -> RelayAccess: ...


@dataclass(frozen=True)
class RelayDoor:
    """Where a client reaches a relay, known only by its access value."""

    access: RelayAccess


@dataclass(frozen=True)
class RequestHead:
    method: str
    target: str
    version: str
    fields: tuple[tuple[str, str], ...]

    @classmethod
    def standard(cls, relay: HasRelayAccess, content_length: int = 0) -> "RequestHead":
        return cls(
            "POST",
            urlsplit(relay.access.url).path,
            "HTTP/1.1",
            (
                ("Host", f"{LOOPBACK_HOST}:{port_of(relay)}"),
                ("Authorization", f"Bearer {relay.access.secret}"),
                ("Content-Length", str(content_length)),
            ),
        )

    def with_field(self, name: str, value: str) -> "RequestHead":
        return self.without(name).adding(name, value)

    def without(self, name: str) -> "RequestHead":
        kept = tuple(item for item in self.fields if item[0].lower() != name.lower())
        return replace(self, fields=kept)

    def replace_target(self, target: str) -> "RequestHead":
        return replace(self, target=target)

    def replace_method(self, method: str) -> "RequestHead":
        return replace(self, method=method)

    def adding(self, name: str, value: str) -> "RequestHead":
        return replace(self, fields=(*self.fields, (name, value)))

    def encode(self) -> bytes:
        lines = [f"{self.method} {self.target} {self.version}"]
        lines += [f"{name}: {value}" for name, value in self.fields]
        return ("\r\n".join(lines) + "\r\n\r\n").encode()


def port_of(relay: HasRelayAccess) -> int:
    port = urlsplit(relay.access.url).port
    assert port is not None
    return port


class RelayWrites:
    """Records what each socket send hands to the kernel, so no delivery order hides it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._writes: list[tuple[Any, bytes]] = []
        monkeypatch.setattr(socket.socket, "send", self._recorded("send", lambda data: data))
        monkeypatch.setattr(socket.socket, "sendall", self._recorded("sendall", lambda data: data))
        monkeypatch.setattr(
            socket.socket, "sendmsg", self._recorded("sendmsg", lambda buffers: b"".join(buffers))
        )

    def _recorded(self, method: str, joined: Callable[[Any], bytes]) -> Callable[..., Any]:
        original = getattr(socket.socket, method)

        def recording(sock: socket.socket, data: Any, *rest: Any) -> Any:
            peer = sock.getpeername()
            data = list(data) if method == "sendmsg" else data
            sent = original(sock, data, *rest)
            offered = bytes(joined(data))
            self._writes.append((peer, offered if method == "sendall" else offered[:sent]))
            return sent

        return recording

    def to(self, client: "RawClient") -> bytes:
        return b"".join(data for peer, data in self._writes if peer == client.address)


class RelayReads:
    """Records how many bytes each relay task's stream reads returned, per task name.

    A task name is reused across request cycles, so a test snapshots ``read_by`` before a send
    and waits for that count plus the bytes sent.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._bytes_by_task: Counter[str] = Counter()
        self._aggregated_minimum: dict[str, int] = {}
        original = asyncio.StreamReader.read

        async def recording(reader: asyncio.StreamReader, size: int = -1) -> bytes:
            name = _current_task_name()
            if not name.startswith("relay:"):
                return await original(reader, size)
            minimum = self._aggregated_minimum.pop(name, None)
            if minimum is None:
                chunk = await original(reader, size)
            else:
                chunk = await _gathered(original, reader, size, minimum)
            self._bytes_by_task[name] += len(chunk)
            return chunk

        monkeypatch.setattr(asyncio.StreamReader, "read", recording)

    def aggregate_first_read(self, task_name: str, minimum_bytes: int) -> None:
        """Make the named task's next read return one chunk of exactly ``minimum_bytes``."""
        self._aggregated_minimum[task_name] = minimum_bytes

    def read_by(self, task_name: str) -> int:
        return self._bytes_by_task[task_name]

    async def until_read(self, task_name: str, count: int) -> None:
        await yield_until(lambda: self.read_by(task_name) >= count)


def _current_task_name() -> str:
    task = asyncio.current_task()
    assert task is not None
    return task.get_name()


async def _gathered(
    read: Callable[[asyncio.StreamReader, int], Awaitable[bytes]],
    reader: asyncio.StreamReader,
    size: int,
    minimum: int,
) -> bytes:
    if not 0 < minimum <= size <= RECEIVE_CHUNK_BYTES:
        raise ValueError(f"cannot aggregate {minimum} bytes into a read of {size}")
    gathered = b""
    async with asyncio.timeout(TEST_WAIT_SECONDS):
        while len(gathered) < minimum:
            chunk = await read(reader, minimum - len(gathered))
            if not chunk:
                raise AssertionError("EOF before the aggregated read was complete")
            gathered += chunk
    return gathered


class RawClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._parser = self._new_parser()
        self._received = 0

    @classmethod
    async def connect(cls, port: int, host: str = LOOPBACK_HOST) -> "RawClient":
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            reader, writer = await asyncio.open_connection(host, port)
        return cls(reader, writer)

    @classmethod
    async def adopt(cls, connected: socket.socket) -> "RawClient":
        connected.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=connected)
        return cls(reader, writer)

    @staticmethod
    def _new_parser() -> h11.Connection:
        parser = h11.Connection(h11.CLIENT)
        RawClient._prime(parser)
        return parser

    @staticmethod
    def _prime(parser: h11.Connection) -> None:
        parser.send(_CLIENT_REQUEST)
        parser.send(h11.EndOfMessage())

    @property
    def received_bytes(self) -> int:
        return self._received

    @property
    def address(self) -> Any:
        return self._writer.get_extra_info("sockname")

    async def send(self, raw: bytes) -> None:
        self._writer.write(raw)
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            await self._writer.drain()

    async def answer(self) -> Answer:
        status = 0
        headers: dict[str, str] = {}
        body = b""
        self._received = 0
        while True:
            event = self._parser.next_event()
            if event is h11.NEED_DATA:
                self._parser.receive_data(await self._read_response_bytes())
            elif isinstance(event, h11.Response):
                status = event.status_code
                headers = {name.decode(): value.decode() for name, value in event.headers}
            elif isinstance(event, h11.Data):
                body += bytes(event.data)
            elif isinstance(event, h11.EndOfMessage):
                if self._parser.our_state is h11.DONE and self._parser.their_state is h11.DONE:
                    self._parser.start_next_cycle()
                    self._prime(self._parser)
                return Answer(status, headers, body)
            else:
                raise AssertionError(f"unexpected {event!r}")

    async def _read_response_bytes(self) -> bytes:
        try:
            async with asyncio.timeout(TEST_WAIT_SECONDS):
                chunk = await self._reader.read(_READ_BYTES)
        except ConnectionResetError:
            chunk = b""
        if not chunk:
            raise ConnectionEnded(Delivery.INTERRUPTED if self._received else Delivery.ABSENT)
        self._received += len(chunk)
        return chunk

    async def delivery(self) -> Delivery:
        try:
            await self.answer()
        except ConnectionEnded as ended:
            return ended.delivery
        return Delivery.ANSWERED

    def has_ended(self) -> bool:
        return self._reader.at_eof()

    async def at_eof(self) -> bool:
        try:
            async with asyncio.timeout(TEST_WAIT_SECONDS):
                while await self._reader.read(_READ_BYTES):
                    pass
        except ConnectionResetError:
            pass
        return True

    def finish_sending(self) -> None:
        self._writer.write_eof()

    def disconnect(self) -> None:
        self._writer.close()

    def reset(self) -> None:
        sock = self._writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self._writer.close()


def blocking_connect(port: int) -> socket.socket:
    """Connect in the kernel without yielding to the event loop."""
    return socket.create_connection((LOOPBACK_HOST, port), timeout=TEST_WAIT_SECONDS)


def post(relay: HasRelayAccess, body: bytes = b"") -> bytes:
    return RequestHead.standard(relay, len(body)).encode() + body


def json_rpc(
    method: str, request_id: RequestId | None = None, params: JsonObject | None = None
) -> bytes:
    message: dict[str, object] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode()


def post_message(relay: HasRelayAccess, body: bytes, protocol_version: str | None = None) -> bytes:
    head = RequestHead.standard(relay, len(body)).with_field("Content-Type", JSON_MEDIA_TYPE)
    if protocol_version is not None:
        head = head.with_field(PROTOCOL_VERSION_HEADER, protocol_version)
    return head.encode() + body


def chunked_head(relay: HasRelayAccess) -> RequestHead:
    return (
        RequestHead.standard(relay).without("Content-Length").adding("Transfer-Encoding", "chunked")
    )


def chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


def record_task_names() -> list[str]:
    """Names of every task created on the running loop from now on, in creation order."""
    names: list[str] = []

    class RecordingTask(asyncio.Task):
        def __init__(self, coro: object, **kwargs: object) -> None:
            super().__init__(coro, **kwargs)
            if (name := kwargs.get("name")) is not None:
                names.append(str(name))

        def set_name(self, value: object) -> None:
            names.append(str(value))
            super().set_name(value)

    def factory(
        loop: asyncio.AbstractEventLoop, coro: object, **kwargs: contextvars.Context
    ) -> asyncio.Task:
        return RecordingTask(coro, loop=loop, **kwargs)

    asyncio.get_running_loop().set_task_factory(factory)
    return names


def count_retrievals() -> Counter[str]:
    """Counts ``result()`` and ``exception()`` calls per name of every task created from now on."""
    retrievals: Counter[str] = Counter()

    class CountingTask(asyncio.Task):
        def result(self) -> object:
            retrievals[self.get_name()] += 1
            return super().result()

        def exception(self) -> BaseException | None:
            retrievals[self.get_name()] += 1
            return super().exception()

    def factory(
        loop: asyncio.AbstractEventLoop, coro: object, **kwargs: contextvars.Context
    ) -> asyncio.Task:
        return CountingTask(coro, loop=loop, **kwargs)

    asyncio.get_running_loop().set_task_factory(factory)
    return retrievals


def relay_tasks(run_id: str = RUN_ID) -> list[str]:
    prefix = f"relay:{run_id}:"
    return sorted(
        name for task in asyncio.all_tasks() if (name := task.get_name()).startswith(prefix)
    )


def task_named(name: str) -> asyncio.Task[Any]:
    return next(task for task in asyncio.all_tasks() if task.get_name() == name)


def retained_relay_tasks(run_id: str = RUN_ID) -> list[str]:
    """Names of the relay's tasks that are still referenced, finished or not."""
    gc.collect()
    prefix = f"relay:{run_id}:"
    return sorted(
        name
        for obj in gc.get_objects()
        if isinstance(obj, asyncio.Task) and (name := obj.get_name()).startswith(prefix)
    )


def connection_numbers(task_names: list[str]) -> list[int]:
    matches = (re.fullmatch(r"relay:.+:conn-(\d+)", name) for name in task_names)
    return [int(match.group(1)) for match in matches if match]


async def yield_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(TEST_WAIT_SECONDS):
        while not condition():
            await asyncio.sleep(0)


async def close_bounded(relay: Relay) -> None:
    await within_the_bound(relay.close())


_HeldCallback = tuple[Callable[..., object], tuple[object, ...], contextvars.Context | None]


class HoldingSteppedLoop(SteppedLoop):
    """A stepped loop that can hold the acceptance callback of one listening port.

    It can also hold every step and wakeup of named tasks, so a test decides which task
    resumes first after an event.
    """

    def __init__(self) -> None:
        super().__init__()
        self._held_port: int | None = None
        self._held: list[_HeldCallback] = []
        self._held_task_names: frozenset[str] = frozenset()
        self._held_wakeup_callbacks: list[_HeldCallback] = []

    @property
    def held_acceptances(self) -> int:
        return len(self._held)

    @property
    def held_wakeups(self) -> int:
        return len(self._held_wakeup_callbacks)

    def hold_wakeups(self, *task_names: str) -> None:
        self._held_task_names = frozenset(task_names)

    def release_wakeups(self) -> None:
        self._held_task_names = frozenset()
        held, self._held_wakeup_callbacks = self._held_wakeup_callbacks, []
        for callback, args, context in held:
            super().call_soon(callback, *args, context=context)

    def hold_acceptance(self, port: int) -> None:
        self._held_port = port

    def release_acceptance(self) -> None:
        self._held_port = None
        held, self._held = self._held, []
        for callback, args, context in held:
            super().call_soon(callback, *args, context=context)

    def call_soon(
        self,
        callback: Callable[..., object],
        *args: object,
        context: contextvars.Context | None = None,
    ) -> asyncio.Handle:
        if self._accepts_on_held_port(callback, args):
            self._held.append((callback, args, context))
            return asyncio.Handle(lambda: None, (), self)
        if self._wakes_held_task(callback):
            self._held_wakeup_callbacks.append((callback, args, context))
            return asyncio.Handle(lambda: None, (), self)
        return super().call_soon(callback, *args, context=context)

    def _wakes_held_task(self, callback: Callable[..., object]) -> bool:
        owner = getattr(callback, "__self__", None)
        return isinstance(owner, asyncio.Task) and owner.get_name() in self._held_task_names

    def _accepts_on_held_port(
        self, callback: Callable[..., object], args: tuple[object, ...]
    ) -> bool:
        if self._held_port is None or getattr(callback, "__name__", "") != "connection_made":
            return False
        transport = args[0]
        return (
            isinstance(transport, asyncio.Transport)
            and transport.get_extra_info("sockname")[1] == self._held_port
        )


def holding_loop() -> HoldingSteppedLoop:
    loop = asyncio.get_running_loop()
    assert isinstance(loop, HoldingSteppedLoop), "run the scenario in a HoldingSteppedLoop"
    return loop


def release_held_wakeups() -> None:
    loop = asyncio.get_running_loop()
    if isinstance(loop, HoldingSteppedLoop):
        loop.release_wakeups()


def drive(
    scenario: Callable[[Relay], Awaitable[None]],
    run_id: str = RUN_ID,
    owner: RelayOwner | None = None,
    clock: RunClock | None = None,
    loop_factory: Callable[[], asyncio.AbstractEventLoop] | None = None,
    calls: ScriptedCalls | None = None,
) -> None:
    async def main() -> None:
        scripted = owner or ScriptedOwner()
        relay = await Relay.start(run_id, scripted, clock or LoopClock())
        try:
            async with asyncio.timeout(TEST_WAIT_SECONDS * 3):
                await scenario(relay)
        finally:
            for gate in (scripted, calls):
                if isinstance(gate, ScriptedOwner | ScriptedCalls):
                    gate.release()
            loop = asyncio.get_running_loop()
            release_held_wakeups()
            if isinstance(loop, HoldingSteppedLoop):
                loop.release_acceptance()
            await close_bounded(relay)

    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(main())


def drive_stepped(
    scenario: Callable[[Relay, StepClock], Awaitable[None]],
    owner: ScriptedOwner | None = None,
    loop_factory: Callable[[], asyncio.AbstractEventLoop] = SteppedLoop,
) -> None:
    clock = StepClock()

    async def with_clock(relay: Relay) -> None:
        await scenario(relay, clock)

    drive(with_clock, owner=owner, clock=clock, loop_factory=loop_factory)


async def parked_at(clock: StepClock, instant: float | None) -> None:
    """Wait until the relay's next clock wait is ``instant``, letting real socket I/O land."""
    async with asyncio.timeout(TEST_WAIT_SECONDS):
        while await clock.parked() != instant:
            await asyncio.sleep(0)
