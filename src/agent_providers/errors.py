"""Named co-writer route failures. No adapter exposes upstream error text."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, NoReturn

from pydantic import BaseModel

if TYPE_CHECKING:
    from agent_providers.text_tool_protocol import TextToolRejection


class SafeRouteReasonCode(StrEnum):
    API_KEY_NOT_SET = "api_key_not_set"
    CLI_LOGIN_NOT_CONFIGURED = "cli_login_not_configured"
    CLI_AUTH_REJECTED = "cli_auth_rejected"
    CLI_BINARY_UNAVAILABLE = "cli_binary_unavailable"
    CLI_CAPACITY_EXHAUSTED = "cli_capacity_exhausted"
    CLI_PROTOCOL_ERROR = "cli_protocol_error"
    API_HTTP_ERROR = "api_http_error"
    API_PROTOCOL_ERROR = "api_protocol_error"
    CATALOGUE_HTTP_ERROR = "catalogue_http_error"
    CATALOGUE_PROTOCOL_ERROR = "catalogue_protocol_error"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    TOOL_PROTOCOL_ERROR = "tool_protocol_error"
    TOOL_LIMIT_EXCEEDED = "tool_limit_exceeded"
    NO_IMAGE_TOOL = "no_image_tool"
    ROUTE_FAILED = "route_failed"


class ToolProtocolRejectionCode(StrEnum):
    TOOL_RESULT_BATCH_INVALID = "tool_result_batch_invalid"
    TEXT_TOOL_RESPONSE_INVALID = "text_tool_response_invalid"
    NATIVE_TOOL_BLOCKED = "native_tool_blocked"
    TOOL_LOOP_TERMINAL_INVALID = "tool_loop_terminal_invalid"
    CLI_STREAM_RESPONSE_INVALID = "cli_stream_response_invalid"


_SAFE_MESSAGES: dict[SafeRouteReasonCode, str] = {
    SafeRouteReasonCode.API_KEY_NOT_SET: "API key is not set.",
    SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED: "CLI is not signed in.",
    SafeRouteReasonCode.CLI_AUTH_REJECTED: "CLI login was rejected or has expired.",
    SafeRouteReasonCode.CLI_BINARY_UNAVAILABLE: "CLI is unavailable.",
    SafeRouteReasonCode.CLI_CAPACITY_EXHAUSTED: "Codex is busy. Try again shortly.",
    SafeRouteReasonCode.CLI_PROTOCOL_ERROR: "CLI returned an invalid response.",
    SafeRouteReasonCode.API_HTTP_ERROR: "API request failed.",
    SafeRouteReasonCode.API_PROTOCOL_ERROR: "API returned an invalid response.",
    SafeRouteReasonCode.CATALOGUE_HTTP_ERROR: "Model catalogue request failed.",
    SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR: "Model catalogue response was invalid.",
    SafeRouteReasonCode.TOOL_EXECUTION_FAILED: "Tool failed.",
    SafeRouteReasonCode.TOOL_PROTOCOL_ERROR: "Tool response was invalid.",
    SafeRouteReasonCode.TOOL_LIMIT_EXCEEDED: "Tool-call limit was reached.",
    SafeRouteReasonCode.NO_IMAGE_TOOL: "no image tool",
    SafeRouteReasonCode.ROUTE_FAILED: "Selected route failed.",
}


class SafeRouteReason(BaseModel):
    code: SafeRouteReasonCode
    message: str


def normalize_route_failure(kind: SafeRouteReasonCode) -> SafeRouteReason:
    """Return the only public representation of a route failure."""
    return SafeRouteReason(code=kind, message=_SAFE_MESSAGES[kind])


class ProviderError(Exception):
    def __init__(self, provider: str, message: str):
        self.provider = provider
        super().__init__(message)


class ProviderUnavailableError(ProviderError):
    def __init__(
        self,
        provider: str,
        route_or_message: str,
        reason: SafeRouteReason | None = None,
    ):
        self.route = route_or_message if reason is not None else None
        self.reason = reason
        super().__init__(provider, reason.message if reason is not None else route_or_message)


class TextToolCallRejectedError(ProviderUnavailableError):
    """A text tool call was refused; ``rejection`` says which rule and where.

    The reason a host shows its user is the generic tool-protocol sentence.
    The refused text sits only in ``rejection.excerpt``, masked from display.
    """

    def __init__(self, provider: str, route: str, rejection: TextToolRejection) -> None:
        super().__init__(
            provider,
            route,
            normalize_route_failure(SafeRouteReasonCode.TOOL_PROTOCOL_ERROR),
        )
        self.rejection = rejection

    def __reduce__(self) -> tuple[type[TextToolCallRejectedError], tuple[object, ...]]:
        return (type(self), (self.provider, self.route, self.rejection))


def raise_without_chain(error: BaseException) -> NoReturn:
    """Raise ``error`` with no ``__context__`` or ``__cause__``, even from a handler.

    An exception raised while the caller handles another one is chained to it
    implicitly, and a chain can carry the very text the error must not.
    """
    try:
        raise error
    except BaseException:
        error.__context__ = None
        error.__cause__ = None
        raise


class ProviderModelCatalogUnavailableError(ProviderError):
    def __init__(
        self,
        provider: str,
        message: str,
        reason: SafeRouteReason | None = None,
    ):
        self.reason = reason
        super().__init__(provider, reason.message if reason is not None else message)


class CodexProcessPoolSaturatedError(Exception):
    """Codex admission was refused before a CLI process could start."""

    def __init__(self, *, kind: str, scope: str) -> None:
        self.kind = kind
        self.scope = scope
        super().__init__(f"Codex {scope} process capacity is busy")
