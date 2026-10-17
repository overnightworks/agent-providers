"""The service's client of an app's MCP endpoint, in the JSON-only profile of ``mcp_wire``.

One session per run: ``open_app_tools`` handshakes (``initialize``, then
``notifications/initialized``) and hands back a session that lists tools and
calls one. The client is not a general Streamable HTTP client: it sends POST
only, accepts ``application/json`` answers only and reads them as raw bytes
bounded at ``MAX_EVENT_BYTES``. Every wire rule is ``mcp_wire``'s; this module
owns only the conversation: status precedence, answer correlation, the
session id and the pagination bound.

Wire, protocol, HTTP-status and transport failures leave as the closed
``UpstreamError`` members below, which name only a reason or a status, never a
URL, header, body or the capability, and chain to no ``httpx`` exception. A
request is POSTed once: after its bytes may have been sent it is never
retransmitted, so a lost connection ends that call as ``UpstreamCallLost``.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from itertools import count
from typing import Any, Final, NoReturn

import httpx

from agent_client.capability import MCP_ENDPOINT_PATH, McpOrigin
from agent_client.wire import MAX_EVENT_BYTES, JsonObject
from agent_providers.errors import raise_without_chain
from agent_service.mcp_wire import (
    LATEST_PROTOCOL_VERSION,
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
    require_json_answer,
    require_session_id,
    require_supported_version,
)

UPSTREAM_IO_SECONDS: Final = 10.0
MAX_TOOL_LIST_PAGES: Final = 10
_IDENTITY_ENCODING: Final = "identity"
_CLIENT_NAME: Final = "agent-service"
_CLIENT_VERSION: Final = "1"
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


class UpstreamRedirected(UpstreamError):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


class UpstreamUnreachable(UpstreamError):
    """No request byte can have reached the app: connect, TLS or pool failed."""


class UpstreamCallLost(UpstreamError):
    """The request may have been processed; a caller must not retry it automatically."""


_NEVER_SENT: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _transport_failure(failure: httpx.HTTPError) -> NoReturn:
    if isinstance(failure, _NEVER_SENT):
        raise_without_chain(UpstreamUnreachable())
    raise_without_chain(UpstreamCallLost())


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


def _require_request_answer_headers(response: httpx.Response) -> None:
    if response.status_code != HTTPStatus.OK:
        _protocol_error(ProtocolFailure.UNEXPECTED_STATUS)
    _require_identity_encoding(response)
    with _wire_refusals_as_protocol_errors():
        require_json_answer(response.headers.get("Content-Type"))


def _require_acknowledgement_headers(response: httpx.Response) -> None:
    with _wire_refusals_as_protocol_errors():
        require_acknowledgement(response.status_code, b"")
    _require_identity_encoding(response)


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
        self,
        body: bytes,
        negotiated: ProtocolVersion | None,
        session_id: str | None,
        require_answer_headers: Callable[[httpx.Response], None],
    ) -> _Received:
        headers = self._headers(negotiated, session_id)
        try:
            async with self.http.stream(
                "POST", self.endpoint, content=body, headers=headers
            ) as sent:
                try:
                    return await self._received(sent, require_answer_headers)
                except UpstreamError:
                    await _close_refused(sent)
                    raise
        except httpx.HTTPError as failure:
            _transport_failure(failure)

    @staticmethod
    async def _received(
        sent: httpx.Response, require_answer_headers: Callable[[httpx.Response], None]
    ) -> _Received:
        if not 200 <= sent.status_code < 300:
            raise_without_chain(UpstreamRefused(sent.status_code))
        require_answer_headers(sent)
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
        received = await self._post(
            encode_message(message), negotiated, session_id, _require_request_answer_headers
        )
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
        received = await self._post(body, negotiated, session_id, _require_acknowledgement_headers)
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
        for _ in range(MAX_TOOL_LIST_PAGES):
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
        "protocolVersion": LATEST_PROTOCOL_VERSION.value,
        "capabilities": {},
        "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
    }
    answered = await conversation.request(McpMethod.INITIALIZE, initialize_params, None, None)
    with _wire_refusals_as_protocol_errors():
        negotiated = require_supported_version(
            read_payload(InitializeParams, answered.result).protocol_version
        )
        session_id = (
            None if answered.session_id is None else require_session_id(answered.session_id)
        )
    await conversation.notify(McpMethod.INITIALIZED, negotiated, session_id)
    return AppToolsSession(conversation, negotiated, session_id)


async def _close_refused(response: httpx.Response) -> None:
    """Close a refused response; a failing close never replaces the refusal."""
    with suppress(httpx.HTTPError):
        await response.aclose()


async def _refuse_redirect(response: httpx.Response) -> None:
    """Decide a 3xx before httpx parses its ``Location``; a failing close never replaces it."""
    if not 300 <= response.status_code < 400:
        return
    await _close_refused(response)
    raise_without_chain(UpstreamRedirected(response.status_code))


def _http_client(tls_context: ssl.SSLContext) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=0, verify=tls_context),
        follow_redirects=False,
        event_hooks={"response": [_refuse_redirect]},
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
