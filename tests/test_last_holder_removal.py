"""One removal, made once by the last of a closure and its reservations."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial

import pytest
from anchored_support import TEST_WAIT_SECONDS
from provider_test_support import ThreadedOperation, code_of

from agent_providers.process import HolderReservation, LastHolderRemoval

RESERVATIONS_CLOSED = "reservations are closed"
PROTOCOL_CODE = code_of(
    LastHolderRemoval.reserve,
    LastHolderRemoval.close,
    LastHolderRemoval._release,
    LastHolderRemoval._claim_when_last,
    HolderReservation.release,
)

BOTH_OUTCOMES = pytest.mark.parametrize(
    "removal_raises", (False, True), ids=("returns", "raises"),
)


class RemovalFailed(Exception):
    pass


@pytest.fixture(autouse=True)
def _no_thread_failed(thread_failures: list[threading.ExceptHookArgs]) -> Iterator[None]:
    yield
    assert thread_failures == []


class ContendedLock:
    """Tells a scheduler that a second thread reached the lock another holds."""

    def __init__(self, real: threading.Lock) -> None:
        self.real = real
        self.contended = threading.Event()

    def __enter__(self) -> bool:
        if self.real.acquire(blocking=False):
            return True
        self.contended.set()
        return self.real.acquire()

    def __exit__(self, *_exception: object) -> None:
        self.real.release()

    def is_free(self) -> bool:
        if not self.real.acquire(blocking=False):
            return False
        self.real.release()
        return True


@dataclass(frozen=True)
class Attempt:
    lock_was_free: bool
    closure_was_made: bool
    outstanding: frozenset[str]


@dataclass
class Holders:
    """A protocol, the row's own ledger of what it did, and the recorder of removals."""

    removal_raises: bool
    attempts: list[Attempt] = field(default_factory=list)
    reservations: dict[str, HolderReservation] = field(default_factory=dict)
    failure: RemovalFailed = field(default_factory=RemovalFailed)
    entered: threading.Event = field(default_factory=threading.Event)
    let_go: threading.Event = field(default_factory=threading.Event)
    hold_attempts: bool = False
    closure_was_made: bool = False
    outstanding: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.protocol = LastHolderRemoval(self._remove)
        self.lock = ContendedLock(self.protocol._lock)
        self.protocol._lock = self.lock

    def _remove(self) -> None:
        self.attempts.append(
            Attempt(self.lock.is_free(), self.closure_was_made, frozenset(self.outstanding)),
        )
        self.entered.set()
        if self.hold_attempts:
            assert self.let_go.wait(TEST_WAIT_SECONDS), "the held removal was never let go"
        if self.removal_raises:
            raise self.failure

    def reserve(self, name: str) -> None:
        self.reservations[name] = self.protocol.reserve()
        self.outstanding.add(name)

    def close(self) -> RemovalFailed | None:
        self.closure_was_made = True
        return self._failure_of(self.protocol.close)

    def release(self, name: str) -> RemovalFailed | None:
        self.outstanding.discard(name)
        return self._failure_of(self.reservations[name].release)

    def _failure_of(self, call: Callable[[], None]) -> RemovalFailed | None:
        try:
            call()
        except RemovalFailed as failure:
            return failure
        return None

    def expected_failure(self) -> RemovalFailed | None:
        return self.failure if self.removal_raises else None

    def assert_one_attempt_by_the_last_holder(self) -> None:
        assert len(self.attempts) == 1
        (attempt,) = self.attempts
        assert attempt.closure_was_made
        assert attempt.outstanding == frozenset()


def operation_of(holders: Holders, step: str) -> Callable[[], object]:
    action, _, name = step.partition(" ")
    return {
        "close": holders.close,
        "release": lambda: holders.release(name),
        "reserve": lambda: holders.reserve(name),
    }[action]


def holders_after(removal_raises: bool, *steps: str) -> Holders:
    holders = Holders(removal_raises)
    for step in steps:
        operation_of(holders, step)()
    return holders


