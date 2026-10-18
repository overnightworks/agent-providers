"""``group_census``: parser, liveness, preconditions, scan and the real kernel."""

from __future__ import annotations

import dataclasses
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest
from provider_test_support import PROC_DEVICE, mountinfo_line

from agent_providers import procfs
from agent_providers.procfs import (
    GroupCensus,
    ProcessStat,
    RealProcView,
    UnparsableStatError,
    group_census,
    holds_mount_point,
    parse_stat,
)
from agent_providers.spawn import ChildProcess, closed_environment, open_pipes

OBSERVATION_DEADLINE_SECONDS = 10.0
CLEANUP_ALLOWANCE_SECONDS = 10.0
STUCK_CHILD_DEADLINE_SECONDS = 0.5
REPORT_FLAGS = os.WEXITED | os.WNOHANG | os.WNOWAIT
OWN_PID = 42
PGID = 500
OTHER_GROUP = 600
STAT_FIELDS_AFTER_COMMAND = 50
WAIT_FOR_STDIN = "import sys; sys.stdin.read()"
NON_UTF8_NAME_THEN_WAIT_FOR_STDIN = (
    "import ctypes, os, sys\n"
    "ctypes.CDLL(None).prctl(15, b'ab\\xff\\xfe')\n"
    "os.write(1, b'r')\n"
    "sys.stdin.read()"
)
CLOSE_STDOUT_THEN_WAIT_FOR_STDIN = "import os, sys; os.close(1); sys.stdin.read()"

requires_linux_proc = pytest.mark.skipif(
    not Path("/proc/self/mountinfo").exists(),
    reason="the real view needs a Linux /proc",
)


def stat_text(
    *,
    comm: str | bytes = "worker",
    state: str = "S",
    pgrp: int | str = PGID,
    threads: int | str = 1,
) -> bytes:
    fields = ["0"] * STAT_FIELDS_AFTER_COMMAND
    fields[0], fields[2], fields[17] = state, str(pgrp), str(threads)
    command = comm.encode() if isinstance(comm, str) else comm
    return f"{OWN_PID} (".encode() + command + f") {' '.join(fields)}\n".encode()


def own_status(*name_space_pid_line: str) -> str:
    return "Name:\tpython\n" + "".join(f"{line}\n" for line in name_space_pid_line)


@dataclasses.dataclass
class ScriptedView:
    pid: int | OSError = OWN_PID
    device: str | OSError = PROC_DEVICE
    mountinfo_text: str | OSError = dataclasses.field(default_factory=mountinfo_line)
    status_text: str | OSError = dataclasses.field(
        default_factory=lambda: own_status(f"NSpid:\t{OWN_PID}"),
    )
    listing: tuple[int, ...] | OSError = (PGID,)
    stats: dict[int, bytes | OSError] = dataclasses.field(
        default_factory=lambda: {PGID: stat_text(state="Z")},
    )

    def own_pid(self) -> int:
        return _delivered(self.pid)

    def proc_device(self) -> str:
        return _delivered(self.device)

    def mountinfo(self) -> str:
        return _delivered(self.mountinfo_text)

    def own_status(self) -> str:
        return _delivered(self.status_text)

    def process_ids(self) -> tuple[int, ...]:
        return _delivered(self.listing)

    def stat(self, pid: int) -> bytes:
        return _delivered(self.stats.get(pid, FileNotFoundError(pid)))


def _delivered[Value](value: Value | OSError) -> Value:
    if isinstance(value, OSError):
        raise value
    return value


def scripted_view(**overrides: object) -> ScriptedView:
    return dataclasses.replace(ScriptedView(), **overrides)


def view_with_members(*members: tuple[int, bytes | OSError]) -> ScriptedView:
    return scripted_view(
        listing=(PGID, *(pid for pid, _ in members)),
        stats={PGID: stat_text(state="Z"), **dict(members)},
    )


