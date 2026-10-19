"""A run whose process group is not confirmed gone keeps its place until it is (A1b-3f2).

Decision record 0001, line 20: when a runner's pump met a ``ContainmentLost``, the run's
admission place passes to ``agent_service.quarantine`` at the run's one end. The quarantine
asks ``confirm_group_gone`` at a fixed interval for every group the pump met and frees exactly
that place once each answered ``True``. The ``Quarantine`` cells run it on a step clock with a
real slot; the routing cells drive ``run_frames`` through ``driven`` with a real lease.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

import anyio
import pytest
from provider_test_support import Recorded, ScriptedAnchoredChild, operations
from service_support import (
    CUT_BY_ITS_SCOPE,
    STRAY_CANCELLATION_CANARY,
    ClientSocket,
    ClockBreaksOnce,
    Drive,
    GatedRunner,
    QuarantineLineFails,
    RunnerCloseLineFails,
    RunnerRaisingThisStray,
    ScriptedConfirmation,
    SplitFailureRunner,
    StepClock,
    client_leaves_mid_run,
    client_takes_nothing_until_dropped,
    closed_inside,
    decoded_events,
    driven,
    failure_lines,
    in_loop,
    quarantine_lines,
    respond_through,
    service_config,
    the_app_on,
)

from agent_client.run_stream import ViolationKind
from agent_client.wire import FailureCode, RunState, RunStatus
from agent_providers.errors import ContainmentLost, RetainedGroup
from agent_providers.events import AssistantTextEvent, FinalEvent
from agent_providers.lost_groups import confirm_group_gone, retain_lost_group
from agent_providers.procfs import GroupCensus
from agent_providers.spawn import ChildIdentityLost
from agent_service.admission import AdmissionSlot, Lease
from agent_service.config import RunConfig
from agent_service.quarantine import QUARANTINE_RETRY_SECONDS, Quarantine
from agent_service.run_records import RunRecords

FIRST = RetainedGroup(pgid=4242, generation=777)
SECOND = RetainedGroup(pgid=4343, generation=888)
INTERVAL: Final = QUARANTINE_RETRY_SECONDS
FINAL = FinalEvent(text="Done.")


@pytest.fixture
def quarantine_line_fails(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    line_fails = QuarantineLineFails()
    caplog.handler.addFilter(line_fails)
    yield
    caplog.handler.removeFilter(line_fails)


@pytest.fixture
def runner_close_line_fails(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    line_fails = RunnerCloseLineFails()
    caplog.handler.addFilter(line_fails)
    yield
    caplog.handler.removeFilter(line_fails)


def _lost(group: RetainedGroup = FIRST) -> ContainmentLost:
    return ContainmentLost(group)


def _free_places(slot: AdmissionSlot) -> int:
    probes = []
    while (probe := slot.try_take()) is not None:
        probes.append(probe)
    for probe in probes:
        probe.release()
    return len(probes)


def _retry_of(run_id: uuid.UUID) -> set[asyncio.Task[object]]:
    return {task for task in asyncio.all_tasks() if task.get_name() == f"quarantine:{run_id}"}


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


@dataclass
class _Rig:
    """A quarantine on a step clock over a real slot, asking a scripted confirmation."""

    slot: AdmissionSlot
    clock: StepClock
    confirmation: ScriptedConfirmation
    quarantine: Quarantine

    def hold(
        self,
        *groups: RetainedGroup,
        lease: Lease | None = None,
        run_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        run_id = run_id or uuid.uuid4()
        self.quarantine.hold(run_id, lease, groups)
        return run_id

    def hold_a_taken_place(self, *groups: RetainedGroup) -> uuid.UUID:
        return self.hold(*groups, lease=self.slot.try_take())

    @property
    def free_places(self) -> int:
        return _free_places(self.slot)


def _rig(
    places: int = 1, scripts: dict[RetainedGroup, list[bool | Exception]] | None = None
) -> _Rig:
    clock = StepClock()
    confirmation = ScriptedConfirmation(scripts, clock)
    return _Rig(AdmissionSlot(places), clock, confirmation, Quarantine(clock, confirmation))


@in_loop
async def test_a_held_place_stays_taken_and_the_old_lease_frees_nothing():
    rig = _rig()
    lease = rig.slot.try_take()

    rig.hold(FIRST, lease=lease)
    lease.release()

    assert rig.free_places == 0


@in_loop
async def test_the_retry_asks_once_per_unconfirmed_group_every_interval_and_not_before():
    rig = _rig(scripts={FIRST: [False, False, True], SECOND: [False, True]})
    rig.hold_a_taken_place(FIRST, SECOND)

    assert await rig.clock.parked() == INTERVAL
    rig.clock.advance(INTERVAL - 0.1)
    assert await rig.clock.parked() == INTERVAL
    assert rig.confirmation.calls == []

    deadlines = [await rig.clock.step_to_next_deadline() for _ in range(3)]

    assert deadlines == [INTERVAL, 2 * INTERVAL, 3 * INTERVAL]
    assert rig.confirmation.calls == [
        (INTERVAL, FIRST),
        (INTERVAL, SECOND),
        (2 * INTERVAL, FIRST),
        (2 * INTERVAL, SECOND),
        (3 * INTERVAL, FIRST),
    ]


@in_loop
async def test_a_quarantined_place_is_freed_right_after_the_first_confirmation(caplog):
    caplog.set_level(logging.INFO)
    rig = _rig(scripts={FIRST: [False, False, True]})
    run_id = rig.hold_a_taken_place(FIRST)

    await rig.clock.step_to_next_deadline()
    await rig.clock.step_to_next_deadline()
    assert (rig.free_places, len(_retry_of(run_id))) == (0, 1)
    assert _messages(caplog, logging.INFO) == []
    await rig.clock.step_to_next_deadline()

    assert (rig.free_places, _retry_of(run_id)) == (1, set())
    (freed,) = _messages(caplog, logging.INFO)
    assert str(run_id) in freed


@pytest.mark.parametrize(
    "raised",
    [OSError("the census failed"), ChildIdentityLost("the leader was reaped by someone else")],
    ids=["os-error", "child-identity-lost"],
)
@in_loop
async def test_an_attempt_that_raises_counts_as_not_confirmed_and_is_retried_silently(
    raised, caplog
):
    caplog.set_level(logging.INFO)
    rig = _rig(scripts={FIRST: [raised, True]})
    rig.hold_a_taken_place(FIRST)

    await rig.clock.step_to_next_deadline()
    assert rig.free_places == 0
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    await rig.clock.step_to_next_deadline()

    assert rig.free_places == 1


@in_loop
async def test_one_error_line_names_the_run_and_every_group_and_retries_write_none(caplog):
    caplog.set_level(logging.ERROR)
    rig = _rig(scripts={FIRST: [False, False, True], SECOND: [False, False, True]})
    run_id = rig.hold_a_taken_place(FIRST, SECOND)

    await rig.clock.step_to_next_deadline()
    await rig.clock.step_to_next_deadline()

    assert quarantine_lines(caplog) == [(run_id, [FIRST.pgid, SECOND.pgid])]
    text_without_run_id = caplog.text.replace(str(run_id), "<run>")
    named = [str(group.generation) in text_without_run_id for group in (FIRST, SECOND)]
    assert named == [False, False]


@in_loop
async def test_a_cancelled_retry_releases_nothing_and_writes_no_release_line(caplog):
    caplog.set_level(logging.INFO)
    rig = _rig(scripts={FIRST: [False]})
    run_id = rig.hold_a_taken_place(FIRST)
    await rig.clock.parked()
    (retry,) = _retry_of(run_id)

    retry.cancel()
    await asyncio.gather(retry, return_exceptions=True)

    assert (retry.cancelled(), rig.free_places) == (True, 0)
    assert _messages(caplog, logging.INFO) == []


@in_loop
async def test_a_run_confirmed_gone_frees_its_own_place_and_not_another_runs():
    rig = _rig(places=2, scripts={SECOND: [False, False]})
    rig.hold_a_taken_place(FIRST)
    rig.hold_a_taken_place(SECOND)

    await rig.clock.step_to_next_deadline()

    assert rig.free_places == 1


@in_loop
async def test_a_second_hold_of_a_held_run_raises_and_changes_nothing(caplog):
    caplog.set_level(logging.ERROR)
    rig = _rig(places=2)
    run_id = rig.hold_a_taken_place(FIRST)
    second_lease = rig.slot.try_take()
    assert second_lease is not None

    with pytest.raises(ValueError, match=str(run_id)):
        rig.hold(SECOND, lease=second_lease, run_id=run_id)

    second_lease.release()
    assert (rig.free_places, len(_retry_of(run_id)), len(quarantine_lines(caplog))) == (1, 1, 1)
    await rig.clock.step_to_next_deadline()
    assert (rig.free_places, rig.confirmation.calls) == (2, [(INTERVAL, FIRST)])


@in_loop
async def test_a_failing_log_handler_cannot_free_the_place_or_stop_the_retry(quarantine_line_fails):
    rig = _rig()
    lease = rig.slot.try_take()

    with pytest.raises(OSError, match="the log handler broke"):
        rig.hold(FIRST, lease=lease)

    assert rig.free_places == 0
    await rig.clock.step_to_next_deadline()
    assert rig.free_places == 1


@in_loop
async def test_a_run_without_a_lease_has_its_groups_retried_and_no_place_taken_or_freed(caplog):
    caplog.set_level(logging.ERROR)
    rig = _rig(scripts={FIRST: [False, True]})
    run_id = rig.hold(FIRST)

    assert (rig.free_places, quarantine_lines(caplog)) == (1, [(run_id, [FIRST.pgid])])
    await rig.clock.step_to_next_deadline()
    assert (rig.free_places, len(_retry_of(run_id))) == (1, 1)
    await rig.clock.step_to_next_deadline()

    assert (rig.free_places, _retry_of(run_id)) == (1, set())
    assert rig.confirmation.calls == [(INTERVAL, FIRST), (2 * INTERVAL, FIRST)]


def _released_lease_of_a_run_whose_place_a_successor_took(slot: AdmissionSlot):
    stale = slot.try_take()
    stale.release()
    return stale, slot.try_take()


def _lease_transferred_before(slot: AdmissionSlot):
    stale = slot.try_take()
    return stale, stale.transfer()


@pytest.mark.parametrize(
    "stale_lease",
    [_released_lease_of_a_run_whose_place_a_successor_took, _lease_transferred_before],
    ids=["released-with-a-successor", "already-transferred"],
)
@in_loop
async def test_a_stale_lease_hands_over_no_place_and_frees_no_other_holders_place(stale_lease):
    rig = _rig(places=2)
    stale, holder = stale_lease(rig.slot)

    rig.hold(FIRST, lease=stale)

    assert rig.free_places == 1
    stale.release()
    assert rig.free_places == 1
    await rig.clock.step_to_next_deadline()
    assert rig.free_places == 1
    holder.release()
    assert rig.free_places == 2


@dataclass(frozen=True)
class _HandOver:
    groups: tuple[RetainedGroup, ...]
    place_still_with_the_run: bool
    record: RunState


def _spying_on_the_hand_over(
    drive: Drive,
    monkeypatch: pytest.MonkeyPatch,
    after_noting: Callable[[], None] = lambda: None,
) -> list[_HandOver]:
    hand_overs: list[_HandOver] = []
    hold = drive.quarantine.hold

    def spy(run_id, lease, groups) -> None:
        status = drive.status()
        assert status is not None
        hand_overs.append(_HandOver(groups, drive.slot_is_full(), status.state))
        after_noting()
        hold(run_id, lease, groups)

    monkeypatch.setattr(drive.quarantine, "hold", spy)
    return hand_overs


def _assert_place_held_for(drive: Drive, caplog, *groups: RetainedGroup) -> None:
    assert drive.slot_is_full()
    assert quarantine_lines(caplog) == [(drive.run_id, [group.pgid for group in groups])]


@in_loop
async def test_a_loss_at_a_step_hands_the_place_over_before_the_record_ends_and_logs_one_line(
    caplog, monkeypatch
):
    caplog.set_level(logging.ERROR)
    runner = GatedRunner()
    async with driven(runner) as drive:
        hand_overs = _spying_on_the_hand_over(drive, monkeypatch)
        runner.release(_lost())
        await drive.run_to_end()
        await drive.finished()

    assert hand_overs == [_HandOver((FIRST,), True, RunState.RUNNING)]
    _assert_place_held_for(drive, caplog, FIRST)
    assert failure_lines(caplog) == []


@in_loop
async def test_a_loss_at_the_close_of_an_idle_timeout_keeps_the_place_and_logs_no_close_line(
    caplog,
):
    caplog.set_level(logging.ERROR)
    runner = GatedRunner()
    runner.close_error = _lost()
    async with driven(runner) as drive:
        await drive.run_to_end()
        await drive.finished()

    _assert_place_held_for(drive, caplog, FIRST)
    assert failure_lines(caplog) == []


@in_loop
async def test_a_loss_at_the_close_after_a_final_answer_keeps_the_place(caplog):
    caplog.set_level(logging.ERROR)
    runner = GatedRunner()
    runner.close_error = _lost()
    async with driven(runner) as drive:
        runner.release(FINAL)
        await drive.run_to_end()
        await drive.finished()

    _assert_place_held_for(drive, caplog, FIRST)
    assert failure_lines(caplog) == []


@pytest.mark.parametrize(
    "client_leaves",
    [client_leaves_mid_run, client_takes_nothing_until_dropped],
    ids=["disconnect", "drop"],
)
@in_loop
async def test_a_loss_at_the_close_after_the_client_left_keeps_the_place(client_leaves, caplog):
    caplog.set_level(logging.ERROR)
    runner = GatedRunner()
    runner.close_error = _lost()
    async with driven(runner, read=False) as drive:
        await client_leaves(drive)
        await drive.clock.parked()
        await drive.frames.aclose()

    _assert_place_held_for(drive, caplog, FIRST)


@pytest.mark.parametrize(
    ("ending", "kind"),
    [
        pytest.param("step", "RuntimeError", id="a-step-raises-an-error-carrying-a-loss"),
        pytest.param("close", "ExceptionGroup", id="a-close-raises-a-group-holding-a-loss"),
    ],
)
@in_loop
async def test_a_loss_carried_by_another_error_keeps_the_place_and_that_errors_own_line(
    ending, kind, caplog
):
    caplog.set_level(logging.ERROR)
    runner = GatedRunner()
    async with driven(runner) as drive:
        if ending == "step":
            carrier = RuntimeError("carrier")
            carrier.__context__ = _lost()
            runner.release(carrier)
        else:
            runner.close_error = ExceptionGroup("close", [_lost()])
            runner.release(FINAL)
        await drive.run_to_end()
        await drive.finished()

    _assert_place_held_for(drive, caplog, FIRST)
    assert [line[1] for line in failure_lines(caplog)] == [kind]


@in_loop
async def test_a_close_line_that_fails_cannot_keep_the_place_from_the_quarantine(
    runner_close_line_fails, caplog
):
    caplog.set_level(logging.ERROR)
    runner = GatedRunner()
    runner.close_error = ExceptionGroup("close", [_lost()])
    async with driven(runner) as drive:
        runner.release(FINAL)
        await drive.run_to_end()
        await asyncio.gather(drive.reader, return_exceptions=True)

        assert len(drive.quarantine_retries()) == 1
        drive.lease.release()
        assert drive.slot_is_full()
        await drive.quarantine_clock.step_to_next_deadline()
        assert not drive.slot_is_full()


def _close_line_failure(run_id: uuid.UUID) -> tuple[str, str]:
    return f"The close line of run {run_id} failed", "OSError"


def _the_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    return [(event, kind) for event, kind, _raiser in failure_lines(caplog)]


def _assert_ended_run(status: RunStatus | None, state: RunState, code: FailureCode | None) -> None:
    assert status is not None
    assert (status.state, status.failure_code) == (state, code)


def _assert_ended(drive: Drive, state: RunState, code: FailureCode | None) -> None:
    _assert_ended_run(drive.status(), state, code)


DECIDED_ENDINGS: Final = [
    pytest.param(RunConfig(), FINAL, RunState.SUCCEEDED, None, "succeeded", id="a-decided-success"),
    pytest.param(
        RunConfig(max_bytes=10),
        AssistantTextEvent(text="Too long for the budget."),
        RunState.FAILED,
        FailureCode.LIMIT_EXCEEDED,
        "failed",
        id="a-decided-limit-failure",
    ),
]


@pytest.mark.parametrize(("runs", "ending", "state", "code", "terminal"), DECIDED_ENDINGS)
@in_loop
async def test_a_close_line_that_fails_leaves_the_ending_the_run_had_decided(
    runs, ending, state, code, terminal, runner_close_line_fails, caplog
):
    caplog.set_level(logging.ERROR)
    runner = GatedRunner()
    runner.close_error = RuntimeError("the close broke")
    async with driven(runner, runs=runs) as drive:
        runner.release(ending)
        await drive.run_to_end()
        await drive.finished()

    _assert_ended(drive, state, code)
    assert drive.types[-1] == terminal
    assert drive.end_of_stream() == []
    assert not drive.slot_is_full()
    assert _the_lines(caplog) == [_close_line_failure(drive.run_id)]


@in_loop
async def test_a_close_line_that_fails_beside_a_stream_closed_before_any_decision_ends_it_cancelled(
    runner_close_line_fails, caplog
):
    caplog.set_level(logging.ERROR)
    runner = GatedRunner()
    runner.close_error = RuntimeError("the close broke")
    async with driven(runner, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.clock.parked()
        await drive.frames.aclose()

    _assert_ended(drive, RunState.CANCELLED, None)
    assert drive.types == ["run_started"]
    assert drive.end_of_stream() == [ViolationKind.TRUNCATED]
    assert not drive.slot_is_full()
    assert _the_lines(caplog) == [_close_line_failure(drive.run_id)]


@in_loop
async def test_a_close_line_that_fails_over_a_loss_still_ends_the_record_failed_and_holds_the_place(
    runner_close_line_fails, caplog, monkeypatch
):
    caplog.set_level(logging.ERROR)
    runner = GatedRunner()
    runner.close_error = ExceptionGroup("close", [_lost()])
    async with driven(runner) as drive:
        hand_overs = _spying_on_the_hand_over(drive, monkeypatch)
        runner.release(FINAL)
        await drive.run_to_end()
        await asyncio.gather(drive.reader, return_exceptions=True)

    assert hand_overs == [_HandOver((FIRST,), True, RunState.RUNNING)]
    _assert_ended(drive, RunState.FAILED, FailureCode.PROVIDER_ERROR)
    _assert_place_held_for(drive, caplog, FIRST)
    assert _the_lines(caplog) == [_close_line_failure(drive.run_id)]


@in_loop
async def test_a_cancelled_close_is_made_inert_though_its_line_fails(
    runner_close_line_fails, caplog
):
    caplog.set_level(logging.ERROR)
    stray = asyncio.CancelledError(STRAY_CANCELLATION_CANARY)
    stray.__cause__ = RuntimeError("its cause")
    stray.__context__ = RuntimeError("its context")
    async with driven(RunnerRaisingThisStray(stray), read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.clock.parked()
        await drive.frames.aclose()

    assert (stray.__traceback__, stray.__cause__, stray.__context__) == (None, None, None)
    assert _close_line_failure(drive.run_id) in _the_lines(caplog)


class _CloseLineThatBreaksTheClock(RunnerCloseLineFails):
    """The failing close line, after which the store clock breaks once."""

    def __init__(self, clock: ClockBreaksOnce) -> None:
        self._clock = clock

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage().startswith("Closing the runner of run"):
            self._clock.arm()
        return super().filter(record)


@in_loop
async def test_a_later_cleanup_failure_is_raised_and_the_close_line_failure_logged_once(caplog):
    caplog.set_level(logging.ERROR)
    clock = ClockBreaksOnce()
    caplog.handler.addFilter(line_breaks_the_clock := _CloseLineThatBreaksTheClock(clock))
    runner = GatedRunner()
    runner.close_error = RuntimeError("the close broke")
    try:
        async with driven(runner, records=RunRecords(clock), read=False) as drive:
            drive.took(await anext(drive.frames))
            runner.release(FINAL)

            with pytest.raises(RuntimeError, match="the store clock broke once"):
                await anext(drive.frames)
    finally:
        caplog.handler.removeFilter(line_breaks_the_clock)

    _assert_ended(drive, RunState.FAILED, FailureCode.PROVIDER_ERROR)
    assert drive.types == ["run_started"]
    assert _the_lines(caplog) == [_close_line_failure(drive.run_id)]


@in_loop
async def test_a_broken_commit_after_a_loss_hands_over_first_and_ends_the_record_failed(
    caplog, monkeypatch
):
    caplog.set_level(logging.ERROR)
    clock = ClockBreaksOnce()
    runner = GatedRunner()
    runner.close_error = _lost()
    async with driven(runner, records=RunRecords(clock), read=False) as drive:
        hand_overs = _spying_on_the_hand_over(drive, monkeypatch, after_noting=clock.arm)
        drive.took(await anext(drive.frames))
        runner.release(FINAL)

        with pytest.raises(RuntimeError, match="the store clock broke once"):
            await anext(drive.frames)

    assert hand_overs == [_HandOver((FIRST,), True, RunState.RUNNING)]
    _assert_ended(drive, RunState.FAILED, FailureCode.PROVIDER_ERROR)
    _assert_place_held_for(drive, caplog, FIRST)


@pytest.mark.parametrize(("runs", "ending", "state", "code", "terminal"), DECIDED_ENDINGS)
@in_loop
async def test_a_failed_close_line_reads_the_decided_ending_through_the_app_and_frees_the_place(
    runs, ending, state, code, terminal, runner_close_line_fails, caplog, monkeypatch
):
    caplog.set_level(logging.ERROR)
    runner = GatedRunner()
    runner.close_error = RuntimeError("the close broke")
    async with driven(runner, runs=runs, read=False) as drive:
        app = the_app_on(
            drive,
            monkeypatch,
            config=service_config(runs={**runs.model_dump(), "max_parallel_cli_runs": 1}),
        )
        client = ClientSocket()
        response = await respond_through(app, client)
        await client.received(b"run_started")
        runner.release(ending)
        await response

        _assert_ended_run(drive.status(client.run_id), state, code)
        assert [event.type for event in decoded_events(client.body)][-1] == terminal
        next_run = ClientSocket()
        next_response = await respond_through(app, next_run)
        await next_run.received(b"run_started")
        assert next_run.messages[0]["status"] == 200
        runner.release(FINAL)
        await next_response

    assert _the_lines(caplog) == [
        _close_line_failure(client.run_id),
        _close_line_failure(next_run.run_id),
    ]


@in_loop
async def test_a_loss_beside_a_cancellation_in_an_escaping_group_keeps_the_place(
    caplog, monkeypatch
):
    caplog.set_level(logging.ERROR)
    runner = GatedRunner()
    async with driven(runner) as drive:
        hand_overs = _spying_on_the_hand_over(drive, monkeypatch)
        runner.release(BaseExceptionGroup("cleanup", [asyncio.CancelledError(), _lost()]))
        await drive.run_to_end()
        await asyncio.gather(drive.reader, return_exceptions=True)
        drive.assert_nothing_left()

    assert [hand_over.groups for hand_over in hand_overs] == [(FIRST,)]
    _assert_place_held_for(drive, caplog, FIRST)
    assert [line[1] for line in failure_lines(caplog)] == ["BaseExceptionGroup"]


CANARIES: Final = ("CANARY_A", "CANARY_B", "CANARY_C", "CANARY_D")


@in_loop
async def test_no_text_of_a_failure_reaches_a_log_record_and_the_line_names_every_group(caplog):
    caplog.set_level(logging.DEBUG)
    loss = _lost(FIRST)
    loss.add_note(CANARIES[1])
    carrier = RuntimeError(CANARIES[0])
    carrier.__context__ = loss
    carrier.__cause__ = OSError(CANARIES[2])
    runner = SplitFailureRunner(carrier, ExceptionGroup(CANARIES[3], [_lost(SECOND)]))
    async with driven(runner) as drive:
        await drive.run_to_end()
        await drive.finished()

    assert [canary in caplog.text for canary in CANARIES] == [False] * len(CANARIES)
    assert [record.exc_info for record in caplog.records] == [None] * len(caplog.records)
    assert quarantine_lines(caplog) == [(drive.run_id, [FIRST.pgid, SECOND.pgid])]


@in_loop
async def test_a_place_is_handed_over_only_when_the_pump_ended_with_every_group_it_met(
    monkeypatch,
):
    runner = SplitFailureRunner(_lost(FIRST), _lost(SECOND))
    runner.hold_close()
    confirmation = ScriptedConfirmation({FIRST: [True], SECOND: [False, True]})
    async with driven(runner, confirm=confirmation) as drive:
        hand_overs = _spying_on_the_hand_over(drive, monkeypatch)
        await drive.clock.parked()
        for _ in range(3):
            drive.quarantine_clock.advance(INTERVAL)
        await drive.quarantine_clock.parked()

        assert [entry for entry in runner.log if not entry.startswith("taken:")] == [
            "step raised",
            "closing",
        ]
        assert (hand_overs, confirmation.calls) == ([], [])

        runner.release_close()
        await drive.finished()
        assert [hand_over.groups for hand_over in hand_overs] == [(FIRST, SECOND)]
        await drive.quarantine_clock.step_to_next_deadline()
        assert drive.slot_is_full()
        await drive.quarantine_clock.step_to_next_deadline()
        assert not drive.slot_is_full()


class _RecorderFailsOver:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def over(recorder, fallback=None):
            raise RuntimeError("the recorder broke")

        monkeypatch.setattr("agent_service.run_records.RunRecorder.over", over)


@in_loop
async def test_a_failing_record_end_cannot_free_the_place_handed_over_before_it(
    monkeypatch, caplog
):
    caplog.set_level(logging.ERROR)
    _RecorderFailsOver(monkeypatch)
    runner = GatedRunner()
    async with driven(runner) as drive:
        runner.release(_lost())
        await drive.run_to_end()
        (outcome,) = await asyncio.gather(drive.reader, return_exceptions=True)

    assert isinstance(outcome, RuntimeError)
    _assert_place_held_for(drive, caplog, FIRST)


@pytest.mark.parametrize("record_end_fails", [False, True], ids=["record-ends", "record-fails"])
@in_loop
async def test_a_hand_over_that_raises_still_ends_the_record_is_raised_and_keeps_the_place(
    record_end_fails, quarantine_line_fails, monkeypatch, caplog
):
    caplog.set_level(logging.ERROR)
    if record_end_fails:
        _RecorderFailsOver(monkeypatch)
    runner = GatedRunner()
    async with driven(runner) as drive:
        runner.release(_lost())
        await drive.run_to_end()
        (outcome,) = await asyncio.gather(drive.reader, return_exceptions=True)

        assert isinstance(outcome, OSError)
        assert drive.slot_is_full()
        await drive.quarantine_clock.step_to_next_deadline()
        assert not drive.slot_is_full()

    if record_end_fails:
        assert [(line[0], line[1]) for line in failure_lines(caplog)] == [
            (f"The cleanup of run {drive.run_id} failed", "RuntimeError")
        ]
    else:
        status = drive.status()
        assert status is not None
        assert (status.state, status.failure_code) == (RunState.FAILED, FailureCode.PROVIDER_ERROR)


@pytest.mark.parametrize(
    ("record_end_fails", "logged_kinds"),
    [(False, ["OSError"]), (True, ["OSError", "RuntimeError"])],
    ids=["record-ends", "record-fails"],
)
@in_loop
async def test_a_scope_cut_closer_over_a_failing_hand_over_is_cancelled_after_the_record_end(
    record_end_fails, logged_kinds, quarantine_line_fails, monkeypatch, caplog
):
    caplog.set_level(logging.ERROR)
    if record_end_fails:
        _RecorderFailsOver(monkeypatch)
    runner = GatedRunner()
    runner.close_error = _lost()
    runner.hold_close()
    async with driven(runner, read=False) as drive:
        drive.took(await anext(drive.frames))
        await drive.clock.parked()
        scope = anyio.CancelScope()
        closing = asyncio.create_task(closed_inside(scope, drive.frames.aclose()))
        await drive.clock.parked()
        scope.cancel()
        await drive.clock.parked()
        assert not closing.done()

        runner.release_close()

        assert await closing is CUT_BY_ITS_SCOPE
        assert drive.slot_is_full()
        await drive.quarantine_clock.step_to_next_deadline()
        assert not drive.slot_is_full()

    cleanup = f"The cleanup of run {drive.run_id} failed"
    assert [(line[0], line[1]) for line in failure_lines(caplog)] == [
        (cleanup, kind) for kind in logged_kinds
    ]
    if not record_end_fails:
        status = drive.status()
        assert status is not None
        assert (status.state, status.failure_code) == (RunState.FAILED, FailureCode.PROVIDER_ERROR)


@in_loop
async def test_a_real_retained_group_frees_the_place_only_after_the_core_released_its_slot():
    log: list[Recorded] = []
    slot = AdmissionSlot(1)
    full_when_the_core_released_its_slot: list[bool] = []

    def release_slot() -> None:
        log.append(Recorded("slot", FIRST.pgid, threading.current_thread()))
        full_when_the_core_released_its_slot.append(_free_places(slot) == 0)

    child = ScriptedAnchoredChild(
        FIRST.pgid, log, censuses=[GroupCensus.LIVE, GroupCensus.EMPTY], returncode=3
    )
    group = retain_lost_group(child, release_slot)
    attempts: asyncio.Queue[bool] = asyncio.Queue()
    clock = StepClock()

    async def confirm(retained: RetainedGroup) -> bool:
        answer = await confirm_group_gone(retained)
        attempts.put_nowait(answer)
        return answer

    Quarantine(clock, confirm).hold(uuid.uuid4(), slot.try_take(), (group,))

    await clock.parked()
    clock.advance_to(INTERVAL)
    assert await asyncio.wait_for(attempts.get(), 5) is False
    await clock.parked()
    assert (_free_places(slot), operations(log)) == (0, [("kill", FIRST.pgid)])
    clock.advance_to(2 * INTERVAL)
    assert await asyncio.wait_for(attempts.get(), 5) is True
    await clock.parked()

    assert operations(log) == [("kill", FIRST.pgid)] * 2 + [
        ("release", FIRST.pgid),
        ("slot", FIRST.pgid),
    ]
    assert full_when_the_core_released_its_slot == [True]
    assert _free_places(slot) == 1
    assert await confirm_group_gone(group) is True
    assert len(operations(log)) == 4
