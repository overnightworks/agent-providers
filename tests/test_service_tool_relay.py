import asyncio
import base64
import json
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from bounds import TEST_WAIT_SECONDS
from pydantic import SecretStr
from relay_support import (
    OWNER_ANSWER,
    OWNER_BODY,
    RUN_ID,
    AbsorbingOwner,
    Delivery,
    HoldingSteppedLoop,
    RawClient,
    RelayReads,
    RelayWrites,
    RequestHead,
    ScriptedOwner,
    chunk,
    chunked_head,
    close_bounded,
    connection_numbers,
    drive,
    holding_loop,
    port_of,
    post,
    relay_tasks,
    task_named,
    yield_until,
)

from agent_runtime.claude.provider import ClaudeRun
from agent_runtime.cli_run import RelayAccess as CoreRelayAccess
from agent_runtime_client.mcp_wire import ACKNOWLEDGEMENT, JSON_MEDIA_TYPE, HttpAnswer
from agent_runtime_client.wire import MAX_EVENT_BYTES, TurnMessage, TurnRole
from agent_runtime_service.run_liveness import LoopClock
from agent_runtime_service.tool_relay import (
    LOOPBACK_HOST,
    MAX_PIPELINED_BYTES,
    MAX_RELAY_CONNECTIONS,
    RECEIVE_CHUNK_BYTES,
    UNANSWERED,
    Relay,
    RelayAccess,
)

CANARY = "canary-4c9e1b7a"
HEAD_LIMIT_BYTES = 16_384
UNFINISHED_HEAD_BYTES = 20_480
REFUSED_HEAD_BYTES = 24_577
ADMITTED_LONG_HEAD_BYTES = 24_576
SIGNALS = (signal.SIGINT, signal.SIGTERM)

HeadBuilder = Callable[[Relay], bytes]


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
        RequestHead.standard(relay)
        .with_field("Host", f" \t{host_of(relay)} \t")
        .with_field("Authorization", f"\t Bearer {relay.access.secret} ")
    )


def head_from_the_claude_run_file(relay: Relay) -> bytes:
    run = ClaudeRun(
        Path("/opt/claude/claude"),
        SecretStr("token"),
        "system",
        1,
        (TurnMessage(role=TurnRole.USER, text="hi"),),
        relay=relay.access,
        tool_timeout_seconds=600.0,
        model=None,
    )
    server = json.loads(run.run_files()["relay-mcp.json"])["mcpServers"]["relay"]
    return (
        RequestHead.standard(relay)
        .replace_target(urlsplit(server["url"]).path)
        .with_field("Authorization", server["headers"]["Authorization"])
        .encode()
    )


ADMITTED_HEADS = [
    pytest.param(head_from_the_claude_run_file, id="5r"),
    pytest.param(lambda r: padded_values(r).encode(), id="C35"),
    pytest.param(lambda r: mixed_case_names(RequestHead.standard(r)).encode(), id="C36"),
    pytest.param(lambda r: padded_to(RequestHead.standard(r), HEAD_LIMIT_BYTES), id="C37"),
    pytest.param(lambda r: padded_to(RequestHead.standard(r), ADMITTED_LONG_HEAD_BYTES), id="C38"),
]


def test_the_released_relay_access_import_is_the_core_type() -> None:
    assert RelayAccess is CoreRelayAccess


@pytest.mark.parametrize("build", ADMITTED_HEADS)
def test_hands_a_head_that_passes_every_gate_to_the_owner(build: HeadBuilder) -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(build(relay))
        answer = await client.answer()
        assert answer.status == HTTPStatus.OK
        assert answer.body == OWNER_BODY
        assert owner.bodies == [b""]

    drive(scenario, owner=owner)


def test_binds_loopback_and_hands_an_authorised_post_to_the_owner() -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay) -> None:
        port = port_of(relay)
        assert port != 0
        assert relay.access.url == f"http://127.0.0.1:{port}/mcp"
        with pytest.raises(ConnectionRefusedError):
            await RawClient.connect(port, host="127.0.0.2")
        client = await RawClient.connect(port)
        await client.send(RequestHead.standard(relay, content_length=10).encode() + b"helloworld")
        answer = await client.answer()
        assert (answer.status, answer.body) == (HTTPStatus.OK, OWNER_BODY)
        assert owner.bodies == [b"helloworld"]

    drive(scenario, owner=owner)


