import asyncio
import logging
import signal
from collections.abc import Callable
from dataclasses import replace
from http import HTTPStatus

import pytest
from mcp_app_fake import WAIT_SECONDS
from relay_support import (
    RUN_ID,
    RawClient,
    RequestHead,
    close_bounded,
    connection_numbers,
    drive,
    port_of,
    relay_tasks,
    yield_until,
)

from agent_service.tool_relay import (
    ADMITTED_STAND_IN,
    LOOPBACK_HOST,
    MAX_HEAD_BYTES,
    RECEIVE_CHUNK_BYTES,
    Relay,
)

CANARY = "canary-4c9e1b7a"
ADMITTED = int(ADMITTED_STAND_IN)
SIGNALS = (signal.SIGINT, signal.SIGTERM)

HeadBuilder = Callable[[Relay], bytes]


def standard(relay: Relay, content_length: int = 0) -> RequestHead:
    return RequestHead.standard(relay, content_length)


def host_of(relay: Relay) -> str:
    return f"{LOOPBACK_HOST}:{port_of(relay)}"


def other_secret(relay: Relay) -> str:
    secret = relay.access.secret
    return secret[:-1] + ("A" if secret[-1] != "A" else "B")


def padded_to(head: RequestHead, size: int) -> bytes:
    pad_overhead = len(head.adding("X-Pad", "").encode())
    return head.adding("X-Pad", "a" * (size - pad_overhead)).encode()


def mixed_case_names(head: RequestHead) -> RequestHead:
    renamed = {"host": "HOST", "authorization": "AuThOrIzAtIoN", "content-length": "cOnTeNt-LeNgTh"}
    return replace(head, fields=tuple((renamed[n.lower()], v) for n, v in head.fields))


def padded_values(relay: Relay) -> RequestHead:
    return (
        standard(relay)
        .with_field("Host", f" \t{host_of(relay)} \t")
        .with_field("Authorization", f"\t Bearer {relay.access.secret} ")
    )


ADMITTED_HEADS = [
    pytest.param(lambda r: padded_values(r).encode(), id="A6"),
    pytest.param(lambda r: mixed_case_names(standard(r)).encode(), id="A7"),
    pytest.param(lambda r: padded_to(standard(r), MAX_HEAD_BYTES), id="A8"),
]


@pytest.mark.parametrize("build", ADMITTED_HEADS)
def test_admits_a_head_that_passes_every_gate(build: HeadBuilder) -> None:
    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(build(relay))
        answer = await client.answer()
        assert answer.status == ADMITTED
        assert answer.body == b""
        assert await client.at_eof()

    drive(scenario)


def test_binds_loopback_and_admits_an_authorised_post_without_reading_its_body() -> None:
    async def scenario(relay: Relay) -> None:
        port = port_of(relay)
        assert port != 0
        assert relay.access.url == f"http://{LOOPBACK_HOST}:{port}/mcp"
        with pytest.raises(ConnectionRefusedError):
            await RawClient.connect(port, host="127.0.0.2")
        client = await RawClient.connect(port)
        await client.send(standard(relay, content_length=5).encode() + b"hello")
        assert (await client.answer()).status == ADMITTED
        assert await client.at_eof()

    drive(scenario)


def test_two_relays_differ_and_show_no_secret_in_repr() -> None:
    async def main() -> None:
        first = await Relay.start("run-a")
        second = await Relay.start("run-b")
        try:
            assert port_of(first) != port_of(second)
            assert first.access.secret != second.access.secret
            for relay in (first, second):
                assert relay.access.secret not in repr(relay)
                assert relay.access.secret not in repr(relay.access)
        finally:
            await close_bounded(first)
            await close_bounded(second)

    asyncio.run(main())


def test_leaves_signal_handlers_unchanged_while_serving_and_after_close() -> None:
    def handlers() -> dict[signal.Signals, object]:
        return {sig: signal.getsignal(sig) for sig in SIGNALS}

    async def main() -> None:
        before = handlers()
        relay = await Relay.start(RUN_ID)
        try:
            client = await RawClient.connect(port_of(relay))
            await client.send(b"POST /mcp HTTP/1.1\r\nHost:")
            await yield_until(lambda: len(relay_tasks()) == 1)
            assert handlers() == before
        finally:
            await close_bounded(relay)
        assert handlers() == before

    asyncio.run(main())