COMMAND_NAMES = {
    "spaces": "a b c",
    "a closing parenthesis": "a)b",
    "an opening parenthesis": "a(b",
    "a newline": "a\nb",
    "a fake tail": "x) S 9 9 (y",
    "bytes that are not UTF-8": b"ab\xff\xfe",
    "bytes that are not UTF-8 beside a parenthesis": b"\xff)\n(\xfe",
}


@pytest.mark.parametrize("comm", COMMAND_NAMES.values(), ids=COMMAND_NAMES.keys())
def test_a_stat_whose_command_name_is_awkward_is_still_read_after_the_last_parenthesis(comm):
    parsed = parse_stat(stat_text(comm=comm, state="D", pgrp=77, threads=3))

    assert parsed == ProcessStat(state="D", pgrp=77, threads=3)


@pytest.mark.parametrize("state", list("RSDTtZXIPKW"))
def test_every_kernel_state_letter_is_read_as_the_state(state):
    assert parse_stat(stat_text(state=state)).state == state


def test_a_stat_whose_group_is_gone_reads_as_minus_one():
    assert parse_stat(stat_text(pgrp=-1)).pgrp == -1


UNPARSABLE_STATS = {
    "a truncated file": b"42 (worker) S 1 1",
    "exactly one field short of the thread count": b"42 (worker) " + b" ".join([b"0"] * 17),
    "a file without a closing parenthesis": b"42 worker S " + b"0 " * 50,
    "a non-ASCII digit group": stat_text(pgrp="\u0663"),
    "non-ASCII text after the command name": stat_text(state="\u00e9"),
    "an empty state field": stat_text(state=""),
    "a non-number group": stat_text(pgrp="x"),
    "a non-number thread count": stat_text(threads="x"),
    "a group wider than any kernel integer": stat_text(pgrp="9" * 4301),
    "a thread count wider than any kernel integer": stat_text(threads="9" * 4301),
    "an empty file": b"",
}


@pytest.mark.parametrize("contents", UNPARSABLE_STATS.values(), ids=UNPARSABLE_STATS.keys())
def test_an_unparsable_stat_is_refused_by_the_parser(contents):
    with pytest.raises(UnparsableStatError):
        parse_stat(contents)


@pytest.mark.parametrize("contents", UNPARSABLE_STATS.values(), ids=UNPARSABLE_STATS.keys())
def test_an_unparsable_stat_is_never_an_empty_group(contents):
    view = view_with_members((PGID + 1, contents))

    assert group_census(view, PGID) is GroupCensus.PROC_UNREADABLE


DEAD_STATES = [("Z", 0), ("Z", 1), ("X", 0), ("X", 1)]
LIVE_STATES = [
    ("Z", 2),
    ("X", 2),
    *((state, threads) for state in "RSDTtIPKW?q" for threads in (1, 3)),
]


@pytest.mark.parametrize(
    ("state", "threads", "expected"),
    [(*row, GroupCensus.EMPTY) for row in DEAD_STATES]
    + [(*row, GroupCensus.LIVE) for row in LIVE_STATES],
)
def test_a_process_is_live_unless_it_is_an_exited_single_thread(state, threads, expected):
    view = scripted_view(stats={PGID: stat_text(state=state, threads=threads)})

    assert group_census(view, PGID) is expected


HIDEPID_VALUES = ["1", "2", "4", "noaccess", "invisible", "ptraceable"]
STACKED_PROC_ELSEWHERE = mountinfo_line(device="0:99", filesystem_type="tmpfs")

