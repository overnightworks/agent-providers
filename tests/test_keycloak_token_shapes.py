"""The token shapes a real Keycloak 26.8.1 issues for the agent service's callers.

The AI entrance builds on these claims, so they are proven against a real
issuer rather than assumed: `tests/keycloak/realm.json` mirrors the platform
realm, the `keycloak-token-shapes` CI job serves it from Keycloak, and every
token here comes from the realm's token endpoint and is verified against its
published key set with webauth's RS256 verifier.

Runs only where `KEYCLOAK_URL` names that Keycloak. Needs nothing from the
workspace: the job runs it with `--noconftest` in the hash-locked environment
of `tests/keycloak/requirements.txt`.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping

import httpx
import pytest

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL")

pytestmark = [
    pytest.mark.keycloak,
    pytest.mark.skipif(KEYCLOAK_URL is None, reason="KEYCLOAK_URL names no test Keycloak"),
]

REALM = "agent-test"
AGENT_SERVICE_AUDIENCE = "agent-service"
WORKLOAD_ROLE = "agent-workload"
MAX_ACCESS_TOKEN_SECONDS = 300
USER_LOGIN_CLIENT = "songmaker-test"

# Fixed test-only secrets of the throwaway realm in tests/keycloak/realm.json.
CLIENT_SECRETS = {
    "songmaker-test": "songmaker-test-secret",
    "presentator-test": "presentator-test-secret",
}
USER_PASSWORDS = {
    "alice": "alice-test-password",
    "bob": "bob-test-password",
    "carol": "carol-test-password",
}
EXPECTED_GROUPS = {
    "alice": {"/ki-abo", "/apps/songmaker"},
    "bob": {"/apps/songmaker"},
    "carol": set(),
}

WORKLOAD_CLIENTS = sorted(CLIENT_SECRETS)
USERS = sorted(USER_PASSWORDS)

Claims = Mapping[str, object]


def _realm_url() -> str:
    return f"{KEYCLOAK_URL}/realms/{REALM}"


def _fetch_key_set_from_test_keycloak(_configured_url: str) -> bytes:
    """The test Keycloak serves plain HTTP, while the verifier accepts only an
    https key-set URL; its injected fetch is the seam that reads the same key
    set from the realm's real certs endpoint instead."""
    response = httpx.get(f"{_realm_url()}/protocol/openid-connect/certs", timeout=10)
    response.raise_for_status()
    return response.content


@pytest.fixture(scope="module")
def verify():
    from webauth.jwt_verification import JwtVerifier, VerifiedClaims

    verifier = JwtVerifier(
        issuer=_realm_url(),
        audiences=[AGENT_SERVICE_AUDIENCE],
        jwks_url=f"https://keycloak.test/realms/{REALM}/protocol/openid-connect/certs",
        leeway_seconds=0,
        fetch_key_set=_fetch_key_set_from_test_keycloak,
        wall_clock=time.time,
        monotonic_clock=time.monotonic,
    )

    def verified_claims(access_token: str) -> Claims:
        result = verifier.verify(access_token)
        assert isinstance(result, VerifiedClaims), f"webauth rejected the token: {result}"
        return result.claims

    return verified_claims


def _access_token(form: dict[str, str]) -> str:
    response = httpx.post(f"{_realm_url()}/protocol/openid-connect/token", data=form, timeout=10)
    assert response.status_code == 200, (
        f"token endpoint answered {response.status_code}: {response.text}"
    )
    return response.json()["access_token"]


@pytest.fixture(scope="module")
def service_account_claims(verify) -> dict[str, Claims]:
    return {
        client: verify(
            _access_token(
                {
                    "grant_type": "client_credentials",
                    "client_id": client,
                    "client_secret": secret,
                }
            )
        )
        for client, secret in CLIENT_SECRETS.items()
    }


@pytest.fixture(scope="module")
def user_claims(verify) -> dict[str, Claims]:
    return {
        user: verify(
            _access_token(
                {
                    "grant_type": "password",
                    "client_id": USER_LOGIN_CLIENT,
                    "client_secret": CLIENT_SECRETS[USER_LOGIN_CLIENT],
                    "username": user,
                    "password": password,
                }
            )
        )
        for user, password in USER_PASSWORDS.items()
    }


