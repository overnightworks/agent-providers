"""The neutral reap of ``anchored_turn``: its protocol on a pool that is not Claude's.

Every row builds its own pool with its own ``ReapBounds`` and enters
``anchored_turn_child`` through the one spawn seam, so no Claude name takes part.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from provider_test_support import (
    ExitOnKill,
    ScenarioDeadline,
    ScriptedClaudeChild,
    a_claude_cli_child,
    confirm_the_retained_group_gone,
    held_claude_slots,
    install_claude_cli_children,
    killpg_forbidden,
    operations,
    recorded_census_workers,
    run_bounded,
    scenario_holds,
)

from agent_providers import anchored_turn, lost_groups, process
from agent_providers.anchored_turn import CliProcessPool, ReapBounds, anchored_turn_child
from agent_providers.errors import ContainmentLost, RetainedGroup
from agent_providers.procfs import GroupCensus
from agent_providers.spawn import (
    ChildIdentityLost,
    ChildProcess,
    KillUntilGoneResult,
    closed_environment,
)

PASSED_CONFIRMATION_SECONDS = 7.5
KILL = "kill_until_gone"


def a_pool(*, confirmation_seconds: float = PASSED_CONFIRMATION_SECONDS) -> CliProcessPool:
    return CliProcessPool(
        limit=1,
        name="Any CLI",
        reap=ReapBounds(sigterm_grace_seconds=0, confirmation_seconds=confirmation_seconds),
    )


def slot_is_held(pool: CliProcessPool) -> bool:
    reservation = pool.reserve()
    if reservation is None:
        return True
    pool.release(reservation)
    return False


@dataclass
class Retentions:
    kept: list[tuple[RetainedGroup, Callable[[], None]]] = field(default_factory=list)

    def free_every_slot(self) -> None:
        for _group, release_slot in self.kept:
            release_slot()


@pytest.fixture
def retentions(monkeypatch: pytest.MonkeyPatch) -> Retentions:
    retentions = Retentions()
    real_retain = anchored_turn.retain_lost_group

    def keep(child, release_slot) -> RetainedGroup:
        group = real_retain(child, release_slot)
        retentions.kept.append((group, release_slot))
        return group

    monkeypatch.setattr(anchored_turn, "retain_lost_group", keep)
    return retentions


@dataclass
class NeutralTurn:
    pool: CliProcessPool
    home: process.PrivateHome
    child: ScriptedClaudeChild
    spawn: ChildProcess

    async def enter(self) -> None:
        reservation = self.home.reserve()
        try:
            async with anchored_turn_child(self.pool, self.spawn, home_reservation=reservation):
                pass
        finally:
            self.home.close()

    def home_is_present(self) -> bool:
        return self.home.path.exists()


@pytest.fixture
def a_turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., NeutralTurn]:
    def build(pool: CliProcessPool, **child_script: Any) -> NeutralTurn:
        child = a_claude_cli_child(**child_script)
        install_claude_cli_children(monkeypatch, child)
        home = process.PrivateHome(prefix="neutral-turn-")
        spawn = ChildProcess(Path("/bin/true"), (), closed_environment(home.path), tmp_path)
        return NeutralTurn(pool, home, child, spawn)

    return build


@dataclass(frozen=True)
class ExitCase:
    script: dict[str, Any]
    log: list[str]


EVERY_EXIT = {
    "published_before_the_reap": ExitCase({"returncode": 0}, [KILL, "release"]),
    "ended_by_sigterm": ExitCase(
        {"returncode": None, "natural_exit": None}, ["SIGTERM", KILL, "release"],
    ),
    "survives_sigterm": ExitCase(
        {"returncode": None, "natural_exit": None, "survives": {signal.SIGTERM}},
        ["SIGTERM", KILL, "release"],
    ),
}


@pytest.mark.parametrize("case", EVERY_EXIT.values(), ids=EVERY_EXIT)
def test_a_leader_is_killed_and_counted_with_the_pools_bound_before_its_slot_is_freed(
    case: ExitCase, a_turn,
) -> None:
    pool = a_pool()
    turn = a_turn(pool, **case.script)

    with killpg_forbidden():
        run_bounded(turn.enter())

    assert [name for name, _pid in operations(turn.child.log)] == case.log
    assert turn.child.kill_bounds == [PASSED_CONFIRMATION_SECONDS]
    assert not slot_is_held(pool)
    assert not turn.home_is_present()
    assert held_claude_slots() == 0


UNCONFIRMED_RESULTS = {
    "live_in_time": KillUntilGoneResult(GroupCensus.LIVE, ended_in_time=True),
    **{
        census.value: KillUntilGoneResult(census, ended_in_time=True)
        for census in (
            GroupCensus.NOT_PROCFS,
            GroupCensus.OTHER_PID_NAMESPACE,
            GroupCensus.PIDS_HIDDEN,
            GroupCensus.PROC_UNREADABLE,
            GroupCensus.ANCHOR_NOT_VISIBLE,
        )
    },
    "empty_too_late": KillUntilGoneResult(GroupCensus.EMPTY, ended_in_time=False),
}


async def enter_a_turn_that_is_retained(turn: NeutralTurn) -> ContainmentLost:
    with pytest.raises(ContainmentLost) as lost:
        await turn.enter()
    return lost.value


async def confirm_the_retention_then_check_the_slot_and_home_are_freed(
    turn: NeutralTurn, group: RetainedGroup,
) -> None:
    assert await confirm_the_retained_group_gone(turn.child, group)
    assert not slot_is_held(turn.pool)
    assert not turn.home_is_present()


@pytest.mark.parametrize("result", UNCONFIRMED_RESULTS.values(), ids=UNCONFIRMED_RESULTS)
def test_a_group_that_is_not_counted_empty_in_time_keeps_its_slot_and_home(
    result: KillUntilGoneResult, a_turn, monkeypatch,
) -> None:
    pool = a_pool()
    turn = a_turn(pool, returncode=0, gone=[result])

    async def scenario() -> None:
        lost = await enter_a_turn_that_is_retained(turn)
        try:
            assert lost.group.pgid == turn.child.pid
            assert lost.__cause__ is None
            assert operations(turn.child.log) == [(KILL, turn.child.pid)]
            assert slot_is_held(pool)
            assert turn.home_is_present()
        finally:
            await confirm_the_retention_then_check_the_slot_and_home_are_freed(turn, lost.group)

    with killpg_forbidden(), recorded_census_workers(monkeypatch):
        run_bounded(scenario())


async def publish_the_exit_once_the_group_was_killed(
    child: ScriptedClaudeChild, deadline: ScenarioDeadline,
) -> None:
    while all(name != KILL for name, _pid in operations(child.log)):
        assert deadline.remaining() > 0, "the group was never killed"
        await asyncio.sleep(0)
    child.publish_exit(-signal.SIGKILL)


NEVER_PUBLISHES_ON_KILL = {
    "returncode": None,
    "natural_exit": None,
    "survives": {signal.SIGTERM},
    "exit_on_kill": ExitOnKill.NONE,
}

COMMITS_WITHOUT_ANNOUNCING_ON_KILL = {
    **NEVER_PUBLISHES_ON_KILL,
    "exit_on_kill": ExitOnKill.COMMITTED_UNANNOUNCED,
}


def test_a_confirmed_count_waits_for_the_exit_before_the_slot_is_freed(a_turn) -> None:
    pool = a_pool()
    turn = a_turn(pool, **NEVER_PUBLISHES_ON_KILL)
    deadline = ScenarioDeadline()

    async def scenario() -> None:
        tasks: list[asyncio.Future[Any]] = []
        async with scenario_holds([], tasks, set()):
            publishing = publish_the_exit_once_the_group_was_killed(turn.child, deadline)
            tasks.append(asyncio.ensure_future(publishing))
            await turn.enter()

    with killpg_forbidden():
        run_bounded(scenario())

    assert [name for name, _pid in operations(turn.child.log)] == ["SIGTERM", KILL, "release"]
    assert not slot_is_held(pool)
    assert not turn.home_is_present()


def test_a_confirmed_count_whose_exit_is_never_published_keeps_the_slot_without_a_cause(
    a_turn, monkeypatch,
) -> None:
    pool = a_pool(confirmation_seconds=0)
    turn = a_turn(pool, **NEVER_PUBLISHES_ON_KILL)

    async def scenario() -> None:
        lost = await enter_a_turn_that_is_retained(turn)
        try:
            assert lost.__cause__ is None
            assert [name for name, _pid in operations(turn.child.log)] == ["SIGTERM", KILL]
            assert slot_is_held(pool)
            assert turn.home_is_present()
        finally:
            await confirm_the_retention_then_check_the_slot_and_home_are_freed(turn, lost.group)

    with killpg_forbidden(), recorded_census_workers(monkeypatch):
        run_bounded(scenario())


def test_a_confirmed_count_whose_exit_is_committed_but_not_published_keeps_the_slot(
    a_turn, monkeypatch,
) -> None:
    pool = a_pool(confirmation_seconds=0)
    turn = a_turn(pool, **COMMITS_WITHOUT_ANNOUNCING_ON_KILL)

    async def scenario() -> None:
        lost = await enter_a_turn_that_is_retained(turn)
        try:
            assert lost.__cause__ is None
            assert [name for name, _pid in operations(turn.child.log)] == ["SIGTERM", KILL]
            assert slot_is_held(pool)
            assert turn.home_is_present()
        finally:
            await confirm_the_retention_then_check_the_slot_and_home_are_freed(turn, lost.group)

    with killpg_forbidden(), recorded_census_workers(monkeypatch):
        run_bounded(scenario())


def test_a_lost_anchor_inside_the_count_is_retained_with_the_loss_as_cause(
    a_turn, retentions, monkeypatch,
) -> None:
    pool = a_pool()
    loss = ChildIdentityLost("something else reaped the leader")
    turn = a_turn(pool, returncode=0, gone=[loss])

    async def scenario() -> None:
        lost = await enter_a_turn_that_is_retained(turn)
        try:
            assert lost.__cause__ is loss
            assert operations(turn.child.log) == [(KILL, turn.child.pid)]
            assert slot_is_held(pool)
            assert turn.home_is_present()
            for _ in range(2):
                with pytest.raises(ChildIdentityLost):
                    await lost_groups.confirm_group_gone(lost.group)
            assert slot_is_held(pool)
        finally:
            retentions.free_every_slot()

    with killpg_forbidden(), recorded_census_workers(monkeypatch):
        run_bounded(scenario())

    assert not slot_is_held(pool)
    assert not turn.home_is_present()


class AnInterruption(BaseException):
    pass


def test_an_interruption_of_the_count_is_raised_unchanged_and_is_not_a_retention(
    a_turn, monkeypatch,
) -> None:
    pool = a_pool()
    interruption = AnInterruption()
    turn = a_turn(pool, returncode=0, gone=[interruption])
    reservations: list[Any] = []
    real_reserve = pool.reserve

    def reserve_and_note(home_reservation=None):
        reservation = real_reserve(home_reservation)
        reservations.append(reservation)
        return reservation

    monkeypatch.setattr(pool, "reserve", reserve_and_note)

    async def scenario() -> None:
        try:
            with pytest.raises(AnInterruption) as raised:
                await turn.enter()
            assert raised.value is interruption
        finally:
            for reservation in reservations:
                pool.release(reservation)

    with killpg_forbidden(), recorded_census_workers(monkeypatch):
        run_bounded(scenario())

    assert not slot_is_held(pool)


@pytest.mark.parametrize("field_name", ["sigterm_grace_seconds", "confirmation_seconds"])
@pytest.mark.parametrize("bad_bound", [float("nan"), float("inf"), -1.0], ids=str)
def test_reap_bounds_refuse_a_bound_that_is_not_finite_and_non_negative(
    field_name: str, bad_bound: float,
) -> None:
    bounds = {"sigterm_grace_seconds": 1.0, "confirmation_seconds": 1.0, field_name: bad_bound}

    with pytest.raises(ValueError, match=field_name):
        ReapBounds(**bounds)


def test_reap_bounds_accept_zero() -> None:
    bounds = ReapBounds(sigterm_grace_seconds=0, confirmation_seconds=0)

    assert (bounds.sigterm_grace_seconds, bounds.confirmation_seconds) == (0, 0)


@dataclass(frozen=True)
class FailingStep:
    script: dict[str, Any]
    log: list[str]


FAILING_STEPS: dict[str, Callable[[Exception], FailingStep]] = {
    "sigterm_signalling": lambda failure: FailingStep(
        {"returncode": None, "natural_exit": None, "signal_failure": failure}, ["SIGTERM"],
    ),
    "counting": lambda failure: FailingStep({"returncode": 0, "gone": [failure]}, [KILL]),
    "leader_release": lambda failure: FailingStep(
        {"returncode": 0, "release_failures": [failure]}, [KILL, "release"],
    ),
}


@pytest.mark.parametrize("step", FAILING_STEPS.values(), ids=FAILING_STEPS)
def test_an_ordinary_failure_of_a_reap_step_is_retained_with_its_cause(
    step: Callable[[Exception], FailingStep], a_turn, monkeypatch,
) -> None:
    pool = a_pool()
    failure = OSError("injected")
    expected = step(failure)
    turn = a_turn(pool, **expected.script)

    async def scenario() -> None:
        lost = await enter_a_turn_that_is_retained(turn)
        try:
            assert lost.group.pgid == turn.child.pid
            assert lost.__cause__ is failure
            assert [name for name, _pid in operations(turn.child.log)] == expected.log
            assert slot_is_held(pool)
            assert turn.home_is_present()
        finally:
            await confirm_the_retention_then_check_the_slot_and_home_are_freed(turn, lost.group)

    with killpg_forbidden(), recorded_census_workers(monkeypatch):
        run_bounded(scenario())
