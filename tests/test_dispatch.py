"""The two entry points a host calls to run a turn and to ask for an image."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import pytest
from provider_test_support import drive_cowriter_turn

import agent_providers
from agent_providers import dispatch
from agent_providers.catalog import ProviderRoute
from agent_providers.dispatch import ImageCapability, image_capability
from agent_providers.errors import (
    ProviderUnavailableError,
    SafeRouteReasonCode,
)


def test_stream_turn_refuses_an_unknown_provider_before_any_route_runs(monkeypatch):
    routes_started: list[str] = []

    def start_route(**arguments):
        routes_started.append(arguments["provider"])
        raise ProviderUnavailableError(arguments["provider"], "route started")

    monkeypatch.setattr(dispatch, "_stream_for_route", start_route)

    turn = drive_cowriter_turn("bob")

    assert isinstance(turn.error, ProviderUnavailableError)
    assert turn.error.reason.code is SafeRouteReasonCode.ROUTE_FAILED
    assert turn.events == []
    assert turn.executed == []
    assert routes_started == []


def test_stream_turn_takes_only_keyword_arguments():
    parameters = inspect.signature(dispatch.stream_turn).parameters.values()

    assert {parameter.kind for parameter in parameters} == {inspect.Parameter.KEYWORD_ONLY}


def test_image_capability_says_a_grok_cli_route_carries_no_image_tool():
    capability = image_capability("grok", ProviderRoute.CLI)

    assert isinstance(capability, ImageCapability)
    assert capability.carries_image_tool is False
    assert capability.failure.code is SafeRouteReasonCode.NO_IMAGE_TOOL


README = Path(__file__).resolve().parents[1] / "README.md"
DOTTED_NAME = re.compile(r"`((?:[a-z_]+\.)+[A-Za-z_]+)`")


def names_table_rows() -> list[tuple[str, str]]:
    names_section = README.read_text().split("### Names and values", 1)[1].split("\n## ", 1)[0]
    table_lines = [line for line in names_section.splitlines() if line.startswith("|")]
    rows = []
    for line in table_lines[2:]:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            raise ValueError(f"Malformed Names-table row: {line!r}")
        rows.append((cells[0], cells[1]))
    return rows


def renamed_public_names() -> list[str]:
    return sorted({
        old
        for before, now in names_table_rows()
        for old in DOTTED_NAME.findall(before)
        if old not in DOTTED_NAME.findall(now)
    })


def package_modules() -> list[object]:
    return [
        importlib.import_module(module_info.name)
        for module_info in pkgutil.walk_packages(agent_providers.__path__, "agent_providers.")
    ] + [agent_providers]


def test_the_names_table_lists_the_renames_this_release_made():
    assert renamed_public_names()
    assert any(now == "`dispatch.stream_turn`" for _, now in names_table_rows())


@pytest.mark.parametrize("old_name", renamed_public_names())
def test_a_renamed_public_name_is_gone_from_every_module_without_an_alias(old_name: str):
    attribute = old_name.rpartition(".")[2]

    modules_still_naming_it = [
        module.__name__ for module in package_modules() if hasattr(module, attribute)
    ]

    assert modules_still_naming_it == []
