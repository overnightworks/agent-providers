"""The public route-failure vocabulary is stable and secret-free."""

from __future__ import annotations

import asyncio
import copy
import json
import pickle
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass

import pytest
from provider_test_support import (
    A_CANARY,
    SAMPLE_TOOL_CATALOG,
    a_call_to_an_undeclared_tool_carrying,
)
from pydantic import BaseModel, SecretStr

from agent_providers import dispatch
from agent_providers.errors import (
    CodexProcessPoolSaturatedError,
    ProviderError,
    ProviderModelCatalogUnavailableError,
    ProviderUnavailableError,
    SafeRouteReasonCode,
    TextToolCallRejectedError,
    ToolProtocolRejectionCode,
    normalize_route_failure,
)
from agent_providers.text_tool_protocol import (
    TextToolProtocolError,
    TextToolRejection,
    TextToolRejectionRule,
    TextToolStreamParser,
)


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (SafeRouteReasonCode.NO_IMAGE_TOOL, "no image tool"),
        (SafeRouteReasonCode.API_KEY_NOT_SET, "API key is not set."),
        (SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED, "CLI is not signed in."),
        (SafeRouteReasonCode.CLI_AUTH_REJECTED, "CLI login was rejected or has expired."),
        (SafeRouteReasonCode.CLI_UNAVAILABLE, "CLI is unavailable."),
        (SafeRouteReasonCode.CLI_CAPACITY_EXHAUSTED, "Codex is busy. Try again shortly."),
        (SafeRouteReasonCode.CLI_PROTOCOL_ERROR, "CLI returned an invalid response."),
        (SafeRouteReasonCode.API_HTTP_ERROR, "API request failed."),
        (SafeRouteReasonCode.API_PROTOCOL_ERROR, "API returned an invalid response."),
        (SafeRouteReasonCode.CATALOGUE_HTTP_ERROR, "Model catalogue request failed."),
        (SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR, "Model catalogue response was invalid."),
        (SafeRouteReasonCode.TOOL_EXECUTION_FAILED, "Tool failed."),
        (SafeRouteReasonCode.TOOL_PROTOCOL_ERROR, "Tool response was invalid."),
        (SafeRouteReasonCode.TOOL_LIMIT_EXCEEDED, "Tool-call limit was reached."),
        (SafeRouteReasonCode.ROUTE_FAILED, "Selected route failed."),
    ],
)
def test_route_failure_code_has_its_exact_safe_message(code, message):
    assert normalize_route_failure(code).model_dump() == {"code": code, "message": message}


def test_the_reason_codes_are_exactly_the_fifteen_public_values():
    assert {code.value for code in SafeRouteReasonCode} == {
        "api_key_not_set",
        "cli_login_not_configured",
        "cli_auth_rejected",
        "cli_unavailable",
        "cli_capacity_exhausted",
        "cli_protocol_error",
        "api_http_error",
        "api_protocol_error",
        "catalogue_http_error",
        "catalogue_protocol_error",
        "tool_execution_failed",
        "tool_protocol_error",
        "tool_limit_exceeded",
        "no_image_tool",
        "route_failed",
    }


def test_tool_protocol_rejection_codes_are_closed() -> None:
    assert {code.value for code in ToolProtocolRejectionCode} == {
        "tool_result_batch_invalid",
        "text_tool_response_invalid",
        "native_tool_blocked",
        "tool_loop_terminal_invalid",
        "cli_stream_response_invalid",
    }


def test_terminal_tool_loop_rejection_logs_a_closed_code_without_input(caplog) -> None:
    marker = "synthetic-confidential-input"

    class EmptyTransport:
        async def stream(self, _message):
            if False:
                yield None

        async def aclose(self) -> None:
            return None

    async def collect() -> None:
        turn = dispatch._stream_cli_tool_turn(
            provider="grok",
            system=marker,
            messages=[],
            executor=lambda _name, _arguments: None,
            transport=EmptyTransport(),
            correlation_id=None,
        )
        with pytest.raises(ProviderUnavailableError) as raised:
            async for _ in turn:
                pass
        assert raised.value.reason.code is SafeRouteReasonCode.TOOL_PROTOCOL_ERROR

    caplog.set_level("WARNING", logger="agent_providers.dispatch")
    asyncio.run(collect())
    assert (
        f"Tool protocol rejection provider=grok route=cli code="
        f"{ToolProtocolRejectionCode.TOOL_LOOP_TERMINAL_INVALID}"
    ) in caplog.text
    assert marker not in caplog.text


