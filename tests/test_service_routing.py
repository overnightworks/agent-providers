"""H5 routing from the caller's groups (decision record 0001).

The subscription groups, the API groups and the app's own group decide the route; the
purpose never does. Test values: subscription groups ``/old-sub`` and ``/new-sub``, API
group ``/api-members``, one Claude profile and the alias ``assistant-api``.
"""

from __future__ import annotations

import pytest

from agent_service.config import IdentityConfig, ProfileConfig, Provider, RouteConfig
from agent_service.routing import Api, Cli, NotEnabled, route

IDENTITY = IdentityConfig(
    issuer="https://keycloak.test/realms/agent-test",
    jwks_url="https://keycloak.test/realms/agent-test/protocol/openid-connect/certs",
    audience="agent-service",
    subscription_groups=frozenset({"/old-sub", "/new-sub"}),
    api_groups=frozenset({"/api-members"}),
    group_claim="groups",
)
PROFILES = {"claude-main": ProfileConfig(provider=Provider.CLAUDE)}
ROUTE = RouteConfig(api_alias="assistant-api")
CLI = Cli("claude-main")
API = Api("assistant-api")
OFF = NotEnabled()
APP = "/apps/songmaker"


def _routed(groups, *, profiles=PROFILES, route_facts=ROUTE, identity=IDENTITY, app="songmaker"):
    return route(identity, route_facts, profiles, frozenset(groups), app)


@pytest.mark.parametrize(
    ("groups", "profiles", "route_facts", "expected"),
    [
        pytest.param({"/new-sub"}, PROFILES, ROUTE, CLI, id="R1-new-subscription-group"),
        pytest.param({"/old-sub"}, PROFILES, ROUTE, CLI, id="R2-stale-token-keeps-its-route"),
        pytest.param({"/old-sub", "/new-sub"}, PROFILES, ROUTE, CLI, id="R3-both-names"),
        pytest.param(
            {"/new-sub", "/api-members", APP}, PROFILES, ROUTE, CLI, id="R4-subscription-wins"
        ),
        pytest.param(
            {"/new-sub", "/api-members", APP}, {}, ROUTE, API, id="R5-no-profile-uses-the-api"
        ),
        pytest.param({"/new-sub"}, {}, None, OFF, id="R6-nothing-configured"),
        pytest.param({"/api-members", APP}, PROFILES, ROUTE, API, id="R7-api-member"),
        pytest.param({"/api-members", APP}, PROFILES, None, OFF, id="R8-no-alias"),
        pytest.param({"/api-members"}, PROFILES, ROUTE, OFF, id="R9-api-group-without-the-app"),
        pytest.param({APP}, PROFILES, ROUTE, OFF, id="R10-app-group-alone"),
        pytest.param(
            {"/api-members", "/apps/presentator"}, PROFILES, ROUTE, OFF, id="R11-another-app"
        ),
        pytest.param({"/new-sub/friends"}, PROFILES, ROUTE, OFF, id="R12-subscription-subgroup"),
        pytest.param({"/new-sub-x"}, PROFILES, ROUTE, OFF, id="R12-subscription-look-alike"),
        pytest.param({"/api-members/friends", APP}, PROFILES, ROUTE, OFF, id="R12a-api-subgroup"),
        pytest.param({"/api-members-x", APP}, PROFILES, ROUTE, OFF, id="R12a-api-look-alike"),
        pytest.param(
            {"/api-members", f"{APP}/friends"}, PROFILES, ROUTE, OFF, id="R12b-app-subgroup"
        ),
        pytest.param({"/api-members", f"{APP}-x"}, PROFILES, ROUTE, OFF, id="R12b-app-look-alike"),
        pytest.param(set(), PROFILES, ROUTE, OFF, id="R13-no-groups"),
    ],
)
def test_the_route_follows_the_groups(groups, profiles, route_facts, expected):
    assert _routed(groups, profiles=profiles, route_facts=route_facts) == expected


def test_a_stale_subscription_name_is_not_enabled_once_the_configuration_drops_it():
    after_the_cut_over = IDENTITY.model_copy(
        update={"subscription_groups": frozenset({"/new-sub"})}
    )

    assert _routed({"/old-sub"}, identity=after_the_cut_over) == OFF