@BOTH_OUTCOMES
@pytest.mark.parametrize("release_order", (("A", "B"), ("B", "A")), ids=("a-then-b", "b-then-a"))
def test_the_last_release_removes_once_after_the_closure(
    removal_raises: bool, release_order: tuple[str, str],
) -> None:
    first, second = release_order
    holders = holders_after(removal_raises, "reserve A", "reserve B", "close")

    first_outcome = holders.release(first)
    attempts_before_the_last = len(holders.attempts)
    last_outcome = holders.release(second)
    later_outcomes = [holders.release("A"), holders.release("B"), holders.close()]

    assert (first_outcome, attempts_before_the_last) == (None, 0)
    assert last_outcome is holders.expected_failure()
    assert later_outcomes == [None, None, None]
    holders.assert_one_attempt_by_the_last_holder()
    assert holders.attempts[0].lock_was_free


@BOTH_OUTCOMES
def test_a_duplicate_release_does_not_stand_in_for_a_reservation(removal_raises: bool) -> None:
    holders = holders_after(removal_raises, "reserve A", "reserve B", "close")

    holders.release("A")
    duplicate_outcome = holders.release("A")
    attempts_after_the_duplicate = len(holders.attempts)
    last_outcome = holders.release("B")
    later_outcomes = [holders.release("A"), holders.release("B")]

    assert (duplicate_outcome, attempts_after_the_duplicate) == (None, 0)
    assert last_outcome is holders.expected_failure()
    assert later_outcomes == [None, None]
    holders.assert_one_attempt_by_the_last_holder()
    assert holders.attempts[0].lock_was_free


@BOTH_OUTCOMES
@pytest.mark.parametrize(
    ("held", "attempts_after_the_closure"), (((), 1), (("A",), 0)), ids=("no-reservation", "held"),
)
def test_the_closure_removes_only_when_no_reservation_is_held(
    removal_raises: bool, held: tuple[str, ...], attempts_after_the_closure: int,
) -> None:
    holders = holders_after(removal_raises, *(f"reserve {name}" for name in held))

    closing_outcome = holders.close()
    attempts_after_the_first_closure = len(holders.attempts)
    with pytest.raises(RuntimeError, match=RESERVATIONS_CLOSED):
        holders.protocol.reserve()
    releasing_outcomes = [holders.release(name) for name in held]
    outcomes = [closing_outcome, *releasing_outcomes]
    second_closure_outcome = holders.close()
    with pytest.raises(RuntimeError, match=RESERVATIONS_CLOSED):
        holders.protocol.reserve()

    assert attempts_after_the_first_closure == attempts_after_the_closure
    expected_outcomes = [None, holders.expected_failure()] if held else [holders.expected_failure()]
    assert outcomes == expected_outcomes
    assert second_closure_outcome is None
    holders.assert_one_attempt_by_the_last_holder()
    assert holders.attempts[0].lock_was_free


@dataclass(frozen=True)
class Race:
    setup: tuple[str, ...]
    paused: str
    other: str


@contextmanager
def interleaved(paused: ThreadedOperation, other: ThreadedOperation, contended: threading.Event):
    other_started = False
    try:
        paused.start()
        other.start(or_when=contended)
        other_started = True
        yield
    finally:
        try:
            paused.finish()
        finally:
            if other_started:
                other.finish()


def step_through(
    race: Race,
    removal_raises: bool,
    outcome_holds: Callable[[Holders, ThreadedOperation, ThreadedOperation], None],
) -> int:
    position = 1
    while True:
        holders = holders_after(removal_raises, *race.setup)
        paused = ThreadedOperation(
            operation_of(holders, race.paused), pause_in=PROTOCOL_CODE, at_line=position,
        )
        other = ThreadedOperation(operation_of(holders, race.other))
        with interleaved(paused, other, holders.lock.contended):
            pass
        outcome_holds(holders, paused, other)
        if not paused.paused:
            return position
        position += 1


def assert_exactly_one_operation_made_the_attempt(
    holders: Holders, paused: ThreadedOperation, other: ThreadedOperation,
) -> None:
    holders.assert_one_attempt_by_the_last_holder()
    assert paused.refusal is None
    assert other.refusal is None
    outcomes = [paused.outcome, other.outcome]
    assert sorted(outcomes, key=lambda outcome: outcome is None) == (
        [holders.failure, None] if holders.removal_raises else [None, None]
    )


