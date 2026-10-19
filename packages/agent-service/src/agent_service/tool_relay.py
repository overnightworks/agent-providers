"""The per-run loopback relay: a listener that judges each request and hands the admitted on."""

import asyncio
import copy
import hmac
import logging
import secrets
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from http import HTTPStatus
from itertools import count
from typing import Any, Final, Protocol

import h11

from agent_client.capability import MCP_ENDPOINT_PATH
from agent_client.wire import MAX_EVENT_BYTES
from agent_providers.cli_run import RelayAccess as RelayAccess
from agent_providers.cli_run import relay_authorization
from agent_providers.errors import raise_without_chain
from agent_providers.events import ToolCallEvent, ToolResultEvent
from agent_service.capability import Capability
from agent_service.mcp_wire import (
    ACKNOWLEDGEMENT,
    CallToolParams,
    CancelledParams,
    HttpAnswer,
    InitializeParams,
    JsonRpcError,
    JsonRpcErrorCode,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    McpMethod,
    McpWireError,
    Reply,
    RequestId,
    ToolDefinition,
    ToolOutcome,
    answer_for,
    error_response,
    initialize_response,
    parse_message,
    protocol_header_status,
    read_payload,
    tool_result_response,
    tools_list_response,
)
from agent_service.run_liveness import RunClock, log_failure, unfinished_by

log = logging.getLogger(__name__)

LOOPBACK_HOST: Final = "127.0.0.1"
MAX_HEAD_BYTES: Final = 16_384
RECEIVE_CHUNK_BYTES: Final = 8_192
MAX_PIPELINED_BYTES: Final = MAX_HEAD_BYTES + MAX_EVENT_BYTES
MAX_RELAY_CONNECTIONS: Final = 4
HEAD_RECEIVE_SECONDS: Final = 5.0
BODY_RECEIVE_SECONDS: Final = 10.0
RELAY_CLOSE_SECONDS: Final = 1.0
MAX_PENDING_TOOL_CALLS: Final = MAX_RELAY_CONNECTIONS - 1
_SECRET_BYTES: Final = 32
_ENDPOINT_TARGET: Final = MCP_ENDPOINT_PATH.encode()
_VERSION_FIELD: Final = b"mcp-protocol-version"
_VERSION_SEPARATOR: Final = b", "
_BODYLESS_REFUSALS: Final = frozenset({HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED})
CALL_GIVEN_UP_TEXT: Final = "The tool call ended before it ran."
CALL_FAILED_TEXT: Final = "The tool call failed; it may have run."


@dataclass(frozen=True)
class RelayRequest:
    body: bytes = field(repr=False)
    protocol_version: str | None


class Unanswered(Enum):
    """An owner's outcome for a request the CLI no longer waits for: close it without a byte."""

    UNANSWERED = "unanswered"


UNANSWERED: Final = Unanswered.UNANSWERED


class RelayOwner(Protocol):
    async def answer(self, request: RelayRequest) -> HttpAnswer | Unanswered:
        """Answer one admitted request; the body is untrusted input for ``parse_message``.

        ``Unanswered`` closes the request's connection without writing a byte. An owner must
        let ``asyncio.CancelledError`` propagate.
        """
        ...


class RelayCleanupIncomplete(Exception):
    """The close ended with relay tasks still running; they stay tracked for the next attempt."""

    def __init__(self, outstanding: int) -> None:
        super().__init__(f"{outstanding} relay tasks outlived the close")


class _Refusal(Exception):
    def __init__(self, status: HTTPStatus) -> None:
        super().__init__(status)
        self.status = status


def _field_values(request: h11.Request, name: bytes) -> list[bytes]:
    return [value for field_name, value in request.headers if field_name == name]


def _declares_no_body(request: h11.Request) -> bool:
    return not _field_values(request, b"transfer-encoding") and _declared_length(request) == 0


def _declares_both_framings(request: h11.Request) -> bool:
    return bool(_field_values(request, b"transfer-encoding")) and bool(
        _field_values(request, b"content-length")
    )


def _declared_length(request: h11.Request) -> int:
    return max((int(length) for length in _field_values(request, b"content-length")), default=0)


def _protocol_version(request: h11.Request) -> str | None:
    values = _field_values(request, _VERSION_FIELD)
    return _VERSION_SEPARATOR.join(values).decode("iso-8859-1") if values else None


def _empty(status: HTTPStatus) -> HttpAnswer:
    return HttpAnswer(status, None, b"")


