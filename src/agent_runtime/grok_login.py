"""The copy of the Grok login a child may hold."""

from __future__ import annotations

import json
from typing import Final

_RENEWAL_NAMES: Final = frozenset({"refresh_token", "refreshToken", "refresh"})


def blanked_copy(document: bytes) -> bytes | None:
    """Return the login a child may hold: the document with its renewal members empty.

    A child that renews would rotate the operator's real refresh token and break
    the stored login, so every Grok child receives this copy instead of the
    operator's file. A document that is not a JSON object has no copy.
    """
    try:
        parsed = json.loads(document)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return json.dumps(_blanked(parsed)).encode()
    except RecursionError:
        return None


def _blanked(value: object) -> object:
    if isinstance(value, dict):
        return {
            name: "" if name in _RENEWAL_NAMES else _blanked(member)
            for name, member in value.items()
        }
    if isinstance(value, list):
        return [_blanked(member) for member in value]
    return value
