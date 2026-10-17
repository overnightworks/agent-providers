"""The service's one configuration: identity, listener, profiles, purposes and apps.

The service accepts at most one subscription profile, for Claude, which needs its
token file. An app that lists tools names its one https MCP origin; an origin is
never shared between apps and needs the signing key. At load, every such app's
capability is measured against the 8,192-character limit with a reserved subject
size and timestamp width, so an app whose tools do not fit stops startup instead
of failing its users.

A TOML file is read once at startup into frozen models that forbid unknown
fields. A file the models refuse stops the service with an error naming the
field, so a typo never becomes a silently ignored policy. The signing key file
and each profile's token file the TOML names are read and checked with it, in the same pass.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator, Mapping
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Annotated, Final, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    StrictFloat,
    StrictInt,
    ValidationError,
    model_validator,
)
from webauth.https_client import is_https_url

from agent_client.capability import MAX_CAPABILITY_CHARACTERS, McpOrigin
from agent_client.tools import ToolEffect
from agent_client.wire import MAX_EVENT_BYTES
from agent_service.capability import (
    CapabilityScope,
    CapabilityTooLarge,
    preflight_claims,
    signed_capability,
)
from agent_service.secret_file import read_token_file, shown_text
from agent_service.signing import SigningKey, load_signing_key

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


def _absolute_path_to(file_description: str, value: object) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"must be an absolute path to the {file_description}; ~ is not expanded")
    if not Path(value).is_absolute():
        raise ValueError(
            f"{shown_text(value)} must be an absolute path to the {file_description};"
            " ~ is not expanded"
        )
    return Path(value)


def _signing_key_in_file(key_file: object) -> SigningKey:
    """Loads the key the absolute path names; a refusal stops startup as a field error."""
    return load_signing_key(_absolute_path_to("signing key file", key_file))


def _token_in_file(token_file: object) -> SecretStr:
    """Reads the token the absolute path names; a refusal stops startup as a field error."""
    return SecretStr(read_token_file(_absolute_path_to("token file", token_file)))


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
    max_parallel_cli_runs: StrictInt = Field(default=5, ge=1)

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
    token: Annotated[SecretStr, BeforeValidator(_token_in_file)] | None = Field(
        default=None, validation_alias="token_file"
    )


class PurposeConfig(_Section):
    """Which routes may serve one purpose (decision record 0001, H5)."""

    cli_provider: Provider | None = None
    api_alias: NonEmptyText | None = None


class AppConfig(_Section):
    """One app: its workload client, the purposes it may ask for, and its tools' effects.

    A tool the app does not list is treated as side-effecting. The keys of the tools
    table are the tools a capability permits; an empty table permits none.
    """

    workload_client: NonEmptyText
    purposes: tuple[NonEmptyText, ...]
    tools: dict[NonEmptyText, ToolEffect] = Field(default_factory=dict)
    mcp_origin: McpOrigin | None = None

    def granted_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self.tools))

    def tool_effect(self, tool_name: str) -> ToolEffect:
        return self.tools.get(tool_name, ToolEffect.SIDE_EFFECTING)


def _named_tables(document: object, section: str) -> Iterator[tuple[str, Mapping[str, object]]]:
    """The named tables of a section of the raw document; any other shape is left to the fields."""
    if not isinstance(document, Mapping) or not isinstance(document.get(section), Mapping):
        return
    for name, table in document[section].items():
        if isinstance(name, str) and isinstance(table, Mapping):
            yield name, table


def _raw_profiles(document: object) -> Iterator[tuple[str, Mapping[str, object], Provider | None]]:
    for name, table in _named_tables(document, "profiles"):
        provider = table.get("provider")
        yield name, table, next((known for known in Provider if known == provider), None)


def _refuse_a_second_profile_for_one_provider(document: object) -> None:
    owners: dict[Provider, str] = {}
    for name, _table, provider in _raw_profiles(document):
        if provider is None:
            continue
        owner = owners.setdefault(provider, name)
        if owner != name:
            raise ValueError(
                f"profiles.{shown_text(name)}.provider: {provider} already has profile {owner!r}"
            )


def _refuse_a_profile_for_a_provider_without_a_route(document: object) -> None:
    for name, _table, provider in _raw_profiles(document):
        if provider not in (None, Provider.CLAUDE):
            raise ValueError(
                f"profiles.{shown_text(name)}.provider: {provider} has no"
                " subscription route yet; only claude can have a profile"
            )


def _refuse_a_claude_profile_without_its_token_file(document: object) -> None:
    for name, table, provider in _raw_profiles(document):
        if provider is Provider.CLAUDE and "token_file" not in table:
            raise ValueError(
                f"profiles.{shown_text(name)}.token_file: required for a claude profile"
            )


def _refuse_an_origin_without_signing(document: object) -> None:
    if not isinstance(document, Mapping) or "signing" in document:
        return
    for app_name, app in _named_tables(document, "apps"):
        if "mcp_origin" in app:
            raise ValueError(
                f"apps.{shown_text(app_name)}.mcp_origin: needs a [signing] section,"
                " because the app's runs carry a signed capability"
            )


def _refuse_tools_without_an_origin(document: object) -> None:
    for app_name, app in _named_tables(document, "apps"):
        tools = app.get("tools")
        if isinstance(tools, Mapping) and tools and "mcp_origin" not in app:
            raise ValueError(
                f"apps.{shown_text(app_name)}.mcp_origin: required because the app lists tools"
            )


RULES_ON_THE_RAW_DOCUMENT: Final = (
    _refuse_a_second_profile_for_one_provider,
    _refuse_a_profile_for_a_provider_without_a_route,
    _refuse_a_claude_profile_without_its_token_file,
    _refuse_an_origin_without_signing,
    _refuse_tools_without_an_origin,
)


class ServiceConfig(_Section):
    identity: IdentityConfig
    server: ServerConfig = ServerConfig()
    runs: RunConfig = RunConfig()
    signing: SigningConfig | None = None
    profiles: dict[NonEmptyText, ProfileConfig] = Field(default_factory=dict)
    purposes: dict[NonEmptyText, PurposeConfig]
    apps: dict[NonEmptyText, AppConfig]

    @model_validator(mode="before")
    @classmethod
    def _relationships_that_need_no_secret(cls, document: object) -> object:
        """Decided on the raw document, so no token or key is loaded when a refusal is raised."""
        for refuse_if_broken in RULES_ON_THE_RAW_DOCUMENT:
            refuse_if_broken(document)
        return document

    @model_validator(mode="after")
    def _apps_name_known_purposes(self) -> Self:
        for app_name, app in self.apps.items():
            for purpose in app.purposes:
                if purpose not in self.purposes:
                    raise ValueError(
                        f"apps.{shown_text(app_name)}.purposes: unknown purpose {purpose!r}"
                    )
        return self

    @model_validator(mode="after")
    def _workload_clients_are_unique(self) -> Self:
        owners: dict[str, str] = {}
        for app_name, app in self.apps.items():
            owner = owners.setdefault(app.workload_client, app_name)
            if owner != app_name:
                raise ValueError(
                    f"apps.{shown_text(app_name)}.workload_client:"
                    f" {app.workload_client!r} is already"
                    f" the workload client of app {owner!r}"
                )
        return self

    @model_validator(mode="after")
    def _origins_are_unique(self) -> Self:
        owners: dict[str, str] = {}
        for app_name, app in self.apps.items():
            if app.mcp_origin is None:
                continue
            owner = owners.setdefault(app.mcp_origin, app_name)
            if owner != app_name:
                raise ValueError(
                    f"apps.{shown_text(app_name)}.mcp_origin: {app.mcp_origin} is already"
                    f" the MCP origin of app {owner!r}"
                )
        return self

    @model_validator(mode="after")
    def _every_capability_fits(self) -> Self:
        for app_name, app in self.apps.items():
            if (scope := self.capability_scope(app_name)) is None:
                continue
            for purpose in app.purposes:
                claims = preflight_claims(
                    scope,
                    app=app_name,
                    purpose=purpose,
                    issuer=self.identity.issuer,
                    max_duration_s=self.runs.max_duration_s,
                )
                try:
                    signed_capability(scope, claims)
                except CapabilityTooLarge as too_large:
                    raise ValueError(
                        f"apps.{shown_text(app_name)}.tools: the capability for purpose"
                        f" {purpose!r} would be {too_large.length} characters;"
                        f" the limit is {MAX_CAPABILITY_CHARACTERS}"
                    ) from None
        return self

    def capability_scope(self, app_name: str) -> CapabilityScope | None:
        app = self.apps[app_name]
        if app.mcp_origin is None or self.signing is None:
            return None
        return CapabilityScope(app.mcp_origin, app.granted_tools(), self.signing.key)

    def app_of_workload_client(self, client_id: str) -> str | None:
        return self._apps_by_workload_client.get(client_id)

    @cached_property
    def _apps_by_workload_client(self) -> dict[str, str]:
        return {app.workload_client: app_name for app_name, app in self.apps.items()}


def load_config(path: Path) -> ServiceConfig:
    try:
        text = path.read_text(encoding="utf-8")  # NOSONAR(S8707) the operator names this file
    except OSError as error:
        raise ConfigError(f"{shown_text(str(path))}: cannot be read: {error.strerror}") from error
    except UnicodeDecodeError as error:
        raise ConfigError(f"{shown_text(str(path))}: not UTF-8 TOML") from error
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{shown_text(str(path))}: not TOML: {shown_text(str(error))}") from error
    try:
        return ServiceConfig.model_validate(document)
    except ValidationError as error:
        raise ConfigError(f"{shown_text(str(path))}: {_named_fields(error)}") from error


def _named_fields(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(shown_text(str(part)) for part in problem['loc'])}: {_message(problem['msg'])}"
        if problem["loc"]
        else _message(problem["msg"])
        for problem in error.errors(include_input=False, include_url=False)
    )


def _message(pydantic_message: str) -> str:
    return pydantic_message.removeprefix("Value error, ")
