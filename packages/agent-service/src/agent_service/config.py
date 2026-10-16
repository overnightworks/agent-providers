"""The service's one configuration: identity, listener, profiles, purposes and apps.

A TOML file is read once at startup into frozen models that forbid unknown
fields. A file the models refuse stops the service with an error naming the
field, so a typo never becomes a silently ignored policy. The signing key file
the TOML names is read and checked with it, in the same pass.
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
    model_validator,
)
from webauth.https_client import is_https_url

from agent_client.tools import ToolEffect
from agent_client.wire import MAX_EVENT_BYTES
from agent_service.signing import SigningKey, load_signing_key, shown_path

NonEmptyText = Annotated[str, Field(min_length=1)]


def _https_url_the_verifier_accepts(url: str) -> str:
    """Stops startup as a field error on the key-set URL webauth's verifier would refuse."""
    if not is_https_url(url):
        raise ValueError(
            "must be an https URL naming a host, a valid port if any,"
            " and no credentials, spaces or control characters"
        )
    return url


HttpsUrl = Annotated[str, AfterValidator(_https_url_the_verifier_accepts)]


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


def _signing_key_in_file(key_file: object) -> SigningKey:
    """Loads the key the absolute path names; a refusal stops startup as a field error."""
    if not isinstance(key_file, str):
        raise ValueError("must be an absolute path to the signing key file; ~ is not expanded")
    shown = shown_path(key_file)
    if shown != key_file:
        raise ValueError(f"{shown} contains a control or non-printing character; a path must not")
    if not Path(key_file).is_absolute():
        raise ValueError(
            f"{shown} must be an absolute path to the signing key file; ~ is not expanded"
        )
    return load_signing_key(Path(key_file))


class SigningConfig(_Section):
    """The one key the service signs with, read once at startup (decision record 0001, A1a4)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    key: Annotated[SigningKey, BeforeValidator(_signing_key_in_file)] = Field(
        validation_alias="key_file"
    )


class ServerConfig(_Section):
    host: NonEmptyText = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)


class RunConfig(_Section):
    """How a run's pace, length and volume are limited (decision record 0001, H6).

    Strict numbers: TOML ``true`` is not a heartbeat of 1 second and ``15.5``
    is not a heartbeat of 15. The heartbeat is announced on the wire, whose
    bound is 1..300.
    """

    heartbeat_interval_s: StrictInt = Field(default=15, ge=1, le=300)
    stall_timeout_s: StrictFloat = Field(default=30.0, gt=0, allow_inf_nan=False)
    idle_timeout_s: StrictFloat = Field(default=120.0, gt=0, allow_inf_nan=False)
    max_duration_s: StrictFloat = Field(default=600.0, gt=0, allow_inf_nan=False)
    max_tool_calls: StrictInt = Field(default=30, ge=1)
    max_bytes: StrictInt = Field(default=4 * MAX_EVENT_BYTES, ge=1)

    @model_validator(mode="after")
    def _heartbeat_before_stall_before_idle_before_maximum(self) -> Self:
        if not (
            self.heartbeat_interval_s
            < self.stall_timeout_s
            <= self.idle_timeout_s
            < self.max_duration_s
        ):
            raise ValueError(
                "must satisfy heartbeat_interval_s < stall_timeout_s <= idle_timeout_s"
                " < max_duration_s (got"
                f" {self.heartbeat_interval_s}, {self.stall_timeout_s},"
                f" {self.idle_timeout_s}, {self.max_duration_s})"
            )
        return self


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
    runs: RunConfig = RunConfig()
    signing: SigningConfig | None = None
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
