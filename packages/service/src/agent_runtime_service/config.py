"""The service's one configuration: identity, listener, profiles, the route and apps.

The service accepts at most one subscription profile, for Claude, which needs its
token file and a ``[cli.claude]`` table naming the absolute program, the exact version it
must report and the models it serves, beside the ``[cli]`` section's private
``working_root``, checked at load. An
app that offers tools names its one https MCP origin, and the service signs a capability for
each run that grants the tools its request names; an origin is never shared between apps and
needs the signing key. The ``[mcp]`` section names the one CA bundle (``ca_file``, an absolute
path) the service trusts for those origins; it is required as soon as any app declares an
origin, and otherwise optional and unused.

A TOML file is read once at startup into frozen models that forbid unknown
fields. A file the models refuse stops the service with an error naming the
field, so a typo never becomes a silently ignored policy. The signing key file
and each profile's token file the TOML names are read and checked with it, in the same pass;
the configuration keeps the key, but only a token file's path, since the runner reads the
token again at the start of every run.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Iterator, Mapping
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
    PlainValidator,
    PrivateAttr,
    StrictFloat,
    StrictInt,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from webauth.https_client import is_https_url
from webauth.oidc_login import AdmissionConfig

from agent_runtime_client.capability import McpOrigin
from agent_runtime_client.wire import MAX_EVENT_BYTES, MODEL_PART_PATTERN, ModelName, Purpose
from agent_runtime.config import PrivateWorkingRoot, WorkingRootNotPrivateError
from agent_runtime.secret_file import read_token_file, shown_text
from agent_runtime_service.capability import CapabilityScope
from agent_runtime_service.signing import SigningKey, load_signing_key

NonEmptyText = Annotated[str, Field(min_length=1, strict=True)]


def _https_url_the_verifier_accepts(url: str) -> str:
    """Stops startup as a field error on the key-set URL webauth's verifier would refuse."""
    if not is_https_url(url):
        raise ValueError(
            "must be an https URL naming a host, a valid port if any,"
            " and no credentials, spaces or control characters"
        )
    return url


HttpsUrl = Annotated[str, AfterValidator(_https_url_the_verifier_accepts)]


APP_GROUP_PREFIX: Final = "/apps/"


class ConfigError(ValueError):
    """A configuration file the service refuses to start with; the message names the field."""


class Provider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    GROK = "grok"


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _group_path(name: str) -> str:
    if not name.startswith("/"):
        raise ValueError("must begin with /")
    return name


GroupName = Annotated[NonEmptyText, AfterValidator(_group_path)]


class IdentityConfig(_Section):
    """Who issues the tokens the service accepts, and which claims carry the groups."""

    issuer: NonEmptyText
    jwks_url: HttpsUrl
    audience: NonEmptyText
    subscription_groups: frozenset[GroupName] = Field(min_length=1)
    api_groups: frozenset[GroupName] = frozenset()
    group_claim: NonEmptyText
    leeway_seconds: int = Field(default=0, ge=0, le=300)


def _not_a_path_to(file_description: str) -> ValueError:
    return ValueError(f"must be an absolute path to the {file_description}; ~ is not expanded")


def _absolute_path_to(file_description: str, value: object) -> Path:
    if not isinstance(value, str):
        raise _not_a_path_to(file_description)
    if not Path(value).is_absolute():
        raise ValueError(
            f"{shown_text(value)} must be an absolute path to the {file_description};"
            " ~ is not expanded"
        )
    return Path(value)


def _signing_key_in_file(key_file: object) -> SigningKey:
    """Loads the key the absolute path names; a refusal stops startup as a field error."""
    return load_signing_key(_absolute_path_to("signing key file", key_file))


def _sound_token_file(token_file: object) -> Path:
    """The absolute path, once its token reads soundly; a refusal stops startup as a field error.

    The token read here is dropped: a swapped file takes effect on the next run without a
    restart (decision record 0001, line 2), so only the runner's read at each run's start counts.
    """
    path = _absolute_path_to("token file", token_file)
    read_token_file(path)
    return path


