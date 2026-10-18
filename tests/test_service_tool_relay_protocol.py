"""The MCP relay owner: the messages it answers, the tools it narrows to, and the leak gate that
lets no body carrying the run's capability out to the CLI."""

import asyncio
import json
from http import HTTPStatus
from typing import Any

import pytest
from capability_support import canary_token, capability_carrying
from relay_support import (
    Delivery,
    RawClient,
    RelayWrites,
    RequestHead,
    ScriptedCalls,
    clean_reply,
    drive,
    json_rpc,
    port_of,
)
from relay_support import (
    post_message as post_message_with,
)
from service_support import failure_lines

from agent_service.mcp_wire import (
    ACKNOWLEDGEMENT,
    CallToolParams,
    HttpAnswer,
    JsonRpcError,
    JsonRpcErrorBody,
    JsonRpcResponse,
    ToolDefinition,
    answer_for,
)
from agent_service.tool_relay import (
    UNANSWERED,
    CapabilityReflected,
    McpRelayOwner,
    Relay,
    RelayRequest,
    RepeatedToolOffer,
    Unanswered,
)

CLAIM = ("list_songs", "read_lyrics", "save_lyrics")
SUPPORTED = "2025-11-25"
CLI_DISCOVERY_VERSION = "2026-07-28"
CANARY = canary_token()
SEGMENT = CANARY.rsplit(".", 1)[-1]
OWNER_FAILED = "The owner of the relay of run run-under-test failed"
EMPTY_BAD_REQUEST = HttpAnswer(HTTPStatus.BAD_REQUEST, None, b"")
INITIALIZE_PARAMS = {"protocolVersion": SUPPORTED}
SAVE_SCHEMA: dict[str, Any] = {"type": "object", "default": None}
READ_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"id": {"type": "string"}}}


def tool(name: str, description: str | None, schema: dict[str, Any]) -> ToolDefinition:
    members: dict[str, Any] = {
        "name": name,
        "inputSchema": schema,
        "annotations": {"readOnlyHint": True},
        "outputSchema": {"type": "object"},
        "title": "A title",
        "_meta": {"origin": "app"},
    }
    if description is not None:
        members["description"] = description
    return ToolDefinition.model_validate(members)


def the_offer(
    save_description: str = "Saves lyrics.",
    read_schema: dict[str, Any] = READ_SCHEMA,
    delete_description: str = "Deletes a song.",
) -> list[ToolDefinition]:
    return [
        tool("save_lyrics", save_description, SAVE_SCHEMA),
        tool("delete_song", delete_description, {"type": "object"}),
        tool("read_lyrics", None, read_schema),
    ]


def owner_of(
    offered: list[ToolDefinition] | None = None,
    claim: tuple[str, ...] = CLAIM,
    calls: ScriptedCalls | None = None,
) -> McpRelayOwner:
    return McpRelayOwner(
        capability_carrying(CANARY, tools=claim),
        the_offer() if offered is None else offered,
        calls or ScriptedCalls(),
    )


def answered(
    owner: McpRelayOwner, body: bytes, protocol_version: str | None = SUPPORTED
) -> HttpAnswer | Unanswered:
    return asyncio.run(owner.answer(RelayRequest(body, protocol_version)))


def written(document: dict[str, Any]) -> bytes:
    return json.dumps({"jsonrpc": "2.0", **document}).encode()


def error_body(request_id: int | str, code: int, message: str) -> bytes:
    return written({"id": request_id, "error": {"code": code, "message": message}})


def ok_json(body: bytes) -> HttpAnswer:
    return HttpAnswer(HTTPStatus.OK, "application/json", body)


INITIALIZED = written(
    {
        "id": 1,
        "result": {
            "protocolVersion": SUPPORTED,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "agent-service", "version": "1"},
        },
    }
)
NARROWED_LIST = written(
    {
        "id": 2,
        "result": {
            "tools": [
                {"name": "save_lyrics", "description": "Saves lyrics.", "inputSchema": SAVE_SCHEMA},
                {"name": "read_lyrics", "inputSchema": READ_SCHEMA},
            ]
        },
    }
)


@pytest.mark.parametrize(
    "version",
    [
        pytest.param(None, id="O1a-initialize-without-the-header"),
        pytest.param(CLI_DISCOVERY_VERSION, id="O1b-initialize-is-exempt-from-the-header-rule"),
    ],
)
def test_initialize_is_answered_from_constants_whatever_its_header(version: str | None) -> None:
    calls = ScriptedCalls()
    body = json_rpc("initialize", 1, INITIALIZE_PARAMS)

    assert answered(owner_of(calls=calls), body, version) == ok_json(INITIALIZED)
    assert calls.calls == []


