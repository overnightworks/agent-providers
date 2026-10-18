"""A run's own directories: their layout, their setup rollback and their one removal.

The real rows start a Python leader directly through the real door; the retained
group rows use the fake door and the process-wide Claude pool.
"""

from __future__ import annotations

import asyncio
import errno
import gc
import logging
import os
import shutil
import sys
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from anchored_kernel import join_watcher, recorded_watcher_starts
from anchored_leaders import spawn_warnings
from anchored_support import GONE, TEST_WAIT_SECONDS, ChildCleanup
from provider_test_support import (
    DESTINATION_FAULTS,
    ScriptedClaudeChild,
    a_child_that_outlives_sigkill,
    a_credential_destination_that_fails,
    confirm_the_retained_group_gone,
    fail_mkdtemp,
    fail_the_directory_named,
    held_claude_slots,
    install_claude_cli_children,
    killpg_forbidden,
    mountinfo_line,
    reap_without_waiting,
    recorded_census_workers,
    run_bounded,
    tree_snapshot,
)

from agent_providers import lost_groups, process, procfs
from agent_providers.anchored_turn import (
    CliProcessPool,
    ReapBounds,
    anchored_turn_child,
)
from agent_providers.claude.provider import CLAUDE_CLI_PROCESS_POOL
from agent_providers.config import (
    PrivateWorkingRoot,
    ProviderRuntimeNotConfiguredError,
    current_config,
    reset_config,
)
from agent_providers.errors import ContainmentLost
from agent_providers.lost_groups import RetainedGroup
from agent_providers.process import (
    CliEnvironmentCause,
    CliEnvironmentUnavailableError,
    HolderReservation,
    RunDirectories,
    run_directories,
)
from agent_providers.procfs import GroupCensus
from agent_providers.spawn import ChildProcess, ChildProcessSpecificationError, closed_environment

REAL_REAP_BOUNDS = ReapBounds(
    sigterm_grace_seconds=TEST_WAIT_SECONDS, confirmation_seconds=TEST_WAIT_SECONDS,
)
TREE_PATTERN = "agent-run-*"
PROMPT = "multi-line\nnon-ASCII text: grüße, 日本語\n".encode()
FILES = {"prompt": PROMPT, "empty": b""}
NAME_REFUSED = "A run file needs a single path component as its name"
REMOVAL_FAILED = "private credential home removal failed (PermissionError)"
RESERVATIONS_CLOSED = "reservations are closed"
OUTSIDE_FILE_BYTES = b"the profile's login\n"
LEADER_SCRIPT = """\
import os
import sys

outside_file, outside_directory = sys.argv[1:3]
home, temporary = os.environ["HOME"], os.environ["TMPDIR"]
with open(os.path.join(home, "prompt"), "rb") as prompt:
    sys.stdout.buffer.write(prompt.read())
    sys.stdout.buffer.flush()
with open(os.path.join(home, ".cli.json"), "w") as settings:
    settings.write("{}")
sessions = os.path.join(home, ".cli", "sessions")
os.makedirs(sessions)
session = os.path.join(sessions, "session")
with open(session, "w") as handle:
    handle.write("session")
os.chmod(session, 0o400)
os.chmod(sessions, 0o500)
scratch = os.path.join(temporary, "cli-tmp")
os.mkdir(scratch)
os.chmod(scratch, 0o000)
with open("result", "w") as result:
    result.write("done")
os.symlink(outside_file, os.path.join(home, "outside-file"))
os.symlink(outside_directory, os.path.join(home, "outside-dir"))
"""
HOME_AFTER_THE_LEADER = [
    ".cli", ".cli.json", "cli-tmp", "outside-dir", "outside-file", "prompt",
]
WORK_AFTER_THE_LEADER = ["result"]


def a_private_root(parent: Path, name: str = "root") -> Path:
    root = parent / name
    root.mkdir()
    root.chmod(0o700)
    return root


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    return a_private_root(tmp_path)


def removal_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.get_records("call")
        if record.name == process.log.name and record.levelno == logging.ERROR
    ]


def descriptors_open() -> int:
    return len(os.listdir("/proc/self/fd"))


