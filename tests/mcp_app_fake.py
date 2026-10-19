"""A fake app that speaks the JSON-only MCP profile over real TLS sockets.

The server is stdlib ``ThreadingHTTPServer`` on 127.0.0.1 behind a test CA and
a server certificate generated at run time with ``cryptography``; nothing is
committed. Every request is recorded and answered by a responder the test
supplies. Every wait carries a timeout and ``close`` releases every held
response, so no handler outlives its test. Connections are HTTP/1.0 unless
the server is started with ``keep_alive``; every accepted connection is
counted before its TLS handshake, and every ended connection once its handler is done.
A response from ``HeldRequests`` is held: the request is taken, no answer is sent, and only
the peer leaving that request's own connection marks it as left.
"""

from __future__ import annotations

import json
import ssl
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

LOOPBACK: Final = "127.0.0.1"
CERTIFICATE_HOST: Final = "localhost"
WAIT_SECONDS: Final = 10.0
POLL_SECONDS: Final = 0.01
_VALID_FOR: Final = timedelta(days=1)
_BACKDATED: Final = timedelta(minutes=5)


@dataclass(frozen=True)
class TlsCredentials:
    certificate_file: Path
    key_file: Path


def _certificate_signing_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=True,
        encipher_only=False,
        decipher_only=False,
    )


class TestAuthority:
    """A throwaway CA that issues server certificates for the fake."""

    __test__ = False

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._key = ec.generate_private_key(ec.SECP256R1())
        self._name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake app test CA")])
        self._certificate = self._issue_certificate(
            self._name, self._key.public_key(), [], is_authority=True
        )
        self.ca_file = directory / "ca.pem"
        self.ca_file.write_bytes(self._certificate.public_bytes(serialization.Encoding.PEM))

    def _issue_certificate(
        self,
        subject: x509.Name,
        public_key: Any,
        hostnames: list[str],
        *,
        is_authority: bool = False,
    ) -> x509.Certificate:
        now = datetime.now(UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._name)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _BACKDATED)
            .not_valid_after(now + _VALID_FOR)
            .add_extension(x509.BasicConstraints(ca=is_authority, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()),
                critical=False,
            )
        )
        if is_authority:
            builder = builder.add_extension(_certificate_signing_usage(), critical=True)
        if hostnames:
            names = [x509.DNSName(host) for host in hostnames]
            builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
        return builder.sign(self._key, hashes.SHA256())

    def credentials_for(self, hostname: str) -> TlsCredentials:
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
        certificate = self._issue_certificate(subject, key.public_key(), [hostname])
        certificate_file = self._directory / f"{hostname}.pem"
        key_file = self._directory / f"{hostname}.key"
        certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        return TlsCredentials(certificate_file, key_file)

    def client_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.ca_file))


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes
    connection: int = 0

    @property
    def message(self) -> dict[str, Any]:
        return json.loads(self.body) if self.body else {}

    @property
    def rpc_method(self) -> str | None:
        return self.message.get("method")

    @property
    def identity(self) -> tuple[int, Any]:
        return self.connection, self.message.get("id")


class HeldRequest:
    """A request the app took and answers only once released.

    The handler of its connection calls ``keep_until_released``; the request counts as left only
    when that connection shows the peer's TLS end or reset. Running into the bound, the app
    closing and the test releasing never mark it, and a failure of the handler is kept for the
    test thread to see in ``wait_for_the_end``.
    """

    def __init__(self, request: RecordedRequest, answer: FakeResponse, bound_seconds: float):
        self.request = request
        self.answer = answer
        self._bound_seconds = bound_seconds
        self._released = threading.Event()
        self._ended = threading.Event()
        self._peer_left = threading.Event()
        self._failure: Exception | None = None

    @property
    def peer_left(self) -> bool:
        return self._peer_left.is_set()

    @property
    def released(self) -> bool:
        return self._released.is_set()

    def release(self) -> None:
        self._released.set()

    def wait_for_the_end(self) -> bool:
        """Whether the peer left, once the handler stopped holding; raises what failed it."""
        assert self._ended.wait(self._bound_seconds + WAIT_SECONDS), "the held request never ended"
        if self._failure is not None:
            raise self._failure
        return self.peer_left

    def wait_for_peer_left(self) -> None:
        assert self.wait_for_the_end(), "the held request ended without its peer leaving"

    def keep_until_released(self, connection: ssl.SSLSocket, app_closing: threading.Event) -> bool:
        """Hold the connection; true when the test released it and the answer is to be sent."""
        connection.settimeout(POLL_SECONDS)
        deadline = time.monotonic() + self._bound_seconds
        try:
            while not (self._released.is_set() or app_closing.is_set()):
                if time.monotonic() > deadline:
                    raise TimeoutError("the held request was neither released nor left")
                if self._peer_has_left(connection):
                    self._peer_left.set()
                    return False
            return self._released.is_set()
        except Exception as failure:
            self._failure = failure
            return False
        finally:
            self._ended.set()

    @staticmethod
    def _peer_has_left(connection: ssl.SSLSocket) -> bool:
        try:
            received = connection.recv(1)
        except TimeoutError:
            return False
        except (ConnectionResetError, BrokenPipeError):
            return True
        if received:
            raise RuntimeError("the peer sent more while its request was held")
        return True


