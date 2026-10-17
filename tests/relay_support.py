import asyncio
import re
import socket
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

import h11
from mcp_app_fake import WAIT_SECONDS

from agent_service.tool_relay import LOOPBACK_HOST, Relay

RUN_ID = "run-under-test"
_READ_BYTES = 65_536
_CLIENT_REQUEST = h11.Request(method="POST", target="/mcp", headers=[("host", "x")])


@dataclass(frozen=True)
class Answer:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class RequestHead:
    method: str
    target: str
    version: str
    fields: tuple[tuple[str, str], ...]

    @classmethod
    def standard(cls, relay: Relay, content_length: int = 0) -> "RequestHead":
        return cls(
            "POST",
            "/mcp",
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


def port_of(relay: Relay) -> int:
    port = urlsplit(relay.access.url).port
    assert port is not None
    return port


class RawClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._parser = self._new_parser()

    @classmethod
    async def connect(cls, port: int, host: str = LOOPBACK_HOST) -> "RawClient":
        async with asyncio.timeout(WAIT_SECONDS):
            reader, writer = await asyncio.open_connection(host, port)
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

    async def send(self, raw: bytes) -> None:
        self._writer.write(raw)
        async with asyncio.timeout(WAIT_SECONDS):
            await self._writer.drain()

    async def answer(self) -> Answer:
        status = 0
        headers: dict[str, str] = {}
        body = b""
        while True:
            event = self._parser.next_event()
            if event is h11.NEED_DATA:
                async with asyncio.timeout(WAIT_SECONDS):
                    self._parser.receive_data(await self._reader.read(_READ_BYTES))
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
                raise AssertionError(f"connection ended before an answer: {event!r}")

    async def at_eof(self) -> bool:
        try:
            async with asyncio.timeout(WAIT_SECONDS):
                while await self._reader.read(_READ_BYTES):
                    pass
        except ConnectionResetError:
            pass
        return True

    def disconnect(self) -> None:
        self._writer.close()

    def reset(self) -> None:
        sock = self._writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self._writer.close()


def relay_tasks(run_id: str = RUN_ID) -> list[str]:
    prefix = f"relay:{run_id}:"
    return sorted(
        name for task in asyncio.all_tasks() if (name := task.get_name()).startswith(prefix)
    )


def connection_numbers(task_names: list[str]) -> list[int]:
    return [int(re.fullmatch(r"relay:.+:conn-(\d+)", name).group(1)) for name in task_names]


async def yield_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(WAIT_SECONDS):
        while not condition():
            await asyncio.sleep(0)


async def close_bounded(relay: Relay) -> None:
    async with asyncio.timeout(WAIT_SECONDS):
        await relay.close()


def drive(scenario: Callable[[Relay], Awaitable[None]], run_id: str = RUN_ID) -> None:
    async def main() -> None:
        relay = await Relay.start(run_id)
        try:
            async with asyncio.timeout(WAIT_SECONDS * 3):
                await scenario(relay)
        finally:
            await close_bounded(relay)

    asyncio.run(main())
