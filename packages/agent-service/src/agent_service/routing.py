"""Which route serves one caller (decision record 0001, H5).

``route`` is the one decider: the CLI route under the subscription profile for
members of a subscription group, else the API route under the installation's alias
for members of an API group who also hold the app's group, else not enabled.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from agent_service.config import IdentityConfig, ProfileConfig, RouteConfig

APP_GROUP_PREFIX: Final = "/apps/"


@dataclass(frozen=True)
class Cli:
    profile: str


@dataclass(frozen=True)
class Api:
    alias: str


@dataclass(frozen=True)
class NotEnabled:
    pass


Route = Cli | Api | NotEnabled
ServedRoute = Cli | Api


def route(
    identity: IdentityConfig,
    route_facts: RouteConfig | None,
    profiles: Mapping[str, ProfileConfig],
    groups: frozenset[str],
    app: str,
) -> Route:
    if profiles and not identity.subscription_groups.isdisjoint(groups):
        return Cli(next(iter(profiles)))
    if (
        route_facts is not None
        and not identity.api_groups.isdisjoint(groups)
        and f"{APP_GROUP_PREFIX}{app}" in groups
    ):
        return Api(route_facts.api_alias)
    return NotEnabled()