@BOTH_OUTCOMES
@pytest.mark.parametrize(
    "race",
    [
        pytest.param(Race(("reserve A",), "close", "release A"), id="close-against-release"),
        pytest.param(Race(("reserve A",), "release A", "close"), id="release-against-close"),
        pytest.param(
            Race(("reserve A", "reserve B", "close"), "release A", "release B"),
            id="release-against-release",
        ),
        pytest.param(Race((), "close", "close"), id="close-against-close"),
        pytest.param(
            Race(("reserve A", "close"), "release A", "release A"),
            id="release-against-duplicate-release",
        ),
    ],
)
def test_competing_final_operations_make_one_attempt_at_every_line(
    race: Race, removal_raises: bool,
) -> None:
    positions = step_through(race, removal_raises, assert_exactly_one_operation_made_the_attempt)

    assert positions > 1


def assert_a_reservation_was_refused_or_delayed_the_attempt(
    race: Race, holders: Holders, paused: ThreadedOperation, other: ThreadedOperation,
) -> None:
    reserving, closing = (paused, other) if race.paused.startswith("reserve") else (other, paused)
    if reserving.refusal is not None:
        assert str(reserving.refusal) == RESERVATIONS_CLOSED
        assert closing.outcome is holders.expected_failure()
        holders.assert_one_attempt_by_the_last_holder()
        return
    assert holders.attempts == []
    assert closing.outcome is None
    assert holders.release("late") is holders.expected_failure()
    holders.assert_one_attempt_by_the_last_holder()


@BOTH_OUTCOMES
@pytest.mark.parametrize(
    "race",
    [
        pytest.param(Race((), "reserve late", "close"), id="reserve-against-close"),
        pytest.param(Race((), "close", "reserve late"), id="close-against-reserve"),
    ],
)
def test_a_reservation_racing_the_closure_is_refused_or_delays_the_one_attempt(
    race: Race, removal_raises: bool,
) -> None:
    outcome_holds = partial(assert_a_reservation_was_refused_or_delayed_the_attempt, race)

    positions = step_through(race, removal_raises, outcome_holds)

    assert positions > 1


@contextmanager
def running_on_a_thread(operation: ThreadedOperation, or_when: threading.Event | None = None):
    try:
        operation.start(or_when)
        yield operation
    finally:
        operation.finish()


@BOTH_OUTCOMES
@pytest.mark.parametrize("winner", ("closure", "last-release"))
def test_competitors_finish_while_the_one_attempt_is_held(
    removal_raises: bool, winner: str,
) -> None:
    holders = holders_after(removal_raises, "reserve A")
    holders.hold_attempts = True
    if winner == "closure":
        holders.release("A")
        winning_call = holders.close
    else:
        holders.close()
        winning_call = partial(holders.release, "A")
    competing = [
        ThreadedOperation(operation_of(holders, step))
        for step in ("close", "release A", "reserve late")
    ]
    holding = ThreadedOperation(winning_call)

    with running_on_a_thread(holding, or_when=holders.entered):
        assert holders.entered.wait(TEST_WAIT_SECONDS)
        try:
            for competitor in competing:
                with running_on_a_thread(competitor):
                    pass
            attempts_while_held = len(holders.attempts)
        finally:
            holders.let_go.set()

    assert attempts_while_held == 1
    assert [competitor.outcome for competitor in competing[:-1]] == [None, None]
    assert str(competing[-1].refusal) == RESERVATIONS_CLOSED
    assert holding.outcome is holders.expected_failure()
    holders.assert_one_attempt_by_the_last_holder()
    assert holders.attempts[0].lock_was_free


class PauseThatExpiresAtOnce(ThreadedOperation):
    def _wait_for_resume(self) -> bool:
        return False


class PauseThatExpiresOnResume(ThreadedOperation):
    def _wait_for_resume(self) -> bool:
        self._resume.wait(TEST_WAIT_SECONDS)
        return False


def an_operation_with_lines() -> str:
    reached = "the end"
    return reached


@pytest.mark.parametrize(
    "expiring_pause",
    (PauseThatExpiresAtOnce, PauseThatExpiresOnResume),
    ids=("at-once", "on-resume"),
)
def test_an_expired_pause_fails_the_row_after_both_threads_are_joined(
    expiring_pause: type[ThreadedOperation],
) -> None:
    threads_before = threading.active_count()
    operation = expiring_pause(
        an_operation_with_lines, pause_in=code_of(an_operation_with_lines), at_line=1,
    )
    operation.start()

    with pytest.raises(AssertionError, match="pause expired"):
        operation.finish()

    assert operation.outcome == "the end"
    assert threading.active_count() == threads_before
