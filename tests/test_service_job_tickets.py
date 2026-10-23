"""``POST /job-tickets``: who gets a ticket, what it binds, and where it may appear
(#447 J1, J2, J3, J11, J12, J14, J16, J17)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from mcp_server_support import VERIFICATION_KEY
from pydantic import TypeAdapter, ValidationError
from service_support import (
    API_GROUP,
    OLD_SUBSCRIPTION_GROUP,
    PRESENTATOR_CLIENT,
    SONGMAKER_GROUP,
    SUBSCRIPTION_GROUP,
    TokenMint,
    assert_rejected,
    plain_service_config,
    process_signing_key_file,
    reaches_text,
    run_headers,
    served,
    service_config,
)

from agent_runtime_client.capability import CAPABILITY_ISSUER, McpOrigin, carries_signature_of
from agent_runtime_client.mcp_server import CapabilityRefusal, verify_capability
from agent_runtime_client.wire import (
    MAX_JOB_TICKET_SECONDS,
    PROTOCOL_VERSION,
    JobTicket,
    RejectionCode,
)
from agent_runtime_service.app import create_app
from agent_runtime_service.job_ticket import JOB_TICKET_AUDIENCE, JOB_TICKET_TOKEN_TYPE
from agent_runtime_service.run_request import MAX_REQUEST_BYTES

MINT = TokenMint()
ADMIN_GROUPS = (SUBSCRIPTION_GROUP, SONGMAKER_GROUP + "/admin", "/family")


@pytest.fixture(scope="module")
def service() -> Iterator[str]:
    app = create_app(service_config(), fetch_key_set=MINT.fetch_key_set)
    with served(app) as base_url:
        yield base_url


def _request(**overrides: object) -> dict[str, object]:
    return {"v": PROTOCOL_VERSION, "purpose": "judge", "expires_in_s": 3600, **overrides}


def _post_ticket(
    base_url: str, headers: dict[str, str], body: object = None, *, content: bytes | None = None
) -> httpx.Response:
    payload = content if content is not None else json.dumps(_request() if body is None else body)
    return httpx.post(
        f"{base_url}/job-tickets",
        headers={"Content-Type": "application/json", **headers},
        content=payload,
        timeout=10,
    )


def _member_headers(groups: object = ADMIN_GROUPS) -> dict[str, str]:
    return run_headers(MINT.workload(), MINT.user(groups))


def _verified(ticket: str) -> dict[str, Any]:
    public_key = serialization.load_pem_private_key(
        process_signing_key_file().read_bytes(), password=None
    ).public_key()
    assert jwt.get_unverified_header(ticket)["typ"] == JOB_TICKET_TOKEN_TYPE
    return jwt.decode(
        ticket,
        public_key,
        algorithms=["RS256"],
        audience=JOB_TICKET_AUDIENCE,
        issuer=CAPABILITY_ISSUER,
        options={"verify_exp": False},
    )


def _minted(base_url: str, groups: object = ADMIN_GROUPS, **overrides: object) -> JobTicket:
    response = _post_ticket(base_url, _member_headers(groups), _request(**overrides))
    assert response.status_code == 200, response.text
    return JobTicket.model_validate_json(response.content)


def test_a_member_with_both_tokens_gets_a_ticket_bound_to_app_person_agent_and_expiry(service):
    """J2, J1: the ticket binds app, person, agent and expiry."""

    ticket = _minted(service)

    claims = _verified(ticket.ticket)
    assert claims["app"] == "songmaker"
    assert claims["principal"] == {"iss": MINT.user_claims()["iss"], "sub": "user-alice"}
    assert claims["purpose"] == "judge"
    assert claims["exp"] - claims["iat"] == 3600
    assert ticket.expires_at == datetime.fromtimestamp(claims["exp"], UTC)
    assert claims["groups"] == [SONGMAKER_GROUP + "/admin", SUBSCRIPTION_GROUP]


def test_a_ticket_names_the_workload_client_of_the_app(service):
    """J2: the ticket binds the app's workload client."""

    assert _verified(_minted(service).ticket)["client"] == "songmaker-test"


