"""Protocol models for the kernel's ``waitid`` as the anchored leader's watcher sees it.

They replace ``os.waitid`` for ``agent_providers.spawn`` only; the child owner binds its own
``waitid`` at import and never passes through them. They forward the code under test's numeric
calls, so they live outside the support module that names no pid number. Each model owns its
call log, its holds and its violation list: a protocol break is recorded, never raised inside
the watcher, and a hold that the test never opens lets the call through after the bound.
"""

from __future__ import annotations

import os
import signal
import threading
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace

from anchored_support import TEST_WAIT_SECONDS

real_waitid = os.waitid

WATCHER_THREAD_PREFIX = "child-exit:"
OBSERVE_EXIT = os.WEXITED | os.WNOWAIT
CONSUME_STOP_REPORT = os.WSTOPPED | os.WNOHANG
SPIN_LIMIT = 3


@dataclass(frozen=True)
class WaitidCall:
    thread: str
    pid: int
    options: int


def traced_stop() -> SimpleNamespace:
    return SimpleNamespace(si_code=os.CLD_TRAPPED, si_status=int(signal.SIGTRAP))


class RecordingKernel:
    def __init__(self) -> None:
        self._changed = threading.Condition()
        self.calls: list[WaitidCall] = []
        self.violations: list[str] = []

    def await_calls(self, count: int) -> None:
        with self._changed:
            if not self._changed.wait_for(lambda: len(self.calls) >= count, TEST_WAIT_SECONDS):
                raise AssertionError(f"fewer than {count} waitid calls within the bound")

    def _record(self, pid: int, options: int) -> int:
        with self._changed:
            self.calls.append(WaitidCall(threading.current_thread().name, pid, options))
            self._changed.notify_all()
            return len(self.calls)

    def _violate(self, message: str) -> None:
        with self._changed:
            self.violations.append(message)


class PassThroughKernel(RecordingKernel):
    """Logs every call and forwards it, optionally holding one before its entry."""

    def __init__(self, *, hold_before_call: int | None = None) -> None:
        super().__init__()
        self._hold_before_call = hold_before_call
        self._opened = threading.Event()

    def open_hold(self) -> None:
        self._opened.set()

    def __call__(self, idtype: int, pid: int, options: int):
        position = self._record(pid, options)
        if position == self._hold_before_call and not self._opened.wait(TEST_WAIT_SECONDS):
            self._violate(f"call {position} was held past the bound")
        return real_waitid(idtype, pid, options)


class KernelModel(RecordingKernel):
    """A stateful ``waitid`` with scripted traced stops and an optional scripted loss.

    A wait returns a traced stop while one is queued and only a consume call removes it;
    afterwards waits reach the real kernel. A loss is what the kernel reports after a foreign
    reap, answered without one, and every later call for that pid is a violation.
    """

    def __init__(self, *, stops: int = 0, loses_at: str | None = None) -> None:
        super().__init__()
        self._stops = deque(traced_stop() for _ in range(stops))
        self._loses_at = loses_at
        self._lost = False
        self._stops_without_consumption = 0

    def __call__(self, idtype: int, pid: int, options: int):
        self._record(pid, options)
        refusal = self._refusal(idtype, pid, options)
        if refusal:
            self._violate(refusal)
            raise ChildProcessError(refusal)
        if options & os.WEXITED:
            return self._observe_exit(pid, options)
        return self._consume_stop_report(pid, options)

    def _refusal(self, idtype: int, pid: int, options: int) -> str | None:
        if idtype != os.P_PID or pid != self._watched_pid():
            return f"a call named pid {pid}, not the watched leader"
        if self._lost:
            return "a call followed a reported loss"
        if options & os.WEXITED and options & os.WSTOPPED:
            return "a stop consumer carried WEXITED and could reap"
        if options & os.WEXITED and options != OBSERVE_EXIT:
            return f"an exit observation carried options {options:#x}"
        if not options & os.WEXITED and options & ~os.WNOWAIT != CONSUME_STOP_REPORT:
            return f"a call carried options {options:#x}"
        return None

    @staticmethod
    def _watched_pid() -> int:
        return int(threading.current_thread().name.removeprefix(WATCHER_THREAD_PREFIX))

    def _observe_exit(self, pid: int, options: int):
        with self._changed:
            if self._loses_at == "wait":
                self._lost = True
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
                self._lost = True
                raise ChildProcessError("scripted loss at the stop consumption")
            if not self._stops:
                return None
            report = self._stops[0]
            if not options & os.WNOWAIT:
                self._stops.popleft()
                self._stops_without_consumption = 0
            return report