def enter_run_directories(
    root: Path, files: dict[str, bytes], entered: list[RunDirectories] | None = None,
) -> None:
    with run_directories(PrivateWorkingRoot(root), files) as directories:
        if entered is not None:
            entered.append(directories)


def an_io_error() -> OSError:
    return OSError(errno.EIO, "synthetic failure")


def fail_chmod_of(monkeypatch: pytest.MonkeyPatch, name_prefix: str) -> None:
    real_chmod = os.chmod

    def chmod(path: Any, mode: int, **keywords: Any) -> None:
        if Path(path).name.startswith(name_prefix):
            raise an_io_error()
        real_chmod(path, mode, **keywords)

    monkeypatch.setattr(process.os, "chmod", chmod)


def make_writes(monkeypatch: pytest.MonkeyPatch, behaviour: Callable[[int, bytes], int]) -> None:
    def write(descriptor: int, data: Any) -> int:
        return behaviour(descriptor, bytes(data))

    monkeypatch.setattr(process.os, "write", write)


def mkdtemp_must_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(**_arguments: Any) -> str:
        pytest.fail("a run tree was created for a refused name")

    monkeypatch.setattr(process.tempfile, "mkdtemp", forbidden)


def refuse_to_remove(_path: Any, *_arguments: Any, **_keywords: Any) -> None:
    raise PermissionError(errno.EACCES, "synthetic refusal")


