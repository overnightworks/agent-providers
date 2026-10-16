"""The service's one signing key: what a sound key is, how it is read, and how it signs.

The operator creates the key file by hand (decision record 0001, A1a4 ruling);
the service only reads it, once, and refuses one it cannot trust. Nothing here
writes, re-reads or reloads a key, and no message carries key material.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

SIGNING_KEY_BITS: Final = 3072
SIGNING_KEY_EXPONENT: Final = 65537
MAX_KEY_FILE_BYTES: Final = 16 * 1024
_PEM_BLOCK_START: Final = b"-----BEGIN "
_OPEN_FLAGS: Final = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC


class UnsoundSigningKey(ValueError):
    """A key file the service refuses to sign with; the message is the service's own words."""


class EmptyTokenType(ValueError):
    """A token must say what it is for; an empty ``typ`` would silently drop the header."""


def shown_path(path: str) -> str:
    """The path with control and non-printing characters escaped, so a message stays one line."""
    return "".join(map(_shown_character, path))


def _shown_character(character: str) -> str:
    return character if character.isprintable() else character.encode("unicode_escape").decode()


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
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise UnsoundSigningKey("is not an RSA key")
        if private_key.key_size != SIGNING_KEY_BITS:
            raise UnsoundSigningKey(
                f"is an RSA key of {private_key.key_size} bits; it must have {SIGNING_KEY_BITS}"
            )
        exponent = private_key.public_key().public_numbers().e
        if exponent != SIGNING_KEY_EXPONENT:
            raise UnsoundSigningKey(
                f"has public exponent {exponent}; it must be {SIGNING_KEY_EXPONENT}"
            )
        public_key = private_key.public_key()
        return cls(key_id(public_key), public_key, private_key)

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
    shown = shown_path(str(path))
    try:
        return SigningKey.from_private_key(_parsed_private_key(_read_key_file(path)))
    except OSError as error:
        raise UnsoundSigningKey(f"{shown} cannot be read: {error.strerror}") from error
    except UnsoundSigningKey as refusal:
        raise UnsoundSigningKey(f"{shown} {refusal}") from None


def _read_key_file(path: Path) -> bytes:
    descriptor = os.open(path, _OPEN_FLAGS)  # NOSONAR The operator names this file.
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise UnsoundSigningKey("is not a regular file")
        if status.st_mode & 0o077:
            raise UnsoundSigningKey(
                f"is open to group or others (mode {stat.S_IMODE(status.st_mode):04o});"
                " a key file must grant them nothing, for example 0600 or 0400"
            )
        if status.st_size > MAX_KEY_FILE_BYTES:
            raise UnsoundSigningKey(f"is larger than {MAX_KEY_FILE_BYTES} bytes")
        contents = _read_at_most(descriptor, MAX_KEY_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(contents) > MAX_KEY_FILE_BYTES:
        raise UnsoundSigningKey(f"is larger than {MAX_KEY_FILE_BYTES} bytes")
    return contents


def _read_at_most(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining and (chunk := os.read(descriptor, remaining)):
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _parsed_private_key(contents: bytes) -> object:
    if contents.count(_PEM_BLOCK_START) > 1:
        raise UnsoundSigningKey("holds more than one PEM block; it must hold exactly one")
    try:
        return serialization.load_pem_private_key(contents, password=None)
    except TypeError:
        raise UnsoundSigningKey("is encrypted; it must be an unencrypted PEM") from None
    except UnsupportedAlgorithm:
        raise UnsoundSigningKey("uses a key algorithm the service does not support") from None
    except ValueError:
        raise UnsoundSigningKey("is not an unencrypted PEM private key") from None
