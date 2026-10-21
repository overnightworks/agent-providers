"""How the service reads a signed token: the verifier as webauth ships it, driven end to end.

A token the issuer signed is read as Python's JSON reader reads it: a repeated member keeps its
last value, and the header members that name a key are ignored. A token signed by anyone else
is still refused, and only the configured key set is ever fetched. The checks that precede
the key (size, encoding, header shape) and the order key, signature, payload hold. Cells
that are not driven here, with their upstream evidence, are listed in the service's decision
record (A1a1 amendment).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm
from service_support import (
    ATTACKER_KEY,
    SONGMAKER_CLIENT,
    TEST_JWKS_URL,
    ScriptedRunner,
    StepClock,
    TokenMint,
    assert_rejected,
    decoded_events,
    get_run,
    post_run,
    run_headers,
    served,
    service_config,
)
from webauth.https_client import KeySetFetchError
from webauth.jwt_verification import JwtVerifier, Rejected, RejectionReason, VerifiedClaims

from agent_client.wire import RejectionCode
from agent_providers.events import AssistantTextEvent, FinalEvent
from agent_service.app import create_app
from agent_service.identity import build_verifier
from agent_service.runners import Runners

ANSWER = (AssistantTextEvent(text="via the CLI"), FinalEvent(text="answer"))
ATTACKER_URL = "https://attacker.test/keys"
UNKNOWN_KID = "never-published"
UNAUTHORIZED = RejectionCode.UNAUTHORIZED

MINT = TokenMint()


@pytest.fixture(scope="module")
def runners() -> Runners:
    return Runners(cli=ScriptedRunner(ANSWER), api=ScriptedRunner(ANSWER))


@pytest.fixture(scope="module")
def service(runners) -> Iterator[str]:
    app = create_app(service_config(), runners=runners, fetch_key_set=MINT.fetch_key_set)
    with served(app) as base_url:
        yield base_url


def _header(kid: str = MINT.key_id, **members: object) -> str:
    return json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid, **members})


def _payload(claims: dict[str, object], **repeated: tuple[object, ...]) -> str:
    """The claims as JSON text, with each named member written once per given value."""
    kept = {name: value for name, value in claims.items() if name not in repeated}
    members = [(name, value) for name, values in repeated.items() for value in values]
    return (
        "{"
        + ", ".join(
            f"{json.dumps(name)}: {json.dumps(value)}" for name, value in [*members, *kept.items()]
        )
        + "}"
    )


def _admitted(response: httpx.Response) -> None:
    assert response.status_code == 200, response.text
    assert [event.type for event in decoded_events(response.content)][-1] == "succeeded"


def _verifier_and_mint() -> tuple[JwtVerifier, TokenMint]:
    """A fresh, cold verifier over a mint of its own, so its recording is this token's alone."""
    mint = TokenMint()
    return build_verifier(service_config().identity, mint.fetch_key_set, StepClock()), mint


def _user_token(mint: TokenMint, header: str, **signing: Any) -> str:
    return mint.raw_token(header, _payload(mint.user_claims()), **signing)


def _embedded_ec_jwk() -> dict[str, str]:
    """A small EC public key: unlike a full RSA key it fits the verifier's header bound."""
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm.to_jwk(key.public_key()))
    return {name: jwk[name] for name in ("kty", "crv", "x", "y")}


def test_a_token_the_attacker_signed_is_refused_whatever_url_its_header_names():
    verifier, mint = _verifier_and_mint()
    header = _header(jku=ATTACKER_URL)

    answer = verifier.verify(_user_token(mint, header, key=ATTACKER_KEY))

    assert answer == Rejected(RejectionReason.INVALID_SIGNATURE)
    assert mint.fetched_urls == [TEST_JWKS_URL]


@pytest.mark.parametrize(
    "members", [{"jku": ATTACKER_URL}, {"jwk": _embedded_ec_jwk()}], ids=["jku", "jwk"]
)
def test_a_token_the_issuer_signed_is_admitted_with_a_key_naming_header(members):
    verifier, mint = _verifier_and_mint()

    answer = verifier.verify(_user_token(mint, _header(**members)))

    assert isinstance(answer, VerifiedClaims)
    assert mint.fetched_urls == [TEST_JWKS_URL]