class _HostModel(BaseModel):
    rejection: TextToolRejection


@dataclass
class _HostRecord:
    rejection: TextToolRejection


def _the_parser_refuses_the_canary_call() -> TextToolProtocolError:
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)
    parser.feed(a_call_to_an_undeclared_tool_carrying())
    try:
        parser.finish()
    except TextToolProtocolError as refused:
        return refused
    raise AssertionError("the canary call was accepted")


def _the_host_catches(error: TextToolCallRejectedError) -> TextToolCallRejectedError:
    try:
        raise error
    except TextToolCallRejectedError as caught:
        return caught


@pytest.fixture
def parser_error() -> TextToolProtocolError:
    return _the_parser_refuses_the_canary_call()


@pytest.fixture
def host_error(parser_error) -> TextToolCallRejectedError:
    return _the_host_catches(TextToolCallRejectedError("grok", "cli", parser_error.rejection))


def test_the_canary_call_is_refused_under_the_unknown_tool_rule(parser_error):
    assert parser_error.rejection.rule is TextToolRejectionRule.UNKNOWN_TOOL
    assert A_CANARY in parser_error.rejection.excerpt.get_secret_value()


type _Rendering = Callable[[TextToolProtocolError, TextToolCallRejectedError], str]

_RENDERINGS: dict[str, tuple[_Rendering, bool]] = {
    "str-of-the-record": (lambda refused, host: str(refused.rejection), True),
    "repr-of-the-record": (lambda refused, host: repr(refused.rejection), True),
    "f-string-of-the-record": (lambda refused, host: f"{refused.rejection}", True),
    "percent-s-of-the-record": (lambda refused, host: "%s" % refused.rejection, True),  # noqa: UP031
    "str-of-the-excerpt": (lambda refused, host: str(refused.rejection.excerpt), True),
    "str-of-the-parser-error": (lambda refused, host: str(refused), False),
    "repr-of-the-parser-error": (lambda refused, host: repr(refused), False),
    "str-of-the-host-error": (lambda refused, host: str(host), False),
    "repr-of-the-host-error": (lambda refused, host: repr(host), False),
    "f-string-of-the-host-error": (lambda refused, host: f"{host}", False),
    "percent-s-of-the-host-error": (lambda refused, host: "%s" % host, False),  # noqa: UP031
    "args-of-the-host-error": (lambda refused, host: repr(host.args), False),
    "traceback-of-the-parser-error": (
        lambda refused, host: "".join(traceback.format_exception(refused)), False,
    ),
    "traceback-of-the-host-error": (
        lambda refused, host: "".join(traceback.format_exception(host)), False,
    ),
    "reason-json-of-the-host-error": (lambda refused, host: host.reason.model_dump_json(), False),
    "repr-of-a-host-pydantic-model": (
        lambda refused, host: repr(_HostModel(rejection=refused.rejection)), True,
    ),
    "json-of-a-host-pydantic-model": (
        lambda refused, host: _HostModel(rejection=refused.rejection).model_dump_json(), True,
    ),
    "json-mode-dump-of-a-host-pydantic-model": (
        lambda refused, host: repr(
            _HostModel(rejection=refused.rejection).model_dump(mode="json"),
        ),
        True,
    ),
    "repr-of-a-host-dataclass": (
        lambda refused, host: repr(_HostRecord(refused.rejection)), True,
    ),
    "asdict-of-a-host-dataclass": (
        lambda refused, host: repr(asdict(_HostRecord(refused.rejection))), True,
    ),
}


@pytest.mark.parametrize(
    ("render", "shows_the_mask"), _RENDERINGS.values(), ids=_RENDERINGS.keys(),
)
def test_no_ordinary_rendering_shows_the_refused_text(
    parser_error, host_error, render, shows_the_mask,
):
    rendered = render(parser_error, host_error)

    assert A_CANARY not in rendered
    assert ("**********" in rendered) is shows_the_mask


