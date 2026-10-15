"""Which route serves a purpose for one caller (decision record 0001, H5).

``route`` is the one decider: the CLI route under the purpose's provider
profile for members of the subscription group, else the API route under the
purpose's alias for members of the app's group, else not enabled.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from agent_service.config import ProfileConfig, PurposeConfig

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
    purpose: PurposeConfig,
    profiles: Mapping[str, ProfileConfig],
    groups: frozenset[str],
    app: str,
    *,
    subscription_group: str,
) -> Route:
    profile = _profile_for(purpose, profiles)
    if profile is not None and subscription_group in groups:
        return Cli(profile)
    if purpose.api_alias is not None and f"{APP_GROUP_PREFIX}{app}" in groups:
        return Api(purpose.api_alias)
    return NotEnabled()


def _profile_for(purpose: PurposeConfig, profiles: Mapping[str, ProfileConfig]) -> str | None:
    if purpose.cli_provider is None:
        return None
    return next(
        (name for name, profile in profiles.items() if profile.provider is purpose.cli_provider),
        None,
    )
