"""Protocol models for the kernel's ``waitid`` and ``waitpid`` as the anchored leader sees them.

They replace ``os.waitid`` (and, through ``waitpid``, ``os.waitpid``) for
``agent_providers.spawn`` only; the child owner binds its own ``waitid`` at import and never
passes through them. They forward the code under test's numeric
calls, so they live outside the support module that names no pid number. Each model owns its
call log, its holds and its violation list: a protocol break is recorded, never raised inside
the watcher, and a hold that the test never opens lets the call through after the bound.
:class:`GroupSignalModel` does the same for ``os.killpg``, and :class:`CensusRecorder` does the
same for ``procfs.group_census``.
"""

from __future__ import annotations

import os
import signal
import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Literal

import pytest
from anchored_support import TEST_WAIT_SECONDS

from agent_providers.procfs import GroupCensus, ProcView, group_census

real_waitid = os.waitid
real_waitpid = os.waitpid
real_killpg = os.killpg

WATCHER_THREAD_PREFIX = "child-exit:"
OBSERVE_EXIT = os.WEXITED | os.WNOWAIT
CONSUME_STOP_REPORT = os.WSTOPPED | os.WNOHANG
SPIN_LIMIT = 3


@dataclass(frozen=True)
class WaitidCall:
    thread: str
    pid: int
    options: int
    name: str = "waitid"


def watcher_thread(pid: int) -> threading.Thread:
    name = f"{WATCHER_THREAD_PREFIX}{pid}"
    watcher = next((t for t in threading.enumerate() if t.name == name), None)
    assert watcher is not None, f"no {name} thread exists"
    return watcher


@dataclass
class WatcherStarts:
    """The watcher threads started so far, kept whether or not they have already ended."""

    _threads: dict[int, threading.Thread] = field(default_factory=dict)

    def keep(self, watcher: threading.Thread) -> None:
        self._threads[int(watcher.name.removeprefix(WATCHER_THREAD_PREFIX))] = watcher

    def thread(self, pid: int) -> threading.Thread:
        assert pid in self._threads, f"no {WATCHER_THREAD_PREFIX}{pid} thread was started"
        return self._threads[pid]


@contextmanager
def recorded_watcher_starts(monkeypatch: pytest.MonkeyPatch) -> Iterator[WatcherStarts]:
    """Keep every watcher thread at its start, for a leader that can exit before it is looked up."""
    starts = WatcherStarts()
    real_start = threading.Thread.start

    def start(thread: threading.Thread) -> None:
        if thread.name.startswith(WATCHER_THREAD_PREFIX):
            starts.keep(thread)
        real_start(thread)

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", start)
        yield starts


def watcher_names() -> set[str]:
    return {t.name for t in threading.enumerate() if t.name.startswith(WATCHER_THREAD_PREFIX)}


def join_watcher(watcher: threading.Thread) -> None:
    watcher.join(TEST_WAIT_SECONDS)
    assert not watcher.is_alive(), "the watcher did not end within the bound"


def traced_stop() -> SimpleNamespace:
    return SimpleNamespace(si_code=os.CLD_TRAPPED, si_status=int(signal.SIGTRAP))


WaitpidAnswer = tuple[int, int] | Exception


