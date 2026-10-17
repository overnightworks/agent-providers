"""What each provider offers on each route, and what its setup still needs.

Two questions live here. *Which models can this provider serve on this
route right now* — answered by probing the mounted CLI or the provider's
model endpoint. And *is this provider set up at all* — answered as one of
four closed cases a host can render without re-deriving them.

A host names its own surfaces (a chat, a batch job) and states per
surface which CLI setup methods that surface can actually use; this package
knows the providers, not the host's surfaces.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from importlib.util import find_spec
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from agent_providers.claude.provider import (
    CLAUDE_CLI_MODEL_CATALOG_ERROR,
    CliLoginNotConfiguredError,
    CliUnavailableError,
    cli_login_status,
    cli_model_catalog,
)
from agent_providers.claude.provider import (
    UnavailableError as ClaudeCliUnavailableError,
)
from agent_providers.config import ProviderRuntimeConfig, current_config
from agent_providers.constants import (
    ANTHROPIC_API_VERSION,
    ANTHROPIC_MODELS_URL,
    CLAUDE_MODEL_PREFIX,
    GROK_MODEL_PREFIX,
    GROK_MODELS_URL,
    GROK_NON_CHAT_MARKERS,
    OPENAI_CHAT_PREFIXES,
    OPENAI_MODELS_URL,
    OPENAI_NON_CHAT_MARKERS,
    PROBE_TIMEOUT_SECONDS,
    ProviderName,
)
from agent_providers.errors import (
    ProviderModelCatalogUnavailableError,
    ProviderUnavailableError,
    SafeRouteReason,
    SafeRouteReasonCode,
    normalize_route_failure,
)
from agent_providers.process import (
    CODEX_CLI_BINARY_MISSING_DETAIL,
    GROK_CLI_BINARY_MISSING_DETAIL,
    AgentCliUnavailableError,
    CliEnvironmentCause,
    CliEnvironmentUnavailableError,
    CliLoginUnusableError,
    codex_cli_login,
    codex_cli_login_is_usable,
    codex_cli_model_catalog,
    grok_cli_status,
    grok_cli_token_is_present,
    require_cli_binary,
)

_ANTHROPIC_SDK_DISTRIBUTION: Final = "anthropic"
ANTHROPIC_API_KEY_ENVIRONMENT: Final = "ANTHROPIC_API_KEY"
XAI_API_KEY_ENVIRONMENT: Final = "XAI_API_KEY"
OPENAI_API_KEY_ENVIRONMENT: Final = "OPENAI_API_KEY"
_API_KEY_SETUP_LABEL: Final = "API key"
_CLI_LOGIN_SETUP_LABEL: Final = "CLI login"
_CODEX_CLI_HIDDEN_VISIBILITY: Final = "hide"

log = logging.getLogger(__name__)


class ProviderSetupMethod(StrEnum):
    API_KEY = "api_key"
    CLAUDE_CLI = "claude_cli"
    GROK_CLI = "grok_cli"
    CODEX_CLI = "codex_cli"


class _CodexCliCatalogLine(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    slug: str = Field(min_length=1)
    visibility: str = Field(min_length=1)
    priority: int


class ProviderRoute(StrEnum):
    CLI = "cli"
    API = "api"


class ProviderRouteReadinessState(StrEnum):
    READY = "ready"
    NOT_CONFIGURED = "not_configured"
    DISTURBED = "disturbed"
    UNVERIFIED = "unverified"


class ProviderRouteCapability(StrEnum):
    TOOLS_AVAILABLE = "tools_available"
    TEXT_ONLY = "text_only"


@dataclass(frozen=True)
class ProviderReady:
    """This provider is set up for the surface that asked, and how."""

    provider: str
    method: ProviderSetupMethod
    environment_key: str | None = None


@dataclass(frozen=True)
class ProviderNeedsKey:
    """A CLI is signed in, but this surface can only reach the HTTP API."""

    provider: str
    method: ProviderSetupMethod
    missing_environment_key: str


@dataclass(frozen=True)
class ProviderNotLoggedIn:
    """Neither an API key nor a signed-in CLI answers for this provider."""

    provider: str
    missing_environment_key: str


@dataclass(frozen=True)
class ProviderCliUnavailable:
    """No API key is set, and this provider's CLI cannot run here."""

    provider: str
    cause: CliEnvironmentCause
    missing_environment_key: str


