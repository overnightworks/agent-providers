"""The one decider of whether a Codex login can start a child."""

from __future__ import annotations

import copy
import json
import pickle

import pytest
from provider_test_support import (
    A_CODEX_LOGIN_WITH_INVALID_UTF8,
    CANARY_ACCESS_TOKEN,
    CANARY_ID_TOKEN,
    a_codex_login,
)

from agent_providers.codex_login import CodexLogin, CodexLoginVerdict, judge_codex_login

A_DEEPLY_NESTED_DOCUMENT = b"[" * 100_000
A_DOCUMENT_WITH_A_HUGE_INTEGER = b'{"tokens": {"account_id": ' + b"9" * 5_000 + b"}}"

USABLE, INCOMPLETE, MALFORMED = (
    CodexLoginVerdict.USABLE, CodexLoginVerdict.INCOMPLETE, CodexLoginVerdict.MALFORMED,
)


@pytest.mark.parametrize(
    ("document", "verdict"),
    [
        pytest.param(a_codex_login(), USABLE, id="complete"),
        pytest.param(A_CODEX_LOGIN_WITH_INVALID_UTF8, MALFORMED, id="invalid-encoding"),
        pytest.param(A_DEEPLY_NESTED_DOCUMENT, MALFORMED, id="excessive-nesting"),
        pytest.param(A_DOCUMENT_WITH_A_HUGE_INTEGER, MALFORMED, id="integer-over-the-digit-limit"),
        pytest.param(b"not json", MALFORMED, id="bad-syntax"),
        pytest.param(b"[]", MALFORMED, id="top-level-not-an-object"),
        pytest.param(
            b'{"auth_mode":"apikey","OPENAI_API_KEY":"sk-synthetic"}', INCOMPLETE,
            id="api-key-only",
        ),
        pytest.param(b'{"tokens":null}', MALFORMED, id="tokens-null"),
        pytest.param(b'{"tokens":[]}', MALFORMED, id="tokens-not-an-object"),
        pytest.param(b'{"auth_mode":"chatgpt","tokens":{}}', INCOMPLETE, id="empty-tokens"),
        pytest.param(a_codex_login(tokens={"id_token": ""}), INCOMPLETE, id="empty-id-token"),
        pytest.param(a_codex_login(auth_mode=""), INCOMPLETE, id="empty-auth-mode"),
        pytest.param(a_codex_login(last_refresh=None), MALFORMED, id="null-last-refresh"),
        pytest.param(a_codex_login(tokens={"account_id": 7}), MALFORMED, id="numeric-account-id"),
        pytest.param(b'{"tokens":{"account_id":null}}', MALFORMED, id="null-account-id"),
    ],
)
def test_judge_codex_login_names_each_document_shape(
    document: bytes, verdict: CodexLoginVerdict,
) -> None:
    assert judge_codex_login(document).verdict is verdict


def test_a_usable_login_hands_a_child_the_login_without_its_refresh_token() -> None:
    blanked = judge_codex_login(a_codex_login()).blanked_document

    assert blanked == json.dumps({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "last_refresh": "2026-09-04T19:20:00Z",
        "tokens": {
            "id_token": CANARY_ID_TOKEN,
            "access_token": CANARY_ACCESS_TOKEN,
            "account_id": "account",
            "refresh_token": "",
        },
    }).encode()


@pytest.mark.parametrize("document", [b"not json", b'{"tokens":{}}', a_codex_login(last_refresh=3)])
def test_a_login_that_is_not_usable_carries_no_document(document: bytes) -> None:
    assert judge_codex_login(document).blanked_document is None


@pytest.mark.parametrize(
    "disclose",
    [
        pytest.param(repr, id="repr"),
        pytest.param(str, id="str"),
        pytest.param(pickle.dumps, id="pickle"),
        pytest.param(copy.copy, id="copy"),
        pytest.param(copy.deepcopy, id="deepcopy"),
    ],
)
def test_a_usable_codex_login_never_shows_its_tokens(disclose) -> None:
    login = judge_codex_login(a_codex_login())
    assert isinstance(login, CodexLogin)

    if disclose in (repr, str):
        shown = disclose(login)
        assert CANARY_ACCESS_TOKEN not in shown
        assert CANARY_ID_TOKEN not in shown
    else:
        with pytest.raises(TypeError):
            disclose(login)
