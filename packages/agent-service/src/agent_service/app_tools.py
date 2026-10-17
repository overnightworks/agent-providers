"""The service's client of an app's MCP endpoint, in the JSON-only profile of ``mcp_wire``.

One session per run: ``open_app_tools`` handshakes (``initialize``, then
``notifications/initialized``) and hands back a session that lists tools and
calls one. The client is not a general Streamable HTTP client: it sends POST
only, accepts ``application/json`` answers only and reads them as raw bytes
bounded at ``MAX_EVENT_BYTES``. Every wire rule is ``mcp_wire``'s; this module
owns only the conversation: status precedence, answer correlation, the
session id and the pagination bound.

Wire, protocol and HTTP-status refusals leave as the closed ``UpstreamError``
members below, which name only a reason, never a URL, header, body or the
capability. Transport exceptions of ``httpx`` are not mapped here.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from itertools import count
from typing import Any, Final, NoReturn

import httpx

from agent_client.capability import MCP_ENDPOINT_PATH, McpOrigin
from agent_client.wire import MAX_EVENT_BYTES, JsonObject
from agent_providers.errors import raise_without_chain
from agent_service.mcp_wire import (
    SESSION_ID_HEADER,
    InitializeParams,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    ListToolsResult,
    McpMethod,
    McpWireError,
    ProtocolVersion,
    ToolDefinition,
    ToolOutcome,
    WireRefusal,
    encode_message,
    read_answer,
    read_payload,
    read_tool_outcome,
    request_headers,
    require_acknowledgement,
    require_supported_version,
)

UPSTREAM_IO_SECONDS: Final = 10.0
MAX_TOOL_LIST_PAGES: Final = 10
_IDENTITY_ENCODING: Final = "identity"
_CLIENT_NAME: Final = "agent-service"
_CLIENT_VERSION: Final = "1"
_OFFERED_VERSION: Final = tuple(ProtocolVersion)[-1]
_FIRST_REQUEST_ID: Final = 1


class ProtocolFailure(StrEnum):
    PAGE_BOUND = "page_bound"
    ID_MISMATCH = "id_mismatch"
    CONTENT_ENCODING = "content_encoding"
    UNEXPECTED_STATUS = "unexpected_status"
    JSON_RPC_ERROR = "json_rpc_error"


class UpstreamError(Exception):
    """The closed base of what this client raises for an app that broke the profile."""


class UpstreamProtocolError(UpstreamError):
    def __init__(self, reason: WireRefusal | ProtocolFailure, rpc_error_code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.rpc_error_code = rpc_error_code


class UpstreamRefused(UpstreamError):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


def _protocol_error(
    reason: WireRefusal | ProtocolFailure, rpc_error_code: int | None = None
) -> NoReturn:
    raise_without_chain(UpstreamProtocolError(reason, rpc_error_code))


@contextmanager
def _wire_refusals_as_protocol_errors() -> Iterator[None]:
    try:
        yield
    except McpWireError as refusal:
        _protocol_error(refusal.refusal)


@dataclass(frozen=True)
class _Received:
    status: int
    content_type: str | None
    body: bytes
    session_id: str | None


@dataclass(frozen=True)
class _Answered:
    result: JsonObject
    session_id: str | None


async def _raw_body_within_bound(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_raw():
        body += chunk
        if len(body) > MAX_EVENT_BYTES:
            _protocol_error(WireRefusal.TOO_LARGE)
    return bytes(body)


def _require_identity_encoding(response: httpx.Response) -> None:
    encoding = response.headers.get("Content-Encoding")
    if encoding is not None and encoding.strip().lower() != _IDENTITY_ENCODING:
        _protocol_error(ProtocolFailure.CONTENT_ENCODING)


@dataclass(frozen=True)
class _Conversation:
    http: httpx.AsyncClient
    endpoint: str
    capability: str
    request_ids: Iterator[int]

    def _headers(
        self, negotiated: ProtocolVersion | None, session_id: str | None
    ) -> dict[str, str]:
        headers = {
            **request_headers(negotiated),
            "Authorization": f"Bearer {self.capability}",
            "Accept-Encoding": _IDENTITY_ENCODING,
        }
        if session_id is not None:
            headers[SESSION_ID_HEADER] = session_id
        return headers

    async def _post(
        self, body: bytes, negotiated: ProtocolVersion | None, session_id: str | None
    ) -> _Received:
        headers = self._headers(negotiated, session_id)
        async with self.http.stream("POST", self.endpoint, content=body, headers=headers) as sent:
            if not 200 <= sent.status_code < 300:
                raise_without_chain(UpstreamRefused(sent.status_code))
            _require_identity_encoding(sent)
            return _Received(
                sent.status_code,
                sent.headers.get("Content-Type"),
                await _raw_body_within_bound(sent),
                sent.headers.get(SESSION_ID_HEADER),
            )

    async def request(
        self,
        method: McpMethod,
        params: JsonObject,
        negotiated: ProtocolVersion | None,
        session_id: str | None,
    ) -> _Answered:
        request_id = next(self.request_ids)
        message = JsonRpcRequest(id=request_id, method=method.value, params=params)
        received = await self._post(encode_message(message), negotiated, session_id)
        if received.status != httpx.codes.OK:
            _protocol_error(ProtocolFailure.UNEXPECTED_STATUS)
        with _wire_refusals_as_protocol_errors():
            reply = read_answer(received.content_type, received.body)
        if reply.id != request_id:
            _protocol_error(ProtocolFailure.ID_MISMATCH)
        if isinstance(reply, JsonRpcError):
            _protocol_error(ProtocolFailure.JSON_RPC_ERROR, reply.error.code)
        return _Answered(reply.result, received.session_id)

    async def notify(
        self, method: McpMethod, negotiated: ProtocolVersion, session_id: str | None
    ) -> None:
        body = encode_message(JsonRpcNotification(method=method.value))
        received = await self._post(body, negotiated, session_id)
        with _wire_refusals_as_protocol_errors():
            require_acknowledgement(received.status, received.body)


class AppToolsSession:
    """An app after a completed handshake; concurrent calls on it are allowed."""

    def __init__(
        self, conversation: _Conversation, negotiated: ProtocolVersion, session_id: str | None
    ) -> None:
        self._conversation = conversation
        self._negotiated = negotiated
        self._session_id = session_id

    async def _request(self, method: McpMethod, params: JsonObject) -> JsonObject:
        answered = await self._conversation.request(
            method, params, self._negotiated, self._session_id
        )
        return answered.result

    async def list_tools(self) -> tuple[ToolDefinition, ...]:
        tools: list[ToolDefinition] = []
        params: JsonObject = {}
        for page in range(1, MAX_TOOL_LIST_PAGES + 1):
            with _wire_refusals_as_protocol_errors():
                listed = read_payload(
                    ListToolsResult, await self._request(McpMethod.TOOLS_LIST, params)
                )
            tools.extend(listed.tools)
            if listed.next_cursor is None:
                return tuple(tools)
            params = {"cursor": listed.next_cursor}
        _protocol_error(ProtocolFailure.PAGE_BOUND)

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        params = {"name": name, "arguments": dict(arguments)}
        result = await self._request(McpMethod.TOOLS_CALL, params)
        with _wire_refusals_as_protocol_errors():
            return read_tool_outcome(result)


async def _handshake(conversation: _Conversation) -> AppToolsSession:
    initialize_params: JsonObject = {
        "protocolVersion": _OFFERED_VERSION.value,
        "capabilities": {},
        "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
    }
    answered = await conversation.request(McpMethod.INITIALIZE, initialize_params, None, None)
    with _wire_refusals_as_protocol_errors():
        negotiated = require_supported_version(
            read_payload(InitializeParams, answered.result).protocol_version
        )
    await conversation.notify(McpMethod.INITIALIZED, negotiated, answered.session_id)
    return AppToolsSession(conversation, negotiated, answered.session_id)


def _http_client(tls_context: ssl.SSLContext) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=0, verify=tls_context),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(
            connect=UPSTREAM_IO_SECONDS,
            read=None,
            write=UPSTREAM_IO_SECONDS,
            pool=UPSTREAM_IO_SECONDS,
        ),
    )


@asynccontextmanager
async def open_app_tools(
    origin: McpOrigin, capability: str, tls_context: ssl.SSLContext
) -> AsyncIterator[AppToolsSession]:
    """Handshake with the app at ``origin``; the caller's context decides whom to trust."""
    async with _http_client(tls_context) as http:
        conversation = _Conversation(
            http, f"{origin}{MCP_ENDPOINT_PATH}", capability, count(_FIRST_REQUEST_ID)
        )
        yield await _handshake(conversation)
