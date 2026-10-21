"""An app verifies a run capability against one trusted public key, its origin and its clock."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from service_support import generated_rsa_key, pem_of

from agent_client.capability import CAPABILITY_TOKEN_TYPE, MAX_CAPABILITY_CHARACTERS
from agent_client.mcp_server import (
    CapabilityRefusal,
    CapabilityVerificationKey,
    UnsoundVerificationKey,
    verify_capability,
)
from agent_service.capability import (
    CapabilityScope,
    capability_claims,
    preflight_claims,
    signed_capability,
)
from agent_service.signing import SigningKey

ORIGIN = "https://notes.internal"
OTHER_ORIGIN = "https://other.internal"
ISSUED_AT = 1_792_152_000
LIFETIME = 600
EXPIRES_AT = ISSUED_AT + LIFETIME
RUN_ID = UUID("8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01")
TOOLS = ("edit_note", "read_note")

TRUSTED_PRIVATE_KEY = generated_rsa_key()
OTHER_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=3072)
TRUSTED_SIGNING_KEY = SigningKey.from_private_key(TRUSTED_PRIVATE_KEY)


def _verification_key(private_key: Any) -> CapabilityVerificationKey:
    return CapabilityVerificationKey.from_pem(_public_pem(private_key))


def _public_pem(private_key: Any) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


TRUSTED = _verification_key(TRUSTED_PRIVATE_KEY)
OTHER = _verification_key(OTHER_PRIVATE_KEY)
SCOPE = CapabilityScope(ORIGIN, TOOLS, TRUSTED_SIGNING_KEY)


def _minted_claims(now: float = ISSUED_AT):
    return capability_claims(
        SCOPE,
        run_id=RUN_ID,
        app="notes",
        issuer="https://keycloak.test/realms/agent-test",
        subject="user-alice",
        purpose="notes.assistant",
        now=now,
        max_duration_s=LIFETIME,
    )


def _minted_token() -> str:
    return signed_capability(SCOPE, _minted_claims()).token


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _json_bytes(document: object) -> bytes:
    return json.dumps(document, separators=(",", ":")).encode()


GOOD_HEADER: dict[str, Any] = {"alg": "RS256", "kid": "key-1", "typ": CAPABILITY_TOKEN_TYPE}


def _claims_with(**overrides: object) -> dict[str, object]:
    claims = _minted_claims().model_dump(mode="json")
    merged = {**claims, **overrides}
    return {name: value for name, value in merged.items() if value is not None}


def _signed_token(
    header: Mapping[str, object] | bytes = GOOD_HEADER,
    payload: Mapping[str, object] | bytes | None = None,
    *,
    signer: Any = TRUSTED_PRIVATE_KEY,
) -> str:
    header_bytes = header if isinstance(header, bytes) else _json_bytes(header)
    payload = _claims_with() if payload is None else payload
    payload_bytes = payload if isinstance(payload, bytes) else _json_bytes(payload)
    signing_input = f"{_base64url(header_bytes)}.{_base64url(payload_bytes)}"
    signature = signer.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_base64url(signature)}"


def _verified(token: str, *, now: float = ISSUED_AT, origin: str = ORIGIN, key=TRUSTED):
    return verify_capability(token, key=key, origin=origin, now=now)


def test_a_minted_capability_verifies_to_its_claims_when_it_is_issued():
    assert _verified(_minted_token()) == _minted_claims()


def test_a_capability_checked_for_another_origin_is_for_the_wrong_audience():
    assert _verified(_minted_token(), origin=OTHER_ORIGIN) is CapabilityRefusal.WRONG_AUDIENCE


@pytest.mark.parametrize(
    ("now", "outcome"),
    [
        (EXPIRES_AT - 1, _minted_claims()),
        (EXPIRES_AT, CapabilityRefusal.EXPIRED),
        (EXPIRES_AT + 1, CapabilityRefusal.EXPIRED),
    ],
)
def test_a_capability_is_valid_until_the_second_it_expires(now, outcome):
    assert _verified(_minted_token(), now=now) == outcome


def test_a_capability_is_refused_before_the_second_it_is_issued():
    assert _verified(_minted_token(), now=ISSUED_AT - 1) is CapabilityRefusal.ISSUED_IN_FUTURE


def test_the_preflight_capability_is_refused_as_issued_in_the_future():
    token = signed_capability(
        SCOPE,
        preflight_claims(
            SCOPE,
            app="notes",
            issuer="https://k.test",
            max_duration_s=60,
        ),
    ).token

    assert _verified(token) is CapabilityRefusal.ISSUED_IN_FUTURE


def test_a_capability_signed_by_another_key_is_refused_for_its_signature():
    assert _verified(_signed_token(signer=OTHER_PRIVATE_KEY)) is CapabilityRefusal.INVALID_SIGNATURE


def test_a_capability_with_a_flipped_signature_bit_is_refused_for_its_signature():
    header, payload, signature = _minted_token().split(".")
    flipped = bytes([base64.urlsafe_b64decode(signature + "==")[0] ^ 1])
    tampered = _base64url(flipped + base64.urlsafe_b64decode(signature + "==")[1:])

    assert _verified(f"{header}.{payload}.{tampered}") is CapabilityRefusal.INVALID_SIGNATURE


def _token_signed_by(header: Mapping[str, object], sign: Callable[[bytes], bytes]) -> str:
    signing_input = f"{_base64url(_json_bytes(header))}.{_base64url(_json_bytes(_claims_with()))}"
    return f"{signing_input}.{_base64url(sign(signing_input.encode()))}"


def _hs256_token_keyed_with_the_public_pem() -> str:
    public_pem = _public_pem(TRUSTED_PRIVATE_KEY)
    return _token_signed_by(
        {**GOOD_HEADER, "alg": "HS256"},
        lambda signing_input: hmac.new(public_pem, signing_input, hashlib.sha256).digest(),
    )


def _unsigned_token() -> str:
    return _token_signed_by({**GOOD_HEADER, "alg": "none"}, lambda _: b"")


def _without(member: str) -> dict[str, object]:
    return {name: value for name, value in GOOD_HEADER.items() if name != member}


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(_unsigned_token(), id="alg none"),
        pytest.param(
            _hs256_token_keyed_with_the_public_pem(), id="HS256 keyed with the public PEM"
        ),
        pytest.param(_signed_token({**GOOD_HEADER, "alg": "RS512"}), id="RS512"),
        pytest.param(_signed_token({**GOOD_HEADER, "alg": 7}), id="alg not a string"),
        pytest.param(_signed_token(_without("typ")), id="typ absent"),
        pytest.param(_signed_token(_without("kid")), id="kid absent"),
        pytest.param(_signed_token({**GOOD_HEADER, "kid": 7}), id="kid a number"),
        *(
            pytest.param(_signed_token({**GOOD_HEADER, member: value}), id=f"extra {member}")
            for member, value in {
                "jku": "https://keys.example/set",
                "jwk": {"kty": "RSA"},
                "x5u": "https://keys.example/cert",
                "x5c": ["AAAA"],
                "b64": False,
                "crit": ["b64"],
            }.items()
        ),
    ],
)
def test_a_header_other_than_alg_kid_typ_with_rs256_is_refused(token):
    assert _verified(token) is CapabilityRefusal.UNEXPECTED_HEADER


@pytest.mark.parametrize("token_type", ["JWT", "at+jwt", "", None, 1])
def test_a_capability_of_another_token_type_is_refused(token_type):
    assert (
        _verified(_signed_token({**GOOD_HEADER, "typ": token_type})) is CapabilityRefusal.WRONG_TYPE
    )


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"nbf": ISSUED_AT}, id="extra nbf"),
        pytest.param({"tools": None}, id="tools missing"),
        pytest.param({"iss": "someone-else"}, id="another issuer"),
        pytest.param({"exp": ISSUED_AT}, id="exp equal to iat"),
        pytest.param({"exp": ISSUED_AT - 1}, id="exp before iat"),
        pytest.param({"sub": "run-1"}, id="sub not a run id"),
        pytest.param({"sub": str(RUN_ID).upper()}, id="sub an upper-case run id"),
    ],
)
def test_a_validly_signed_payload_that_is_not_a_capability_is_refused_for_its_claims(overrides):
    token = _signed_token(payload=_claims_with(**overrides))

    assert _verified(token) is CapabilityRefusal.INVALID_CLAIMS


def test_a_validly_signed_capability_over_the_character_limit_is_refused_as_too_long():
    token = _signed_token(payload=_claims_with(purpose="p" * MAX_CAPABILITY_CHARACTERS))

    assert len(token) > MAX_CAPABILITY_CHARACTERS
    assert _verified(token) is CapabilityRefusal.TOO_LONG


def test_a_malformed_token_over_the_character_limit_is_refused_as_too_long():
    assert _verified("a" * (MAX_CAPABILITY_CHARACTERS + 1)) is CapabilityRefusal.TOO_LONG


class _PlaceholderSigner:
    """Signs nothing but has the signature's length, to measure a token before signing it."""

    def sign(self, *_: object) -> bytes:
        return bytes(TRUSTED_PRIVATE_KEY.key_size // 8)


def _token_padded_to_the_limit() -> tuple[str, str]:
    """Base64 cannot represent every output length, so a purpose on the limit is searched."""
    for purpose_length in range(MAX_CAPABILITY_CHARACTERS // 2, MAX_CAPABILITY_CHARACTERS):
        purpose = "p" * purpose_length
        payload = _claims_with(purpose=purpose)
        unsigned = _signed_token(payload=payload, signer=_PlaceholderSigner())
        if len(unsigned) >= MAX_CAPABILITY_CHARACTERS:
            return _signed_token(payload=payload), purpose
    raise AssertionError("no purpose reaches the limit")


def test_a_capability_of_exactly_the_character_limit_is_still_verified():
    token, purpose = _token_padded_to_the_limit()

    assert len(token) == MAX_CAPABILITY_CHARACTERS
    assert _verified(token) == _minted_claims().model_copy(update={"purpose": purpose})


NESTED_AS_DEEP_AS_THE_LIMIT_ALLOWS = b"[" * 4_500
VALID_PAYLOAD = _base64url(_json_bytes(_claims_with()))
VALID_SIGNATURE = _base64url(b"signature")


def _segments(header: bytes) -> str:
    return f"{_base64url(header)}.{VALID_PAYLOAD}.{VALID_SIGNATURE}"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("a.b", id="two segments"),
        pytest.param("a.b.c.d", id="four segments"),
        pytest.param(f"!!!.{VALID_PAYLOAD}.{VALID_SIGNATURE}", id="bad base64"),
        pytest.param(f"{_base64url(b'{}')}=.{VALID_PAYLOAD}.{VALID_SIGNATURE}", id="padded base64"),
        pytest.param(f"é.{VALID_PAYLOAD}.{VALID_SIGNATURE}", id="non-ASCII segment"),
        pytest.param(_segments(b"not json"), id="header not JSON"),
        pytest.param(_segments(b"[]"), id="header an array"),
        pytest.param(_segments(b'"RS256"'), id="header a string"),
        pytest.param(_segments(b"null"), id="header null"),
        pytest.param(
            _segments(b'{"alg":"RS256","alg":"RS256","kid":"k","typ":"t"}'), id="duplicate member"
        ),
        pytest.param(_segments(b'{"alg":NaN}'), id="non-JSON constant"),
        pytest.param(_segments(b"\xff\xfe"), id="header not UTF-8"),
        pytest.param(
            _segments(NESTED_AS_DEEP_AS_THE_LIMIT_ALLOWS),
            id="header nested as deep as the limit allows",
        ),
        pytest.param(f"e31.{VALID_PAYLOAD}.{VALID_SIGNATURE}", id="non-canonical base64"),
        pytest.param("", id="empty"),
    ],
)
def test_a_token_that_is_not_three_segments_with_a_json_object_header_is_malformed(token):
    assert _verified(token) is CapabilityRefusal.MALFORMED


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"\xff\xfe\xfd", id="not UTF-8"),
        pytest.param(b"not json", id="not JSON"),
        pytest.param(b"[]", id="an array"),
        pytest.param(b'"capability"', id="a string"),
        pytest.param(b"null", id="null"),
        pytest.param(
            b'{"sub":"' + str(RUN_ID).encode() + b'",' + _json_bytes(_claims_with())[1:],
            id="duplicate member",
        ),
        pytest.param(
            _json_bytes(_claims_with()).replace(b'"exp":', b'"exp":NaN,"x":'),
            id="non-JSON constant",
        ),
        pytest.param(NESTED_AS_DEEP_AS_THE_LIMIT_ALLOWS, id="nested as deep as the limit allows"),
    ],
)
def test_a_validly_signed_payload_that_the_strict_reader_refuses_is_refused_for_its_claims(payload):
    assert _verified(_signed_token(payload=payload)) is CapabilityRefusal.INVALID_CLAIMS