@contextmanager
def rmtree_refused(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with monkeypatch.context() as patched:
        patched.setattr(process.shutil, "rmtree", refuse_to_remove)
        yield


@contextmanager
def rmtree_counted(
    monkeypatch: pytest.MonkeyPatch, events: list[str] | None = None,
) -> Iterator[list[str]]:
    seen: list[str] = [] if events is None else events
    real_rmtree = shutil.rmtree

    def counted(path: Any, *arguments: Any, **keywords: Any) -> None:
        seen.append("rmtree")
        real_rmtree(path, *arguments, **keywords)

    with monkeypatch.context() as patched:
        patched.setattr(process.shutil, "rmtree", counted)
        yield seen


def the_only_tree(root: Path) -> Path:
    (tree,) = root.glob(TREE_PATTERN)
    return tree


def remove_the_leftover(tree: Path) -> None:
    shutil.rmtree(tree)


def test_a_run_needs_no_installed_runtime(private_root: Path) -> None:
    reset_config()
    with pytest.raises(ProviderRuntimeNotConfiguredError):
        current_config()
    inside: list[list[str]] = []

    with run_directories(PrivateWorkingRoot(private_root), {}) as directories:
        inside.append([entry.name for entry in private_root.iterdir()])
        assert directories.home.is_dir()

    assert (len(inside[0]), list(private_root.iterdir())) == (1, [])


@pytest.mark.parametrize("umask", (0o022, 0o277, 0o000), ids=oct)
@pytest.mark.parametrize("files", ({}, FILES), ids=("no-files", "files"))
def test_a_run_gets_a_private_home_a_separate_empty_work_directory_and_exact_files(
    private_root: Path, umask: int, files: dict[str, bytes],
) -> None:
    previous_umask = os.umask(umask)
    try:
        with run_directories(PrivateWorkingRoot(private_root), files) as directories:
            snapshot = tree_snapshot(private_root)
            tree = directories.home.parent
            paths = (directories.home, directories.working_directory)
    finally:
        os.umask(previous_umask)

    expected = {
        ".": (0o700, None),
        tree.name: (0o700, None),
        f"{tree.name}/home": (0o700, None),
        f"{tree.name}/work": (0o700, None),
        **{f"{tree.name}/home/{name}": (0o600, content) for name, content in files.items()},
    }
    assert (snapshot, tree.name.startswith("agent-run-"), tree.parent) == (
        expected, True, private_root,
    )
    assert all(path.is_absolute() for path in paths)
    assert list(private_root.iterdir()) == []


def test_a_run_file_is_written_whole_when_the_kernel_takes_one_byte_at_a_time(
    private_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_write = os.write
    written: list[bytes] = []
    make_writes(monkeypatch, lambda descriptor, data: real_write(descriptor, data[:1]))

    with run_directories(PrivateWorkingRoot(private_root), {"prompt": PROMPT}) as directories:
        written.append((directories.home / "prompt").read_bytes())

    assert written == [PROMPT]


@pytest.mark.parametrize("name", ("", ".", "..", "a/b", "../x", "x\0y"))
def test_a_refused_file_name_creates_nothing(
    private_root: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    mkdtemp_must_not_run(monkeypatch)

    with pytest.raises(ChildProcessSpecificationError) as refused:
        enter_run_directories(private_root, {"ok": b"x", name: b"y"})

    assert (
        str(refused.value), refused.value.__cause__, refused.value.__context__,
        list(private_root.iterdir()),
    ) == (NAME_REFUSED, None, None, [])


@contextmanager
def setup_fault(kind: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    destination_closes: list[int] = []
    if kind == "mkdtemp":
        fail_mkdtemp(monkeypatch, Path("/"))
    elif kind in CHMOD_TARGETS:
        fail_chmod_of(monkeypatch, CHMOD_TARGETS[kind])
    elif kind.endswith("-mkdir"):
        fail_the_directory_named(monkeypatch, kind.removesuffix("-mkdir"), an_io_error())
    elif kind == "zero-progress":
        make_writes(monkeypatch, lambda _descriptor, _data: 0)
    else:
        with a_credential_destination_that_fails(kind) as destination_closes:
            yield destination_closes
        return
    yield destination_closes


CHMOD_TARGETS = {"tree-chmod": "agent-run-", "home-chmod": "home", "work-chmod": "work"}
SETUP_FAULTS = (
    "mkdtemp", "tree-chmod", "home-chmod", "work-chmod", "home-mkdir", "work-mkdir",
    "zero-progress", *DESTINATION_FAULTS,
)


@pytest.mark.parametrize("kind", SETUP_FAULTS)
def test_a_setup_io_error_becomes_the_closed_environment_error_and_leaves_nothing(
    private_root: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    entered: list[RunDirectories] = []
    descriptors_before = descriptors_open()

    with setup_fault(kind, monkeypatch) as destination_closes:
        with pytest.raises(CliEnvironmentUnavailableError) as unavailable:
            enter_run_directories(private_root, {"prompt": PROMPT}, entered)

    error = unavailable.value
    assert (error.cause, str(error), error.__cause__, error.__context__) == (
        CliEnvironmentCause.HOME_NOT_CREATED, "could not create a private CLI home", None, None,
    )
    assert (entered, list(private_root.iterdir()), descriptors_open()) == (
        [], [], descriptors_before,
    )
    assert len(destination_closes) == (1 if kind in DESTINATION_FAULTS else 0)


def test_a_setup_interruption_propagates_unchanged_after_the_rollback(
    private_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    interruption = KeyboardInterrupt()

    def interrupt(_descriptor: int, _data: bytes) -> int:
        raise interruption

    make_writes(monkeypatch, interrupt)

    with pytest.raises(KeyboardInterrupt) as raised:
        enter_run_directories(private_root, {"prompt": PROMPT})

    assert (raised.value is interruption, list(private_root.iterdir())) == (True, [])


def test_a_failed_rollback_is_logged_once_and_the_creation_error_still_raises(
    private_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    with setup_fault("write", monkeypatch), rmtree_refused(monkeypatch):
        with pytest.raises(CliEnvironmentUnavailableError):
            enter_run_directories(private_root, {"prompt": PROMPT})

    leftover = the_only_tree(private_root)
    assert removal_errors(caplog) == [REMOVAL_FAILED]
    remove_the_leftover(leftover)


@dataclass
class ReapObservation:
    leader_gone: bool
    slots_held: int
    home_entries: list[str]
    work_entries: list[str]


def held_slots(pool: CliProcessPool) -> int:
    with pool._lock:
        return len(pool._reservations)


@dataclass
class RealRuns:
    """Drives real leaders through the real door in a module-local pool."""

    root: Path
    outside_file: Path
    outside_directory: Path
    script: Path
    child_cleanup: ChildCleanup
    monkeypatch: pytest.MonkeyPatch
    pool: CliProcessPool = field(
        default_factory=lambda: CliProcessPool(
            limit=1, name="run directories test", reap=REAL_REAP_BOUNDS,
        )
    )
    stdout: bytes = b""
    after_reap: ReapObservation | None = None

    def outside_snapshot(self) -> dict[str, tuple[int, bytes | str | None]]:
        return tree_snapshot(self.outside_file.parent)

    async def run(
        self,
        ending: Callable[[], Awaitable[None]] | None = None,
        before_removal: Callable[[RunDirectories], None] | None = None,
    ) -> None:
        with run_directories(PrivateWorkingRoot(self.root), {"prompt": PROMPT}) as directories:
            try:
                await self._run_the_leader(directories, ending)
            finally:
                self._observe_the_reap(directories)
            if before_removal is not None:
                before_removal(directories)

    async def _run_the_leader(
        self, directories: RunDirectories, ending: Callable[[], Awaitable[None]] | None,
    ) -> None:
        child = ChildProcess(
            binary=Path(sys.executable),
            arguments=("-I", str(self.script), str(self.outside_file), str(self.outside_directory)),
            environment=closed_environment(directories.home),
            working_directory=directories.working_directory,
        )
        async with anchored_turn_child(
            self.pool, child, home_reservation=directories.reserve(),
        ) as anchored:
            self.stdout, _stderr = await anchored.communicate()
            if ending is not None:
                await ending()

    def _observe_the_reap(self, directories: RunDirectories) -> None:
        owned = self.child_cleanup.listed()[-1]
        self.after_reap = ReapObservation(
            leader_gone=self.child_cleanup.state(owned) is GONE,
            slots_held=held_slots(self.pool),
            home_entries=sorted(entry.name for entry in directories.home.iterdir()),
            work_entries=sorted(entry.name for entry in directories.working_directory.iterdir()),
        )

    def drive(self, scenario: Coroutine[Any, Any, None]) -> None:
        children_before = len(self.child_cleanup.listed())
        with recorded_watcher_starts(self.monkeypatch) as watchers:
            try:
                self.child_cleanup.capture(lambda: run_bounded(scenario))
            finally:
                for owned in self.child_cleanup.listed()[children_before:]:
                    join_watcher(watchers.thread(owned.pid))


@pytest.fixture
def real_runs(
    tmp_path: Path,
    private_root: Path,
    child_cleanup: ChildCleanup,
    thread_failures: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[RealRuns]:
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "login.json"
    outside_file.write_bytes(OUTSIDE_FILE_BYTES)
    outside_directory = outside / "program"
    outside_directory.mkdir()
    (outside_directory / "binary").write_bytes(b"installed program")
    outside_directory.chmod(0o500)
    script = tmp_path / "bin" / "leader.py"
    script.parent.mkdir()
    script.write_text(LEADER_SCRIPT)
    runs = RealRuns(
        private_root, outside_file, outside_directory, script, child_cleanup, monkeypatch,
    )
    yield runs
    outside_directory.chmod(0o700)
    assert [child_cleanup.state(owned) is GONE for owned in child_cleanup.listed()] == [
        True for _ in child_cleanup.listed()
    ]
    assert (held_slots(runs.pool), thread_failures, spawn_warnings(caplog)) == (0, [], [])


def assert_a_clean_real_run(real_runs: RealRuns, outside_before: dict[str, Any]) -> None:
    assert real_runs.stdout == PROMPT
    assert real_runs.after_reap == ReapObservation(
        leader_gone=True, slots_held=0,
        home_entries=HOME_AFTER_THE_LEADER, work_entries=WORK_AFTER_THE_LEADER,
    )
    assert list(real_runs.root.iterdir()) == []
    assert real_runs.outside_snapshot() == outside_before


def fails_with(error: BaseException) -> Callable[[], Awaitable[None]]:
    async def ending() -> None:
        raise error

    return ending


def test_a_real_run_ends_with_its_directories_removed_after_the_reap_when_the_body_returns(
    real_runs: RealRuns,
) -> None:
    outside_before = real_runs.outside_snapshot()

    real_runs.drive(real_runs.run())

    assert_a_clean_real_run(real_runs, outside_before)


@pytest.mark.parametrize(
    "error", (RuntimeError("body failed"), an_io_error()), ids=("runtime-error", "os-error"),
)
def test_a_real_run_whose_body_raises_removes_its_directories_and_propagates_the_same_error(
    real_runs: RealRuns, error: Exception,
) -> None:
    outside_before = real_runs.outside_snapshot()

    with pytest.raises(type(error)) as raised:
        real_runs.drive(real_runs.run(fails_with(error)))

    assert raised.value is error
    assert_a_clean_real_run(real_runs, outside_before)


def test_a_real_run_whose_task_is_cancelled_removes_its_directories(real_runs: RealRuns) -> None:
    outside_before = real_runs.outside_snapshot()

    async def cancelled_run() -> None:
        reached = asyncio.Event()

        async def wait_to_be_cancelled() -> None:
            reached.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(real_runs.run(wait_to_be_cancelled))
        await reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    real_runs.drive(cancelled_run())

    assert_a_clean_real_run(real_runs, outside_before)


def link_in_place_of(entry: str | None, target: Path) -> Callable[[RunDirectories], None]:
    def swap(directories: RunDirectories) -> None:
        tree = directories.home.parent
        replaced = tree if entry is None else tree / entry
        replaced.rename(replaced.with_name(f"moved-{replaced.name}"))
        replaced.symlink_to(target)

    return swap


@pytest.mark.parametrize(
    ("entry", "target_name"), (("home", "outside_directory"), ("work", "outside_file")),
)
def test_a_removal_never_follows_a_run_entry_replaced_by_a_link(
    real_runs: RealRuns, entry: str, target_name: str,
) -> None:
    outside_before = real_runs.outside_snapshot()
    swap = link_in_place_of(entry, getattr(real_runs, target_name))

    real_runs.drive(real_runs.run(before_removal=swap))

    assert (list(real_runs.root.iterdir()), real_runs.outside_snapshot()) == ([], outside_before)


def test_a_removal_never_follows_the_tree_replaced_by_a_link_and_logs_once(
    real_runs: RealRuns, caplog: pytest.LogCaptureFixture,
) -> None:
    outside_before = real_runs.outside_snapshot()
    swap = link_in_place_of(None, real_runs.outside_directory)

    real_runs.drive(real_runs.run(before_removal=swap))

    link = the_only_tree(real_runs.root)
    renamed = real_runs.root / f"moved-{link.name}"
    assert (link.is_symlink(), real_runs.outside_snapshot(), len(removal_errors(caplog))) == (
        True, outside_before, 1,
    )
    link.unlink()
    assert process._remove_private_tree(renamed)


def mount_table_naming(root: Path, locate: Callable[[Path], Path]) -> Callable[[], str]:
    real_mountinfo = procfs.RealProcView.mountinfo

    def answer() -> str:
        location = os.path.realpath(locate(the_only_tree(root)))
        mount = mountinfo_line(mount_point=location.replace(" ", "\\040"))
        return real_mountinfo(procfs.RealProcView()) + "\n" + mount

    return answer


@contextmanager
def mountinfo_answering(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[], str] | OSError,
) -> Iterator[None]:
    def mountinfo(_view: procfs.RealProcView) -> str:
        if isinstance(answer, OSError):
            raise answer
        return answer()

    with monkeypatch.context() as patched:
        patched.setattr(procfs.RealProcView, "mountinfo", mountinfo)
        yield


def the_tree(tree: Path) -> Path:
    return tree


def its_cli_directory(tree: Path) -> Path:
    return tree / "home" / ".cli"


def a_root_named(name: str) -> Callable[[Path], Path]:
    return lambda parent: a_private_root(parent, name)


def a_root_behind_a_link(parent: Path) -> Path:
    real = parent / "real"
    real.mkdir()
    a_private_root(real)
    (parent / "alias").symlink_to(real)
    return parent / "alias" / "root"


@pytest.mark.parametrize(
    ("make_root", "locate", "reason"),
    (
        pytest.param(a_root_named("root"), the_tree, "mount_point", id="at-the-tree"),
        pytest.param(a_root_named("root"), its_cli_directory, "mount_point", id="below-the-tree"),
        pytest.param(
            a_root_named("root with space"), the_tree, "mount_point", id="escaped-mount-point",
        ),
        pytest.param(a_root_behind_a_link, the_tree, "mount_point", id="root-behind-a-link"),
        pytest.param(a_root_named("root"), None, "mount_table_unreadable", id="unreadable-table"),
    ),
)
def test_a_tree_holding_a_mount_point_or_an_unreadable_mount_table_is_left_and_logged_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    make_root: Callable[[Path], Path],
    locate: Callable[[Path], Path] | None,
    reason: str,
) -> None:
    root = make_root(tmp_path)
    answer = (
        PermissionError(errno.EACCES, "synthetic")
        if locate is None
        else mount_table_naming(root, locate)
    )

    with mountinfo_answering(monkeypatch, answer):
        enter_run_directories(root, FILES)

    leftover = the_only_tree(root)
    assert (
        removal_errors(caplog), sorted(entry.name for entry in (leftover / "home").iterdir()),
    ) == ([f"run directory removal refused ({reason})"], ["empty", "prompt"])
    enter_run_directories(root, FILES)
    assert list(root.glob(TREE_PATTERN)) == [leftover]
    remove_the_leftover(leftover)


def test_a_mount_at_the_root_itself_refuses_nothing(
    private_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    at_the_root = mount_table_naming(private_root, lambda tree: tree.parent)

    with mountinfo_answering(monkeypatch, at_the_root):
        enter_run_directories(private_root, FILES)

    assert (list(private_root.iterdir()), removal_errors(caplog)) == ([], [])


def test_the_mount_table_is_read_once_before_the_deletion_and_a_later_change_does_not_matter(
    private_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    real_mountinfo = procfs.RealProcView.mountinfo
    reads = 0

    def clean_then_mounted() -> str:
        nonlocal reads
        reads += 1
        events.append("mountinfo")
        table = real_mountinfo(procfs.RealProcView())
        if reads == 1:
            return table
        mount_point = str(its_cli_directory(the_only_tree(private_root)))
        return table + "\n" + mountinfo_line(mount_point=mount_point)

    with rmtree_counted(monkeypatch, events), mountinfo_answering(monkeypatch, clean_then_mounted):
        enter_run_directories(private_root, FILES)

    assert (events, list(private_root.iterdir()), removal_errors(caplog)) == (
        ["mountinfo", "rmtree"], [], [],
    )


def run_and_drop_every_reference(
    root: Path,
    ending: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_error = RuntimeError("body failed")
    with rmtree_refused(monkeypatch):
        directories_context = run_directories(PrivateWorkingRoot(root), FILES)
        if ending == "raising":
            with pytest.raises(RuntimeError) as raised:
                with directories_context:
                    raise body_error
            assert raised.value is body_error
            del raised
        else:
            with directories_context as directories:
                if ending == "double-release":
                    reservation = directories.reserve()
                    reservation.release()
                    reservation.release()
                    del reservation
                del directories


@pytest.mark.parametrize("ending", ("returning", "raising", "double-release"))
def test_an_unremovable_tree_is_logged_once_never_retried_and_holds_no_later_run(
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ending: str,
) -> None:
    run_and_drop_every_reference(private_root, ending, monkeypatch)
    gc.collect()

    leftover = the_only_tree(private_root)
    assert removal_errors(caplog) == [REMOVAL_FAILED]
    enter_run_directories(private_root, FILES)
    assert (list(private_root.glob(TREE_PATTERN)), removal_errors(caplog)) == (
        [leftover], [REMOVAL_FAILED],
    )
    remove_the_leftover(leftover)


def test_a_reservation_after_the_context_ended_is_refused(private_root: Path) -> None:
    with run_directories(PrivateWorkingRoot(private_root), {}) as directories:
        pass

    with pytest.raises(RuntimeError, match=RESERVATIONS_CLOSED):
        directories.reserve()

    assert list(private_root.iterdir()) == []


def test_a_hundred_real_runs_leave_the_root_empty(real_runs: RealRuns) -> None:
    outside_before = real_runs.outside_snapshot()
    names: set[str] = set()

    def remember_the_tree(directories: RunDirectories) -> None:
        names.add(directories.home.parent.name)

    for _ in range(100):
        real_runs.drive(real_runs.run(before_removal=remember_the_tree))
        assert_a_clean_real_run(real_runs, outside_before)

    assert len(names) == 100


@dataclass
class RunScope:
    """Owns what a row leaves open: reservations and retained groups."""

    reservations: list[HolderReservation] = field(default_factory=list)
    retained: list[tuple[ScriptedClaudeChild, RetainedGroup]] = field(default_factory=list)

    def hold(self, reservation: HolderReservation) -> HolderReservation:
        self.reservations.append(reservation)
        return reservation

    def retain(self, child: ScriptedClaudeChild, group: RetainedGroup) -> None:
        self.retained.append((child, group))


@pytest.fixture
def run_scope(monkeypatch: pytest.MonkeyPatch) -> Iterator[RunScope]:
    scope = RunScope()
    yield scope
    for reservation in scope.reservations:
        reservation.release()

    async def discharge() -> None:
        for child, group in scope.retained:
            assert await confirm_the_retained_group_gone(child, group)

    with recorded_census_workers(monkeypatch):
        run_bounded(discharge())


@pytest.mark.parametrize(
    "reservation_released_first", (False, True), ids=("context-first", "release-first"),
)
def test_the_tree_is_removed_by_one_attempt_made_by_the_last_of_the_context_and_its_reservation(
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_scope: RunScope,
    reservation_released_first: bool,
) -> None:
    def trees_standing() -> int:
        return len(list(private_root.glob(TREE_PATTERN)))

    with rmtree_counted(monkeypatch) as attempts:
        with run_directories(PrivateWorkingRoot(private_root), {}) as directories:
            reservation = run_scope.hold(directories.reserve())
            if reservation_released_first:
                reservation.release()
                trees_after_the_first_holder = trees_standing()
        if not reservation_released_first:
            trees_after_the_first_holder = trees_standing()
            reservation.release()

    assert (trees_after_the_first_holder, list(private_root.iterdir()), attempts) == (
        1, [], ["rmtree"],
    )


async def lost_group_of(directories: RunDirectories) -> RetainedGroup:
    spec = ChildProcess(
        binary=Path(sys.executable),
        arguments=("-I",),
        environment=closed_environment(directories.home),
        working_directory=directories.working_directory,
    )
    with pytest.raises(ContainmentLost) as lost:
        async with anchored_turn_child(
            CLAUDE_CLI_PROCESS_POOL, spec, home_reservation=directories.reserve(),
        ):
            pass
    return lost.value.group


def drive_a_retained_run(
    monkeypatch: pytest.MonkeyPatch,
    scenario: Callable[[ScriptedClaudeChild], Coroutine[Any, Any, None]],
) -> None:
    child = a_child_that_outlives_sigkill(censuses=(GroupCensus.LIVE, GroupCensus.EMPTY))
    install_claude_cli_children(monkeypatch, child)
    reap_without_waiting(monkeypatch)
    with recorded_census_workers(monkeypatch), killpg_forbidden():
        run_bounded(scenario(child))


def test_a_retained_groups_directories_stay_until_its_group_is_confirmed_gone(
    private_root: Path, monkeypatch: pytest.MonkeyPatch, run_scope: RunScope,
) -> None:
    async def scenario(child: ScriptedClaudeChild) -> None:
        with run_directories(PrivateWorkingRoot(private_root), FILES) as directories:
            group = await lost_group_of(directories)
            run_scope.retain(child, group)
        tree = the_only_tree(private_root)
        assert (held_claude_slots(), sorted(entry.name for entry in tree.iterdir())) == (
            1, ["home", "work"],
        )
        assert not await lost_groups.confirm_group_gone(group)
        assert (held_claude_slots(), tree.exists()) == (1, True)
        assert await confirm_the_retained_group_gone(child, group)

    drive_a_retained_run(monkeypatch, scenario)

    assert (held_claude_slots(), list(private_root.iterdir())) == (0, [])


def test_a_retained_groups_directories_wait_for_the_context_when_the_group_is_confirmed_first(
    private_root: Path, monkeypatch: pytest.MonkeyPatch, run_scope: RunScope,
) -> None:
    async def scenario(child: ScriptedClaudeChild) -> None:
        with run_directories(PrivateWorkingRoot(private_root), FILES) as directories:
            group = await lost_group_of(directories)
            run_scope.retain(child, group)
            assert not await lost_groups.confirm_group_gone(group)
            assert await confirm_the_retained_group_gone(child, group)
            assert (held_claude_slots(), directories.home.is_dir()) == (0, True)

    drive_a_retained_run(monkeypatch, scenario)

    assert list(private_root.iterdir()) == []
