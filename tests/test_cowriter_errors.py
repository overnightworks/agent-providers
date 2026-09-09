"""The public route-failure vocabulary is stable and secret-free."""

from __future__ import annotations

import asyncio

import pytest

from agent_providers import dispatch
from agent_providers.errors import (
    ProviderUnavailableError,
    SafeRouteReasonCode,
    ToolProtocolRejectionCode,
    normalize_route_failure,
)


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (SafeRouteReasonCode.NO_IMAGE_TOOL, "no image tool"),
        (SafeRouteReasonCode.API_KEY_NOT_SET, "API key is not set."),
        (SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED, "CLI is not signed in."),
        (SafeRouteReasonCode.CLI_AUTH_REJECTED, "CLI login was rejected or has expired."),
        (SafeRouteReasonCode.CLI_BINARY_UNAVAILABLE, "CLI is unavailable."),
        (SafeRouteReasonCode.CLI_PROTOCOL_ERROR, "CLI returned an invalid response."),
        (SafeRouteReasonCode.API_HTTP_ERROR, "API request failed."),
        (SafeRouteReasonCode.API_PROTOCOL_ERROR, "API returned an invalid response."),
        (SafeRouteReasonCode.CATALOGUE_HTTP_ERROR, "Model catalogue request failed."),
        (SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR, "Model catalogue response was invalid."),
        (SafeRouteReasonCode.TOOL_EXECUTION_FAILED, "Co-Writer tool failed."),
        (SafeRouteReasonCode.TOOL_PROTOCOL_ERROR, "Co-Writer tool response was invalid."),
        (SafeRouteReasonCode.TOOL_LIMIT_EXCEEDED, "Co-Writer tool-call limit was reached."),
        (SafeRouteReasonCode.ROUTE_FAILED, "Selected route failed."),
    ],
)
def test_route_failure_code_has_its_exact_safe_message(code, message):
    assert normalize_route_failure(code).model_dump() == {"code": code, "message": message}


def test_tool_protocol_rejection_codes_are_closed() -> None:
    assert {code.value for code in ToolProtocolRejectionCode} == {
        "tool_result_batch_invalid",
        "text_tool_response_invalid",
        "native_tool_blocked",
        "tool_loop_terminal_invalid",
        "cli_stream_response_invalid",
    }


def test_terminal_tool_loop_rejection_logs_a_closed_code_without_input(caplog) -> None:
    marker = "synthetic-confidential-input"

    class EmptyTransport:
        async def stream(self, _message):
            if False:
                yield None

        async def aclose(self) -> None:
            return None

    async def collect() -> None:
        with pytest.raises(ProviderUnavailableError) as raised:
            async for _ in dispatch._stream_cli_tool_turn(
                provider="grok",
                system=marker,
                messages=[],
                executor=lambda _name, _arguments: None,
                transport=EmptyTransport(),
                correlation_id=None,
            ):
                pass
        assert raised.value.reason.code is SafeRouteReasonCode.TOOL_PROTOCOL_ERROR

    caplog.set_level("WARNING", logger="agent_providers.dispatch")
    asyncio.run(collect())
    assert ToolProtocolRejectionCode.TOOL_LOOP_TERMINAL_INVALID in caplog.text
    assert marker not in caplog.text
