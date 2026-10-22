"""The agent service: the one authenticated entrance through which apps start agent runs.

An app sends a purpose, a turn and the tools of the run with two tokens, the end user's and
its own workload's (decision record 0001, H1 and H3). The service verifies both,
decides the route from its one policy (H5), and streams the run's events in
the wire contract of ``agent_runtime_client.wire``. Nothing imports this package: it
builds on the provider core and the agent client, never the reverse.
"""
