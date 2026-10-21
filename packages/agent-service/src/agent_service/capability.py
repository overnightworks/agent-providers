"""What a run's capability says and whether it fits (decision record 0001, A1a4 ruling).

``capability_claims`` is the one place the claim values and the expiry
arithmetic live; ``signed_capability`` is the one decider of whether a signed
capability fits the 8,192 characters every verifier accepts. Nothing here mints
a capability for a run: that is admission (A1a4-2b).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from uuid import UUID

from agent_client.capability import (
    CAPABILITY_ISSUER,
    CAPABILITY_TOKEN_TYPE,
    MAX_CAPABILITY_CHARACTERS,
    CapabilityClaims,
    CapabilityPrincipal,
    carries_signature_of,
)
from agent_service.signing import SigningKey


class CapabilityTooLarge(Exception):
    """The compact capability is longer than a verifier accepts; the token is dropped."""

    def __init__(self, length: int) -> None:
        super().__init__(f"a capability of {length} characters exceeds {MAX_CAPABILITY_CHARACTERS}")
        self.length = length


@dataclass(frozen=True)
class CapabilityScope:
    """What one app's capabilities are bound to and signed with."""

    origin: str
    tools: tuple[str, ...]
    key: SigningKey = field(repr=False)


@dataclass(frozen=True)
class Capability:
    claims: CapabilityClaims
    token: str = field(repr=False)

    def appears_in(self, data: bytes) -> bool:
        """Whether ``data`` carries this capability, whole or as its signature segment."""
        return carries_signature_of(self.token, data)


def capability_claims(
    scope: CapabilityScope,
    *,
    run_id: UUID,
    app: str,
    issuer: str,
    subject: str,
    purpose: str,
    now: float,
    max_duration_s: float,
) -> CapabilityClaims:
    return CapabilityClaims(
        iss=CAPABILITY_ISSUER,
        aud=scope.origin,
        sub=str(run_id),
        iat=math.floor(now),
        exp=math.floor(now + max_duration_s),
        app=app,
        purpose=purpose,
        principal=CapabilityPrincipal(iss=issuer, sub=subject),
        tools=scope.tools,
    )


def signed_capability(scope: CapabilityScope, claims: CapabilityClaims) -> Capability:
    token = scope.key.sign(claims.model_dump(mode="json"), CAPABILITY_TOKEN_TYPE)
    length = len(token)
    if length > MAX_CAPABILITY_CHARACTERS:
        del token  # the refusal's traceback keeps this frame alive
        raise CapabilityTooLarge(length)
    return Capability(claims, token)