@dataclass(frozen=True)
class ProviderCapabilityMissing:
    """A key is set, but the distribution its route needs is not installed."""

    provider: str
    dependency: str


type ProviderConfiguration = (
    ProviderReady
    | ProviderNeedsKey
    | ProviderNotLoggedIn
    | ProviderCliUnavailable
    | ProviderCapabilityMissing
)


@dataclass(frozen=True)
class ProviderRouteSnapshot:
    """What one probe of a provider route found.

    ``models`` is ``list_provider_models``' answer for the route when
    ``readiness`` is ``READY``, and empty otherwise. It is neither fixed nor
    complete: a name missing from it is no proof that the route refuses it.
    ``catalogue_failure`` names why a probed catalogue failed.
    ``default_model_label`` is the Claude CLI's own name for what a turn with no
    model runs. It is for display only, never a model to pass, and ``None`` for
    every other route and for a route that is not ready.
    """

    models: tuple[str, ...]
    catalogue_failure: SafeRouteReason | None
    catalog_source: str | None
    catalog_version: str | None
    readiness: ProviderRouteReadinessState
    capability: ProviderRouteCapability
    reason: SafeRouteReason | None
    probed_at: datetime
    setup_label: str
    default_model_label: str | None


@dataclass(frozen=True)
class _RouteOffer:
    models: tuple[str, ...]
    default_model_label: str | None


@dataclass(frozen=True)
class _ProviderApiCredential:
    secret: SecretStr | None
    environment_key: str


def probe_provider_route(provider: str, route: ProviderRoute) -> ProviderRouteSnapshot:
    """Probe one route's credentials and model catalog, right now."""
    config = current_config()
    now = datetime.now(timezone.utc)
    capability = provider_route_capability()
    credential = _provider_api_credential(provider, config)
    preflight = _provider_route_preflight(provider, route, credential, capability, now)
    if preflight is not None:
        return preflight
    try:
        offer = _route_offer(provider, route)
    except ProviderModelCatalogUnavailableError as exc:
        reason = exc.reason or normalize_route_failure(
            SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR,
        )
        return ProviderRouteSnapshot(
            (), reason, None, None, _readiness_of_fault(reason.code),
            capability, reason, now,
            _CLI_LOGIN_SETUP_LABEL if route is ProviderRoute.CLI else _API_KEY_SETUP_LABEL,
            None,
        )
    except ProviderUnavailableError as exc:
        reason = exc.reason or normalize_route_failure(
            SafeRouteReasonCode.ROUTE_FAILED,
        )
        return ProviderRouteSnapshot(
            (), reason, None, None, _readiness_of_fault(reason.code),
            capability, reason, now,
            _CLI_LOGIN_SETUP_LABEL if route is ProviderRoute.CLI else _API_KEY_SETUP_LABEL,
            None,
        )
    if route is ProviderRoute.API:
        source = "provider API"
    else:
        source = "provider CLI"
    return ProviderRouteSnapshot(
        tuple(offer.models), None, source, None, ProviderRouteReadinessState.READY,
        capability, None, now,
        _CLI_LOGIN_SETUP_LABEL if route is ProviderRoute.CLI else _API_KEY_SETUP_LABEL,
        offer.default_model_label,
    )


def _provider_route_preflight(
    provider: str,
    route: ProviderRoute,
    credential: _ProviderApiCredential,
    capability: ProviderRouteCapability,
    now: datetime,
) -> ProviderRouteSnapshot | None:
    if route is ProviderRoute.API:
        return _api_route_preflight(provider, credential, capability, now)
    return _cli_route_preflight(provider, capability, now)


