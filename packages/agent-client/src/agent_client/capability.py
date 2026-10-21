"""The run capability: the claims the service signs and an app's MCP server verifies.

The service mints one capability per run (decision record 0001, H3 and the A1a4
ruling) and hands it to the runner as a bearer token of type
``agent-capability+jwt``. ``agent_client.mcp_server`` verifies against this module:
it pins RS256 itself, trusts the one configured public key, checks the token type, the
issuer, the audience (its own ``McpOrigin``), the length bound and these claims,
refuses a token issued in the future, and checks each call's tool against
``tools``. This module owns the claim names and the one decider of an origin;
it signs and verifies nothing itself. A changed claim set gets a new token type.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from agent_client.wire import NonEmptyUnicodeText

CAPABILITY_ISSUER: Final = "agent-service"
CAPABILITY_TOKEN_TYPE: Final = "agent-capability+jwt"
MAX_CAPABILITY_CHARACTERS: Final = 8192
SIGNING_KEY_BITS: Final = 3072
SIGNING_KEY_EXPONENT: Final = 65537
MCP_ENDPOINT_PATH: Final = "/mcp"

_HTTPS_SCHEME: Final = "https://"
_DEFAULT_HTTPS_PORT: Final = 443
_HIGHEST_PORT: Final = 65535
_LONGEST_PORT: Final = len(str(_HIGHEST_PORT))
_LONGEST_HOST: Final = 253
_LONGEST_LABEL: Final = 63
_STRICT_ORIGIN: Final = re.compile(r"https://(?P<host>[a-z0-9.-]+)(?::(?P<port>\d+))?", re.ASCII)
_LOOSE_ORIGIN: Final = re.compile(
    r"https://(?P<host>[a-z0-9.-]+)(?::(?P<port>\d+))?/?", re.ASCII | re.IGNORECASE
)
_DNS_LABEL: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", re.ASCII)
_NUMERIC_LABEL: Final = re.compile(r"\d+|0x[0-9a-f]*", re.ASCII)


def _refusal_of_strict_origin(text: str) -> str | None:
    if (parts := _STRICT_ORIGIN.fullmatch(text)) is None:
        return (
            "must be https://<host> or https://<host>:<port> and nothing else: no user"
            " information, path, query, fragment, space, control or non-ASCII character"
        )
    if not _is_dns_name(parts["host"]):
        return (
            "must name a lower-case DNS host: labels of letters, digits and hyphens, 1 to 63"
            " characters each, none starting or ending with a hyphen, 253 in all"
        )
    if _NUMERIC_LABEL.fullmatch(parts["host"].rsplit(".", 1)[-1]):
        return "must name a DNS host, not an IP address or a number"
    port = parts["port"]
    if port is not None and not _is_written_non_default_port(port):
        return (
            "must write a port as decimal digits from 1 to 65535, without leading zeros, never 443"
        )
    return None


def _is_dns_name(host: str) -> bool:
    return len(host) <= _LONGEST_HOST and all(
        len(label) <= _LONGEST_LABEL and _DNS_LABEL.fullmatch(label) for label in host.split(".")
    )


def _is_written_non_default_port(port: str) -> bool:
    return (
        len(port) <= _LONGEST_PORT
        and port[0] != "0"
        and int(port) <= _HIGHEST_PORT
        and int(port) != _DEFAULT_HTTPS_PORT
    )


def _canonical_form_that_loads(text: str) -> str | None:
    if (parts := _LOOSE_ORIGIN.fullmatch(text)) is None:
        return None
    candidate = (
        f"{_HTTPS_SCHEME}{parts['host'].removesuffix('.').lower()}{_shown_port(parts['port'])}"
    )
    return candidate if _refusal_of_strict_origin(candidate) is None else None


def _shown_port(written: str | None) -> str:
    port = (written or "").lstrip("0") or ("0" if written else "")
    return "" if port in ("", str(_DEFAULT_HTTPS_PORT)) else f":{port}"


def _origin_in_serialized_form(text: str) -> str:
    refusal = _refusal_of_strict_origin(text)
    if refusal is None:
        return text
    if (canonical := _canonical_form_that_loads(text)) is not None:
        raise ValueError(f"must be written in its canonical form {canonical}")
    if not text.lower().startswith(_HTTPS_SCHEME):
        raise ValueError("must be an https origin: https://<host> or https://<host>:<port>")
    raise ValueError(refusal)


McpOrigin = Annotated[str, AfterValidator(_origin_in_serialized_form)]
"""The one decider of an app's MCP origin: a canonical https origin on a DNS name."""


def _run_id_in_canonical_text(text: str) -> str:
    try:
        canonical = str(UUID(text))
    except ValueError:
        raise ValueError("must be a run id in lower-case 8-4-4-4-12 form") from None
    if canonical != text:
        raise ValueError("must be a run id in lower-case 8-4-4-4-12 form")
    return text


RunIdText = Annotated[str, AfterValidator(_run_id_in_canonical_text)]


class _StrictClaims(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class CapabilityPrincipal(_StrictClaims):
    """The verified caller exactly as the service saw it: no bound, no normalization."""

    iss: NonEmptyUnicodeText
    sub: NonEmptyUnicodeText


class CapabilityClaims(_StrictClaims):
    """What a capability says; ``sub`` names the run and does not prove the run is live."""

    iss: Literal[CAPABILITY_ISSUER]
    aud: McpOrigin
    sub: RunIdText
    iat: int
    exp: int
    app: NonEmptyUnicodeText
    purpose: NonEmptyUnicodeText
    principal: CapabilityPrincipal
    tools: Annotated[tuple[NonEmptyUnicodeText, ...], Field(strict=False)]

    @model_validator(mode="after")
    def _expires_after_it_is_issued_and_names_each_tool_once(self) -> Self:
        if self.exp <= self.iat:
            raise ValueError("exp must be later than iat")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("tools must name each tool once")
        return self

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> Self:
        """Reads a decoded mapping; strict pydantic refuses a read-only proxy, so it is copied."""
        return cls.model_validate(dict(claims))


def carries_signature_of(token: str, data: bytes) -> bool:
    """Whether ``data`` carries ``token``, whole or as its signature segment.

    One needle is enough: every whole token contains its signature segment, the part
    after the last ``.``. Bodies the relay writes are ``json.dumps`` output, which
    writes base64url characters and ``.`` verbatim, and tool results are asked after
    decoding. A token split across strings, base64-encoded or reversed is not
    detected: this stops reflection, not an app that encodes its own capability.
    """
    signature_segment = token.rsplit(".", 1)[-1]
    return signature_segment.encode("ascii") in data
