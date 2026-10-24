"""A run started with a job ticket instead of a live user token (#457, J1 to J18).

The oracle is the live entrance: a ticket run is ``POST /runs`` with the workload token and a
ticket minted by the real ``POST /job-tickets``, compared with the same body run with live
tokens. Test values: app ``songmaker``, the person ``user-alice``, one Claude profile.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import jwt
import pytest
from bounds import TEST_WAIT_SECONDS
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from service_support import (
    API_GROUP,
    CLI_PROFILE,
    COWRITER,
    PRESENTATOR_CLIENT,
    SONGMAKER_CLIENT,
    SONGMAKER_GROUP,
    SONGMAKER_TOOLS,
    SUBSCRIPTION_GROUP,
    TEST_ISSUER,
    WALL_CLOCK_START,
    TokenMint,
    answer_of,
    assert_rejected,
    generated_rsa_key,
    get_run,
    post_run,
    reaches_text,
    run_body,
    run_headers,
    service_config,
    written_key_file,
)
from test_service_admission import HeldRunner
from ticket_run_support import minted_ticket as minted_ticket_for
from ticket_run_support import (
    process_key,
    run_id_of,
    running_service,
    signed_ticket,
    ticket_headers,
    with_background,
)
from webauth.https_client import KeySetFetchError

from agent_runtime_client.capability import CAPABILITY_ISSUER
from agent_runtime_client.wire import RejectionCode, RunStatus
from agent_runtime_service.config import SigningConfig
from agent_runtime_service.job_ticket import JOB_TICKET_AUDIENCE, JOB_TICKET_TOKEN_TYPE
from agent_runtime_service.routing import Api, Cli

MINT = TokenMint()
OTHER_PURPOSE = "judge"
UNAUTHORIZED = (RejectionCode.UNAUTHORIZED, 401)
FORBIDDEN = (RejectionCode.FORBIDDEN, 403)
NOT_ENABLED = (RejectionCode.NOT_ENABLED, 403)
START = int(WALL_CLOCK_START.timestamp())


SUBSCRIBER = (SUBSCRIPTION_GROUP, SONGMAKER_GROUP)


def minted_ticket(base_url, mint, groups=SUBSCRIBER, **overrides):
    """A ticket of the default person and agent from the real ``POST /job-tickets``."""
    options = {"purpose": COWRITER, "client": SONGMAKER_CLIENT, **overrides}
    return minted_ticket_for(base_url, mint, groups, **options)


def ticket_claims(**overrides: object) -> dict[str, object]:
    """The claims of a valid ticket for ``user-alice``, at the clock's start."""
    issued_at = int(WALL_CLOCK_START.timestamp())
    claims = {
        "iss": CAPABILITY_ISSUER,
        "aud": JOB_TICKET_AUDIENCE,
        "iat": issued_at,
        "exp": issued_at + 3600,
        "app": "songmaker",
        "client": SONGMAKER_CLIENT,
        "principal": {"iss": TEST_ISSUER, "sub": "user-alice"},
        "purpose": COWRITER,
        "groups": [SONGMAKER_GROUP, SUBSCRIPTION_GROUP],
    }
    return {**claims, **overrides}


def _live(service, groups=SUBSCRIBER, **body: object) -> httpx.Response:
    return post_run(
        service.base_url, run_headers(MINT.workload(), MINT.user(groups)), run_body(**body)
    )


def _ticket_run(service, ticket: str, **body: object) -> httpx.Response:
    return post_run(service.base_url, ticket_headers(MINT.workload(), ticket), run_body(**body))


def _service(config=None, **options):
    return running_service(config or service_config(), MINT.fetch_key_set, **options)


def _unreachable(_url: str) -> bytes:
    raise KeySetFetchError("the issuer cannot be reached")


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unsigned_ticket_with(algorithm: str, sign) -> str:
    header = {"alg": algorithm, "typ": JOB_TICKET_TOKEN_TYPE, "kid": process_key().kid}
    signing_input = (
        f"{_base64url(json.dumps(header).encode())}."
        f"{_base64url(json.dumps(ticket_claims()).encode())}"
    )
    return f"{signing_input}.{_base64url(sign(signing_input.encode()))}"