def test_initialize_without_a_protocol_version_is_invalid_params() -> None:
    body = json_rpc("initialize", 1, {})

    assert answered(owner_of(), body, None) == ok_json(error_body(1, -32602, "Invalid params"))


@pytest.mark.parametrize(
    ("body", "version", "expected"),
    [
        pytest.param(
            json_rpc("server/discover", 3),
            CLI_DISCOVERY_VERSION,
            EMPTY_BAD_REQUEST,
            id="O2a-discover-with-the-measured-header",
        ),
        pytest.param(
            json_rpc("server/discover", 3),
            SUPPORTED,
            ok_json(error_body(3, -32601, "Method not found")),
            id="O2b-discover-with-a-supported-header",
        ),
        pytest.param(json_rpc("ping", 4), None, EMPTY_BAD_REQUEST, id="O2c-ping-without-header"),
        pytest.param(
            json_rpc("notifications/initialized"),
            None,
            EMPTY_BAD_REQUEST,
            id="O2d-notification-without-header",
        ),
        pytest.param(b"{not json", SUPPORTED, EMPTY_BAD_REQUEST, id="O3a-malformed-json"),
        pytest.param(
            b"[" + json_rpc("ping", 1) + b"]", SUPPORTED, EMPTY_BAD_REQUEST, id="O3b-batch"
        ),
        pytest.param(
            b'{"jsonrpc":"2.0","id":1,"result":{}}',
            SUPPORTED,
            EMPTY_BAD_REQUEST,
            id="O3c-a-response-from-the-cli",
        ),
        pytest.param(
            b'{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"x"}}',
            SUPPORTED,
            EMPTY_BAD_REQUEST,
            id="O3d-an-error-from-the-cli",
        ),
        pytest.param(
            json_rpc("notifications/initialized"),
            SUPPORTED,
            ACKNOWLEDGEMENT,
            id="O4a-initialized-notification",
        ),
        pytest.param(
            json_rpc("notifications/cancelled", None, {"requestId": 1}),
            SUPPORTED,
            ACKNOWLEDGEMENT,
            id="O4b-cancelled-notification",
        ),
        pytest.param(
            json_rpc("notifications/unknown"), SUPPORTED, ACKNOWLEDGEMENT, id="O4c-any-notification"
        ),
        pytest.param(
            json_rpc("ping", 7),
            SUPPORTED,
            ok_json(written({"id": 7, "result": {}})),
            id="O4d-ping",
        ),
        pytest.param(
            json_rpc("resources/list", 8),
            SUPPORTED,
            ok_json(error_body(8, -32601, "Method not found")),
            id="O4e-unknown-method-names-nothing",
        ),
    ],
)
def test_answers_each_message_without_reaching_the_tool_port(
    body: bytes, version: str | None, expected: HttpAnswer
) -> None:
    calls = ScriptedCalls()

    answer = answered(owner_of(calls=calls), body, version)

    assert answer == expected
    assert b"resources/list" not in answer.body
    assert calls.calls == []


@pytest.mark.parametrize(
    ("params", "routed"),
    [
        ({"requestId": 4}, [4]),
        ({"requestId": "a", "reason": "x"}, ["a"]),
        (None, []),
        ({"requestId": 1.5}, []),
        ({"requestId": True}, []),
    ],
    ids=["number", "text-with-reason", "no-params", "fraction", "boolean"],
)
def test_a_cancel_reaches_the_tool_port_only_with_a_well_formed_request_id(
    params: dict[str, Any] | None, routed: list[int | str]
) -> None:  # C1
    calls = ScriptedCalls()

    answer = answered(owner_of(calls=calls), json_rpc("notifications/cancelled", None, params))

    assert answer == ACKNOWLEDGEMENT
    assert calls.cancelled_ids == routed
    assert calls.calls == []


def test_tools_list_holds_the_allowed_offered_tools_in_the_offers_order() -> None:
    answer = answered(owner_of(), json_rpc("tools/list", 2))

    assert answer == ok_json(NARROWED_LIST)


def test_an_empty_claim_lists_no_tool() -> None:
    answer = answered(owner_of(claim=()), json_rpc("tools/list", 2))

    assert answer == ok_json(written({"id": 2, "result": {"tools": []}}))


def test_an_offer_naming_an_allowed_tool_twice_is_refused_without_naming_it() -> None:
    offer = [*the_offer(), tool("save_lyrics", "Again.", SAVE_SCHEMA)]

    with pytest.raises(RepeatedToolOffer) as refusal:
        owner_of(offer)

    assert "save_lyrics" not in f"{refusal.value!s} {refusal.value!r}"


