"""What the service remembers of a run, for how long, and who may ask (decision record 0001, H6).

A record holds the run's owner, its two instants, how many events the service
accepted, its ending and whether the client was lost: no event text, no tool
input or result, no failure message, no token and no group. A running record
never expires. An ended record lives until its retention has passed or, once
``max_ended`` ended records are held, until it is the oldest of them; both
removals happen first in every store operation, so nothing needs a timer. The
store is used on the event loop only, and nothing is persisted.
"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from agent_client.wire import (
    PROTOCOL_VERSION,
    Cancelled,
    ClientLoss,
    Failed,
    FailureCode,
    RunEvent,
    RunStarted,
    RunState,
    RunStatus,
    Succeeded,
    TerminalEvent,
)
from agent_service.identity import Principal

RECORD_RETENTION: Final = timedelta(hours=1)
MAX_ENDED_RECORDS: Final = 10_000


@dataclass(frozen=True)
class _Owner:
    app: str
    issuer: str
    subject: str


_NO_OWNER: Final = _Owner(app="", issuer="", subject="")
"""Stands in for the owner of an id nobody began; no caller carries an empty app or subject."""


@dataclass(frozen=True)
class _Ending:
    state: RunState
    failure_code: FailureCode | None


@dataclass
class _Record:
    run_id: UUID
    owner: _Owner
    started_at: datetime
    events: int = 1
    pending_ending: _Ending | None = None
    ending: _Ending | None = None
    ended_at: datetime | None = None
    client_lost: ClientLoss | None = None


def _ending_of(event: TerminalEvent) -> _Ending:
    match event:
        case Succeeded():
            return _Ending(RunState.SUCCEEDED, None)
        case Failed(code=code):
            return _Ending(RunState.FAILED, code)
        case Cancelled():
            return _Ending(RunState.CANCELLED, None)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RunRecords:
    """The runs of one service process, begun by ``begin`` and read by their owner."""

    def __init__(
        self,
        now: Callable[[], datetime] = _utc_now,
        *,
        retention: timedelta = RECORD_RETENTION,
        max_ended: int = MAX_ENDED_RECORDS,
    ) -> None:
        self._now = now
        self._retention = retention
        self._max_ended = max_ended
        self._records: dict[UUID, _Record] = {}
        self._ended: list[tuple[datetime, int, UUID]] = []
        self._completions = itertools.count()

    def begin(self, started: RunStarted, app: str, principal: Principal) -> RunRecorder:
        moment = self._now()
        self._purge(moment)
        if started.run_id in self._records:
            raise ValueError(f"run {started.run_id} is already recorded")
        record = _Record(
            run_id=started.run_id,
            owner=_Owner(app, principal.issuer, principal.subject),
            started_at=moment,
        )
        self._records[started.run_id] = record
        return RunRecorder(self, record)

    def read(self, run_id: UUID, app: str, principal: Principal) -> RunStatus | None:
        self._purge(self._now())
        record = self._records.get(run_id)
        owner = _NO_OWNER if record is None else record.owner
        matches = [
            owner.app == app,
            owner.issuer == principal.issuer,
            owner.subject == principal.subject,
        ]
        if record is None or not all(matches):
            return None
        return _status_of(record)

    def _accept(self, record: _Record, event: RunEvent) -> None:
        self._purge(self._now())
        if isinstance(event, RunStarted):
            raise ValueError("only begin counts the opening event")
        if record.ending is not None:
            raise RuntimeError(f"run {record.run_id} is already over")
        record.events += 1
        if isinstance(event, TerminalEvent):
            record.pending_ending = _ending_of(event)

    def _finish(self, record: _Record, fallback: Failed | Cancelled | None) -> None:
        moment = self._now()
        self._purge(moment)
        if record.ending is not None:
            raise RuntimeError(f"run {record.run_id} is already over")
        if record.pending_ending is not None:
            record.ending = record.pending_ending
        elif fallback is not None:
            record.ending = _ending_of(fallback)
        else:
            raise RuntimeError(f"run {record.run_id} has no ending and no fallback")
        record.ended_at = moment
        heapq.heappush(self._ended, (moment, next(self._completions), record.run_id))
        while len(self._ended) > self._max_ended:
            self._forget_first_ended()

    def _lose(self, record: _Record, reason: ClientLoss) -> None:
        self._purge(self._now())
        if self._records.get(record.run_id) is record and record.client_lost is None:
            record.client_lost = reason

    def _purge(self, moment: datetime) -> None:
        while self._ended and moment - self._ended[0][0] >= self._retention:
            self._forget_first_ended()

    def _forget_first_ended(self) -> None:
        _, _, run_id = heapq.heappop(self._ended)
        del self._records[run_id]


class RunRecorder:
    """The writer of one run's record: the supervisor of the run feeds it what it accepts."""

    def __init__(self, records: RunRecords, record: _Record) -> None:
        self._records = records
        self._record = record

    def accepted(self, event: RunEvent) -> None:
        self._records._accept(self._record, event)

    def over(self, fallback: Failed | Cancelled | None = None) -> None:
        self._records._finish(self._record, fallback)

    def lost(self, reason: ClientLoss) -> None:
        self._records._lose(self._record, reason)


def _status_of(record: _Record) -> RunStatus:
    ending = record.ending or _Ending(RunState.RUNNING, None)
    return RunStatus(
        v=PROTOCOL_VERSION,
        run_id=record.run_id,
        state=ending.state,
        failure_code=ending.failure_code,
        events=record.events,
        started_at=record.started_at,
        ended_at=record.ended_at,
        client_lost=record.client_lost,
    )
