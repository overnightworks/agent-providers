"""What a job ticket says, how it is minted and how it is read back (decision record 0001, H12).

A job ticket lets an app start a run later, with no live user token. It is signed with
the capability's one key under its own token type and audience, so a verifier of
capabilities refuses it, and this module's verifier refuses a capability. The service
stores no ticket: this module builds and signs one, and reads one back into the facts it bound.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, Self

import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from webauth.oidc_login import app_role

from agent_runtime_client.capability import CAPABILITY_ISSUER, CapabilityPrincipal
from agent_runtime_client.wire import (
    PROTOCOL_VERSION,
    JobTicket,
    JobTicketRequest,
    NonEmptyUnicodeText,
    Purpose,
    RejectionCode,
)
from agent_runtime_service.config import ServiceConfig
from agent_runtime_service.identity import Caller, Principal, TicketBinding
from agent_runtime_service.run_request import parse_versioned_model
from agent_runtime_service.signing import SigningKey

JOB_TICKET_TOKEN_TYPE: Final = "agent-job-ticket+jwt"
JOB_TICKET_AUDIENCE: Final = "agent-job-ticket"
_TICKET_ALGORITHM: Final = "RS256"


class JobTicketClaims(BaseModel):
    """What a ticket says; the audience is no URL, so it never equals an MCP origin."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    iss: Literal[CAPABILITY_ISSUER]
    aud: Literal[JOB_TICKET_AUDIENCE]
    iat: int
    exp: int
    app: NonEmptyUnicodeText
    client: NonEmptyUnicodeText
    principal: CapabilityPrincipal
    purpose: Purpose
    groups: Annotated[tuple[NonEmptyUnicodeText, ...], Field(strict=False)]

    @model_validator(mode="after")
    def _expires_after_it_is_issued(self) -> Self:
        if self.exp <= self.iat:
            raise ValueError("exp must be later than iat")
        return self


def parse_job_ticket_request(body: bytes) -> JobTicketRequest | RejectionCode:
    return parse_versioned_model(body, JobTicketRequest)


def job_ticket_claims(
    config: ServiceConfig, caller: Caller, request: JobTicketRequest, now: float
) -> JobTicketClaims:
    return JobTicketClaims(
        iss=CAPABILITY_ISSUER,
        aud=JOB_TICKET_AUDIENCE,
        iat=math.floor(now),
        exp=math.floor(now + request.expires_in_s),
        app=caller.app,
        client=config.apps[caller.app].workload_client,
        principal=CapabilityPrincipal(iss=caller.principal.issuer, sub=caller.principal.subject),
        purpose=request.purpose,
        groups=tuple(sorted(_groups_the_service_reads(config, caller))),
    )


def signed_job_ticket(key: SigningKey, claims: JobTicketClaims) -> JobTicket:
    ticket = key.sign(claims.model_dump(mode="json"), JOB_TICKET_TOKEN_TYPE)
    return JobTicket(
        v=PROTOCOL_VERSION, ticket=ticket, expires_at=datetime.fromtimestamp(claims.exp, UTC)
    )


def verified_job_ticket(key: SigningKey, text: str, now: float) -> TicketBinding | None:
    """What the ticket bound, or ``None`` for any text that is not this key's live ticket.

    No refusal reason leaves, and none carries any part of ``text``.
    """
    try:
        decoded = jwt.PyJWS().decode_complete(text, key.public_key, algorithms=[_TICKET_ALGORITHM])
        header = decoded["header"]
        if header.get("typ") != JOB_TICKET_TOKEN_TYPE or header.get("kid") != key.kid:
            return None
        claims = JobTicketClaims.model_validate_json(decoded["payload"])
    except (jwt.PyJWTError, ValidationError):
        return None
    if claims.iat > now or now >= claims.exp:
        return None
    return TicketBinding(
        app=claims.app,
        client=claims.client,
        principal=Principal(claims.principal.iss, claims.principal.sub),
        groups=frozenset(claims.groups),
        purpose=claims.purpose,
    )


def _groups_the_service_reads(config: ServiceConfig, caller: Caller) -> set[str]:
    """The subscription groups, the API groups, and the app's own path with all below it."""
    route_groups = config.identity.subscription_groups | config.identity.api_groups
    admission = config.app_admission(caller.app)
    return {
        group
        for group in caller.groups
        if group in route_groups or app_role((group,), admission) is not None
    }