def test_a_repeated_name_outside_the_claim_is_dropped_not_refused() -> None:
    offer = [*the_offer(), tool("delete_song", "Again.", SAVE_SCHEMA)]

    assert answered(owner_of(offer), json_rpc("tools/list", 2)) == ok_json(NARROWED_LIST)


def test_tools_call_hands_the_parsed_params_to_the_port_and_answers_its_reply() -> None:
    calls = ScriptedCalls()
    params = {"name": "save_lyrics", "arguments": {"text": "x"}, "_meta": {"progressToken": 1}}

    answer = answered(owner_of(calls=calls), json_rpc("tools/call", 5, params))

    assert calls.calls == [(5, CallToolParams(name="save_lyrics", arguments={"text": "x"}))]
    assert answer == answer_for(clean_reply(5))


def test_tools_call_without_a_name_is_invalid_params_and_not_handed_on() -> None:
    calls = ScriptedCalls()

    answer = answered(owner_of(calls=calls), json_rpc("tools/call", 6, {"arguments": {}}))

    assert answer == ok_json(error_body(6, -32602, "Invalid params"))
    assert calls.calls == []


def reflected_in_a_description() -> list[ToolDefinition]:
    return the_offer(save_description=f"Saves {CANARY}")


def reflected_in_a_schema() -> list[ToolDefinition]:
    return the_offer(read_schema={"type": "object", "description": SEGMENT})


@pytest.mark.parametrize(
    "offer",
    [
        pytest.param(reflected_in_a_description, id="O7a-the-whole-token-in-a-description"),
        pytest.param(reflected_in_a_schema, id="O7b-the-segment-in-a-schema"),
    ],
)
def test_a_tool_list_carrying_the_capability_is_refused(offer: Any) -> None:
    owner = owner_of(offer())
    body = json_rpc("tools/list", 2)

    with pytest.raises(CapabilityReflected):
        answered(owner, body)


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(
            JsonRpcResponse(id=5, result={"content": [{"type": "text", "text": CANARY}]}),
            id="O7c-a-result-text",
        ),
        pytest.param(
            JsonRpcError(id=5, error=JsonRpcErrorBody(code=-32603, message=f"x {SEGMENT}")),
            id="O7d-an-error-message",
        ),
    ],
)
def test_a_tool_reply_carrying_the_capability_is_refused(
    reply: JsonRpcResponse | JsonRpcError,
) -> None:
    owner = owner_of(calls=ScriptedCalls([reply]))
    body = json_rpc("tools/call", 5, {"name": "save_lyrics"})

    with pytest.raises(CapabilityReflected):
        answered(owner, body)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        pytest.param("initialize", INITIALIZE_PARAMS, id="O7e-a-locally-generated-result"),
        pytest.param("resources/list", None, id="O7f-a-locally-generated-error"),
    ],
)
def test_a_locally_generated_reply_echoing_the_capability_as_its_id_is_refused(
    method: str, params: dict[str, Any] | None
) -> None:
    owner = owner_of()
    body = json_rpc(method, SEGMENT, params)

    with pytest.raises(CapabilityReflected):
        answered(owner, body)


def test_a_dropped_definition_never_reaches_the_cli_even_when_it_carries_the_capability() -> None:
    owner = owner_of(the_offer(delete_description=f"Deletes {CANARY}"))

    answer = answered(owner, json_rpc("tools/list", 2))

    assert answer == ok_json(NARROWED_LIST)
    assert SEGMENT.encode() not in answer.body


def message_for(relay: Relay, body: bytes, version: str | None = SUPPORTED) -> bytes:
    return post_message_with(relay, body, version)


def test_the_measured_cli_sequence_is_answered_on_one_connection() -> None:
    calls = ScriptedCalls()
    owner = owner_of(calls=calls)

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        get = RequestHead.standard(relay).replace_method("GET").encode()
        steps = [
            (message_for(relay, json_rpc("server/discover", 1), CLI_DISCOVERY_VERSION), 400),
            (message_for(relay, json_rpc("initialize", 1, INITIALIZE_PARAMS), None), 200),
            (message_for(relay, json_rpc("notifications/initialized")), 202),
            (get, 405),
            (message_for(relay, json_rpc("tools/list", 2)), 200),
        ]
        statuses = []
        for raw, _ in steps:
            await client.send(raw)
            answer = await client.answer()
            statuses.append(answer.status)
        assert statuses == [step[1] for step in steps]
        assert answer.body == NARROWED_LIST
        assert not client.has_ended()
        assert calls.calls == []

    drive(scenario, owner=owner, calls=calls)


async def refused_exchange(relay: Relay, body: bytes) -> None:
    client = await RawClient.connect(port_of(relay))
    await client.send(message_for(relay, body))
    answer = await client.answer()
    assert (answer.status, answer.body) == (HTTPStatus.INTERNAL_SERVER_ERROR, b"")
    assert SEGMENT not in repr(answer)