class SigningConfig(_Section):
    """The one key the service signs with, read once at startup (decision record 0001, A1a4)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    key: Annotated[SigningKey, BeforeValidator(_signing_key_in_file)] = Field(
        validation_alias="key_file"
    )


VERSION_PATTERN: Final = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", re.ASCII)
MALFORMED_PIN: Final = (
    "must be three dot-separated integers without leading zeros, written as a string,"
    " such as 2.1.302"
)


def _pin_in_grammar(value: object) -> str:
    if not isinstance(value, str) or VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError(MALFORMED_PIN)
    return value


PinnedVersion = Annotated[str, BeforeValidator(_pin_in_grammar)]


def _program(path: object) -> Path:
    return _absolute_path_to("program", path)


def _model_part_in_grammar(name: str) -> str:
    if MODEL_PART_PATTERN.fullmatch(name) is None:
        raise ValueError("must be a model name from a-z, 0-9, . and -, with no provider or version")
    return name


ListedModel = Annotated[str, Field(strict=True), AfterValidator(_model_part_in_grammar)]


class ProgramPin(_Section):
    """One provider's program, the exact version it must report and the models it serves.

    ``models`` lists what this installation runs on the subscription; an empty list
    lists nothing, so a named model then goes to the API route.
    """

    binary: Annotated[Path, BeforeValidator(_program)]
    version: PinnedVersion
    models: frozenset[ListedModel]


def _private_working_root(value: object) -> PrivateWorkingRoot:
    """Constructs the root the string names; only the closed violation reaches the message."""
    if not isinstance(value, str):
        raise _not_a_path_to("working root")
    try:
        return PrivateWorkingRoot(Path(value))
    except WorkingRootNotPrivateError as error:
        raise ValueError(f"not private: {error.violation.value}") from None


class CliConfig(_Section):
    """The private root every program run lives in, and each provider's pinned program."""

    working_root: Annotated[PrivateWorkingRoot, PlainValidator(_private_working_root)] = Field(
        exclude=True, repr=False
    )
    claude: ProgramPin | None = None

    def programs(self) -> Mapping[Provider, ProgramPin]:
        """The pinned programs this installation sets, in ``Provider`` order."""
        pinned = {Provider.CLAUDE: self.claude}
        return {provider: pin for provider, pin in pinned.items() if pin is not None}


def _ca_bundle(path: object) -> Path:
    return _absolute_path_to("CA bundle", path)


class McpConfig(_Section):
    """The one CA bundle that signs the certificates of the apps' MCP origins."""

    ca_file: Annotated[Path, BeforeValidator(_ca_bundle)] | None = None


class ServerConfig(_Section):
    host: NonEmptyText = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)


MAX_TOOL_TIMEOUT_MS: Final = 2_147_483_647


class RunConfig(_Section):
    """How a run's pace, length and volume are limited (decision record 0001, H6).

    Strict numbers: TOML ``true`` is not a heartbeat of 1 second and ``15.5``
    is not a heartbeat of 15. The heartbeat is announced on the wire, whose
    bound is 1..300.
    """

    heartbeat_interval_s: StrictInt = Field(default=15, ge=1, le=300)
    stall_timeout_s: StrictFloat = Field(default=30.0, gt=0, allow_inf_nan=False)
    idle_timeout_s: StrictFloat = Field(default=120.0, gt=0, allow_inf_nan=False)
    max_duration_s: StrictFloat = Field(
        default=600.0, gt=0, le=MAX_TOOL_TIMEOUT_MS // 1000, allow_inf_nan=False
    )
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


def _text_only(value: object) -> object:
    """Refuses bytes and other lax forms of a name the relationship rules read as text."""
    if not isinstance(value, str):
        raise ValueError("must be text")
    return value


class ProfileConfig(_Section):
    """One subscription login the CLI route may run under."""

    provider: Annotated[Provider, BeforeValidator(_text_only)]
    token_file: Annotated[Path, BeforeValidator(_sound_token_file)] | None = None


