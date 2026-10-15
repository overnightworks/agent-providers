"""The rules a run's event stream keeps, checked one event at a time.

``parse_event`` turns one decoded event into its wire type or names why it is
not one; ``RunStream`` holds what the events so far promise and names the first
event that breaks a rule. Both are pure: the transport that reads the stream
and decides what to do with a violation lives elsewhere.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from agent_client.tools import ToolEffect
from agent_client.wire import (
    PROTOCOL_VERSION,
    Cancelled,
    Failed,
    FailureCode,
    ProtocolVersion,
    RunEvent,
    RunStarted,
    TerminalEvent,
    ToolCall,
    ToolResult,
)

INTERRUPTED_MESSAGE: Final = "The run was cut short after a side-effecting tool call."
_SHOWN_VERSION_LENGTH: Final = 20

_RUN_EVENT: Final[TypeAdapter[RunEvent]] = TypeAdapter(RunEvent)
_PROTOCOL_VERSION: Final[TypeAdapter[ProtocolVersion]] = TypeAdapter(ProtocolVersion)
_UNKNOWN_TAG_ERRORS: Final = frozenset({"union_tag_invalid", "union_tag_not_found"})


class ViolationKind(StrEnum):
    """Which promise of the protocol an event broke."""

    UNKNOWN_TYPE = "unknown_type"
    UNSUPPORTED_VERSION = "unsupported_version"
    SCHEMA_MISMATCH = "schema_mismatch"
    MISSING_RUN_STARTED = "missing_run_started"
    RUN_ID_CHANGED = "run_id_changed"
    SEQUENCE_GAP = "sequence_gap"
    EVENT_AFTER_TERMINAL = "event_after_terminal"
    DUPLICATE_TOOL_USE_ID = "duplicate_tool_use_id"
    UNKNOWN_TOOL_USE_ID = "unknown_tool_use_id"
    CANCELLED_AFTER_SIDE_EFFECT = "cancelled_after_side_effect"
    TRUNCATED = "truncated"


class StreamViolation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ViolationKind
    detail: str


def parse_event(raw: dict[str, Any]) -> RunEvent | StreamViolation:
    """Read one decoded event, checking its version before its shape.

    The version comes first so a newer service's event is reported as a
    version the client does not speak, not as a malformed one.
    """
    version = raw.get("v")
    try:
        _PROTOCOL_VERSION.validate_python(version)
    except ValidationError:
        return StreamViolation(
            kind=ViolationKind.UNSUPPORTED_VERSION,
            detail=f"{_shown_version(raw)}, this client speaks {PROTOCOL_VERSION}",
        )
    try:
        return _RUN_EVENT.validate_python(raw)
    except ValidationError as error:
        return _violation_from(error)


def _shown_version(raw: dict[str, Any]) -> str:
    """Name an untrusted version briefly enough to log."""
    if "v" not in raw:
        return "no protocol version"
    return f"protocol version {repr(raw['v'])[:_SHOWN_VERSION_LENGTH]}"


def _violation_from(error: ValidationError) -> StreamViolation:
    problems = error.errors(include_input=False, include_url=False)
    kind = (
        ViolationKind.UNKNOWN_TYPE
        if any(problem["type"] in _UNKNOWN_TAG_ERRORS for problem in problems)
        else ViolationKind.SCHEMA_MISMATCH
    )
    detail = "; ".join(
        f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
        for problem in problems
    )
    return StreamViolation(kind=kind, detail=detail)


class RunStream:
    """What one run's events so far promise, and the first event that breaks it.

    Feed the events in arrival order. ``accept`` names the rule an event
    breaks and leaves the stream as it was, so the caller decides whether to
    stop. The protocol version needs no check here: an event of another
    version never parses.
    """

    def __init__(self) -> None:
        self._run_id: UUID | None = None
        self._next_seq = 0
        self._terminal: TerminalEvent | None = None
        self._tool_effects: dict[str, ToolEffect] = {}

    def accept(self, event: RunEvent) -> StreamViolation | None:
        violation = self._broken_rule(event)
        if violation is None:
            self._record(event)
        return violation

    def close(self) -> StreamViolation | None:
        """Report a stream that ended without its terminal event."""
        if self._terminal is not None:
            return None
        return StreamViolation(
            kind=ViolationKind.TRUNCATED,
            detail=f"the stream ended after {self._next_seq} events without a terminal event",
        )

    def cancellation_terminal(self) -> Failed | Cancelled:
        """The terminal event that ends this run if it is cancelled or cut off now.

        A side-effecting call counts from its emission, whether or not its
        result arrived, so the run may already have changed state.
        """
        if self._run_id is None:
            raise RuntimeError("a run that never started cannot be cancelled")
        if self._terminal is not None:
            raise RuntimeError(f"the run already ended as {self._terminal.type}")
        if self._side_effect_emitted:
            return Failed(
                v=PROTOCOL_VERSION,
                run_id=self._run_id,
                seq=self._next_seq,
                code=FailureCode.INTERRUPTED,
                message=INTERRUPTED_MESSAGE,
            )
        return Cancelled(v=PROTOCOL_VERSION, run_id=self._run_id, seq=self._next_seq)

    @property
    def _side_effect_emitted(self) -> bool:
        return ToolEffect.SIDE_EFFECTING in self._tool_effects.values()

    def _broken_rule(self, event: RunEvent) -> StreamViolation | None:
        if self._terminal is not None:
            return StreamViolation(
                kind=ViolationKind.EVENT_AFTER_TERMINAL,
                detail=f"{event.type} after the terminal {self._terminal.type}",
            )
        if self._run_id is None and not isinstance(event, RunStarted):
            return StreamViolation(
                kind=ViolationKind.MISSING_RUN_STARTED,
                detail=f"the stream opened with {event.type}",
            )
        if self._run_id is not None and event.run_id != self._run_id:
            return StreamViolation(
                kind=ViolationKind.RUN_ID_CHANGED,
                detail=f"run {event.run_id} inside run {self._run_id}",
            )
        if event.seq != self._next_seq:
            return StreamViolation(
                kind=ViolationKind.SEQUENCE_GAP,
                detail=f"seq {event.seq} where {self._next_seq} was due",
            )
        return self._broken_tool_rule(event)

    def _broken_tool_rule(self, event: RunEvent) -> StreamViolation | None:
        if isinstance(event, ToolCall) and event.tool_use_id in self._tool_effects:
            return StreamViolation(
                kind=ViolationKind.DUPLICATE_TOOL_USE_ID,
                detail=f"a second call {event.tool_use_id!r}",
            )
        if isinstance(event, ToolResult) and event.tool_use_id not in self._tool_effects:
            return StreamViolation(
                kind=ViolationKind.UNKNOWN_TOOL_USE_ID,
                detail=f"a result for no call {event.tool_use_id!r}",
            )
        if isinstance(event, Cancelled) and self._side_effect_emitted:
            return StreamViolation(
                kind=ViolationKind.CANCELLED_AFTER_SIDE_EFFECT,
                detail="cancelled after a side-effecting call; the run must fail as interrupted",
            )
        return None

    def _record(self, event: RunEvent) -> None:
        self._run_id = event.run_id
        self._next_seq += 1
        if isinstance(event, ToolCall):
            self._tool_effects[event.tool_use_id] = event.effect
        if isinstance(event, TerminalEvent):
            self._terminal = event
