"""The JSON-only MCP wire profile the service speaks with an app and with the CLI.

The profile is POST only, answered in ``application/json`` only, carries
``MCP-Protocol-Version`` on every request after ``initialize`` and answers a
notification with 202 and an empty body (decision record 0001, HD1). This
module holds the pure rules: parse, encode, negotiate, decide a header, decide
a content type. It opens no socket and reads no clock or environment, so the
relay (towards the CLI) and the upstream client (towards the app) both build
on it.

Every refusal is one closed ``McpWireError`` that names only the rule, never
the input: a body, a header or a result may carry the capability.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Any, Final, Literal, NoReturn, Self, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from agent_client.wire import MAX_EVENT_BYTES, JsonObject, UnicodeText, refuse_non_json_constant
from agent_providers.errors import raise_without_chain

JSON_MEDIA_TYPE: Final = "application/json"
EVENT_STREAM_MEDIA_TYPE: Final = "text/event-stream"
PROTOCOL_VERSION_HEADER: Final = "MCP-Protocol-Version"
_ASSUMED_PROTOCOL_VERSION: Final = "2025-03-26"
_SERVER_NAME: Final = "agent-service"
_SERVER_VERSION: Final = "1"
_JSON_RPC_VERSION: Final = "2.0"
NON_TEXT_BLOCK_MESSAGE: Final = "the tool returned content that is not text"
_TEXT_BLOCK_SEPARATOR: Final = "\n"


class ProtocolVersion(StrEnum):
    """The protocol versions this profile implements, newest last."""

    V2025_11_25 = "2025-11-25"


SUPPORTED_PROTOCOL_VERSIONS: Final = frozenset(ProtocolVersion)
_LATEST_PROTOCOL_VERSION: Final = ProtocolVersion.V2025_11_25


class McpMethod(StrEnum):
    """The methods the profile serves; any other method parses as an ordinary request."""

    INITIALIZE = "initialize"
    INITIALIZED = "notifications/initialized"
    CANCELLED = "notifications/cancelled"
    PING = "ping"
    TOOLS_LIST = "tools/list"
    TOOLS_CALL = "tools/call"


class WireRefusal(StrEnum):
    MALFORMED_JSON = "malformed_json"
    TOO_LARGE = "too_large"
    BATCH = "batch"
    NOT_A_MESSAGE = "not_a_message"
    UNEXPECTED_MESSAGE = "unexpected_message"
    INVALID_PAYLOAD = "invalid_payload"
    NOT_JSON_ANSWER = "not_json_answer"
    NOT_ACKNOWLEDGED = "not_acknowledged"
    UNSUPPORTED_VERSION = "unsupported_version"


class McpWireError(Exception):
    """A closed refusal: the message is the rule that refused, never the input."""

    def __init__(self, refusal: WireRefusal) -> None:
        super().__init__(refusal)
        self.refusal = refusal


def _refuse(refusal: WireRefusal) -> NoReturn:
    raise_without_chain(McpWireError(refusal))


class _Model(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        alias_generator=to_camel,
    )


class _Tolerant(_Model):
    """A payload from the other side: unknown members are read past, never kept."""

    model_config = ConfigDict(extra="ignore")


RequestId = int | UnicodeText


class JsonRpcRequest(_Model):
    id: RequestId
    method: UnicodeText
    params: JsonObject = Field(default_factory=dict)


class JsonRpcNotification(_Model):
    method: UnicodeText
    params: JsonObject = Field(default_factory=dict)


class JsonRpcResponse(_Model):
    id: RequestId
    result: JsonObject


class JsonRpcErrorBody(_Model):
    code: int
    message: UnicodeText


class JsonRpcError(_Model):
    id: RequestId | None
    error: JsonRpcErrorBody


type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse | JsonRpcError
_Item = TypeVar("_Item")
_JsonArray = Annotated[tuple[_Item, ...], Field(strict=False)]
type Reply = JsonRpcResponse | JsonRpcError


def _object_without_duplicate_members(members: list[tuple[str, Any]]) -> dict[str, Any]:
    document = dict(members)
    if len(document) != len(members):
        raise ValueError("an object repeats a member")
    return document


def _model_of(document: dict[str, Any]) -> type[JsonRpcMessage]:
    if "method" in document:
        return JsonRpcRequest if "id" in document else JsonRpcNotification
    if "result" in document:
        return JsonRpcResponse
    if "error" in document:
        return JsonRpcError
    _refuse(WireRefusal.NOT_A_MESSAGE)


def parse_message(body: bytes) -> JsonRpcMessage:
    if len(body) > MAX_EVENT_BYTES:
        _refuse(WireRefusal.TOO_LARGE)
    try:
        document = json.loads(
            body.decode("utf-8"),
            parse_constant=refuse_non_json_constant,
            object_pairs_hook=_object_without_duplicate_members,
        )
    except (ValueError, RecursionError):
        _refuse(WireRefusal.MALFORMED_JSON)
    if isinstance(document, list):
        _refuse(WireRefusal.BATCH)
    if not isinstance(document, dict) or document.pop("jsonrpc", None) != _JSON_RPC_VERSION:
        _refuse(WireRefusal.NOT_A_MESSAGE)
    try:
        return _model_of(document).model_validate(document)
    except ValidationError:
        _refuse(WireRefusal.NOT_A_MESSAGE)


def encode_message(message: JsonRpcMessage) -> bytes:
    members = message.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return json.dumps({"jsonrpc": _JSON_RPC_VERSION, **members}).encode("utf-8")


@dataclass(frozen=True)
class HttpAnswer:
    status: HTTPStatus
    content_type: str | None
    body: bytes


ACKNOWLEDGEMENT: Final = HttpAnswer(HTTPStatus.ACCEPTED, None, b"")


def answer_for(reply: Reply | None) -> HttpAnswer:
    """The HTTP answer to one message: ``None`` is a notification, which gets no body."""
    if reply is None:
        return ACKNOWLEDGEMENT
    return HttpAnswer(HTTPStatus.OK, JSON_MEDIA_TYPE, encode_message(reply))


def require_acknowledgement(status: int, body: bytes) -> None:
    if (status, body) != (ACKNOWLEDGEMENT.status, ACKNOWLEDGEMENT.body):
        _refuse(WireRefusal.NOT_ACKNOWLEDGED)


def read_answer(content_type: str | None, body: bytes) -> Reply:
    """The answer to a request: only ``application/json``, never an event stream."""
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type != JSON_MEDIA_TYPE:
        _refuse(WireRefusal.NOT_JSON_ANSWER)
    message = parse_message(body)
    if not isinstance(message, JsonRpcResponse | JsonRpcError):
        _refuse(WireRefusal.UNEXPECTED_MESSAGE)
    return message


def negotiate_protocol_version(requested: str) -> ProtocolVersion:
    """A requested version inside the set is kept; any other gets the latest one."""
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return ProtocolVersion(requested)
    return _LATEST_PROTOCOL_VERSION


def require_supported_version(offered: str) -> ProtocolVersion:
    """The client's check of the version a server answered ``initialize`` with."""
    if offered not in SUPPORTED_PROTOCOL_VERSIONS:
        _refuse(WireRefusal.UNSUPPORTED_VERSION)
    return ProtocolVersion(offered)


