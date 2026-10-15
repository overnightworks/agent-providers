"""Light client of the agent service, for the apps that use it.

It owns what an app and the service must agree on without the provider core:
the tool contract an app declares (``agent_client.tools``) and the streamed run
contract (``agent_client.wire``, with its stream rules in
``agent_client.run_stream`` and its SSE framing in ``agent_client.sse``). It
depends on pydantic only, so an app installs it without the provider
machinery.
"""