def _api_route_preflight(
    provider: str,
    credential: _ProviderApiCredential,
    capability: ProviderRouteCapability,
    now: datetime,
) -> ProviderRouteSnapshot | None:
    if not _secret(credential.secret):
        return _route_preflight_snapshot(
            capability, now, ProviderRouteReadinessState.NOT_CONFIGURED,
            SafeRouteReasonCode.API_KEY_NOT_SET, _API_KEY_SETUP_LABEL,
        )
    if provider == ProviderName.CLAUDE and not _anthropic_sdk_available():
        return _route_preflight_snapshot(
            capability, now, ProviderRouteReadinessState.DISTURBED,
            SafeRouteReasonCode.API_HTTP_ERROR, _API_KEY_SETUP_LABEL,
        )
    return None


def _cli_route_preflight(
    provider: str,
    capability: ProviderRouteCapability,
    now: datetime,
) -> ProviderRouteSnapshot | None:
    try:
        if _cli_is_logged_in(provider):
            return None
    except AgentCliUnavailableError as exc:
        reason_code = _cli_fault_reason_code(exc)
        return _route_preflight_snapshot(
            capability, now, _readiness_of_fault(reason_code), reason_code,
            _CLI_LOGIN_SETUP_LABEL,
        )
    return _route_preflight_snapshot(
        capability, now,
        _readiness_of_fault(SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED),
        SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED, _CLI_LOGIN_SETUP_LABEL,
    )


def _readiness_of_fault(reason_code: SafeRouteReasonCode) -> ProviderRouteReadinessState:
    if reason_code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED:
        return ProviderRouteReadinessState.NOT_CONFIGURED
    return ProviderRouteReadinessState.DISTURBED


def _cli_fault_reason_code(fault: Exception) -> SafeRouteReasonCode:
    if isinstance(fault, CliLoginUnusableError | CliLoginNotConfiguredError):
        return SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED
    if isinstance(fault, CliEnvironmentUnavailableError | CliUnavailableError):
        return SafeRouteReasonCode.CLI_UNAVAILABLE
    return SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR


def _route_preflight_snapshot(
    capability: ProviderRouteCapability,
    now: datetime,
    readiness: ProviderRouteReadinessState,
    reason_code: SafeRouteReasonCode,
    setup_label: str,
) -> ProviderRouteSnapshot:
    return ProviderRouteSnapshot(
        (), None, None, None, readiness, capability,
        normalize_route_failure(reason_code), now, setup_label, None,
    )


def route_setup_label(route: ProviderRoute) -> str:
    """Name the credential a transport route needs to become usable."""
    return _CLI_LOGIN_SETUP_LABEL if route is ProviderRoute.CLI else _API_KEY_SETUP_LABEL


def provider_route_capability() -> ProviderRouteCapability:
    """Return the fixed feature capability of a provider transport route."""
    return ProviderRouteCapability.TOOLS_AVAILABLE


def _unknown_provider_message(provider: str) -> str:
    return f"Unknown provider '{provider}'"


def _cli_is_logged_in(provider: str) -> bool:
    if provider == ProviderName.CLAUDE:
        return cli_login_status().logged_in
    if provider == ProviderName.GROK:
        return grok_cli_status().login.logged_in
    if provider == ProviderName.CODEX:
        return codex_cli_login().logged_in
    raise ValueError(_unknown_provider_message(provider))


def list_provider_models(provider: str, route: ProviderRoute) -> list[str]:
    """The model names this route offers for a turn, as its provider lists them now.

    Every provider answers with the same kind of value: a name that a turn on
    this route accepts as its model. A name is not a fixed model. The Claude CLI
    lists aliases (``opus``, ``sonnet[1m]``) that the CLI maps itself, and an API
    id can be a provider-side alias too. Nor is the list complete: it keeps only
    the names that carry the provider's model prefix or pass its chat filter,
    Codex leaves out the slugs its CLI hides, and the Claude CLI
    also accepts full model ids that it does not list. A name missing from the
    list is no proof that the route refuses it.

    Codex keeps its CLI's priority order; every other route is sorted by name.
    Raises ProviderModelCatalogUnavailableError rather than return an empty
    list, and ProviderUnavailableError for an unknown provider or a missing API
    key.
    """
    return list(_route_offer(provider, route).models)


