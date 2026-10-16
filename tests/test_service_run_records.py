"""The run-record store: what a record holds, how long it lives, and who may read it.

Decision record 0001, A1a2 ruling, items 1 to 4. The store is driven through
its writer and reader API with a wall clock the test moves by hand.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import tracemalloc
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from service_support import WallClock, instant, reaches_text
from wire_builders import (
    RUN_ID,
    call,
    cancelled,
    heartbeat,
    interrupted,
    result,
    started,
    succeeded,
)

from agent_client.wire import (
    PROTOCOL_VERSION,
    AssistantText,
    ClientLoss,
    Failed,
    FailureCode,
    RunState,
    RunStatus,
)
from agent_service.identity import Principal
from agent_service.run_records import RunRecorder, RunRecords

ISSUER = "https://keycloak.test/realms/agent-test"
ALICE = Principal(ISSUER, "user-alice")
APP = "songmaker"
MEMORY_GROWTH_LIMIT_BYTES = 64 * 1024


@pytest.fixture
def clock() -> WallClock:
    return WallClock()


@pytest.fixture
def records(clock: WallClock) -> RunRecords:
    return RunRecords(clock)


def _begin(records: RunRecords, run_id: UUID | None = None) -> tuple[UUID, RunRecorder]:
    run_id = run_id or uuid4()
    return run_id, records.begin(started(run_id), APP, ALICE)


def _read(records: RunRecords, run_id: UUID) -> RunStatus | None:
    return records.read(run_id, APP, ALICE)


def _end_at(clock: WallClock, recorder: RunRecorder, seconds: float) -> None:
    clock.set_to(instant(seconds))
    recorder.over(cancelled(1))


def _ended_run_at(records: RunRecords, clock: WallClock, seconds: float) -> UUID:
    run_id, recorder = _begin(records)
    _end_at(clock, recorder, seconds)
    return run_id


def _is_readable(records: RunRecords, run_id: UUID) -> bool:
    return _read(records, run_id) is not None


def test_a_begun_run_reads_as_running_with_its_opening_event_counted(
    records: RunRecords,
) -> None:
    run_id, _ = _begin(records)

    status = _read(records, run_id)

    assert status == RunStatus(
        v=PROTOCOL_VERSION,
        run_id=run_id,
        state=RunState.RUNNING,
        failure_code=None,
        events=1,
        started_at=instant(0),
        ended_at=None,
        client_lost=None,
    )


@pytest.mark.parametrize(
    ("ended_at", "read_at", "readable"),
    [
        pytest.param(1000, 4599.999, True, id="one millisecond before the hour is out"),
        pytest.param(1000, 4600, False, id="when the hour is out"),
        pytest.param(600, 4199.999, True, id="expiry counts from the end, not the start"),
        pytest.param(600, 4200, False, id="a run begun at 0 and ended at 600 is gone at 4200"),
    ],
)
def test_an_ended_record_lives_one_hour_from_its_end(
    records: RunRecords, clock: WallClock, ended_at: float, read_at: float, readable: bool
) -> None:
    run_id = _ended_run_at(records, clock, ended_at)

    clock.set_to(instant(read_at))

    assert _is_readable(records, run_id) is readable


def test_a_running_record_is_never_expired(records: RunRecords, clock: WallClock) -> None:
    run_id, _ = _begin(records)

    clock.set_to(instant(36000))

    status = _read(records, run_id)
    assert status is not None
    assert status.state is RunState.RUNNING


def test_a_bound_evicts_the_oldest_ended_record_when_a_fourth_ends(clock: WallClock) -> None:
    records = RunRecords(clock, max_ended=3)

    run_ids = [_ended_run_at(records, clock, seconds) for seconds in (10, 20, 30, 40)]

    assert [_is_readable(records, run_id) for run_id in run_ids] == [False, True, True, True]


def test_running_records_do_not_count_against_the_bound(clock: WallClock) -> None:
    records = RunRecords(clock, max_ended=3)
    running_id, _ = _begin(records)

    ended_ids = [_ended_run_at(records, clock, seconds) for seconds in (10, 20, 30, 40)]

    assert _is_readable(records, running_id)
    assert [_is_readable(records, run_id) for run_id in ended_ids] == [False, True, True, True]


def test_a_backwards_clock_step_cannot_hide_an_expired_record_behind_a_live_one(
    records: RunRecords, clock: WallClock
) -> None:
    first = _ended_run_at(records, clock, 1000)
    second = _ended_run_at(records, clock, 900)

    clock.set_to(instant(4500))

    assert not _is_readable(records, second)
    assert _is_readable(records, first)


def test_a_backwards_clock_step_evicts_the_record_that_ended_earliest_by_time(
    clock: WallClock,
) -> None:
    records = RunRecords(clock, max_ended=2)
    first = _ended_run_at(records, clock, 1000)
    second = _ended_run_at(records, clock, 900)

    third = _ended_run_at(records, clock, 1100)

    assert [_is_readable(records, run_id) for run_id in (first, second, third)] == [
        True,
        False,
        True,
    ]


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda records, recorder: records.begin(started(), APP, ALICE), id="begin"),
        pytest.param(lambda records, recorder: recorder.accepted(heartbeat(1)), id="accepted"),
        pytest.param(lambda records, recorder: recorder.over(cancelled(1)), id="over"),
        pytest.param(lambda records, recorder: recorder.lost(ClientLoss.DROPPED), id="lost"),
    ],
)
def test_a_write_alone_purges_expired_records(
    records: RunRecords,
    clock: WallClock,
    operation: Callable[[RunRecords, RunRecorder], object],
) -> None:
    expired = _ended_run_at(records, clock, 10)
    _, recorder = _begin(records)
    clock.set_to(instant(4000))

    operation(records, recorder)

    clock.set_to(instant(3000))
    assert not _is_readable(records, expired)


@pytest.mark.parametrize(
    ("max_ended", "seconds_between_runs"),
    [pytest.param(3, 1, id="the bound"), pytest.param(10_000, 7200, id="expiry")],
)
def test_writes_alone_keep_the_memory_bounded(
    clock: WallClock, max_ended: int, seconds_between_runs: float
) -> None:
    records = RunRecords(clock, max_ended=max_ended)

    def run_many(count: int) -> None:
        for _ in range(count):
            _, recorder = _begin(records)
            clock.advance(seconds_between_runs)
            recorder.over(cancelled(1))

    tracemalloc.start()
    try:
        run_many(200)
        gc.collect()
        after_few = tracemalloc.get_traced_memory()[0]
        run_many(20_000)
        gc.collect()
        after_many = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()

    assert after_many - after_few < MEMORY_GROWTH_LIMIT_BYTES


def test_the_store_starts_no_task(records: RunRecords, clock: WallClock) -> None:
    async def use_the_store() -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
        before = asyncio.all_tasks()
        run_id, recorder = _begin(records)
        recorder.accepted(succeeded(1))
        _end_at(clock, recorder, 5)
        _read(records, run_id)
        return before, asyncio.all_tasks()

    before, after = asyncio.run(use_the_store())

    assert after == before


def test_events_and_the_ending_are_counted_and_the_ending_waits_for_over(
    records: RunRecords, clock: WallClock
) -> None:
    run_id, recorder = _begin(records)
    recorder.accepted(heartbeat(1))
    recorder.accepted(succeeded(2))

    before_over = _read(records, run_id)
    clock.set_to(instant(20))
    recorder.over()
    after_over = _read(records, run_id)

    assert before_over is not None
    assert (before_over.state, before_over.events, before_over.ended_at) == (
        RunState.RUNNING,
        3,
        None,
    )
    assert after_over is not None
    assert (after_over.state, after_over.events, after_over.ended_at) == (
        RunState.SUCCEEDED,
        3,
        instant(20),
    )


def test_a_pending_failure_shows_neither_state_nor_code_until_over(
    records: RunRecords,
) -> None:
    run_id, recorder = _begin(records)
    recorder.accepted(interrupted(1))

    pending = _read(records, run_id)
    recorder.over()
    over = _read(records, run_id)

    assert pending is not None
    assert (pending.state, pending.failure_code) == (RunState.RUNNING, None)
    assert over is not None
    assert (over.state, over.failure_code) == (RunState.FAILED, FailureCode.INTERRUPTED)


def test_the_fallback_ends_a_run_that_accepted_no_terminal_without_counting_it(
    records: RunRecords,
) -> None:
    run_id, recorder = _begin(records)

    recorder.over(cancelled(1))

    status = _read(records, run_id)
    assert status is not None
    assert (status.state, status.events) == (RunState.CANCELLED, 1)


def test_a_failed_fallback_records_its_code(records: RunRecords) -> None:
    run_id, recorder = _begin(records)

    recorder.over(interrupted(1))

    status = _read(records, run_id)
    assert status is not None
    assert (status.state, status.failure_code) == (RunState.FAILED, FailureCode.INTERRUPTED)


def test_the_accepted_terminal_wins_over_the_fallback(records: RunRecords) -> None:
    run_id, recorder = _begin(records)
    recorder.accepted(succeeded(1))

    recorder.over(interrupted(2))

    status = _read(records, run_id)
    assert status is not None
    assert (status.state, status.failure_code, status.events) == (RunState.SUCCEEDED, None, 2)


def _over_twice(recorder: RunRecorder) -> None:
    recorder.over(cancelled(1))
    recorder.over(cancelled(1))


def _accepted_after_over(recorder: RunRecorder) -> None:
    recorder.over(cancelled(1))
    recorder.accepted(heartbeat(2))


@pytest.mark.parametrize(
    ("misuse", "error"),
    [
        pytest.param(lambda recorder: recorder.over(), RuntimeError, id="over with no ending"),
        pytest.param(_over_twice, RuntimeError, id="a second over"),
        pytest.param(_accepted_after_over, RuntimeError, id="accepted after over"),
        pytest.param(
            lambda recorder: recorder.accepted(started()), ValueError, id="an opening event again"
        ),
    ],
)
def test_a_recorder_refuses_what_would_corrupt_its_record(
    records: RunRecords, misuse: Callable[[RunRecorder], None], error: type[Exception]
) -> None:
    _, recorder = _begin(records)

    with pytest.raises(error):
        misuse(recorder)


def test_an_id_the_store_already_holds_cannot_begin_again(records: RunRecords) -> None:
    run_id, _ = _begin(records)

    with pytest.raises(ValueError):
        _begin(records, run_id)


def test_the_first_loss_cause_wins_and_the_ending_and_expiry_stay_put(
    records: RunRecords, clock: WallClock
) -> None:
    run_id, recorder = _begin(records)
    _end_at(clock, recorder, 20)
    clock.set_to(instant(45))

    recorder.lost(ClientLoss.DROPPED)
    recorder.lost(ClientLoss.DISCONNECTED)

    status = _read(records, run_id)
    assert status is not None
    assert (status.client_lost, status.state, status.ended_at, status.events) == (
        ClientLoss.DROPPED,
        RunState.CANCELLED,
        instant(20),
        1,
    )
    clock.set_to(instant(3620))
    assert not _is_readable(records, run_id)


def test_a_client_lost_while_running_is_recorded_without_ending_the_run(
    records: RunRecords,
) -> None:
    run_id, recorder = _begin(records)

    recorder.lost(ClientLoss.DISCONNECTED)

    status = _read(records, run_id)
    assert status is not None
    assert (status.state, status.client_lost) == (RunState.RUNNING, ClientLoss.DISCONNECTED)


def test_a_loss_after_eviction_restores_nothing(clock: WallClock) -> None:
    records = RunRecords(clock, max_ended=1)
    run_id, recorder = _begin(records)
    _end_at(clock, recorder, 10)
    _ended_run_at(records, clock, 20)

    recorder.lost(ClientLoss.DROPPED)

    assert not _is_readable(records, run_id)


def test_a_loss_after_expiry_restores_nothing(records: RunRecords, clock: WallClock) -> None:
    run_id, recorder = _begin(records)
    _end_at(clock, recorder, 10)
    clock.set_to(instant(3610))

    recorder.lost(ClientLoss.DROPPED)

    assert not _is_readable(records, run_id)


def test_two_reads_return_equal_distinct_frozen_snapshots_that_a_later_write_leaves_alone(
    records: RunRecords,
) -> None:
    run_id, recorder = _begin(records)
    first, second = _read(records, run_id), _read(records, run_id)
    assert first is not None
    assert second is not None

    recorder.accepted(heartbeat(1))

    assert first == second
    assert first is not second
    assert first.events == 1
    with pytest.raises(ValidationError):
        first.events = 2  # type: ignore[misc]


OTHER_ISSUER = "https://elsewhere.test/realms/x"
DENIED_READERS = [
    pytest.param("presentator", ALICE, id="another app, same user"),
    pytest.param(APP, Principal(ISSUER, "user-bob"), id="another subject"),
    pytest.param(APP, Principal(OTHER_ISSUER, "user-alice"), id="another issuer"),
    pytest.param("presentator", Principal(ISSUER, "user-bob"), id="another app and subject"),
    pytest.param("presentator", Principal(OTHER_ISSUER, "user-alice"), id="another app and issuer"),
    pytest.param(APP, Principal(OTHER_ISSUER, "user-bob"), id="another subject and issuer"),
]


def test_only_the_app_and_principal_that_began_a_run_read_it(records: RunRecords) -> None:
    run_id, _ = _begin(records)

    assert records.read(run_id, APP, ALICE) is not None


@pytest.mark.parametrize(("app", "principal"), DENIED_READERS)
def test_any_other_reader_gets_the_answer_for_an_id_never_begun(
    records: RunRecords, app: str, principal: Principal
) -> None:
    run_id, _ = _begin(records)
    never_begun = uuid4()

    assert records.read(run_id, app, principal) == records.read(never_begun, app, principal)
    assert records.read(run_id, app, principal) is None


def test_a_record_is_keyed_by_its_whole_id(records: RunRecords) -> None:
    run_id, _ = _begin(records)
    neighbour = UUID(int=run_id.int ^ 1)

    assert _read(records, neighbour) is None


def test_a_read_neither_raises_nor_logs_for_an_unknown_id(
    records: RunRecords, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    assert _read(records, uuid4()) is None
    assert caplog.records == []


CANARIES = {
    "text": "canary-assistant-text-7f3a",
    "tool_input": "canary-tool-input-91bc",
    "tool_result": "canary-tool-result-5d2e",
    "answer": "canary-final-answer-a04c",
    "message": "canary-failure-message-c8e1",
}


def _feed_events_with_canaries(recorder: RunRecorder) -> None:
    recorder.accepted(
        AssistantText(v=PROTOCOL_VERSION, run_id=RUN_ID, seq=1, text=CANARIES["text"])
    )
    recorder.accepted(call(2, "call-1", tool_input={"note": CANARIES["tool_input"]}))
    recorder.accepted(result(3, "call-1", CANARIES["tool_result"]))
    recorder.accepted(succeeded(4, CANARIES["answer"]))
    recorder.over(
        Failed(
            v=PROTOCOL_VERSION,
            run_id=RUN_ID,
            seq=5,
            code=FailureCode.INTERRUPTED,
            message=CANARIES["message"],
        )
    )


def test_no_event_text_is_reachable_from_the_store_or_its_recorder(
    clock: WallClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    records = RunRecords(clock)
    run_id, recorder = _begin(records)

    _feed_events_with_canaries(recorder)
    _read(records, run_id)

    needles = list(CANARIES.values())
    assert not reaches_text(records, needles)
    assert not reaches_text(recorder, needles)
    assert not any(needle in caplog.text for needle in needles)


def test_the_reachability_walk_finds_a_canary_that_is_there() -> None:
    assert reaches_text({"nested": [("deep", "xx-canary-xx")]}, ["canary"])


class _ComparisonSpy(str):
    def __new__(cls, text: str, compared: list[tuple[str, object]]) -> _ComparisonSpy:
        spy = super().__new__(cls, text)
        spy.compared = compared
        return spy

    def __eq__(self, other: object) -> bool:
        self.compared.append((str(self), other))
        return super().__eq__(other)

    __hash__ = str.__hash__


def test_a_read_of_an_id_never_begun_still_compares_app_issuer_and_subject(
    records: RunRecords,
) -> None:
    compared: list[tuple[str, object]] = []
    app = _ComparisonSpy("app", compared)
    reader = Principal(_ComparisonSpy("issuer", compared), _ComparisonSpy("subject", compared))

    answer = records.read(uuid4(), app, reader)

    assert answer is None
    assert sorted(compared) == [("app", ""), ("issuer", ""), ("subject", "")]
