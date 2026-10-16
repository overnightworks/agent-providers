"""One admitted run as the frames of its wire stream.

This module owns the mapping from the provider core's turn events
(``agent_providers.events``) onto the run events of ``agent_client.wire``. A
run opens with ``run_started`` and ends with exactly one terminal event, except
the cut stream of a dropped client (decision record 0001, H6).
Nothing reaches the wire that ``encode_event`` cannot write or that the
service's own ``RunStream`` refuses: the run ends as ``failed:
provider_error`` instead, before the offending event is sent. The run's
budget of tool calls and bytes (``RunConfig``) is charged at the same place:
the event that passes it is not sent and the run ends as ``failed:
limit_exceeded``. Each accepted event is also recorded
(``agent_service.run_records``), and the run is recorded over once its runner is
closed. When frames go out, and when a silent or overlong run is
ended, ``run_liveness`` decides.

A tool call's input is deep-copied into its wire event, so the event never
shares a list or dict with the runner that built it, and its effect comes from
the app's tool list, where an unlisted tool counts as side-effecting.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing, contextmanager
from typing import Final

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from agent_client.run_stream import RunStream
from agent_client.sse import EventTooLarge, encode_event
from agent_client.wire import (
    PROTOCOL_VERSION,
    AssistantText,
    ClientLoss,
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
from agent_service.run_records import RunRecorder, RunRecords
from agent_service.runners import AdmittedRun, Runner

log = logging.getLogger(__name__)

NO_RUNNER_MESSAGE: Final = "No provider can serve this route yet."
RUNNER_FAILED_MESSAGE: Final = "The provider failed during the run."
NO_ANSWER_MESSAGE: Final = "The provider ended the run without an answer."
UNMAPPABLE_EVENT_MESSAGE: Final = "The provider sent an event the service cannot map."
INVALID_EVENT_MESSAGE: Final = "The provider sent an event that is not valid."
TOO_MANY_TOOL_CALLS_MESSAGE: Final = "The run made too many tool calls."
TOO_MUCH_DATA_MESSAGE: Final = "The run sent too much data."
_SERVICE_WRITTEN: Final = (RunStarted, Heartbeat, Failed)


class _Unsendable(Exception):
    """A provider event the run cannot put on the wire; the message is the service's own."""


class _OverBudget(Exception):
    """A provider event past the run's budget; the message is the service's own."""


async def run_frames(
    admitted: AdmittedRun,
    runner: Runner | None,
    app: AppConfig,
    runs: RunConfig,
    clock: RunClock,
    records: RunRecords,
) -> AsyncGenerator[bytes, None]:
    wire = _WireRun(admitted, app, runs, records)
    started = wire.started()
    started_at = clock()
    if runner is None:
        failed = wire.failed(FailureCode.PROVIDER_UNAVAILABLE, NO_RUNNER_MESSAGE)
        wire.run_over()
        with _client_lost_when_closed(wire):
            yield started
            yield failed
        return
    events = runner.run(admitted)
    async with (
        SupervisedRun(admitted.run_id, wire, events, runs, clock, started_at) as supervised,
        aclosing(supervised.frames()) as frames,
    ):
        with _client_lost_when_closed(wire):
            yield started
            async for frame in frames:
                yield frame


@contextmanager
def _client_lost_when_closed(wire: _WireRun) -> Iterator[None]:
    """Latch the loss before any cleanup runs, when the response cancels or closes the stream."""
    try:
        yield
    except (asyncio.CancelledError, GeneratorExit):
        wire.client_lost(ClientLoss.DISCONNECTED)
        raise


class _WireRun:
    def __init__(
        self, admitted: AdmittedRun, app: AppConfig, runs: RunConfig, records: RunRecords
    ) -> None:
        self._admitted = admitted
        self._run_id = admitted.run_id
        self._app = app
        self._runs = runs
        self._records = records
        self._recorder: RunRecorder | None = None
        self._stream = RunStream()
        self._seq = 0
        self._tool_calls = 0
        self._bytes = 0
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

    def run_over(self) -> None:
        self._recorder_of_started_run().over(
            None if self.ended else self._stream.cancellation_terminal()
        )

    def client_lost(self, loss: ClientLoss) -> None:
        self._recorder_of_started_run().lost(loss)

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
        except _OverBudget as over_budget:
            return self.failed(FailureCode.LIMIT_EXCEEDED, str(over_budget))

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
        self._charge(event, len(frame))
        violation = self._stream.accept(event)
        if violation is not None:
            raise _Unsendable(f"The provider broke the run's stream rules: {violation.kind}.")
        self._seq += 1
        self.ended = isinstance(event, TerminalEvent)
        self._record(event)
        return frame

    def _record(self, event: RunEvent) -> None:
        if isinstance(event, RunStarted):
            self._recorder = self._records.begin(
                event, self._admitted.app, self._admitted.principal
            )
        else:
            self._recorder_of_started_run().accepted(event)

    def _recorder_of_started_run(self) -> RunRecorder:
        assert self._recorder is not None
        return self._recorder

    def _charge(self, event: RunEvent, frame_bytes: int) -> None:
        """Count a runner-caused frame against the budget.

        An event the budget refuses is charged nothing.
        """
        if isinstance(event, _SERVICE_WRITTEN):
            return
        is_call = isinstance(event, ToolCall)
        if is_call and self._tool_calls >= self._runs.max_tool_calls:
            raise _OverBudget(TOO_MANY_TOOL_CALLS_MESSAGE)
        if self._bytes + frame_bytes > self._runs.max_bytes:
            raise _OverBudget(TOO_MUCH_DATA_MESSAGE)
        self._tool_calls += is_call
        self._bytes += frame_bytes
