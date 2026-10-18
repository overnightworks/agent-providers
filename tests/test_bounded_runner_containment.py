"""The bounded runner contains its group on real children, with no provider patched.

A ``/bin/sh`` leader starts a Python descendant that ignores SIGTERM, and the test thread owns
that descendant by a pidfd before the runner is allowed to reap. Each row reads the
descendant's death at the runner's return, before any cleanup kill of its own.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

import pytest
from anchored_support import TEST_WAIT_SECONDS, ChildCleanup
from descendant_support import DescendantRow, Handshake, lingering_descendant

from agent_providers.process import CliRunReason, run_catalog_cli, run_cli_bounded
from agent_providers.spawn import ChildProcess, closed_environment

A_READ_DEADLINE_SECONDS = 2.0
IGNORES_SIGTERM_ITSELF = "trap '' TERM\n"


def a_leader_with_a_descendant(
    directory: Path,
    handshake: Handshake,
    *,
    blocks: bool = False,
    escapes: bool = False,
    ignores_sigterm: bool = False,
) -> ChildProcess:
    script = lingering_descendant(
        "", exit_code=0, blocks=blocks, escapes=escapes, handshake=handshake,
    )
    return ChildProcess(
        binary=Path("/bin/sh"),
        arguments=("-c", (IGNORES_SIGTERM_ITSELF if ignores_sigterm else "") + script),
        environment=closed_environment(directory),
        working_directory=directory,
    )


@dataclass
class BoundedRun:
    spawned: list[int] = field(default_factory=list)
    reaped: list[tuple[int, bool]] = field(default_factory=list)


def run_bounded(child: ChildProcess, run: BoundedRun, deadline_seconds: float):
    return run_cli_bounded(
        child,
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + deadline_seconds,
        on_spawned=run.spawned.append,
        on_reaped=lambda process_id, became_zombie: run.reaped.append((process_id, became_zombie)),
    )


@pytest.fixture
def row(handshake: Handshake, child_cleanup: ChildCleanup) -> Iterator[DescendantRow]:
    with DescendantRow(handshake, child_cleanup) as row:
        yield row


def _complete_through_run_cli_bounded(child: ChildProcess, row: DescendantRow) -> BoundedRun:
    run = BoundedRun()
    outcome = row.run_beside(lambda: run_bounded(child, run, TEST_WAIT_SECONDS))
    assert outcome.complete
    assert outcome.became_zombie is False
    return run


def _complete_through_run_catalog_cli(child: ChildProcess, row: DescendantRow) -> BoundedRun:
    catalog_run = row.run_beside(lambda: run_catalog_cli(child))
    assert catalog_run is not None
    assert catalog_run.complete
    return BoundedRun()


@pytest.mark.parametrize(
    ("complete_through", "calls_back"),
    [
        pytest.param(_complete_through_run_cli_bounded, True, id="run_cli_bounded"),
        pytest.param(_complete_through_run_catalog_cli, False, id="run_catalog_cli"),
    ],
)
def test_a_run_that_ends_normally_still_kills_the_descendants_of_its_group(
    tmp_path: Path,
    handshake: Handshake,
    row: DescendantRow,
    complete_through: Callable[[ChildProcess, DescendantRow], BoundedRun],
    calls_back: bool,
) -> None:
    run = complete_through(a_leader_with_a_descendant(tmp_path, handshake), row)

    assert row.dead_at_return is True
    if calls_back:
        assert len(run.spawned) == 1
        assert run.reaped == [(run.spawned[0], False)]


def test_a_leader_that_ignores_sigterm_and_blocks_is_killed_with_its_descendant_at_the_deadline(
    tmp_path: Path, handshake: Handshake, row: DescendantRow,
) -> None:
    run = BoundedRun()
    child = a_leader_with_a_descendant(tmp_path, handshake, blocks=True, ignores_sigterm=True)

    outcome = row.run_beside(lambda: run_bounded(child, run, A_READ_DEADLINE_SECONDS))

    assert outcome.reason is CliRunReason.DEADLINE_WHILE_READING
    assert outcome.returncode == -signal.SIGKILL
    assert outcome.became_zombie is False
    assert row.dead_at_return is True
    assert run.reaped == [(run.spawned[0], False)]


def test_a_descendant_that_left_the_group_is_not_counted_and_keeps_running(
    tmp_path: Path, handshake: Handshake, row: DescendantRow,
) -> None:
    run = BoundedRun()
    child = a_leader_with_a_descendant(tmp_path, handshake, escapes=True)

    outcome = row.run_beside(lambda: run_bounded(child, run, TEST_WAIT_SECONDS))

    assert outcome.complete
    assert outcome.became_zombie is False
    assert row.dead_at_return is False
    assert run.reaped == [(run.spawned[0], False)]


@contextlib.contextmanager
def kernel_spy() -> Iterator[list[tuple]]:
    events: list[tuple] = []
    killpg = os.killpg
    waitpid = os.waitpid

    def spy_on_killpg(process_group: int, signal_number: int) -> None:
        events.append(("signal", process_group, signal_number))
        killpg(process_group, signal_number)

    def spy_on_waitpid(process_id: int, options: int) -> tuple[int, int]:
        answer = waitpid(process_id, options)
        if answer[0] != 0:
            events.append(("reaped", answer[0]))
        return answer

    with (
        mock.patch.object(os, "killpg", spy_on_killpg),
        mock.patch.object(os, "waitpid", spy_on_waitpid),
    ):
        yield events


def test_the_group_is_signalled_while_the_leader_is_still_unreaped_and_never_after_the_reap(
    tmp_path: Path, handshake: Handshake, row: DescendantRow,
) -> None:
    run = BoundedRun()
    child = a_leader_with_a_descendant(tmp_path, handshake)

    with kernel_spy() as events:
        row.run_beside(lambda: run_bounded(child, run, TEST_WAIT_SECONDS))
    (leader,) = run.spawned

    signals = [index for index, event in enumerate(events) if event[:2] == ("signal", leader)]
    reaps = [index for index, event in enumerate(events) if event == ("reaped", leader)]
    assert ("signal", leader, signal.SIGKILL) in events
    assert len(reaps) == 1
    assert max(signals) < reaps[0], events