def _realm_roles(claims: Claims) -> set[str]:
    realm_access = claims.get("realm_access") or {}
    return set(realm_access.get("roles", []))


@pytest.mark.parametrize("client", WORKLOAD_CLIENTS)
def test_service_account_token_names_its_client_in_azp_and_client_id(
    service_account_claims, client
):
    claims = service_account_claims[client]
    assert claims.get("azp") == client, f"azp is {claims.get('azp')!r}, not {client!r}"
    assert claims.get("client_id") == client, (
        f"client_id is {claims.get('client_id')!r}, not {client!r}"
    )


@pytest.mark.parametrize("client", WORKLOAD_CLIENTS)
def test_service_account_token_carries_the_agent_workload_role(service_account_claims, client):
    roles = _realm_roles(service_account_claims[client])
    assert WORKLOAD_ROLE in roles, f"realm_access.roles {sorted(roles)} lacks {WORKLOAD_ROLE}"


@pytest.mark.parametrize("client", WORKLOAD_CLIENTS)
def test_service_account_token_lives_at_most_300_seconds(service_account_claims, client):
    claims = service_account_claims[client]
    lifetime = claims["exp"] - claims["iat"]
    assert lifetime <= MAX_ACCESS_TOKEN_SECONDS, f"exp - iat is {lifetime} s, over 300 s"


@pytest.mark.parametrize("user", USERS)
def test_user_token_never_carries_the_agent_workload_role(user_claims, user):
    roles = _realm_roles(user_claims[user])
    assert WORKLOAD_ROLE not in roles, (
        f"realm_access.roles of {user} carries {WORKLOAD_ROLE}: {sorted(roles)}"
    )


@pytest.mark.parametrize("user", USERS)
def test_user_token_names_the_login_client_in_azp(user_claims, user):
    claims = user_claims[user]
    # Recorded, not relied on: whether a user token carries client_id.
    print(f"{user}: client_id {claims.get('client_id', '<absent>')!r}")
    assert claims.get("azp") == USER_LOGIN_CLIENT, (
        f"azp of {user} is {claims.get('azp')!r}, not {USER_LOGIN_CLIENT!r}"
    )


@pytest.mark.parametrize("user", USERS)
def test_user_token_audience_contains_the_agent_service(user_claims, user):
    audience = user_claims[user].get("aud")
    audiences = {audience} if isinstance(audience, str) else set(audience or [])
    assert AGENT_SERVICE_AUDIENCE in audiences, (
        f"aud of {user} is {audience!r}, without {AGENT_SERVICE_AUDIENCE}"
    )


@pytest.mark.parametrize("user", USERS)
def test_user_token_lists_groups_as_full_paths(user_claims, user):
    groups = user_claims[user].get("groups") or []
    assert set(groups) == EXPECTED_GROUPS[user], (
        f"groups of {user} are {groups!r}, not {sorted(EXPECTED_GROUPS[user])}"
    )


@pytest.mark.parametrize("user", USERS)
def test_user_token_subject_differs_from_every_service_account_subject(
    user_claims, service_account_claims, user
):
    subject = user_claims[user]["sub"]
    service_account_subjects = {claims["sub"] for claims in service_account_claims.values()}
    assert subject not in service_account_subjects, (
        f"sub of {user} equals a service account's sub {subject!r}"
    )


# `exp`, `iat` and `jti` differ between any two tokens; `sub` is the id Keycloak
# generated for the service-account user, which no registry knows beforehand.
PER_TOKEN_CLAIMS = {"exp", "iat", "jti", "sub"}
CLIENT_NAMING_CLAIMS = {"azp", "client_id"}


def test_service_account_tokens_differ_only_by_the_client_naming_claims(service_account_claims):
    songmaker = service_account_claims["songmaker-test"]
    presentator = service_account_claims["presentator-test"]
    ignored = PER_TOKEN_CLAIMS | CLIENT_NAMING_CLAIMS
    differing = {
        claim
        for claim in (songmaker.keys() | presentator.keys()) - ignored
        if songmaker.get(claim) != presentator.get(claim)
    }
    assert not differing, f"service-account tokens also differ in {sorted(differing)}"
    for claim in CLIENT_NAMING_CLAIMS:
        assert songmaker.get(claim) != presentator.get(claim), (
            f"{claim} does not tell the two workload clients apart"
        )