class HeldRequests:
    """The requests a test holds in the app; ``responder`` turns an answer into a held one."""

    def __init__(self, bound_seconds: float = WAIT_SECONDS) -> None:
        self._bound_seconds = bound_seconds
        self._condition = threading.Condition()
        self._held: list[HeldRequest] = []

    def responder(self, answer: Responder) -> Responder:
        def holding(request: RecordedRequest) -> FakeResponse:
            held = HeldRequest(request, answer(request), self._bound_seconds)
            with self._condition:
                self._held.append(held)
                self._condition.notify_all()
            return FakeResponse(fate=Fate.HOLD, hold=held)

        return holding

    def wait_for(self, count: int) -> list[HeldRequest]:
        """The requests held right now, once at least ``count`` are."""
        with self._condition:
            assert self._condition.wait_for(
                lambda: len(self._unreleased()) >= count, self._bound_seconds
            ), f"fewer than {count} requests were held"
            return self._unreleased()

    def release_all(self) -> None:
        with self._condition:
            for held in self._held:
                held.release()

    def _unreleased(self) -> list[HeldRequest]:
        return [held for held in self._held if not held.released]


class Fate(StrEnum):
    ANSWER = "answer"
    DROP_UNANSWERED = "drop_unanswered"
    CUT_MID_BODY = "cut_mid_body"
    HOLD = "hold"


@dataclass(frozen=True)
class FakeResponse:
    """One scripted answer.

    ``held_back`` is sent only after the test releases it. The ``fate`` makes
    the app close the connection without any answer, or after ``body`` while
    the headers promised ``held_back`` as well. The ``HOLD`` fate carries the ``hold`` that
    decides when, and whether, its answer is sent.
    """

    status: int = 200
    content_type: str | None = "application/json"
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    held_back: bytes = b""
    fate: Fate = Fate.ANSWER
    hold: HeldRequest | None = None

    def __post_init__(self) -> None:
        if (self.fate is Fate.HOLD) != (self.hold is not None):
            raise ValueError("a response is held exactly when it carries its hold")


DROPPED: Final = FakeResponse(fate=Fate.DROP_UNANSWERED)


def rpc_result(request: RecordedRequest, result: Mapping[str, Any], **kwargs: Any) -> FakeResponse:
    reply = {"jsonrpc": "2.0", "id": request.message["id"], "result": result}
    return FakeResponse(body=json.dumps(reply).encode(), **kwargs)


def rpc_error(request: RecordedRequest, code: int, message: str) -> FakeResponse:
    reply = {
        "jsonrpc": "2.0",
        "id": request.message["id"],
        "error": {"code": code, "message": message},
    }
    return FakeResponse(body=json.dumps(reply).encode())


ACKNOWLEDGED: Final = FakeResponse(status=202, content_type=None)
Responder = Callable[[RecordedRequest], FakeResponse]


def tool_listing(names: list[str], next_cursor: str | None = None) -> dict[str, Any]:
    tools = [
        {"name": name, "description": name, "inputSchema": {"type": "object"}} for name in names
    ]
    return {"tools": tools} if next_cursor is None else {"tools": tools, "nextCursor": next_cursor}


