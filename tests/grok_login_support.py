"""The Grok login a test child is handed, and the copy it may hold (#255 E10, E10a)."""

from __future__ import annotations

import json
from typing import Any

CANARY_GROK_REFRESH_TOKEN = "canary-grok-refresh-token"


def a_grok_login() -> bytes:
    return json.dumps({
        "xai.example": {
            "key": "grok-synthetic-key",
            "refresh_token": CANARY_GROK_REFRESH_TOKEN,
        },
    }).encode()


def a_blanked_grok_login() -> dict[str, Any]:
    return {"xai.example": {"key": "grok-synthetic-key", "refresh_token": ""}}