class RecordingKernel:
    """One ordered log of ``waitid`` and ``waitpid`` calls, with holds and refuse-after-reap.

    A hold is the n-th call of its name, before the real call is made (``hold_before_call``,
    ``hold_before_waitpid``) or after it returned (``hold_after_call``, which sets ``reached``).
    A pid that a forwarded ``waitpid`` returned, or that an injected loss named, is reaped for
    good: every later call naming it is a recorded violation answered with ``ChildProcessError``
    and never forwarded. A ``waitpid`` is admitted only with ``reap_options``, the non-blocking
    reap of a release by default.
    """

    def __init__(
        self,
        *,
        hold_before_call: int | None = None,
        hold_after_call: int | None = None,
        hold_before_waitpid: int | None = None,
        waitpid_answer: WaitpidAnswer | None = None,
        reap_options: int = os.WNOHANG,
    ) -> None:
        self._changed = threading.Condition()
        self.calls: list[WaitidCall] = []
        self.violations: list[str] = []
        self.reached = threading.Event()
        self._opened = threading.Event()
        self._holds = {
            ("waitid", "before"): hold_before_call,
            ("waitid", "after"): hold_after_call,
            ("waitpid", "before"): hold_before_waitpid,
        }
        self._waitpid_answer = waitpid_answer
        self._reap_options = reap_options
        self._reaped: set[int] = set()
        self._leader_pid: int | None = None

    def bind(self, leader_pid: int) -> None:
        self._leader_pid = leader_pid

    def answer_next_waitpid(self, answer: WaitpidAnswer) -> None:
        self._waitpid_answer = answer

    def open_hold(self) -> None:
        self._opened.set()

    def await_reached(self) -> None:
        assert self.reached.wait(TEST_WAIT_SECONDS), "the hold after the real call was not reached"

    def await_calls(self, count: int, name: Literal["waitid", "waitpid"]) -> None:
        with self._changed:
            if not self._changed.wait_for(
                lambda: sum(call.name == name for call in self.calls) >= count, TEST_WAIT_SECONDS
            ):
                raise AssertionError(f"fewer than {count} {name} calls within the bound")

    def waitpid(self, pid: int, options: int) -> tuple[int, int]:
        position = self._record(pid, options, "waitpid")
        self._hold(position, "waitpid", "before")
        with self._changed:
            refusal = self._waitpid_refusal(pid, options)
            if refusal:
                self._violate(refusal)
                raise ChildProcessError(refusal)
            answer, self._waitpid_answer = self._waitpid_answer, None
            if isinstance(answer, Exception):
                self._reaped.add(pid)
                raise answer
            if answer is not None:
                return answer
            result = real_waitpid(pid, options)
            if result[0] == pid:
                self._reaped.add(pid)
            return result

    def _waitpid_refusal(self, pid: int, options: int) -> str | None:
        if pid != self._leader_pid:
            return f"a waitpid named pid {pid}, not the bound leader"
        if options != self._reap_options:
            return f"a waitpid carried options {options:#x}"
        return self._reaped_refusal(pid)

    def _reaped_refusal(self, pid: int) -> str | None:
        if pid in self._reaped:
            return f"a call named pid {pid} after it was reaped"
        return None

    def _record(self, pid: int, options: int, name: str = "waitid") -> int:
        with self._changed:
            self.calls.append(WaitidCall(threading.current_thread().name, pid, options, name))
            self._changed.notify_all()
            return sum(call.name == name for call in self.calls)

    def _hold(self, position: int, name: str, when: Literal["before", "after"]) -> None:
        if position != self._holds[name, when]:
            return
        if when == "after":
            self.reached.set()
        if not self._opened.wait(TEST_WAIT_SECONDS):
            self._violate(f"{name} call {position} was held past the bound")

    def _violate(self, message: str) -> None:
        with self._changed:
            self.violations.append(message)


class PassThroughKernel(RecordingKernel):
    """Logs every call and forwards it, optionally holding one before or after the real call."""

    def __call__(self, idtype: int, pid: int, options: int):
        position = self._record(pid, options)
        self._hold(position, "waitid", "before")
        with self._changed:
            refusal = self._reaped_refusal(pid)
        if refusal:
            self._violate(refusal)
            raise ChildProcessError(refusal)
        reported = real_waitid(idtype, pid, options)
        self._hold(position, "waitid", "after")
        return reported