class ScriptedApp:
    """A well-behaved app; ``overrides`` replace the answer to one JSON-RPC method."""

    def __init__(
        self,
        *,
        pages: list[dict[str, Any]] | None = None,
        protocol_version: str = "2025-11-25",
        session_id: str | None = None,
        overrides: Mapping[str, Responder] | None = None,
    ) -> None:
        self._pages = pages or [tool_listing(["echo"])]
        self._protocol_version = protocol_version
        self._session_id = session_id
        self._overrides = dict(overrides or {})

    def __call__(self, request: RecordedRequest) -> FakeResponse:
        method = request.rpc_method
        if method in self._overrides:
            return self._overrides[method](request)
        if method == "initialize":
            headers = {} if self._session_id is None else {"Mcp-Session-Id": self._session_id}
            return rpc_result(request, {"protocolVersion": self._protocol_version}, headers=headers)
        if method == "notifications/initialized":
            return ACKNOWLEDGED
        if method == "tools/list":
            cursor = request.message.get("params", {}).get("cursor")
            index = 0 if cursor is None else int(cursor.removeprefix("page-"))
            return rpc_result(request, self._pages[index])
        if method == "tools/call":
            text = request.message["params"]["name"]
            return rpc_result(request, {"content": [{"type": "text", "text": text}]})
        return FakeResponse(status=404, content_type=None)


def _lower_cased(headers: Any) -> dict[str, str]:
    return {name.lower(): value for name, value in headers}


class _Handler(BaseHTTPRequestHandler):
    server: McpAppFake
    timeout = WAIT_SECONDS

    def setup(self) -> None:
        super().setup()
        self.protocol_version = "HTTP/1.1" if self.server.keep_alive else "HTTP/1.0"

    def _record(self, method: str, body: bytes) -> RecordedRequest:
        request = RecordedRequest(
            method,
            self.path,
            _lower_cased(self.headers.items()),
            body,
            connection=self.client_address[1],
        )
        self.server.record(request)
        return request

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        request = self._record("POST", self.rfile.read(length))
        response = self.server.responder(request)
        if response.fate is Fate.DROP_UNANSWERED:
            self.close_connection = True
            return
        if response.hold is not None:
            if not response.hold.keep_until_released(self.connection, self.server.closing):
                self.close_connection = True
                return
            response = response.hold.answer
        self._send(response)

    def do_DELETE(self) -> None:
        self._record("DELETE", b"")
        self._send(FakeResponse(status=405, content_type=None))

    def do_GET(self) -> None:
        self._record("GET", b"")
        self._send(FakeResponse(status=405, content_type=None))

    def _send(self, response: FakeResponse) -> None:
        self.send_response(response.status)
        if response.content_type is not None:
            self.send_header("Content-Type", response.content_type)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body) + len(response.held_back)))
        self.end_headers()
        self.wfile.write(response.body)
        self.wfile.flush()
        if response.fate is Fate.CUT_MID_BODY:
            self.close_connection = True
            return
        if response.held_back and self.server.tail_released.wait(WAIT_SECONDS):
            self.wfile.write(response.held_back)

    def log_message(self, format: str, *args: Any) -> None:
        return None


class McpAppFake(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, responder: Responder, credentials: TlsCredentials, *, keep_alive: bool = False
    ) -> None:
        super().__init__((LOOPBACK, 0), _Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(credentials.certificate_file, credentials.key_file)
        self.socket = context.wrap_socket(
            self.socket, server_side=True, do_handshake_on_connect=False
        )
        self.responder = responder
        self.keep_alive = keep_alive
        self.tail_released = threading.Event()
        self.closing = threading.Event()
        self._accepted_connections = 0
        self._ended_connections = 0
        self._recorded: list[RecordedRequest] = []
        self._lock = threading.Lock()

    @property
    def origin(self) -> str:
        return f"https://{CERTIFICATE_HOST}:{self.server_address[1]}"

    @property
    def requests(self) -> list[RecordedRequest]:
        with self._lock:
            return list(self._recorded)

    @property
    def accepted_connections(self) -> int:
        with self._lock:
            return self._accepted_connections

    @property
    def ended_connections(self) -> int:
        with self._lock:
            return self._ended_connections

    def shutdown_request(self, request: Any) -> None:
        super().shutdown_request(request)
        with self._lock:
            self._ended_connections += 1

    def get_request(self) -> Any:
        accepted = super().get_request()
        with self._lock:
            self._accepted_connections += 1
        return accepted

    def record(self, request: RecordedRequest) -> None:
        with self._lock:
            self._recorded.append(request)

    def handle_error(self, request: Any, client_address: Any) -> None:
        return None


@contextmanager
def serving(
    responder: Responder, credentials: TlsCredentials, *, keep_alive: bool = False
) -> Iterator[McpAppFake]:
    fake = McpAppFake(responder, credentials, keep_alive=keep_alive)
    thread = threading.Thread(target=fake.serve_forever, args=(POLL_SECONDS,), daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        fake.closing.set()
        fake.tail_released.set()
        fake.shutdown()
        fake.server_close()
        thread.join(WAIT_SECONDS)
        assert not thread.is_alive()
