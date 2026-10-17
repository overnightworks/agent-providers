"""The upstream MCP client reaches an app over verified TLS and speaks only the JSON-only profile.

Every scenario drives ``open_app_tools`` against ``tests/mcp_app_fake.py``, a
server on real TLS sockets with a test CA generated per session. The client
never reads from the network without an external deadline here.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack
from dataclasses import replace
from typing import Any

import httpx
import pytest
from mcp_app_fake import (
    ACKNOWLEDGED,
    CERTIFICATE_HOST,
    WAIT_SECONDS,
    FakeResponse,
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
from agent_service.app_tools import (
    AppToolsSession,
    ProtocolFailure,
    UpstreamProtocolError,
    UpstreamRefused,
    open_app_tools,
)
from agent_service.mcp_wire import NON_TEXT_BLOCK_MESSAGE, WireRefusal

CAPABILITY = "capability-for-this-run"
VERSION = "2025-11-25"
CLIENT_DEADLINE_SECONDS = 30


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
        ) -> McpAppFake:
            return stack.enter_context(serving(responder or ScriptedApp(), served_credentials))

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
        assert request.headers["authorization"] == f"Bearer {CAPABILITY}"
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
    assert not fake.tail_released.is_set()


@pytest.mark.parametrize("encoding", ["gzip", "br", "identity, gzip"])
def test_a_content_encoded_answer_is_refused_before_its_body_is_read(serve, context, encoding):
    encoded = withheld(FakeResponse(body=b"{}", headers={"Content-Encoding": encoding}))
    fake = serve(answering("tools/list", encoded))

    with pytest.raises(UpstreamProtocolError) as refused:
        list_tool_names(fake, context)

    assert refused.value.reason == ProtocolFailure.CONTENT_ENCODING
    assert not fake.tail_released.is_set()


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
    assert call.headers["authorization"] == f"Bearer {CAPABILITY}"


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
    ],
    ids=["200-json", "202-with-body", "204", "200-empty"],
)
def test_a_notification_answered_by_anything_but_an_empty_202_is_a_protocol_error(
    serve, context, response
):
    fake = serve(answering("notifications/initialized", response))

    with pytest.raises(UpstreamProtocolError) as refused:
        list_tool_names(fake, context)

    assert refused.value.reason == WireRefusal.NOT_ACKNOWLEDGED


def test_an_empty_202_acknowledges_the_notification(serve, context):
    fake = serve(answering("notifications/initialized", ACKNOWLEDGED))

    assert list_tool_names(fake, context) == ["echo"]


@pytest.mark.parametrize("rpc_method", ["initialize", "notifications/initialized", "tools/list"])
@pytest.mark.parametrize("status", [401, 307])
def test_a_non_2xx_status_is_a_refusal_carrying_only_the_status(serve, context, rpc_method, status):
    fake = serve(answering(rpc_method, FakeResponse(status=status, content_type=None)))

    with pytest.raises(UpstreamRefused) as refused:
        list_tool_names(fake, context)

    assert refused.value.status == status


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
    assert not fake.tail_released.is_set()


def test_a_refused_status_wins_over_an_encoded_body_that_is_still_withheld(serve, context):
    unauthorized = withheld(
        FakeResponse(status=401, body=b"{}", headers={"Content-Encoding": "gzip"})
    )
    fake = serve(answering("initialize", unauthorized))

    with pytest.raises(UpstreamRefused) as refused:
        list_tool_names(fake, context)

    assert refused.value.status == 401
    assert not fake.tail_released.is_set()


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

    with pytest.raises(httpx.ConnectError):
        list_tool_names(fake, untrusting)

    assert fake.requests == []
