"""Verifies a run capability for an app: one RS256 public key, one origin, the injected clock.

The service signs the capability (``agent_runtime_client.capability`` owns its format) and an app
trusts one PEM public key exported from the service's signing key. The capability is valid
until ``exp`` and is never revoked, so a verified capability proves the signed run id and
nothing about the run being live: the app's ownership check stays mandatory (decision
record 0001, O4 and H3). Nothing here reads the environment, a file or the network.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from agent_runtime_client.capability import (
    CAPABILITY_TOKEN_TYPE,
    MAX_CAPABILITY_CHARACTERS,
    SIGNING_KEY_BITS,
    SIGNING_KEY_EXPONENT,
    CapabilityClaims,
)
from agent_runtime_client.errors import raise_without_chain
from agent_runtime_client.mcp_wire import read_strict_json

_ALGORITHM: Final = "RS256"
_HEADER_MEMBERS: Final = frozenset({"alg", "kid", "typ"})
_PEM_BEGIN: Final = b"-----BEGIN "
_PUBLIC_KEY_BEGIN: Final = b"-----BEGIN PUBLIC KEY-----"
_PUBLIC_KEY_END: Final = b"-----END PUBLIC KEY-----"
_BASE64URL_SEGMENT: Final = re.compile(r"[A-Za-z0-9_-]*", re.ASCII)
_SEGMENTS: Final = 3


class UnsoundVerificationKey(ValueError):
    """A key the verifier refuses to trust; the message names the rule and never the bytes."""


class CapabilityRefusal(StrEnum):
    """The closed set of reasons a capability is refused, in the order they are checked."""

    TOO_LONG = "too_long"
    MALFORMED = "malformed"
    UNEXPECTED_HEADER = "unexpected_header"
    WRONG_TYPE = "wrong_type"
    INVALID_SIGNATURE = "invalid_signature"
    INVALID_CLAIMS = "invalid_claims"
    WRONG_AUDIENCE = "wrong_audience"
    EXPIRED = "expired"
    ISSUED_IN_FUTURE = "issued_in_future"


@dataclass(frozen=True, eq=False)
class CapabilityVerificationKey:
    """The one RSA-3072 key (exponent 65537) an app trusts; its repr shows no key material."""

    public_key: rsa.RSAPublicKey = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.public_key, rsa.RSAPublicKey):
            raise_without_chain(UnsoundVerificationKey("is not an RSA public key"))
        if self.public_key.key_size != SIGNING_KEY_BITS:
            raise_without_chain(
                UnsoundVerificationKey(f"must be an RSA key of {SIGNING_KEY_BITS} bits")
            )
        if self.public_key.public_numbers().e != SIGNING_KEY_EXPONENT:
            raise_without_chain(
                UnsoundVerificationKey(f"must have public exponent {SIGNING_KEY_EXPONENT}")
            )

    @classmethod
    def from_pem(cls, pem: bytes) -> CapabilityVerificationKey:
        """Accepts exactly one PEM ``PUBLIC KEY`` block of an RSA-3072 key, exponent 65537."""
        reason = _unsound_pem_reason(pem)
        if reason is not None:
            raise_without_chain(UnsoundVerificationKey(reason))
        try:
            public_key = serialization.load_pem_public_key(pem)
        except (ValueError, UnsupportedAlgorithm):
            raise_without_chain(UnsoundVerificationKey("is not a PEM public key"))
        return cls(public_key)


def _unsound_pem_reason(pem: bytes) -> str | None:
    if pem.count(_PEM_BEGIN) != 1 or pem.count(_PUBLIC_KEY_END) != 1:
        return "must hold exactly one PEM block"
    block = pem.strip()
    if not (block.startswith(_PUBLIC_KEY_BEGIN) and block.endswith(_PUBLIC_KEY_END)):
        return "must be a PEM PUBLIC KEY block and nothing else"
    return None


def verify_capability(
    token: str, *, key: CapabilityVerificationKey, origin: str, now: float
) -> CapabilityClaims | CapabilityRefusal:
    """The claims of a sound capability for ``origin`` at ``now``, else the first failing check.

    ``now`` is the app's clock in epoch seconds, without leeway: the app shares the service's
    host clock. Never raises for token content, and no refusal carries any part of the token.
    """
    if len(token) > MAX_CAPABILITY_CHARACTERS:
        return CapabilityRefusal.TOO_LONG
    segments = _decoded_segments(token)
    if segments is None:
        return CapabilityRefusal.MALFORMED
    header = _strict_json(segments[0])
    if not isinstance(header, dict):
        return CapabilityRefusal.MALFORMED
    if not _is_expected_header(header):
        return CapabilityRefusal.UNEXPECTED_HEADER
    if header["typ"] != CAPABILITY_TOKEN_TYPE:
        return CapabilityRefusal.WRONG_TYPE
    try:
        payload = jwt.PyJWS().decode(token, key.public_key, algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return CapabilityRefusal.INVALID_SIGNATURE
    claims = _claims_of(payload)
    if claims is None:
        return CapabilityRefusal.INVALID_CLAIMS
    if claims.aud != origin:
        return CapabilityRefusal.WRONG_AUDIENCE
    if now >= claims.exp:
        return CapabilityRefusal.EXPIRED
    if now < claims.iat:
        return CapabilityRefusal.ISSUED_IN_FUTURE
    return claims


def _decoded_segments(token: str) -> list[bytes] | None:
    texts = token.split(".")
    if len(texts) != _SEGMENTS:
        return None
    decoded = []
    for text in texts:
        segment = _canonical_base64url(text)
        if segment is None:
            return None
        decoded.append(segment)
    return decoded


def _canonical_base64url(text: str) -> bytes | None:
    if _BASE64URL_SEGMENT.fullmatch(text) is None:
        return None
    try:
        decoded = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error:
        return None
    return decoded if base64.urlsafe_b64encode(decoded).rstrip(b"=") == text.encode() else None


def _strict_json(text: bytes) -> Any:
    try:
        return read_strict_json(text.decode("utf-8"))
    except ValueError:
        return None


def _is_expected_header(header: dict[str, Any]) -> bool:
    return (
        header.keys() == _HEADER_MEMBERS
        and header["alg"] == _ALGORITHM
        and isinstance(header["kid"], str)
    )


def _claims_of(payload: bytes) -> CapabilityClaims | None:
    document = _strict_json(payload)
    if not isinstance(document, dict):
        return None
    try:
        return CapabilityClaims.from_claims(document)
    except ValidationError:
        return None