def _route_offer(provider: str, route: ProviderRoute) -> _RouteOffer:
    config = current_config()
    if provider not in ProviderName:
        raise ProviderUnavailableError(provider, _unknown_provider_message(provider))
    if route is ProviderRoute.CLI:
        method = _cli_setup_method_for(provider)
        if method is ProviderSetupMethod.CLAUDE_CLI:
            return _claude_cli_offer()
        return _RouteOffer(tuple(_models_for_setup_method(provider, method, config)), None)
    key = _secret(_provider_api_credential(provider, config).secret)
    if not key:
        raise ProviderUnavailableError(
            provider, route.value, normalize_route_failure(SafeRouteReasonCode.API_KEY_NOT_SET),
        )
    models = _models_for_setup_method(provider, ProviderSetupMethod.API_KEY, config)
    return _RouteOffer(tuple(models), None)


def _cli_setup_method_for(provider: str) -> ProviderSetupMethod:
    if provider == ProviderName.CLAUDE:
        return ProviderSetupMethod.CLAUDE_CLI
    if provider == ProviderName.GROK:
        return ProviderSetupMethod.GROK_CLI
    if provider == ProviderName.CODEX:
        return ProviderSetupMethod.CODEX_CLI
    raise ValueError(_unknown_provider_message(provider))


def models_with_active_model(models: list[str], active_model: str | None) -> list[str]:
    catalog = list(models)
    if active_model and active_model not in catalog:
        catalog.append(active_model)
    return catalog


def _models_for_setup_method(
    provider: str,
    method: ProviderSetupMethod,
    config: ProviderRuntimeConfig,
) -> list[str]:
    if method is ProviderSetupMethod.GROK_CLI:
        return _list_grok_cli_models()
    if method is ProviderSetupMethod.CODEX_CLI:
        return _list_codex_cli_models()
    if method is ProviderSetupMethod.CLAUDE_CLI:
        raise ValueError("the Claude CLI listing carries a default label; use _claude_cli_offer")

    key = _secret(_provider_api_credential(provider, config).secret)
    if provider == ProviderName.GROK:
        return _list_grok_models(key)
    if provider == ProviderName.CODEX:
        return _list_openai_models(key)
    if provider == ProviderName.CLAUDE:
        return _list_claude_models(key)
    raise ProviderUnavailableError(provider, _unknown_provider_message(provider))


def provider_configuration(
    provider: str,
    cli_methods: frozenset[ProviderSetupMethod],
) -> ProviderConfiguration:
    """Say how this provider is set up for a surface that accepts ``cli_methods``.

    A surface that runs a tool-using session accepts every CLI; one that only
    needs a single completion accepts fewer. The host names its surfaces and
    states their sets; this package answers for the provider.
    """
    credential = _provider_api_credential(provider, current_config())
    cli_method = _cli_setup_method(provider)
    if _secret(credential.secret):
        if provider == ProviderName.CLAUDE and not _anthropic_sdk_available():
            return ProviderCapabilityMissing(provider, _ANTHROPIC_SDK_DISTRIBUTION)
        return ProviderReady(
            provider,
            ProviderSetupMethod.API_KEY,
            credential.environment_key,
        )
    if cli_method is None:
        return ProviderNotLoggedIn(provider, credential.environment_key)
    if isinstance(cli_method, CliEnvironmentCause):
        return ProviderCliUnavailable(provider, cli_method, credential.environment_key)
    if cli_method in cli_methods:
        return ProviderReady(provider, cli_method)
    return ProviderNeedsKey(provider, cli_method, credential.environment_key)


