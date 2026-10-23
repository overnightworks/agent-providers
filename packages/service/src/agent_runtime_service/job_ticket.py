"""What a job ticket says and how it is minted (decision record 0001, H12).

A job ticket lets an app start a run later, with no live user token. It is signed with
the capability's one key under its own token type and audience, so a verifier of
capabilities refuses it. The service stores no ticket: this module only builds and signs.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
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
from agent_runtime_service.identity import Caller
from agent_runtime_service.run_request import versioned_document
from agent_runtime_service.signing import SigningKey

JOB_TICKET_TOKEN_TYPE: Final = "agent-job-ticket+jwt"
JOB_TICKET_AUDIENCE: Final = "agent-job-ticket"


class JobTicketClaims(BaseModel):
    """What a ticket says; the audience is no URL, so it never equals an MCP origin."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    iss: Literal[CAPABILITY_ISSUER]
    aud: Literal[JOB_TICKET_AUDIENCE]
    iat: int
    exp: int
    jti: Annotated[UUID, Field(strict=False)]
    app: NonEmptyUnicodeText
    client: NonEmptyUnicodeText
    principal: CapabilityPrincipal
    purpose: Purpose
    groups: list[NonEmptyUnicodeText]

    @model_validator(mode="after")
    def _expires_after_it_is_issued(self) -> Self:
        if self.exp <= self.iat:
            raise ValueError("exp must be later than iat")
        return self


def parse_job_ticket_request(body: bytes) -> JobTicketRequest | RejectionCode:
    document = versioned_document(body)
    if isinstance(document, RejectionCode):
        return document
    try:
        return JobTicketRequest.model_validate(document)
    except ValueError:
        return RejectionCode.INVALID_REQUEST


def job_ticket_claims(
    config: ServiceConfig, caller: Caller, request: JobTicketRequest, now: float
) -> JobTicketClaims:
    return JobTicketClaims(
        iss=CAPABILITY_ISSUER,
        aud=JOB_TICKET_AUDIENCE,
        iat=math.floor(now),
        exp=math.floor(now + request.expires_in_s),
        jti=uuid.uuid4(),
        app=caller.app,
        client=config.apps[caller.app].workload_client,
        principal=CapabilityPrincipal(iss=caller.principal.issuer, sub=caller.principal.subject),
        purpose=request.purpose,
        groups=sorted(_groups_the_service_reads(config, caller)),
    )


def signed_job_ticket(key: SigningKey, claims: JobTicketClaims) -> JobTicket:
    ticket = key.sign(claims.model_dump(mode="json"), JOB_TICKET_TOKEN_TYPE)
    return JobTicket(
        v=PROTOCOL_VERSION, ticket=ticket, expires_at=datetime.fromtimestamp(claims.exp, UTC)
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