class RouteConfig(_Section):
    """The paid route's alias (decision record 0001, H5)."""

    api_alias: NonEmptyText


class AppConfig(_Section):
    """One app: its workload client, its MCP origin and its per-agent model overrides.

    The app's tools and their effects arrive with each run request. ``models`` maps an
    agent's name (the ``purpose`` of its runs) to the model that agent runs on instead of
    the one it asks for.
    """

    workload_client: NonEmptyText
    mcp_origin: McpOrigin | None = None
    models: dict[Purpose, ModelName] = Field(default_factory=dict)


def _named_tables(
    document: object, section: str, built_model: type[_Section]
) -> Iterator[tuple[str, object]]:
    """The named tables of a section, raw or already built; other shapes are left to the fields."""
    if not isinstance(document, Mapping) or not isinstance(document.get(section), Mapping):
        return
    for name, table in document[section].items():
        if isinstance(name, str) and isinstance(table, Mapping | built_model):
            yield name, table


def _value(table: object, key: str) -> object:
    """What pydantic will see under ``key``: ``None`` is absent, in a mapping or a built model."""
    if isinstance(table, Mapping):
        return table.get(key)
    return getattr(table, key)


def _raw_profiles(document: object) -> Iterator[tuple[str, object, Provider | None]]:
    for name, table in _named_tables(document, "profiles", ProfileConfig):
        provider = _value(table, "provider")
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
        if provider is Provider.CLAUDE and _value(table, "token_file") is None:
            raise ValueError(
                f"profiles.{shown_text(name)}.token_file: required for a claude profile"
            )


def _refuse_a_claude_profile_without_its_program_table(document: object) -> None:
    cli = _value(document, "cli")
    if isinstance(cli, Mapping | CliConfig) and _value(cli, "claude") is not None:
        return
    if any(provider is Provider.CLAUDE for _name, _table, provider in _raw_profiles(document)):
        raise ValueError("cli.claude: required for a claude profile")


def _refuse_an_origin_without_signing(document: object) -> None:
    if not isinstance(document, Mapping) or document.get("signing") is not None:
        return
    for app_name, app in _named_tables(document, "apps", AppConfig):
        if _value(app, "mcp_origin") is not None:
            raise ValueError(
                f"apps.{shown_text(app_name)}.mcp_origin: needs a [signing] section,"
                " because the app's runs carry a signed capability"
            )


def _declares_an_origin(app: object) -> bool:
    return _value(app, "mcp_origin") is not None


def _refuse_an_origin_without_a_ca_bundle(document: object) -> None:
    mcp = _value(document, "mcp")
    if isinstance(mcp, Mapping | McpConfig) and _value(mcp, "ca_file") is not None:
        return
    for app_name, app in _named_tables(document, "apps", AppConfig):
        if _declares_an_origin(app):
            raise ValueError(
                f"mcp.ca_file: required because apps.{shown_text(app_name)} declares an mcp_origin"
            )


_AGENT_NAME: Final = TypeAdapter(Purpose)
_MODEL_NAME: Final = TypeAdapter(ModelName)


def _refuse_a_malformed_model_override(document: object) -> None:
    for app_name, app in _named_tables(document, "apps", AppConfig):
        overrides = _value(app, "models")
        where = f"apps.{shown_text(app_name)}.models"
        if overrides is None:
            continue
        if not isinstance(overrides, Mapping):
            raise ValueError(f'{where}: must be a table of <agent> = "<provider>/<model>"')
        for agent, model in overrides.items():
            for decider, text, what in (
                (_AGENT_NAME, agent, "agent name"),
                (_MODEL_NAME, model, "model"),
            ):
                try:
                    decider.validate_python(text, strict=True)
                except ValidationError as error:
                    problem = _message(error.errors()[0]["msg"])
                    raise ValueError(
                        f"{where}.{shown_text(str(agent))}: {what} {problem}"
                    ) from error


