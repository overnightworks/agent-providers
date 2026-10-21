"""The app side of the JSON-only MCP profile; it needs the ``mcp-server`` extra.

No base module of ``agent_client`` imports this package, so the base keeps its two
dependencies, pydantic and ruamel.yaml.
"""

from agent_client.mcp_server.app import HandlerFailureWithheld, McpServer, McpTool
from agent_client.mcp_server.verification import (
    CapabilityRefusal,
    CapabilityVerificationKey,
    UnsoundVerificationKey,
    verify_capability,
)

__all__ = [
    "HandlerFailureWithheld",
    "McpServer",
    "McpTool",
    "CapabilityRefusal",
    "CapabilityVerificationKey",
    "UnsoundVerificationKey",
    "verify_capability",
]
