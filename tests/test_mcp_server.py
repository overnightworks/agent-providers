"""The ASGI MCP server: head checks in order, the profile's messages, and the exit check.

Every case drives the app through a raw ASGI scope, so duplicate headers and odd paths reach it
as written. Each refusal case also asserts that no handler ran.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pytest
from mcp_server_support import (
    ISSUED_AT,
    LIFETIME,
    OTHER_RUN_ID,
    RUN_ID,
    VERIFICATION_KEY,
    RecordingHandlers,
    declaration,
    minted_capability,
    signature_segment_of,
)
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from agent_client.mcp_server import HandlerFailureWithheld, McpServer, McpTool
from agent_client.mcp_wire import ToolOutcome
from agent_client.tools import ToolEffect
from agent_client.wire import MAX_EVENT_BYTES

ORIGIN = "https://notes.internal"
HOST = b"notes.internal"
VERSION = "2025-11-25"
LOGGER = "agent_client.mcp_server"
INVALID_PARAMS = {"code": -32602, "message": "Invalid params"}
Headers = list[tuple[bytes, bytes]]


@dataclass
class Reply:
    started: bool
    status: int | None
    headers: Headers
    body: bytes

    def header(self, name: str) -> list[str]:
        return [v.decode() for k, v in self.headers if k == name.encode()]

    def json(self) -> Any:
        return json.loads(self.body)


async def drive_scope(app: Any, scope: dict[str, Any], body: bytes = b"") -> Reply:
    sent: list[dict[str, Any]] = []
    inbox = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict[str, Any]:
        return inbox.pop(0) if inbox else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    try:
        await app(scope, receive, send)
    finally:
        started = [m for m in sent if m["type"] == "http.response.start"]
    reply_body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Reply(
        bool(started),
        started[0]["status"] if started else None,
        started[0]["headers"] if started else [],
        reply_body,
    )


def request_headers(
    token: str | None = "", *, host: Iterable[bytes] = (HOST,), version: str | None = VERSION
) -> Headers:
    headers = [(b"host", value) for value in host]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if version is not None:
        headers.append((b"mcp-protocol-version", version.encode()))
    return headers


def rpc(method: str, params: dict[str, Any] | None = None, request_id: Any = 1) -> bytes:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode()


def call_params(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def http_scope(
    headers: Headers, *, path: str = "/mcp", method: str = "POST", query: bytes = b""
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "headers": headers,
    }


@dataclass
class Fixture:
    handlers: RecordingHandlers
    server: McpServer
    token: str
    host_calls: list[str]

    def front(self) -> Any:
        async def host_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            self.host_calls.append(scope["type"])

        return self.server.in_front_of(host_app)

    def post(self, body: bytes, **options: Any) -> Reply:
        headers = request_headers(options.pop("token", self.token), **options.pop("fields", {}))
        return asyncio.run(drive_scope(self.server, http_scope(headers, **options), body))

    def request(self, headers: Headers, **options: Any) -> Reply:
        body = options.pop("body", rpc("ping"))
        return asyncio.run(drive_scope(self.server, http_scope(headers, **options), body))


@pytest.fixture
def handlers() -> RecordingHandlers:
    return RecordingHandlers()


@pytest.fixture
def served(handlers: RecordingHandlers) -> Fixture:
    tools = [
        handlers.tool("read_note"),
        handlers.tool("edit_note", ToolEffect.SIDE_EFFECTING),
        handlers.tool("other_tool"),
    ]
    server = McpServer(ORIGIN, VERIFICATION_KEY, tools, wall_clock=lambda: ISSUED_AT + 1)
    return Fixture(handlers, server, minted_capability(ORIGIN).token, [])


def test_initialize_negotiates_the_one_version_with_no_session_and_initialized_is_accepted(
    served: Fixture,
) -> None:
    reply = served.post(
        rpc("initialize", {"protocolVersion": "2024-11-05"}), fields={"version": None}
    )

    assert reply.status == 200
    assert reply.header("content-type") == ["application/json"]
    assert reply.json()["result"]["protocolVersion"] == VERSION
    assert reply.header("mcp-session-id") == []
    acknowledged = served.post(rpc("notifications/initialized", request_id=None))
    assert (acknowledged.status, acknowledged.body) == (202, b"")


def test_tools_list_gives_declared_tools_the_capability_allows_in_declaration_order(
    served: Fixture,
) -> None:
    reversed_capability = minted_capability(ORIGIN, tools=("edit_note", "read_note"))

    reply = served.post(rpc("tools/list"), token=reversed_capability.token)
    listed = reply.json()["result"]["tools"]

    assert [tool["name"] for tool in listed] == ["read_note", "edit_note"]
    assert set(listed[0]) == {"name", "description", "inputSchema"}
    assert listed[0]["inputSchema"]["type"] == "object"


def test_an_allowed_call_hands_the_handler_the_verified_claims_and_the_arguments(
    served: Fixture,
) -> None:
    reply = served.post(rpc("tools/call", call_params("read_note", note="n1")))

    assert reply.json()["result"]["content"] == [{"type": "text", "text": "done"}]
    [(name, claims, arguments)] = served.handlers.calls
    assert (name, claims.sub, arguments) == ("read_note", str(RUN_ID), {"note": "n1"})


def test_interleaved_calls_of_two_runs_each_get_the_claims_of_their_own_request(
    served: Fixture,
) -> None:
    seen: list[tuple[str, str]] = []

    async def handler(claims: Any, arguments: Any) -> ToolOutcome:
        await asyncio.sleep(0)
        seen.append((arguments["tag"], claims.sub))
        return ToolOutcome("ok", is_error=False)

    server = McpServer(
        ORIGIN,
        VERIFICATION_KEY,
        [McpTool(declaration("read_note"), handler)],
        wall_clock=lambda: ISSUED_AT + 1,
    )
    runs = {"first": RUN_ID, "second": OTHER_RUN_ID}

    async def both() -> None:
        await asyncio.gather(
            *(
                drive_scope(
                    server,
                    http_scope(request_headers(minted_capability(ORIGIN, run_id=run).token)),
                    rpc("tools/call", call_params("read_note", tag=tag)),
                )
                for tag, run in runs.items()
            )
        )

    asyncio.run(both())

    assert sorted(seen) == sorted((tag, str(run)) for tag, run in runs.items())


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(call_params("other_tool"), id="declared-but-not-allowed"),
        pytest.param(call_params("ghost_tool"), id="allowed-but-not-declared"),
        pytest.param({"arguments": {}}, id="no-name"),
        pytest.param({"name": 5}, id="name-of-the-wrong-type"),
    ],
)
def test_a_call_outside_the_allowlist_or_with_bad_params_is_invalid_params_and_runs_nothing(
    served: Fixture, params: dict[str, Any]
) -> None:
    reply = served.post(rpc("tools/call", params))

    assert reply.status == 200
    assert reply.json()["error"] == INVALID_PARAMS
    assert served.handlers.calls == []


@pytest.mark.parametrize(
    "authorizations",
    [
        pytest.param([], id="none"),
        pytest.param(["Bearer {token}", "Bearer {token}"], id="two"),
        pytest.param(["Basic {token}"], id="basic"),
        pytest.param(["bearer {token}"], id="lower-case-bearer"),
    ],
)
def test_authorization_other_than_exactly_one_bearer_is_a_401_challenge(
    served: Fixture, authorizations: list[str]
) -> None:
    headers = request_headers(None) + [
        (b"authorization", text.format(token=served.token).encode()) for text in authorizations
    ]

    reply = served.request(headers)

    assert (reply.status, reply.body) == (401, b"")
    assert reply.header("www-authenticate") == ["Bearer"]
    assert served.handlers.calls == []


@pytest.mark.parametrize(
    ("capability", "reason"),
    [
        pytest.param(minted_capability(ORIGIN, issued_at=ISSUED_AT - 2 * LIFETIME), "expired"),
        pytest.param(minted_capability("https://other.internal"), "wrong_audience"),
    ],
)
def test_a_refused_capability_is_a_401_and_one_warning_that_names_only_the_reason(
    served: Fixture, capability: Any, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)

    reply = served.post(rpc("ping"), token=capability.token)

    assert (reply.status, reply.body) == (401, b"")
    [record] = caplog.records
    assert record.getMessage().endswith(reason)
    assert signature_segment_of(capability) not in caplog.text + repr(reply.headers)
    assert served.handlers.calls == []


@pytest.mark.parametrize(
    "host",
    [
        pytest.param([], id="absent"),
        pytest.param([b"other.internal"], id="another-host"),
        pytest.param([b"notes.internal:443"], id="default-port-written"),
        pytest.param([HOST, HOST], id="two"),
    ],
)
def test_a_host_that_is_not_exactly_the_origin_authority_is_forbidden(
    served: Fixture, host: list[bytes]
) -> None:
    reply = served.post(rpc("ping"), fields={"host": host})

    assert (reply.status, reply.body) == (403, b"")
    assert served.handlers.calls == []


@pytest.mark.parametrize("origin", [ORIGIN, "https://evil.example"])
def test_any_origin_header_is_forbidden_even_the_apps_own(served: Fixture, origin: str) -> None:
    headers = request_headers(served.token) + [(b"origin", origin.encode())]

    reply = served.request(headers)

    assert (reply.status, reply.body) == (403, b"")
    assert served.handlers.calls == []


def test_the_apps_authority_with_a_port_is_matched_exactly() -> None:
    server = McpServer(
        "https://notes.internal:8443", VERIFICATION_KEY, [], wall_clock=lambda: ISSUED_AT + 1
    )
    token = minted_capability("https://notes.internal:8443").token
    scope = http_scope(request_headers(token, host=[b"notes.internal:8443"]))

    reply = asyncio.run(drive_scope(server, scope, rpc("ping")))

    assert reply.status == 200


def host_app() -> Starlette:
    async def answer(request: Any) -> PlainTextResponse:
        return PlainTextResponse("host answer")

    return Starlette(routes=[Route("/", answer), Route("/mcp", answer), Route("/mcpx", answer)])


@pytest.mark.parametrize(
    ("path", "query", "extra", "status"),
    [
        pytest.param("/mcp/", b"", [], 404, id="trailing-slash"),
        pytest.param("/mcp/x", b"", [], 404, id="below"),
        pytest.param("/mcp", b"x=1", [], 404, id="query"),
        pytest.param("/mcp/", b"", [(b"origin", b"https://x.example")], 403, id="slash-origin"),
    ],
)
def test_the_helper_owns_the_prefix_and_never_redirects(
    served: Fixture, path: str, query: bytes, extra: Headers, status: int
) -> None:
    app = served.server.in_front_of(host_app())
    scope = http_scope(request_headers(served.token) + extra, path=path, query=query)

    reply = asyncio.run(drive_scope(app, scope, rpc("ping")))

    assert (reply.status, reply.body) == (status, b"")


def test_the_prefix_without_authorization_is_401_and_other_paths_are_the_hosts() -> None:
    server = McpServer(ORIGIN, VERIFICATION_KEY, [])
    app = server.in_front_of(host_app())
    unauthorized = asyncio.run(drive_scope(app, http_scope(request_headers(None), path="/mcp/")))
    unauthenticated = request_headers(None)
    hosts = asyncio.run(drive_scope(app, http_scope(unauthenticated, path="/", method="GET")))
    beside = asyncio.run(drive_scope(app, http_scope(unauthenticated, path="/mcpx", method="GET")))
    premise = asyncio.run(
        drive_scope(host_app(), http_scope(request_headers(None), path="/mcp/", method="GET"))
    )

    assert unauthorized.status == 401
    assert (hosts.status, hosts.body) == (200, b"host answer")
    assert (beside.status, beside.body) == (200, b"host answer")
    assert premise.status == 307


@pytest.mark.parametrize(
    ("extra", "status"),
    [
        pytest.param([(b"Origin", b"https://x.example")], 403, id="origin"),
        pytest.param([(b"HOST", HOST)], 403, id="second-host"),
        pytest.param([(b"Authorization", b"Bearer {token}")], 401, id="second-authorization"),
    ],
)
def test_header_names_are_matched_without_regard_to_case(
    served: Fixture, extra: Headers, status: int
) -> None:
    written = [(name, value.replace(b"{token}", served.token.encode())) for name, value in extra]

    reply = served.request(request_headers(served.token) + written)

    assert (reply.status, reply.body) == (status, b"")
    assert served.handlers.calls == []


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_other_methods_get_405_at_the_endpoint_and_404_below_it(
    served: Fixture, method: str
) -> None:
    at_endpoint = served.request(request_headers(served.token), method=method)
    below = served.request(request_headers(served.token), method=method, path="/mcp/")

    assert (at_endpoint.status, at_endpoint.body) == (405, b"")
    assert at_endpoint.header("allow") == ["POST"]
    assert below.status == 404


def test_a_body_over_the_bound_is_413_and_a_call_without_a_version_is_400(
    served: Fixture,
) -> None:
    oversized = served.post(b"x" * (MAX_EVENT_BYTES + 1))
    unversioned = served.post(rpc("tools/call", call_params("read_note")), fields={"version": None})

    assert (oversized.status, oversized.body) == (413, b"")
    assert (unversioned.status, unversioned.body) == (400, b"")
    assert served.handlers.calls == []


def test_a_handler_failure_without_the_token_leaves_the_app_before_any_response(
    served: Fixture,
) -> None:
    served.handlers.failure = RuntimeError("boom")
    sent: list[Any] = []

    async def scenario() -> None:
        scope = http_scope(request_headers(served.token))

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": rpc("tools/call", call_params("read_note"))}

        async def send(message: Any) -> None:
            sent.append(message)

        await served.server(scope, receive, send)

    running = scenario()
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(running)
    assert sent == []


def test_a_repeated_tool_name_or_a_non_canonical_origin_is_refused_at_construction(
    handlers: RecordingHandlers,
) -> None:
    repeated = [handlers.tool("read_note"), handlers.tool("read_note")]
    with pytest.raises(ValueError):
        McpServer(ORIGIN, VERIFICATION_KEY, repeated)
    with pytest.raises(ValueError):
        McpServer("https://Notes.internal/", VERIFICATION_KEY, [])


def test_lifespan_is_a_type_error_on_the_bare_server_and_the_hosts_through_the_helper(
    served: Fixture,
) -> None:
    async def lifespan(app: Any) -> None:
        await app({"type": "lifespan"}, None, None)

    bare = lifespan(served.server)
    with pytest.raises(TypeError):
        asyncio.run(bare)
    asyncio.run(lifespan(served.front()))

    assert served.host_calls == ["lifespan"]


@pytest.mark.parametrize(
    ("fields", "headers_extra", "path", "method", "body_size", "status"),
    [
        pytest.param(
            {"host": []}, [(b"origin", b"x")], "/mcp", "POST", 0, 403, id="host-then-origin"
        ),
        pytest.param(
            {"token": None},
            [(b"origin", b"x")],
            "/mcp",
            "POST",
            0,
            403,
            id="origin-then-authorization",
        ),
        pytest.param({"token": None}, [], "/nowhere", "POST", 0, 401, id="authorization-then-path"),
        pytest.param({}, [], "/nowhere", "GET", 0, 404, id="path-then-method"),
        pytest.param({}, [], "/mcp", "GET", MAX_EVENT_BYTES + 1, 405, id="method-then-size"),
    ],
)
def test_two_failures_at_once_answer_with_the_earlier_check(
    served: Fixture,
    fields: dict[str, Any],
    headers_extra: Headers,
    path: str,
    method: str,
    body_size: int,
    status: int,
) -> None:
    token = fields.pop("token", served.token)
    headers = request_headers(token, **fields) + headers_extra

    reply = served.request(headers, path=path, method=method, body=b"x" * body_size)

    assert (reply.status, reply.body) == (status, b"")
    assert served.handlers.calls == []


def reflected_ping(segment: str) -> bytes:
    return rpc("ping", request_id=segment)


def withheld_cases() -> list[Any]:
    return [
        pytest.param(lambda s: reflected_ping(s), "ping", id="ping-id"),
        pytest.param(
            lambda s: rpc("tools/call", call_params("other_tool"), request_id=s),
            "tools/call other_tool",
            id="refused-call-id",
        ),
        pytest.param(
            lambda s: rpc("tools/call", call_params(f"evil-{s}"), request_id=s),
            "tools/call unknown_tool",
            id="unregistered-tool-name",
        ),
        pytest.param(lambda s: rpc(s, request_id=s), "unknown_method", id="id-and-unknown-method"),
        pytest.param(
            lambda s: rpc("tools/call", {"name": [s]}, request_id=s),
            "tools/call unknown_tool",
            id="list-tool-name",
        ),
        pytest.param(
            lambda s: rpc("tools/call", {"name": {"a": 1}}, request_id=s),
            "tools/call unknown_tool",
            id="object-tool-name",
        ),
    ]


@pytest.mark.parametrize(("body_of", "label"), withheld_cases())
def test_an_answer_that_echoes_the_signature_is_an_empty_500_with_a_closed_label(
    served: Fixture, body_of: Any, label: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    segment = served.token.rsplit(".", 1)[-1]

    reply = served.post(body_of(segment))

    assert (reply.status, reply.body) == (500, b"")
    [record] = caplog.records
    assert record.getMessage().endswith(label)
    assert segment not in caplog.text


def test_a_handler_result_that_carries_the_signature_is_an_empty_500(
    served: Fixture, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    segment = served.token.rsplit(".", 1)[-1]
    served.handlers.outcome = ToolOutcome(f"the token is {segment}", is_error=False)

    reply = served.post(rpc("tools/call", call_params("read_note")))

    assert (reply.status, reply.body) == (500, b"")
    [record] = caplog.records
    assert record.getMessage().endswith("tools/call read_note")
    assert segment not in caplog.text


def test_a_malformed_bearer_with_an_empty_signature_is_the_empty_401_not_a_500(
    served: Fixture,
) -> None:
    reply = served.post(rpc("ping"), token="aaaa.bbbb.")

    assert (reply.status, reply.body) == (401, b"")


def failures_carrying(segment: str) -> list[Exception]:
    in_message = RuntimeError(f"failed with {segment}")
    try:
        raise RuntimeError("outer") from ValueError(segment)
    except RuntimeError as in_cause:
        return [in_message, in_cause, ValueError("\ud800" + segment)]


@pytest.mark.parametrize("which", [0, 1, 2], ids=["message", "cause", "lone-surrogate"])
def test_a_handler_failure_carrying_the_signature_is_withheld_without_a_chain(
    served: Fixture, which: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    segment = served.token.rsplit(".", 1)[-1]
    served.handlers.failure = failures_carrying(segment)[which]

    call = rpc("tools/call", call_params("read_note"))
    with pytest.raises(HandlerFailureWithheld) as raised:
        served.post(call)

    withheld = raised.value
    assert withheld.tool_name == "read_note"
    assert (withheld.__cause__, withheld.__context__) == (None, None)
    assert segment not in "".join(traceback.format_exception(withheld))
    assert segment not in caplog.text
    assert len(caplog.records) == 1


def test_the_default_clock_is_the_real_one_so_a_capability_long_expired_is_refused(
    handlers: RecordingHandlers,
) -> None:
    server = McpServer(ORIGIN, VERIFICATION_KEY, [handlers.tool("read_note")])
    token = minted_capability(ORIGIN).token

    reply = asyncio.run(drive_scope(server, http_scope(request_headers(token)), rpc("ping")))

    assert reply.status == 401


@pytest.mark.parametrize(
    "outcome_of",
    [
        pytest.param(lambda s: ToolOutcome(s, is_error=s), id="non-bool-is-error"),
        pytest.param(lambda s: ToolOutcome("\ud800" + s, is_error=False), id="lone-surrogate"),
    ],
)
def test_an_outcome_that_cannot_be_written_is_treated_as_a_handler_failure(
    served: Fixture, outcome_of: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER)
    segment = served.token.rsplit(".", 1)[-1]
    served.handlers.outcome = outcome_of(segment)

    message = rpc("tools/call", call_params("read_note"))
    with pytest.raises(HandlerFailureWithheld) as raised:
        served.post(message)

    assert (raised.value.__cause__, raised.value.__context__) == (None, None)
    assert segment not in "".join(traceback.format_exception(raised.value)) + caplog.text


@pytest.mark.parametrize(
    ("path", "method", "body_size", "status"),
    [
        pytest.param("/nowhere", "POST", 0, 404, id="not-found"),
        pytest.param("/mcp", "GET", 0, 405, id="method"),
        pytest.param("/mcp", "POST", MAX_EVENT_BYTES + 1, 413, id="too-large"),
    ],
)
def test_every_answer_after_verification_leaves_through_the_exit_check(
    served: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    method: str,
    body_size: int,
    status: int,
) -> None:
    monkeypatch.setattr("agent_client.mcp_server.app.carries_signature_of", lambda *_: True)
    unverified = served.request(request_headers(None), path=path, method=method)

    reply = served.request(
        request_headers(served.token), path=path, method=method, body=b"x" * body_size
    )

    assert unverified.status == 401
    assert (reply.status, reply.body) == (500, b"")
