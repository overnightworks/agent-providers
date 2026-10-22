"""The capability format an app's MCP server verifies: its origin rule and its claims."""

from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from agent_runtime_client.capability import (
    CAPABILITY_ISSUER,
    CAPABILITY_TOKEN_TYPE,
    MAX_CAPABILITY_CHARACTERS,
    CapabilityClaims,
    McpOrigin,
)

ORIGIN = "https://notes.internal"
EXAMPLE_CLAIMS: dict[str, Any] = {
    "iss": "agent-service",
    "aud": ORIGIN,
    "sub": "8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01",
    "iat": 1792152000,
    "exp": 1792152600,
    "app": "notes",
    "purpose": "notes.assistant",
    "principal": {"iss": "https://keycloak.test/realms/agent-test", "sub": "user-alice"},
    "tools": ["edit_note", "read_note"],
}
EXAMPLE_TEXT = json.dumps(EXAMPLE_CLAIMS, separators=(",", ":"))
LABEL_OF_64 = "a" * 64
HOST_OF_254 = ".".join(["a" * 63] * 3 + ["a" * 62])
HOST_OF_253 = ".".join(["a" * 63] * 3 + ["a" * 61])

_origin = TypeAdapter(McpOrigin)


def _refusal_of(origin: str) -> str:
    with pytest.raises(ValidationError) as refusal:
        _origin.validate_python(origin)
    return str(refusal.value.errors(include_input=False)[0]["msg"])


@pytest.mark.parametrize(
    "origin",
    [
        ORIGIN,
        "https://notes.internal:8443",
        "https://xn--bcher-kva.internal:8443",
        "https://123.internal",
        "https://api.0xfoo",
    ],
)
def test_a_canonical_https_dns_origin_is_accepted_unchanged(origin):
    assert _origin.validate_python(origin) == origin


@pytest.mark.parametrize(
    "origin",
    [
        "HTTPS://Notes.internal",
        "https://notes.internal:443",
        "https://notes.internal:0443",
        "https://notes.internal/",
        "https://notes.internal.",
    ],
)
def test_an_origin_with_a_canonical_form_is_refused_giving_it(origin):
    assert _refusal_of(origin).endswith(f"canonical form {ORIGIN}")


@pytest.mark.parametrize(
    "origin",
    [
        "https://notes.internal/mcp",
        "https://notes.internal?x",
        "https://notes.internal#x",
        "https://u:p@notes.internal",
        "http://notes.internal",
        "ftp://notes",
        "https://",
        "https://:8443",
        "notes:8443",
        "https://-notes",
        "https://notes-.internal",
        pytest.param(f"https://{LABEL_OF_64}.internal", id="label-of-64-characters"),
        pytest.param(f"https://{HOST_OF_254}", id="host-of-254-characters"),
        "https://notes.internal:0",
        "https://notes.internal:65536",
        "https://notes.internal:8x",
        "https://notes.internal:",
        "https://no tes.internal",
        "https://notes.internal ",
        "https://notes\t.internal",
        "https://notes.internal\n",
        "https://notes\x00.internal",
        "https://notes%2e.internal",
        "https://10.0.0.5:8443",
        "https://127.1",
        "https://notes.123",
        "https://0x7f000001",
        "https://127.0.0.0x1",
        "https://notes.0x",
        "https://[fd00::1]:8443",
        "https://bücher.internal",
    ],
)
def test_any_other_origin_is_refused_without_a_canonical_form(origin):
    assert "canonical form" not in _refusal_of(origin)


@pytest.mark.parametrize(
    ("origin", "refusal"),
    [
        (
            "HTTPS://notes.internal:0",
            "must be https://<host> or https://<host>:<port> and nothing else: no user"
            " information, path, query, fragment, space, control or non-ASCII character",
        ),
        (
            f"{ORIGIN}:{'9' * 5000}",
            "must write a port as decimal digits from 1 to 65535, without leading zeros, never 443",
        ),
    ],
    ids=["upper-case-scheme", "port-of-5000-digits"],
)
def test_an_origin_is_refused_in_the_format_s_own_words(origin, refusal):
    assert _refusal_of(origin).removeprefix("Value error, ") == refusal


def test_a_host_of_253_characters_is_accepted():
    origin = f"https://{HOST_OF_253}"

    assert _origin.validate_python(origin) == origin


def test_the_example_claims_round_trip_unchanged():
    claims = CapabilityClaims.model_validate_json(EXAMPLE_TEXT)

    assert claims.model_dump_json() == EXAMPLE_TEXT
    assert CapabilityClaims.model_validate_json(claims.model_dump_json()) == claims


