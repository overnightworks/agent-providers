"""The per-run loopback relay: a listener that judges each request and hands the admitted on."""

import asyncio
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from http import HTTPStatus
from itertools import count
from typing import Final, Protocol

import h11

from agent_client.capability import MCP_ENDPOINT_PATH
from agent_client.wire import MAX_EVENT_BYTES
from agent_providers.errors import raise_without_chain
from agent_service.mcp_wire import HttpAnswer
from agent_service.run_liveness import RunClock, log_failure

log = logging.getLogger(__name__)

LOOPBACK_HOST: Final = "127.0.0.1"
MAX_HEAD_BYTES: Final = 16_384
RECEIVE_CHUNK_BYTES: Final = 8_192
MAX_RELAY_CONNECTIONS: Final = 4
HEAD_RECEIVE_SECONDS: Final = 5.0
BODY_RECEIVE_SECONDS: Final = 10.0
RELAY_CLOSE_SECONDS: Final = 1.0
_SECRET_BYTES: Final = 32
_BEARER_SCHEME: Final = b"Bearer "
_ENDPOINT_TARGET: Final = MCP_ENDPOINT_PATH.encode()
_VERSION_FIELD: Final = b"mcp-protocol-version"
_VERSION_SEPARATOR: Final = b", "
_BODYLESS_REFUSALS: Final = frozenset({HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED})


@dataclass(frozen=True)
class RelayRequest:
    body: bytes = field(repr=False)
    protocol_version: str | None


class RelayOwner(Protocol):
    async def answer(self, request: RelayRequest) -> HttpAnswer:
        """Answer one admitted request; the body is untrusted input for ``parse_message``.

        An owner must let ``asyncio.CancelledError`` propagate.
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


@dataclass(frozen=True)
class RelayAccess:
    url: str
    secret: str = field(repr=False)


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

    def start(self) -> None:
        if self._timer is None:
            self._instant = self._clock() + self._seconds
            self._timer = asyncio.get_running_loop().create_task(
                self._abort_at(self._instant), name=self._task_name
            )

    async def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            await asyncio.gather(self._timer, return_exceptions=True)

    async def _abort_at(self, instant: float) -> None:
        await self._clock.wait_until(instant)
        self._transport.abort()


def _unfinished(tasks: set[asyncio.Task[None]]) -> set[asyncio.Task[None]]:
    return {task for task in tasks if not task.done()}


class Relay:
    def __init__(self, run_id: str, secret: str, owner: RelayOwner, clock: RunClock) -> None:
        self._run_id = run_id
        self._owner = owner
        self._clock = clock
        self._bearer = _BEARER_SCHEME + secret.encode()
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

    async def _close_once(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = self._clock() + RELAY_CLOSE_SECONDS
        self._closing = True
        self._server.close()
        connections = dict(self._connections)
        for transport in connections.values():
            transport.abort()
        for task in connections:
            task.cancel()
        wait_closed = loop.create_task(
            self._server.wait_closed(), name=f"relay:{self._run_id}:wait-closed"
        )
        expiry = loop.create_task(
            self._clock.wait_until(deadline), name=f"relay:{self._run_id}:close-deadline"
        )
        observed = {*connections, wait_closed}
        try:
            while not expiry.done() and (outstanding := _unfinished(observed)):
                await asyncio.wait({expiry, *outstanding}, return_when=asyncio.FIRST_COMPLETED)
            outstanding = _unfinished(observed)
        finally:
            for own in (expiry, wait_closed):
                own.cancel()
            await asyncio.gather(expiry, wait_closed, return_exceptions=True)
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
            request = await self._read_head(
                connection, reader, self._receive_deadline(HEAD_RECEIVE_SECONDS, writer, "head")
            )
            if request is None:
                return False
            if (status := self._head_status(request)) is not None:
                return await self._refuse(connection, writer, request, status)
            if _declared_length(request) > MAX_EVENT_BYTES:
                raise _Refusal(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            body = await self._read_body(
                connection, reader, self._receive_deadline(BODY_RECEIVE_SECONDS, writer, "body")
            )
        except _Refusal as refusal:
            await self._answer(connection, writer, _empty(refusal.status), closing=True)
            return False
        if body is None:
            return False
        answer = await self._owner_answer(RelayRequest(body, _protocol_version(request)))
        mixed_framing = _declares_both_framings(request)
        await self._answer(connection, writer, answer, closing=mixed_framing)
        return not mixed_framing and _ready_for_next_cycle(connection)

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

    async def _owner_answer(self, request: RelayRequest) -> HttpAnswer:
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
        while True:
            try:
                event = connection.next_event()
            except h11.RemoteProtocolError as refusal:
                raise _Refusal(HTTPStatus(refusal.error_status_hint)) from None
            if event is not h11.NEED_DATA:
                return None if deadline.expired else event
            chunk = await reader.read(RECEIVE_CHUNK_BYTES)
            if not chunk:
                return None
            deadline.start()
            connection.receive_data(chunk)

    async def _read_head(
        self, connection: h11.Connection, reader: asyncio.StreamReader, deadline: _ReceiveDeadline
    ) -> h11.Request | None:
        if connection.trailing_data[0]:
            deadline.start()
        try:
            event = await self._next_event(connection, reader, deadline)
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


def _ready_for_next_cycle(connection: h11.Connection) -> bool:
    return connection.our_state is h11.DONE and connection.their_state is h11.DONE
