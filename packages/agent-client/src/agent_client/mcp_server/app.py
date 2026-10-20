"""The ASGI server an app mounts to serve its tools over the JSON-only MCP profile.

``McpServer.in_front_of(host_app)`` is the outermost ASGI app: it owns ``MCP_ENDPOINT_PATH``
and everything under ``MCP_ENDPOINT_PATH + "/"``, so no router of the host answers there with
a redirect or a middleware. It answers the first failing head check with an empty body, in
this order: Host, Origin, authorization, path, method, body size. It is stateless: each handler
gets exactly the claims of its own request's capability. The capability proves the signed run
id and nothing about the run being live, so the handler's ownership check stays mandatory
(decision record 0001, O4 and H3).

Once a capability is verified, every body written for that request is asked
``carries_signature_of(token, body)``; a hit writes an empty 500 instead. A handler failure
whose traceback carries the signature becomes ``HandlerFailureWithheld``. The log names only
closed labels, never caller-controlled text.
"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Final

from pydantic import TypeAdapter

from agent_client.capability import (
    MCP_ENDPOINT_PATH,
    CapabilityClaims,
    McpOrigin,
    carries_signature_of,
)
from agent_client.errors import raise_without_chain
from agent_client.mcp_server.verification import (
    CapabilityRefusal,
    CapabilityVerificationKey,
    verify_capability,
)
from agent_client.mcp_wire import (
    ACKNOWLEDGEMENT,
    PROTOCOL_VERSION_HEADER,
    Answered,
    Call,
    HttpAnswer,
    JsonRpcErrorCode,
    Listing,
    McpMethod,
    McpWireError,
    Notified,
    ToolDefinition,
    ToolOutcome,
    Unoffered,
    answer_for,
    decide_server_message,
    error_response,
    parse_message,
    tool_result_response,
    tools_list_response,
)
from agent_client.tools import ToolDeclaration
from agent_client.wire import MAX_EVENT_BYTES, JsonObject

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type AsgiApp = Callable[[Scope, Receive, Send], Awaitable[None]]
type ToolHandler = Callable[[CapabilityClaims, JsonObject], Awaitable[ToolOutcome]]

_LOG: Final = logging.getLogger("agent_client.mcp_server")
_UNKNOWN_METHOD: Final = "unknown_method"
_UNKNOWN_TOOL: Final = "unknown_tool"
_KNOWN_METHODS: Final = frozenset(McpMethod)
_HTTPS_SCHEME: Final = "https://"
_BEARER_PREFIX: Final = "Bearer "
_HEADER_ENCODING: Final = "iso-8859-1"
_VERSION_SEPARATOR: Final = ", "
_CONTENT_TYPE_FIELD: Final = b"content-type"
_CONTENT_LENGTH_FIELD: Final = b"content-length"
_HEADERS_OF_STATUS: Final = {
    HTTPStatus.UNAUTHORIZED: [(b"www-authenticate", b"Bearer")],
    HTTPStatus.METHOD_NOT_ALLOWED: [(b"allow", b"POST")],
}
_WITHHELD: Final = HttpAnswer(HTTPStatus.INTERNAL_SERVER_ERROR, None, b"")
_ORIGIN: Final[TypeAdapter[str]] = TypeAdapter(McpOrigin)


class HandlerFailureWithheld(Exception):
    """A handler failed with the capability in its traceback; the failure itself is dropped."""

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"the failure of tool {tool_name} carried the capability")
        self.tool_name = tool_name


@dataclass(frozen=True)
class McpTool:
    declaration: ToolDeclaration
    handler: ToolHandler


@dataclass(frozen=True)
class _Verified:
    token: str
    claims: CapabilityClaims


def _field_values(scope: Scope, name: bytes) -> list[bytes]:
    return [value for field_name, value in scope["headers"] if field_name == name]


def _definition_of(declaration: ToolDeclaration) -> ToolDefinition:
    return ToolDefinition.model_validate(
        {
            "name": declaration.name,
            "description": declaration.description,
            "inputSchema": declaration.parameters,
        }
    )


def _is_under_endpoint(scope: Scope) -> bool:
    if scope["type"] != "http":
        return False
    path = scope["path"]
    return path == MCP_ENDPOINT_PATH or path.startswith(f"{MCP_ENDPOINT_PATH}/")


def _protocol_version(scope: Scope) -> str | None:
    values = _field_values(scope, PROTOCOL_VERSION_HEADER.lower().encode("ascii"))
    return _VERSION_SEPARATOR.join(v.decode(_HEADER_ENCODING) for v in values) if values else None


class McpServer:
    """One app's MCP endpoint: an ASGI 3 app that serves only ``MCP_ENDPOINT_PATH``."""

    def __init__(
        self,
        origin: str,
        key: CapabilityVerificationKey,
        tools: Sequence[McpTool],
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._origin = _ORIGIN.validate_python(origin)
        self._host = self._origin.removeprefix(_HTTPS_SCHEME).encode("ascii")
        self._key = key
        self._wall_clock = wall_clock
        self._tools = {tool.declaration.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("a tool name is declared more than once")
        self._definitions = {
            name: _definition_of(tool.declaration) for name, tool in self._tools.items()
        }

    def in_front_of(self, host_app: AsgiApp) -> AsgiApp:
        """The app to serve: the MCP prefix goes to this server, every other scope to the host."""

        async def front(scope: Scope, receive: Receive, send: Send) -> None:
            target = self if _is_under_endpoint(scope) else host_app
            await target(scope, receive, send)

        return front

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            raise TypeError(f"the MCP server serves http only, not {scope['type']}")
        head = self._decided_head(scope)
        if isinstance(head, HTTPStatus):
            await _write(send, HttpAnswer(head, None, b""))
            return
        body = await _read_body(receive)
        if body is None:
            await _write(send, HttpAnswer(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, None, b""))
            return
        answer = await self._answer(head, body, _protocol_version(scope))
        await _write(send, self._cleared(head, answer, body))

    def _decided_head(self, scope: Scope) -> _Verified | HTTPStatus:
        if _field_values(scope, b"host") != [self._host]:
            return HTTPStatus.FORBIDDEN
        if _field_values(scope, b"origin"):
            return HTTPStatus.FORBIDDEN
        verified = self._verified(scope)
        if isinstance(verified, HTTPStatus):
            return verified
        if scope["path"] != MCP_ENDPOINT_PATH or scope["query_string"]:
            return HTTPStatus.NOT_FOUND
        if scope["method"] != "POST":
            return HTTPStatus.METHOD_NOT_ALLOWED
        return verified

    def _verified(self, scope: Scope) -> _Verified | HTTPStatus:
        authorizations = _field_values(scope, b"authorization")
        if len(authorizations) != 1:
            return HTTPStatus.UNAUTHORIZED
        value = authorizations[0].decode(_HEADER_ENCODING)
        if not value.startswith(_BEARER_PREFIX):
            return HTTPStatus.UNAUTHORIZED
        token = value.removeprefix(_BEARER_PREFIX)
        verdict = verify_capability(
            token, key=self._key, origin=self._origin, now=self._wall_clock()
        )
        if isinstance(verdict, CapabilityRefusal):
            _LOG.warning("a capability was refused: %s", verdict.value)
            return HTTPStatus.UNAUTHORIZED
        return _Verified(token, verdict)

    def _allowed_definitions(self, claims: CapabilityClaims) -> list[ToolDefinition]:
        allowed = frozenset(claims.tools)
        return [definition for name, definition in self._definitions.items() if name in allowed]

    async def _answer(
        self, verified: _Verified, body: bytes, protocol_version: str | None
    ) -> HttpAnswer:
        allowed = self._allowed_definitions(verified.claims)
        decision = decide_server_message(
            body, protocol_version, frozenset(definition.name for definition in allowed)
        )
        match decision:
            case Answered(answer):
                return answer
            case Notified():
                return ACKNOWLEDGEMENT
            case Listing(request_id):
                return answer_for(tools_list_response(request_id, allowed))
            case Unoffered(request_id):
                return answer_for(error_response(request_id, JsonRpcErrorCode.INVALID_PARAMS))
            case Call(request_id, params):
                tool = self._tools[params.name]
                outcome = await _handled(tool, verified, params.arguments)
                return answer_for(tool_result_response(request_id, outcome))
            case _:
                raise AssertionError("a server decision is not handled")

    def _cleared(self, verified: _Verified, answer: HttpAnswer, request_body: bytes) -> HttpAnswer:
        if not carries_signature_of(verified.token, answer.body):
            return answer
        _LOG.warning("an answer was withheld: %s", self._request_label(request_body))
        return _WITHHELD

    def _request_label(self, body: bytes) -> str:
        """Only closed words: a profile method and a registered tool name, else a fixed stand-in."""
        try:
            message = parse_message(body)
        except McpWireError:
            return _UNKNOWN_METHOD
        method = getattr(message, "method", None)
        if method not in _KNOWN_METHODS:
            return _UNKNOWN_METHOD
        if method != McpMethod.TOOLS_CALL:
            return method
        name = message.params.get("name")
        return f"{method} {name if name in self._tools else _UNKNOWN_TOOL}"


async def _handled(tool: McpTool, verified: _Verified, arguments: JsonObject) -> ToolOutcome:
    try:
        return await tool.handler(verified.claims, arguments)
    except Exception as failure:
        formatted = "".join(traceback.format_exception(failure))
        if not carries_signature_of(verified.token, formatted.encode("utf-8")):
            raise
        name = tool.declaration.name
        _LOG.warning("a failure was withheld: tool %s", name)
        raise_without_chain(HandlerFailureWithheld(name))


async def _read_body(receive: Receive) -> bytes | None:
    """The request body, or ``None`` once it is past ``MAX_EVENT_BYTES``."""
    body = bytearray()
    while True:
        message = await receive()
        body += message.get("body", b"")
        if len(body) > MAX_EVENT_BYTES:
            return None
        if message["type"] == "http.disconnect" or not message.get("more_body", False):
            return bytes(body)


async def _write(send: Send, answer: HttpAnswer) -> None:
    headers = [(_CONTENT_LENGTH_FIELD, str(len(answer.body)).encode("ascii"))]
    if answer.content_type is not None:
        headers.append((_CONTENT_TYPE_FIELD, answer.content_type.encode("ascii")))
    headers.extend(_HEADERS_OF_STATUS.get(answer.status, []))
    await send({"type": "http.response.start", "status": int(answer.status), "headers": headers})
    await send({"type": "http.response.body", "body": answer.body})
