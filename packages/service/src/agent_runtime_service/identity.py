"""Who is asking: the app's workload token and its end user's token, checked together,
or the workload token and the job ticket the app got for that user earlier.

Both tokens are verified by webauth's RS256 verifier against the configured
issuer and audience (decision record 0001, H1). The workload token is checked
first, then the user token, and the first failure decides: a token that does
not verify is ``unauthorized``, a key set that cannot be fetched is
``unavailable``, and verified tokens that do not bind one registered app to one
end user are ``forbidden`` (H3), as is a user who is no member of the app: none of their groups is
``/apps/<app>`` or below it. A user token whose subject is missing, or is
not valid Unicode text (a lone surrogate), is a credential no later token can
name, so it is ``unauthorized`` for every app; any other subject, of any
length, is accepted.

A workload token binds its app only through claims a user token cannot carry:
the realm role ``agent-workload``, which only service accounts hold, and
``azp == client_id ==`` the app's registered workload client. ``client_id``
alone proves nothing, since a user token may carry one too.

A job ticket (``agent_runtime_service.job_ticket``) is the second way to be a caller. Its
text is turned into a ``TicketBinding`` before this module sees it; the workload token is
checked first, then the ticket's binding to that workload client, to the app and to the
configured issuer is ``forbidden`` when any differs, and the role comes from the groups bound
at mint.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import anyio.to_thread
from pydantic import TypeAdapter, ValidationError
from webauth.jwt_verification import (
    JwtVerifier,
    KeySetFetch,
    MonotonicClock,
    Rejected,
    RejectionReason,
    VerifiedClaims,
)
from webauth.oidc_login import AppRole, app_role

from agent_runtime_client.wire import AgentRole, Purpose, RejectionCode, UnicodeText
from agent_runtime_service.config import IdentityConfig, ServiceConfig

WORKLOAD_ROLE: Final = "agent-workload"
MAX_WORKLOAD_TOKEN_SECONDS: Final = 300
_UNICODE_TEXT: Final = TypeAdapter(UnicodeText)


@dataclass(frozen=True)
class Principal:
    """The end user, named by the issuer and their subject (H1)."""

    issuer: str
    subject: str


_AGENT_ROLE_OF: Final[Mapping[AppRole, AgentRole]] = {
    AppRole.ADMIN: "admin",
    AppRole.USER: "user",
}


@dataclass(frozen=True)
class TicketBinding:
    """What a verified job ticket bound at mint: one app, one person, one agent."""

    app: str
    client: str
    principal: Principal
    groups: frozenset[str]
    purpose: Purpose


@dataclass(frozen=True)
class Caller:
    """A verified app acting for a verified end user, who holds ``role`` in that app.

    ``purpose`` is the one agent a job ticket allows, and ``None`` for a live caller.
    """

    app: str
    principal: Principal
    groups: frozenset[str]
    role: AgentRole
    purpose: Purpose | None


def build_verifier(
    identity: IdentityConfig,
    fetch_key_set: KeySetFetch,
    monotonic_clock: MonotonicClock,
) -> JwtVerifier:
    """The process's one verifier: it owns the key-set cache, refresh lock and cooldown."""
    return JwtVerifier(
        issuer=identity.issuer,
        audiences=[identity.audience],
        jwks_url=identity.jwks_url,
        leeway_seconds=identity.leeway_seconds,
        fetch_key_set=fetch_key_set,
        wall_clock=time.time,
        monotonic_clock=monotonic_clock,
    )


class _Refused(Exception):
    def __init__(self, code: RejectionCode) -> None:
        super().__init__(code)
        self.code = code