class KernelModel(RecordingKernel):
    """A stateful ``waitid`` with scripted traced stops and an optional scripted loss.

    A wait returns a traced stop while one is queued and only a consume call removes it;
    afterwards waits reach the real kernel. A loss is what the kernel reports after a foreign
    reap, answered without one, and every later call for that pid is a violation.
    """

    def __init__(
        self,
        *,
        stops: int = 0,
        loses_at: str | None = None,
        hold_before_call: int | None = None,
        hold_after_call: int | None = None,
        hold_before_waitpid: int | None = None,
    ) -> None:
        super().__init__(
            hold_before_call=hold_before_call,
            hold_after_call=hold_after_call,
            hold_before_waitpid=hold_before_waitpid,
        )
        self._stops = deque(traced_stop() for _ in range(stops))
        self._loses_at = loses_at
        self._stops_without_consumption = 0

    def __call__(self, idtype: int, pid: int, options: int):
        position = self._record(pid, options)
        self._hold(position, "waitid", "before")
        refusal = self._refusal(idtype, pid, options)
        if refusal:
            self._violate(refusal)
            raise ChildProcessError(refusal)
        if options & os.WEXITED:
            reported = self._observe_exit(pid, options)
        else:
            reported = self._consume_stop_report(pid, options)
        self._hold(position, "waitid", "after")
        return reported

    def _refusal(self, idtype: int, pid: int, options: int) -> str | None:
        if idtype != os.P_PID or pid != self._watched_pid():
            return f"a call named pid {pid}, not the watched leader"
        with self._changed:
            reaped = self._reaped_refusal(pid)
        if reaped:
            return reaped
        if options & os.WEXITED and options & os.WSTOPPED:
            return "a stop consumer carried WEXITED and could reap"
        if options & os.WEXITED and options != OBSERVE_EXIT:
            return f"an exit observation carried options {options:#x}"
        if not options & os.WEXITED and options & ~os.WNOWAIT != CONSUME_STOP_REPORT:
            return f"a call carried options {options:#x}"
        return None

    @staticmethod
    def _watched_pid() -> int | None:
        name = threading.current_thread().name
        if not name.startswith(WATCHER_THREAD_PREFIX):
            return None
        return int(name.removeprefix(WATCHER_THREAD_PREFIX))

    def _observe_exit(self, pid: int, options: int):
        with self._changed:
            if self._loses_at == "wait":
                self._reaped.add(pid)
                raise ChildProcessError("scripted loss at the exit observation")
            if self._stops and self._stops_without_consumption < SPIN_LIMIT:
                self._stops_without_consumption += 1
                return self._stops[0]
            if self._stops:
                self.violations.append("stop reports were repeated without a consumption")
                self._stops.clear()
        return real_waitid(os.P_PID, pid, options)

    def _consume_stop_report(self, pid: int, options: int):
        with self._changed:
            if self._loses_at == "consume":
                self._reaped.add(pid)
                raise ChildProcessError("scripted loss at the stop consumption")
            if not self._stops:
                return None
            report = self._stops[0]
            if not options & os.WNOWAIT:
                self._stops.popleft()
                self._stops_without_consumption = 0
            return report


def install_kernel(patch: pytest.MonkeyPatch, kernel: RecordingKernel) -> None:
    patch.setattr(os, "waitid", kernel)
    patch.setattr(os, "waitpid", kernel.waitpid)


@dataclass(frozen=True)
class GroupSignalCall:
    thread: str
    pgid: int
    sig: int


class GroupSignalModel:
    """``os.killpg`` as the anchored leader's group signal sees it.

    Every call is logged. A call naming a group other than the bound leader's is a recorded
    violation and is never forwarded. ``during`` runs first, before the model answers or
    forwards. A ``failure`` answers the first call only and is never forwarded; ``forward=False``
    answers every call with nothing.
    """

    def __init__(
        self,
        *,
        forward: bool = True,
        failure: OSError | None = None,
        during: Callable[[GroupSignalCall], None] | None = None,
    ) -> None:
        self.calls: list[GroupSignalCall] = []
        self.violations: list[str] = []
        self._forward = forward
        self._failure = failure
        self._during = during
        self._leader_pid: int | None = None

    def bind(self, leader_pid: int) -> None:
        self._leader_pid = leader_pid

    def install(self, patch: pytest.MonkeyPatch) -> None:
        patch.setattr(os, "killpg", self)

    def __call__(self, pgid: int, sig: int) -> None:
        call = GroupSignalCall(threading.current_thread().name, pgid, int(sig))
        self.calls.append(call)
        if pgid != self._leader_pid:
            self.violations.append(f"a group signal named {pgid}, not the bound leader")
            return
        if self._during is not None:
            self._during(call)
        failure, self._failure = self._failure, None
        if failure is not None:
            raise failure
        if self._forward:
            real_killpg(pgid, sig)


class EventLog(list[tuple[str, str]]):
    def record(self, kind: str) -> None:
        self.append((kind, threading.current_thread().name))

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _ in self]


class CensusRecorder(list[int]):
    def __init__(self) -> None:
        super().__init__()
        self.log: EventLog | None = None
        self.answer: GroupCensus | None = None
        self.answers: deque[GroupCensus] = deque()
        self.failure: Exception | None = None
        self.during: Callable[[], None] | None = None

    def __call__(self, view: ProcView, pgid: int) -> GroupCensus:
        self.append(pgid)
        if self.log is not None:
            self.log.record("count")
        if self.during is not None:
            self.during()
        if self.failure is not None:
            raise self.failure
        if self.answers:
            return self.answers.popleft()
        if self.answer is not None:
            return self.answer
        return group_census(view, pgid)
