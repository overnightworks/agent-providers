"""The copy of the Grok login a child may hold (#255 E10, E10a; defect:#442)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from grok_login_support import CANARY_GROK_REFRESH_TOKEN
from provider_test_support import (
    GROK_LOGGED_IN,
    SAMPLE_TOOL_CATALOG,
    override_grok_runtime,
)

from agent_runtime import process
from agent_runtime.errors import ProviderUnavailableError, SafeRouteReasonCode
from agent_runtime.grok.transport import GrokCliToolTransport
from agent_runtime.grok_login import blanked_copy
from agent_runtime.process import (
    CliRunOutcome,
    CliRunReason,
    clear_agent_cli_caches,
    grok_cli_status,
)

A_KEPT_MEMBERS = {"key": "grok-synthetic-key", "expires_at": 1234, "scopes": ["a", "b"]}


@pytest.mark.parametrize("name", ["refresh_token", "refreshToken", "refresh"])
@pytest.mark.parametrize("depth", [1, 2])
def test_every_renewal_name_is_blanked_at_every_depth(name: str, depth: int) -> None:
    member = {name: CANARY_GROK_REFRESH_TOKEN, **A_KEPT_MEMBERS}
    document = member if depth == 1 else {"realm": member}

    copy = blanked_copy(json.dumps(document).encode())

    assert copy is not None
    assert json.loads(copy) == (
        {name: "", **A_KEPT_MEMBERS} if depth == 1 else {"realm": {name: "", **A_KEPT_MEMBERS}}
    )
    assert CANARY_GROK_REFRESH_TOKEN.encode() not in copy


def test_a_renewal_member_inside_a_list_is_blanked() -> None:
    document = {"realms": [{"refresh_token": CANARY_GROK_REFRESH_TOKEN, "key": "k"}]}

    copy = blanked_copy(json.dumps(document).encode())

    assert copy is not None
    assert json.loads(copy) == {"realms": [{"refresh_token": "", "key": "k"}]}


def test_a_member_whose_name_only_resembles_a_renewal_name_is_kept() -> None:
    document = {"refresh_token_expiry": 5, "Refresh": "kept", "x.refresh_token": "kept"}

    assert json.loads(blanked_copy(json.dumps(document).encode()) or b"null") == document


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"[]", id="array"),
        pytest.param(b'"refresh_token"', id="string"),
        pytest.param(b"null", id="null"),
        pytest.param(b'{"refresh_token": "\xff"}', id="invalid-utf8"),
        pytest.param('{"a": 1}'.encode("utf-16"), id="utf-16"),
        pytest.param(b'\xef\xbb\xbf{"a": 1}', id="utf-8-byte-order-mark"),
        pytest.param(b"[" * 100_000, id="excessive-nesting"),
    ],
)
def test_a_document_that_is_not_a_json_object_has_no_copy(document: bytes) -> None:
    assert blanked_copy(document) is None


@pytest.mark.parametrize("document", [b"[]", b"not json"], ids=["array", "not-json"])
def test_grok_turn_whose_login_is_not_a_json_object_is_logged_out(
    tmp_path: Path, document: bytes,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(document)
    override_grok_runtime(token_file=source)

    with pytest.raises(ProviderUnavailableError) as raised:
        GrokCliToolTransport(model="grok-test", catalog=SAMPLE_TOOL_CATALOG)

    assert raised.value.reason.code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED


@pytest.mark.parametrize("document", [b"[]", b"not json"], ids=["array", "not-json"])
def test_the_grok_probe_hands_its_child_no_login_when_the_source_is_not_a_json_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: bytes,
) -> None:
    auth_file = tmp_path / "grok-auth.json"
    auth_file.write_bytes(document)
    override_grok_runtime(token_file=auth_file)
    homes_with_a_login: list[bool] = []

    def run_cli_bounded(child, **kwargs):
        has_login = (Path(child.environment["GROK_HOME"]) / "auth.json").exists()
        homes_with_a_login.append(has_login)
        kwargs["on_reaped"](0, False)
        return CliRunOutcome(
            started=True, spawn_error=None, returncode=0,
            stdout=GROK_LOGGED_IN if has_login else "You are not authenticated.\n", stderr="",
            complete=True, became_zombie=False, reason=CliRunReason.COMPLETE,
        )

    monkeypatch.setattr(process, "run_cli_bounded", run_cli_bounded)
    clear_agent_cli_caches()

    assert grok_cli_status().login.logged_in is False
    assert homes_with_a_login == [False]