@pytest.mark.parametrize(
    ("token", "now", "origin", "outcome"),
    [
        pytest.param(
            _signed_token({**GOOD_HEADER, "jku": "x"}, signer=OTHER_PRIVATE_KEY),
            ISSUED_AT,
            ORIGIN,
            CapabilityRefusal.UNEXPECTED_HEADER,
            id="header before signature",
        ),
        pytest.param(
            _signed_token({**GOOD_HEADER, "typ": "JWT"}, signer=OTHER_PRIVATE_KEY),
            ISSUED_AT,
            ORIGIN,
            CapabilityRefusal.WRONG_TYPE,
            id="type before signature",
        ),
        pytest.param(
            _signed_token(payload=_claims_with(sub="run-1"), signer=OTHER_PRIVATE_KEY),
            ISSUED_AT,
            ORIGIN,
            CapabilityRefusal.INVALID_SIGNATURE,
            id="signature before claims",
        ),
        pytest.param(
            _signed_token(payload=_claims_with(sub="run-1")),
            EXPIRES_AT,
            OTHER_ORIGIN,
            CapabilityRefusal.INVALID_CLAIMS,
            id="claims before audience",
        ),
        pytest.param(
            _minted_token(),
            EXPIRES_AT,
            OTHER_ORIGIN,
            CapabilityRefusal.WRONG_AUDIENCE,
            id="audience before expiry",
        ),
        pytest.param(
            _minted_token(),
            ISSUED_AT - 1,
            OTHER_ORIGIN,
            CapabilityRefusal.WRONG_AUDIENCE,
            id="audience before issue time",
        ),
    ],
)
def test_two_failures_at_once_are_answered_by_the_earlier_check(token, now, origin, outcome):
    assert _verified(token, now=now, origin=origin) is outcome


