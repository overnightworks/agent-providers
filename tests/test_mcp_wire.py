"""The JSON-only MCP wire profile (decision record 0001, HD1): pure rules, driven directly."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any

import pytest

from agent_runtime_client.capability import MCP_ENDPOINT_PATH
from agent_runtime_client.mcp_wire import (
    ACKNOWLEDGEMENT,
    NON_TEXT_BLOCK_MESSAGE,
    SUPPORTED_PROTOCOL_VERSIONS,
    Answered,
    Call,
    CallToolParams,
    HttpAnswer,
    InitializeParams,
    JsonRpcError,
    JsonRpcErrorBody,
    JsonRpcErrorCode,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    Listing,
    ListToolsParams,
    ListToolsResult,
    McpWireError,
    Notified,
    ProtocolVersion,
    ToolDefinition,
    ToolOutcome,
    Unoffered,
    WireRefusal,
    answer_for,
    decide_server_message,
    encode_message,
    error_response,
    initialize_response,
    negotiate_protocol_version,
    parse_message,
    protocol_header_status,
    read_answer,
    read_payload,
    read_strict_json,
    read_tool_outcome,
    request_headers,
    require_acknowledgement,
    require_json_answer,
    require_session_id,
    require_supported_version,
    tool_result_response,
    tools_list_response,
)
from agent_runtime_client.wire import MAX_EVENT_BYTES, MAX_TOOL_INPUT_DEPTH

SECRET = "capability-token-must-not-leak"
NEGOTIATED = ProtocolVersion.V2025_11_25
REQUEST = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {"cursor": "c1"}}
NOTIFICATION = {"jsonrpc": "2.0", "method": "notifications/initialized"}
RESPONSE = {"jsonrpc": "2.0", "id": "a", "result": {"tools": []}}
ERROR = {"jsonrpc": "2.0", "id": None, "error": {"code": -32601, "message": "method not found"}}


def body_of(document: object) -> bytes:
    return json.dumps(document).encode()


def nested_in_an_array(depth: int) -> Any:
    value: Any = 0
    for _ in range(depth):
        value = [value]
    return value


def refusal_of(action: Any, *arguments: Any) -> McpWireError:
    with pytest.raises(McpWireError) as caught:
        action(*arguments)
    return caught.value


def test_the_endpoint_path_is_the_one_the_design_names() -> None:
    assert MCP_ENDPOINT_PATH == "/mcp"


@pytest.mark.parametrize(
    ("document", "expected_type"),
    [
        (REQUEST, JsonRpcRequest),
        (NOTIFICATION, JsonRpcNotification),
        (RESPONSE, JsonRpcResponse),
        (ERROR, JsonRpcError),
        ({"jsonrpc": "2.0", "id": 1, "method": "server/discover"}, JsonRpcRequest),
    ],
    ids=["request", "notification", "response", "error", "unserved method"],
)
def test_a_message_parses_to_its_kind_and_encodes_to_the_same_document(
    document: dict[str, Any], expected_type: type
) -> None:
    message = parse_message(body_of(document))

    assert type(message) is expected_type
    assert json.loads(encode_message(message)) == document


@pytest.mark.parametrize(
    "document",
    [
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "extra": 1},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "result": []},
        {"id": 1, "method": "ping"},
        {"id": 1, "result": {}},
        {"jsonrpc": 2.0, "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": ERROR["error"]},
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": None},
        {"jsonrpc": "2.0", "id": 1, "result": {"n": "\ud800"}},
        {"jsonrpc": "2.0", "id": "\ud800", "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": "\ud800"},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "ping",
            "params": {"p": nested_in_an_array(MAX_TOOL_INPUT_DEPTH)},
        },
        "ping",
        3,
    ],
    ids=[
        "version",
        "boolean id",
        "extra member",
        "no method",
        "result not object",
        "no jsonrpc request",
        "no jsonrpc response",
        "jsonrpc not text",
        "float id",
        "result and error",
        "null params",
        "lone surrogate in result",
        "lone surrogate in id",
        "lone surrogate in method",
        "params too deep",
        "text",
        "number",
    ],
)
def test_a_document_that_is_no_message_is_refused(document: object) -> None:
    refusal = refusal_of(parse_message, body_of(document))

    assert refusal.refusal is WireRefusal.NOT_A_MESSAGE


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("2025-11-25", NEGOTIATED),
        ("2025-06-18", NEGOTIATED),
        ("2026-07-28", NEGOTIATED),
        ("", NEGOTIATED),
    ],
)
def test_a_version_outside_the_set_negotiates_to_the_one_supported_version(
    requested: str, expected: ProtocolVersion
) -> None:
    assert negotiate_protocol_version(requested) is expected
    assert SUPPORTED_PROTOCOL_VERSIONS == {NEGOTIATED}


def test_a_client_refuses_a_server_version_outside_the_set() -> None:
    assert require_supported_version("2025-11-25") is NEGOTIATED
    assert (
        refusal_of(require_supported_version, "2025-06-18").refusal
        is WireRefusal.UNSUPPORTED_VERSION
    )


@pytest.mark.parametrize("session_id", ["a", "~", "!", "session-1.AbC_9", "\x21\x7e"])
def test_a_visible_ascii_session_id_is_kept(session_id: str) -> None:
    assert require_session_id(session_id) == session_id


@pytest.mark.parametrize(
    "session_id",
    ["", " ", "a b", "tab\t", "line\n", "\x7f", "caf\xe9", "snow\u2603", "\x00"],
    ids=["empty", "space", "inner-space", "tab", "newline", "del", "latin-1", "unicode", "nul"],
)
def test_a_session_id_outside_visible_ascii_is_refused_and_echoes_nothing(session_id: str) -> None:
    refusal = refusal_of(require_session_id, session_id)

    assert refusal.refusal is WireRefusal.INVALID_SESSION_ID
    assert str(refusal) == "invalid_session_id"
    assert refusal.__cause__ is None
    assert refusal.__context__ is None


def test_the_initialize_result_is_built_from_constants_and_echoes_no_client_member() -> None:
    params = read_payload(
        InitializeParams,
        {
            "protocolVersion": "1999-01-01",
            "capabilities": {"roots": {}},
            "clientInfo": {"name": SECRET},
            "instructions": SECRET,
        },
    )

    encoded = encode_message(initialize_response(1, params.protocol_version))

    assert json.loads(encoded)["result"] == {
        "protocolVersion": "2025-11-25",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "agent-service", "version": "1"},
    }
    assert SECRET.encode() not in encoded


def test_tools_list_reads_its_cursor_and_offers_the_next_one() -> None:
    cursor = read_payload(ListToolsParams, REQUEST["params"]).cursor
    page = read_payload(
        ListToolsResult,
        {
            "tools": [{"name": "echo", "inputSchema": {"type": "object"}, "title": "ignored"}],
            "nextCursor": "c2",
        },
    )

    assert cursor == "c1"
    assert [tool.name for tool in page.tools] == ["echo"]
    assert page.next_cursor == "c2"
    assert read_payload(ListToolsParams, None).cursor is None


def test_tools_call_params_default_to_no_arguments() -> None:
    assert read_payload(CallToolParams, {"name": "echo"}).arguments == {}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
            ToolOutcome("a\nb", False),
        ),
        (
            {"content": [{"type": "text", "text": "boom"}], "isError": True},
            ToolOutcome("boom", True),
        ),
        ({"content": []}, ToolOutcome("", False)),
        (
            {"content": [{"type": "text", "text": "a"}, {"type": "image", "data": SECRET}]},
            ToolOutcome(NON_TEXT_BLOCK_MESSAGE, True),
        ),
    ],
    ids=["joined", "is_error", "empty", "non-text block"],
)
def test_a_tool_result_is_read_as_joined_text_with_is_error(
    result: dict[str, Any], expected: ToolOutcome
) -> None:
    assert read_tool_outcome(result) == expected


def test_a_tool_result_without_content_is_refused_without_echo() -> None:
    refusal = refusal_of(read_tool_outcome, {"isError": SECRET})

    assert refusal.refusal is WireRefusal.INVALID_PAYLOAD
    assert SECRET not in str(refusal) + repr(refusal)
    assert refusal.__cause__ is None
    assert refusal.__context__ is None


def test_a_number_that_overflows_to_infinity_is_not_accepted_as_a_message() -> None:
    body = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"n":1e999}}'

    assert refusal_of(parse_message, body).refusal is WireRefusal.NOT_A_MESSAGE


@pytest.mark.parametrize(
    ("payload_type", "payload"),
    [
        (InitializeParams, {"protocol_version": "2025-11-25"}),
        (ListToolsResult, {"tools": [{"name": "echo", "input_schema": {}}]}),
        (CallToolParams, {"name": "echo", "arguments": {"a": float("inf")}}),
        (ListToolsParams, {"cursor": "\ud800"}),
        (InitializeParams, {"protocolVersion": "\ud800"}),
        (ListToolsResult, {"tools": [{"name": "e", "inputSchema": {"a": float("inf")}}]}),
    ],
    ids=[
        "snake-case protocol version",
        "snake-case input schema",
        "infinite argument",
        "surrogate",
        "surrogate protocol version",
        "infinite input schema",
    ],
)
def test_a_payload_member_read_by_its_python_name_or_not_json_is_refused(
    payload_type: type, payload: dict[str, Any]
) -> None:
    assert refusal_of(read_payload, payload_type, payload).refusal is WireRefusal.INVALID_PAYLOAD


@pytest.mark.parametrize(
    "block",
    [{"type": "text", "text": 5}, {"type": "text"}, {"type": "text", "text": "\ud800"}],
    ids=["number text", "no text", "surrogate text"],
)
def test_a_malformed_text_block_is_refused_not_read_as_another_kind(block: dict[str, Any]) -> None:
    refusal = refusal_of(read_tool_outcome, {"content": [block]})

    assert refusal.refusal is WireRefusal.INVALID_PAYLOAD


def test_members_sent_under_their_python_names_are_not_read() -> None:
    page = read_payload(ListToolsResult, {"tools": [], "next_cursor": "c2"})
    outcome = read_tool_outcome({"content": [{"type": "text", "text": "x"}], "is_error": True})

    assert page.next_cursor is None
    assert outcome == ToolOutcome("x", False)


@pytest.mark.parametrize(
    "body",
    [
        f"{{{SECRET}".encode(),
        f'{{"jsonrpc": "2.0", "id": NaN, "method": "{SECRET}"}}'.encode(),
        b"\xff\xfe" + SECRET.encode(),
        b"",
        b'{"jsonrpc":"1.0","jsonrpc":"2.0","id":1,"method":"ping"}',
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"a":{"b":1,"b":2}}}',
        b"[" * 20000,
    ],
    ids=[
        "truncated",
        "non-json constant",
        "not utf-8",
        "empty",
        "duplicate member",
        "nested duplicate member",
        "too deep to decode",
    ],
)
def test_malformed_json_gives_a_closed_error_that_echoes_nothing(body: bytes) -> None:
    refusal = refusal_of(parse_message, body)

    assert refusal.refusal is WireRefusal.MALFORMED_JSON
    assert str(refusal) == "malformed_json"
    assert refusal.__cause__ is None
    assert refusal.__context__ is None


def test_a_body_over_the_event_bound_is_refused_and_one_at_the_bound_is_read() -> None:
    padding = '{"jsonrpc":"2.0","method":"ping","params":{"p":""}}'
    at_bound = padding.replace('""', '"' + "x" * (MAX_EVENT_BYTES - len(padding)) + '"').encode()

    assert len(at_bound) == MAX_EVENT_BYTES
    assert isinstance(parse_message(at_bound), JsonRpcNotification)
    assert refusal_of(parse_message, at_bound + b" ").refusal is WireRefusal.TOO_LARGE
    assert (
        refusal_of(parse_message, b"\xff" * (MAX_EVENT_BYTES + 1)).refusal is WireRefusal.TOO_LARGE
    )


@pytest.mark.parametrize(
    ("content_type", "accepted"),
    [
        ("application/json", True),
        ("Application/JSON; charset=utf-8", True),
        ("text/event-stream", False),
        ("application/jsonl", False),
        ("", False),
        (None, False),
    ],
)
def test_only_an_application_json_answer_is_accepted(
    content_type: str | None, accepted: bool
) -> None:
    body = body_of(RESPONSE)

    if accepted:
        assert isinstance(read_answer(content_type, body), JsonRpcResponse)
        assert require_json_answer(content_type) is None
    else:
        assert refusal_of(read_answer, content_type, body).refusal is WireRefusal.NOT_JSON_ANSWER
        assert refusal_of(require_json_answer, content_type).refusal is WireRefusal.NOT_JSON_ANSWER


def test_an_answer_that_is_a_request_is_refused() -> None:
    assert (
        refusal_of(read_answer, "application/json", body_of(REQUEST)).refusal
        is WireRefusal.UNEXPECTED_MESSAGE
    )


def test_a_batch_array_is_refused() -> None:
    assert refusal_of(parse_message, body_of([REQUEST, NOTIFICATION])).refusal is WireRefusal.BATCH


@pytest.mark.parametrize(
    ("header_value", "status"),
    [
        ("2025-11-25", HTTPStatus.OK),
        ("2025-06-18", HTTPStatus.BAD_REQUEST),
        ("2026-07-28", HTTPStatus.BAD_REQUEST),
        ("", HTTPStatus.BAD_REQUEST),
        (None, HTTPStatus.BAD_REQUEST),
    ],
    ids=["supported", "older", "probe version", "empty", "absent means 2025-03-26"],
)
def test_a_request_header_outside_the_set_or_absent_is_refused_with_400(
    header_value: str | None, status: HTTPStatus
) -> None:
    assert protocol_header_status(header_value) == status


def test_the_client_sends_the_negotiated_version_only_after_initialize() -> None:
    assert "MCP-Protocol-Version" not in request_headers(None)
    after_initialize = request_headers(NEGOTIATED)
    assert after_initialize["MCP-Protocol-Version"] == "2025-11-25"
    assert after_initialize["Accept"] == "application/json, text/event-stream"
    assert after_initialize["Content-Type"] == "application/json"


def test_a_notification_is_answered_with_202_and_no_body() -> None:
    answer = answer_for(None)

    assert answer == ACKNOWLEDGEMENT
    assert (answer.status, answer.body, answer.content_type) == (HTTPStatus.ACCEPTED, b"", None)
    require_acknowledgement(202, b"")


@pytest.mark.parametrize(
    ("status", "body"), [(200, b""), (202, b"{}"), (202, b" "), (204, b""), (400, b"")]
)
def test_a_notification_answer_other_than_202_with_an_empty_body_is_refused(
    status: int, body: bytes
) -> None:
    assert refusal_of(require_acknowledgement, status, body).refusal is WireRefusal.NOT_ACKNOWLEDGED


def test_a_reply_is_answered_with_200_and_its_json() -> None:
    reply = JsonRpcError(id=1, error=JsonRpcErrorBody(code=-32601, message="method not found"))

    answer = answer_for(reply)

    assert (answer.status, answer.content_type) == (HTTPStatus.OK, "application/json")
    assert json.loads(answer.body) == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32601, "message": "method not found"},
    }


@pytest.mark.parametrize(
    ("request_id", "code", "expected"),
    [
        pytest.param(
            8,
            JsonRpcErrorCode.METHOD_NOT_FOUND,
            {"jsonrpc": "2.0", "id": 8, "error": {"code": -32601, "message": "Method not found"}},
            id="W1a-method-not-found",
        ),
        pytest.param(
            "r1",
            JsonRpcErrorCode.INVALID_PARAMS,
            {"jsonrpc": "2.0", "id": "r1", "error": {"code": -32602, "message": "Invalid params"}},
            id="W1b-invalid-params",
        ),
    ],
)
def test_an_error_response_writes_a_closed_message_and_echoes_only_the_id(
    request_id: int | str, code: JsonRpcErrorCode, expected: dict[str, Any]
) -> None:
    assert encode_message(error_response(request_id, code)) == json.dumps(expected).encode()


@pytest.mark.parametrize("outcome", [ToolOutcome("a\nb", True), ToolOutcome("ok", False)])
def test_a_tool_result_response_is_written_by_alias_and_reads_back_to_the_same_outcome(
    outcome: ToolOutcome,
) -> None:
    written = encode_message(tool_result_response(5, outcome))

    assert written == body_of(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "result": {
                "content": [{"type": "text", "text": outcome.content}],
                "isError": outcome.is_error,
            },
        }
    )
    parsed = parse_message(written)
    assert isinstance(parsed, JsonRpcResponse)
    assert read_tool_outcome(parsed.result) == outcome


def test_a_tools_list_response_writes_kept_definitions_by_alias_without_a_null_description() -> (
    None
):
    upstream = [
        {
            "name": "echo",
            "description": "Say it back",
            "inputSchema": {"type": "object", "properties": {"text": {"default": None}}},
            "annotations": {"readOnlyHint": True},
            "outputSchema": {"type": "object"},
            "_meta": {"x": 1},
        },
        {"name": "ping", "description": None, "inputSchema": {"type": "object"}, "_meta": {}},
    ]
    tools = [read_payload(ToolDefinition, definition) for definition in upstream]
    expected = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "tools": [
                {
                    "name": "echo",
                    "description": "Say it back",
                    "inputSchema": {"type": "object", "properties": {"text": {"default": None}}},
                },
                {"name": "ping", "inputSchema": {"type": "object"}},
            ]
        },
    }

    assert encode_message(tools_list_response(3, tools)) == json.dumps(expected).encode()


OFFERED = frozenset({"echo"})


def _decide(message: dict[str, Any], version: str | None = "2025-11-25") -> Any:
    return decide_server_message(json.dumps(message).encode(), version, OFFERED)


def _request(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    members = {} if params is None else {"params": params}
    return {"jsonrpc": "2.0", "id": 5, "method": method, **members}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_request("tools/list"), Listing(5)),
        (
            _request("tools/call", {"name": "echo"}),
            Call(5, CallToolParams(name="echo")),
        ),
        (_request("tools/call", {"name": "other"}), Unoffered(5)),
        (
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}},
            Notified(cancelled=3),
        ),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, Notified()),
        (
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"requestId": 3}},
            Notified(),
        ),
        (
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": []}},
            Notified(),
        ),
    ],
)
def test_the_server_half_hands_state_bound_messages_to_its_caller(
    message: dict[str, Any], expected: Any
) -> None:
    assert _decide(message) == expected


@pytest.mark.parametrize(
    ("message", "reply"),
    [
        (_request("ping"), JsonRpcResponse(id=5, result={})),
        (
            _request("initialize", {"protocolVersion": "2025-11-25"}),
            initialize_response(5, "2025-11-25"),
        ),
        (_request("initialize", {}), error_response(5, JsonRpcErrorCode.INVALID_PARAMS)),
        (_request("tools/call", {}), error_response(5, JsonRpcErrorCode.INVALID_PARAMS)),
        (_request("resources/list"), error_response(5, JsonRpcErrorCode.METHOD_NOT_FOUND)),
    ],
)
def test_the_server_half_answers_what_needs_no_state_itself(
    message: dict[str, Any], reply: JsonRpcResponse | JsonRpcError
) -> None:
    assert _decide(message) == Answered(answer_for(reply))


@pytest.mark.parametrize(
    ("message", "version"),
    [
        (_request("tools/list"), None),
        (_request("tools/list"), "2024-11-05"),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, None),
    ],
)
def test_the_server_half_refuses_a_message_after_initialize_without_a_supported_version(
    message: dict[str, Any], version: str | None
) -> None:
    refused = _decide(message, version)
    assert refused == Answered(HttpAnswer(HTTPStatus.BAD_REQUEST, None, b""))


def test_the_server_half_lets_initialize_through_without_a_version_header() -> None:
    decision = _decide(_request("initialize", {"protocolVersion": "2025-11-25"}), None)
    assert decision == Answered(answer_for(initialize_response(5, "2025-11-25")))


@pytest.mark.parametrize("message", [RESPONSE, ERROR])
def test_the_server_half_refuses_a_message_that_is_an_answer(message: dict[str, Any]) -> None:
    assert _decide(message) == Answered(HttpAnswer(HTTPStatus.BAD_REQUEST, None, b""))


def test_the_server_half_refuses_a_body_that_is_not_a_message() -> None:
    decision = decide_server_message(b"not json", "2025-11-25", OFFERED)
    assert decision == Answered(HttpAnswer(HTTPStatus.BAD_REQUEST, None, b""))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('{"a": 1, "a": 2}', id="duplicate-member"),
        pytest.param('{"a": {"b": 1, "b": 1}}', id="nested-duplicate-member"),
        pytest.param('{"a": NaN}', id="not-a-number"),
        pytest.param("[Infinity]", id="infinity"),
        pytest.param("[" * 100_000, id="nesting-past-the-recursion-limit"),
    ],
)
def test_the_strict_reader_refuses_with_value_error_and_keeps_no_recursion_error(text: str) -> None:
    with pytest.raises(ValueError) as refused:
        read_strict_json(text)

    assert type(refused.value) in (ValueError, json.JSONDecodeError)
    assert not isinstance(refused.value.__context__, RecursionError)


def test_the_strict_reader_reads_a_plain_document() -> None:
    assert read_strict_json('{"a": [1, "b", null, {"c": true}]}') == {
        "a": [1, "b", None, {"c": True}]
    }
