"""The run contract between the agent service and its clients.

This module is the one owner of what travels between a client and the
service (decision record 0001, H6): the request that starts a run, the run's
event stream as a closed union discriminated on ``type`` with every event
carrying the protocol version, the run id and its sequence number, and the
typed body the service answers with when it refuses a request, and the status
body it answers ``GET /runs/{id}`` with.
A new variant or a new field is a new ``PROTOCOL_VERSION``.

Validation is strict, so the wire's JSON types are the contract: a sequence
number sent as ``"3"`` is a mismatch, not a coercion. Only the run id and the
closed codes and roles are read from their JSON strings. The version and sequence
number have no defaults.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self, TypeGuard, get_args
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from agent_client.tools import ToolEffect

PROTOCOL_VERSION: Final = 2
MAX_EVENT_BYTES: Final = 1_048_576
MAX_TOOL_INPUT_DEPTH: Final = 128
MAX_MESSAGE_LENGTH: Final = 500
MAX_PURPOSE_LENGTH: Final = 64
_PURPOSE_PATTERN: Final = re.compile(rf"[a-z][a-z0-9-]{{0,{MAX_PURPOSE_LENGTH - 1}}}")


class FailureCode(StrEnum):
    """Why a run ended as ``failed``."""

    INTERRUPTED = "interrupted"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_ERROR = "provider_error"
    LIMIT_EXCEEDED = "limit_exceeded"
    IDLE_TIMEOUT = "idle_timeout"
    INTERNAL_ERROR = "internal_error"


class RunState(StrEnum):
    """Where a run stands: still running, or how it ended."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ClientLoss(StrEnum):
    """How the service lost the client of a run: it fell behind, or it went away."""

    DROPPED = "dropped"
    DISCONNECTED = "disconnected"


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
    CAPABILITY_TOO_LARGE = "capability_too_large"


def _json_integer(value: object) -> object:
    # A Literal ignores strict mode and matches by equality, so without this
    # JSON ``true`` and ``1.0`` would pass as 1 and ``false`` as 0.
    if type(value) is not int:
        raise ValueError("must be a JSON integer")
    return value


ProtocolVersion = Annotated[Literal[2], BeforeValidator(_json_integer)]
"""The one decider of whether a ``v`` is the version this package speaks."""


def refuse_non_json_constant(constant: str) -> object:
    """The one decider of NaN and Infinity in text being parsed: JSON has no word for them.

    Passed as ``parse_constant`` by every reader of untrusted JSON text: the SSE
    frame decoder, the text tool-call protocol and the service's request body.
    """
    raise ValueError(f"{constant} is not a JSON value")


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


def _refuse_non_unicode_text(text: str) -> str:
    _refuse_lone_surrogates(text)
    return text


UnicodeText = Annotated[str, AfterValidator(_refuse_non_unicode_text)]
"""The one type of wire text: valid Unicode, so the body or frame it lands in encodes as JSON."""

ToolUseId = Annotated[UnicodeText, Field(min_length=1)]
"""The one decider of a valid call id, so a call and its result accept the same ids."""


def _refuse_non_purpose(text: str) -> str:
    if _PURPOSE_PATTERN.fullmatch(text) is None:
        raise ValueError(
            f"must be 1 to {MAX_PURPOSE_LENGTH} characters from a-z, 0-9 and -, "
            "starting with a letter"
        )
    return text


Purpose = Annotated[str, AfterValidator(_refuse_non_purpose)]
"""The one decider of an agent's name: the purpose a run is started for."""

AgentRole = Literal["admin", "user"]
"""The two roles an app gives a person; the host's own role names map onto these."""


def _refuse_non_agent_roles(roles: list[AgentRole]) -> list[AgentRole]:
    if roles not in (["admin"], ["admin", "user"]):
        raise ValueError("must be exactly [admin] or [admin, user]")
    return roles