def _claims_with(**overrides: object) -> dict[str, Any]:
    """Overrides replace claims; an override of ``None`` removes the claim."""
    merged = {**EXAMPLE_CLAIMS, **overrides}
    return {name: value for name, value in merged.items() if value is not None}


def _principal_with(**overrides: object) -> dict[str, Any]:
    merged = {**EXAMPLE_CLAIMS["principal"], **overrides}
    return {name: value for name, value in merged.items() if value is not None}


REFUSED_CLAIMS = [
    pytest.param(_claims_with(extra="x"), id="unknown-claim"),
    *[pytest.param(_claims_with(**{name: None}), id=f"missing-{name}") for name in EXAMPLE_CLAIMS],
    pytest.param(_claims_with(iss="other-service"), id="another-issuer"),
    pytest.param(_claims_with(aud=[ORIGIN]), id="audience-as-a-list"),
    pytest.param(_claims_with(aud="https://Notes.internal"), id="audience-not-canonical"),
    pytest.param(_claims_with(sub="not-a-uuid"), id="subject-not-a-uuid"),
    pytest.param(_claims_with(sub="{8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01}"), id="braced-subject"),
    pytest.param(_claims_with(sub="8C2F6C1E-3B5A-4D8E-9F10-2A7B4C6D8E01"), id="upper-case-subject"),
    pytest.param(_claims_with(sub="8c2f6c1e3b5a4d8e9f102a7b4c6d8e01"), id="bare-hex-subject"),
    *[
        pytest.param(_claims_with(**{claim: value}), id=f"{claim}-as-{value!r}")
        for claim in ("iat", "exp")
        for value in (True, 1.5, "1")
    ],
    pytest.param(_claims_with(exp=EXAMPLE_CLAIMS["iat"]), id="expires-when-issued"),
    pytest.param(_claims_with(exp=EXAMPLE_CLAIMS["iat"] - 1), id="expires-before-issued"),
    pytest.param(_claims_with(app=""), id="empty-app"),
    pytest.param(_claims_with(purpose=""), id="empty-purpose"),
    pytest.param(_claims_with(principal=_principal_with(sub=None)), id="principal-without-sub"),
    pytest.param(_claims_with(principal=_principal_with(sub="")), id="principal-empty-sub"),
    pytest.param(_claims_with(principal=_principal_with(sub="\ud800")), id="principal-lone-sub"),
    pytest.param(_claims_with(principal=_principal_with(extra="x")), id="principal-extra-member"),
    pytest.param(_claims_with(tools=["read_note", "read_note"]), id="tool-named-twice"),
    pytest.param(_claims_with(tools=[""]), id="empty-tool-name"),
]


@pytest.mark.parametrize("claims", REFUSED_CLAIMS)
def test_claims_that_break_the_format_are_refused(claims):
    with pytest.raises(ValidationError):
        CapabilityClaims.from_claims(claims)


@pytest.mark.parametrize(
    "subject",
    [
        "user\x00alice",
        "user-\U0001f3b5-alice",
        "u" * 600,
        '"' * 255,
        " User-Alice ",
        "Stra\u00dfe",
        "e\u0301",
        "caf\u00e9",
        "\ufb01",
    ],
    ids=[
        "nul",
        "astral",
        "600-characters",
        "255-quotation-marks",
        "padded-and-capitalised",
        "sharp-s",
        "decomposed-e",
        "precomposed-e",
        "fi-ligature",
    ],
)
def test_a_principal_subject_round_trips_exactly(subject):
    claims = CapabilityClaims.from_claims(_claims_with(principal=_principal_with(sub=subject)))

    assert CapabilityClaims.model_validate_json(claims.model_dump_json()).principal.sub == subject


def test_a_decoded_read_only_mapping_with_a_list_of_tools_is_read():
    decoded = MappingProxyType({**EXAMPLE_CLAIMS, "tools": ["a", "b"]})

    claims = CapabilityClaims.from_claims(decoded)

    assert claims.tools == ("a", "b")


def test_claims_are_immutable():
    claims = CapabilityClaims.model_validate_json(EXAMPLE_TEXT)

    with pytest.raises(ValidationError):
        claims.exp = claims.exp + 1


def test_the_format_constants_are_the_ones_apps_verify_against():
    assert (CAPABILITY_ISSUER, CAPABILITY_TOKEN_TYPE, MAX_CAPABILITY_CHARACTERS) == (
        "agent-service",
        "agent-capability+jwt",
        8192,
    )
