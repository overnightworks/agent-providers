"""Light client of the agent service, for the apps that use it.

It owns what an app and the service must agree on without the provider core:
the tool contract an app declares (``agent_client.tools``) and the run contract
(``agent_client.wire``: the request that starts a run and the events it
streams, with its stream rules in ``agent_client.run_stream`` and its SSE
framing in ``agent_client.sse``), and the format of the run capability the
service mints and an app's MCP server verifies (``agent_client.capability``),
and the JSON-only MCP wire profile the service and an app speak
(``agent_client.mcp_wire``).
It depends on pydantic only, so an app
installs it without the provider machinery.
"""