def _cli_setup_method(provider: str) -> ProviderSetupMethod | CliEnvironmentCause | None:
    try:
        if provider == ProviderName.CLAUDE and cli_login_status().logged_in:
            return ProviderSetupMethod.CLAUDE_CLI
        if provider == ProviderName.GROK and grok_cli_token_is_present():
            require_cli_binary(
                current_config().grok_cli_binary, GROK_CLI_BINARY_MISSING_DETAIL,
            )
            return ProviderSetupMethod.GROK_CLI
        if provider == ProviderName.CODEX and codex_cli_login_is_usable():
            require_cli_binary(
                current_config().codex_cli_binary, CODEX_CLI_BINARY_MISSING_DETAIL,
            )
            return ProviderSetupMethod.CODEX_CLI
    except AgentCliUnavailableError as exc:
        log.warning("%s CLI probe unavailable: %s", provider, type(exc).__name__)
        if isinstance(exc, CliEnvironmentUnavailableError):
            return exc.cause
    return None


def _anthropic_sdk_available() -> bool:
    try:
        return find_spec(_ANTHROPIC_SDK_DISTRIBUTION) is not None
    except ModuleNotFoundError:
        return False


def _provider_api_credential(
    provider: str, config: ProviderRuntimeConfig,
) -> _ProviderApiCredential:
    if provider == ProviderName.CLAUDE:
        return _ProviderApiCredential(
            config.anthropic_api_key, ANTHROPIC_API_KEY_ENVIRONMENT,
        )
    if provider == ProviderName.GROK:
        return _ProviderApiCredential(
            config.xai_api_key, XAI_API_KEY_ENVIRONMENT,
        )
    if provider == ProviderName.CODEX:
        return _ProviderApiCredential(
            config.openai_api_key, OPENAI_API_KEY_ENVIRONMENT,
        )
    raise ProviderUnavailableError(provider, _unknown_provider_message(provider))


def _secret(value) -> str:
    if value is None:
        return ""
    return value.get_secret_value()


