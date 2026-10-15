"""The body that starts a run, read strictly.

The body is strict JSON — no ``NaN`` or infinities, no member named twice — and
is parsed before its protocol version is read, so only a parsed object whose
``v`` this service does not speak is ``unsupported_version``; everything else
that is not a ``RunRequest`` is ``invalid_request``. The body is UTF-8 only
(``json.loads`` on bytes would also sniff UTF-16 and UTF-32). The size limit is
the transport's to enforce before it buffers the body.
"""

from __future__ import annotations

import json
from typing import Any, Final

from pydantic import TypeAdapter, ValidationError

from agent_client.wire import ProtocolVersion, RejectionCode, RunRequest

MAX_REQUEST_BYTES: Final = 256 * 1024

_PROTOCOL_VERSION: Final[TypeAdapter[ProtocolVersion]] = TypeAdapter(ProtocolVersion)


class _NotStrictJson(ValueError):
    pass


def parse_run_request(body: bytes) -> RunRequest | RejectionCode:
    try:
        document = json.loads(
            body.decode("utf-8-sig"),
            object_pairs_hook=_refuse_duplicate_members,
            parse_constant=_refuse_constant,
        )
    except (ValueError, RecursionError):
        return RejectionCode.INVALID_REQUEST
    if not isinstance(document, dict):
        return RejectionCode.INVALID_REQUEST
    try:
        _PROTOCOL_VERSION.validate_python(document.get("v"))
    except ValidationError:
        return RejectionCode.UNSUPPORTED_VERSION
    try:
        return RunRequest.model_validate(document)
    except ValidationError:
        return RejectionCode.INVALID_REQUEST


def _refuse_duplicate_members(members: list[tuple[str, Any]]) -> dict[str, Any]:
    document = dict(members)
    if len(document) != len(members):
        raise _NotStrictJson("a member is named twice")
    return document


def _refuse_constant(constant: str) -> object:
    raise _NotStrictJson(f"{constant} is not a JSON value")