PRECONDITION_ROWS = {
    "no proc mount of the device": (
        {"mountinfo_text": mountinfo_line(device="0:99")},
        GroupCensus.NOT_PROCFS,
    ),
    "another filesystem type": (
        {"mountinfo_text": mountinfo_line(filesystem_type="tmpfs")},
        GroupCensus.NOT_PROCFS,
    ),
    "another mount point": (
        {"mountinfo_text": mountinfo_line(mount_point="/mnt")},
        GroupCensus.NOT_PROCFS,
    ),
    "the matching line is not a procfs while another is": (
        {
            "mountinfo_text": "\n".join(
                [mountinfo_line(device="0:99"), mountinfo_line(filesystem_type="tmpfs")],
            ),
        },
        GroupCensus.NOT_PROCFS,
    ),
    **{
        f"hidepid={value}": (
            {"mountinfo_text": mountinfo_line(super_options=f"rw,hidepid={value}")},
            GroupCensus.PIDS_HIDDEN,
        )
        for value in HIDEPID_VALUES
    },
    "two namespace fields, own pid first": (
        {"status_text": own_status("NSpid:\t42\t1")},
        GroupCensus.OTHER_PID_NAMESPACE,
    ),
    "two namespace fields, own pid last": (
        {"status_text": own_status("NSpid:\t9042\t42")},
        GroupCensus.OTHER_PID_NAMESPACE,
    ),
    "another own pid": (
        {"status_text": own_status("NSpid:\t7")},
        GroupCensus.OTHER_PID_NAMESPACE,
    ),
    "no namespace line": ({"status_text": own_status()}, GroupCensus.OTHER_PID_NAMESPACE),
    "unreadable own status": (
        {"status_text": PermissionError("status")},
        GroupCensus.OTHER_PID_NAMESPACE,
    ),
    "unreadable mountinfo": (
        {"mountinfo_text": PermissionError("mountinfo")},
        GroupCensus.PROC_UNREADABLE,
    ),
    "unobtainable device": ({"device": OSError("stat")}, GroupCensus.PROC_UNREADABLE),
    "hidepid=0": (
        {"mountinfo_text": mountinfo_line(super_options="rw,hidepid=0")},
        GroupCensus.EMPTY,
    ),
    "hidepid=off": (
        {"mountinfo_text": mountinfo_line(super_options="rw,hidepid=off")},
        GroupCensus.EMPTY,
    ),
    "subset=pid": (
        {"mountinfo_text": mountinfo_line(super_options="rw,subset=pid")},
        GroupCensus.EMPTY,
    ),
    "several optional fields": (
        {"mountinfo_text": mountinfo_line(optional_fields="shared:13 master:2")},
        GroupCensus.EMPTY,
    ),
    "no optional field": (
        {"mountinfo_text": mountinfo_line(optional_fields="")},
        GroupCensus.EMPTY,
    ),
    "a stacked proc line of another device first": (
        {"mountinfo_text": "\n".join([STACKED_PROC_ELSEWHERE, mountinfo_line()])},
        GroupCensus.EMPTY,
    ),
    "a stacked proc line of another device last": (
        {"mountinfo_text": "\n".join([mountinfo_line(), STACKED_PROC_ELSEWHERE])},
        GroupCensus.EMPTY,
    ),
}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    PRECONDITION_ROWS.values(),
    ids=PRECONDITION_ROWS.keys(),
)
def test_the_scan_is_available_only_when_proc_is_visibly_the_own_complete_procfs(
    overrides,
    expected,
):
    assert group_census(scripted_view(**overrides), PGID) is expected