def _http_model_ids(url: str, headers: dict[str, str], provider: str) -> list[str]:
    try:
        response = httpx.get(
            url, headers=headers, timeout=PROBE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise ProviderModelCatalogUnavailableError(
            provider,
            f"could not list {provider} models",
            normalize_route_failure(SafeRouteReasonCode.CATALOGUE_HTTP_ERROR),
        ) from exc
    if response.status_code >= 400:
        raise ProviderModelCatalogUnavailableError(
            provider,
            f"could not list {provider} models",
            normalize_route_failure(SafeRouteReasonCode.CATALOGUE_HTTP_ERROR),
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderModelCatalogUnavailableError(
            provider,
            f"could not list {provider} models",
            normalize_route_failure(SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR),
        ) from exc
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ProviderModelCatalogUnavailableError(
            provider,
            f"could not list {provider} models",
            normalize_route_failure(SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR),
        )
    ids: list[str] = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            ids.append(row["id"])
    return ids


def _list_grok_models(key: str) -> list[str]:
    ids = _http_model_ids(
        GROK_MODELS_URL,
        {"Authorization": f"Bearer {key}"},
        ProviderName.GROK,
    )
    chat = [
        model_id for model_id in ids
        if _is_provider_model_id(ProviderName.GROK, model_id)
    ]
    if not chat:
        raise ProviderModelCatalogUnavailableError(
            ProviderName.GROK,
            "no chat models returned by grok",
            normalize_route_failure(SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR),
        )
    return sorted(chat)


def _list_grok_cli_models() -> list[str]:
    try:
        status = grok_cli_status()
    except AgentCliUnavailableError as exc:
        raise ProviderModelCatalogUnavailableError(
            ProviderName.GROK,
            "could not list grok CLI models",
            normalize_route_failure(_cli_fault_reason_code(exc)),
        ) from exc
    if not status.login.logged_in:
        raise ProviderModelCatalogUnavailableError(
            ProviderName.GROK,
            "could not list grok CLI models",
            normalize_route_failure(SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED),
        )
    chat = [
        model_id for model_id in status.model_names
        if _is_provider_model_id(ProviderName.GROK, model_id)
    ]
    if not chat:
        raise ProviderModelCatalogUnavailableError(
            ProviderName.GROK,
            "no chat models returned by grok CLI",
            normalize_route_failure(SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR),
        )
    return sorted(chat)


def _list_codex_cli_models() -> list[str]:
    try:
        output = codex_cli_model_catalog()
    except AgentCliUnavailableError as exc:
        raise ProviderModelCatalogUnavailableError(
            ProviderName.CODEX,
            "could not list codex CLI models",
            normalize_route_failure(_cli_fault_reason_code(exc)),
        ) from exc
    try:
        payload = json.loads(output)
        rows = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("codex debug models did not return a models list")
        models = [_CodexCliCatalogLine.model_validate(row) for row in rows]
        visible_models = [
            model for model in models
            if model.visibility != _CODEX_CLI_HIDDEN_VISIBILITY
        ]
        if not visible_models:
            raise ValueError("codex debug models did not return a visible model")
        if len({model.slug for model in visible_models}) != len(visible_models):
            raise ValueError("codex debug models returned duplicate model slugs")
    except (ValidationError, ValueError) as exc:
        raise ProviderModelCatalogUnavailableError(
            ProviderName.CODEX,
            "could not parse codex CLI model catalog",
            normalize_route_failure(SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR),
        ) from exc
    ordered_models = sorted(
        visible_models,
        key=lambda model: (model.priority, model.slug),
    )
    return [model.slug for model in ordered_models]


def _list_openai_models(key: str) -> list[str]:
    ids = _http_model_ids(
        OPENAI_MODELS_URL,
        {"Authorization": f"Bearer {key}"},
        ProviderName.CODEX,
    )
    chat = [
        model_id for model_id in ids
        if _is_provider_model_id(ProviderName.CODEX, model_id)
    ]
    if not chat:
        raise ProviderModelCatalogUnavailableError(
            ProviderName.CODEX,
            "no chat models returned by codex",
            normalize_route_failure(SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR),
        )
    return sorted(chat)


def _claude_cli_offer() -> _RouteOffer:
    try:
        catalog = cli_model_catalog()
    except ClaudeCliUnavailableError as exc:
        raise ProviderModelCatalogUnavailableError(
            ProviderName.CLAUDE,
            CLAUDE_CLI_MODEL_CATALOG_ERROR,
            normalize_route_failure(_cli_fault_reason_code(exc)),
        ) from exc
    return _RouteOffer(tuple(sorted(catalog.aliases)), catalog.current_model_label)


def _list_claude_models(key: str) -> list[str]:
    ids = _http_model_ids(
        ANTHROPIC_MODELS_URL,
        {
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        },
        ProviderName.CLAUDE,
    )
    chat = [
        model_id for model_id in ids
        if _is_provider_model_id(ProviderName.CLAUDE, model_id)
    ]
    if not chat:
        raise ProviderModelCatalogUnavailableError(
            ProviderName.CLAUDE,
            "no chat models returned by claude",
            normalize_route_failure(SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR),
        )
    return sorted(chat)


def _contains_marker(model_id: str, markers: tuple[str, ...]) -> bool:
    lowered = model_id.lower()
    return any(marker in lowered for marker in markers)


def _is_provider_model_id(provider: str, model_id: str) -> bool:
    if provider == ProviderName.CLAUDE:
        return model_id.startswith(CLAUDE_MODEL_PREFIX)
    if provider == ProviderName.GROK:
        return (
            model_id.startswith(GROK_MODEL_PREFIX)
            and not _contains_marker(model_id, GROK_NON_CHAT_MARKERS)
        )
    if provider == ProviderName.CODEX:
        return (
            model_id.startswith(OPENAI_CHAT_PREFIXES)
            and not _contains_marker(model_id, OPENAI_NON_CHAT_MARKERS)
        )
    return False