def test_close_ends_idle_and_mid_head_connections_and_refuses_the_old_port() -> None:
    async def scenario(relay: Relay) -> None:
        port = port_of(relay)
        idle = await RawClient.connect(port)
        await idle.send(standard(relay).replace_method("GET").encode())
        assert (await idle.answer()).status == HTTPStatus.METHOD_NOT_ALLOWED
        mid_head = await RawClient.connect(port)
        await mid_head.send(b"POST /mcp HTTP/1.1\r\nHost:")
        await yield_until(lambda: len(relay_tasks()) == 2)
        numbers = connection_numbers(relay_tasks())
        assert len(set(numbers)) == 2

        await close_bounded(relay)

        assert await idle.at_eof()
        assert await mid_head.at_eof()
        with pytest.raises(ConnectionRefusedError):
            await RawClient.connect(port)
        assert relay_tasks() == []
        async with asyncio.timeout(WAIT_SECONDS):
            await relay.close()

    drive(scenario)


def relay_bearer(relay: Relay) -> str:
    return f"Bearer {relay.access.secret}"


def test_a_client_disconnecting_mid_head_leaves_nothing_behind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(b"POST /mcp HTTP/1.1\r\nHost:")
        await yield_until(lambda: len(relay_tasks()) == 1)
        client.disconnect()
        await yield_until(lambda: relay_tasks() == [])

    drive(scenario)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def refusal(relay: Relay, **changes: str) -> RequestHead:
    head = standard(relay, 10).adding("X-Canary", CANARY)
    return replace(head, **changes) if changes else head


def canary_target(path: str = "/mcp") -> str:
    return f"{path}?c={CANARY}"


def over_head_limit(relay: Relay) -> bytes:
    return padded_to(refusal(relay), MAX_HEAD_BYTES + RECEIVE_CHUNK_BYTES + 1)


def unfinished_head(relay: Relay) -> bytes:
    return b"POST /mcp HTTP/1.1\r\nX-Pad: " + b"a" * (MAX_HEAD_BYTES + 4096)


def own_origin(relay: Relay) -> str:
    return f"http://{host_of(relay)}"


CLOSING_REFUSALS = [
    pytest.param(
        lambda r: refusal(r, target=canary_target()).without("Host").encode(), 400, id="A9"
    ),
    pytest.param(
        lambda r: refusal(r, target=canary_target()).adding("Host", host_of(r)).encode(),
        400,
        id="A10",
    ),
    pytest.param(
        lambda r: refusal(r, target=canary_target()).adding("Content-Length", "11").encode(),
        400,
        id="A11",
    ),
    pytest.param(
        lambda r: refusal(r).without("Content-Length").adding("Transfer-Encoding", "gzip").encode(),
        501,
        id="A12",
    ),
    pytest.param(lambda r: b"this is not http\r\n\r\n", 400, id="A13"),
    pytest.param(over_head_limit, 431, id="A14"),
    pytest.param(unfinished_head, 431, id="A15"),
    pytest.param(
        lambda r: refusal(r).with_field("Host", f"localhost:{port_of(r)}").encode(), 403, id="A16"
    ),
    pytest.param(
        lambda r: refusal(r).with_field("Host", f"{LOOPBACK_HOST}:{port_of(r) + 1}").encode(),
        403,
        id="A17",
    ),
    pytest.param(lambda r: refusal(r, version="HTTP/1.0").without("Host").encode(), 403, id="A18"),
    pytest.param(
        lambda r: (
            refusal(r, target=canary_target())
            .with_field("Host", "localhost:1")
            .with_field("Authorization", "Bearer wrong")
            .encode()
        ),
        403,
        id="A19",
    ),
    pytest.param(lambda r: refusal(r).adding("Origin", own_origin(r)).encode(), 403, id="A20"),
    pytest.param(lambda r: refusal(r).adding("Origin", "").encode(), 403, id="A21"),
    pytest.param(
        lambda r: (
            refusal(r, method="OPTIONS", target=canary_target())
            .without("Authorization")
            .adding("Origin", "https://example.invalid")
            .encode()
        ),
        403,
        id="A22",
    ),
    pytest.param(lambda r: refusal(r).without("Authorization").encode(), 401, id="A23"),
    pytest.param(
        lambda r: (
            refusal(r, target=canary_target())
            .with_field("Authorization", f"Bearer {other_secret(r)}")
            .encode()
        ),
        401,
        id="A24",
    ),
    pytest.param(
        lambda r: refusal(r).with_field("Authorization", f"bearer {r.access.secret}").encode(),
        401,
        id="A25",
    ),
    pytest.param(
        lambda r: refusal(r).adding("Authorization", relay_bearer(r)).encode(), 401, id="A26"
    ),
    pytest.param(
        lambda r: refusal(r, method="GET").without("Authorization").encode(), 401, id="A27"
    ),
    pytest.param(
        lambda r: refusal(r, target="/other").without("Authorization").encode(), 401, id="A28"
    ),
    pytest.param(lambda r: refusal(r, method="GET").encode(), 405, id="A36"),
    pytest.param(lambda r: refusal(r, target=canary_target("/other")).encode(), 404, id="A37"),
    pytest.param(
        lambda r: (
            refusal(r, method="GET")
            .without("Content-Length")
            .adding("Transfer-Encoding", "chunked")
            .encode()
        ),
        405,
        id="A38",
    ),
    pytest.param(
        lambda r: (
            refusal(r, method="GET")
            .with_field("Content-Length", "0")
            .adding("Connection", "close")
            .encode()
        ),
        405,
        id="A39",
    ),
]