RULES_ON_THE_RAW_DOCUMENT: Final = (
    _refuse_a_second_profile_for_one_provider,
    _refuse_a_profile_for_a_provider_without_a_route,
    _refuse_a_claude_profile_without_its_token_file,
    _refuse_a_claude_profile_without_its_program_table,
    _refuse_an_origin_without_signing,
    _refuse_an_origin_without_a_ca_bundle,
    _refuse_a_malformed_model_override,
)


class ServiceConfig(_Section):
    identity: IdentityConfig
    server: ServerConfig = ServerConfig()
    runs: RunConfig = RunConfig()
    cli: CliConfig | None = None
    mcp: McpConfig | None = None
    signing: SigningConfig | None = None
    profiles: dict[NonEmptyText, ProfileConfig] = Field(default_factory=dict)
    route: RouteConfig | None = None
    apps: dict[NonEmptyText, AppConfig]
    _admissions: dict[str, AdmissionConfig] = PrivateAttr(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _relationships_that_need_no_secret(cls, document: object) -> object:
        """Decided on the raw document, so no token or key is loaded when a refusal is raised."""
        for refuse_if_broken in RULES_ON_THE_RAW_DOCUMENT:
            refuse_if_broken(document)
        return document

    @model_validator(mode="after")
    def _api_groups_and_alias_come_together(self) -> Self:
        if self.identity.api_groups and self.route is None:
            raise ValueError("route.api_alias: required because identity.api_groups is set")
        if self.route is not None and not self.identity.api_groups:
            raise ValueError("identity.api_groups: required because route.api_alias is set")
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
    def _app_names_are_group_segments(self) -> Self:
        for app_name in self.apps:
            try:
                self._admissions[app_name] = self._admission_of(app_name)
            except ValueError as error:
                raise ValueError(
                    f"apps.{shown_text(app_name)}: not one group path segment: "
                    f"{shown_text(str(error))}"
                ) from error
        return self

    def capability_scope(self, app_name: str, tools: Iterable[str]) -> CapabilityScope | None:
        """What the app's capability is bound to, granting exactly ``tools`` sorted."""
        app = self.apps[app_name]
        if app.mcp_origin is None or self.signing is None:
            return None
        return CapabilityScope(app.mcp_origin, tuple(sorted(tools)), self.signing.key)

    def subscription_models(self) -> Mapping[Provider, frozenset[str]]:
        """The model names each provider's subscription program serves; empty without ``[cli]``."""
        if self.cli is None:
            return {}
        return {provider: pin.models for provider, pin in self.cli.programs().items()}

    def model_for(self, app_name: str, purpose: str, requested: str | None) -> str | None:
        """The model the route is asked about: the agent's override, else the request's."""
        return self.apps[app_name].models.get(purpose, requested)

    def mcp_trust_bundle(self) -> Path | None:
        """The CA bundle for the apps' MCP origins; ``None`` while no app declares an origin."""
        if self.mcp is None or not any(map(_declares_an_origin, self.apps.values())):
            return None
        return self.mcp.ca_file

    def app_admission(self, app_name: str) -> AdmissionConfig:
        """Who may use the app: its group ``/apps/<app>``, segment-aware, and its admins below."""
        return self._admissions[app_name]

    def _admission_of(self, app_name: str) -> AdmissionConfig:
        if "/" in app_name:
            raise ValueError("an app name is exactly one path segment")
        group = f"{APP_GROUP_PREFIX}{app_name}"
        return AdmissionConfig(
            bound_issuer=self.identity.issuer,
            login_group=group,
            admin_group=f"{group}/admin",
        )

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
        refusal = f"{shown_text(str(path))}: {_named_fields(error)}"
    raise ConfigError(refusal)


def _named_fields(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(shown_text(str(part)) for part in problem['loc'])}: {_message(problem['msg'])}"
        if problem["loc"]
        else _message(problem["msg"])
        for problem in error.errors(include_input=False, include_url=False)
    )


def _message(pydantic_message: str) -> str:
    return pydantic_message.removeprefix("Value error, ")
