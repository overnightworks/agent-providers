"""The two entry points a host calls to run a turn and to ask for an image."""

from __future__ import annotations

import inspect

from provider_test_support import drive_cowriter_turn

from agent_providers import dispatch
from agent_providers.catalog import ProviderRoute
from agent_providers.dispatch import ImageCapability, image_capability
from agent_providers.errors import (
    ProviderUnavailableError,
    SafeRouteReasonCode,
)


def test_stream_turn_refuses_an_unknown_provider_before_any_route_runs():
    turn = drive_cowriter_turn("bob")

    assert isinstance(turn.error, ProviderUnavailableError)
    assert turn.error.reason.code is SafeRouteReasonCode.ROUTE_FAILED
    assert turn.events == []
    assert turn.executed == []


def test_stream_turn_takes_only_keyword_arguments():
    parameters = inspect.signature(dispatch.stream_turn).parameters.values()

    assert {parameter.kind for parameter in parameters} == {inspect.Parameter.KEYWORD_ONLY}


def test_image_capability_says_a_grok_cli_route_carries_no_image_tool():
    capability = image_capability("grok", ProviderRoute.CLI)

    assert isinstance(capability, ImageCapability)
    assert capability.carries_image_tool is False
    assert capability.failure.code is SafeRouteReasonCode.NO_IMAGE_TOOL