@pytest.mark.parametrize(("build", "status"), CLOSING_REFUSALS)
def test_refuses_with_an_empty_closing_answer_and_leaks_no_request_content(
    build: HeadBuilder,
    status: int,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    secrets_seen: list[str] = []

    async def scenario(relay: Relay) -> None:
        secrets_seen.append(relay.access.secret)
        client = await RawClient.connect(port_of(relay))
        await client.send(build(relay))
        answer = await client.answer()
        assert answer.status == status
        assert answer.body == b""
        assert answer.headers["connection"] == "close"
        assert "www-authenticate" not in answer.headers
        assert (answer.headers.get("allow") == "POST") == (status == 405)
        assert await client.at_eof()

    drive(scenario)
    captured = capfd.readouterr()
    logged = caplog.text + captured.out + captured.err
    assert CANARY not in logged
    assert secrets_seen[0] not in logged


def own_port_url(relay: Relay) -> str:
    return f"http://{host_of(relay)}/mcp"


KEPT_REFUSALS = [
    pytest.param(lambda r: standard(r).replace_target("/mcp/"), 404, id="A31"),
    pytest.param(lambda r: standard(r).replace_target("/mcp?x=1"), 404, id="A32"),
    pytest.param(lambda r: standard(r).replace_target(own_port_url(r)), 404, id="A33"),
    pytest.param(lambda r: standard(r).replace_method("GET"), 405, id="A34"),
    pytest.param(lambda r: standard(r).replace_method("DELETE"), 405, id="A35"),
]


@pytest.mark.parametrize(("build", "status"), KEPT_REFUSALS)
def test_keeps_the_connection_after_a_bodyless_not_found_or_method_refusal(
    build: Callable[[Relay], RequestHead], status: int
) -> None:
    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(build(relay).encode())
        refused = await client.answer()
        assert refused.status == status
        assert refused.body == b""
        assert "connection" not in refused.headers
        await client.send(standard(relay).encode())
        assert (await client.answer()).status == ADMITTED

    drive(scenario)


def test_a_closed_relays_secret_is_refused_by_a_new_relay_of_the_same_run() -> None:
    async def main() -> None:
        old = await Relay.start(RUN_ID)
        old_secret = old.access.secret
        await close_bounded(old)
        new = await Relay.start(RUN_ID)
        try:
            client = await RawClient.connect(port_of(new))
            stale = standard(new).with_field("Authorization", f"Bearer {old_secret}")
            await client.send(stale.encode())
            assert (await client.answer()).status == HTTPStatus.UNAUTHORIZED
        finally:
            await close_bounded(new)

    asyncio.run(main())


def test_two_open_relays_each_refuse_the_other_secret_and_admit_their_own() -> None:
    async def main() -> None:
        first = await Relay.start("run-a")
        second = await Relay.start("run-b")
        try:
            for own, foreign in ((first, second), (second, first)):
                client = await RawClient.connect(port_of(own))
                crossed = standard(own).with_field("Authorization", relay_bearer(foreign))
                await client.send(crossed.encode())
                assert (await client.answer()).status == HTTPStatus.UNAUTHORIZED
                client = await RawClient.connect(port_of(own))
                await client.send(standard(own).encode())
                assert (await client.answer()).status == ADMITTED
        finally:
            await close_bounded(first)
            await close_bounded(second)

    asyncio.run(main())
