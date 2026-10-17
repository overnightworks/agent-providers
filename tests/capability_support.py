"""A synthetic ``Capability`` for tests that need an object but no signing key."""

from __future__ import annotations

from agent_client.capability import CAPABILITY_ISSUER, CapabilityClaims, CapabilityPrincipal
from agent_service.capability import Capability

_RUN_ID = "8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01"
_ISSUED_AT = 1_792_152_000


def capability_carrying(token: str) -> Capability:
    claims = CapabilityClaims(
        iss=CAPABILITY_ISSUER,
        aud="https://app.example",
        sub=_RUN_ID,
        iat=_ISSUED_AT,
        exp=_ISSUED_AT + 600,
        app="songmaker",
        purpose="cowriter",
        principal=CapabilityPrincipal(iss="https://issuer.example", sub="user-alice"),
        tools=("echo",),
    )
    return Capability(claims, token)
