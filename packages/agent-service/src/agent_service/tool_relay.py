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
from agent_service.mcp_wire import HttpAnswer
from agent_service.run_liveness import log_failure

log = logging.getLogger(__name__)

LOOPBACK_HOST: Final = "127.0.0.1"
MAX_HEAD_BYTES: Final = 16_384
RECEIVE_CHUNK_BYTES: Final = 8_192
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
    if _field_values(request, b"transfer-encoding"):
        return False
    return all(int(length) == 0 for length in _field_values(request, b"content-length"))


def _declared_length(request: h11.Request) -> int:
    return max((int(length) for length in _field_values(request, b"content-length")), default=0)


def _protocol_version(request: h11.Request) -> str | None:
    values = _field_values(request, _VERSION_FIELD)
    return _VERSION_SEPARATOR.join(values).decode("iso-8859-1") if values else None


def _empty(status: HTTPStatus) -> HttpAnswer:
    return HttpAnswer(status, None, b"")


class Relay:
    def __init__(self, run_id: str, secret: str, owner: RelayOwner) -> None:
        self._run_id = run_id
        self._owner = owner
        self._bearer = _BEARER_SCHEME + secret.encode()
        self._secret = secret
        self._expected_host = b""
        self._connection_numbers = count(1)
        self._connections: dict[asyncio.Task[None], asyncio.Transport] = {}
        self._closing = False
        self._server: asyncio.Server
        self.access: RelayAccess

    @classmethod
    async def start(cls, run_id: str, owner: RelayOwner) -> "Relay":
        relay = cls(run_id, secrets.token_urlsafe(_SECRET_BYTES), owner)
        relay._server = await asyncio.start_server(relay._accept, LOOPBACK_HOST, 0)
        port = relay._server.sockets[0].getsockname()[1]
        relay._expected_host = f"{LOOPBACK_HOST}:{port}".encode()
        url = f"http://{LOOPBACK_HOST}:{port}{MCP_ENDPOINT_PATH}"  # NOSONAR(S5332) loopback only
        relay.access = RelayAccess(url=url, secret=relay._secret)
        return relay

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._server.close()
        connections = dict(self._connections)
        for transport in connections.values():
            transport.abort()
        for task in connections:
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        await self._server.wait_closed()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
            request = await self._read_head(connection, reader)
            if request is None:
                return False
            if (status := self._head_status(request)) is not None:
                return await self._refuse(connection, writer, request, status)
            if _declared_length(request) > MAX_EVENT_BYTES:
                raise _Refusal(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            body = await self._read_body(connection, reader)
        except _Refusal as refusal:
            await self._answer(connection, writer, _empty(refusal.status), closing=True)
            return False
        if body is None:
            return False
        answer = await self._owner_answer(RelayRequest(body, _protocol_version(request)))
        await self._answer(connection, writer, answer, closing=False)
        return _ready_for_next_cycle(connection)

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

    async def _next_event(
        self, connection: h11.Connection, reader: asyncio.StreamReader
    ) -> h11.Event | type[h11.NEED_DATA] | None:
        while True:
            try:
                event = connection.next_event()
            except h11.RemoteProtocolError as refusal:
                raise _Refusal(HTTPStatus(refusal.error_status_hint)) from None
            if event is not h11.NEED_DATA:
                return event
            chunk = await reader.read(RECEIVE_CHUNK_BYTES)
            if not chunk:
                return None
            connection.receive_data(chunk)

    async def _read_head(
        self, connection: h11.Connection, reader: asyncio.StreamReader
    ) -> h11.Request | None:
        event = await self._next_event(connection, reader)
        return event if isinstance(event, h11.Request) else None

    async def _read_body(
        self, connection: h11.Connection, reader: asyncio.StreamReader
    ) -> bytes | None:
        body = bytearray()
        while True:
            event = await self._next_event(connection, reader)
            if isinstance(event, h11.Data):
                body += event.data
                if len(body) > MAX_EVENT_BYTES:
                    raise _Refusal(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            elif isinstance(event, h11.EndOfMessage):
                return bytes(body)
            elif event is None:
                return None

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
