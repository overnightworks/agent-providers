"""The streamed run contract between the agent service and its clients.

This module is the one owner of what travels on a run's event stream
(decision record 0001, H6): a closed union discriminated on ``type``, every
event carrying the protocol version, the run id and its sequence number, and
the typed body the service answers with when it refuses a run before
admission. A new variant or a new field is a new ``PROTOCOL_VERSION``.

Validation is strict, so the wire's JSON types are the contract: a sequence
number sent as ``"3"`` is a mismatch, not a coercion. Only the run id and the
closed codes are read from their JSON strings. The envelope has no defaults:
an event without its version or sequence number is not an event.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from agent_client.tools import ToolEffect

PROTOCOL_VERSION: Final = 1
MAX_EVENT_BYTES: Final = 1_048_576
MAX_MESSAGE_LENGTH: Final = 500


class FailureCode(StrEnum):
    """Why a run ended as ``failed``."""

    INTERRUPTED = "interrupted"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_ERROR = "provider_error"
    LIMIT_EXCEEDED = "limit_exceeded"
    IDLE_TIMEOUT = "idle_timeout"
    INTERNAL_ERROR = "internal_error"


class RejectionCode(StrEnum):
    """Why the service refused a run before admitting it."""

    NOT_ENABLED = "not_enabled"
    BUSY = "busy"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    INVALID_REQUEST = "invalid_request"


def _json_integer(value: object) -> object:
    # A Literal ignores strict mode and matches by equality, so without this
    # JSON ``true`` and ``1.0`` would pass as 1 and ``false`` as 0.
    if type(value) is not int:
        raise ValueError("must be a JSON integer")
    return value


ProtocolVersion = Annotated[Literal[1], BeforeValidator(_json_integer)]
"""The one decider of whether a ``v`` is the version this package speaks."""


class _WireModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    v: ProtocolVersion


class _RunEvent(_WireModel):
    run_id: Annotated[UUID, Field(strict=False)]
    seq: int = Field(ge=0)


class RunStarted(_RunEvent):
    """The first event of every run, naming the heartbeat cadence."""

    type: Literal["run_started"] = "run_started"
    seq: Annotated[Literal[0], BeforeValidator(_json_integer)] = 0
    heartbeat_interval_s: int = Field(ge=1, le=300)


class AssistantText(_RunEvent):
    type: Literal["assistant_text"] = "assistant_text"
    text: str = Field(min_length=1)


class ToolCall(_RunEvent):
    """A tool the model asked for, emitted before the tool runs."""

    type: Literal["tool_call"] = "tool_call"
    tool_use_id: str
    name: str
    input: dict[str, Any]
    effect: Annotated[ToolEffect, Field(strict=False)]


class ToolResult(_RunEvent):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool


class Heartbeat(_RunEvent):
    """Proof of life while a run is idle; it counts in ``seq`` like any event."""

    type: Literal["heartbeat"] = "heartbeat"


class Succeeded(_RunEvent):
    type: Literal["succeeded"] = "succeeded"
    text: str


class Failed(_RunEvent):
    """A run that ended without success.

    ``message`` is written by the service, never a provider's raw output.
    """

    type: Literal["failed"] = "failed"
    code: Annotated[FailureCode, Field(strict=False)]
    message: str = Field(max_length=MAX_MESSAGE_LENGTH)


class Cancelled(_RunEvent):
    type: Literal["cancelled"] = "cancelled"


TerminalEvent = Succeeded | Failed | Cancelled

RunEvent = Annotated[
    RunStarted | AssistantText | ToolCall | ToolResult | Heartbeat | Succeeded | Failed | Cancelled,
    Field(discriminator="type"),
]


class RunRejection(_WireModel):
    """The HTTP body of a run the service refused before admission."""

    code: Annotated[RejectionCode, Field(strict=False)]
    message: str
