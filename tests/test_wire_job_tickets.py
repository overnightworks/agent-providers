"""The job ticket's wire bodies, owned by ``agent_runtime_client`` (#447 J3, J11, J14)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_runtime_client.wire import (
    MAX_JOB_TICKET_SECONDS,
    PROTOCOL_VERSION,
    JobTicket,
    JobTicketRequest,
)

JOB_TICKET_REQUEST = {"v": PROTOCOL_VERSION, "purpose": "reviewer", "expires_in_s": 3600}
JOB_TICKET = {"v": PROTOCOL_VERSION, "ticket": "a.b.c", "expires_at": "2026-10-24T12:00:00Z"}


def test_a_job_ticket_request_round_trips_through_its_json() -> None:
    """J14: the request is `{v, purpose, expires_in_s}`."""

    request = JobTicketRequest.model_validate_json(json.dumps(JOB_TICKET_REQUEST))

    assert json.loads(request.model_dump_json()) == JOB_TICKET_REQUEST


@pytest.mark.parametrize(
    "overrides",
    [
        {"v": 1},
        {"expires_in_s": 0},
        {"expires_in_s": MAX_JOB_TICKET_SECONDS + 1},
        {"expires_in_s": 3600.0},
        {"expires_in_s": "3600"},
        {"purpose": "Not A Purpose"},
        {"surplus": 1},
    ],
)
def test_a_job_ticket_request_off_the_contract_is_refused(overrides: dict[str, object]) -> None:
    """J3, J14: a lifetime outside the bounds or a body off the contract is refused."""

    with pytest.raises(ValidationError):
        JobTicketRequest.model_validate({**JOB_TICKET_REQUEST, **overrides})


def test_a_job_ticket_request_has_no_default_lifetime() -> None:
    """J3: there is no default lifetime."""

    with pytest.raises(ValidationError):
        JobTicketRequest.model_validate({"v": PROTOCOL_VERSION, "purpose": "reviewer"})


def test_a_job_ticket_round_trips_and_its_repr_hides_the_ticket() -> None:
    """J14, J11: the answer is `{v, ticket, expires_at}` and its repr hides the ticket."""

    ticket = JobTicket.model_validate_json(json.dumps(JOB_TICKET))

    assert json.loads(ticket.model_dump_json()) == JOB_TICKET
    assert "a.b.c" not in repr(ticket)


@pytest.mark.parametrize(
    "overrides", [{"ticket": ""}, {"expires_at": "2026-10-24T12:00:00+02:00"}, {"surplus": 1}]
)
def test_a_job_ticket_off_the_contract_is_refused(overrides: dict[str, object]) -> None:
    """J14: an answer off the contract is refused."""

    with pytest.raises(ValidationError):
        JobTicket.model_validate_json(json.dumps({**JOB_TICKET, **overrides}))