def _hs256_keyed_with_the_public_key() -> str:
    public_pem = process_key().public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return _unsigned_ticket_with(
        "HS256", lambda data: hmac.new(public_pem, data, hashlib.sha256).digest()
    )


def _signed_by_another_key() -> str:
    return jwt.encode(
        ticket_claims(),
        generated_rsa_key(2048),
        algorithm="RS256",
        headers={"kid": process_key().kid, "typ": JOB_TICKET_TOKEN_TYPE},
    )


def test_a_ticket_run_is_admitted_on_the_background_profile_and_recorded_for_the_person():
    """J14, J8, J7"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)
        live = _live(service)
        response = _ticket_run(service, ticket)
        status = get_run(
            service.base_url,
            ticket_headers(MINT.workload(), ticket),
            str(run_id_of(response.content)),
        )

        assert response.status_code == 200, response.text
        ticket_run, live_run = service.admitted()[-1], service.admitted()[-2]
        assert live.status_code == 200
        assert ticket_run.route == live_run.route == Cli(CLI_PROFILE)
        assert (ticket_run.app, ticket_run.principal) == (live_run.app, live_run.principal)
        assert status.status_code == 200
        assert RunStatus.model_validate_json(status.content).run_id == ticket_run.run_id


def test_one_ticket_carries_many_runs():
    """J5"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)

        statuses = [_ticket_run(service, ticket).status_code for _ in range(3)]

        assert statuses == [200, 200, 200]
        assert len(service.admitted()) == 3


def test_a_ticket_run_never_takes_a_profile_without_background():
    """J8"""
    with _service(with_background(service_config(), False)) as service:
        ticket = minted_ticket(service.base_url, MINT)

        assert_rejected(_ticket_run(service, ticket), *NOT_ENABLED)
        assert _live(service).status_code == 200
        assert service.admitted()[-1].route == Cli(CLI_PROFILE)


def test_a_ticket_run_goes_on_over_the_api_when_the_profile_is_not_for_background():
    """J8"""
    groups = (SUBSCRIPTION_GROUP, API_GROUP, SONGMAKER_GROUP)
    with _service(with_background(service_config(), False)) as service:
        ticket = minted_ticket(service.base_url, MINT, groups)

        response = _ticket_run(service, ticket)

        assert response.status_code == 200, response.text
        assert isinstance(service.api.admitted[-1].route, Api)
        assert _live(service, groups).status_code == 200
        assert service.admitted()[-1].route == Cli(CLI_PROFILE)


def test_a_ticket_run_has_the_role_of_the_groups_bound_at_mint():
    """J2: `/family` changes no outcome"""
    admin_groups = (SUBSCRIPTION_GROUP, SONGMAKER_GROUP + "/admin", "/family")
    with _service() as service:
        admin = minted_ticket(service.base_url, MINT, admin_groups)
        member = minted_ticket(service.base_url, MINT)

        assert _ticket_run(service, admin, roles=["admin"]).status_code == 200
        assert_rejected(_ticket_run(service, member, roles=["admin"]), *FORBIDDEN)


def test_a_ticket_without_app_membership_is_forbidden():
    """J2, J15"""
    with _service() as service:
        ticket = signed_ticket(ticket_claims(groups=[SUBSCRIPTION_GROUP]))

        assert_rejected(_ticket_run(service, ticket), *FORBIDDEN)


