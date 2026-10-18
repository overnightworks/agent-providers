"""The session zombie anchor holds real children by pidfd and fails the session for the rest."""

from __future__ import annotations

import ast
import errno
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import anchored_support
import pytest
from anchored_support import (
    NOBODY,
    P_PIDFD,
    RUNNING,
    SIGKILLED,
    TEST_WAIT_SECONDS,
    UNREPORTED,
    ChildErrorKind,
    Exited,
    FakeHandle,
    OwnedChild,
    Recipient,
    ResolvedChildTouched,
    SessionCleanupFailed,
    ZombieAnchor,
    kill_children,
    resolve_children,
    state,
    zombie_anchor_session,
)

from agent_providers.spawn import closed_environment

SUPPORT_MODULE = Path(anchored_support.__file__)
EXIT_WITH_LINE = (
    "import sys\nline = sys.stdin.readline()\nsys.exit(int(line) if line.strip() else 0)\n"
)
FORBIDDEN_NUMBERED_OPERATIONS = {"kill", "killpg", "waitpid", "wait", "wait3", "wait4"}
KERNEL_BINDINGS = {"pidfd_open", "_pidfd_send_signal", "_waitid", "wait_for_exits"}


class Cell:
    """One test's children and anchors; everything unresolved goes to the session's anchor."""

    def __init__(self, tmp_path: Path, session_anchor: ZombieAnchor) -> None:
        self._tmp_path = tmp_path
        self._session_anchor = session_anchor
        self._anchors: list[ZombieAnchor] = []
        self._stdin_writers: dict[int, int] = {}
        self._witnesses: dict[int, int] = {}
        self.unnamed: list[subprocess.Popen] = []

    def anchor(self) -> ZombieAnchor:
        return self.track(ZombieAnchor())

    def track(self, anchor: ZombieAnchor) -> ZombieAnchor:
        self._anchors.append(anchor)
        return anchor

    def start_child(
        self, anchor: ZombieAnchor, recipient: Recipient | FakeHandle = NOBODY
    ) -> OwnedChild:
        stdin_reader, stdin_writer = os.pipe()
        try:
            popen = subprocess.Popen(
                [sys.executable, "-I", "-c", EXIT_WITH_LINE],
                stdin=stdin_reader,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=closed_environment(self._tmp_path),
                cwd=self._tmp_path,
                start_new_session=True,
            )
        except OSError:
            os.close(stdin_writer)
            raise
        finally:
            os.close(stdin_reader)
        try:
            owned = OwnedChild.name(popen, recipient)
        except OSError:
            self._reap_unnamed(popen, stdin_writer)
            raise
        self._stdin_writers[popen.pid] = stdin_writer
        anchor.hold(owned)
        self._witnesses[popen.pid] = os.dup(owned.pidfd)
        return owned

    def _reap_unnamed(self, popen: subprocess.Popen, stdin_writer: int) -> None:
        # No pidfd exists for this child, so EOF on its stdin ends it and the Popen, the only
        # object that knows its number, is the one thing that can reap it.
        self.unnamed.append(popen)
        os.close(stdin_writer)
        popen.wait(timeout=TEST_WAIT_SECONDS)

    def tell_to_exit_with(self, owned: OwnedChild, code: int) -> None:
        os.write(self._stdin_writers[owned.pid], f"{code}\n".encode())

    def witness_descriptors(self) -> set[int]:
        return set(self._witnesses.values())

    def assert_reaped(self, owned: OwnedChild) -> None:
        with pytest.raises(ChildProcessError):
            os.waitid(P_PIDFD, self._witnesses[owned.pid], os.WEXITED | os.WNOHANG | os.WNOWAIT)

    def close(self) -> None:
        for descriptor in [*self._stdin_writers.values(), *self._witnesses.values()]:
            os.close(descriptor)
        for anchor in self._anchors:
            anchor.hand_over(self._session_anchor)


def open_pidfds() -> set[int]:
    descriptors = set()
    for entry in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{entry}") == "anon_inode:[pidfd]":
                descriptors.add(int(entry))
        except FileNotFoundError:
            continue
    return descriptors


def child_pids() -> set[int]:
    children = Path(f"/proc/self/task/{os.getpid()}/children").read_text()
    return {int(pid) for pid in children.split()}


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
        cell.start_child(anchor, NOBODY),
        cell.start_child(anchor, UNREPORTED),
        cell.start_child(anchor, cyclic_handle),
    ]
    cyclic_handle.cycle = cyclic_handle
    del cyclic_handle

    anchor.finalize()

    assert [owned.popen.returncode for owned in children] == [SIGKILLED] * 3
    for owned in children:
        cell.assert_reaped(owned)