def _require_list(roles: object) -> object:
    if not isinstance(roles, list):
        raise ValueError("must be a list")
    return roles


AgentRoles = Annotated[
    list[AgentRole], BeforeValidator(_require_list), AfterValidator(_refuse_non_agent_roles)
]
"""The one decider of which roles an agent is offered to: admin alone, or admin and user."""


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
    text: Annotated[UnicodeText, Field(min_length=1)]


class ToolCall(_RunEvent):
    """A tool the model asked for, emitted before the tool runs."""

    type: Literal["tool_call"] = "tool_call"
    tool_use_id: ToolUseId
    name: UnicodeText
    input: JsonObject
    effect: Annotated[ToolEffect, Field(strict=False)]


class ToolResult(_RunEvent):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: ToolUseId
    content: UnicodeText
    is_error: bool


class Heartbeat(_RunEvent):
    """Proof of life while a run is idle; it counts in ``seq`` like any event."""

    type: Literal["heartbeat"] = "heartbeat"


class Succeeded(_RunEvent):
    type: Literal["succeeded"] = "succeeded"
    text: Annotated[UnicodeText, Field(min_length=1)]


class Failed(_RunEvent):
    """A run that ended without success.

    ``message`` is written by the service, never a provider's raw output.
    """

    type: Literal["failed"] = "failed"
    code: Annotated[FailureCode, Field(strict=False)]
    message: Annotated[UnicodeText, Field(max_length=MAX_MESSAGE_LENGTH)]


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
    message: Annotated[UnicodeText, Field(max_length=MAX_MESSAGE_LENGTH)]


def _refuse_non_utc_instant(instant: AwareDatetime) -> AwareDatetime:
    if instant.utcoffset() != timedelta(0):
        raise ValueError("must be a UTC instant")
    return instant


UtcInstant = Annotated[AwareDatetime, AfterValidator(_refuse_non_utc_instant)]
"""The one decider of a wire timestamp: an aware instant written in UTC."""


class RunStatus(_WireModel):
    """The HTTP body of ``GET /runs/{id}``: where a run stands.

    ``events`` counts what the service accepted, not what the client received.
    ``ended_at`` may precede ``started_at`` when the service's clock stepped back.
    """

    run_id: Annotated[UUID, Field(strict=False)]
    state: Annotated[RunState, Field(strict=False)]
    failure_code: Annotated[FailureCode | None, Field(strict=False)]
    events: int = Field(ge=1)
    started_at: UtcInstant
    ended_at: UtcInstant | None
    client_lost: Annotated[ClientLoss | None, Field(strict=False)]

    @model_validator(mode="after")
    def _ending_matches_state(self) -> Self:
        if (self.failure_code is not None) != (self.state is RunState.FAILED):
            raise ValueError("failure_code is set exactly when the run failed")
        if (self.ended_at is None) != (self.state is RunState.RUNNING):
            raise ValueError("ended_at is set exactly when the run is over")
        return self


ToolName = Annotated[UnicodeText, Field(min_length=1)]
"""The one decider of a tool's name on the wire, the type of a capability's ``tools`` entries."""


class TurnRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class TurnMessage(_StrictModel):
    role: Annotated[TurnRole, Field(strict=False)]
    text: UnicodeText


class Turn(_StrictModel):
    """The conversation a run answers: the system prompt and the messages so far.

    It names no provider, model, route, user or MCP endpoint: the service
    decides those from the caller's identity and the app's registration.
    """

    system: UnicodeText
    messages: list[TurnMessage] = Field(min_length=1)


class RunRequest(_WireModel):
    """The HTTP body a client sends to start a run.

    ``tools`` maps each tool the run may call to its effect. The service signs
    exactly those names into the run's capability and stamps the effects onto
    the run's tool calls; it grants nothing else.
    """

    purpose: Purpose
    turn: Turn
    tools: dict[ToolName, Annotated[ToolEffect, Field(strict=False)]]
