"""One admitted run as the frames of its wire stream.

This module owns the mapping from the provider core's turn events
(``agent_providers.events``) onto the run events of ``agent_client.wire``. A
run opens with ``run_started`` and ends with exactly one terminal event.
Nothing reaches the wire that ``encode_event`` cannot write or that the
service's own ``RunStream`` refuses: the run ends as ``failed:
provider_error`` instead, before the offending event is sent.

A tool call's input is deep-copied into its wire event, so the event never
shares a list or dict with the runner that built it, and its effect comes from
the app's tool list, where an unlisted tool counts as side-effecting.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import AsyncGenerator, AsyncIterator
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
from agent_service.config import AppConfig
from agent_service.runners import AdmittedRun, Runner

log = logging.getLogger(__name__)

# The heartbeat itself arrives with the run's liveness limits (slice A1a3).
HEARTBEAT_INTERVAL_S: Final = 15

NO_RUNNER_MESSAGE: Final = "No provider can serve this route yet."
RUNNER_FAILED_MESSAGE: Final = "The provider failed during the run."
NO_ANSWER_MESSAGE: Final = "The provider ended the run without an answer."


class _Unsendable(Exception):
    """A provider event the run cannot put on the wire; the message is the service's own."""


async def run_frames(
    admitted: AdmittedRun, runner: Runner | None, app: AppConfig
) -> AsyncIterator[bytes]:
    wire = _WireRun(admitted.run_id, app)
    yield wire.started()
    if runner is None:
        yield wire.failed(FailureCode.PROVIDER_UNAVAILABLE, NO_RUNNER_MESSAGE)
        return
    async with aclosing(runner.run(admitted)) as events:
        while not wire.ended:
            frame = await wire.next_frame(events)
            if frame is not None:
                yield frame


class _WireRun:
    def __init__(self, run_id: UUID, app: AppConfig) -> None:
        self._run_id = run_id
        self._app = app
        self._stream = RunStream()
        self._seq = 0
        self.ended = False

    def started(self) -> bytes:
        return self._sent(
            RunStarted(
                v=PROTOCOL_VERSION,
                run_id=self._run_id,
                seq=self._seq,
                heartbeat_interval_s=HEARTBEAT_INTERVAL_S,
            )
        )

    def failed(self, code: FailureCode, message: str) -> bytes:
        return self._sent(
            Failed(
                v=PROTOCOL_VERSION, run_id=self._run_id, seq=self._seq, code=code, message=message
            )
        )

    async def next_frame(self, events: AsyncGenerator[StreamEvent, None]) -> bytes | None:
        """The frame of the runner's next event, the run's failure, or nothing for an empty text."""
        try:
            core_event = await anext(events)
        except StopAsyncIteration:
            return self.failed(FailureCode.PROVIDER_ERROR, NO_ANSWER_MESSAGE)
        except Exception:
            log.exception("The runner of run %s failed", self._run_id)
            return self.failed(FailureCode.PROVIDER_ERROR, RUNNER_FAILED_MESSAGE)
        try:
            event = self._wire_event(core_event)
            return None if event is None else self._sent(event)
        except _Unsendable as unsendable:
            return self.failed(FailureCode.PROVIDER_ERROR, str(unsendable))

    def _wire_event(self, core_event: StreamEvent) -> RunEvent | None:
        try:
            return self._mapped(core_event)
        except ValidationError as error:
            raise _Unsendable(f"The provider's {core_event.type} is not a valid event.") from error

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
        raise _Unsendable(f"The provider sent a {core_event.type} event the service cannot map.")

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
