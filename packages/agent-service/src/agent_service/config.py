"""The service's one configuration: identity, listener, profiles, purposes and apps.

A TOML file is read once at startup into frozen models that forbid unknown
fields. A file the models refuse stops the service with an error naming the
field, so a typo never becomes a silently ignored policy.
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent_client.tools import ToolEffect

NonEmptyText = Annotated[str, Field(min_length=1)]
HttpsUrl = Annotated[str, Field(pattern=r"^https://")]


class ConfigError(ValueError):
    """A configuration file the service refuses to start with; the message names the field."""


class Provider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    GROK = "grok"


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IdentityConfig(_Section):
    """Who issues the tokens the service accepts, and which claims carry the groups."""

    issuer: NonEmptyText
    jwks_url: HttpsUrl
    audience: NonEmptyText = "agent-service"
    subscription_group: NonEmptyText = "/ki-abo"
    group_claim: NonEmptyText = "groups"
    leeway_seconds: int = Field(default=0, ge=0, le=300)


class ServerConfig(_Section):
    host: NonEmptyText = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)


class ProfileConfig(_Section):
    """One subscription login the CLI route may run under."""

    provider: Provider


class PurposeConfig(_Section):
    """Which routes may serve one purpose (decision record 0001, H5)."""

    cli_provider: Provider | None = None
    api_alias: NonEmptyText | None = None


class AppConfig(_Section):
    """One app: its workload client, the purposes it may ask for, and its tools' effects.

    A tool the app does not list is treated as side-effecting.
    """

    workload_client: NonEmptyText
    purposes: tuple[NonEmptyText, ...]
    tools: dict[NonEmptyText, ToolEffect] = Field(default_factory=dict)

    def tool_effect(self, tool_name: str) -> ToolEffect:
        return self.tools.get(tool_name, ToolEffect.SIDE_EFFECTING)


class ServiceConfig(_Section):
    identity: IdentityConfig
    server: ServerConfig = ServerConfig()
    profiles: dict[NonEmptyText, ProfileConfig] = Field(default_factory=dict)
    purposes: dict[NonEmptyText, PurposeConfig]
    apps: dict[NonEmptyText, AppConfig]

    @model_validator(mode="after")
    def _one_profile_per_provider(self) -> Self:
        owners: dict[Provider, str] = {}
        for name, profile in self.profiles.items():
            owner = owners.setdefault(profile.provider, name)
            if owner != name:
                raise ValueError(
                    f"profiles.{name}.provider: {profile.provider} already has profile {owner!r}"
                )
        return self

    @model_validator(mode="after")
    def _apps_name_known_purposes(self) -> Self:
        for app_name, app in self.apps.items():
            for purpose in app.purposes:
                if purpose not in self.purposes:
                    raise ValueError(f"apps.{app_name}.purposes: unknown purpose {purpose!r}")
        return self

    @model_validator(mode="after")
    def _workload_clients_are_unique(self) -> Self:
        owners: dict[str, str] = {}
        for app_name, app in self.apps.items():
            owner = owners.setdefault(app.workload_client, app_name)
            if owner != app_name:
                raise ValueError(
                    f"apps.{app_name}.workload_client: {app.workload_client!r} is already"
                    f" the workload client of app {owner!r}"
                )
        return self

    def app_of_workload_client(self, client_id: str) -> str | None:
        return self._apps_by_workload_client.get(client_id)

    @cached_property
    def _apps_by_workload_client(self) -> dict[str, str]:
        return {app.workload_client: app_name for app_name, app in self.apps.items()}


def load_config(path: Path) -> ServiceConfig:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"{path}: cannot be read: {error.strerror}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: not TOML: {error}") from error
    try:
        return ServiceConfig.model_validate(document)
    except ValidationError as error:
        raise ConfigError(f"{path}: {_named_fields(error)}") from error


def _named_fields(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
        if problem["loc"]
        else problem["msg"].removeprefix("Value error, ")
        for problem in error.errors(include_input=False, include_url=False)
    )