def test_a_ticket_keeps_the_groups_of_mint_time():
    """J4: a person who left the subscription group after minting still has a working ticket"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)
        left = run_headers(MINT.workload(), MINT.user([SONGMAKER_GROUP]))

        assert_rejected(post_run(service.base_url, left, run_body()), *NOT_ENABLED)
        assert _ticket_run(service, ticket).status_code == 200


def test_a_changed_configuration_applies_to_an_open_ticket():
    """J4"""
    config = service_config()
    identity = config.identity.model_copy(update={"subscription_groups": frozenset()})
    with _service() as before, _service(config.model_copy(update={"identity": identity})) as after:
        ticket = minted_ticket(before.base_url, MINT)

        assert _ticket_run(before, ticket).status_code == 200
        assert_rejected(_ticket_run(after, ticket), *NOT_ENABLED)


def test_a_new_signing_key_ends_every_open_ticket(tmp_path: Path):
    """J10"""
    other_key = written_key_file(tmp_path, rsa.generate_private_key(65537, 3072))
    changed = service_config().model_copy(
        update={"signing": SigningConfig.model_validate({"key_file": str(other_key)})}
    )
    with _service() as before, _service(changed) as after:
        ticket = minted_ticket(before.base_url, MINT)

        assert _ticket_run(before, ticket).status_code == 200
        assert_rejected(_ticket_run(after, ticket), *UNAUTHORIZED)


def test_an_expired_ticket_is_unauthorized():
    """J15, J3: valid until `exp` and not at it; a ticket issued after the clock is refused"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)
        future = signed_ticket(ticket_claims(iat=START + 100, exp=START + 4000))

        service.clock.advance(3599)
        assert _ticket_run(service, ticket).status_code == 200
        service.clock.advance(1)
        assert_rejected(_ticket_run(service, ticket), *UNAUTHORIZED)
        service.clock.set_to(WALL_CLOCK_START)
        assert_rejected(_ticket_run(service, future), *UNAUTHORIZED)


@pytest.mark.parametrize(
    "ticket",
    [
        pytest.param(lambda: "garbage", id="garbage"),
        pytest.param(lambda: signed_ticket(ticket_claims())[:-10], id="truncated"),
        pytest.param(lambda: "", id="empty"),
        pytest.param(lambda: _unsigned_ticket_with("none", lambda _: b""), id="none-algorithm"),
        pytest.param(_hs256_keyed_with_the_public_key, id="hs256-with-the-public-key"),
        pytest.param(_signed_by_another_key, id="another-key"),
    ],
)
def test_a_ticket_that_does_not_verify_is_unauthorized(ticket):
    """J15"""
    with _service() as service:
        assert_rejected(_ticket_run(service, ticket()), *UNAUTHORIZED)
        assert service.admitted() == []


def test_a_capability_is_not_a_ticket():
    """J12"""
    with _service() as service:
        response = _live(service, tools=SONGMAKER_TOOLS)
        capability = service.admitted()[-1].capability

        assert response.status_code == 200 and capability is not None
        assert_rejected(_ticket_run(service, capability.token), *UNAUTHORIZED)


def _signed_with(headers: dict[str, str], claims: dict[str, object]) -> str:
    return jwt.encode(claims, process_key().private_key, algorithm="RS256", headers=headers)


@pytest.mark.parametrize(
    "ticket",
    [
        pytest.param(lambda: signed_ticket(ticket_claims(), "JWT"), id="wrong-type"),
        pytest.param(
            lambda: _signed_with({"kid": "another", "typ": JOB_TICKET_TOKEN_TYPE}, ticket_claims()),
            id="wrong-kid",
        ),
        pytest.param(
            lambda: signed_ticket(ticket_claims(aud="https://songmaker.internal")),
            id="wrong-audience",
        ),
        pytest.param(
            lambda: signed_ticket(ticket_claims(jti="9d4d6c94-8c55-4a61-93b0-7f3a7b0e4c5d")),
            id="a-jti-still-carried",
        ),
        pytest.param(lambda: signed_ticket(ticket_claims(extra=1)), id="an-extra-claim"),
    ],
)
def test_a_ticket_off_the_contract_is_unauthorized(ticket):
    """J12"""
    with _service() as service:
        assert_rejected(_ticket_run(service, ticket()), *UNAUTHORIZED)


def test_the_ticket_of_another_app_is_forbidden():
    """J15"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)
        headers = ticket_headers(MINT.workload(PRESENTATOR_CLIENT), ticket)

        assert_rejected(post_run(service.base_url, headers, run_body()), *FORBIDDEN)


def test_a_ticket_of_a_retired_workload_client_is_forbidden():
    """J15, J2"""
    renamed = "songmaker-renamed"
    with (
        _service() as before,
        _service(service_config(songmaker={"workload_client": renamed})) as after,
    ):
        ticket = minted_ticket(before.base_url, MINT)
        headers = ticket_headers(MINT.workload(renamed), ticket)

        assert_rejected(post_run(after.base_url, headers, run_body()), *FORBIDDEN)


def test_a_ticket_runs_only_its_own_agent():
    """J15"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT, purpose=OTHER_PURPOSE)

        assert_rejected(_ticket_run(service, ticket), *FORBIDDEN)
        assert service.admitted() == []
        assert _ticket_run(service, ticket, purpose=OTHER_PURPOSE).status_code == 200


