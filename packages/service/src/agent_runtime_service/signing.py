"""The service's one signing key: what a sound key is, how it is read, and how it signs.

The operator creates the key file by hand (decision record 0001, A1a4 ruling);
the service only reads it, once, and refuses one it cannot trust. Nothing here
writes, re-reads or reloads a key, and no message carries key material.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from agent_runtime.secret_file import UnsoundSecretFile, read_secret_file, shown_text
from agent_runtime_client.capability import SIGNING_KEY_BITS, SIGNING_KEY_EXPONENT

_PEM_BLOCK_START: Final = b"-----BEGIN "


class UnsoundSigningKey(ValueError):
    """A key file the service refuses to sign with; the message is the service's own words."""


class EmptyTokenType(ValueError):
    """A token must say what it is for; an empty ``typ`` would silently drop the header."""


def key_id(public_key: rsa.RSAPublicKey) -> str:
    """The RFC 7638 SHA-256 thumbprint: members ``e``, ``kty``, ``n`` in order, no whitespace."""
    jwk = RSAAlgorithm.to_jwk(public_key, as_dict=True)
    required_members = {name: jwk[name] for name in ("e", "kty", "n")}
    canonical = json.dumps(required_members, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, eq=False)
class SigningKey:
    """An RSA-3072 key with exponent 65537; its repr names only the ``kid``."""

    kid: str
    public_key: rsa.RSAPublicKey = field(repr=False)
    private_key: rsa.RSAPrivateKey = field(repr=False)

    @classmethod
    def from_private_key(cls, private_key: object) -> SigningKey:
        reason = _unsound_reason(private_key)
        if reason is not None:
            raise UnsoundSigningKey(reason)
        return _signing_key_of(cls, private_key)

    def sign(self, claims: Mapping[str, object], token_type: str) -> str:
        if not token_type:
            raise EmptyTokenType("a token type must not be empty")
        return jwt.encode(
            dict(claims),
            self.private_key,
            algorithm="RS256",
            headers={"kid": self.kid, "typ": token_type},
        )


def load_signing_key(path: Path) -> SigningKey:
    """Reads the file through one descriptor and raises ``UnsoundSigningKey`` for any refusal."""
    try:
        outcome = _signing_key_in_pem(read_secret_file(path, "key file"))
    except UnsoundSecretFile as refusal:
        raise UnsoundSigningKey(str(refusal)) from None
    if isinstance(outcome, _KeyRefusal):
        raise UnsoundSigningKey(f"{shown_text(str(path))} {outcome.reason}")
    return outcome


@dataclass(frozen=True)
class _KeyRefusal:
    """Returned, never raised: the frames that held the key are gone before anyone raises."""

    reason: str


def _signing_key_in_pem(contents: bytes) -> SigningKey | _KeyRefusal:
    if contents.count(_PEM_BLOCK_START) > 1:
        return _KeyRefusal("holds more than one PEM block; it must hold exactly one")
    try:
        parsed = serialization.load_pem_private_key(contents, password=None)
    except TypeError:
        return _KeyRefusal("is encrypted; it must be an unencrypted PEM")
    except UnsupportedAlgorithm:
        return _KeyRefusal("uses a key algorithm the service does not support")
    except ValueError:
        return _KeyRefusal("is not an unencrypted PEM private key")
    reason = _unsound_reason(parsed)
    if reason is not None:
        return _KeyRefusal(reason)
    return _signing_key_of(SigningKey, parsed)


def _unsound_reason(private_key: object) -> str | None:
    if not isinstance(private_key, rsa.RSAPrivateKey):
        return "is not an RSA key"
    if private_key.key_size != SIGNING_KEY_BITS:
        return f"is an RSA key of {private_key.key_size} bits; it must have {SIGNING_KEY_BITS}"
    exponent = private_key.public_key().public_numbers().e
    if exponent != SIGNING_KEY_EXPONENT:
        return f"has public exponent {exponent}; it must be {SIGNING_KEY_EXPONENT}"
    return None


def _signing_key_of[KeyT: SigningKey](key_type: type[KeyT], private_key: rsa.RSAPrivateKey) -> KeyT:
    public_key = private_key.public_key()
    return key_type(key_id(public_key), public_key, private_key)