def test_replacing_the_trusted_key_refuses_the_old_keys_unexpired_capability():
    old_keys_token = _minted_token()
    new_keys_token = _signed_token(signer=OTHER_PRIVATE_KEY)

    assert _verified(old_keys_token, key=OTHER) is CapabilityRefusal.INVALID_SIGNATURE
    assert _verified(new_keys_token, key=OTHER) == _minted_claims()
    assert _verified(new_keys_token, key=TRUSTED) is CapabilityRefusal.INVALID_SIGNATURE


def test_a_single_spki_public_key_is_trusted():
    assert isinstance(
        CapabilityVerificationKey.from_pem(_public_pem(TRUSTED_PRIVATE_KEY)),
        CapabilityVerificationKey,
    )


def _two_blocks() -> bytes:
    return _public_pem(TRUSTED_PRIVATE_KEY) + _public_pem(OTHER_PRIVATE_KEY)


def _ec_public_pem() -> bytes:
    return _public_pem(ec.generate_private_key(ec.SECP256R1()))


def _pkcs1_public_pem() -> bytes:
    return TRUSTED_PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.PKCS1
    )


@pytest.mark.parametrize(
    "pem",
    [
        pytest.param(pem_of(TRUSTED_PRIVATE_KEY), id="private key"),
        pytest.param(_two_blocks(), id="two blocks"),
        pytest.param(_ec_public_pem(), id="EC key"),
        pytest.param(_pkcs1_public_pem(), id="PKCS#1 block"),
        pytest.param(
            b"-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n", id="garbage body"
        ),
        pytest.param(b"not a pem at all", id="garbage"),
        pytest.param(b"", id="empty"),
        pytest.param(b"junk\n" + _public_pem(TRUSTED_PRIVATE_KEY), id="text before the block"),
        pytest.param(_public_pem(TRUSTED_PRIVATE_KEY) + b"junk\n", id="text after the block"),
        pytest.param(
            _public_pem(TRUSTED_PRIVATE_KEY) + b"junk\n-----END PUBLIC KEY-----\n",
            id="text and a second end marker after the block",
        ),
        pytest.param(
            _public_pem(TRUSTED_PRIVATE_KEY) + b"-----END PUBLIC KEY-----",
            id="a second end marker after the block",
        ),
    ],
)
def test_a_key_that_is_not_one_rsa_public_key_block_is_refused_without_its_bytes(pem):
    with pytest.raises(UnsoundVerificationKey) as refusal:
        CapabilityVerificationKey.from_pem(pem)

    assert isinstance(refusal.value, ValueError)
    assert refusal.value.__cause__ is None
    assert refusal.value.__context__ is None
    body = [
        line for line in pem.decode(errors="ignore").splitlines() if line and "-----" not in line
    ]
    assert not any(line in str(refusal.value) for line in body)


