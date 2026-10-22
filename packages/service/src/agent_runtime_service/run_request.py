"""The body that starts a run, read strictly.

The body is strict JSON — no ``NaN`` or infinities, no member named twice — and
is parsed before its protocol version is read, so only a parsed object whose
``v`` this service does not speak is ``unsupported_version``; everything else
that is not a ``RunRequest`` is ``invalid_request``. The body is UTF-8 only
(``json.loads`` on bytes would also sniff UTF-16 and UTF-32). The size limit is
the transport's to enforce before it buffers the body.
"""

from __future__ import annotations

from typing import Final

from pydantic import TypeAdapter, ValidationError

from agent_runtime_client.mcp_wire import read_strict_json
from agent_runtime_client.wire import ProtocolVersion, RejectionCode, RunRequest

MAX_REQUEST_BYTES: Final = 256 * 1024

_PROTOCOL_VERSION: Final[TypeAdapter[ProtocolVersion]] = TypeAdapter(ProtocolVersion)


def parse_run_request(body: bytes) -> RunRequest | RejectionCode:
    try:
        document = read_strict_json(body.decode("utf-8-sig"))
    except ValueError:
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
