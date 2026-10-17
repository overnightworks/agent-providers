"""The session zombie anchor holds real children by pidfd and fails the session for the rest."""

from __future__ import annotations

import ast
import errno
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import anchored_support
import pytest
from anchored_support import (
    NOBODY,
    RUNNING,
    TEST_WAIT_SECONDS,
    UNREPORTED,
    ChildErrorKind,
    Exited,
    FakeHandle,
    OwnedChild,
    Recipient,
    SessionCleanupFailed,
    ZombieAnchor,
    state,
    zombie_anchor_session,
)

from agent_providers.spawn import closed_environment

SUPPORT_MODULE = Path(anchored_support.__file__)
EXIT_WITH_LINE = (
    "import sys\nline = sys.stdin.readline()\nsys.exit(int(line) if line.strip() else 0)\n"
)
FORBIDDEN_NUMBERED_OPERATIONS = {"kill", "killpg", "waitpid", "wait", "wait3", "wait4"}
KERNEL_BINDINGS = {"_pidfd_open", "_pidfd_send_signal", "_waitid", "_wait_for_exits"}
SIGKILLED = -9


class Cell:
    """One test's children and anchors; everything unresolved goes to the session's anchor."""

    def __init__(self, tmp_path: Path, session_anchor: ZombieAnchor) -> None:
        self._tmp_path = tmp_path
        self._session_anchor = session_anchor
        self._anchors: list[ZombieAnchor] = []
        self._stdin_writers: dict[int, int] = {}

    def anchor(self) -> ZombieAnchor:
        return self.track(ZombieAnchor())

    def track(self, anchor: ZombieAnchor) -> ZombieAnchor:
        self._anchors.append(anchor)
        return anchor

    def start_child(self, recipient: Recipient | FakeHandle = NOBODY) -> OwnedChild:
        stdin_reader, stdin_writer = os.pipe()
        popen = subprocess.Popen(
            [sys.executable, "-I", "-c", EXIT_WITH_LINE],
            stdin=stdin_reader,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=closed_environment(self._tmp_path),
            cwd=self._tmp_path,
            start_new_session=True,
        )
        os.close(stdin_reader)
        self._stdin_writers[popen.pid] = stdin_writer
        return OwnedChild.name(popen, recipient)

    def tell_to_exit_with(self, owned: OwnedChild, code: int) -> None:
        os.write(self._stdin_writers[owned.pid], f"{code}\n".encode())

    def close(self) -> None:
        for stdin_writer in self._stdin_writers.values():
            os.close(stdin_writer)
        for anchor in self._anchors:
            anchor.hand_over(self._session_anchor)


@pytest.fixture
def cell(tmp_path: Path, zombie_anchor: ZombieAnchor) -> Iterator[Cell]:
    own_cell = Cell(tmp_path, zombie_anchor)
    yield own_cell
    own_cell.close()


def error_pairs(
    failure: pytest.ExceptionInfo[SessionCleanupFailed],
) -> set[tuple[int, ChildErrorKind]]:
    return {(error.pid, error.kind) for error in failure.value.errors}


def test_the_support_module_names_no_pid_number() -> None:
    tree = ast.parse(SUPPORT_MODULE.read_text())
    numbered_operations = [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and node.attr in FORBIDDEN_NUMBERED_OPERATIONS
    ]
    imported_numbered_operations = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "os"
        for alias in node.names
        if alias.name in FORBIDDEN_NUMBERED_OPERATIONS
    ]
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    calls_on_a_popen = [
        call
        for call in calls
        if isinstance(call.func, ast.Attribute) and _names_a_popen(call.func.value)
    ]
    waitid_calls = [
        call for call in calls if isinstance(call.func, ast.Name) and call.func.id == "_waitid"
    ]
    waitid_first_arguments = {ast.unparse(call.args[0]) for call in waitid_calls}
    module_level_names = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    } | {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}

    assert numbered_operations == []
    assert imported_numbered_operations == []
    assert calls_on_a_popen == []
    assert waitid_calls
    assert waitid_first_arguments == {"P_PIDFD"}
    assert KERNEL_BINDINGS <= module_level_names


