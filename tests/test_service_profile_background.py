"""A profile says whether a job ticket's run may use it: ``background``, required (J9, J16)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import uvicorn
from service_support import (
    SUBSCRIPTION_GROUP,
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    service_config,
    service_config_file,
)

from agent_runtime_service.cli import EXIT_BAD_CONFIG, main
from agent_runtime_service.config import load_config

PROFILE_NAME = "claude-main"
BACKGROUND_LINE = "background = true"
SIGNING_TABLE = re.compile(r"\n\[signing\]\nkey_file = [^\n]*\n")
ORIGIN_LINE = 'mcp_origin = "https://songmaker.internal"'


@pytest.fixture(autouse=True)
def _never_really_serve(monkeypatch):
    def fail_instead_of_binding_a_port(*_arguments, **_where):
        raise AssertionError("a refused configuration reached serving")

    monkeypatch.setattr(uvicorn, "run", fail_instead_of_binding_a_port)


def _configuration(tmp_path: Path, *, background_line: str, signing: bool, origin: bool) -> Path:
    path = service_config_file(tmp_path, subscription_groups=[SUBSCRIPTION_GROUP])
    text = path.read_text(encoding="utf-8")
    assert BACKGROUND_LINE in text
    text = text.replace(BACKGROUND_LINE, background_line)
    if not signing:
        text = SIGNING_TABLE.sub("\n", text)
    if not origin:
        text = re.sub(rf"{ORIGIN_LINE}\n?", "", text)
    path.write_text(text, encoding="utf-8")
    return path


def _refusal(path: Path, capsys) -> str:
    assert main(["--config", str(path)]) == EXIT_BAD_CONFIG
    refusal = capsys.readouterr().err
    assert refusal.count("\n") == 1
    return refusal


def _start(path: Path, monkeypatch) -> list[object]:
    served_apps: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **_where: served_apps.append(app))
    assert main(["--config", str(path)]) == 0
    return served_apps


@pytest.mark.parametrize(
    "background_line",
    [
        pytest.param('background = "true"', id="A1-text"),
        pytest.param("background = 1", id="A1-integer"),
    ],
)
def test_a_profile_with_a_non_boolean_background_stops_the_start(tmp_path, capsys, background_line):
    """J9/A1: a background that is not a boolean refuses the start on one line"""
    path = _configuration(tmp_path, background_line=background_line, signing=True, origin=True)

    assert f"profiles.{PROFILE_NAME}.background" in _refusal(path, capsys)


@pytest.mark.parametrize("background_line", ["background = true", "background = false"])
def test_background_is_a_required_boolean(
    tmp_path, monkeypatch, logging_state_restored, background_line
):
    """J9/A1: true and false both start the service and are read as written"""
    path = _configuration(tmp_path, background_line=background_line, signing=True, origin=False)

    assert len(_start(path, monkeypatch)) == 1
    assert load_config(path).profiles[PROFILE_NAME].background is ("true" in background_line)


def test_a_profile_without_background_stops_the_start(tmp_path, capsys):
    """J9/A1: a profile that omits background refuses the start on one line"""
    path = _configuration(tmp_path, background_line="", signing=True, origin=True)

    assert f"profiles.{PROFILE_NAME}.background" in _refusal(path, capsys)


def test_a_background_profile_needs_a_signing_key(tmp_path, capsys):
    """J16/A2: a background profile without a signing key refuses the start"""
    path = _configuration(
        tmp_path, background_line="background = true", signing=False, origin=False
    )

    assert f"profiles.{PROFILE_NAME}.background" in _refusal(path, capsys)


@pytest.mark.parametrize(
    ("background_line", "signing"),
    [("background = false", False), ("background = true", True)],
)
def test_a_profile_that_needs_no_signing_key_or_has_one_starts(
    tmp_path, monkeypatch, logging_state_restored, background_line, signing
):
    """J16/A2: no signing key is fine for a non-background profile, a key for either"""
    path = _configuration(tmp_path, background_line=background_line, signing=signing, origin=False)

    assert len(_start(path, monkeypatch)) == 1


def test_the_origin_rule_still_speaks_first(tmp_path, capsys):
    """A3: the mcp_origin signing rule is reported before the background rule"""
    path = _configuration(tmp_path, background_line="background = true", signing=False, origin=True)

    assert "apps.songmaker.mcp_origin: needs a [signing] section" in _refusal(path, capsys)


def test_background_profiles_are_those_marked_true():
    """J9/A7: of a true and a false profile, only the true one is a background profile"""
    config = service_config()
    profile = config.profiles[PROFILE_NAME]
    mixed = config.model_copy(
        update={
            "profiles": {
                "marked-true": profile.model_copy(update={"background": True}),
                "marked-false": profile.model_copy(update={"background": False}),
            }
        }
    )

    assert set(mixed.background_profiles()) == {"marked-true"}
