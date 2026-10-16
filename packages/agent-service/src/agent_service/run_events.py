"""One admitted run as the frames of its wire stream.

This module owns the mapping from the provider core's turn events
(``agent_providers.events``) onto the run events of ``agent_client.wire``. A
run opens with ``run_started`` and ends with exactly one terminal event.
Nothing reaches the wire that ``encode_event`` cannot write or that the
service's own ``RunStream`` refuses: the run ends as ``failed:
provider_error`` instead, before the offending event is sent. When frames go
out, and when a silent or overlong run is ended, ``run_liveness`` decides.

A tool call's input is deep-copied into its wire event, so the event never
shares a list or dict with the runner that built it, and its effect comes from
the app's tool list, where an unlisted tool counts as side-effecting.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Final
from uuid import UUID

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from agent_client.run_stream import RunStream
from agent_client.sse import EventTooLarge, encode_event
from agent_client.wire import (
    PROTOCOL_VERSION,
    AssistantText,
    Failed,
    FailureCode,
    Heartbeat,
    RunEvent,
    RunStarted,
    Succeeded,
    TerminalEvent,
    ToolCall,
    ToolResult,
)
from agent_providers.events import (
    AssistantTextEvent,
    FinalEvent,
    StreamEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_service.config import AppConfig, RunConfig
from agent_service.run_liveness import (
    RunClock,
    RunnerExhausted,
    RunnerOutcome,
    RunnerRaised,
    RunnerYielded,
    SupervisedRun,
)
from agent_service.runners import AdmittedRun, Runner

log = logging.getLogger(__name__)

NO_RUNNER_MESSAGE: Final = "No provider can serve this route yet."
RUNNER_FAILED_MESSAGE: Final = "The provider failed during the run."
NO_ANSWER_MESSAGE: Final = "The provider ended the run without an answer."
UNMAPPABLE_EVENT_MESSAGE: Final = "The provider sent an event the service cannot map."
INVALID_EVENT_MESSAGE: Final = "The provider sent an event that is not valid."


class _Unsendable(Exception):
    """A provider event the run cannot put on the wire; the message is the service's own."""


async def run_frames(
    admitted: AdmittedRun,
    runner: Runner | None,
    app: AppConfig,
    runs: RunConfig,
    clock: RunClock,
) -> AsyncIterator[bytes]:
    wire = _WireRun(admitted.run_id, app, runs)
    started = wire.started()
    started_at = clock()
    yield started
    if runner is None:
        yield wire.failed(FailureCode.PROVIDER_UNAVAILABLE, NO_RUNNER_MESSAGE)
        return
    events = runner.run(admitted)
    async with SupervisedRun(admitted.run_id, wire, events, runs, clock, started_at) as supervised:
        async with aclosing(supervised.frames()) as frames:
            async for frame in frames:
                yield frame


class _WireRun:
    def __init__(self, run_id: UUID, app: AppConfig, runs: RunConfig) -> None:
        self._run_id = run_id
        self._app = app
        self._runs = runs
        self._stream = RunStream()
        self._seq = 0
        self.ended = False

    def started(self) -> bytes:
        return self._sent(
            RunStarted(
                v=PROTOCOL_VERSION,
                run_id=self._run_id,
                seq=self._seq,
                heartbeat_interval_s=self._runs.heartbeat_interval_s,
            )
        )

    def heartbeat(self) -> bytes:
        return self._sent(Heartbeat(v=PROTOCOL_VERSION, run_id=self._run_id, seq=self._seq))

    def failed(self, code: FailureCode, message: str) -> bytes:
        return self._sent(
            Failed(
                v=PROTOCOL_VERSION, run_id=self._run_id, seq=self._seq, code=code, message=message
            )
        )

    def frame_of(self, outcome: RunnerOutcome) -> bytes | None:
        """The frame of the runner's outcome, the run's failure, or nothing for an empty text."""
        match outcome:
            case RunnerExhausted():
                return self.failed(FailureCode.PROVIDER_ERROR, NO_ANSWER_MESSAGE)
            case RunnerRaised(error=error):
                log.error("The runner of run %s failed", self._run_id, exc_info=error)
                return self.failed(FailureCode.PROVIDER_ERROR, RUNNER_FAILED_MESSAGE)
            case RunnerYielded(event=core_event):
                return self._frame_of_event(core_event)

    def _frame_of_event(self, core_event: StreamEvent) -> bytes | None:
        try:
            event = self._wire_event(core_event)
            return None if event is None else self._sent(event)
        except _Unsendable as unsendable:
            return self.failed(FailureCode.PROVIDER_ERROR, str(unsendable))

    def _wire_event(self, core_event: StreamEvent) -> RunEvent | None:
        try:
            return self._mapped(core_event)
        except ValidationError as error:
            raise _Unsendable(INVALID_EVENT_MESSAGE) from error

    def _mapped(self, core_event: StreamEvent) -> RunEvent | None:
        header = {"v": PROTOCOL_VERSION, "run_id": self._run_id, "seq": self._seq}
        match core_event:
            case AssistantTextEvent(text=""):
                return None
            case AssistantTextEvent(text=text):
                return AssistantText(**header, text=text)
            case ToolCallEvent():
                return self._tool_call(core_event, header)
            case ToolResultEvent(tool_use_id=tool_use_id, content=content, is_error=is_error):
                return ToolResult(
                    **header, tool_use_id=tool_use_id, content=content, is_error=is_error
                )
            case FinalEvent(text=text):
                return Succeeded(**header, text=text)
        raise _Unsendable(UNMAPPABLE_EVENT_MESSAGE)

    def _tool_call(self, core_event: ToolCallEvent, header: dict[str, object]) -> ToolCall:
        call = ToolCall(
            **header,
            tool_use_id=core_event.tool_use_id,
            name=core_event.name,
            input=core_event.input,
            effect=self._app.tool_effect(core_event.name),
        )
        return call.model_copy(update={"input": copy.deepcopy(call.input)})

    def _sent(self, event: RunEvent) -> bytes:
        try:
            frame = encode_event(event)
        except (EventTooLarge, PydanticSerializationError) as error:
            raise _Unsendable(f"The provider's {event.type} cannot be sent.") from error
        violation = self._stream.accept(event)
        if violation is not None:
            raise _Unsendable(f"The provider broke the run's stream rules: {violation.kind}.")
        self._seq += 1
        self.ended = isinstance(event, TerminalEvent)
        return frame
