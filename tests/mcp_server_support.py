"""Shared arrangement for the MCP server suites: one trusted key, minted capabilities, tools."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from service_support import generated_rsa_key

from agent_client.capability import CapabilityClaims
from agent_client.mcp_server import CapabilityVerificationKey, McpTool
from agent_client.mcp_wire import ToolOutcome
from agent_client.tools import ToolDeclaration, ToolEffect
from agent_client.wire import JsonObject
from agent_service.capability import (
    Capability,
    CapabilityScope,
    capability_claims,
    signed_capability,
)
from agent_service.signing import SigningKey

ISSUED_AT = 1_792_152_000
LIFETIME = 600
RUN_ID = UUID("8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01")
OTHER_RUN_ID = UUID("1d4e7a90-5c3b-4f2a-8e6d-0b9c7a5f3e21")
ALLOWED_TOOLS = ("read_note", "edit_note", "ghost_tool")
DONE = ToolOutcome("done", is_error=False)

_PRIVATE_KEY = generated_rsa_key()
SIGNING_KEY = SigningKey.from_private_key(_PRIVATE_KEY)
VERIFICATION_KEY = CapabilityVerificationKey.from_pem(
    _PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
)


def minted_capability(
    origin: str,
    *,
    run_id: UUID = RUN_ID,
    tools: Sequence[str] = ALLOWED_TOOLS,
    issued_at: float = ISSUED_AT,
) -> Capability:
    scope = CapabilityScope(origin, tuple(tools), SIGNING_KEY)
    claims = capability_claims(
        scope,
        run_id=run_id,
        app="notes",
        issuer="https://keycloak.test/realms/agent-test",
        subject="user-alice",
        purpose="notes.assistant",
        now=issued_at,
        max_duration_s=LIFETIME,
    )
    return signed_capability(scope, claims)


def signature_segment_of(capability: Capability) -> str:
    return capability.token.rsplit(".", 1)[-1]


def declaration(name: str, effect: ToolEffect = ToolEffect.READ_ONLY) -> ToolDeclaration:
    return ToolDeclaration(
        name=name,
        description=f"{name} for tests",
        parameters={"type": "object", "properties": {"note": {"type": "string"}}},
        effect=effect,
    )


@dataclass
class RecordingHandlers:
    """Tool handlers that record each call and answer with a settable outcome or failure."""

    calls: list[tuple[str, CapabilityClaims, JsonObject]] = field(default_factory=list)
    outcome: ToolOutcome = DONE
    failure: Exception | None = None

    def tool(self, name: str, effect: ToolEffect = ToolEffect.READ_ONLY) -> McpTool:
        async def handler(claims: CapabilityClaims, arguments: JsonObject) -> ToolOutcome:
            self.calls.append((name, claims, arguments))
            if self.failure is not None:
                raise self.failure
            return self.outcome

        return McpTool(declaration(name, effect), handler)