def test_no_representation_of_a_refusal_the_key_or_the_claims_shows_a_token_segment():
    token = _minted_token()
    shown = [
        repr(TRUSTED),
        str(TRUSTED),
        repr(_verified(token)),
        str(_verified(token)),
        *(
            text
            for refusal in (
                _verified(token, origin=OTHER_ORIGIN),
                _verified(token, now=EXPIRES_AT),
                _verified(token, key=OTHER),
            )
            for text in (str(refusal), repr(refusal))
        ),
    ]

    assert not any(segment in text for segment in token.split(".") for text in shown)
    assert "RSAPublicKey" not in repr(TRUSTED)


@pytest.mark.parametrize(
    ("document_part", "outcome"),
    [("header", CapabilityRefusal.MALFORMED), ("payload", CapabilityRefusal.INVALID_CLAIMS)],
)
def test_a_document_nested_past_the_interpreters_recursion_limit_is_refused_not_raised(
    monkeypatch, document_part, outcome
):
    token = _signed_token()
    real_loads = json.loads

    def loads_failing_on_the_part(text: str | bytes, **options: Any) -> Any:
        if isinstance(text, str) and ('"typ"' in text) == (document_part == "header"):
            raise RecursionError
        return real_loads(text, **options)

    monkeypatch.setattr(json, "loads", loads_failing_on_the_part)

    assert _verified(token) is outcome


@pytest.mark.parametrize(
    "unsound",
    [
        pytest.param(lambda: TRUSTED_PRIVATE_KEY, id="RSA private key"),
        pytest.param(lambda: _public_pem(TRUSTED_PRIVATE_KEY), id="PEM bytes"),
        pytest.param(lambda: ec.generate_private_key(ec.SECP256R1()).public_key(), id="EC key"),
        pytest.param(lambda: generated_rsa_key(2048).public_key(), id="2048-bit key"),
        pytest.param(lambda: generated_rsa_key(3072, 3).public_key(), id="exponent 3"),
    ],
)
def test_the_constructor_refuses_anything_but_a_sound_rsa_public_key(unsound):
    unsound_key = unsound()

    with pytest.raises(UnsoundVerificationKey) as refusal:
        CapabilityVerificationKey(unsound_key)

    assert refusal.value.__cause__ is None
    assert refusal.value.__context__ is None