@pytest.mark.parametrize("signer", ["attacker", "issuer"])
def test_a_header_carrying_a_full_rsa_key_is_refused_for_its_size_before_any_fetch(signer):
    verifier, mint = _verifier_and_mint()
    header = _header(jwk=mint.key_set()["keys"][0])
    key = ATTACKER_KEY if signer == "attacker" else None

    answer = verifier.verify(_user_token(mint, header, key=key, oversized_header=True))

    assert answer == Rejected(RejectionReason.MALFORMED_TOKEN)
    assert mint.fetched_urls == []


def test_a_user_whose_token_names_sub_twice_is_the_last_one(service, runners):
    claims = MINT.user_claims()
    token = MINT.raw_token(_header(), _payload(claims, sub=("user-alice", "user-bob")))

    _admitted(post_run(service, run_headers(MINT.workload(), token)))

    assert runners.cli.admitted[-1].principal.subject == "user-bob"
    run_id = str(runners.cli.admitted[-1].run_id)
    bob = get_run(service, run_headers(MINT.workload(), MINT.user(sub="user-bob")), run_id)
    alice = get_run(service, run_headers(MINT.workload(), MINT.user()), run_id)
    assert bob.status_code == 200
    assert_rejected(alice, RejectionCode.NOT_FOUND, 404)


@pytest.mark.parametrize(
    ("kids", "admitted"),
    [
        pytest.param((UNKNOWN_KID, MINT.key_id), True, id="published-last"),
        pytest.param((MINT.key_id, UNKNOWN_KID), False, id="published-first"),
    ],
)
def test_a_header_naming_kid_twice_binds_the_last_value(service, kids, admitted):
    header = f'{{"alg": "RS256", "kid": "{kids[0]}", "kid": "{kids[1]}"}}'
    token = MINT.raw_token(header, _payload(MINT.user_claims()))

    response = post_run(service, run_headers(MINT.workload(), token))

    if admitted:
        _admitted(response)
    else:
        assert_rejected(response, UNAUTHORIZED, 401)


def _with_non_ascii_character(token: str) -> bytes:
    return f"Bearer {token}".encode() + "é".encode("latin-1")


def test_a_token_holding_a_non_ascii_character_is_refused(service):
    headers = run_headers(MINT.workload(), None)
    headers["Authorization"] = _with_non_ascii_character(MINT.user())

    assert_rejected(post_run(service, headers), UNAUTHORIZED, 401)


def test_a_signed_token_with_a_header_segment_over_the_bound_is_refused(service):
    token = MINT.raw_token(
        _header(padding="x" * 600), _payload(MINT.user_claims()), oversized_header=True
    )

    assert_rejected(post_run(service, run_headers(MINT.workload(), token)), UNAUTHORIZED, 401)


def test_a_token_that_marks_exp_as_critical_is_refused(service):
    token = MINT.raw_token(_header(crit=["exp"]), _payload(MINT.user_claims()))

    assert_rejected(post_run(service, run_headers(MINT.workload(), token)), UNAUTHORIZED, 401)


def test_a_badly_signed_token_with_a_payload_that_is_no_object_is_refused_over_http(service):
    token = MINT.raw_token(_header(), "[1]", key=ATTACKER_KEY)

    assert_rejected(post_run(service, run_headers(MINT.workload(), token)), UNAUTHORIZED, 401)


@pytest.mark.parametrize(
    ("signer", "reason"),
    [
        pytest.param(ATTACKER_KEY, RejectionReason.INVALID_SIGNATURE, id="bad-signature"),
        pytest.param(None, RejectionReason.MALFORMED_TOKEN, id="good-signature"),
    ],
)
def test_the_signature_answers_before_the_payload_is_read(signer, reason):
    verifier, mint = _verifier_and_mint()
    token = mint.raw_token(_header(), "[1]", key=signer)

    assert verifier.verify(token) == Rejected(reason)


def _cold_issuer(_url: str) -> bytes:
    raise KeySetFetchError("the issuer cannot be reached")