SCAN_ROWS = {
    "a zombie leader alone": (scripted_view(), GroupCensus.EMPTY),
    "a live leader": (
        scripted_view(stats={PGID: stat_text(state="S")}),
        GroupCensus.LIVE,
    ),
    "a live member beside a zombie anchor": (
        view_with_members((PGID + 1, stat_text(state="S"))),
        GroupCensus.LIVE,
    ),
    "a live process of another group": (
        view_with_members((OTHER_GROUP, stat_text(state="S", pgrp=OTHER_GROUP))),
        GroupCensus.EMPTY,
    ),
    "a member whose group is gone": (
        view_with_members((PGID + 1, stat_text(state="S", pgrp=-1))),
        GroupCensus.EMPTY,
    ),
    "an entry that vanished": (
        scripted_view(listing=(PGID, PGID + 1)),
        GroupCensus.EMPTY,
    ),
    "an entry whose process vanished": (
        view_with_members((PGID + 1, ProcessLookupError("gone"))),
        GroupCensus.EMPTY,
    ),
    "an unreadable entry": (
        view_with_members((PGID + 1, PermissionError("stat"))),
        GroupCensus.PROC_UNREADABLE,
    ),
    "an unreadable entry beside a live member": (
        view_with_members(
            (PGID + 1, stat_text(state="S")),
            (PGID + 2, PermissionError("stat")),
        ),
        GroupCensus.PROC_UNREADABLE,
    ),
    "an unreadable listing": (
        scripted_view(listing=OSError("listdir")),
        GroupCensus.PROC_UNREADABLE,
    ),
    "an unreadable anchor": (
        scripted_view(stats={PGID: PermissionError("stat")}),
        GroupCensus.PROC_UNREADABLE,
    ),
    "a missing anchor": (
        scripted_view(listing=(), stats={}),
        GroupCensus.ANCHOR_NOT_VISIBLE,
    ),
    "an anchor of another group": (
        scripted_view(stats={PGID: stat_text(state="Z", pgrp=1)}),
        GroupCensus.ANCHOR_NOT_VISIBLE,
    ),
    "a hidden procfs beside a live member": (
        scripted_view(
            mountinfo_text=mountinfo_line(super_options="rw,hidepid=2"),
            stats={PGID: stat_text(state="S")},
        ),
        GroupCensus.PIDS_HIDDEN,
    ),
    "a foreign pid namespace beside a live member": (
        scripted_view(
            status_text=own_status("NSpid:\t9042\t42"),
            stats={PGID: stat_text(state="S")},
        ),
        GroupCensus.OTHER_PID_NAMESPACE,
    ),
    "a non-procfs mount beside a live member": (
        scripted_view(
            mountinfo_text=mountinfo_line(filesystem_type="tmpfs"),
            stats={PGID: stat_text(state="S")},
        ),
        GroupCensus.NOT_PROCFS,
    ),
    "an unrelated process whose name is not UTF-8": (
        view_with_members((OTHER_GROUP, stat_text(comm=b"ab\xff\xfe", pgrp=OTHER_GROUP))),
        GroupCensus.EMPTY,
    ),
    "a missing anchor beside a live member": (
        scripted_view(
            listing=(PGID + 1,),
            stats={PGID + 1: stat_text(state="S")},
        ),
        GroupCensus.ANCHOR_NOT_VISIBLE,
    ),
}


@pytest.mark.parametrize(("view", "expected"), SCAN_ROWS.values(), ids=SCAN_ROWS.keys())
def test_the_scan_counts_live_members_by_group_and_never_calls_an_error_empty(view, expected):
    assert group_census(view, PGID) is expected


def hide_pids(view: ScriptedView) -> None:
    view.mountinfo_text = mountinfo_line(super_options="rw,hidepid=2")


def enter_another_namespace(view: ScriptedView) -> None:
    view.status_text = own_status("NSpid:\t42\t1")


def lose_the_anchor(view: ScriptedView) -> None:
    view.stats = {}


CHANGES_BETWEEN_SCANS = {
    "pids become hidden": (hide_pids, GroupCensus.PIDS_HIDDEN),
    "the pid namespace changes": (enter_another_namespace, GroupCensus.OTHER_PID_NAMESPACE),
    "the anchor disappears": (lose_the_anchor, GroupCensus.ANCHOR_NOT_VISIBLE),
}


@pytest.mark.parametrize(
    ("change", "expected_second"),
    CHANGES_BETWEEN_SCANS.values(),
    ids=CHANGES_BETWEEN_SCANS.keys(),
)
def test_every_scan_checks_the_preconditions_again(change, expected_second):
    view = scripted_view()

    first = group_census(view, PGID)
    change(view)
    second = group_census(view, PGID)

    assert (first, second) == (GroupCensus.EMPTY, expected_second)


class ChildDidNotEndError(Exception):
    """The child neither ended nor reported within the observation deadline."""


class StdoutStillOpenError(ChildDidNotEndError):
    """The child kept its stdout open past the deadline."""


class ExitNotReportedError(ChildDidNotEndError):
    """The child closed stdout but reported no exit before the deadline."""