@pytest.mark.parametrize(
    "extra_headers",
    [
        pytest.param(lambda: {"Authorization": f"Bearer {MINT.user(SUBSCRIBER)}"}, id="bearer"),
        pytest.param(lambda: {"Authorization": "Basic dXNlcjpwdw=="}, id="not-bearer"),
    ],
)
@pytest.mark.parametrize("with_workload_token", [True, False])
def test_a_ticket_with_a_user_token_is_unauthorized(extra_headers, with_workload_token):
    """J14"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)
        workload = MINT.workload() if with_workload_token else None
        headers = {**ticket_headers(workload, ticket), **extra_headers()}

        assert_rejected(post_run(service.base_url, headers, run_body()), *UNAUTHORIZED)
        assert service.admitted() == []


def test_a_ticket_run_needs_the_workload_token():
    """J14, J7"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)
        run_id = run_id_of(_ticket_run(service, ticket).content)

        assert_rejected(
            post_run(service.base_url, ticket_headers(None, ticket), run_body()), *UNAUTHORIZED
        )
        assert_rejected(
            post_run(service.base_url, ticket_headers(MINT.workload(), None), run_body()),
            *UNAUTHORIZED,
        )
        assert_rejected(
            get_run(service.base_url, ticket_headers(None, ticket), str(run_id)), *UNAUTHORIZED
        )


def test_an_unfetchable_key_set_is_unavailable_for_a_ticket_run():
    """J14: as a live run, also with a garbage ticket"""
    with running_service(service_config(), _unreachable) as service:
        for ticket in (signed_ticket(ticket_claims()), "garbage"):
            response = post_run(
                service.base_url, ticket_headers(MINT.workload(), ticket), run_body()
            )
            assert_rejected(response, RejectionCode.UNAVAILABLE, 503)


@pytest.mark.parametrize(
    ("fetch_key_set", "workload", "expected"),
    [
        pytest.param(MINT.fetch_key_set, MINT.workload(), UNAUTHORIZED, id="valid-workload-token"),
        pytest.param(MINT.fetch_key_set, "garbage", UNAUTHORIZED, id="invalid-workload-token"),
        pytest.param(_unreachable, MINT.workload(), (RejectionCode.UNAVAILABLE, 503), id="key-set"),
    ],
)
def test_a_ticket_cannot_be_valid_without_a_signing_key(fetch_key_set, workload, expected):
    """J10, J15: the workload token is checked first"""
    config = service_config().model_copy(update={"signing": None})
    with running_service(config, fetch_key_set) as service:
        headers = ticket_headers(workload, signed_ticket(ticket_claims()))

        assert_rejected(post_run(service.base_url, headers, run_body()), *expected)


def test_a_ticket_reads_the_run_it_started():
    """J7"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)
        run_id = str(run_id_of(_ticket_run(service, ticket).content))

        by_ticket = get_run(service.base_url, ticket_headers(MINT.workload(), ticket), run_id)
        live = get_run(
            service.base_url, run_headers(MINT.workload(), MINT.user(SUBSCRIBER)), run_id
        )

        assert by_ticket.status_code == 200
        assert set(json.loads(by_ticket.content)) == set(RunStatus.model_fields)
        assert answer_of(by_ticket) == answer_of(live)


def test_a_ticket_reads_only_the_run_of_its_app_and_person():
    """J7: app and person, not purpose"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)
        run_id = str(run_id_of(_ticket_run(service, ticket).content))
        bob = minted_ticket(service.base_url, MINT, subject="user-bob")
        elsewhere = minted_ticket(
            service.base_url,
            MINT,
            (SUBSCRIPTION_GROUP, "/apps/presentator"),
            client=PRESENTATOR_CLIENT,
        )
        other_purpose = minted_ticket(service.base_url, MINT, purpose=OTHER_PURPOSE)

        def read(ticket, workload=MINT.workload(), segment=run_id, **more):
            headers = {**ticket_headers(workload, ticket), **more}
            return get_run(service.base_url, headers, segment)

        assert_rejected(read(bob), RejectionCode.NOT_FOUND, 404)
        assert_rejected(
            read(elsewhere, MINT.workload(PRESENTATOR_CLIENT)), RejectionCode.NOT_FOUND, 404
        )
        assert_rejected(read(ticket, segment="not-a-run"), RejectionCode.NOT_FOUND, 404)
        assert_rejected(
            read(ticket, Authorization=f"Bearer {MINT.user(SUBSCRIBER)}"), *UNAUTHORIZED
        )
        assert read(other_purpose).status_code == 200


