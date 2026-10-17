"""A synthetic ``Capability`` for tests that need an object but no signing key."""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence

from agent_client.capability import CAPABILITY_ISSUER, CapabilityClaims, CapabilityPrincipal
from agent_service.capability import Capability

_RUN_ID = "8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01"
_ISSUED_AT = 1_792_152_000


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def canary_token() -> str:
    """An RS256-shaped token built at run time: its signature segment holds ``-`` and ``_``."""
    header = _base64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _base64url(json.dumps({"sub": "canary"}).encode())
    signature = _base64url(bytes(range(256)) + bytes(reversed(range(128))))
    return ".".join([header, payload, signature])


def capability_carrying(token: str, tools: Sequence[str] = ("echo",)) -> Capability:
    claims = CapabilityClaims(
        iss=CAPABILITY_ISSUER,
        aud="https://app.example",
        sub=_RUN_ID,
        iat=_ISSUED_AT,
        exp=_ISSUED_AT + 600,
        app="example-app",
        purpose="assist",
        principal=CapabilityPrincipal(iss="https://issuer.example", sub="user-alice"),
        tools=tuple(tools),
    )
    return Capability(claims, token)
