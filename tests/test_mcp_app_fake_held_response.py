"""The fake app's held response learns of a departing peer on that request's own connection.

Only the peer's TLS end or reset on the connection that carried the request marks it as left;
being held, being released, running into the bound and the app closing never do, and a failure
of the holding handler reaches the test that waits for the request's end.
"""

from __future__ import annotations

import json
import socket
import ssl
import struct
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress

import pytest
from mcp_app_fake import (
    CERTIFICATE_HOST,
    WAIT_SECONDS,
    HeldRequests,
    McpAppFake,
    TestAuthority,
    rpc_result,
    serving,
)
from service_support import fake_app_authority  # noqa: F401 - a fixture, requested by name

ANSWER = {"content": [{"type": "text", "text": "saved"}]}


@contextmanager
def an_app_holding_every_request(
    authority: TestAuthority, held: HeldRequests
) -> Iterator[McpAppFake]:
    responder = held.responder(lambda request: rpc_result(request, ANSWER))
    with serving(responder, authority.credentials_for(CERTIFICATE_HOST)) as fake:
        yield fake


def a_client_that_asked(
    fake: McpAppFake, authority: TestAuthority, request_id: int = 1
) -> ssl.SSLSocket:
    raw = socket.create_connection(("127.0.0.1", fake.server_address[1]), timeout=WAIT_SECONDS)
    context = authority.client_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    client = context.wrap_socket(raw, server_hostname=CERTIFICATE_HOST)
    body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {}})
    head = f"POST /mcp HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n\r\n"
    client.sendall((head + body).encode())
    return client


def sends_the_tls_end(client: ssl.SSLSocket) -> None:
    with suppress(ssl.SSLEOFError):
        client.unwrap()
    client.close()


def closes_the_socket(client: ssl.SSLSocket) -> None:
    client.close()


def resets_the_connection(client: ssl.SSLSocket) -> None:
    """The kernel may report the reset as an end of stream; both mark the peer left."""
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    client.close()


@pytest.mark.parametrize(
    "leave",
    [
        pytest.param(sends_the_tls_end, id="tls-end"),
        pytest.param(closes_the_socket, id="closed-socket"),
        pytest.param(resets_the_connection, id="reset"),
    ],
)
def test_a_peer_leaving_marks_only_the_request_of_its_own_connection(
    authority: TestAuthority, leave: Callable[[ssl.SSLSocket], None]
) -> None:
    held = HeldRequests()
    with an_app_holding_every_request(authority, held) as fake:
        leaving = a_client_that_asked(fake, authority, request_id=1)
        staying = a_client_that_asked(fake, authority, request_id=2)
        try:
            by_id = {request.request.message["id"]: request for request in held.wait_for(2)}

            leave(leaving)
            by_id[1].wait_for_peer_left()

            assert (by_id[1].peer_left, by_id[2].peer_left) == (True, False)
        finally:
            held.release_all()
            staying.close()


def test_a_held_request_is_not_marked_before_its_peer_does_anything(
    authority: TestAuthority,
) -> None:
    held = HeldRequests()
    with an_app_holding_every_request(authority, held) as fake:
        client = a_client_that_asked(fake, authority)
        try:
            [only] = held.wait_for(1)

            assert (only.peer_left, only.released) == (False, False)
        finally:
            held.release_all()
            client.close()


def test_a_released_request_is_answered_and_its_close_after_the_answer_is_no_departure(
    authority: TestAuthority,
) -> None:
    held = HeldRequests()
    with an_app_holding_every_request(authority, held) as fake:
        client = a_client_that_asked(fake, authority)
        try:
            [only] = held.wait_for(1)
            only.release()

            answered = b""
            while chunk := client.recv(4096):
                answered += chunk

            assert (b"saved" in answered, only.wait_for_the_end()) == (True, False)
        finally:
            client.close()


def test_a_held_request_survives_the_app_closing_unmarked(authority: TestAuthority) -> None:
    held = HeldRequests()
    with an_app_holding_every_request(authority, held) as fake:
        client = a_client_that_asked(fake, authority)
        [only] = held.wait_for(1)

    try:
        assert only.wait_for_the_end() is False
    finally:
        client.close()


def test_a_held_request_running_into_its_bound_fails_the_wait_and_is_not_marked(
    authority: TestAuthority,
) -> None:
    held = HeldRequests(bound_seconds=0.2)
    with an_app_holding_every_request(authority, held) as fake:
        client = a_client_that_asked(fake, authority)
        try:
            [only] = held.wait_for(1)

            with pytest.raises(TimeoutError):
                only.wait_for_the_end()
            assert only.peer_left is False
        finally:
            client.close()


def test_a_failure_of_the_holding_handler_reaches_the_waiting_test(
    authority: TestAuthority,
) -> None:
    held = HeldRequests()
    with an_app_holding_every_request(authority, held) as fake:
        client = a_client_that_asked(fake, authority)
        try:
            [only] = held.wait_for(1)
            client.sendall(b"more than the request")

            with pytest.raises(RuntimeError, match="sent more"):
                only.wait_for_the_end()
            assert only.peer_left is False
        finally:
            held.release_all()
            client.close()