def test_the_host_error_is_a_provider_unavailable_error_with_the_same_safe_answer(host_error):
    assert isinstance(host_error, ProviderUnavailableError)
    assert host_error.reason == normalize_route_failure(SafeRouteReasonCode.TOOL_PROTOCOL_ERROR)
    assert str(host_error) == "Tool response was invalid."
    assert (host_error.provider, host_error.route) == ("grok", "cli")
    assert host_error.__context__ is None
    assert host_error.__cause__ is None


def test_the_excerpt_cannot_be_serialized_by_plain_json(parser_error):
    with pytest.raises(TypeError):
        json.dumps(parser_error.rejection.excerpt)


_ROUND_TRIPS: dict[str, Callable] = {
    "pickle": lambda error: pickle.loads(pickle.dumps(error)),  # noqa: S301
    "copy": copy.copy,
    "deepcopy": copy.deepcopy,
}


@pytest.mark.parametrize("round_trip", _ROUND_TRIPS.values(), ids=_ROUND_TRIPS.keys())
@pytest.mark.parametrize("which", ["parser-error", "host-error"])
def test_a_rejection_error_survives_a_round_trip_with_its_mask(
    parser_error, host_error, which, round_trip,
):
    original = parser_error if which == "parser-error" else host_error

    rebuilt = round_trip(original)

    assert type(rebuilt) is type(original)
    assert str(rebuilt) == str(original)
    assert rebuilt.rejection == original.rejection
    assert isinstance(rebuilt.rejection.excerpt, SecretStr)
    assert rebuilt.rejection.excerpt.get_secret_value() == (
        original.rejection.excerpt.get_secret_value()
    )
    if which == "host-error":
        assert (rebuilt.provider, rebuilt.route, rebuilt.reason) == (
            original.provider, original.route, original.reason,
        )


@pytest.mark.parametrize("which", ["parser-error", "host-error"])
def test_pickled_bytes_carry_the_excerpt_in_plain_text(parser_error, host_error, which):
    original = parser_error if which == "parser-error" else host_error

    assert A_CANARY.encode() in pickle.dumps(original)


def test_each_error_rebuilds_from_its_constructor_arguments(parser_error, host_error):
    rejection = parser_error.rejection

    assert parser_error.__reduce__() == (TextToolProtocolError, (rejection,))
    assert host_error.__reduce__() == (
        TextToolCallRejectedError, ("grok", "cli", rejection), host_error.__dict__,
    )


_TOOL_PROTOCOL_REASON = normalize_route_failure(SafeRouteReasonCode.TOOL_PROTOCOL_ERROR)

_PLAIN_ERRORS = {
    "provider-error": ProviderError("grok", "grok is down"),
    "unavailable-with-message": ProviderUnavailableError("grok", "no key"),
    "unavailable-with-reason": ProviderUnavailableError("grok", "cli", _TOOL_PROTOCOL_REASON),
    "catalog-with-message": ProviderModelCatalogUnavailableError("codex", "no catalog"),
    "catalog-with-reason": ProviderModelCatalogUnavailableError(
        "codex", "ignored", _TOOL_PROTOCOL_REASON,
    ),
    "pool-saturated": CodexProcessPoolSaturatedError(kind="turn", scope="host"),
}


@pytest.mark.parametrize("round_trip", _ROUND_TRIPS.values(), ids=_ROUND_TRIPS.keys())
@pytest.mark.parametrize("original", _PLAIN_ERRORS.values(), ids=_PLAIN_ERRORS.keys())
def test_a_route_error_survives_a_round_trip_with_its_attributes(original, round_trip):
    original = copy.copy(original)
    original.add_note("a note")
    original.extra = "an extra attribute"

    rebuilt = round_trip(original)

    assert rebuilt.__notes__ == ["a note"]
    assert rebuilt.extra == "an extra attribute"
    assert type(rebuilt) is type(original)
    assert str(rebuilt) == str(original)
    assert rebuilt.__dict__ == original.__dict__