def test_two_relays_differ_and_show_no_secret_in_repr() -> None:
    async def main() -> None:
        first = await Relay.start("run-a", ScriptedOwner(), LoopClock())
        second = await Relay.start("run-b", ScriptedOwner(), LoopClock())
        try:
            assert port_of(first) != port_of(second)
            assert first.access.secret != second.access.secret
            for relay in (first, second):
                assert len(base64.urlsafe_b64decode(relay.access.secret + "=")) == 32
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
        relay = await Relay.start(RUN_ID, ScriptedOwner(), LoopClock())
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
        await idle.send(RequestHead.standard(relay).replace_method("GET").encode())
        assert (await idle.answer()).status == HTTPStatus.METHOD_NOT_ALLOWED
        mid_head = await RawClient.connect(port)
        get = RequestHead.standard(relay).replace_method("GET").encode()
        await mid_head.send(get + b"POST /mcp HTTP/1.1\r\nHost:")
        assert (await mid_head.answer()).status == HTTPStatus.METHOD_NOT_ALLOWED
        await yield_until(lambda: len(connection_numbers(relay_tasks())) == 2)
        assert len(relay_tasks()) == 3
        assert len(set(connection_numbers(relay_tasks()))) == 2

        await close_bounded(relay)

        assert await idle.at_eof()
        assert await mid_head.at_eof()
        with pytest.raises(ConnectionRefusedError):
            await RawClient.connect(port)
        assert relay_tasks() == []
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            await relay.close()

    drive(scenario)


def relay_bearer(relay: Relay) -> str:
    return f"Bearer {relay.access.secret}"


def unlogged_beyond_asyncio_debug(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name != "asyncio" or r.levelno >= logging.WARNING]


def warnings_logged(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    "leave",
    [
        pytest.param(RawClient.disconnect, id="A5"),
        pytest.param(RawClient.reset, id="A42"),
    ],
)
def test_a_client_leaving_mid_head_leaves_nothing_behind(
    leave: Callable[[RawClient], None], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(b"POST /mcp HTTP/1.1\r\nHost:")
        await yield_until(lambda: len(relay_tasks()) == 1)
        leave(client)
        await yield_until(lambda: relay_tasks() == [])

    drive(scenario)
    assert unlogged_beyond_asyncio_debug(caplog) == []


def refusal(relay: Relay, content_length: int = 0, **changes: str) -> RequestHead:
    head = RequestHead.standard(relay, content_length).adding("X-Canary", CANARY)
    return replace(head, **changes) if changes else head


def canary_target(path: str = "/mcp") -> str:
    return f"{path}?c={CANARY}"


def over_head_limit(relay: Relay) -> bytes:
    return padded_to(refusal(relay), REFUSED_HEAD_BYTES)


def unfinished_head(relay: Relay) -> bytes:
    return b"POST /mcp HTTP/1.1\r\nX-Pad: " + b"a" * UNFINISHED_HEAD_BYTES


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
    pytest.param(lambda r: f"this is not http {CANARY}\r\n\r\n".encode(), 400, id="A13"),
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
            refusal(r, 10, target=canary_target())
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
            refusal(r, 10, method="OPTIONS", target=canary_target())
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
            refusal(r, 10, target=canary_target())
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
    pytest.param(lambda r: refusal(r, 10, method="GET").encode(), 405, id="A36"),
    pytest.param(lambda r: refusal(r, 10, target=canary_target("/other")).encode(), 404, id="A37"),
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
            .without("Content-Length")
            .adding("Transfer-Encoding", "chunked")
            .encode()
            + f"zz-{CANARY}\r\n".encode()
        ),
        405,
        id="A40",
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
        assert "content-type" not in answer.headers
        assert (answer.headers.get("allow") == "POST") == (status == 405)
        assert await client.at_eof()

    drive(scenario)
    captured = capfd.readouterr()
    logged = caplog.text + captured.out + captured.err
    assert warnings_logged(caplog) == []
    assert CANARY not in logged
    assert secrets_seen[0] not in logged


def own_port_url(relay: Relay) -> str:
    return f"http://{host_of(relay)}/mcp"


KEPT_REFUSALS = [
    pytest.param(lambda r: RequestHead.standard(r).replace_target("/mcp/"), 404, id="C40"),
    pytest.param(lambda r: RequestHead.standard(r).replace_target("/mcp?x=1"), 404, id="C41"),
    pytest.param(lambda r: RequestHead.standard(r).replace_target(own_port_url(r)), 404, id="C42"),
    pytest.param(lambda r: RequestHead.standard(r).replace_method("GET"), 405, id="C43"),
    pytest.param(lambda r: RequestHead.standard(r).replace_method("DELETE"), 405, id="C44"),
    pytest.param(
        lambda r: RequestHead.standard(r).replace_method("GET").replace_target("/other"),
        404,
        id="C45",
    ),
]