def protocol_header_status(header_value: str | None) -> HTTPStatus:
    """The relay's decision for a request after ``initialize``; an absent header is not a pass."""
    effective = _ASSUMED_PROTOCOL_VERSION if header_value is None else header_value
    if effective in SUPPORTED_PROTOCOL_VERSIONS:
        return HTTPStatus.OK
    return HTTPStatus.BAD_REQUEST


def request_headers(negotiated: ProtocolVersion | None) -> dict[str, str]:
    """The client's headers; ``initialize`` is the one request sent before a version exists."""
    headers = {
        "Content-Type": JSON_MEDIA_TYPE,
        "Accept": f"{JSON_MEDIA_TYPE}, {EVENT_STREAM_MEDIA_TYPE}",
    }
    if negotiated is not None:
        headers[PROTOCOL_VERSION_HEADER] = negotiated.value
    return headers


def read_payload[Payload: _Model](
    payload_type: type[Payload], payload: Mapping[str, Any] | None
) -> Payload:
    try:
        return payload_type.model_validate({} if payload is None else dict(payload))
    except ValidationError:
        _refuse(WireRefusal.INVALID_PAYLOAD)


class InitializeParams(_Tolerant):
    protocol_version: UnicodeText


class _ServerInfo(_Model):
    name: str = _SERVER_NAME
    version: str = _SERVER_VERSION


class _ToolsCapability(_Model):
    list_changed: bool = False


class _ServerCapabilities(_Model):
    tools: _ToolsCapability = _ToolsCapability()


class _InitializeResult(_Model):
    protocol_version: ProtocolVersion
    capabilities: _ServerCapabilities = _ServerCapabilities()
    server_info: _ServerInfo = _ServerInfo()

    @classmethod
    def negotiated(cls, requested: str) -> Self:
        return cls.model_validate({"protocolVersion": negotiate_protocol_version(requested)})


def initialize_response(request_id: RequestId, requested_version: str) -> JsonRpcResponse:
    result = _InitializeResult.negotiated(requested_version)
    return JsonRpcResponse(id=request_id, result=result.model_dump(mode="json", by_alias=True))


class ListToolsParams(_Tolerant):
    cursor: UnicodeText | None = None


class ToolDefinition(_Tolerant):
    name: UnicodeText
    description: UnicodeText | None = None
    input_schema: JsonObject


class ListToolsResult(_Tolerant):
    tools: _JsonArray[ToolDefinition]
    next_cursor: UnicodeText | None = None


class CallToolParams(_Tolerant):
    name: UnicodeText
    arguments: JsonObject = Field(default_factory=dict)


class _TextBlock(_Tolerant):
    type: Literal["text"]
    text: UnicodeText


def _refuse_text_type(block_type: str) -> str:
    if block_type == "text":
        raise ValueError("a text block that is malformed is not a block of another type")
    return block_type


class _OtherBlock(_Tolerant):
    type: Annotated[UnicodeText, AfterValidator(_refuse_text_type)]


class _CallToolResult(_Tolerant):
    content: _JsonArray[Annotated[_TextBlock | _OtherBlock, Field(union_mode="left_to_right")]]
    is_error: bool = False


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    is_error: bool


def read_tool_outcome(result: Mapping[str, Any]) -> ToolOutcome:
    """What a model is shown of a ``tools/call`` result: its text blocks, joined."""
    called = read_payload(_CallToolResult, result)
    if not all(isinstance(block, _TextBlock) for block in called.content):
        return ToolOutcome(NON_TEXT_BLOCK_MESSAGE, is_error=True)
    texts = (block.text for block in called.content if isinstance(block, _TextBlock))
    return ToolOutcome(_TEXT_BLOCK_SEPARATOR.join(texts), called.is_error)