class _ClientEnd(Enum):
    WHILE_PENDING = "while the answer was pending"
    AFTER_ANSWER = "after the answer completed"


@dataclass(frozen=True)
class _Answered:
    answer: HttpAnswer | Unanswered
    client_ended: bool


class _ReceiveDeadline:
    """Aborts the connection when a receive phase outlasts its bound on the run's clock."""

    def __init__(
        self, clock: RunClock, seconds: float, transport: asyncio.Transport, task_name: str
    ) -> None:
        self._clock = clock
        self._seconds = seconds
        self._transport = transport
        self._task_name = task_name
        self._instant: float | None = None
        self._timer: asyncio.Task[None] | None = None

    @property
    def expired(self) -> bool:
        return self._instant is not None and self._clock() >= self._instant

    def arm(self) -> float:
        if self._instant is None:
            self._instant = self._clock() + self._seconds
        return self._instant

    def start(self) -> None:
        if self._timer is None:
            self._timer = asyncio.get_running_loop().create_task(
                self._abort_at(self.arm()), name=self._task_name
            )

    async def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            await asyncio.gather(self._timer, return_exceptions=True)

    async def _abort_at(self, instant: float) -> None:
        await self._clock.wait_until(instant)
        self._transport.abort()


class Relay:
    def __init__(self, run_id: str, secret: str, owner: RelayOwner, clock: RunClock) -> None:
        self._run_id = run_id
        self._owner = owner
        self._clock = clock
        self._bearer = relay_authorization(secret).encode()
        self._secret = secret
        self._expected_host = b""
        self._connection_numbers = count(1)
        self._connections: dict[asyncio.Task[None], asyncio.Transport] = {}
        self._closing = False
        self._closed = False
        self._close_attempt: asyncio.Task[None] | None = None
        self._server: asyncio.Server
        self.access: RelayAccess

    @classmethod
    async def start(cls, run_id: str, owner: RelayOwner, clock: RunClock) -> "Relay":
        relay = cls(run_id, secrets.token_urlsafe(_SECRET_BYTES), owner, clock)
        relay._server = await asyncio.start_server(relay._accept, LOOPBACK_HOST, 0)
        port = relay._server.sockets[0].getsockname()[1]
        relay._expected_host = f"{LOOPBACK_HOST}:{port}".encode()
        url = f"http://{LOOPBACK_HOST}:{port}{MCP_ENDPOINT_PATH}"  # NOSONAR(S5332) loopback only
        relay.access = RelayAccess(url=url, secret=relay._secret)
        return relay

    async def close(self) -> None:
        """End the relay within ``RELAY_CLOSE_SECONDS`` or raise ``RelayCleanupIncomplete``.

        A raise keeps the outstanding tasks tracked; calling again retries with a new budget.
        """
        if self._closed:
            return
        if self._close_attempt is None or self._close_attempt.done():
            self._close_attempt = asyncio.get_running_loop().create_task(
                self._close_once(), name=f"relay:{self._run_id}:close"
            )
            self._close_attempt.add_done_callback(self._retrieve_close_outcome)
        await asyncio.shield(self._close_attempt)

    @staticmethod
    def _retrieve_close_outcome(attempt: asyncio.Task[None]) -> None:
        if not attempt.cancelled():
            attempt.exception()

    def stop_listening(self) -> None:
        """Close the listening socket now; the connections stay served until ``close()``.

        The old port refuses connections and one accepted from here on is aborted. This is
        the first step of ``RunTools.close``; ``close()`` calls it again harmlessly.
        """
        self._closing = True
        self._server.close()

    async def _close_once(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = self._clock() + RELAY_CLOSE_SECONDS
        self.stop_listening()
        connections = dict(self._connections)
        for transport in connections.values():
            transport.abort()
        for task in connections:
            task.cancel()
        wait_closed = loop.create_task(
            self._server.wait_closed(), name=f"relay:{self._run_id}:wait-closed"
        )
        try:
            outstanding = await unfinished_by(
                self._clock,
                deadline,
                {*connections, wait_closed},
                f"relay:{self._run_id}:close-deadline",
            )
        finally:
            wait_closed.cancel()
            await asyncio.gather(wait_closed, return_exceptions=True)
        if outstanding:
            raise_without_chain(RelayCleanupIncomplete(len(outstanding)))
        self._closed = True

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing or len(self._connections) >= MAX_RELAY_CONNECTIONS:
            writer.transport.abort()
            return
        name = f"relay:{self._run_id}:conn-{next(self._connection_numbers)}"
        task = asyncio.get_running_loop().create_task(self._serve(reader, writer), name=name)
        self._connections[task] = writer.transport
        task.add_done_callback(self._connection_ended)

    def _connection_ended(self, task: asyncio.Task[None]) -> None:
        del self._connections[task]
        if not task.cancelled() and (failure := task.exception()) is not None:
            log_failure(log, f"A connection of the relay of run {self._run_id} failed", failure)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_HEAD_BYTES)
        try:
            while await self._serve_one(connection, reader, writer):
                connection.start_next_cycle()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def _serve_one(
        self, connection: h11.Connection, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        try:
            body_deadline = self._receive_deadline(BODY_RECEIVE_SECONDS, writer, "body")
            request = await self._read_head(
                connection,
                reader,
                self._receive_deadline(HEAD_RECEIVE_SECONDS, writer, "head"),
                body_deadline,
            )
            if request is None:
                return False
            if (status := self._head_status(request)) is not None:
                return await self._refuse(connection, writer, request, status)
            if _declared_length(request) > MAX_EVENT_BYTES:
                raise _Refusal(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            body = await self._read_body(connection, reader, body_deadline)
        except _Refusal as refusal:
            await self._answer(connection, writer, _empty(refusal.status), closing=True)
            return False
        if body is None:
            return False
        answered = await self._answered_while_watching(
            connection, reader, RelayRequest(body, _protocol_version(request))
        )
        if answered.answer is UNANSWERED:
            return False
        mixed_framing = _declares_both_framings(request)
        await self._answer(connection, writer, answered.answer, closing=mixed_framing)
        return not (mixed_framing or answered.client_ended) and _ready_for_next_cycle(connection)

    async def _refuse(
        self,
        connection: h11.Connection,
        writer: asyncio.StreamWriter,
        request: h11.Request,
        status: HTTPStatus,
    ) -> bool:
        can_stay = (
            status in _BODYLESS_REFUSALS
            and _declares_no_body(request)
            and isinstance(connection.next_event(), h11.EndOfMessage)
        )
        allowed = [(b"allow", b"POST")] if status is HTTPStatus.METHOD_NOT_ALLOWED else []
        await self._answer(connection, writer, _empty(status), allowed, closing=not can_stay)
        return can_stay and _ready_for_next_cycle(connection)

    async def _answered_while_watching(
        self, connection: h11.Connection, reader: asyncio.StreamReader, request: RelayRequest
    ) -> _Answered:
        """Run the owner as a child awaited through a forwarding gather, with a close-watch.

        The forwarding await lets the relay's close cancel the owner within its own step, and the
        watch cancels in the step it sees the end, so the barrier's give-up is recorded before
        the connection task resumes and a call the CLI closed is not forwarded in between.
        """
        loop = asyncio.get_running_loop()
        name = _connection_task().get_name()
        answer = loop.create_task(self._owner_answer(request), name=f"{name}:answer")
        watch = loop.create_task(_watch(connection, reader, answer), name=f"{name}:watch")
        try:
            outcome = await _outcome_of(answer)
        finally:
            end = await _stopped(watch)
        if end is _ClientEnd.WHILE_PENDING:
            return _Answered(UNANSWERED, client_ended=True)
        return _Answered(outcome, client_ended=end is not None)

    async def _owner_answer(self, request: RelayRequest) -> HttpAnswer | Unanswered:
        try:
            return await self._owner.answer(request)
        except Exception as failure:
            log_failure(log, f"The owner of the relay of run {self._run_id} failed", failure)
            return _empty(HTTPStatus.INTERNAL_SERVER_ERROR)

    def _receive_deadline(
        self, seconds: float, writer: asyncio.StreamWriter, phase: str
    ) -> _ReceiveDeadline:
        return _ReceiveDeadline(
            self._clock, seconds, writer.transport, f"relay:{self._run_id}:{phase}-deadline"
        )

    async def _next_event(
        self, connection: h11.Connection, reader: asyncio.StreamReader, deadline: _ReceiveDeadline
    ) -> h11.Event | type[h11.NEED_DATA] | None:
        while not deadline.expired:
            try:
                event = connection.next_event()
            except h11.RemoteProtocolError as refusal:
                raise _Refusal(HTTPStatus(refusal.error_status_hint)) from None
            if event is not h11.NEED_DATA:
                return event
            chunk = await reader.read(RECEIVE_CHUNK_BYTES)
            if not chunk:
                return None
            deadline.start()
            connection.receive_data(chunk)
        return None

    async def _read_head(
        self,
        connection: h11.Connection,
        reader: asyncio.StreamReader,
        deadline: _ReceiveDeadline,
        body_deadline: _ReceiveDeadline,
    ) -> h11.Request | None:
        if connection.trailing_data[0]:
            deadline.start()
        try:
            event = await self._next_event(connection, reader, deadline)
            if isinstance(event, h11.Request):
                body_deadline.arm()
        finally:
            await deadline.stop()
        return event if isinstance(event, h11.Request) else None

    async def _read_body(
        self, connection: h11.Connection, reader: asyncio.StreamReader, deadline: _ReceiveDeadline
    ) -> bytes | None:
        body = bytearray()
        deadline.start()
        try:
            while True:
                event = await self._next_event(connection, reader, deadline)
                if isinstance(event, h11.Data):
                    body += event.data
                    if len(body) > MAX_EVENT_BYTES:
                        raise _Refusal(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                elif isinstance(event, h11.EndOfMessage):
                    return bytes(body)
                elif event is None:
                    return None
        finally:
            await deadline.stop()

    def _head_status(self, request: h11.Request) -> HTTPStatus | None:
        if _field_values(request, b"host") != [self._expected_host]:
            return HTTPStatus.FORBIDDEN
        if _field_values(request, b"origin"):
            return HTTPStatus.FORBIDDEN
        authorizations = _field_values(request, b"authorization")
        if len(authorizations) != 1 or not hmac.compare_digest(authorizations[0], self._bearer):
            return HTTPStatus.UNAUTHORIZED
        if request.target != _ENDPOINT_TARGET:
            return HTTPStatus.NOT_FOUND
        if request.method != b"POST":
            return HTTPStatus.METHOD_NOT_ALLOWED
        return None

    async def _answer(
        self,
        connection: h11.Connection,
        writer: asyncio.StreamWriter,
        answer: HttpAnswer,
        headers: list[tuple[bytes, bytes]] | None = None,
        *,
        closing: bool,
    ) -> None:
        fields = [(b"content-length", str(len(answer.body)).encode()), *(headers or [])]
        if answer.content_type is not None:
            fields.append((b"content-type", answer.content_type.encode()))
        if closing:
            fields.append((b"connection", b"close"))
        response = h11.Response(status_code=answer.status.value, headers=fields)
        writer.write(
            connection.send(response)
            + (connection.send(h11.Data(data=answer.body)) if answer.body else b"")
            + connection.send(h11.EndOfMessage())
        )
        await writer.drain()


def _connection_task() -> asyncio.Task[Any]:
    task = asyncio.current_task()
    assert task is not None
    return task


async def _outcome_of(answer: asyncio.Task[HttpAnswer | Unanswered]) -> HttpAnswer | Unanswered:
    (outcome,) = await asyncio.gather(answer, return_exceptions=True)
    if answer.cancelled():
        return UNANSWERED
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


async def _stopped(watch: asyncio.Task[_ClientEnd]) -> _ClientEnd | None:
    watch.cancel()
    await asyncio.gather(watch, return_exceptions=True)
    return None if watch.cancelled() else watch.result()


async def _watch(
    connection: h11.Connection,
    reader: asyncio.StreamReader,
    answer: asyncio.Task[HttpAnswer | Unanswered],
) -> _ClientEnd:
    """Read until the client ends, keeping pipelined bytes in order for the next request.

    An end is cut off in the step it is seen, so the pending answer is cancelled before
    anything else runs. The count covers every unparsed byte the connection retains, so
    pipelining cannot grow the parser's buffer without bound.
    """
    retained = len(connection.trailing_data[0])
    while True:
        try:
            chunk = await reader.read(RECEIVE_CHUNK_BYTES)
        except ConnectionError:
            chunk = b""
        retained += len(chunk)
        if not chunk or retained > MAX_PIPELINED_BYTES:
            if answer.done():
                return _ClientEnd.AFTER_ANSWER
            if not answer.cancelling():
                answer.cancel()
            return _ClientEnd.WHILE_PENDING
        connection.receive_data(chunk)


def _ready_for_next_cycle(connection: h11.Connection) -> bool:
    return connection.our_state is h11.DONE and connection.their_state is h11.DONE


class ToolCalls(Protocol):
    async def call(self, request_id: RequestId, params: CallToolParams) -> Reply | Unanswered:
        """Answer one parsed ``tools/call``, or ``Unanswered`` once the CLI gave it up.

        It must let ``asyncio.CancelledError`` propagate. A caller must reach it through an await
        chain that forwards ``Task.cancel()`` at once (a direct await or ``asyncio.gather``), so
        that cancelling the caller's task records the give-up before ``Task.cancel()`` returns.
        """
        ...

    def cancel(self, request_id: RequestId) -> None:
        """Give up the pending call with this id; an id matching no pending call is ignored.

        It is synchronous: the give-up is recorded before it returns.
        """
        ...

    def unoffered(self) -> None:
        """Record that the CLI called a name it was never offered; the owner has refused it."""
        ...

    def reflected(self) -> None:
        """Record that an answer carried the capability; the owner has refused itself for good."""
        ...


class AppTools(Protocol):
    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome: ...


class UnofferedToolCalled(Exception):
    """The CLI called a name it was never offered; the message names no tool."""

    def __init__(self) -> None:
        super().__init__("the CLI called a tool it was never offered")


class ToolCallsMisused(Exception):
    """The runner broke the hand-out and accept rule, or used the barrier after its stop."""

    def __init__(self) -> None:
        super().__init__("the barrier was used against its hand-out and accept rule")


@dataclass(eq=False)
class _PendingCall:
    request_id: RequestId
    params: CallToolParams
    event: ToolCallEvent
    response: asyncio.Future[Reply | Unanswered]
    cancelled_by_cli: bool = False
    outcome: ToolOutcome | None = None

    @property
    def given_up(self) -> bool:
        return (
            self.cancelled_by_cli
            or self.response.cancelled()
            or (self.response.done() and self.response.result() is UNANSWERED)
        )


@dataclass(frozen=True)
class _ReadyEvent:
    pending: _PendingCall
    event: ToolCallEvent | ToolResultEvent


class ToolCallBarrier:
    """The ``ToolCalls`` of a run: a call runs, and its result is released, only once accepted.

    Each admitted ``tools/call`` is one pending call under the relay-minted id ``call-<n>``,
    numbered in arrival order. It is waiting, handed out, forwarded, resulted, or ended
    (released, refused, or given up). The service alone accepts: the runner takes one event at
    a time with ``next_event()``, yields it, and calls ``accept(event)`` only after the service
    resumed it past that event, that is after it checked, counted, recorded and queued it.
    ``accept`` takes exactly the handed-out event, matched by kind and id: a call's event
    forwards the call, its result's event releases the reply. Any other event, a repeated
    accept, or an accept after ``stop()`` raises ``ToolCallsMisused`` and does nothing.

    ``next_event()`` hands out, in this order, the given-up result of the last accept, then
    calls and results in the order they became ready (a call at its arrival, a result when its
    forward ended). Every event stays handed out until ``accept`` takes it, so a second
    ``next_event()`` before that raises ``ToolCallsMisused``.

    A forward runs as the task ``upstream:<run_id>:call-<n>``, which a give-up never cancels.
    One completion owner, attached to it at creation, retrieves its outcome exactly once,
    whether or not the CLI still waits, and turns it into the call's one ``ToolResultEvent``:
    an outcome as it is, an upstream failure as ``CALL_FAILED_TEXT`` with ``is_error`` after
    one log line by class. A cancelled forward yields no result. An outcome whose decoded text
    carries the run's capability yields no event and no reply: the barrier is refused for good
    and ``next_event()`` raises ``CapabilityReflected``; so does the owner's ``reflected()``
    report of a hit in any other relay answer. ``settle()`` returns once every
    forward's outcome was retrieved. ``has_running_forward()`` is true from the accept of a
    call until its outcome was retrieved. ``has_ready_event()`` is true while a queued event, a
    ready call that was not given up, an unoffered call or a reflected outcome waits; it makes no
    promise after ``stop()`` or while an event is handed out (where ``next_event()`` raises
    ``ToolCallsMisused`` at once). It assumes the merger accepts the handed-out event before it
    asks for the next, and that production stops the barrier only at the close.

    ``call()`` awaits one response future from admission to its answer. It is resolved by the
    release of the reply, by the refusal ``-32602`` at ``stop()``, or by ``Unanswered``
    (``cancel()``, a cancelled forward, a capability hit), or cancelled by the cancellation of
    the task running ``call()``. A duplicate pending id, a call beyond
    ``MAX_PENDING_TOOL_CALLS`` and every call after ``stop()`` are refused ``-32602`` before
    admission, so no future exists for them. That task must reach ``call()`` through an
    await chain that forwards ``Task.cancel()`` at once (a direct await or ``asyncio.gather``; no
    ``asyncio.wait``, shield or ``wait_for``), so the give-up is recorded before
    ``Task.cancel()`` returns, in every phase. An explicit ``cancel()`` is recorded beside the
    future and wins over an earlier resolution: ``call()`` then returns ``Unanswered``.

    A give-up withholds only the reply: the call ran, so its result is still handed out and
    its accept releases nothing. ``stop()`` refuses with ``-32602`` every admitted call whose
    reply was not released, a running forward, a queued result and a handed-out result
    included; a running forward keeps running upstream. A call holds its pending slot until its
    ``call()`` returns: released (once ``accept`` of its result resolved the reply), refused, or
    given up. ``cancel()`` frees it at once; a forward's completion alone never does.
    The bound reserves no connection, because idle keep-alive connections count toward the
    listener's cap too.

    The listener cancels the task running ``call()`` when the CLI closes the connection of a
    pending call, in the step it sees the close.
    """

    def __init__(self, run_id: str, app: AppTools, capability: Capability) -> None:
        self._run_id = run_id
        self._app = app
        self._capability = capability
        self._numbers = count(1)
        self._pending: list[_PendingCall] = []
        self._ready: deque[_ReadyEvent] = deque()
        self._handed_out: _ReadyEvent | None = None
        self._forwards: set[asyncio.Task[ToolOutcome]] = set()
        self._changed = asyncio.Event()
        self._forward_released = asyncio.Event()
        self._stopped = False
        self._unoffered = False
        self._reflected = False

    async def call(self, request_id: RequestId, params: CallToolParams) -> Reply | Unanswered:
        if not self._admits(request_id):
            return error_response(request_id, JsonRpcErrorCode.INVALID_PARAMS)
        pending = self._admit(request_id, params)
        try:
            reply = await pending.response
        finally:
            self._end(pending)
        return UNANSWERED if pending.cancelled_by_cli else reply

    def cancel(self, request_id: RequestId) -> None:
        pending = self._pending_with(request_id)
        if pending is None:
            return
        self._pending.remove(pending)
        self._drop_queued_call(pending)
        pending.cancelled_by_cli = True
        self._resolve(pending, UNANSWERED)

    def unoffered(self) -> None:
        self._unoffered = True
        self._changed.set()

    def reflected(self) -> None:
        self._reflected = True
        self._changed.set()

    def has_running_forward(self) -> bool:
        """Whether a forward still runs upstream, whether or not the CLI still waits for it."""
        return bool(self._forwards)

    async def next_event(self) -> ToolCallEvent | ToolResultEvent:
        while True:
            if self._handed_out is not None:
                raise_without_chain(ToolCallsMisused())
            self._require_usable()
            if (ready := self._next_ready()) is not None:
                self._handed_out = ready
                return ready.event
            self._changed.clear()
            await self._changed.wait()

    def has_ready_event(self) -> bool:
        """Whether an event waits that ``next_event()`` hands out or raises (see the class)."""
        return (
            self._unoffered
            or self._reflected
            or any(
                isinstance(ready.event, ToolResultEvent) or not ready.pending.given_up
                for ready in self._ready
            )
        )

    def accept(self, event: ToolCallEvent | ToolResultEvent) -> None:
        handed_out = self._handed_out_for(event)
        self._handed_out = None
        pending = handed_out.pending
        if isinstance(event, ToolResultEvent):
            self._release(pending)
        elif pending.given_up:
            self._ready.appendleft(
                _ReadyEvent(
                    pending,
                    ToolResultEvent(
                        tool_use_id=event.tool_use_id, content=CALL_GIVEN_UP_TEXT, is_error=True
                    ),
                )
            )
        else:
            self._forward(pending, event)

    def stop(self) -> None:
        self._stopped = True
        for pending in self._pending:
            self._resolve(
                pending, error_response(pending.request_id, JsonRpcErrorCode.INVALID_PARAMS)
            )
        self._ready.clear()
        self._handed_out = None
        self._changed.set()

    def abort_forwards(self) -> None:
        """Cancel every running forward once; ``RunTools.close`` calls it, ``stop()`` never does."""
        for forward in self._forwards:
            forward.cancel()

    async def settle(self) -> None:
        while self._forwards:
            self._forward_released.clear()
            await self._forward_released.wait()

    def _forward(self, pending: _PendingCall, event: ToolCallEvent) -> None:
        forward = asyncio.create_task(
            self._app.call_tool(pending.params.name, pending.params.arguments),
            name=f"upstream:{self._run_id}:{event.tool_use_id}",
        )
        self._forwards.add(forward)
        forward.add_done_callback(partial(self._completed, pending))

    def _completed(self, pending: _PendingCall, forward: asyncio.Task[ToolOutcome]) -> None:
        try:
            if forward.cancelled():
                self._resolve(pending, UNANSWERED)
            else:
                self._queue_result(pending, self._outcome_of(forward))
        finally:
            self._forwards.discard(forward)
            self._forward_released.set()

    def _outcome_of(self, forward: asyncio.Task[ToolOutcome]) -> ToolOutcome:
        try:
            return forward.result()
        except Exception as failure:
            log_failure(log, f"A forwarded call of run {self._run_id} failed", failure)
            return ToolOutcome(CALL_FAILED_TEXT, is_error=True)

    def _queue_result(self, pending: _PendingCall, outcome: ToolOutcome) -> None:
        if self._capability.appears_in(outcome.content.encode("utf-8")):
            self.reflected()
            self._resolve(pending, UNANSWERED)
        else:
            pending.outcome = outcome
            result = ToolResultEvent(
                tool_use_id=pending.event.tool_use_id,
                content=outcome.content,
                is_error=outcome.is_error,
            )
            self._ready.append(_ReadyEvent(pending, result))
        self._changed.set()

    def _release(self, pending: _PendingCall) -> None:
        if pending.outcome is not None:
            self._resolve(pending, tool_result_response(pending.request_id, pending.outcome))

    @staticmethod
    def _resolve(pending: _PendingCall, answer: Reply | Unanswered) -> None:
        if not pending.response.done():
            pending.response.set_result(answer)

    def _handed_out_for(self, event: ToolCallEvent | ToolResultEvent) -> _ReadyEvent:
        handed_out = self._handed_out
        if (
            handed_out is not None
            and type(event) is type(handed_out.event)
            and event.tool_use_id == handed_out.event.tool_use_id
        ):
            return handed_out
        raise_without_chain(ToolCallsMisused())

    def _next_ready(self) -> _ReadyEvent | None:
        while self._ready:
            ready = self._ready.popleft()
            if isinstance(ready.event, ToolResultEvent) or not ready.pending.given_up:
                return ready
        return None

    def _admits(self, request_id: RequestId) -> bool:
        return (
            not self._stopped
            and len(self._pending) < MAX_PENDING_TOOL_CALLS
            and self._pending_with(request_id) is None
        )

    def _pending_with(self, request_id: RequestId) -> _PendingCall | None:
        return next((call for call in self._pending if call.request_id == request_id), None)

    def _admit(self, request_id: RequestId, params: CallToolParams) -> _PendingCall:
        event = ToolCallEvent(
            tool_use_id=f"call-{next(self._numbers)}",
            name=params.name,
            input=copy.deepcopy(params.arguments),
        )
        pending = _PendingCall(
            request_id, params, event, asyncio.get_running_loop().create_future()
        )
        self._pending.append(pending)
        self._ready.append(_ReadyEvent(pending, event))
        self._changed.set()
        return pending

    def _end(self, pending: _PendingCall) -> None:
        if pending in self._pending:
            self._pending.remove(pending)
        self._drop_queued_call(pending)

    def _drop_queued_call(self, pending: _PendingCall) -> None:
        self._ready = deque(
            ready
            for ready in self._ready
            if ready.pending is not pending or isinstance(ready.event, ToolResultEvent)
        )

    def _require_usable(self) -> None:
        if self._unoffered:
            raise_without_chain(UnofferedToolCalled())
        if self._reflected:
            raise_without_chain(CapabilityReflected())
        if self._stopped:
            raise_without_chain(ToolCallsMisused())


class CapabilityReflected(Exception):
    """A body bound for the CLI carried the run's capability; the message names no content."""

    def __init__(self) -> None:
        super().__init__("a body to the CLI carried the run's capability")


class RepeatedToolOffer(Exception):
    """The app offered one allowed tool more than once; the message names no tool."""

    def __init__(self) -> None:
        super().__init__("an allowed tool was offered more than once")


def _narrowed_to(
    allowed: Sequence[str], offered: Sequence[ToolDefinition]
) -> tuple[ToolDefinition, ...]:
    permitted = frozenset(allowed)
    kept = [tool for tool in offered if tool.name in permitted]
    if len({tool.name for tool in kept}) != len(kept):
        raise_without_chain(RepeatedToolOffer())
    return tuple(kept)


class McpRelayOwner:
    """Answers the CLI's MCP messages and lets no body carrying the capability out.

    Every answer leaves through ``_cleared_for_cli``. A hit refuses the owner for good and is
    reported once to ``ToolCalls.reflected()``, which ends the run like a reflected result. A
    ``notifications/cancelled`` with a well-formed ``requestId`` is routed to
    ``ToolCalls.cancel``; every notification is still acknowledged. An ``Unanswered`` call
    passes through untouched: it carries no body, and the CLI no longer waits for it.
    """

    def __init__(
        self, capability: Capability, offered: Sequence[ToolDefinition], calls: ToolCalls
    ) -> None:
        self._capability = capability
        self._narrowed = _narrowed_to(capability.claims.tools, offered)
        self._narrowed_names = frozenset(tool.name for tool in self._narrowed)
        self._calls = calls
        self._refused = False

    async def answer(self, request: RelayRequest) -> HttpAnswer | Unanswered:
        self._require_not_refused()
        answer = await self._respond(request)
        return answer if answer is UNANSWERED else self._cleared_for_cli(answer)

    def _require_not_refused(self) -> None:
        if self._refused:
            raise_without_chain(CapabilityReflected())

    def _cleared_for_cli(self, answer: HttpAnswer) -> HttpAnswer:
        self._require_not_refused()
        if self._capability.appears_in(answer.body):
            self._refused = True
            self._calls.reflected()
            raise_without_chain(CapabilityReflected())
        return answer

    async def _respond(self, request: RelayRequest) -> HttpAnswer | Unanswered:
        try:
            message = parse_message(request.body)
        except McpWireError:
            return _empty(HTTPStatus.BAD_REQUEST)
        if isinstance(message, JsonRpcResponse | JsonRpcError):
            return _empty(HTTPStatus.BAD_REQUEST)
        if message.method != McpMethod.INITIALIZE and (
            protocol_header_status(request.protocol_version) is not HTTPStatus.OK
        ):
            return _empty(HTTPStatus.BAD_REQUEST)
        if isinstance(message, JsonRpcNotification):
            self._route(message)
            return ACKNOWLEDGEMENT
        reply = await self._reply_to(message)
        return reply if reply is UNANSWERED else answer_for(reply)

    def _route(self, notification: JsonRpcNotification) -> None:
        if notification.method != McpMethod.CANCELLED:
            return
        try:
            params = read_payload(CancelledParams, notification.params)
        except McpWireError:
            return
        self._calls.cancel(params.request_id)

    async def _reply_to(self, request: JsonRpcRequest) -> Reply | Unanswered:
        match request.method:
            case McpMethod.INITIALIZE:
                return self._initialized(request)
            case McpMethod.PING:
                return JsonRpcResponse(id=request.id, result={})
            case McpMethod.TOOLS_LIST:
                return tools_list_response(request.id, self._narrowed)
            case McpMethod.TOOLS_CALL:
                return await self._called(request)
            case _:
                return error_response(request.id, JsonRpcErrorCode.METHOD_NOT_FOUND)

    @staticmethod
    def _initialized(request: JsonRpcRequest) -> Reply:
        try:
            params = read_payload(InitializeParams, request.params)
        except McpWireError:
            return error_response(request.id, JsonRpcErrorCode.INVALID_PARAMS)
        return initialize_response(request.id, params.protocol_version)

    async def _called(self, request: JsonRpcRequest) -> Reply | Unanswered:
        try:
            params = read_payload(CallToolParams, request.params)
        except McpWireError:
            return error_response(request.id, JsonRpcErrorCode.INVALID_PARAMS)
        if params.name not in self._narrowed_names:
            self._calls.unoffered()
            return error_response(request.id, JsonRpcErrorCode.INVALID_PARAMS)
        return await self._calls.call(request.id, params)
