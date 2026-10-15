"""Who is asking: the app's workload token and its end user's token, checked together.

Both tokens are verified by webauth's RS256 verifier against the configured
issuer and audience (decision record 0001, H1). The workload token is checked
first, then the user token, and the first failure decides: a token that does
not verify is ``unauthorized``, a key set that cannot be fetched is
``unavailable``, and verified tokens that do not bind one registered app to one
end user are ``forbidden`` (H3).

A workload token binds its app only through claims a user token cannot carry:
the realm role ``agent-workload``, which only service accounts hold, and
``azp == client_id ==`` the app's registered workload client. ``client_id``
alone proves nothing, since a user token may carry one too.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import anyio.to_thread
from webauth.jwt_verification import (
    JwtVerifier,
    KeySetFetch,
    Rejected,
    RejectionReason,
    VerifiedClaims,
)

from agent_client.wire import RejectionCode
from agent_service.config import IdentityConfig, ServiceConfig

WORKLOAD_ROLE: Final = "agent-workload"
MAX_WORKLOAD_TOKEN_SECONDS: Final = 300


@dataclass(frozen=True)
class Principal:
    """The end user, named by the issuer and their subject (H1)."""

    issuer: str
    subject: str


@dataclass(frozen=True)
class Caller:
    """A verified app acting for a verified end user."""

    app: str
    principal: Principal
    groups: frozenset[str]


def build_verifier(identity: IdentityConfig, fetch_key_set: KeySetFetch) -> JwtVerifier:
    """The process's one verifier: it owns the key-set cache, refresh lock and cooldown."""
    return JwtVerifier(
        issuer=identity.issuer,
        audiences=[identity.audience],
        jwks_url=identity.jwks_url,
        leeway_seconds=identity.leeway_seconds,
        fetch_key_set=fetch_key_set,
        wall_clock=time.time,
        monotonic_clock=time.monotonic,
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
        subject = _non_empty_text(user.claims, "sub", refusal=RejectionCode.UNAUTHORIZED)
        groups = self._groups(user.claims)
        app = self._workload_app(workload)
        if user.claims.get("azp") != workload.claims["azp"]:
            raise _Refused(RejectionCode.FORBIDDEN)
        if subject == workload.claims["sub"]:
            raise _Refused(RejectionCode.FORBIDDEN)
        return Caller(app=app, principal=Principal(user.issuer, subject), groups=groups)

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
        _non_empty_text(claims, "sub", refusal=RejectionCode.FORBIDDEN)
        client = _non_empty_text(claims, "azp", refusal=RejectionCode.FORBIDDEN)
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


def _rejection_code(rejected: Rejected) -> RejectionCode:
    if rejected.reason is RejectionReason.KEY_SET_UNAVAILABLE:
        return RejectionCode.UNAVAILABLE
    return RejectionCode.UNAUTHORIZED


def _non_empty_text(claims: Mapping[str, object], name: str, *, refusal: RejectionCode) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise _Refused(refusal)
    return value


def _realm_roles(claims: Mapping[str, object]) -> frozenset[str]:
    realm_access = claims.get("realm_access")
    if not isinstance(realm_access, dict):
        return frozenset()
    roles = realm_access.get("roles")
    if not isinstance(roles, list):
        return frozenset()
    return frozenset(role for role in roles if isinstance(role, str))
