"""The run contract between the agent service and its clients.

This module is the one owner of what travels between a client and the
service (decision record 0001, H6): the request that starts a run, the run's
event stream as a closed union discriminated on ``type`` with every event
carrying the protocol version, the run id and its sequence number, and the
typed body the service answers with when it refuses a request.
A new variant or a new field is a new ``PROTOCOL_VERSION``.

Validation is strict, so the wire's JSON types are the contract: a sequence
number sent as ``"3"`` is a mismatch, not a coercion. Only the run id and the
closed codes and roles are read from their JSON strings. The version and sequence
number have no defaults.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, TypeGuard, get_args
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

from agent_client.tools import ToolEffect

PROTOCOL_VERSION: Final = 1
MAX_EVENT_BYTES: Final = 1_048_576
MAX_TOOL_INPUT_DEPTH: Final = 128
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
    """Why the service refused a request: to start a run, or to read one."""

    NOT_ENABLED = "not_enabled"
    BUSY = "busy"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"
    NOT_FOUND = "not_found"


def _json_integer(value: object) -> object:
    # A Literal ignores strict mode and matches by equality, so without this
    # JSON ``true`` and ``1.0`` would pass as 1 and ``false`` as 0.
    if type(value) is not int:
        raise ValueError("must be a JSON integer")
    return value


ProtocolVersion = Annotated[Literal[1], BeforeValidator(_json_integer)]
"""The one decider of whether a ``v`` is the version this package speaks."""

ToolUseId = Annotated[str, Field(min_length=1)]
"""The one decider of a valid call id, so a call and its result accept the same ids."""


def _refuse_non_json_object(value: dict[Any, Any]) -> dict[str, Any]:
    # The depth limit means depth never stops a built input from encoding:
    # pydantic's serializer refuses a value nested about 255 levels deep.
    # Only the containers on the current path count as open, so a value shared
    # by two branches is accepted, as ``json.dumps`` accepts it. The path holds
    # each open container's remaining children, so the walk's own bookkeeping
    # never mixes with the caller's values.
    open_containers: set[int] = set()
    path = [_enter_container(value, open_containers)]
    while path:
        container_id, children = path[-1]
        for child in children:
            if isinstance(child, dict | list):
                path.append(_enter_container(child, open_containers))
                break
            _refuse_non_json_scalar(child)
        else:
            path.pop()
            open_containers.remove(container_id)
    return value


def _enter_container(
    container: dict[Any, Any] | list[Any], open_containers: set[int]
) -> tuple[int, Iterator[object]]:
    if id(container) in open_containers:
        raise ValueError("a value that contains itself is not JSON")
    if len(open_containers) == MAX_TOOL_INPUT_DEPTH:
        raise ValueError(f"tool input nests deeper than {MAX_TOOL_INPUT_DEPTH} objects and arrays")
    open_containers.add(id(container))
    if isinstance(container, dict):
        for key in container:
            _refuse_non_json_key(key)
        return id(container), iter(container.values())
    return id(container), iter(container)


def _refuse_non_json_key(key: object) -> None:
    if not isinstance(key, str):
        raise ValueError(f"a {type(key).__name__} key is not a JSON object key")
    _refuse_lone_surrogates(key)


def _refuse_non_json_scalar(value: object) -> None:
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{value} is not a JSON number")
    elif isinstance(value, str):
        _refuse_lone_surrogates(value)
    else:
        raise ValueError(f"a {type(value).__name__} is not a JSON value")


def _refuse_lone_surrogates(text: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("a string with a lone surrogate is not valid Unicode") from error


JsonObject = Annotated[dict[str, Any], AfterValidator(_refuse_non_json_object)]
"""The one decider of a JSON object on the wire: one that survives a round trip unchanged."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class _WireModel(_StrictModel):
    v: ProtocolVersion


class _RunEvent(_WireModel):
    run_id: Annotated[UUID, Field(strict=False)]
    seq: int = Field(ge=0)


class RunStarted(_RunEvent):
    """The first event of every run, naming the heartbeat cadence."""

    type: Literal["run_started"] = "run_started"
    seq: Annotated[Literal[0], BeforeValidator(_json_integer)]
    heartbeat_interval_s: int = Field(ge=1, le=300)


class AssistantText(_RunEvent):
    type: Literal["assistant_text"] = "assistant_text"
    text: str = Field(min_length=1)


class ToolCall(_RunEvent):
    """A tool the model asked for, emitted before the tool runs."""

    type: Literal["tool_call"] = "tool_call"
    tool_use_id: ToolUseId
    name: str
    input: JsonObject
    effect: Annotated[ToolEffect, Field(strict=False)]


class ToolResult(_RunEvent):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: ToolUseId
    content: str
    is_error: bool


class Heartbeat(_RunEvent):
    """Proof of life while a run is idle; it counts in ``seq`` like any event."""

    type: Literal["heartbeat"] = "heartbeat"


class Succeeded(_RunEvent):
    type: Literal["succeeded"] = "succeeded"
    text: str = Field(min_length=1)


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

_RunEventVariant = (
    RunStarted | AssistantText | ToolCall | ToolResult | Heartbeat | Succeeded | Failed | Cancelled
)

RunEvent = Annotated[_RunEventVariant, Field(discriminator="type")]

_RUN_EVENT_TYPES: Final = frozenset(get_args(_RunEventVariant))


def is_run_event(value: object) -> TypeGuard[RunEvent]:
    # The exact type, not isinstance: a subclass may add fields the closed
    # v1 union does not carry.
    return type(value) in _RUN_EVENT_TYPES


class RunRejection(_WireModel):
    """The HTTP body of a request the service refused."""

    code: Annotated[RejectionCode, Field(strict=False)]
    message: str = Field(max_length=MAX_MESSAGE_LENGTH)


class TurnRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class TurnMessage(_StrictModel):
    role: Annotated[TurnRole, Field(strict=False)]
    text: str


class Turn(_StrictModel):
    """The conversation a run answers: the system prompt and the messages so far.

    It names no provider, model, route, user or MCP endpoint: the service
    decides those from the run's ``purpose`` and the caller's identity.
    """

    system: str
    messages: list[TurnMessage] = Field(min_length=1)


class RunRequest(_WireModel):
    """The HTTP body a client sends to start a run."""

    purpose: str = Field(min_length=1)
    turn: Turn