DETACHED = _header(b64=False, crit=["b64"])


@pytest.mark.parametrize(
    ("header", "payload", "code", "status"),
    [
        pytest.param(_header(), "[1]", RejectionCode.UNAVAILABLE, 503, id="non-object-payload"),
        pytest.param(DETACHED, '{"a": 1}', UNAUTHORIZED, 401, id="detached-with-a-payload"),
        pytest.param(DETACHED, "", RejectionCode.UNAVAILABLE, 503, id="detached-without-a-payload"),
    ],
)
def test_a_workload_token_is_refused_by_the_stage_it_fails_when_no_key_set_is_held(
    header, payload, code, status
):
    app = create_app(service_config(), fetch_key_set=_cold_issuer)
    with served(app) as base_url:
        token = MINT.raw_token(header, payload)
        response = post_run(base_url, run_headers(token, MINT.user()))

    assert_rejected(response, code, status)


def test_a_detached_workload_token_without_a_payload_is_refused_once_the_key_set_is_held(runners):
    mint = TokenMint()
    app = create_app(service_config(), runners=runners, fetch_key_set=mint.fetch_key_set)
    with served(app) as base_url:
        _admitted(post_run(base_url, run_headers(mint.workload(), mint.user())))
        token = mint.raw_token(DETACHED, "", key=None)
        response = post_run(base_url, run_headers(token, mint.user()))

    assert_rejected(response, UNAUTHORIZED, 401)


WRONG_CLIENT = "unregistered-client"
OTHER_APP = "another-app"


@pytest.mark.parametrize(
    ("azps", "admitted"),
    [
        pytest.param((WRONG_CLIENT, SONGMAKER_CLIENT), True, id="workloads-azp-last"),
        pytest.param((SONGMAKER_CLIENT, WRONG_CLIENT), False, id="wrong-azp-last"),
    ],
)
def test_a_user_token_naming_azp_twice_binds_the_last_value(service, azps, admitted):
    token = MINT.raw_token(_header(), _payload(MINT.user_claims(), azp=azps))

    response = post_run(service, run_headers(MINT.workload(), token))

    if admitted:
        _admitted(response)
    else:
        assert_rejected(response, RejectionCode.FORBIDDEN, 403)


@pytest.mark.parametrize(
    ("audiences", "admitted"),
    [
        pytest.param((OTHER_APP, "agent-service"), True, id="the-service-last"),
        pytest.param(("agent-service", OTHER_APP), False, id="another-app-last"),
    ],
)
def test_a_workload_token_naming_aud_twice_binds_the_last_value(service, audiences, admitted):
    token = MINT.raw_token(_header(), _payload(MINT.workload_claims(), aud=audiences))

    response = post_run(service, run_headers(token, MINT.user()))

    if admitted:
        _admitted(response)
    else:
        assert_rejected(response, UNAUTHORIZED, 401)


@pytest.mark.parametrize(
    ("azps", "admitted"),
    [
        pytest.param((WRONG_CLIENT, SONGMAKER_CLIENT), True, id="registered-last"),
        pytest.param((SONGMAKER_CLIENT, WRONG_CLIENT), False, id="unregistered-last"),
    ],
)
def test_a_workload_token_naming_azp_twice_binds_the_last_value(service, azps, admitted):
    token = MINT.raw_token(_header(), _payload(MINT.workload_claims(), azp=azps))

    response = post_run(service, run_headers(token, MINT.user()))

    if admitted:
        _admitted(response)
    else:
        assert_rejected(response, RejectionCode.FORBIDDEN, 403)


def test_a_signed_claim_nested_300_levels_deep_is_admitted(service):
    deep: object = "bottom"
    for _ in range(300):
        deep = {"inner": deep}

    _admitted(post_run(service, run_headers(MINT.workload(), MINT.user(unrelated=deep))))


def test_a_signed_claim_that_is_not_a_number_is_admitted(service):
    token = MINT.raw_token(_header(), _payload(MINT.user_claims(), unrelated=(float("nan"),)))

    _admitted(post_run(service, run_headers(MINT.workload(), token)))