def wait_for_exit_report(
    stdout: BinaryIO,
    pid: int,
    *,
    clock: Callable[[], float],
    waitid: Callable[[int, int, int], object | None],
    deadline: float,
):
    fd = stdout.fileno()
    while True:
        readable, _, _ = select.select([fd], [], [], max(deadline - clock(), 0))
        if not readable:
            raise StdoutStillOpenError(f"child {pid} kept its stdout open past the deadline")
        if os.read(fd, 4096) == b"":
            break
    while True:
        report = waitid(os.P_PID, pid, REPORT_FLAGS)
        if report is not None:
            return report
        if clock() >= deadline:
            raise ExitNotReportedError(f"child {pid} reported no exit before the deadline")


class ChildNotReapedError(Exception):
    """The child outlived the cleanup allowance."""


def reap_group(
    process: subprocess.Popen[bytes],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    cleanup_deadline = clock() + CLEANUP_ALLOWANCE_SECONDS
    try:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        try:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()
        finally:
            try:
                process.wait(timeout=max(cleanup_deadline - clock(), 0))
            except subprocess.TimeoutExpired as expired:
                raise ChildNotReapedError(f"child {process.pid} outlived its cleanup") from expired


class SteppingClock:
    def __init__(self, start: float = 0.0, step: float = 1.0) -> None:
        self.now, self.step = start, step

    def __call__(self) -> float:
        reading, self.now = self.now, self.now + self.step
        return reading


class ScriptedWaitid:
    def __init__(self, *reports: object | None, error: Exception | None = None) -> None:
        self.reports = list(reports)
        self.error = error
        self.calls: list[tuple[int, int, int]] = []

    def __call__(self, id_type: int, pid: int, flags: int) -> object | None:
        self.calls.append((id_type, pid, flags))
        if self.error:
            raise self.error
        return self.reports.pop(0) if len(self.reports) > 1 else self.reports[0]


@pytest.fixture
def ended_stdout() -> Iterator[BinaryIO]:
    read_end, write_end = os.pipe()
    os.close(write_end)
    with open(read_end, "rb") as stdout:
        yield stdout


REPORT = object()
SOON = 5.0
ALREADY_PAST = -1.0


@pytest.mark.parametrize(
    ("deadline", "reports"),
    [(SOON, (None,)), (ALREADY_PAST, (None,))],
    ids=["a child that never reports", "a deadline that has already passed"],
)
def test_waiting_for_a_child_that_never_reports_ends_at_the_deadline_after_one_poll(
    ended_stdout,
    deadline,
    reports,
):
    waitid = ScriptedWaitid(*reports)
    clock = SteppingClock()

    with pytest.raises(ExitNotReportedError):
        wait_for_exit_report(
            ended_stdout,
            7,
            clock=clock,
            waitid=waitid,
            deadline=deadline,
        )

    assert len(waitid.calls) >= 1


def test_waiting_returns_the_report_of_a_child_that_ends_on_the_third_poll(ended_stdout):
    waitid = ScriptedWaitid(None, None, REPORT)

    report = wait_for_exit_report(
        ended_stdout,
        7,
        clock=SteppingClock(),
        waitid=waitid,
        deadline=SOON,
    )

    assert (report, len(waitid.calls)) == (REPORT, 3)


def test_every_poll_asks_without_blocking_and_without_reaping(ended_stdout):
    waitid = ScriptedWaitid(None, None, REPORT)

    wait_for_exit_report(ended_stdout, 7, clock=SteppingClock(), waitid=waitid, deadline=SOON)

    assert waitid.calls == [(os.P_PID, 7, os.WEXITED | os.WNOHANG | os.WNOWAIT)] * 3


def test_a_child_that_was_already_reaped_fails_the_wait_loudly(ended_stdout):
    waitid = ScriptedWaitid(error=ChildProcessError("reaped"))
    clock = SteppingClock()

    with pytest.raises(ChildProcessError):
        wait_for_exit_report(
            ended_stdout,
            7,
            clock=clock,
            waitid=waitid,
            deadline=SOON,
        )


class ScriptedWait:
    def __init__(self, *, expires: bool) -> None:
        self.expires = expires
        self.timeouts: list[float] = []

    def __call__(self, *, timeout: float) -> int:
        self.timeouts.append(timeout)
        if self.expires:
            raise subprocess.TimeoutExpired("child", timeout)
        return 0


def finished_process(wait: ScriptedWait) -> subprocess.Popen[bytes]:
    return SimpleNamespace(pid=7, returncode=0, stdin=None, stdout=None, wait=wait)


def test_cleanup_waits_only_for_the_allowance_left_since_its_own_start():
    wait = ScriptedWait(expires=False)

    reap_group(finished_process(wait), clock=SteppingClock(start=100.0, step=3.0))

    assert wait.timeouts == [CLEANUP_ALLOWANCE_SECONDS - 3.0]


def test_cleanup_gives_a_child_no_time_once_its_allowance_is_used_up():
    wait = ScriptedWait(expires=False)

    reap_group(finished_process(wait), clock=SteppingClock(step=CLEANUP_ALLOWANCE_SECONDS * 2))

    assert wait.timeouts == [0]


def test_cleanup_fails_loudly_when_the_child_outlives_the_allowance():
    stuck_child = finished_process(ScriptedWait(expires=True))
    clock = SteppingClock()

    with pytest.raises(ChildNotReapedError):
        reap_group(stuck_child, clock=clock)


@dataclasses.dataclass(frozen=True)
class RunningChild:
    process: subprocess.Popen[bytes]
    observation_deadline: float

    @property
    def pid(self) -> int:
        return self.process.pid

    def remaining_observation_time(self) -> float:
        return max(self.observation_deadline - time.monotonic(), 0)


@pytest.fixture
def start_child(tmp_path: Path) -> Iterator[Callable[[str], RunningChild]]:
    with ExitStack() as cleanup:

        def start(code: str) -> RunningChild:
            description = ChildProcess(
                binary=Path(sys.executable),
                arguments=("-I", "-c", code),
                environment=closed_environment(tmp_path),
                working_directory=tmp_path,
            )
            process = open_pipes(description, stderr="devnull")
            cleanup.callback(reap_group, process)
            return RunningChild(process, time.monotonic() + OBSERVATION_DEADLINE_SECONDS)

        yield start


def wait_until_a_zombie(child: RunningChild) -> None:
    child.process.stdin.close()
    wait_for_exit_report(
        child.process.stdout,
        child.pid,
        clock=time.monotonic,
        waitid=os.waitid,
        deadline=child.observation_deadline,
    )
    assert parse_stat(RealProcView().stat(child.pid)).state == "Z"


def wait_until_reaped(child: RunningChild) -> None:
    child.process.stdin.close()
    child.process.wait(timeout=child.remaining_observation_time())


def keep_running(child: RunningChild) -> None:
    del child


def read_one_byte(child: RunningChild) -> bytes:
    stdout = child.process.stdout
    readable, _, _ = select.select([stdout], [], [], child.remaining_observation_time())
    assert readable
    return os.read(stdout.fileno(), 1)


@requires_linux_proc
@pytest.mark.parametrize(
    ("end_the_child", "expected"),
    [
        (keep_running, GroupCensus.LIVE),
        (wait_until_a_zombie, GroupCensus.EMPTY),
        (wait_until_reaped, GroupCensus.ANCHOR_NOT_VISIBLE),
    ],
    ids=["a running child", "a zombie anchor", "a reaped child"],
)
def test_the_real_view_tells_a_running_zombie_and_reaped_anchor_apart(
    start_child,
    end_the_child,
    expected,
):
    child = start_child(WAIT_FOR_STDIN)

    end_the_child(child)

    assert group_census(RealProcView(), child.pid) is expected


@requires_linux_proc
def test_a_child_that_closed_stdout_and_stays_alive_is_killed_and_reaped_after_the_deadline(
    start_child,
):
    child = start_child(CLOSE_STDOUT_THEN_WAIT_FOR_STDIN)
    assert read_one_byte(child) == b""
    deadline = time.monotonic() + STUCK_CHILD_DEADLINE_SECONDS

    with pytest.raises(ExitNotReportedError):
        wait_for_exit_report(
            child.process.stdout,
            child.pid,
            clock=time.monotonic,
            waitid=os.waitid,
            deadline=deadline,
        )
    reap_group(child.process)

    assert child.process.returncode == -signal.SIGKILL
    assert not Path(f"/proc/{child.pid}").exists()


def start_child_named_in_non_utf8(start_child) -> RunningChild:
    child = start_child(NON_UTF8_NAME_THEN_WAIT_FOR_STDIN)
    assert read_one_byte(child) == b"r"
    assert Path(f"/proc/{child.pid}/comm").read_bytes().startswith(b"ab\xff\xfe")
    return child


@requires_linux_proc
def test_a_running_group_whose_process_name_is_not_utf8_counts_as_live(start_child):
    child = start_child_named_in_non_utf8(start_child)

    assert group_census(RealProcView(), child.pid) is GroupCensus.LIVE


@requires_linux_proc
def test_an_unrelated_process_named_in_non_utf8_does_not_stop_the_census_of_an_anchor(
    start_child,
):
    start_child_named_in_non_utf8(start_child)
    anchor = start_child(WAIT_FOR_STDIN)
    wait_until_a_zombie(anchor)

    assert group_census(RealProcView(), anchor.pid) is GroupCensus.EMPTY


@pytest.fixture
def fake_proc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proc"
    (root / "self").mkdir(parents=True)
    monkeypatch.setattr(procfs, "PROC_ROOT", root)
    return root


@pytest.mark.parametrize(
    ("file_name", "read"),
    [
        ("mountinfo", lambda view: view.mountinfo()),
        ("status", lambda view: view.own_status()),
    ],
)
def test_the_real_view_reads_text_files_with_non_utf8_bytes_without_loss(
    fake_proc_root,
    file_name,
    read,
):
    contents = b"/mnt/\xff\xfe name\n"
    (fake_proc_root / "self" / file_name).write_bytes(contents)

    text = read(RealProcView())

    assert text.encode(errors="surrogateescape") == contents


def test_the_real_view_reads_a_stat_with_a_non_utf8_command_name_as_raw_bytes(fake_proc_root):
    (fake_proc_root / "9").mkdir()
    raw = stat_text(comm=b"ab\xff\xfe")
    (fake_proc_root / "9" / "stat").write_bytes(raw)

    assert RealProcView().stat(9) == raw


HELD_DIRECTORY = "/r/agent-run-a"


@pytest.mark.parametrize(
    ("mountinfo", "holds"),
    [
        pytest.param(mountinfo_line(mount_point=HELD_DIRECTORY), True, id="equal"),
        pytest.param(mountinfo_line(mount_point=f"{HELD_DIRECTORY}/work"), True, id="below"),
        pytest.param(
            mountinfo_line(mount_point="/r/agent-run-a1"), False, id="sibling-sharing-a-prefix",
        ),
        pytest.param(mountinfo_line(mount_point="/r"), False, id="parent"),
        pytest.param("28 32 0:25 / /r/agent-run-a rw,nosuid shared:13", False, id="no-separator"),
        pytest.param("", False, id="empty-table"),
    ],
)
def test_a_mount_table_holds_a_directory_when_a_mount_point_is_at_or_below_it(
    mountinfo: str, holds: bool,
) -> None:
    assert holds_mount_point(mountinfo, HELD_DIRECTORY) is holds


@pytest.mark.parametrize(
    ("escape", "decoded"),
    [("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")],
    ids=["space", "tab", "newline", "backslash"],
)
def test_an_escaped_mount_point_is_matched_by_its_decoded_name(escape: str, decoded: str) -> None:
    line = mountinfo_line(mount_point=f"/r/a{escape}b")

    assert holds_mount_point(line, f"/r/a{decoded}b")
    assert not holds_mount_point(line, f"/r/a{escape}b")
