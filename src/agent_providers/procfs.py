"""One scan of one process group's live members, read from ``/proc``.

``/proc`` is an external boundary, so its reading sits behind ``ProcView`` and
the decision between the reads is pure. The module signals nothing, starts
nothing and has no clock; it imports the standard library only.

The checks detect a visible misconfiguration. They do not establish a complete
view: a mount over ``/proc`` or over one of its numeric directories is a
precondition of the supported host and nothing here looks for it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

PROC_ROOT = Path("/proc")
PROC_FILESYSTEM_TYPE = "proc"
HIDEPID_OPTION = "hidepid"
HIDEPID_VISIBLE_VALUES = frozenset({"0", "off"})
NAMESPACE_PID_FIELD = "NSpid:"
MOUNTINFO_SEPARATOR = "-"
MOUNTINFO_DEVICE_INDEX = 2
MOUNTINFO_MOUNT_POINT_INDEX = 4
STAT_STATE_INDEX = 0
STAT_PGRP_INDEX = 2
STAT_THREADS_INDEX = 17
EXITED_STATES = frozenset({"Z", "X"})
VANISHED_ENTRY_ERRORS = (FileNotFoundError, ProcessLookupError)
_INTEGER = re.compile(r"-?[0-9]+")


class GroupCensus(Enum):
    """The outcome of one scan; only ``EMPTY`` confirms an empty group.

    ``EMPTY`` means something only for a scan begun after a SIGKILL sent
    through the group's anchor: the group cannot gain members from inside after
    that, so one scan needs no atomic snapshot. The only production caller is
    the anchored child's ``kill_and_census``. Every other member says the scan
    could not tell, or found a live member, and never confirms.
    """

    EMPTY = "empty"
    LIVE = "live"
    NOT_PROCFS = "not_procfs"
    OTHER_PID_NAMESPACE = "other_pid_namespace"
    PIDS_HIDDEN = "pids_hidden"
    PROC_UNREADABLE = "proc_unreadable"
    ANCHOR_NOT_VISIBLE = "anchor_not_visible"


class ProcView(Protocol):
    """The reads a scan needs; every one lets its ``OSError`` out."""

    def own_pid(self) -> int: ...

    def proc_device(self) -> str:
        """The ``major:minor`` of the ``/proc`` mount."""
        ...

    def mountinfo(self) -> str: ...

    def own_status(self) -> str: ...

    def process_ids(self) -> tuple[int, ...]: ...

    def stat(self, pid: int) -> str: ...


class RealProcView:
    """The view over the real ``/proc``: reads only, no decisions."""

    def own_pid(self) -> int:
        return os.getpid()

    def proc_device(self) -> str:
        device = os.stat(PROC_ROOT).st_dev
        return f"{os.major(device)}:{os.minor(device)}"

    def mountinfo(self) -> str:
        return (PROC_ROOT / "self" / "mountinfo").read_text()

    def own_status(self) -> str:
        return (PROC_ROOT / "self" / "status").read_text()

    def process_ids(self) -> tuple[int, ...]:
        return tuple(int(name) for name in os.listdir(PROC_ROOT) if name.isdecimal())

    def stat(self, pid: int) -> str:
        return (PROC_ROOT / str(pid) / "stat").read_text()


class UnparsableStatError(ValueError):
    """A stat file that does not hold state, pgrp and thread count."""


@dataclass(frozen=True)
class ProcessStat:
    state: str
    pgrp: int
    threads: int

    @property
    def is_live(self) -> bool:
        return not (self.state in EXITED_STATES and self.threads <= 1)


def parse_stat(contents: str) -> ProcessStat:
    """Read state, pgrp and thread count after the last ``)`` of the whole file.

    The command name may hold spaces, parentheses and newlines, so only the
    text after the last ``)`` is split. Only the numeric fields are validated.
    """
    closing = contents.rfind(")")
    if closing < 0:
        raise UnparsableStatError("no closing parenthesis")
    fields = contents[closing + 1 :].removeprefix(" ").rstrip("\n").split(" ")
    if len(fields) <= STAT_THREADS_INDEX:
        raise UnparsableStatError("too few fields")
    state = fields[STAT_STATE_INDEX]
    if len(state) != 1:
        raise UnparsableStatError("state is not one character")
    pgrp, threads = fields[STAT_PGRP_INDEX], fields[STAT_THREADS_INDEX]
    if not (_INTEGER.fullmatch(pgrp) and _INTEGER.fullmatch(threads)):
        raise UnparsableStatError("pgrp or thread count is not a number")
    return ProcessStat(state=state, pgrp=int(pgrp), threads=int(threads))


def group_census(view: ProcView, pgid: int) -> GroupCensus:
    """Count the live members of process group ``pgid`` in one scan.

    Preconditions, checked at every scan; a failure is never ``EMPTY``:
    ``/proc`` is a procfs, it is the process's own PID namespace, no process
    is hidden, every entry is readable, and the anchor ``pgid`` is visible.
    """
    try:
        mount_verdict = _mount_verdict(view)
        if mount_verdict is not None:
            return mount_verdict
        if not _in_own_pid_namespace(view):
            return GroupCensus.OTHER_PID_NAMESPACE
        return _scan(view, pgid)
    except (OSError, UnparsableStatError):
        return GroupCensus.PROC_UNREADABLE


def _mount_verdict(view: ProcView) -> GroupCensus | None:
    device = view.proc_device()
    options = _super_options_of_proc_mount(view.mountinfo(), device)
    if options is None:
        return GroupCensus.NOT_PROCFS
    if _hides_processes(options):
        return GroupCensus.PIDS_HIDDEN
    return None


def _super_options_of_proc_mount(mountinfo: str, device: str) -> list[str] | None:
    options = None
    for line in mountinfo.splitlines():
        fields = line.split()
        if MOUNTINFO_SEPARATOR not in fields:
            continue
        separator = fields.index(MOUNTINFO_SEPARATOR)
        is_proc_mount = (
            len(fields) > MOUNTINFO_MOUNT_POINT_INDEX
            and fields[MOUNTINFO_DEVICE_INDEX] == device
            and fields[MOUNTINFO_MOUNT_POINT_INDEX] == str(PROC_ROOT)
        )
        if is_proc_mount:
            filesystem_type = fields[separator + 1 : separator + 2]
            super_options = fields[separator + 3 : separator + 4]
            options = (
                super_options[0].split(",")
                if filesystem_type == [PROC_FILESYSTEM_TYPE] and super_options
                else None
            )
    return options


def _hides_processes(super_options: list[str]) -> bool:
    for option in super_options:
        name, _, value = option.partition("=")
        if name == HIDEPID_OPTION and value not in HIDEPID_VISIBLE_VALUES:
            return True
    return False


def _in_own_pid_namespace(view: ProcView) -> bool:
    try:
        status = view.own_status()
    except OSError:
        return False
    for line in status.splitlines():
        if line.startswith(NAMESPACE_PID_FIELD):
            return line.split()[1:] == [str(view.own_pid())]
    return False


def _scan(view: ProcView, pgid: int) -> GroupCensus:
    if not _anchor_is_visible(view, pgid):
        return GroupCensus.ANCHOR_NOT_VISIBLE
    found_live_member = False
    for pid in view.process_ids():
        try:
            stat = parse_stat(view.stat(pid))
        except VANISHED_ENTRY_ERRORS:
            continue
        if stat.pgrp == pgid and stat.is_live:
            found_live_member = True
    return GroupCensus.LIVE if found_live_member else GroupCensus.EMPTY


def _anchor_is_visible(view: ProcView, pgid: int) -> bool:
    try:
        anchor = parse_stat(view.stat(pgid))
    except VANISHED_ENTRY_ERRORS:
        return False
    return anchor.pgrp == pgid
