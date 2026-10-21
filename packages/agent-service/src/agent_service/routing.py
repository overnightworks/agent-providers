"""Which route serves one caller (decision record 0001, H5).

``route`` is the one decider: the CLI route under the subscription profile for
members of a subscription group, else the API route under the installation's alias
for members of an API group who also hold the app's group, else not enabled.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from agent_service.config import IdentityConfig, ProfileConfig, Provider, RouteConfig

APP_GROUP_PREFIX: Final = "/apps/"


@dataclass(frozen=True)
class Cli:
    """The subscription route; ``model`` is the listed name the run must use, if one was asked."""

    profile: str
    model: str | None = None


@dataclass(frozen=True)
class Api:
    """The paid route; ``model`` is the full requested ``<provider>/<model>``, if one was asked."""

    alias: str
    model: str | None = None


@dataclass(frozen=True)
class NotEnabled:
    pass


Route = Cli | Api | NotEnabled
ServedRoute = Cli | Api


def _subscription_route(
    identity: IdentityConfig,
    profiles: Mapping[str, ProfileConfig],
    groups: frozenset[str],
    model: str | None,
    listed_models: Mapping[Provider, frozenset[str]],
) -> Cli | None:
    if identity.subscription_groups.isdisjoint(groups):
        return None
    if model is None:
        return Cli(next(iter(profiles)), None) if profiles else None
    provider, _separator, name = model.partition("/")
    for profile, facts in profiles.items():
        if facts.provider == provider and name in listed_models.get(facts.provider, frozenset()):
            return Cli(profile, name)
    return None


def route(
    identity: IdentityConfig,
    route_facts: RouteConfig | None,
    profiles: Mapping[str, ProfileConfig],
    groups: frozenset[str],
    app: str,
    model: str | None,
    listed_models: Mapping[Provider, frozenset[str]],
) -> Route:
    subscription = _subscription_route(identity, profiles, groups, model, listed_models)
    if subscription is not None:
        return subscription
    if (
        route_facts is not None
        and not identity.api_groups.isdisjoint(groups)
        and f"{APP_GROUP_PREFIX}{app}" in groups
    ):
        return Api(route_facts.api_alias, model)
    return NotEnabled()
