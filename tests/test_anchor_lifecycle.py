import itertools
import os

import pytest

from agent_providers.spawn import (
    AnchorState,
    AnchorStateError,
    ChildIdentityLost,
    LeaderEvent,
    LeaderExited,
    LeaderGone,
    LeaderNotReaped,
    LeaderReaped,
    LeaderStatus,
    LeaderStopped,
    admit_group_signal,
    admit_release,
    advance,
    exit_observation,
    reap_outcome,
)

PID = 4242
EXIT_WITH_THREE = 0x300
EXIT_WITH_ZERO = 0x0
KILLED_BY_NINE = 0x9
DUMPED_BY_ELEVEN = 0x8B
STOPPED_BY_TRACE_TRAP = 0x57F
STOPPED_BY_STOP_SIGNAL = 0x137F
CONTINUED = 0xFFFF
NOT_A_STATUS = 0x1FF

UNKNOWN_CODE = 99

RUNNING = AnchorState.RUNNING
EXITED = AnchorState.EXITED
RELEASED = AnchorState.RELEASED
LOST = AnchorState.LOST

ALL_EVENTS_WITH_THREE: list[LeaderEvent] = [
    LeaderExited(3),
    LeaderStopped(),
    LeaderReaped(3),
    LeaderNotReaped(),
    LeaderGone(),
]


@pytest.mark.parametrize(
    ("si_code", "si_status", "expected"),
    [
        (os.CLD_EXITED, 0, LeaderExited(0)),
        (os.CLD_EXITED, 3, LeaderExited(3)),
        (os.CLD_KILLED, 9, LeaderExited(-9)),
        (os.CLD_DUMPED, 11, LeaderExited(-11)),
        (os.CLD_TRAPPED, 5, LeaderStopped()),
        (os.CLD_STOPPED, 19, LeaderStopped()),
        (os.CLD_CONTINUED, 18, ValueError),
        (UNKNOWN_CODE, 0, ValueError),
    ],
)
def test_wait_report_ends_the_leader_only_for_an_exit_a_kill_or_a_dump(
    si_code, si_status, expected
):
    if expected is ValueError:
        with pytest.raises(ValueError):
            exit_observation(si_code, si_status)
    else:
        assert exit_observation(si_code, si_status) == expected


@pytest.mark.parametrize(
    ("reaped_pid", "wait_status", "expected"),
    [
        (0, 0, LeaderNotReaped()),
        (PID, EXIT_WITH_THREE, LeaderReaped(3)),
        (PID, EXIT_WITH_ZERO, LeaderReaped(0)),
        (PID, KILLED_BY_NINE, LeaderReaped(-9)),
        (PID, DUMPED_BY_ELEVEN, LeaderReaped(-11)),
        (PID, STOPPED_BY_TRACE_TRAP, LeaderNotReaped()),
        (PID, STOPPED_BY_STOP_SIGNAL, LeaderNotReaped()),
        (PID, CONTINUED, LeaderNotReaped()),
        (PID, NOT_A_STATUS, ValueError),
    ],
)
def test_reap_releases_the_leader_only_for_a_collected_exit_or_kill(
    reaped_pid, wait_status, expected
):
    if expected is ValueError:
        with pytest.raises(ValueError):
            reap_outcome(reaped_pid, wait_status)
    else:
        assert reap_outcome(reaped_pid, wait_status) == expected


def status_in(state: AnchorState) -> LeaderStatus:
    carries_code = state in (EXITED, RELEASED)
    return LeaderStatus(state, 7 if carries_code else None)


def transition(state: AnchorState, returncode: int | None, announces: bool = False):
    return (LeaderStatus(state, returncode), announces)


UNCHANGED = None

TRANSITIONS = {
    (RUNNING, LeaderExited(3)): transition(EXITED, 3, True),
    (RUNNING, LeaderStopped()): UNCHANGED,
    (RUNNING, LeaderReaped(3)): transition(RELEASED, 3, True),
    (RUNNING, LeaderNotReaped()): UNCHANGED,
    (RUNNING, LeaderGone()): transition(LOST, None, True),
    (EXITED, LeaderExited(3)): UNCHANGED,
    (EXITED, LeaderStopped()): UNCHANGED,
    (EXITED, LeaderReaped(3)): transition(RELEASED, 3),
    (EXITED, LeaderNotReaped()): UNCHANGED,
    (EXITED, LeaderGone()): transition(LOST, 7),
    **{(state, event): UNCHANGED for state in (RELEASED, LOST) for event in ALL_EVENTS_WITH_THREE},
}


@pytest.mark.parametrize(
    ("state", "event"),
    list(itertools.product(AnchorState, ALL_EVENTS_WITH_THREE)),
    ids=lambda value: type(value).__name__ if not isinstance(value, AnchorState) else value.name,
)
def test_every_state_meets_every_event_with_the_designed_transition(state, event):
    before = status_in(state)
    expected = TRANSITIONS[(state, event)]
    expected_status, expected_announces = (before, False) if expected is UNCHANGED else expected

    result = advance(before, event)

    assert (result.status, result.announces) == (expected_status, expected_announces)


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        (RUNNING, None),
        (EXITED, None),
        (RELEASED, AnchorStateError),
        (LOST, ChildIdentityLost),
    ],
)
def test_group_signal_is_admitted_only_while_the_leader_is_anchored(state, outcome):
    if outcome is None:
        admit_group_signal(state)
    else:
        with pytest.raises(outcome):
            admit_group_signal(state)


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        (RUNNING, True),
        (EXITED, True),
        (RELEASED, False),
        (LOST, ChildIdentityLost),
    ],
)
def test_release_reaps_an_anchored_leader_and_refuses_a_lost_one(state, outcome):
    if isinstance(outcome, bool):
        assert admit_release(state) is outcome
    else:
        with pytest.raises(outcome):
            admit_release(state)


EVENT_SEQUENCES = [
    sequence
    for length in (1, 2, 3)
    for sequence in itertools.product(ALL_EVENTS_WITH_THREE, repeat=length)
]


def test_sequences_cover_one_hundred_fifty_five_paths():
    assert len(EVENT_SEQUENCES) == 155


@pytest.mark.parametrize(
    "events", EVENT_SEQUENCES, ids=lambda events: "-".join(type(e).__name__[6:] for e in events)
)
def test_every_path_announces_once_and_stays_ended(events):
    status = LeaderStatus(RUNNING, None)
    announcements = 0
    for event in events:
        step = advance(status, event)
        announcements += step.announces
        if status.state in (RELEASED, LOST):
            assert step.status == status
        status = step.status

    assert announcements == (0 if status.state is RUNNING else 1)
    carries_code = any(isinstance(event, (LeaderExited, LeaderReaped)) for event in events)
    if not carries_code:
        assert status.returncode is None
