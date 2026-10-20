"""The service's own MCP client against the app's server, over TLS under uvicorn."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from bounds import TEST_WAIT_SECONDS
from mcp_server_support import (
    ISSUED_AT,
    VERIFICATION_KEY,
    RecordingHandlers,
    minted_capability,
)
from service_support import (
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    listening_loopback_socket,
    served,
)

from agent_client.mcp_server import McpServer
from agent_client.mcp_wire import ToolOutcome
from agent_client.tools import ToolEffect
from agent_service.app_tools import (
    AppToolsSession,
    ProtocolFailure,
    UpstreamProtocolError,
    UpstreamRefused,
    open_app_tools,
)
from agent_service.capability import Capability

CERTIFICATE_HOST = "localhost"


async def no_host_routes(scope: Any, receive: Any, send: Any) -> None:
    raise AssertionError("the host app is not reached by an MCP request")


@dataclass(frozen=True)
class RunningApp:
    origin: str
    handlers: RecordingHandlers
    tls_context: ssl.SSLContext

    def with_session(
        self, capability: Capability, action: Callable[[AppToolsSession], Awaitable[Any]]
    ) -> Any:
        async def scenario() -> Any:
            async with asyncio.timeout(TEST_WAIT_SECONDS):
                async with open_app_tools(self.origin, capability, self.tls_context) as session:
                    return await action(session)

        return asyncio.run(scenario())

    def capability(self, origin: str | None = None) -> Capability:
        return minted_capability(origin or self.origin)


@pytest.fixture
def app(authority: Any) -> Iterator[RunningApp]:
    handlers = RecordingHandlers(outcome=ToolOutcome("noted", is_error=False))
    listener = listening_loopback_socket()
    origin = f"https://{CERTIFICATE_HOST}:{listener.getsockname()[1]}"
    tools = [
        handlers.tool("edit_note", ToolEffect.SIDE_EFFECTING),
        handlers.tool("read_note"),
        handlers.tool("other_tool"),
    ]
    server = McpServer(origin, VERIFICATION_KEY, tools, wall_clock=lambda: ISSUED_AT + 1)
    with served(
        server.in_front_of(no_host_routes),
        tls=authority.credentials_for(CERTIFICATE_HOST),
        listener=listener,
    ):
        yield RunningApp(origin, handlers, authority.client_context())


def test_the_service_client_lists_the_allowed_tools_and_calls_one_over_tls(
    app: RunningApp,
) -> None:
    async def list_then_call(session: AppToolsSession) -> tuple[list[str], ToolOutcome]:
        listed = [tool.name for tool in await session.list_tools()]
        return listed, await session.call_tool("read_note", {"note": "n1"})

    listed, outcome = app.with_session(app.capability(), list_then_call)

    assert listed == ["edit_note", "read_note"]
    assert outcome == ToolOutcome("noted", is_error=False)
    [(name, _claims, arguments)] = app.handlers.calls
    assert (name, arguments) == ("read_note", {"note": "n1"})


def test_a_tool_off_the_allowlist_is_an_invalid_params_error_the_client_reads(
    app: RunningApp,
) -> None:
    async def call_other(session: AppToolsSession) -> None:
        await session.call_tool("other_tool", {})

    capability = app.capability()
    with pytest.raises(UpstreamProtocolError) as raised:
        app.with_session(capability, call_other)

    assert raised.value.reason is ProtocolFailure.JSON_RPC_ERROR
    assert raised.value.rpc_error_code == -32602
    assert app.handlers.calls == []


def test_a_capability_for_another_origin_is_refused_with_401(app: RunningApp) -> None:
    async def never(session: AppToolsSession) -> None:
        raise AssertionError("the handshake must be refused")

    capability = app.capability("https://other.internal")
    with pytest.raises(UpstreamRefused) as raised:
        app.with_session(capability, never)

    assert raised.value.status == 401
