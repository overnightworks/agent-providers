"""The copy of the Grok login a child may hold (#255 E10, E10a; defect:#442)."""

from __future__ import annotations

import json

import pytest
from provider_test_support import CANARY_GROK_REFRESH_TOKEN

from agent_runtime.grok_login import blanked_copy

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
