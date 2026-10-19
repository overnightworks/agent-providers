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

A run's terminal is decided when its ending happens but encoded, charged,
accepted and recorded only once, by ``commit_terminal`` after the runner's close,
and the record ends in ``run_over``; a containment loss the runner's pump met
turns that terminal, or the fallback of a cut stream, into ``failed:
provider_error``, and passes the run's place to the quarantine
(``agent_service.quarantine``) before the record ends. A terminal whose commit
failed ends the record ``failed: provider_error`` all the same.

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
from dataclasses import dataclass
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
    ToolCall,
    ToolResult,
)
from agent_providers.errors import RetainedGroup
from agent_providers.events import (
    AssistantTextEvent,
    FinalEvent,
    StreamEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_service.admission import Lease
from agent_service.config import AppConfig, RunConfig
from agent_service.quarantine import Quarantine
from agent_service.run_liveness import (
    FailedPart,
    RunClock,
    RunFailures,
    RunnerExhausted,
    RunnerOutcome,
    RunnerRaised,
    RunnerYielded,
    SupervisedRun,
    log_failure,
    reported_by_the_quarantine,
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


@dataclass(frozen=True)
class _DecidedFailure:
    code: FailureCode
    message: str


def _failure_of(refusal: _Unsendable | _OverBudget) -> _DecidedFailure:
    code = (
        FailureCode.LIMIT_EXCEEDED
        if isinstance(refusal, _OverBudget)
        else FailureCode.PROVIDER_ERROR
    )
    return _DecidedFailure(code, str(refusal))


_CONTAINMENT_LOST: Final = _DecidedFailure(FailureCode.PROVIDER_ERROR, RUNNER_FAILED_MESSAGE)
_COMMIT_FAILED: Final = _DecidedFailure(FailureCode.PROVIDER_ERROR, RUNNER_FAILED_MESSAGE)


async def run_frames(
    admitted: AdmittedRun,
    runner: Runner | None,
    app: AppConfig,
    runs: RunConfig,
    clock: RunClock,
    records: RunRecords,
    lease: Lease | None = None,
    *,
    quarantine: Quarantine,
) -> AsyncGenerator[bytes, None]:
    wire = _WireRun(admitted, app, runs, records, lease, quarantine)
    failures = RunFailures(admitted.run_id)
    started = wire.started()
    started_at = clock()
    if runner is None:
        wire.decide_failure(FailureCode.PROVIDER_UNAVAILABLE, NO_RUNNER_MESSAGE)
        with failures.leaving():
            failures.attempt(FailedPart.CLEANUP, lambda: wire.commit_terminal(()))
            failures.attempt(FailedPart.CLEANUP, lambda: wire.run_over(()))
            if failures.met_any:
                return
            with _client_lost_when_closed(wire, failures):
                yield started
                yield wire.terminal_frame()
        return
    events = runner.run(admitted)
    async with (
        SupervisedRun(admitted.run_id, wire, events, runs, clock, started_at, failures) as run,
        aclosing(run.frames()) as frames,
    ):
        with _client_lost_when_closed(wire, failures):
            yield started
            async for frame in frames:
                yield frame


@contextmanager
def _client_lost_when_closed(wire: _WireRun, failures: RunFailures) -> Iterator[None]:
    """Latch the loss before any cleanup runs, when the response cancels or closes the stream.

    A failed report is the cleanup's failure, recorded for the run's exit to decide.
    """
    try:
        yield
    except (asyncio.CancelledError, GeneratorExit):
        failures.attempt(FailedPart.CLEANUP, lambda: wire.client_lost(ClientLoss.DISCONNECTED))
        raise


class _WireRun:
    def __init__(
        self,
        admitted: AdmittedRun,
        app: AppConfig,
        runs: RunConfig,
        records: RunRecords,
        lease: Lease | None,
        quarantine: Quarantine,
    ) -> None:
        self._admitted = admitted
        self._run_id = admitted.run_id
        self._app = app
        self._runs = runs
        self._records = records
        self._lease = lease
        self._quarantine = quarantine
        self._recorder: RunRecorder | None = None
        self._stream = RunStream()
        self._seq = 0
        self._tool_calls = 0
        self._bytes = 0
        self._decided: Succeeded | _DecidedFailure | None = None
        self._terminal_frame: bytes | None = None

    @property
    def ended(self) -> bool:
        return self._decided is not None

    def started(self) -> bytes:
        return self._sent(
            RunStarted(
                v=PROTOCOL_VERSION,
                run_id=self._run_id,
                seq=self._seq,
                heartbeat_interval_s=self._runs.heartbeat_interval_s,
            )
        )

    def hand_over(self, losses: tuple[RetainedGroup, ...]) -> None:
        """Pass the run's place to the quarantine when the runner's pump met a containment loss."""
        if losses:
            self._quarantine.hold(self._run_id, self._lease, losses)

    def commit_terminal(self, losses: tuple[RetainedGroup, ...]) -> None:
        """Commit the decided terminal; a loss turns it into ``failed: provider_error``."""
        if self._decided is not None:
            self._terminal_frame = self._committed_terminal(
                _CONTAINMENT_LOST if losses else self._decided
            )

    def run_over(self, losses: tuple[RetainedGroup, ...]) -> None:
        """End the record and release the lease; only a committed terminal needs no fallback."""
        try:
            recorder = self._recorder_of_started_run()
            if self._terminal_frame is not None:
                recorder.over()
            elif self.ended or losses:
                recorder.over(self._failed_event(_COMMIT_FAILED))
            else:
                recorder.over(self._stream.cancellation_terminal())
        finally:
            if self._lease is not None:
                self._lease.release()

    def terminal_frame(self) -> bytes:
        assert self._terminal_frame is not None
        return self._terminal_frame

    def _committed_terminal(self, decided: Succeeded | _DecidedFailure) -> bytes:
        if isinstance(decided, _DecidedFailure):
            return self._sent(self._failed_event(decided))
        try:
            return self._sent(decided)
        except (_Unsendable, _OverBudget) as refusal:
            return self._sent(self._failed_event(_failure_of(refusal)))

    def client_lost(self, loss: ClientLoss) -> None:
        self._recorder_of_started_run().lost(loss)

    def heartbeat(self) -> bytes:
        return self._sent(Heartbeat(v=PROTOCOL_VERSION, run_id=self._run_id, seq=self._seq))

    def decide_failure(self, code: FailureCode, message: str) -> None:
        self._decided = _DecidedFailure(code, message)

    def _failed_event(self, failure: _DecidedFailure) -> Failed:
        return Failed(
            v=PROTOCOL_VERSION,
            run_id=self._run_id,
            seq=self._seq,
            code=failure.code,
            message=failure.message,
        )

    def frame_of(self, outcome: RunnerOutcome) -> bytes | None:
        """The frame of an outcome sent at once; nothing for an empty text or a decided ending."""
        match outcome:
            case RunnerExhausted():
                self.decide_failure(FailureCode.PROVIDER_ERROR, NO_ANSWER_MESSAGE)
            case RunnerRaised(error=error):
                if not reported_by_the_quarantine(error):
                    log_failure(log, f"The runner of run {self._run_id} failed", error)
                self.decide_failure(FailureCode.PROVIDER_ERROR, RUNNER_FAILED_MESSAGE)
            case RunnerYielded(event=FinalEvent() as final):
                self._decide_success(final)
            case RunnerYielded(event=core_event):
                return self._frame_of_event(core_event)
        return None

    def _decide_success(self, final: FinalEvent) -> None:
        try:
            succeeded = self._wire_event(final)
        except _Unsendable as refusal:
            self._decided = _failure_of(refusal)
        else:
            assert isinstance(succeeded, Succeeded)
            self._decided = succeeded

    def _frame_of_event(self, core_event: StreamEvent) -> bytes | None:
        try:
            event = self._wire_event(core_event)
            return None if event is None else self._sent(event)
        except (_Unsendable, _OverBudget) as refusal:
            self._decided = _failure_of(refusal)
            return None

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