@pytest.mark.parametrize(("build", "status"), KEPT_REFUSALS)
def test_keeps_the_connection_after_a_bodyless_not_found_or_method_refusal(
    build: Callable[[Relay], RequestHead], status: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    owner = ScriptedOwner()

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(build(relay).encode())
        refused = await client.answer()
        assert refused.status == status
        assert refused.body == b""
        assert "connection" not in refused.headers
        assert "content-type" not in refused.headers
        assert (refused.headers.get("allow") == "POST") == (status == 405)
        await client.send(RequestHead.standard(relay).encode())
        followed = await client.answer()
        assert (followed.status, followed.body) == (HTTPStatus.OK, OWNER_BODY)
        assert owner.bodies == [b""]

    drive(scenario, owner=owner)
    assert warnings_logged(caplog) == []


def test_a_closed_relays_secret_is_refused_by_a_new_relay_of_the_same_run() -> None:
    async def main() -> None:
        old = await Relay.start(RUN_ID, ScriptedOwner(), LoopClock())
        old_secret = old.access.secret
        await close_bounded(old)
        new = await Relay.start(RUN_ID, ScriptedOwner(), LoopClock())
        try:
            client = await RawClient.connect(port_of(new))
            stale = RequestHead.standard(new).with_field("Authorization", f"Bearer {old_secret}")
            await client.send(stale.encode())
            assert (await client.answer()).status == HTTPStatus.UNAUTHORIZED
            assert await client.at_eof()
        finally:
            await close_bounded(new)

    asyncio.run(main())


def test_two_open_relays_each_refuse_the_other_secret_and_hand_their_own_to_their_owner() -> None:
    async def main() -> None:
        owners = {"run-a": ScriptedOwner(), "run-b": ScriptedOwner()}
        first = await Relay.start("run-a", owners["run-a"], LoopClock())
        second = await Relay.start("run-b", owners["run-b"], LoopClock())
        try:
            for own, foreign in ((first, second), (second, first)):
                client = await RawClient.connect(port_of(own))
                crossed = RequestHead.standard(own).with_field(
                    "Authorization", relay_bearer(foreign)
                )
                await client.send(crossed.encode())
                assert (await client.answer()).status == HTTPStatus.UNAUTHORIZED
                assert await client.at_eof()
                client = await RawClient.connect(port_of(own))
                await client.send(RequestHead.standard(own).encode())
                assert (await client.answer()).status == HTTPStatus.OK
            assert [len(owner.requests) for owner in owners.values()] == [1, 1]
        finally:
            await close_bounded(first)
            await close_bounded(second)

    asyncio.run(main())


def test_an_unexpected_connection_failure_logs_its_class_and_no_request_content(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    secrets_seen: list[str] = []
    real_drain = asyncio.StreamWriter.drain

    async def scenario(relay: Relay) -> None:
        secret = relay.access.secret
        secrets_seen.append(secret)
        port = port_of(relay)

        async def drain_failing_on_the_relay_side(writer: asyncio.StreamWriter) -> None:
            if writer.get_extra_info("sockname")[1] == port:
                raise OSError("injected")
            await real_drain(writer)

        monkeypatch.setattr(asyncio.StreamWriter, "drain", drain_failing_on_the_relay_side)
        client = await RawClient.connect(port)
        malformed = RequestHead.standard(relay).with_field(
            "Authorization", f"Bearer {secret}\x00{CANARY}"
        )
        await client.send(malformed.encode())
        assert await client.at_eof()
        await yield_until(lambda: relay_tasks() == [])

    drive(scenario)
    captured = capfd.readouterr()
    logged = caplog.text + captured.out + captured.err
    assert "OSError" in logged
    assert CANARY not in logged
    assert secrets_seen[0] not in logged


def pieces_of(data: bytes) -> list[bytes]:
    return [data[i : i + RECEIVE_CHUNK_BYTES] for i in range(0, len(data), RECEIVE_CHUNK_BYTES)]


async def send_in_pieces(client: RawClient, data: bytes) -> None:
    for piece in pieces_of(data):
        await client.send(piece)


async def assert_refused_and_closed(client: RawClient, status: int) -> None:
    answer = await client.answer()
    assert (answer.status, answer.body) == (status, b"")
    assert answer.headers["connection"] == "close"
    assert "content-type" not in answer.headers
    assert await client.at_eof()


def test_the_owner_gets_the_body_and_its_answer_is_written_as_given() -> None:
    body = b'{"jsonrpc":"2.0"}'
    owner = ScriptedOwner([OWNER_ANSWER])

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(post(relay, body))
        answer = await client.answer()
        assert answer.status == HTTPStatus.OK
        assert answer.headers["content-type"] == JSON_MEDIA_TYPE
        assert answer.headers["content-length"] == str(len(OWNER_BODY))
        assert answer.body == OWNER_BODY
        assert owner.bodies == [body]

    drive(scenario, owner=owner)


def test_an_acknowledgement_is_written_without_content_type_or_body() -> None:
    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(post(relay, b"{}"))
        answer = await client.answer()
        assert (answer.status, answer.body) == (HTTPStatus.ACCEPTED, b"")
        assert "content-type" not in answer.headers

    drive(scenario, owner=ScriptedOwner([ACKNOWLEDGEMENT]))


VERSION_FIELDS = [
    pytest.param((), None, id="C3"),
    pytest.param((("mcp-protocol-version", "2026-01-01"),), "2026-01-01", id="C4"),
    pytest.param((("MCP-Protocol-Version", "2026-01-01"),), "2026-01-01", id="C5"),
    pytest.param((("MCP-Protocol-Version", "a"), ("mcp-protocol-version", "b")), "a, b", id="C6"),
    pytest.param(
        (("mcp-protocol-version", "\u00e9"),), "\u00e9".encode().decode("iso-8859-1"), id="obs-text"
    ),
]


@pytest.mark.parametrize(("fields", "expected"), VERSION_FIELDS)
def test_the_owner_gets_the_protocol_version_field_as_received(
    fields: tuple[tuple[str, str], ...], expected: str | None
) -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay) -> None:
        head = RequestHead.standard(relay)
        for name, value in fields:
            head = head.adding(name, value)
        client = await RawClient.connect(port_of(relay))
        await client.send(head.encode())
        await client.answer()
        assert [request.protocol_version for request in owner.requests] == [expected]

    drive(scenario, owner=owner)


def test_a_body_that_is_not_json_reaches_the_owner_unparsed() -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(post(relay, b"\xff not json"))
        await client.answer()
        assert owner.bodies == [b"\xff not json"]

    drive(scenario, owner=owner)


BOUND_BODY_REQUESTS = [
    pytest.param(lambda r, body: post(r, body), id="C8"),
    pytest.param(lambda r, body: chunked_head(r).encode() + chunk(body) + b"0\r\n\r\n", id="C9"),
]


@pytest.mark.parametrize("build", BOUND_BODY_REQUESTS)
def test_a_body_of_exactly_the_bound_reaches_the_owner_by_length_and_chunked(
    build: Callable[[Relay, bytes], bytes],
) -> None:
    owner = ScriptedOwner()
    body = b"a" * MAX_EVENT_BYTES

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(build(relay, body))
        assert (await client.answer()).status == HTTPStatus.OK
        assert owner.bodies == [body]

    drive(scenario, owner=owner)


def test_an_empty_body_reaches_the_owner_and_the_next_request_is_answered() -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        for _ in range(2):
            await client.send(post(relay))
            assert (await client.answer()).status == HTTPStatus.OK
        assert owner.bodies == [b"", b""]

    drive(scenario, owner=owner)


def test_the_measured_cli_sequence_is_answered_on_one_connection() -> None:
    invalid = HttpAnswer(HTTPStatus.BAD_REQUEST, None, b"")
    owner = ScriptedOwner([invalid, OWNER_ANSWER, ACKNOWLEDGEMENT, OWNER_ANSWER])

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        requests = [
            post(relay, b"1"),
            post(relay, b"2"),
            post(relay, b"3"),
            RequestHead.standard(relay).replace_method("GET").encode(),
            post(relay, b"5"),
        ]
        statuses = []
        for raw in requests:
            await client.send(raw)
            answer = await client.answer()
            assert "connection" not in answer.headers
            statuses.append(answer.status)
        assert statuses == [400, 200, 202, 405, 200]
        assert owner.bodies == [b"1", b"2", b"3", b"5"]

    drive(scenario, owner=owner)


def test_two_requests_in_one_write_are_answered_in_order() -> None:
    owner = ScriptedOwner([ACKNOWLEDGEMENT, OWNER_ANSWER])

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(post(relay, b"first") + post(relay, b"second"))
        assert (await client.answer()).status == HTTPStatus.ACCEPTED
        assert (await client.answer()).status == HTTPStatus.OK
        assert owner.bodies == [b"first", b"second"]

    drive(scenario, owner=owner)


MUST_CLOSE_REQUESTS = [
    pytest.param(lambda r: RequestHead.standard(r).adding("Connection", "close"), id="C13"),
    pytest.param(lambda r: replace(RequestHead.standard(r), version="HTTP/1.0"), id="C14"),
]


@pytest.mark.parametrize("build", MUST_CLOSE_REQUESTS)
def test_an_answer_to_a_request_that_must_close_says_so_and_ends_the_connection(
    build: Callable[[Relay], RequestHead], caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(build(relay).encode())
        answer = await client.answer()
        assert (answer.status, answer.body) == (HTTPStatus.OK, OWNER_BODY)
        assert answer.headers["connection"] == "close"
        assert await client.at_eof()

    drive(scenario)
    assert warnings_logged(caplog) == []


def test_a_declared_size_over_the_bound_is_refused_before_a_body_byte() -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(RequestHead.standard(relay, MAX_EVENT_BYTES + 1).encode())
        await assert_refused_and_closed(client, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        assert owner.requests == []

    drive(scenario, owner=owner)


OVERSIZED_DECLARATIONS = [
    pytest.param(
        lambda r: RequestHead.standard(r, MAX_EVENT_BYTES + 1).with_field(
            "Authorization", "Bearer wrong"
        ),
        401,
        id="C16",
    ),
    pytest.param(
        lambda r: RequestHead.standard(r, MAX_EVENT_BYTES + 1).replace_target("/other"),
        404,
        id="C17",
    ),
]


@pytest.mark.parametrize(("build", "status"), OVERSIZED_DECLARATIONS)
def test_an_oversized_declaration_is_judged_by_the_earlier_gates_first(
    build: Callable[[Relay], RequestHead], status: int
) -> None:
    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(build(relay).encode())
        await assert_refused_and_closed(client, status)

    drive(scenario)


def test_a_chunked_body_crossing_the_bound_is_refused_before_its_end() -> None:
    owner = ScriptedOwner()
    small = b"a" * 1_024

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        crossing = chunk(small) * (MAX_EVENT_BYTES // len(small))
        last_byte = b"1\r\na"
        await client.send(chunked_head(relay).encode() + crossing + last_byte)
        await assert_refused_and_closed(client, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        assert owner.requests == []

    drive(scenario, owner=owner)


MALFORMED_TERMINATOR = b"2\r\n{}XX0\r\n\r\n"


def test_a_malformed_chunk_terminator_is_refused_with_400() -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(chunked_head(relay).encode() + MALFORMED_TERMINATOR)
        await assert_refused_and_closed(client, HTTPStatus.BAD_REQUEST)
        assert owner.requests == []

    drive(scenario, owner=owner)


def test_a_wrong_secret_decides_before_a_malformed_body_is_consumed() -> None:
    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        head = chunked_head(relay).with_field("Authorization", "Bearer wrong")
        await client.send(head.encode() + MALFORMED_TERMINATOR)
        assert (await client.answer()).status == HTTPStatus.UNAUTHORIZED
        assert await client.at_eof()

    drive(scenario)


UNFINISHED_BODY_EVENTS = [
    pytest.param(b"2;" + b"a" * (3 * RECEIVE_CHUNK_BYTES), id="C21"),
    pytest.param(b"0\r\nX-T: " + b"a" * (3 * RECEIVE_CHUNK_BYTES), id="C22"),
]


@pytest.mark.parametrize("unfinished", UNFINISHED_BODY_EVENTS)
def test_an_unfinished_body_event_over_the_head_limit_is_refused_with_431(
    unfinished: bytes,
) -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(chunked_head(relay).encode())
        await send_in_pieces(client, unfinished)
        await assert_refused_and_closed(client, HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE)
        assert owner.requests == []

    drive(scenario, owner=owner)


def test_an_unfinished_head_one_byte_over_the_head_limit_is_refused_with_431() -> None:
    owner = ScriptedOwner()
    unfinished = b"POST /mcp HTTP/1.1\r\nX-Pad: " + b"a" * (HEAD_LIMIT_BYTES + 1 - 27)

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await send_in_pieces(client, unfinished)
        await assert_refused_and_closed(client, HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE)
        assert owner.requests == []

    assert len(unfinished) == HEAD_LIMIT_BYTES + 1
    drive(scenario, owner=owner)


def test_an_owner_failure_answers_an_empty_500_with_one_record_of_its_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    owner = ScriptedOwner(failure=RuntimeError(CANARY))

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(post(relay, b"{}"))
        answer = await client.answer()
        assert (answer.status, answer.body) == (HTTPStatus.INTERNAL_SERVER_ERROR, b"")
        assert "connection" not in answer.headers
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1
        assert "RuntimeError" in errors[0].getMessage()
        assert CANARY not in caplog.text
        await client.send(post(relay))
        assert (await client.answer()).status == HTTPStatus.INTERNAL_SERVER_ERROR

    drive(scenario, owner=owner)


def test_an_owner_failure_on_a_closing_request_answers_500_and_closes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(RequestHead.standard(relay).adding("Connection", "close").encode())
        await assert_refused_and_closed(client, HTTPStatus.INTERNAL_SERVER_ERROR)

    drive(scenario, owner=ScriptedOwner(failure=RuntimeError("failing owner")))
    assert len(warnings_logged(caplog)) == 1


def mixed_framing_head(relay: Relay) -> RequestHead:
    return chunked_head(relay).adding("Content-Length", "2")


@pytest.mark.parametrize(
    ("owner", "status"),
    [
        pytest.param(ScriptedOwner, HTTPStatus.OK, id="answered"),
        pytest.param(
            lambda: ScriptedOwner(failure=RuntimeError("failing owner")),
            HTTPStatus.INTERNAL_SERVER_ERROR,
            id="failing",
        ),
    ],
)
def test_a_request_with_both_framings_is_answered_and_the_connection_closes(
    owner: Callable[[], ScriptedOwner], status: HTTPStatus
) -> None:
    scripted = owner()

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(
            mixed_framing_head(relay).encode() + b"2\r\n{}\r\n0\r\n\r\n" + post(relay, b"second")
        )
        answer = await client.answer()
        assert answer.status == status
        assert answer.headers["connection"] == "close"
        assert await client.at_eof()
        assert scripted.bodies == [b"{}"]

    drive(scenario, owner=scripted)


def test_a_chunked_only_request_keeps_its_connection() -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(
            chunked_head(relay).encode() + b"2\r\n{}\r\n0\r\n\r\n" + post(relay, b"second")
        )
        assert "connection" not in (await client.answer()).headers
        assert (await client.answer()).status == HTTPStatus.OK
        assert owner.bodies == [b"{}", b"second"]

    drive(scenario, owner=owner)


def oversized_declaration_with_canary(relay: Relay) -> bytes:
    return RequestHead.standard(relay, MAX_EVENT_BYTES + 1).encode() + CANARY.encode()


def oversized_chunks_with_canary(relay: Relay) -> bytes:
    filler = chunk(b"a" * 4_096) * (MAX_EVENT_BYTES // 4_096)
    return chunked_head(relay).encode() + chunk(CANARY.encode()) + filler + b"1\r\na"


def malformed_chunk_with_canary(relay: Relay) -> bytes:
    return chunked_head(relay).encode() + chunk(CANARY.encode())[:-2] + b"XX0\r\n\r\n"


def post_with_canary(relay: Relay) -> bytes:
    return post(relay, CANARY.encode())


LEAK_CASES = [
    pytest.param(oversized_declaration_with_canary, None, id="C25"),
    pytest.param(oversized_chunks_with_canary, None, id="C26"),
    pytest.param(malformed_chunk_with_canary, None, id="C27"),
    pytest.param(post_with_canary, RuntimeError(CANARY), id="C28"),
]


@pytest.mark.parametrize(("build", "failure"), LEAK_CASES)
def test_no_log_or_stream_carries_request_content_or_an_owner_message(
    build: HeadBuilder,
    failure: Exception | None,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    secrets_seen: list[str] = []

    async def scenario(relay: Relay) -> None:
        secrets_seen.append(relay.access.secret)
        client = await RawClient.connect(port_of(relay))
        await client.send(build(relay))
        await client.answer()

    drive(scenario, owner=ScriptedOwner(failure=failure))
    captured = capfd.readouterr()
    logged = caplog.text + captured.out + captured.err
    assert CANARY not in logged
    assert secrets_seen[0] not in logged


def test_a_client_leaving_mid_body_never_reaches_the_owner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    owner = ScriptedOwner()

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(RequestHead.standard(relay, 10).encode() + b"abc")
        await yield_until(lambda: len(relay_tasks()) == 1)
        client.disconnect()
        await yield_until(lambda: relay_tasks() == [])
        assert owner.requests == []

    drive(scenario, owner=owner)
    assert unlogged_beyond_asyncio_debug(caplog) == []


CONNECTION = f"relay:{RUN_ID}:conn-1"
WATCH = f"{CONNECTION}:watch"
ANSWER = f"{CONNECTION}:answer"
PARTIAL_NEXT_HEAD = b"POST /mcp HTTP/1.1\r\nHost: x\r\n"

CLIENT_ENDS = [
    pytest.param(RawClient.disconnect, False, id="fin"),
    pytest.param(RawClient.finish_sending, True, id="half-close"),
    pytest.param(RawClient.reset, False, id="reset"),
]


async def client_while_owner_works(
    relay: Relay, owner: ScriptedOwner, reads: RelayReads, pipelined: bytes = b""
) -> RawClient:
    client = await RawClient.connect(port_of(relay))
    await client.send(post(relay, b"{}"))
    await owner.wait_entered()
    baseline = reads.read_by(WATCH)
    await client.send(pipelined)
    await reads.until_read(WATCH, baseline + len(pipelined))
    return client


@pytest.mark.parametrize("pipelined", [b"", PARTIAL_NEXT_HEAD], ids=["alone", "after-bytes"])
@pytest.mark.parametrize(("end", "can_still_read"), CLIENT_ENDS)
def test_a_close_while_the_owner_works_cancels_it_and_writes_nothing(
    end: Callable[[RawClient], None],
    can_still_read: bool,
    pipelined: bytes,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """W1 a to c, W2."""
    caplog.set_level(logging.DEBUG)
    writes = RelayWrites(monkeypatch)
    reads = RelayReads(monkeypatch)
    owner = ScriptedOwner(gated=True)

    async def scenario(relay: Relay) -> None:
        client = await client_while_owner_works(relay, owner, reads, pipelined)
        end(client)
        await yield_until(lambda: owner.cancelled)
        await yield_until(lambda: relay_tasks() == [])
        assert writes.to(client) == b""
        if can_still_read:
            assert await client.delivery() is Delivery.ABSENT

    drive(scenario, owner=owner)
    assert unlogged_beyond_asyncio_debug(caplog) == []


def test_an_answer_produced_after_the_close_cancelled_the_owner_is_not_written(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """W1 d."""
    caplog.set_level(logging.DEBUG)
    writes = RelayWrites(monkeypatch)
    owner = AbsorbingOwner()

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(post(relay, b"{}"))
        await owner.wait_entered()
        client.disconnect()
        await yield_until(lambda: WATCH not in relay_tasks())
        owner.release()
        await yield_until(lambda: relay_tasks() == [])
        assert writes.to(client) == b""

    drive(scenario, owner=owner)
    assert unlogged_beyond_asyncio_debug(caplog) == []


def test_a_whole_next_request_sent_while_the_owner_works_is_answered_after_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W3."""
    reads = RelayReads(monkeypatch)
    owner = ScriptedOwner([ACKNOWLEDGEMENT, OWNER_ANSWER], gated=True)

    async def scenario(relay: Relay) -> None:
        client = await client_while_owner_works(relay, owner, reads, post(relay, b"{}"))
        owner.release()
        assert (await client.answer()).status == HTTPStatus.ACCEPTED
        assert (await client.answer()).status == HTTPStatus.OK
        assert owner.bodies == [b"{}", b"{}"]

    drive(scenario, owner=owner)


BoundSender = Callable[[Relay, ScriptedOwner, RelayReads], Awaitable[RawClient]]
PIPELINED_TAIL_BYTES = 4_096


async def send_exactly_the_bound(
    relay: Relay, owner: ScriptedOwner, reads: RelayReads
) -> RawClient:
    return await client_while_owner_works(relay, owner, reads, b"x" * MAX_PIPELINED_BYTES)


async def send_one_byte_over_the_bound(
    relay: Relay, owner: ScriptedOwner, reads: RelayReads
) -> RawClient:
    return await client_while_owner_works(relay, owner, reads, b"x" * (MAX_PIPELINED_BYTES + 1))


async def send_over_the_bound_after_bytes_read_before_the_owner(
    relay: Relay, owner: ScriptedOwner, reads: RelayReads
) -> RawClient:
    request = post(relay, b"{}")
    reads.aggregate_first_read(CONNECTION, len(request) + PIPELINED_TAIL_BYTES)
    client = await RawClient.connect(port_of(relay))
    await client.send(request + b"x" * PIPELINED_TAIL_BYTES)
    await owner.wait_entered()
    assert reads.read_by(CONNECTION) == len(request) + PIPELINED_TAIL_BYTES
    remainder = MAX_PIPELINED_BYTES - PIPELINED_TAIL_BYTES + 1
    await client.send(b"x" * remainder)
    await reads.until_read(WATCH, remainder)
    return client


async def send_over_the_bound_after_bytes_an_earlier_request_left(
    relay: Relay, owner: ScriptedOwner, reads: RelayReads
) -> RawClient:
    second = post(relay, b"{}")
    client = await client_while_owner_works(
        relay, owner, reads, second + b"x" * (MAX_PIPELINED_BYTES - len(second))
    )
    owner.release()
    owner.hold()
    first_answer = await client.answer()
    assert first_answer.status == HTTPStatus.ACCEPTED
    await yield_until(lambda: len(owner.requests) == 2)
    baseline = reads.read_by(WATCH)
    await client.send(b"x" * (len(second) + 1))
    await reads.until_read(WATCH, baseline + len(second) + 1)
    return client


BOUND_CASES = [
    pytest.param(send_exactly_the_bound, False, id="exactly-the-bound"),
    pytest.param(send_one_byte_over_the_bound, True, id="one-byte-over"),
    pytest.param(send_over_the_bound_after_bytes_read_before_the_owner, True, id="prebuffered"),
    pytest.param(send_over_the_bound_after_bytes_an_earlier_request_left, True, id="successive"),
]


@pytest.mark.parametrize(("send", "ends_the_request"), BOUND_CASES)
def test_pipelined_bytes_beyond_the_bound_over_everything_retained_end_the_request(
    send: BoundSender, ends_the_request: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W4."""
    reads = RelayReads(monkeypatch)
    owner = ScriptedOwner([ACKNOWLEDGEMENT, OWNER_ANSWER], gated=True)

    async def scenario(relay: Relay) -> None:
        client = await send(relay, owner, reads)
        if ends_the_request:
            await yield_until(lambda: owner.cancelled)
            await yield_until(lambda: relay_tasks() == [])
            assert await client.delivery() is Delivery.ABSENT
        else:
            owner.release()
            assert (await client.answer()).status == HTTPStatus.ACCEPTED

    drive(scenario, owner=owner)


def test_an_answer_that_completed_before_a_half_close_is_written_and_the_connection_ends() -> None:
    """W5 a."""
    owner = ScriptedOwner(gated=True)

    async def scenario(relay: Relay) -> None:
        loop = holding_loop()
        client = await RawClient.connect(port_of(relay))
        await client.send(post(relay, b"{}"))
        await owner.wait_entered()
        loop.hold_wakeups(CONNECTION)
        owner.release()
        await yield_until(lambda: ANSWER not in relay_tasks())
        client.finish_sending()
        await yield_until(lambda: WATCH not in relay_tasks())
        loop.release_wakeups()
        assert (await client.answer()).status == HTTPStatus.OK
        assert await client.at_eof()

    drive(scenario, owner=owner, loop_factory=HoldingSteppedLoop)


def test_pipelined_bytes_a_cancelled_watch_read_had_not_taken_stay_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W5 b."""
    reads = RelayReads(monkeypatch)
    owner = ScriptedOwner([ACKNOWLEDGEMENT, OWNER_ANSWER], gated=True)

    async def scenario(relay: Relay) -> None:
        loop = holding_loop()
        next_request = post(relay, b"{}")
        half = len(next_request) // 2
        client = await client_while_owner_works(relay, owner, reads, next_request[:half])
        loop.hold_wakeups(WATCH)
        await client.send(next_request[half:])
        await yield_until(lambda: loop.held_wakeups == 1)
        owner.release()
        await yield_until(lambda: task_named(WATCH).cancelling() > 0)
        loop.release_wakeups()
        assert (await client.answer()).status == HTTPStatus.ACCEPTED
        assert (await client.answer()).status == HTTPStatus.OK

    drive(scenario, owner=owner, loop_factory=HoldingSteppedLoop)


def test_an_answer_that_completed_before_an_overflow_is_written_and_no_next_request_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W5 c."""
    owner = ScriptedOwner([ACKNOWLEDGEMENT, OWNER_ANSWER], gated=True)

    async def scenario(relay: Relay) -> None:
        loop = holding_loop()
        client = await RawClient.connect(port_of(relay))
        await client.send(post(relay, b"{}"))
        await owner.wait_entered()
        loop.hold_wakeups(CONNECTION)
        owner.release()
        await yield_until(lambda: ANSWER not in relay_tasks())
        next_request = post(relay, b"{}")
        await client.send(next_request + b"x" * (MAX_PIPELINED_BYTES + 1 - len(next_request)))
        await yield_until(lambda: WATCH not in relay_tasks())
        loop.release_wakeups()
        assert (await client.answer()).status == HTTPStatus.ACCEPTED
        assert await client.delivery() is Delivery.ABSENT
        assert owner.bodies == [b"{}"]

    drive(scenario, owner=owner, loop_factory=HoldingSteppedLoop)


def test_an_unanswered_request_closes_its_connection_without_a_byte_and_frees_the_slot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C3."""
    caplog.set_level(logging.DEBUG, logger="agent_runtime_service")
    relay_writes = RelayWrites(monkeypatch)
    owner = ScriptedOwner([UNANSWERED])

    async def scenario(relay: Relay) -> None:
        clients = [await RawClient.connect(port_of(relay)) for _ in range(MAX_RELAY_CONNECTIONS)]
        await yield_until(lambda: len(relay_tasks()) == MAX_RELAY_CONNECTIONS)
        unanswered, *others = clients

        await unanswered.send(post(relay, b"{}"))

        assert await unanswered.at_eof()
        await yield_until(lambda: len(relay_tasks()) == MAX_RELAY_CONNECTIONS - 1)
        assert relay_writes.to(unanswered) == b""
        fifth = await RawClient.connect(port_of(relay))
        await fifth.send(post(relay, b"{}"))
        assert (await fifth.answer()).body == OWNER_BODY
        assert [relay_writes.to(other) for other in others] == [b""] * len(others)

    drive(scenario, owner=owner)
    assert [r for r in caplog.records if r.name.startswith("agent_runtime_service")] == []


def test_close_cancels_a_cooperating_owner_and_leaves_no_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = ScriptedOwner(gated=True)

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(post(relay, b"{}"))
        await owner.wait_entered()
        assert len(connection_numbers(relay_tasks())) == 1
        await close_bounded(relay)
        assert owner.cancelled
        assert await client.at_eof()
        assert relay_tasks() == []

    drive(scenario, owner=owner)
    assert warnings_logged(caplog) == []


def test_close_mid_body_never_reaches_the_owner_and_leaves_no_task() -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(post(relay, b"{}") + RequestHead.standard(relay, 10).encode() + b"abc")
        assert (await client.answer()).status == HTTPStatus.OK
        await close_bounded(relay)
        assert await client.at_eof()
        assert owner.bodies == [b"{}"]
        assert relay_tasks() == []

    drive(scenario, owner=owner)


def test_signal_handlers_stay_unchanged_while_a_request_is_in_the_owner() -> None:
    def handlers() -> dict[signal.Signals, object]:
        return {sig: signal.getsignal(sig) for sig in SIGNALS}

    async def main() -> None:
        owner = ScriptedOwner(gated=True)
        before = handlers()
        relay = await Relay.start(RUN_ID, owner, LoopClock())
        try:
            client = await RawClient.connect(port_of(relay))
            await client.send(post(relay, b"{}"))
            await owner.wait_entered()
            assert handlers() == before
        finally:
            owner.release()
            await close_bounded(relay)

    asyncio.run(main())