class CallerCheck:
    def __init__(self, verifier: JwtVerifier, config: ServiceConfig) -> None:
        self._verifier = verifier
        self._config = config

    async def caller(
        self, workload_token: str | None, user_token: str | None
    ) -> Caller | RejectionCode:
        try:
            return await self._checked_caller(workload_token, user_token)
        except _Refused as refusal:
            return refusal.code

    async def _checked_caller(self, workload_token: str | None, user_token: str | None) -> Caller:
        workload = await self._verified(workload_token)
        user = await self._verified(user_token)
        if user.subject is None or not _is_unicode_text(user.subject):
            raise _Refused(RejectionCode.UNAUTHORIZED)
        groups = self._groups(user.claims)
        app = self._workload_app(workload)
        if user.claims.get("azp") != workload.claims["azp"]:
            raise _Refused(RejectionCode.FORBIDDEN)
        if user.subject == workload.subject:
            raise _Refused(RejectionCode.FORBIDDEN)
        return Caller(
            app=app,
            principal=Principal(user.issuer, user.subject),
            groups=groups,
            role=self._role_in(app, groups),
            purpose=None,
        )

    async def ticket_caller(
        self, workload_token: str | None, binding: TicketBinding | None
    ) -> Caller | RejectionCode:
        """``binding`` is ``None`` when the ticket text did not verify."""
        try:
            return await self._checked_ticket_caller(workload_token, binding)
        except _Refused as refusal:
            return refusal.code

    async def _checked_ticket_caller(
        self, workload_token: str | None, binding: TicketBinding | None
    ) -> Caller:
        workload = await self._verified(workload_token)
        if binding is None:
            raise _Refused(RejectionCode.UNAUTHORIZED)
        app = self._workload_app(workload)
        if (
            binding.app != app
            or binding.client != workload.claims["azp"]
            or binding.principal.issuer != self._config.identity.issuer
        ):
            raise _Refused(RejectionCode.FORBIDDEN)
        return Caller(
            app=app,
            principal=binding.principal,
            groups=binding.groups,
            role=self._role_in(app, binding.groups),
            purpose=binding.purpose,
        )

    def _role_in(self, app: str, groups: frozenset[str]) -> AgentRole:
        role = app_role(tuple(groups), self._config.app_admission(app))
        if role is None:
            raise _Refused(RejectionCode.FORBIDDEN)
        return _AGENT_ROLE_OF[role]

    async def _verified(self, token: str | None) -> VerifiedClaims:
        if token is None:
            raise _Refused(RejectionCode.UNAUTHORIZED)
        result = await anyio.to_thread.run_sync(self._verifier.verify, token)
        if isinstance(result, Rejected):
            raise _Refused(_rejection_code(result))
        return result

    def _groups(self, claims: Mapping[str, object]) -> frozenset[str]:
        claim = self._config.identity.group_claim
        if claim not in claims:
            return frozenset()
        groups = claims[claim]
        if not isinstance(groups, list) or not all(isinstance(group, str) for group in groups):
            raise _Refused(RejectionCode.UNAUTHORIZED)
        return frozenset(groups)

    def _workload_app(self, workload: VerifiedClaims) -> str:
        claims = workload.claims
        client = claims.get("azp")
        if workload.subject is None or not isinstance(client, str) or not client:
            raise _Refused(RejectionCode.FORBIDDEN)
        if claims.get("client_id") != client:
            raise _Refused(RejectionCode.FORBIDDEN)
        if WORKLOAD_ROLE not in _realm_roles(claims):
            raise _Refused(RejectionCode.FORBIDDEN)
        if workload.expires_at - workload.issued_at > MAX_WORKLOAD_TOKEN_SECONDS:
            raise _Refused(RejectionCode.FORBIDDEN)
        app = self._config.app_of_workload_client(client)
        if app is None:
            raise _Refused(RejectionCode.FORBIDDEN)
        return app


def _is_unicode_text(text: str) -> bool:
    try:
        _UNICODE_TEXT.validate_python(text)
    except ValidationError:
        return False
    return True


def _rejection_code(rejected: Rejected) -> RejectionCode:
    if rejected.reason is RejectionReason.KEY_SET_UNAVAILABLE:
        return RejectionCode.UNAVAILABLE
    return RejectionCode.UNAUTHORIZED


def _realm_roles(claims: Mapping[str, object]) -> frozenset[str]:
    realm_access = claims.get("realm_access")
    if not isinstance(realm_access, dict):
        return frozenset()
    roles = realm_access.get("roles")
    if not isinstance(roles, list):
        return frozenset()
    return frozenset(role for role in roles if isinstance(role, str))