def _names_a_popen(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "popen") or (
        isinstance(node, ast.Attribute) and node.attr == "popen"
    )


def test_unreported_child_and_unreachable_handle_are_reaped_at_the_session_end(cell: Cell) -> None:
    anchor = cell.anchor()
    cyclic_handle = FakeHandle()
    children = [
        cell.start_child(NOBODY),
        cell.start_child(UNREPORTED),
        cell.start_child(cyclic_handle),
    ]
    for owned in children:
        anchor.hold(owned)
    cyclic_handle.cycle = cyclic_handle
    del cyclic_handle

    anchor.finalize()

    assert [owned.popen.returncode for owned in children] == [SIGKILLED] * 3


def test_a_child_reaped_elsewhere_resolves_and_a_waiting_popen_is_reported_and_disarmed(
    cell: Cell,
) -> None:
    anchor = cell.anchor()
    first, second = cell.start_child(), cell.start_child()
    for owned in (first, second):
        anchor.hold(owned)
        cell.tell_to_exit_with(owned, 3)
        assert owned.popen.wait(timeout=TEST_WAIT_SECONDS) == 3
    second.popen.returncode = None

    with pytest.raises(SessionCleanupFailed) as failure:
        anchor.finalize()
    anchor.finalize()

    assert error_pairs(failure) == {(second.pid, ChildErrorKind.REAPED_WHILE_POPEN_WAITED)}
    assert len(failure.value.errors) == 1
    assert first.popen.returncode == 3
    assert second.popen.returncode is anchored_support.DISARMED


def test_a_refused_kill_and_a_handle_still_holding_fail_the_session_and_stay_held(
    cell: Cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor, successor = cell.anchor(), cell.anchor()
    handle = FakeHandle()
    held_by_handle, refused = cell.start_child(handle), cell.start_child()
    anchor.hold(held_by_handle)
    anchor.hold(refused)
    send_signal = anchored_support._pidfd_send_signal

    def refuse_one_pidfd(pidfd: int, signal_number: int) -> None:
        if pidfd == refused.pidfd:
            raise OSError(errno.EPERM, os.strerror(errno.EPERM))
        send_signal(pidfd, signal_number)

    with monkeypatch.context() as refusing:
        refusing.setattr(anchored_support, "_pidfd_send_signal", refuse_one_pidfd)
        with pytest.raises(SessionCleanupFailed) as first_failure:
            anchor.finalize(bound=0.2)
    states_after_failure = (state(held_by_handle), state(refused))
    handle.done_with_pid = True
    anchor.hand_over(successor)
    anchor.finalize()
    successor.finalize()

    assert error_pairs(first_failure) == {
        (held_by_handle.pid, ChildErrorKind.HANDLE_STILL_HOLDS_PID),
        (refused.pid, ChildErrorKind.KILL_REFUSED),
        (refused.pid, ChildErrorKind.NOT_DEAD_AT_THE_BOUND),
    }
    assert states_after_failure == (Exited(SIGKILLED), RUNNING)
    assert held_by_handle.popen.returncode == SIGKILLED
    assert refused.popen.returncode == SIGKILLED


def test_the_session_generator_fails_the_session_for_a_handle_still_holding(cell: Cell) -> None:
    session = zombie_anchor_session()
    anchor = cell.track(next(session))
    handle = FakeHandle()
    unheld, held_by_handle = cell.start_child(), cell.start_child(handle)
    anchor.hold(unheld)
    anchor.hold(held_by_handle)

    with pytest.raises(SessionCleanupFailed) as failure:
        next(session, None)
    states_after_failure = state(held_by_handle)
    handle.done_with_pid = True
    anchor.finalize()

    assert [(error.pid, error.kind) for error in failure.value.errors] == [
        (held_by_handle.pid, ChildErrorKind.HANDLE_STILL_HOLDS_PID)
    ]
    assert states_after_failure == Exited(SIGKILLED)
    assert unheld.popen.returncode == SIGKILLED
    assert held_by_handle.popen.returncode == SIGKILLED
