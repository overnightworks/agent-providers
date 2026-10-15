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
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from agent_client.tools import ToolEffect

NonEmptyText = Annotated[str, Field(min_length=1)]


def _https_url_naming_a_host(url: str) -> str:
    """The key-set URL webauth's verifier accepts, so a bad one stops startup as a field error."""
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError as error:
        raise ValueError("must be a URL with a valid port, if any") from error
    if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
        raise ValueError("must be an https URL naming a host and no credentials")
    return url


HttpsUrl = Annotated[str, AfterValidator(_https_url_naming_a_host)]


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
        text = path.read_text(encoding="utf-8")  # NOSONAR The operator names this file.
    except OSError as error:
        raise ConfigError(f"{path}: cannot be read: {error.strerror}") from error
    except UnicodeDecodeError as error:
        raise ConfigError(f"{path}: not UTF-8 TOML") from error
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: not TOML: {error}") from error
    try:
        return ServiceConfig.model_validate(document)
    except ValidationError as error:
        raise ConfigError(f"{path}: {_named_fields(error)}") from error


def _named_fields(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in problem['loc'])}: {_message(problem['msg'])}"
        if problem["loc"]
        else _message(problem["msg"])
        for problem in error.errors(include_input=False, include_url=False)
    )


def _message(pydantic_message: str) -> str:
    return pydantic_message.removeprefix("Value error, ")
