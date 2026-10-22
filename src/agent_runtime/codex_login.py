"""The one decider of whether a Codex login can start a child."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, NoReturn

_TOP_LEVEL_FIELDS: Final = ("auth_mode", "last_refresh")
_TOKEN_FIELDS: Final = ("access_token", "id_token", "account_id")


class CodexLoginVerdict(StrEnum):
    USABLE = "usable"
    INCOMPLETE = "incomplete"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class CodexLogin:
    """The verdict on a login document and, when usable, the copy a child may hold.

    The blanked document keeps the access and id tokens, so it refuses to be
    shown or serialised.
    """

    verdict: CodexLoginVerdict
    blanked_document: bytes | None = field(default=None, repr=False)

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Codex login holds credentials and cannot be serialised")


def assess_codex_login(document: bytes) -> CodexLogin:
    """Assess a login document: complete and renewable-free, unfinished, or unreadable."""
    try:
        parsed = json.loads(document)
    except (ValueError, RecursionError):
        return CodexLogin(CodexLoginVerdict.MALFORMED)
    if not isinstance(parsed, dict):
        return CodexLogin(CodexLoginVerdict.MALFORMED)
    if "tokens" not in parsed:
        return CodexLogin(CodexLoginVerdict.INCOMPLETE)
    tokens = parsed["tokens"]
    if not isinstance(tokens, dict):
        return CodexLogin(CodexLoginVerdict.MALFORMED)
    fields = [parsed.get(name, "") for name in _TOP_LEVEL_FIELDS]
    fields += [tokens.get(name, "") for name in _TOKEN_FIELDS]
    if not all(isinstance(value, str) for value in fields):
        return CodexLogin(CodexLoginVerdict.MALFORMED)
    if not all(fields):
        return CodexLogin(CodexLoginVerdict.INCOMPLETE)
    return CodexLogin(CodexLoginVerdict.USABLE, _blanked_document(parsed, tokens))


def blanked_copy(document: bytes) -> bytes | None:
    """Return the login a child may hold: complete, but not renewable, or ``None``.

    A child that refreshes would rotate the operator's real refresh token and
    break the stored login, so every Codex child receives this copy instead of
    the operator's file.
    """
    return assess_codex_login(document).blanked_document


def _blanked_document(document: dict[str, object], tokens: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "auth_mode": document["auth_mode"],
            "OPENAI_API_KEY": None,
            "last_refresh": document["last_refresh"],
            "tokens": {
                "id_token": tokens["id_token"],
                "access_token": tokens["access_token"],
                "account_id": tokens["account_id"],
                "refresh_token": "",
            },
        }
    ).encode()
