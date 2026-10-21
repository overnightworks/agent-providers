"""H5 routing from the caller's groups (decision record 0001).

The subscription groups and the API groups decide the route; the purpose never does, and
membership of the app is the caller check's gate, before any route is chosen. Test values:
subscription groups ``/old-sub`` and ``/new-sub``, API group ``/api-members``, one Claude
profile and the alias ``assistant-api``.
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


SONNET_LISTED = {Provider.CLAUDE: frozenset({"sonnet"})}
NOTHING_LISTED = {Provider.CLAUDE: frozenset()}


def _routed(
    groups,
    *,
    profiles=PROFILES,
    route_facts=ROUTE,
    identity=IDENTITY,
    model=None,
    listed=SONNET_LISTED,
):
    return route(identity, route_facts, profiles, frozenset(groups), model, listed)


@pytest.mark.parametrize(
    ("groups", "profiles", "route_facts", "expected"),
    [
        pytest.param({"/new-sub"}, PROFILES, ROUTE, CLI, id="R1-new-subscription-group"),
        pytest.param({"/old-sub"}, PROFILES, ROUTE, CLI, id="R2-stale-token-keeps-its-route"),
        pytest.param({"/old-sub", "/new-sub"}, PROFILES, ROUTE, CLI, id="R3-both-names"),
        pytest.param({"/new-sub", "/api-members"}, PROFILES, ROUTE, CLI, id="R4-subscription-wins"),
        pytest.param({"/new-sub", "/api-members"}, {}, ROUTE, API, id="R5-no-profile-uses-the-api"),
        pytest.param({"/new-sub"}, {}, None, OFF, id="R6-nothing-configured"),
        pytest.param({"/api-members"}, PROFILES, ROUTE, API, id="R7-api-member"),
        pytest.param({"/api-members"}, PROFILES, None, OFF, id="R8-no-alias"),
        pytest.param({"/new-sub/friends"}, PROFILES, ROUTE, OFF, id="R12-subscription-subgroup"),
        pytest.param({"/new-sub-x"}, PROFILES, ROUTE, OFF, id="R12-subscription-look-alike"),
        pytest.param({"/api-members/friends"}, PROFILES, ROUTE, OFF, id="R12a-api-subgroup"),
        pytest.param({"/api-members-x"}, PROFILES, ROUTE, OFF, id="R12a-api-look-alike"),
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


SUBSCRIBER = {"/new-sub"}
API_ONLY = {"/api-members"}
BOTH = {"/new-sub", "/api-members"}


@pytest.mark.parametrize(
    ("groups", "model", "listed", "expected"),
    [
        pytest.param(
            SUBSCRIBER, "claude/sonnet", SONNET_LISTED, Cli("claude-main", "sonnet"), id="R-a-S"
        ),
        pytest.param(
            API_ONLY,
            "claude/sonnet",
            SONNET_LISTED,
            Api("assistant-api", "claude/sonnet"),
            id="R-a-A",
        ),
        pytest.param(
            BOTH, "claude/sonnet", SONNET_LISTED, Cli("claude-main", "sonnet"), id="R-a-D"
        ),
        pytest.param(SUBSCRIBER, "claude/opus", SONNET_LISTED, OFF, id="R-b-S"),
        pytest.param(
            API_ONLY, "claude/opus", SONNET_LISTED, Api("assistant-api", "claude/opus"), id="R-b-A"
        ),
        pytest.param(
            BOTH, "claude/opus", SONNET_LISTED, Api("assistant-api", "claude/opus"), id="R-b-D"
        ),
        pytest.param(SUBSCRIBER, "claude/sonnet", NOTHING_LISTED, OFF, id="R-c-S"),
        pytest.param(
            BOTH, "claude/sonnet", NOTHING_LISTED, Api("assistant-api", "claude/sonnet"), id="R-c-D"
        ),
        pytest.param(SUBSCRIBER, "xai/sonnet", SONNET_LISTED, OFF, id="R-d-S"),
        pytest.param(
            BOTH, "xai/sonnet", SONNET_LISTED, Api("assistant-api", "xai/sonnet"), id="R-d-D"
        ),
        pytest.param(set(), "claude/sonnet", SONNET_LISTED, OFF, id="R-e-N"),
    ],
)
def test_a_named_model_runs_on_the_subscription_only_when_it_is_listed(
    groups, model, listed, expected
):
    assert _routed(groups, model=model, listed=listed) == expected
