"""What a run's capability says and whether it fits (decision record 0001, A1a4 ruling).

``capability_claims`` is the one place the claim values and the expiry
arithmetic live; ``signed_capability`` is the one decider of whether a signed
capability fits the 8,192 characters every verifier accepts. The configuration
preflight measures through the same two functions with reserved values, so the
measured length is the production length. Nothing here mints a capability for a
run: that is admission (A1a4-2b).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from agent_client.capability import (
    CAPABILITY_ISSUER,
    CAPABILITY_TOKEN_TYPE,
    MAX_CAPABILITY_CHARACTERS,
    CapabilityClaims,
    CapabilityPrincipal,
)
from agent_service.signing import SigningKey

PREFLIGHT_SUBJECT: Final = '"' * 255
PREFLIGHT_ISSUED_AT: Final = 9_999_999_999
PREFLIGHT_RUN_ID: Final = UUID(int=0)


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
    if len(token) > MAX_CAPABILITY_CHARACTERS:
        raise CapabilityTooLarge(len(token))
    return Capability(claims, token)


def preflight_claims(
    scope: CapabilityScope, *, app: str, purpose: str, issuer: str, max_duration_s: float
) -> CapabilityClaims:
    return capability_claims(
        scope,
        run_id=PREFLIGHT_RUN_ID,
        app=app,
        issuer=issuer,
        subject=PREFLIGHT_SUBJECT,
        purpose=purpose,
        now=PREFLIGHT_ISSUED_AT,
        max_duration_s=max_duration_s,
    )
