"""A run's capability: its claims and expiry, its signature, and the 8,192-character guard."""

from __future__ import annotations

import base64
import gc
import json
import logging
import types
import uuid
from pathlib import Path

import pytest
from jwt.algorithms import RSAAlgorithm
from service_support import (
    COWRITER,
    SONGMAKER_ORIGIN,
    TEST_ISSUER,
    capable_service_config,
    generated_rsa_key,
    pem_of,
)
from webauth.jwt_verification import (
    MAX_TOKEN_CHARACTERS,
    JwtVerifier,
    Rejected,
    RejectionReason,
    VerifiedClaims,
)

from agent_client.capability import (
    CAPABILITY_ISSUER,
    CAPABILITY_TOKEN_TYPE,
    MAX_CAPABILITY_CHARACTERS,
    CapabilityClaims,
)
from agent_service.capability import (
    PREFLIGHT_ISSUED_AT,
    PREFLIGHT_SUBJECT,
    CapabilityScope,
    CapabilityTooLarge,
    capability_claims,
    preflight_claims,
    signed_capability,
)
from agent_service.config import ServiceConfig
from agent_service.signing import SigningKey

RUN_ID = uuid.UUID("8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01")
NOW_IN_2026 = 1_792_152_000.0
SHORT_TOOL_NAME_LENGTH = 35
SHORTEST_NAME_OF_THE_LAST_TOOL_AT_THE_LIMIT = 55


def _scope(tmp_path: Path, **songmaker: object) -> CapabilityScope:
    scope = capable_service_config(tmp_path, songmaker=songmaker).capability_scope("songmaker")
    assert scope is not None
    return scope


def _claims_of(scope: CapabilityScope, **overrides: object) -> CapabilityClaims:
    values = {
        "run_id": RUN_ID,
        "app": "songmaker",
        "issuer": TEST_ISSUER,
        "subject": "user-alice",
        "purpose": COWRITER,
        "now": NOW_IN_2026,
        "max_duration_s": 600.0,
        **overrides,
    }
    return capability_claims(scope, **values)


@pytest.mark.parametrize(
    ("now", "max_duration_s", "issued_at", "expires_at"),
    [
        pytest.param(1_792_152_000.7, 600.0, 1_792_152_000, 1_792_152_600, id="whole-maximum"),
        pytest.param(1_792_152_000.9, 300.5, 1_792_152_000, 1_792_152_301, id="fractional-maximum"),
    ],
)
def test_a_capability_is_issued_at_the_floored_now_and_expires_at_the_floored_end(
    tmp_path, now, max_duration_s, issued_at, expires_at
):
    claims = _claims_of(_scope(tmp_path), now=now, max_duration_s=max_duration_s)

    assert (claims.iat, claims.exp) == (issued_at, expires_at)


@pytest.mark.parametrize(
    ("songmaker", "tools"),
    [
        pytest.param({}, ("edit_song", "read_song"), id="the-keys-of-the-table-sorted"),
        pytest.param({"tools": {}}, (), id="an-empty-table-grants-nothing"),
    ],
)
def test_a_capability_says_who_where_what_and_which_tools(tmp_path, songmaker, tools):
    claims = _claims_of(_scope(tmp_path, **songmaker))

    assert claims == CapabilityClaims(
        iss=CAPABILITY_ISSUER,
        aud=SONGMAKER_ORIGIN,
        sub=str(RUN_ID),
        iat=1_792_152_000,
        exp=1_792_152_600,
        app="songmaker",
        purpose=COWRITER,
        principal={"iss": TEST_ISSUER, "sub": "user-alice"},
        tools=tools,
    )


def _verifier_of(key: SigningKey, wall_clock_reading: float, audience: str) -> JwtVerifier:
    key_set = {
        "keys": [
            {
                **RSAAlgorithm.to_jwk(key.public_key, as_dict=True),
                "kid": key.kid,
                "use": "sig",
                "alg": "RS256",
            }
        ]
    }
    return JwtVerifier(
        issuer=CAPABILITY_ISSUER,
        audiences=[audience],
        jwks_url="https://keys.test/jwks",
        leeway_seconds=0,
        fetch_key_set=lambda _url: json.dumps(key_set).encode(),
        wall_clock=lambda: wall_clock_reading,
        monotonic_clock=lambda: 0.0,
    )


def _header_of(token: str) -> dict[str, object]:
    header = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))


def test_a_signed_capability_has_exactly_the_capability_header(tmp_path):
    scope = _scope(tmp_path)

    capability = signed_capability(scope, _claims_of(scope))

    assert _header_of(capability.token) == {
        "alg": "RS256",
        "kid": scope.key.kid,
        "typ": CAPABILITY_TOKEN_TYPE,
    }


@pytest.mark.parametrize(
    "subject",
    ["user-alice", "user\x00alice", "user-\U0001f3b5", "u" * 600, '"' * 255],
    ids=["ascii", "nul", "astral", "600-characters", "255-quotation-marks"],
)
def test_a_verifier_accepts_a_capability_until_it_expires_with_the_subject_unchanged(
    tmp_path, subject
):
    scope = _scope(tmp_path)
    claims = _claims_of(scope, subject=subject)
    token = signed_capability(scope, claims).token

    verdict = _verifier_of(scope.key, claims.exp - 0.1, SONGMAKER_ORIGIN).verify(token)

    assert isinstance(verdict, VerifiedClaims)
    assert CapabilityClaims.from_claims(verdict.claims) == claims
    assert verdict.claims["principal"]["sub"] == subject