def test_only_the_groups_the_service_reads_are_bound(service):
    """J2: only the groups the service reads are bound."""

    groups = (
        SUBSCRIPTION_GROUP,
        OLD_SUBSCRIPTION_GROUP,
        API_GROUP,
        SONGMAKER_GROUP,
        SONGMAKER_GROUP + "/admin/deep",
        "/apps/presentator",
        SONGMAKER_GROUP + "-beta",
        "/family",
    )

    claims = _verified(_minted(service, groups).ticket)

    assert claims["groups"] == sorted(
        [
            SUBSCRIPTION_GROUP,
            OLD_SUBSCRIPTION_GROUP,
            API_GROUP,
            SONGMAKER_GROUP,
            SONGMAKER_GROUP + "/admin/deep",
        ]
    )


def test_a_ticket_has_the_capability_issuer_its_own_audience_and_no_run_subject(service):
    """J12, J14: own audience, the capability issuer, no run subject."""

    claims = _verified(_minted(service).ticket)

    assert claims["iss"] == CAPABILITY_ISSUER
    assert claims["aud"] == JOB_TICKET_AUDIENCE
    assert "sub" not in claims
    assert isinstance(claims["iat"], int)
    assert isinstance(claims["exp"], int)
    with pytest.raises(ValidationError):
        TypeAdapter(McpOrigin).validate_python(JOB_TICKET_AUDIENCE)


def test_two_tickets_for_the_same_person_and_agent_are_two_tickets(service):
    """J17: no maximum, nothing stored; a second request is a second ticket."""

    first, second = _minted(service), _minted(service)

    assert first.ticket != second.ticket
    assert _verified(first.ticket)["jti"] != _verified(second.ticket)["jti"]


def test_a_person_with_no_route_right_still_gets_a_ticket(service):
    """J17: a person with no route right still gets a ticket."""

    ticket = _minted(service, (SONGMAKER_GROUP,))

    assert _verified(ticket.ticket)["groups"] == [SONGMAKER_GROUP]


@pytest.mark.parametrize("seconds", [1, MAX_JOB_TICKET_SECONDS])
def test_a_lifetime_within_the_bounds_is_accepted(service, seconds):
    """J3: one second and the 24-hour maximum are accepted."""

    claims = _verified(_minted(service, expires_in_s=seconds).ticket)

    assert claims["exp"] - claims["iat"] == seconds


@pytest.mark.parametrize("seconds", [0, -1, MAX_JOB_TICKET_SECONDS + 1, "3600", 3600.5, True, None])
def test_a_lifetime_outside_the_bounds_is_refused(service, seconds):
    """J3: a lifetime outside the bounds is invalid_request."""

    response = _post_ticket(service, _member_headers(), _request(expires_in_s=seconds))

    assert_rejected(response, RejectionCode.INVALID_REQUEST, 422)


def test_a_ticket_has_no_default_lifetime(service):
    """J3: there is no default lifetime."""

    body = _request()
    del body["expires_in_s"]

    assert_rejected(
        _post_ticket(service, _member_headers(), body), RejectionCode.INVALID_REQUEST, 422
    )


@pytest.mark.parametrize(
    "overrides", [{"purpose": "Not A Purpose"}, {"purpose": 7}, {"surplus": 1}]
)
def test_a_body_off_the_contract_is_refused(service, overrides):
    """J14: a body off the contract is invalid_request."""

    response = _post_ticket(service, _member_headers(), _request(**overrides))

    assert_rejected(response, RejectionCode.INVALID_REQUEST, 422)


def test_a_body_of_another_protocol_version_is_unsupported(service):
    """J14: the protocol version stays 2."""

    response = _post_ticket(service, _member_headers(), _request(v=1))

    assert_rejected(response, RejectionCode.UNSUPPORTED_VERSION, 400)


