"""The upstream MCP client reaches an app over verified TLS and speaks only the JSON-only profile.

Every scenario drives ``open_app_tools`` against ``tests/mcp_app_fake.py``, a
server on real TLS sockets with a test CA generated per session. The client
never reads from the network without an external deadline here.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import ExitStack, closing, nullcontext
from dataclasses import replace
from typing import Any

import httpx
import pytest
from capability_support import capability_carrying
from mcp_app_fake import (
    ACKNOWLEDGED,
    CERTIFICATE_HOST,
    DROPPED,
    WAIT_SECONDS,
    FakeResponse,
    Fate,
    McpAppFake,
    RecordedRequest,
    ScriptedApp,
    TestAuthority,
    TlsCredentials,
    rpc_error,
    rpc_result,
    serving,
    tool_listing,
)

from agent_client.wire import MAX_EVENT_BYTES
from agent_service import app_tools
from agent_service.app_tools import (
    AppToolsSession,
    ProtocolFailure,
    UpstreamCallLost,
    UpstreamError,
    UpstreamProtocolError,
    UpstreamRedirected,
    UpstreamRefused,
    UpstreamUnreachable,
    open_app_tools,
)
from agent_service.mcp_wire import NON_TEXT_BLOCK_MESSAGE, WireRefusal

CAPABILITY = capability_carrying("capability-for-this-run")
CANARY_TOKEN = CAPABILITY.token
VERSION = "2025-11-25"
CLIENT_DEADLINE_SECONDS = 30
CANARY_HEADER = "canary-response-header"
CANARY_BODY = "canary-response-body"
SHORT_IO_SECONDS = 0.3
STALL_DEADLINE_SECONDS = 5


@pytest.fixture(scope="session")
def authority(tmp_path_factory: pytest.TempPathFactory) -> TestAuthority:
    return TestAuthority(tmp_path_factory.mktemp("fake-app-ca"))


@pytest.fixture
def context(authority: TestAuthority) -> ssl.SSLContext:
    return authority.client_context()


@pytest.fixture
def serve(authority: TestAuthority) -> Iterator[Callable[..., McpAppFake]]:
    credentials = authority.credentials_for(CERTIFICATE_HOST)
    with ExitStack() as stack:

        def start(
            responder: Callable[[RecordedRequest], FakeResponse] | None = None,
            served_credentials: TlsCredentials = credentials,
            keep_alive: bool = False,
        ) -> McpAppFake:
            return stack.enter_context(
                serving(responder or ScriptedApp(), served_credentials, keep_alive=keep_alive)
            )

        yield start


class OpenedClients:
    def __init__(self) -> None:
        self.clients: list[httpx.AsyncClient] = []


@pytest.fixture
def opened_clients(monkeypatch: pytest.MonkeyPatch) -> OpenedClients:
    opened = OpenedClients()

    class RecordingClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            opened.clients.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", RecordingClient)
    return opened


def in_session[Outcome](
    fake: McpAppFake,
    context: ssl.SSLContext,
    action: Callable[[AppToolsSession], Awaitable[Outcome]],
) -> Outcome:
    async def scenario() -> Outcome:
        async with asyncio.timeout(CLIENT_DEADLINE_SECONDS):
            async with open_app_tools(fake.origin, CAPABILITY, context) as session:
                return await action(session)

    return asyncio.run(scenario())


def list_tool_names(fake: McpAppFake, context: ssl.SSLContext) -> list[str]:
    async def names(session: AppToolsSession) -> list[str]:
        return [tool.name for tool in await session.list_tools()]

    return in_session(fake, context, names)


def list_then_call_echo(fake: McpAppFake, context: ssl.SSLContext) -> Any:
    async def both(session: AppToolsSession) -> Any:
        await session.list_tools()
        return await session.call_tool("echo", {"word": "hi"})

    return in_session(fake, context, both)


def call_echo(fake: McpAppFake, context: ssl.SSLContext) -> Any:
    return in_session(fake, context, lambda session: session.call_tool("echo", {"word": "hi"}))


def requests_for(fake: McpAppFake, rpc_method: str) -> list[RecordedRequest]:
    return [request for request in fake.requests if request.rpc_method == rpc_method]


def answering(rpc_method: str, response: FakeResponse) -> ScriptedApp:
    return ScriptedApp(overrides={rpc_method: lambda _request: response})


def withheld(response: FakeResponse) -> FakeResponse:
    return replace(response, body=b"", held_back=response.body)


def json_reply(request: RecordedRequest, **members: Any) -> FakeResponse:
    reply = {"jsonrpc": "2.0", "id": request.message["id"], **members}
    return FakeResponse(body=json.dumps(reply).encode())


def assert_closed(error: BaseException) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    visible = f"{error!s} {error!r} {error.args!r} {vars(error)!r}"
    for canary in (CANARY_TOKEN, CANARY_HEADER, CANARY_BODY):
        assert canary not in visible
    carried = [*error.args, *vars(error).values()]
    assert not any(type(value).__module__.startswith("httpx") for value in carried)


def failure_of[Outcome](
    expected: type[UpstreamError], action: Callable[[], Outcome]
) -> UpstreamError:
    with pytest.raises(expected) as raised:
        action()
    assert_closed(raised.value)
    return raised.value


def assert_only_posts(fake: McpAppFake) -> None:
    assert {request.method for request in fake.requests} <= {"POST"}
    assert all("last-event-id" not in request.headers for request in fake.requests)


def listing_of_size(request: RecordedRequest, size: int) -> FakeResponse:
    listing = tool_listing(["padded"])
    listing["tools"][0]["description"] = ""
    padding = size - len(rpc_result(request, listing).body)
    listing["tools"][0]["description"] = "x" * padding
    sized = rpc_result(request, listing)
    assert len(sized.body) == size
    return sized


def test_discovery_over_tls_speaks_the_profile_in_order(serve, context):
    fake = serve()

    assert list_tool_names(fake, context) == ["echo"]

    assert [(r.method, r.path, r.rpc_method) for r in fake.requests] == [
        ("POST", "/mcp", "initialize"),
        ("POST", "/mcp", "notifications/initialized"),
        ("POST", "/mcp", "tools/list"),
    ]
    for request in fake.requests:
        assert request.headers["authorization"] == f"Bearer {CANARY_TOKEN}"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept"] == "application/json, text/event-stream"
        assert request.headers["accept-encoding"] == "identity"
    assert set(fake.requests[0].message["params"]) == {
        "protocolVersion",
        "capabilities",
        "clientInfo",
    }


@pytest.mark.parametrize(
    ("rpc_method", "response", "failure"),
    [
        ("initialize", FakeResponse(status=500, content_type=None), UpstreamRefused),
        ("initialize", None, UpstreamProtocolError),
        ("notifications/initialized", FakeResponse(body=b"{}"), UpstreamProtocolError),
    ],
    ids=["initialize-refused", "version-refused", "initialized-not-acknowledged"],
)
def test_a_failed_handshake_leaves_no_client_open(
    serve, context, opened_clients, rpc_method, response, failure
):
    app = (
        ScriptedApp(protocol_version="1999-01-01")
        if response is None
        else answering(rpc_method, response)
    )
    fake = serve(app)

    with pytest.raises(failure):
        list_tool_names(fake, context)

    assert opened_clients.clients
    assert all(client.is_closed for client in opened_clients.clients)


TEN_PAGES = 10


def test_pagination_returns_every_page_in_order_with_each_cursor_echoed(serve, context):
    last = TEN_PAGES - 1
    pages = [
        tool_listing([f"tool-{index}"], None if index == last else f"page-{index + 1}")
        for index in range(TEN_PAGES)
    ]
    fake = serve(ScriptedApp(pages=pages))

    names = list_tool_names(fake, context)

    assert names == [f"tool-{index}" for index in range(TEN_PAGES)]
    cursors = [r.message["params"].get("cursor") for r in requests_for(fake, "tools/list")]
    assert cursors == [None, *[f"page-{index}" for index in range(1, TEN_PAGES)]]


def test_a_further_cursor_on_the_last_allowed_page_is_refused_and_no_more_is_requested(
    serve, context
):
    pages = [tool_listing([f"tool-{index}"], f"page-{index + 1}") for index in range(TEN_PAGES + 1)]
    fake = serve(ScriptedApp(pages=pages))

    with pytest.raises(UpstreamProtocolError) as refused:
        list_tool_names(fake, context)

    assert refused.value.reason == ProtocolFailure.PAGE_BOUND
    assert len(requests_for(fake, "tools/list")) == TEN_PAGES


def test_an_empty_string_cursor_is_followed_as_a_cursor(serve, context):
    def two_pages(request: RecordedRequest) -> FakeResponse:
        cursor = request.message.get("params", {}).get("cursor")
        if cursor is None:
            return rpc_result(request, tool_listing(["first"], next_cursor=""))
        return rpc_result(request, tool_listing(["second"]))

    fake = serve(ScriptedApp(overrides={"tools/list": two_pages}))

    assert list_tool_names(fake, context) == ["first", "second"]


@pytest.mark.parametrize(
    ("size", "refusal"),
    [(MAX_EVENT_BYTES, None), (MAX_EVENT_BYTES + 1, WireRefusal.TOO_LARGE)],
    ids=["at-the-bound", "one-byte-over"],
)
def test_an_answer_is_accepted_at_the_byte_bound_and_refused_one_byte_over(
    serve, context, size, refusal
):
    def sized(request: RecordedRequest) -> FakeResponse:
        if refusal is None:
            return listing_of_size(request, size)
        return FakeResponse(body=b" " * size)

    fake = serve(ScriptedApp(overrides={"tools/list": sized}))

    if refusal is None:
        assert list_tool_names(fake, context) == ["padded"]
        return
    with pytest.raises(UpstreamProtocolError) as refused:
        list_tool_names(fake, context)
    assert refused.value.reason == refusal


def test_an_oversized_answer_is_refused_before_its_withheld_tail_arrives(serve, context):
    oversized = FakeResponse(body=b" " * (MAX_EVENT_BYTES + 1), held_back=b"tail")
    fake = serve(answering("tools/list", oversized))

    with pytest.raises(UpstreamProtocolError) as refused:
        list_tool_names(fake, context)

    assert refused.value.reason == WireRefusal.TOO_LARGE


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
@pytest.mark.parametrize("encoding", ["gzip", "br", "identity, gzip"])
def test_a_content_encoded_answer_is_refused_before_its_body_is_read(
    serve, context, encoding, content_type
):
    encoded = withheld(
        FakeResponse(content_type=content_type, body=b"{}", headers={"Content-Encoding": encoding})
    )
    fake = serve(answering("tools/list", encoded))

    with pytest.raises(UpstreamProtocolError) as refused:
        list_tool_names(fake, context)

    assert refused.value.reason == ProtocolFailure.CONTENT_ENCODING


def test_an_identity_encoded_answer_is_accepted(serve, context):
    def identity(request: RecordedRequest) -> FakeResponse:
        listed = rpc_result(request, tool_listing(["echo"]))
        return replace(listed, headers={"Content-Encoding": "identity"})

    fake = serve(ScriptedApp(overrides={"tools/list": identity}))

    assert list_tool_names(fake, context) == ["echo"]


def test_a_call_returns_the_joined_text_and_is_posted_once(serve, context):
    def two_blocks(request: RecordedRequest) -> FakeResponse:
        blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        return rpc_result(request, {"content": blocks})

    fake = serve(ScriptedApp(overrides={"tools/call": two_blocks}))

    outcome = call_echo(fake, context)

    assert (outcome.content, outcome.is_error) == ("a\nb", False)
    (call,) = requests_for(fake, "tools/call")
    assert call.message["params"] == {"name": "echo", "arguments": {"word": "hi"}}
    assert call.headers["authorization"] == f"Bearer {CANARY_TOKEN}"


def test_concurrent_calls_answered_in_reverse_order_each_get_their_own_answer(serve, context):
    slow_arrived = threading.Event()
    fast_answered = threading.Event()

    def slow_waits_for_fast(request: RecordedRequest) -> FakeResponse:
        name = request.message["params"]["name"]
        if name == "slow":
            slow_arrived.set()
            assert fast_answered.wait(WAIT_SECONDS)
        return rpc_result(request, {"content": [{"type": "text", "text": name}]})

    fake = serve(ScriptedApp(overrides={"tools/call": slow_waits_for_fast}))

    completed: list[str] = []

    async def named(session: AppToolsSession, name: str):
        outcome = await session.call_tool(name, {})
        completed.append(name)
        return outcome

    async def both(session: AppToolsSession):
        slow = asyncio.create_task(named(session, "slow"))
        assert await asyncio.to_thread(slow_arrived.wait, WAIT_SECONDS)
        fast = await named(session, "fast")
        fast_answered.set()
        return [await slow, fast]

    outcomes = in_session(fake, context, both)

    assert completed == ["fast", "slow"]
    assert [outcome.content for outcome in outcomes] == ["slow", "fast"]
    ids = [call.message["id"] for call in requests_for(fake, "tools/call")]
    assert len(set(ids)) == 2


@pytest.mark.parametrize(
    "members",
    [{"result": {"content": []}}, {"error": {"code": -32000, "message": "no"}}],
    ids=["result", "error"],
)
def test_an_answer_with_another_requests_id_is_refused(serve, context, members):
    def other_id(request: RecordedRequest) -> FakeResponse:
        reply = {"jsonrpc": "2.0", "id": request.message["id"] + 1000, **members}
        return FakeResponse(body=json.dumps(reply).encode())

    fake = serve(ScriptedApp(overrides={"tools/call": other_id}))

    with pytest.raises(UpstreamProtocolError) as refused:
        call_echo(fake, context)

    assert refused.value.reason == ProtocolFailure.ID_MISMATCH


@pytest.mark.parametrize(
    ("result", "content", "is_error"),
    [
        ({"content": [{"type": "text", "text": "boom"}], "isError": True}, "boom", True),
        ({"content": [{"type": "image", "data": "x"}]}, NON_TEXT_BLOCK_MESSAGE, True),
    ],
    ids=["is-error-flag", "non-text-block"],
)
def test_a_tool_error_comes_back_as_an_error_outcome(serve, context, result, content, is_error):
    fake = serve(ScriptedApp(overrides={"tools/call": lambda request: rpc_result(request, result)}))

    outcome = call_echo(fake, context)

    assert (outcome.content, outcome.is_error) == (content, is_error)


def test_a_json_rpc_error_carries_its_code_and_never_its_message(serve, context):
    secret_message = "internal detail that must not travel"
    fake = serve(
        ScriptedApp(overrides={"tools/call": lambda r: rpc_error(r, -32602, secret_message)})
    )

    with pytest.raises(UpstreamProtocolError) as refused:
        call_echo(fake, context)

    assert refused.value.reason == ProtocolFailure.JSON_RPC_ERROR
    assert refused.value.rpc_error_code == -32602
    assert secret_message not in f"{refused.value!s} {refused.value!r}"


def test_only_initialize_goes_without_a_protocol_version_header(serve, context):
    fake = serve()

    list_then_call_echo(fake, context)

    versions = {r.rpc_method: r.headers.get("mcp-protocol-version") for r in fake.requests}
    assert versions == {
        "initialize": None,
        "notifications/initialized": VERSION,
        "tools/list": VERSION,
        "tools/call": VERSION,
    }


def test_a_server_answering_initialize_with_another_version_gets_no_further_request(serve, context):
    fake = serve(ScriptedApp(protocol_version="1999-01-01"))

    with pytest.raises(UpstreamProtocolError) as refused:
        list_tool_names(fake, context)

    assert refused.value.reason == WireRefusal.UNSUPPORTED_VERSION
    assert [r.rpc_method for r in fake.requests] == ["initialize"]


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(content_type="application/json", body=b"{}"),
        FakeResponse(status=202, content_type=None, body=b"x"),
        FakeResponse(status=204, content_type=None),
        FakeResponse(status=200, content_type=None),
        withheld(FakeResponse(content_type="text/event-stream", body=b"data: {}\n\n")),
        withheld(FakeResponse(body=b"{}", headers={"Content-Encoding": "gzip"})),
    ],
    ids=["200-json", "202-with-body", "204", "200-empty", "200-event-stream-withheld", "200-gzip"],
)
def test_a_notification_answered_by_anything_but_an_empty_202_is_a_protocol_error(
    serve, context, response
):
    fake = serve(answering("notifications/initialized", response))

    refused = failure_of(UpstreamProtocolError, lambda: list_tool_names(fake, context))

    assert refused.reason == WireRefusal.NOT_ACKNOWLEDGED
    assert_only_posts(fake)
    assert fake.requests[-1].rpc_method == "notifications/initialized"


def test_an_empty_202_marked_with_a_content_encoding_is_refused(serve, context):
    gzip_acknowledgement = replace(ACKNOWLEDGED, headers={"Content-Encoding": "gzip"})
    fake = serve(answering("notifications/initialized", gzip_acknowledgement))

    refused = failure_of(UpstreamProtocolError, lambda: list_tool_names(fake, context))

    assert refused.reason == ProtocolFailure.CONTENT_ENCODING


def test_an_event_stream_answer_to_a_request_is_refused_before_its_open_stream_ends(serve, context):
    stream = withheld(FakeResponse(content_type="text/event-stream", body=b"data: {}\n\n"))
    fake = serve(answering("tools/list", stream))

    refused = failure_of(UpstreamProtocolError, lambda: list_tool_names(fake, context))

    assert refused.reason == WireRefusal.NOT_JSON_ANSWER


def test_an_empty_202_acknowledges_the_notification(serve, context):
    fake = serve(answering("notifications/initialized", ACKNOWLEDGED))

    assert list_tool_names(fake, context) == ["echo"]


HANDSHAKE_AND_LISTING = ["initialize", "notifications/initialized", "tools/list"]


@pytest.mark.parametrize("rpc_method", HANDSHAKE_AND_LISTING)
def test_a_non_2xx_status_is_a_refusal_carrying_only_the_status(serve, context, rpc_method):
    fake = serve(answering(rpc_method, FakeResponse(status=401, content_type=None)))

    refused = failure_of(UpstreamRefused, lambda: list_tool_names(fake, context))

    assert refused.status == 401


@pytest.mark.parametrize("rpc_method", HANDSHAKE_AND_LISTING)
@pytest.mark.parametrize("status", [300, 302, 304, 307, 308])
@pytest.mark.parametrize("target_kind", ["other-origin", "unparseable"])
def test_any_3xx_is_a_redirect_whose_cross_origin_target_is_never_asked(
    serve, context, rpc_method, status, target_kind
):
    target = serve()
    unparseable = "https://[::1"
    location = {
        "Location": f"{target.origin}/mcp" if target_kind == "other-origin" else unparseable
    }
    redirect = FakeResponse(status, None, CANARY_BODY.encode(), location)
    fake = serve(answering(rpc_method, withheld(redirect)))

    refused = failure_of(UpstreamRedirected, lambda: list_tool_names(fake, context))

    assert refused.status == status
    assert target.requests == []
    assert target.accepted_connections == 0
    assert requests_for(fake, rpc_method) == [fake.requests[-1]]


def test_a_same_origin_redirect_is_not_followed(serve, context):
    def back_to_itself(_request: RecordedRequest) -> FakeResponse:
        return FakeResponse(307, None, headers={"Location": f"{fake.origin}/mcp"})

    fake = serve(ScriptedApp(overrides={"tools/list": back_to_itself}))

    refused = failure_of(UpstreamRedirected, lambda: list_tool_names(fake, context))

    assert refused.status == 307
    assert len(requests_for(fake, "tools/list")) == 1


@pytest.mark.parametrize(
    "created",
    [
        FakeResponse(status=201, body=b"{}"),
        withheld(FakeResponse(status=201, body=b"{}", headers={"Content-Encoding": "gzip"})),
        withheld(FakeResponse(status=201, body=b"{}")),
    ],
    ids=["plain", "gzip-withheld", "body-withheld"],
)
def test_a_request_answered_with_a_2xx_other_than_200_is_refused_before_its_body_is_read(
    serve, context, created
):
    fake = serve(answering("initialize", created))

    with pytest.raises(UpstreamProtocolError) as refused:
        list_tool_names(fake, context)

    assert refused.value.reason == ProtocolFailure.UNEXPECTED_STATUS


def test_a_refused_status_wins_over_an_encoded_body_that_is_still_withheld(serve, context):
    unauthorized = withheld(
        FakeResponse(status=401, body=b"{}", headers={"Content-Encoding": "gzip"})
    )
    fake = serve(answering("initialize", unauthorized))

    with pytest.raises(UpstreamRefused) as refused:
        list_tool_names(fake, context)

    assert refused.value.status == 401


def test_an_issued_session_id_is_echoed_on_every_later_request(serve, context):
    fake = serve(ScriptedApp(session_id="session-1"))

    list_then_call_echo(fake, context)

    sent = {r.rpc_method: r.headers.get("mcp-session-id") for r in fake.requests[1:]}
    assert sent == {
        "notifications/initialized": "session-1",
        "tools/list": "session-1",
        "tools/call": "session-1",
    }


def test_no_session_id_is_sent_when_the_app_issues_none(serve, context):
    fake = serve()

    list_then_call_echo(fake, context)

    assert all("mcp-session-id" not in r.headers for r in fake.requests)


def test_a_session_id_outside_visible_ascii_is_refused_without_echoing_it(serve, context):
    canary = "capability-canary"
    fake = serve(ScriptedApp(session_id=f"{canary}\xe9"))

    with pytest.raises(UpstreamProtocolError) as refused:
        list_tool_names(fake, context)

    assert refused.value.reason == WireRefusal.INVALID_SESSION_ID
    assert refused.value.__cause__ is None
    assert refused.value.__context__ is None
    assert canary not in f"{refused.value!s} {refused.value!r}"
    assert [r.rpc_method for r in fake.requests] == ["initialize"]


def test_a_context_trusting_the_test_ca_reaches_the_app(serve, context):
    fake = serve()

    assert list_tool_names(fake, context) == ["echo"]


@pytest.mark.parametrize("wrong", ["default-bundle", "wrong-hostname"])
def test_a_context_that_does_not_verify_the_app_never_reaches_it(serve, authority, context, wrong):
    if wrong == "default-bundle":
        fake, untrusting = serve(), ssl.create_default_context()
    else:
        fake = serve(served_credentials=authority.credentials_for("other.test"))
        untrusting = context

    failure_of(UpstreamUnreachable, lambda: list_tool_names(fake, untrusting))

    assert fake.requests == []
    assert fake.accepted_connections == 1


def test_an_origin_nothing_listens_on_is_unreachable(context):
    with closing(socket.socket()) as unused:
        unused.bind(("127.0.0.1", 0))
        origin = f"https://{CERTIFICATE_HOST}:{unused.getsockname()[1]}"

    async def connect() -> None:
        async with asyncio.timeout(STALL_DEADLINE_SECONDS):
            async with open_app_tools(origin, CAPABILITY, context):
                pass

    failure_of(UpstreamUnreachable, lambda: asyncio.run(connect()))


def test_a_listener_that_never_accepts_stalls_the_tls_handshake_until_the_connect_deadline(
    monkeypatch, context, opened_clients
):
    monkeypatch.setattr(app_tools, "UPSTREAM_IO_SECONDS", SHORT_IO_SECONDS)

    async def connect(origin: str) -> None:
        async with asyncio.timeout(STALL_DEADLINE_SECONDS):
            async with open_app_tools(origin, CAPABILITY, context):
                pass

    with closing(socket.socket()) as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        origin = f"https://{CERTIFICATE_HOST}:{silent.getsockname()[1]}"
        failure_of(UpstreamUnreachable, lambda: asyncio.run(connect(origin)))

    assert all(client.is_closed for client in opened_clients.clients)


def test_the_client_is_built_without_redirects_environment_or_a_read_deadline(
    serve, context, opened_clients
):
    list_tool_names(serve(), context)

    (client,) = opened_clients.clients
    io = app_tools.UPSTREAM_IO_SECONDS
    assert client.follow_redirects is False
    assert client.trust_env is False
    assert client.timeout == httpx.Timeout(connect=io, read=None, write=io, pool=io)


@pytest.mark.parametrize("session_id", [None, "session-1"])
def test_a_full_conversation_sends_only_posts(serve, context, session_id):
    pages = [tool_listing(["a"], "page-1"), tool_listing(["b"])]
    fake = serve(ScriptedApp(pages=pages, session_id=session_id))

    list_then_call_echo(fake, context)

    assert [request.rpc_method for request in fake.requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/list",
        "tools/call",
    ]
    assert_only_posts(fake)


@pytest.mark.parametrize(
    "refusal",
    [
        FakeResponse(status=307, content_type=None),
        withheld(FakeResponse(content_type="text/event-stream", body=b"data: {}\n\n")),
    ],
    ids=["redirect", "event-stream"],
)
def test_nothing_is_sent_after_a_refused_listing(serve, context, refusal):
    fake = serve(answering("tools/list", refusal))

    failure_of(UpstreamError, lambda: list_tool_names(fake, context))

    assert_only_posts(fake)
    assert fake.requests[-1].rpc_method == "tools/list"


@pytest.mark.parametrize(
    ("rpc_method", "action"),
    [("initialize", list_tool_names), ("tools/call", call_echo)],
)
def test_a_500_is_a_refusal_whose_body_is_never_read(serve, context, rpc_method, action):
    error = FakeResponse(500, None, CANARY_BODY.encode(), {"X-Canary": CANARY_HEADER})
    fake = serve(answering(rpc_method, withheld(error)))

    refused = failure_of(UpstreamRefused, lambda: action(fake, context))

    assert refused.status == 500


LOST_PARTS = [
    FakeResponse(body=b'{"jsonrpc": "2.0", ', held_back=b"x" * 64, fate=Fate.CUT_MID_BODY),
    DROPPED,
]


@pytest.mark.parametrize("lost", LOST_PARTS, ids=["cut-mid-body", "dropped"])
@pytest.mark.parametrize(
    ("rpc_method", "action"), [("tools/call", call_echo), ("tools/list", list_tool_names)]
)
def test_a_connection_lost_after_the_request_ends_the_call_without_a_second_post(
    serve, context, opened_clients, rpc_method, action, lost
):
    executions: list[RecordedRequest] = []

    def execute_then_lose(request: RecordedRequest) -> FakeResponse:
        executions.append(request)
        return lost

    fake = serve(ScriptedApp(overrides={rpc_method: execute_then_lose}))

    failure_of(UpstreamCallLost, lambda: action(fake, context))

    assert len(executions) == 1
    assert len(requests_for(fake, rpc_method)) == 1
    assert fake.requests[-1].rpc_method == rpc_method
    assert all(client.is_closed for client in opened_clients.clients)


def test_a_call_lost_on_a_reused_connection_is_not_sent_again(serve, context):
    executions: list[RecordedRequest] = []

    def execute_then_lose(request: RecordedRequest) -> FakeResponse:
        executions.append(request)
        return DROPPED

    fake = serve(ScriptedApp(overrides={"tools/call": execute_then_lose}), keep_alive=True)

    failure_of(UpstreamCallLost, lambda: call_echo(fake, context))

    assert fake.accepted_connections == 1
    assert len({request.connection for request in fake.requests}) == 1
    assert len(executions) == 1
    assert fake.requests[-1].rpc_method == "tools/call"


class BrokenStream(httpx.AsyncByteStream):
    def __init__(self, failure: httpx.HTTPError | None, during_body: bool) -> None:
        self._failure = failure
        self._during_body = during_body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._failure is not None and self._during_body:
            raise self._failure
        yield b""

    async def aclose(self) -> None:
        if self._failure is not None and not self._during_body:
            raise self._failure


class FailingTransport(httpx.AsyncBaseTransport):
    def __init__(self, failure: httpx.HTTPError, phase: str) -> None:
        self._failure = failure
        self._phase = phase

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._phase == "before-headers":
            raise self._failure
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=BrokenStream(self._failure, during_body=self._phase == "during-body"),
        )


class FutureTransportFailure(httpx.HTTPError):
    pass


NEVER_SENT = [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
POSSIBLY_PROCESSED = [
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.CloseError,
    httpx.RemoteProtocolError,
    httpx.LocalProtocolError,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.DecodingError,
    httpx.TooManyRedirects,
    httpx.HTTPStatusError,
    FutureTransportFailure,
]


def failure_carrying_the_request(failure_type: type[httpx.HTTPError]) -> httpx.HTTPError:
    request = httpx.Request(
        "POST", "https://localhost/mcp", headers={"Authorization": f"Bearer {CANARY_TOKEN}"}
    )
    if failure_type is httpx.HTTPStatusError:
        response = httpx.Response(500, text=CANARY_BODY, request=request)
        failure = httpx.HTTPStatusError(CANARY_BODY, request=request, response=response)
    elif failure_type is FutureTransportFailure:
        failure = FutureTransportFailure(CANARY_BODY)
        failure.request = request
    else:
        failure = failure_type(CANARY_BODY, request=request)
    failure.__cause__ = ValueError(CANARY_HEADER)
    return failure


@pytest.mark.parametrize("phase", ["before-headers", "during-body", "while-closing"])
@pytest.mark.parametrize(
    ("failure_type", "mapped"),
    [(kind, UpstreamUnreachable) for kind in NEVER_SENT]
    + [(kind, UpstreamCallLost) for kind in POSSIBLY_PROCESSED],
)
def test_every_httpx_failure_leaves_as_one_closed_error_by_whether_bytes_may_have_been_sent(
    monkeypatch, context, failure_type, mapped, phase
):
    transport = FailingTransport(failure_carrying_the_request(failure_type), phase)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: transport)

    async def connect() -> None:
        async with open_app_tools("https://localhost:1", CAPABILITY, context):
            pass

    error = failure_of(UpstreamError, lambda: asyncio.run(connect()))

    assert type(error) is mapped


class ClosingFailsTransport(httpx.AsyncBaseTransport):
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self._status = status
        self._headers = headers
        self.close_attempts = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self

        class FailingClose(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b""

            async def aclose(self) -> None:
                transport.close_attempts += 1
                raise failure_carrying_the_request(httpx.CloseError)

        return httpx.Response(self._status, headers=self._headers, stream=FailingClose())


@pytest.mark.parametrize(
    ("status", "headers", "refusal"),
    [
        (307, {"Location": "https://[::1"}, UpstreamRedirected),
        (401, {}, UpstreamRefused),
        (201, {}, UpstreamProtocolError),
        (200, {"Content-Type": "text/event-stream"}, UpstreamProtocolError),
        (
            200,
            {"Content-Type": "application/json", "Content-Encoding": "gzip"},
            UpstreamProtocolError,
        ),
    ],
    ids=["redirect", "refused-status", "unexpected-2xx", "event-stream", "content-encoding"],
)
def test_an_established_refusal_wins_over_a_failure_to_close_the_refused_response(
    monkeypatch, context, status, headers, refusal
):
    transport = ClosingFailsTransport(status, headers)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: transport)

    async def connect() -> None:
        async with open_app_tools("https://localhost:1", CAPABILITY, context):
            pass

    failure_of(refusal, lambda: asyncio.run(connect()))

    assert transport.close_attempts >= 1


def test_nothing_the_client_holds_shows_the_token_in_its_repr(serve, context, opened_clients):
    fake = serve()

    async def reprs(session: AppToolsSession) -> list[str]:
        (client,) = opened_clients.clients
        held = [session, session._conversation, client.auth, CAPABILITY]
        return [repr(thing) for thing in held]

    assert all(CANARY_TOKEN not in text for text in in_session(fake, context, reprs))


def text_locals_of_client_frames(error: BaseException) -> list[str]:
    texts: list[str] = []
    trace = error.__traceback__
    while trace is not None:
        if trace.tb_frame.f_globals["__name__"] == app_tools.__name__:
            for local in trace.tb_frame.f_locals.values():
                members = (
                    local.values()
                    if isinstance(local, dict)
                    else local
                    if isinstance(local, tuple)
                    else [local]
                )
                texts.extend(member for member in members if isinstance(member, str))
        trace = trace.tb_next
    return texts


def refused_at(rpc_method: str, status: int, headers: dict[str, str] | None = None):
    response = FakeResponse(status=status, content_type=None, headers=headers or {})
    return lambda serve: serve(answering(rpc_method, response)).origin


def unused_origin(_serve) -> str:
    with closing(socket.socket()) as unused:
        unused.bind(("127.0.0.1", 0))
        return f"https://{CERTIFICATE_HOST}:{unused.getsockname()[1]}"


def lost_call(serve) -> str:
    return serve(answering("tools/list", DROPPED)).origin


@pytest.mark.parametrize(
    ("expected", "origin_of"),
    [
        (UpstreamRefused, refused_at("initialize", 500)),
        (UpstreamRedirected, refused_at("initialize", 307, {"Location": "https://other.example/"})),
        (UpstreamProtocolError, refused_at("initialize", 200)),
        (UpstreamUnreachable, unused_origin),
        (UpstreamCallLost, lost_call),
    ],
    ids=["refused", "redirected", "protocol-error", "unreachable", "call-lost"],
)
def test_no_client_frame_of_a_closed_error_holds_the_token_as_text(
    serve, context, expected, origin_of
):
    origin = origin_of(serve)

    async def discover() -> None:
        async with asyncio.timeout(STALL_DEADLINE_SECONDS):
            async with open_app_tools(origin, CAPABILITY, context) as session:
                await session.list_tools()

    with pytest.raises(expected) as raised:
        asyncio.run(discover())

    assert CANARY_TOKEN not in text_locals_of_client_frames(raised.value)


def close_failing_with_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_close(_transport: httpx.AsyncHTTPTransport) -> None:
        raise failure_carrying_the_request(httpx.CloseError)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "aclose", failing_close)


def test_a_transport_close_failure_after_a_completed_run_leaves_as_a_closed_error(
    serve, context, monkeypatch
):
    fake = serve()
    close_failing_with_the_request(monkeypatch)

    failure_of(UpstreamCallLost, lambda: list_tool_names(fake, context))


class HostFailure(Exception):
    pass


def exception_in_context_chain(error: BaseException | None, kind: type[BaseException]) -> bool:
    while error is not None:
        if isinstance(error, kind):
            return True
        error = error.__context__
    return False


@pytest.mark.parametrize(
    ("app", "leaving", "raised_in_body"),
    [
        (
            answering("initialize", FakeResponse(status=401, content_type=None)),
            UpstreamRefused,
            None,
        ),
        (ScriptedApp(), HostFailure, HostFailure("host")),
        (ScriptedApp(), asyncio.CancelledError, asyncio.CancelledError()),
    ],
    ids=["refusal", "host-exception", "cancellation"],
)
def test_an_exception_already_leaving_wins_over_a_close_failure(
    serve, context, monkeypatch, app, leaving, raised_in_body
):
    fake = serve(app)
    close_failing_with_the_request(monkeypatch)

    async def connect() -> None:
        async with open_app_tools(fake.origin, CAPABILITY, context):
            if raised_in_body is not None:
                raise raised_in_body

    with pytest.raises(leaving) as raised:
        asyncio.run(connect())

    assert not exception_in_context_chain(raised.value, httpx.CloseError)


@pytest.mark.parametrize(
    ("app", "outcome", "close_fails"),
    [
        (ScriptedApp(), None, False),
        (
            answering("tools/list", FakeResponse(status=500, content_type=None)),
            UpstreamRefused,
            False,
        ),
        (ScriptedApp(), UpstreamCallLost, True),
    ],
    ids=["completed-run", "closed-error-run", "failing-exit"],
)
def test_a_run_sends_post_only_and_never_ends_the_session_itself(
    serve, context, monkeypatch, app, outcome, close_fails
):
    fake = serve(app)
    if close_fails:
        close_failing_with_the_request(monkeypatch)

    with pytest.raises(outcome) if outcome else nullcontext():
        list_tool_names(fake, context)

    assert {request.method for request in fake.requests} == {"POST"}
