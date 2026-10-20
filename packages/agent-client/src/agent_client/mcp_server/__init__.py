"""The app side of the JSON-only MCP profile; it needs the ``mcp-server`` extra.

No base module of ``agent_client`` imports this package, so the base stays pydantic only.
"""

from agent_client.mcp_server.verification import (
    CapabilityRefusal,
    CapabilityVerificationKey,
    UnsoundVerificationKey,
    verify_capability,
)

__all__ = [
    "CapabilityRefusal",
    "CapabilityVerificationKey",
    "UnsoundVerificationKey",
    "verify_capability",
]