def test_the_ticket_reaches_no_log_frame_record_or_runner(caplog):
    """J11"""
    caplog.set_level(logging.DEBUG)
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)
        run = _ticket_run(service, ticket)
        refused = [
            _ticket_run(service, ticket, purpose=OTHER_PURPOSE),
            _ticket_run(service, ticket + "x"),
        ]
        status = get_run(
            service.base_url, ticket_headers(MINT.workload(), ticket), str(run_id_of(run.content))
        )

        answers = [run, status, *refused]
        assert all(ticket not in response.text for response in answers)
        assert all(ticket not in str(response.headers) for response in answers)
        assert ticket not in caplog.text
        assert not reaches_text(service.admitted(), [ticket])


def test_a_ticket_does_not_open_the_offer():
    """J1"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)
        response = httpx.get(
            f"{service.base_url}/offer", headers=ticket_headers(MINT.workload(), ticket), timeout=10
        )

        assert_rejected(response, *UNAUTHORIZED)


def test_the_mint_and_the_offer_ignore_a_ticket_header():
    """J1: the header is not read on those routes"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)
        headers = {
            **run_headers(MINT.workload(), MINT.user(SUBSCRIBER)),
            **ticket_headers(None, ticket),
        }

        offer = httpx.get(f"{service.base_url}/offer", headers=headers, timeout=10)
        mint = httpx.post(
            f"{service.base_url}/job-tickets",
            headers=headers,
            json={"v": 2, "purpose": COWRITER, "expires_in_s": 60},
            timeout=10,
        )

        assert (offer.status_code, mint.status_code) == (200, 200)


def test_a_ticket_run_meets_the_same_limit_and_never_falls_back():
    """J17"""
    groups = (SUBSCRIPTION_GROUP, API_GROUP, SONGMAKER_GROUP)
    held = HeldRunner()
    config = service_config(runs={"max_parallel_cli_runs": 1})
    with _service(config, cli=held) as service, ThreadPoolExecutor() as pool:
        ticket = minted_ticket(service.base_url, MINT, groups)
        first = pool.submit(_ticket_run, service, ticket)
        try:
            assert held.entered.wait(TEST_WAIT_SECONDS)

            assert_rejected(_ticket_run(service, ticket), RejectionCode.BUSY, 429)
            assert service.api.admitted == []
        finally:
            held.proceed_all()
        assert first.result(TEST_WAIT_SECONDS).status_code == 200


def test_a_ticket_run_body_is_a_live_body():
    """J13, J14"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)

        assert _ticket_run(service, ticket).status_code == 200
        assert_rejected(
            _ticket_run(service, ticket, job_id="1"), RejectionCode.INVALID_REQUEST, 422
        )


def test_a_ticket_run_signs_its_capability_for_the_person():
    """J2, J7"""
    with _service() as service:
        ticket = minted_ticket(service.base_url, MINT)

        response = _ticket_run(service, ticket, tools=SONGMAKER_TOOLS)

        claims = service.admitted()[-1].capability.claims
        assert response.status_code == 200
        assert (claims.principal.sub, claims.purpose) == ("user-alice", COWRITER)


def test_a_ticket_of_another_issuer_is_forbidden():
    """J15, J2: the issuer the groups came from must be the configured one"""
    other_issuer = "https://elsewhere.test/realms/x"
    ticket = signed_ticket(ticket_claims(principal={"iss": other_issuer, "sub": "user-alice"}))
    with _service() as service, _service(service_config(issuer=other_issuer)) as accepting:
        assert_rejected(_ticket_run(service, ticket), *FORBIDDEN)
        headers = ticket_headers(MINT.workload(iss=other_issuer), ticket)
        assert post_run(accepting.base_url, headers, run_body()).status_code == 200