def test_a_child_reaped_elsewhere_resolves_and_a_waiting_popen_is_reported_and_disarmed(
    cell: Cell,
) -> None:
    anchor = cell.anchor()
    first, second = cell.start_child(anchor), cell.start_child(anchor)
    for owned in (first, second):
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
    cell.assert_reaped(first)
    cell.assert_reaped(second)


def test_a_refused_kill_and_a_handle_still_holding_fail_the_session_and_stay_held(
    cell: Cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor, successor = cell.anchor(), cell.anchor()
    handle = FakeHandle()
    held_by_handle, refused = cell.start_child(anchor, handle), cell.start_child(anchor)
    send_signal = anchored_support._pidfd_send_signal

    def refuse_one_pidfd(pidfd: int, signal_number: int) -> None:
        if pidfd == refused.pidfd:
            raise OSError(errno.EPERM, os.strerror(errno.EPERM))
        send_signal(pidfd, signal_number)

    assert kill_children([held_by_handle]) == []
    assert held_by_handle.pidfd in anchored_support.wait_for_exits(
        [held_by_handle.pidfd], TEST_WAIT_SECONDS
    )

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
    cell.assert_reaped(held_by_handle)
    cell.assert_reaped(refused)


def test_the_session_generator_fails_the_session_for_a_handle_still_holding(cell: Cell) -> None:
    session = zombie_anchor_session()
    anchor = cell.track(next(session))
    handle = FakeHandle()
    unheld, held_by_handle = cell.start_child(anchor), cell.start_child(anchor, handle)

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
    cell.assert_reaped(unheld)
    cell.assert_reaped(held_by_handle)


def test_an_unreported_child_stays_unreaped_until_the_session_end(cell: Cell) -> None:
    anchor = cell.anchor()
    unreported = cell.start_child(anchor, UNREPORTED)

    kill_children([unreported])
    errors = resolve_children(
        [unreported], deadline=time.monotonic() + TEST_WAIT_SECONDS, session_ended=False
    )
    unresolved_state = state(unreported)
    anchor.finalize()

    assert errors == []
    assert unresolved_state == Exited(SIGKILLED)
    cell.assert_reaped(unreported)


@pytest.mark.parametrize(
    "touch",
    [
        lambda record: kill_children([record]),
        lambda record: resolve_children(
            [record], deadline=time.monotonic() + TEST_WAIT_SECONDS, session_ended=True
        ),
        state,
    ],
    ids=["kill", "resolve", "state"],
)
def test_a_resolved_record_is_rejected_and_never_reaches_a_child_reusing_its_descriptor(
    cell: Cell, touch: Callable[[OwnedChild], object]
) -> None:
    anchor = cell.anchor()
    resolved = cell.start_child(anchor)
    anchor.finalize()
    later = cell.start_child(anchor)
    assert later.pidfd == resolved.pidfd

    with pytest.raises(ResolvedChildTouched):
        touch(resolved)

    assert state(later) is RUNNING
    anchor.finalize()
    assert later.popen.returncode == SIGKILLED
    cell.assert_reaped(later)


def test_a_failed_naming_leaves_no_zombie_and_no_open_pidfd(
    cell: Cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = cell.anchor()
    pidfds_before = open_pidfds()
    open_pidfd = anchored_support.pidfd_open
    calls = []

    def fail_the_second_open(pid: int) -> int:
        calls.append(pid)
        if len(calls) == 2:
            raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
        return open_pidfd(pid)

    monkeypatch.setattr(anchored_support, "pidfd_open", fail_the_second_open)
    named = cell.start_child(anchor)
    with pytest.raises(OSError, match="Function not implemented"):
        cell.start_child(anchor)
    monkeypatch.undo()
    anchor.finalize()

    (unnamed,) = cell.unnamed
    assert unnamed.returncode == 0
    cell.assert_reaped(named)
    assert open_pidfds() - cell.witness_descriptors() == pidfds_before


def test_a_failed_witness_duplication_leaves_no_zombie_and_no_open_pidfd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_anchor = ZombieAnchor()
    own_cell = Cell(tmp_path, session_anchor)
    pidfds_before = open_pidfds()
    children_before = child_pids()
    anchor = own_cell.anchor()

    def exhausted(descriptor: int) -> int:
        raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))

    try:
        with monkeypatch.context() as exhausting:
            exhausting.setattr(os, "dup", exhausted)
            with pytest.raises(OSError, match="Too many open files"):
                own_cell.start_child(anchor)
    finally:
        try:
            own_cell.close()
        finally:
            session_anchor.finalize()

    assert child_pids() == children_before
    assert open_pidfds() == pidfds_before