def test_a_verifier_refuses_a_capability_at_its_expiry_and_for_another_origin(tmp_path):
    scope = _scope(tmp_path)
    claims = _claims_of(scope)
    token = signed_capability(scope, claims).token

    assert _verifier_of(scope.key, claims.exp, SONGMAKER_ORIGIN).verify(token) == Rejected(
        RejectionReason.EXPIRED
    )
    assert _verifier_of(scope.key, claims.exp - 0.1, "https://other.internal").verify(
        token
    ) == Rejected(RejectionReason.INVALID_AUDIENCE)


def test_the_preflight_claims_reserve_the_subject_and_the_widest_timestamps(tmp_path):
    scope = _scope(tmp_path)

    claims = preflight_claims(
        scope, app="songmaker", purpose=COWRITER, issuer=TEST_ISSUER, max_duration_s=600.5
    )

    assert PREFLIGHT_SUBJECT == '"' * 255
    assert (claims.principal.sub, claims.iat, claims.exp) == (
        PREFLIGHT_SUBJECT,
        9_999_999_999,
        10_000_000_599,
    )
    assert len(claims.sub) == 36
    assert (claims.aud, claims.app, claims.purpose, claims.tools) == (
        SONGMAKER_ORIGIN,
        "songmaker",
        COWRITER,
        ("edit_song", "read_song"),
    )


def test_a_token_with_the_preflight_claims_is_refused_as_issued_in_the_future_today(tmp_path):
    scope = _scope(tmp_path)
    claims = preflight_claims(
        scope, app="songmaker", purpose=COWRITER, issuer=TEST_ISSUER, max_duration_s=600.0
    )
    token = signed_capability(scope, claims).token

    verdict = _verifier_of(scope.key, NOW_IN_2026, SONGMAKER_ORIGIN).verify(token)

    assert verdict == Rejected(RejectionReason.ISSUED_IN_FUTURE)
    assert PREFLIGHT_ISSUED_AT > NOW_IN_2026


def test_the_capability_limit_is_the_limit_webauths_verifier_enforces():
    assert MAX_CAPABILITY_CHARACTERS == MAX_TOKEN_CHARACTERS


def _scope_with_the_last_tool_named_to(
    scope: CapabilityScope, last_tool_length: int
) -> CapabilityScope:
    short_tools = [f"{index:03d}".ljust(SHORT_TOOL_NAME_LENGTH, "x") for index in range(127)]
    last_tool = "127".ljust(last_tool_length, "x")
    return CapabilityScope(scope.origin, tuple(sorted([*short_tools, last_tool])), scope.key)


def _preflight_token_of(scope: CapabilityScope) -> str:
    claims = preflight_claims(
        scope, app="songmaker", purpose=COWRITER, issuer=TEST_ISSUER, max_duration_s=600.0
    )
    return signed_capability(scope, claims).token


def test_a_capability_of_exactly_8192_characters_is_signed(tmp_path):
    scope = _scope_with_the_last_tool_named_to(
        _scope(tmp_path), SHORTEST_NAME_OF_THE_LAST_TOOL_AT_THE_LIMIT
    )

    assert len(_preflight_token_of(scope)) == MAX_CAPABILITY_CHARACTERS


def test_a_capability_of_8193_characters_is_refused_with_its_length(tmp_path):
    scope = _scope_with_the_last_tool_named_to(
        _scope(tmp_path), SHORTEST_NAME_OF_THE_LAST_TOOL_AT_THE_LIMIT + 1
    )

    with pytest.raises(CapabilityTooLarge) as refusal:
        _preflight_token_of(scope)

    assert refusal.value.length == MAX_CAPABILITY_CHARACTERS + 1


def test_no_representation_of_a_capability_or_its_scope_shows_the_token_or_the_key(tmp_path):
    scope = _scope(tmp_path)
    capability = signed_capability(scope, _claims_of(scope))
    shown = [repr(capability), str(capability), repr(scope), str(scope)]
    secret_lines = pem_of(generated_rsa_key()).decode().splitlines()

    assert not any(capability.token in text for text in shown)
    assert not any(line in text for text in shown for line in secret_lines)
    assert not any("PRIVATE KEY" in text for text in shown)


def _strings_reachable_from(root: object) -> set[str]:
    skipped = (type, types.ModuleType, types.FunctionType, types.BuiltinFunctionType)
    seen: set[int] = set()
    strings: set[str] = set()
    pending = [root]
    while pending:
        item = pending.pop()
        if id(item) in seen or isinstance(item, skipped):
            continue
        seen.add(id(item))
        if isinstance(item, str):
            strings.add(item)
        pending.extend(gc.get_referents(item))
    return strings


def _loaded_recording_the_signed_tokens(
    tmp_path, monkeypatch, caplog
) -> tuple[ServiceConfig, list[str]]:
    signed: list[str] = []
    sign = SigningKey.sign

    def recording_sign(key, claims, token_type):
        signed.append(sign(key, claims, token_type))
        return signed[-1]

    monkeypatch.setattr(SigningKey, "sign", recording_sign)
    with caplog.at_level(logging.DEBUG):
        config = capable_service_config(tmp_path)
    return config, signed


def test_loading_a_configuration_keeps_no_measurement_token_anywhere(tmp_path, monkeypatch, caplog):
    config, measurement_tokens = _loaded_recording_the_signed_tokens(tmp_path, monkeypatch, caplog)

    formatted = [logging.Formatter().format(record) for record in caplog.records]
    reachable = _strings_reachable_from(config)
    assert measurement_tokens
    for token in measurement_tokens:
        assert not any(token in text for text in formatted)
        assert token not in repr(config)
        assert token not in reachable
