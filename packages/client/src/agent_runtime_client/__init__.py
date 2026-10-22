"""Light client of the agent service, for the apps that use it.

It owns what an app and the service must agree on without the provider core:
the tool contract an app declares (``agent_runtime_client.tools``) and the run contract
(``agent_runtime_client.wire``: the request that starts a run and the events it
streams, with its stream rules in ``agent_runtime_client.run_stream`` and its SSE
framing in ``agent_runtime_client.sse``), and the format of the run capability the
service mints and an app's MCP server verifies (``agent_runtime_client.capability``),
and the JSON-only MCP wire profile the service and an app speak
(``agent_runtime_client.mcp_wire``), and the agent file an app ships
(``agent_runtime_client.agents``).
Its base depends on pydantic and ruamel.yaml only, so an app
installs it without the provider machinery; the ``mcp-server`` extra adds PyJWT with
cryptography for ``agent_runtime_client.mcp_server``, which no base module imports.
"""