def test_a_body_over_the_limit_is_refused(service):
    """J14: the request body limit holds on the mint."""

    response = _post_ticket(
        service, _member_headers(), content=b" " * (MAX_REQUEST_BYTES + 1) + b"{}"
    )

    assert_rejected(response, RejectionCode.INVALID_REQUEST, 422)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda: run_headers(None, MINT.user()), id="no workload token"),
        pytest.param(lambda: run_headers(MINT.workload(), None), id="no user token"),
    ],
)
def test_a_request_without_both_tokens_is_unauthorized(service, headers):
    """J1: a ticket needs both tokens."""

    assert_rejected(_post_ticket(service, headers()), RejectionCode.UNAUTHORIZED, 401)


def test_a_user_outside_the_app_is_forbidden(service):
    """J1: the app check applies to the mint."""

    response = _post_ticket(service, _member_headers((SUBSCRIPTION_GROUP, "/apps/presentator")))

    assert_rejected(response, RejectionCode.FORBIDDEN, 403)


def test_a_user_token_of_another_app_is_forbidden(service):
    """J1: the app check applies to the mint."""

    headers = run_headers(MINT.workload(), MINT.user(azp=PRESENTATOR_CLIENT))

    assert_rejected(_post_ticket(service, headers), RejectionCode.FORBIDDEN, 403)


def test_a_ticket_cannot_mint_a_ticket(service):
    """J1: a ticket cannot fetch a new ticket."""

    ticket = _minted(service).ticket

    in_the_ticket_header = {"Agent-Job-Ticket": ticket, "Agent-Workload-Token": MINT.workload()}
    as_the_user_token = run_headers(MINT.workload(), ticket)

    assert_rejected(_post_ticket(service, in_the_ticket_header), RejectionCode.UNAUTHORIZED, 401)
    assert_rejected(_post_ticket(service, as_the_user_token), RejectionCode.UNAUTHORIZED, 401)


def test_a_capability_verifier_refuses_a_ticket_by_its_type(service):
    """J12: an app's MCP server refuses a ticket as a capability."""

    ticket = _minted(service).ticket

    outcome = verify_capability(
        ticket, key=VERIFICATION_KEY, origin="https://songmaker.internal", now=0
    )

    assert outcome is CapabilityRefusal.WRONG_TYPE


@pytest.fixture(scope="module")
def unsigned_service() -> Iterator[str]:
    config = plain_service_config().model_copy(update={"signing": None})
    with served(create_app(config, fetch_key_set=MINT.fetch_key_set)) as base_url:
        yield base_url


def test_an_installation_without_a_signing_key_does_not_enable_tickets(unsigned_service):
    """J16: no [signing] section answers 403 not_enabled."""

    response = _post_ticket(unsigned_service, _member_headers())

    assert_rejected(response, RejectionCode.NOT_ENABLED, 403)


def test_tokens_are_checked_before_the_body_and_the_body_before_the_installation(
    service, unsigned_service
):
    """J16, J1: tokens, then the body, then the installation."""

    assert_rejected(
        _post_ticket(service, run_headers(None, MINT.user()), content=b"not json"),
        RejectionCode.UNAUTHORIZED,
        401,
    )
    assert_rejected(
        _post_ticket(unsigned_service, _member_headers(), content=b"not json"),
        RejectionCode.INVALID_REQUEST,
        422,
    )


def test_the_ticket_is_in_the_one_response_only(service, caplog):
    """J11: the ticket is in no log, header or repr, only the one body."""

    workload, user = MINT.workload(), MINT.user(ADMIN_GROUPS)
    with caplog.at_level(logging.DEBUG):
        minted = _post_ticket(service, run_headers(workload, user))
        _post_ticket(service, run_headers(workload, MINT.user(azp=PRESENTATOR_CLIENT)))
        _post_ticket(service, run_headers(workload, user), content=b"not json")

    ticket = JobTicket.model_validate_json(minted.content)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert not reaches_text(caplog.records, [ticket.ticket, workload, user])
    assert not carries_signature_of(ticket.ticket, logged.encode())
    assert minted.headers["cache-control"] == "no-store"
    assert not any(ticket.ticket in value for value in minted.headers.values())
    assert ticket.ticket not in repr(ticket)
