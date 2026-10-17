"""The per-run loopback relay: a listener that judges every request head."""

import asyncio
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from http import HTTPStatus
from itertools import count
from typing import Final

import h11

from agent_client.capability import MCP_ENDPOINT_PATH
from agent_service.run_liveness import log_failure

log = logging.getLogger(__name__)

LOOPBACK_HOST: Final = "127.0.0.1"
MAX_HEAD_BYTES: Final = 16_384
RECEIVE_CHUNK_BYTES: Final = 8_192
ADMITTED_STAND_IN: Final = HTTPStatus.SERVICE_UNAVAILABLE
_SECRET_BYTES: Final = 32
_BEARER_SCHEME: Final = b"Bearer "
_ENDPOINT_TARGET: Final = MCP_ENDPOINT_PATH.encode()
_BODYLESS_REFUSALS: Final = frozenset({HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED})


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


class Relay:
    def __init__(self, run_id: str, secret: str) -> None:
        self._run_id = run_id
        self._bearer = _BEARER_SCHEME + secret.encode()
        self._secret = secret
        self._expected_host = b""
        self._connection_numbers = count(1)
        self._connections: dict[asyncio.Task[None], asyncio.Transport] = {}
        self._closing = False
        self._server: asyncio.Server
        self.access: RelayAccess

    @classmethod
    async def start(cls, run_id: str) -> "Relay":
        relay = cls(run_id, secrets.token_urlsafe(_SECRET_BYTES))
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
        request = await self._read_head(connection, reader, writer)
        if request is None:
            return False
        status = self._head_status(request)
        can_stay = (
            status in _BODYLESS_REFUSALS
            and _declares_no_body(request)
            and isinstance(connection.next_event(), h11.EndOfMessage)
        )
        allowed = [(b"allow", b"POST")] if status is HTTPStatus.METHOD_NOT_ALLOWED else []
        await self._answer(connection, writer, status, allowed, closing=not can_stay)
        return can_stay and connection.our_state is h11.DONE and connection.their_state is h11.DONE

    async def _read_head(
        self, connection: h11.Connection, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> h11.Request | None:
        while True:
            try:
                event = connection.next_event()
            except h11.RemoteProtocolError as refusal:
                status = HTTPStatus(refusal.error_status_hint)
                await self._answer(connection, writer, status, [], closing=True)
                return None
            if event is h11.NEED_DATA:
                chunk = await reader.read(RECEIVE_CHUNK_BYTES)
                if not chunk:
                    return None
                connection.receive_data(chunk)
            elif isinstance(event, h11.Request):
                return event
            else:
                return None

    def _head_status(self, request: h11.Request) -> HTTPStatus:
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
        return ADMITTED_STAND_IN

    async def _answer(
        self,
        connection: h11.Connection,
        writer: asyncio.StreamWriter,
        status: HTTPStatus,
        headers: list[tuple[bytes, bytes]],
        *,
        closing: bool,
    ) -> None:
        fields = [(b"content-length", b"0"), *headers]
        if closing:
            fields.append((b"connection", b"close"))
        response = h11.Response(status_code=status.value, headers=fields)
        writer.write(connection.send(response) + connection.send(h11.EndOfMessage()))
        await writer.drain()