def test_a_refused_tool_list_gets_an_empty_500_and_so_does_every_later_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = owner_of(reflected_in_a_description())

    async def scenario(relay: Relay) -> None:
        await refused_exchange(relay, json_rpc("tools/list", 2))
        await refused_exchange(relay, json_rpc("ping", 3))

    drive(scenario, owner=owner)

    assert [(event, kind) for event, kind, _ in failure_lines(caplog)] == [
        (OWNER_FAILED, "CapabilityReflected")
    ] * 2
    assert CANARY not in caplog.text
    assert SEGMENT not in caplog.text


def test_a_request_id_that_is_the_capability_gets_an_empty_500_and_one_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario(relay: Relay) -> None:
        await refused_exchange(relay, json_rpc("ping", SEGMENT))

    drive(scenario, owner=owner_of())

    assert [(event, kind) for event, kind, _ in failure_lines(caplog)] == [
        (OWNER_FAILED, "CapabilityReflected")
    ]
    assert SEGMENT not in caplog.text


@pytest.mark.parametrize(
    ("replies", "refused_lines"),
    [([], 3), ([UNANSWERED], 2)],
    ids=["answered-call-gets-the-empty-500", "unanswered-call-stays-unanswered"],
)
def test_a_call_in_flight_when_another_connection_refuses_the_owner_gets_nothing(
    replies: list[Unanswered], refused_lines: int, caplog: pytest.LogCaptureFixture
) -> None:  # C2
    calls = ScriptedCalls(replies, gated=True)
    owner = owner_of(reflected_in_a_description(), calls=calls)

    async def scenario(relay: Relay) -> None:
        in_flight = await RawClient.connect(port_of(relay))
        other = await RawClient.connect(port_of(relay))
        await in_flight.send(message_for(relay, json_rpc("tools/call", 1, {"name": "save_lyrics"})))
        await calls.wait_entered()
        await other.send(message_for(relay, json_rpc("tools/list", 2)))
        listing = await other.answer()
        assert (listing.status, listing.body) == (HTTPStatus.INTERNAL_SERVER_ERROR, b"")
        calls.release()
        if replies:
            assert await in_flight.delivery() is Delivery.ABSENT
        else:
            resumed = await in_flight.answer()
            assert (resumed.status, resumed.body) == (HTTPStatus.INTERNAL_SERVER_ERROR, b"")
        await other.send(message_for(relay, json_rpc("tools/call", 3, {"name": "save_lyrics"})))
        later = await other.answer()
        assert (later.status, later.body) == (HTTPStatus.INTERNAL_SERVER_ERROR, b"")
        assert [request_id for request_id, _ in calls.calls] == [1]

    drive(scenario, owner=owner, calls=calls)

    assert [(event, kind) for event, kind, _ in failure_lines(caplog)] == [
        (OWNER_FAILED, "CapabilityReflected")
    ] * refused_lines


def test_a_gated_call_sends_nothing_until_released_and_then_exactly_its_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay_writes = RelayWrites(monkeypatch)
    calls = ScriptedCalls(gated=True)

    async def scenario(relay: Relay) -> None:
        caller = await RawClient.connect(port_of(relay))
        bystander = await RawClient.connect(port_of(relay))
        await caller.send(message_for(relay, json_rpc("tools/call", 5, {"name": "save_lyrics"})))
        await calls.wait_entered()
        waiting = asyncio.create_task(caller.answer())
        await bystander.send(message_for(relay, json_rpc("ping", 6)))
        assert (await bystander.answer()).status == HTTPStatus.OK
        assert relay_writes.to(caller) == b""
        calls.release()
        answer = await waiting
        assert answer.status == HTTPStatus.OK
        reply = written({"id": 5, "result": {"content": [{"type": "text", "text": "done"}]}})
        assert answer.body == reply
        written_to_caller = relay_writes.to(caller)
        assert written_to_caller.startswith(b"HTTP/1.1 200 ")
        assert written_to_caller.endswith(reply)
        assert written_to_caller.count(b"HTTP/1.1") == 1

    drive(scenario, owner=owner_of(calls=calls), calls=calls)


def test_closing_the_relay_cancels_a_waiting_call_and_sends_no_answer() -> None:
    calls = ScriptedCalls(gated=True)

    async def scenario(relay: Relay) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(message_for(relay, json_rpc("tools/call", 5, {"name": "save_lyrics"})))
        await calls.wait_entered()
        await relay.close()
        assert calls.cancelled
        assert await client.delivery() is Delivery.ABSENT

    drive(scenario, owner=owner_of(calls=calls), calls=calls)
