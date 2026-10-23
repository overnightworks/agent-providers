"""Bounded probes for mounted agent CLIs."""

from __future__ import annotations

import atexit
import concurrent.futures
import errno
import functools
import json
import logging
import operator
import os
import selectors
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from pathlib import Path
from types import TracebackType
from typing import (
    Any,
    Final,
    Literal,
    NotRequired,
    Protocol,
    Required,
    TypedDict,
    Unpack,
    cast,
)

from agent_runtime import grok_login, procfs
from agent_runtime.codex_login import CodexLoginVerdict, assess_codex_login, blanked_copy
from agent_runtime.config import (
    PrivateWorkingRoot,
    current_claude_runtime,
    current_codex_runtime,
    current_config,
    current_grok_runtime,
)
from agent_runtime.constants import (
    CLAUDE_CLI_AUTH_METHOD_FIELD,
    CLAUDE_CLI_LOGGED_IN_FIELD,
    CLAUDE_CLI_STATUS_ARGS,
    CLAUDE_OAUTH_TOKEN_VARIABLE,
    CLAUDE_RUN_SWITCHES,
    CLI_GROUP_CONFIRMATION_SECONDS,
    CLI_LOGIN_STATUS_CACHE_SECONDS,
    CLI_OUTPUT_READ_LIMIT_BYTES,
    CLI_TERMINATION_GRACE_SECONDS,
    CODEX_CLI_LOGGED_IN_MARKER,
    CODEX_CLI_LOGGED_OUT_MARKER,
    CODEX_CLI_MODELS_ARGS,
    CODEX_CLI_STATUS_ARGS,
    GROK_CLI_LOGGED_IN_MARKER,
    GROK_CLI_LOGGED_OUT_MARKER,
    GROK_CLI_MODEL_BULLETS,
    GROK_CLI_MODEL_LIST_MARKER,
    GROK_CLI_STATUS_ARGS,
    PROBE_TIMEOUT_SECONDS,
)
from agent_runtime.errors import raise_without_chain
from agent_runtime.secret_file import UnsoundSecretFile, read_token_file
from agent_runtime.spawn import (
    BlockingAnchoredChild,
    ChildProcess,
    ChildProcessSpecificationError,
    StderrPolicy,
    closed_environment,
    open_blocking_anchored_pipes,
)

CODEX_CLI_MODEL_CATALOG_OUTPUT_READ_LIMIT_BYTES: Final = 4 * 1024 * 1024


class AgentCliUnavailableError(Exception):
    """A CLI cannot serve this call.

    Raised directly only for a catalogue answer that breaks its contract.
    """


class CliLoginUnusableError(AgentCliUnavailableError):
    """The configured CLI login is missing, unreadable, invalid or too large.

    Or the CLI reported no login status.
    """


class CliEnvironmentCause(StrEnum):
    """Why a CLI cannot run here, whatever its login."""

    BINARY_MISSING = "binary_missing"
    HOME_NOT_CREATED = "home_not_created"
    CREDENTIAL_COPY_NOT_WRITTEN = "credential_copy_not_written"


type _EnvironmentReduction = tuple[
    Callable[[str, CliEnvironmentCause], CliEnvironmentUnavailableError],
    tuple[str, CliEnvironmentCause],
    dict[str, Any],
]


class CliEnvironmentUnavailableError(AgentCliUnavailableError):
    """The CLI cannot run here.

    Its binary is missing, or its private home or credential copy cannot be made.
    """

    def __init__(self, detail: str, *, cause: CliEnvironmentCause) -> None:
        super().__init__(detail)
        self.cause = cause

    def __reduce__(self) -> _EnvironmentReduction:
        return (_restore_environment_error, (str(self), self.cause), self.__dict__)


def _restore_environment_error(
    detail: str, cause: CliEnvironmentCause,
) -> CliEnvironmentUnavailableError:
    return CliEnvironmentUnavailableError(detail, cause=cause)


class CliProbeBudgetExceeded(AgentCliUnavailableError):
    """Raised when one caller outwaits a still-running cached probe."""


@dataclass(frozen=True)
class CliLogin:
    """The subscription login reported by an agent CLI."""

    logged_in: bool
    auth_method: str | None


@dataclass(frozen=True)
class GrokCliStatus:
    """The login and model catalog emitted by ``grok models``."""

    login: CliLogin
    model_names: tuple[str, ...]


@dataclass(frozen=True)
class CliRun:
    """The bounded output and completion state of one CLI invocation."""

    returncode: int | None
    stdout: str
    stderr: str
    complete: bool


class CliRunReason(str, Enum):
    """Why a bounded CLI invocation did or did not complete."""

    SPAWN_FAILED = "spawn_failed"
    DEADLINE_BEFORE_SPAWN = "deadline_before_spawn"
    DEADLINE_WHILE_WRITING = "deadline_while_writing"
    DEADLINE_WHILE_READING = "deadline_while_reading"
    IO_ERROR = "io_error"
    OUTPUT_LIMIT_REACHED = "output_limit_reached"
    OUTPUT_CHANNEL_FULL = "output_channel_full"
    CANCELLED = "cancelled"
    CLEANUP_OVERRAN = "cleanup_overran"
    COMPLETE = "complete"


@dataclass(frozen=True)
class CliRunOutcome:
    """The result of a bounded CLI invocation and its cleanup.

    ``became_zombie`` is true when the reap did not confirm the group gone: the reap goes on
    in the background, and ``on_reaped(pid, True)`` follows only a later confirmation.
    ``returncode`` is the leader's published code, or ``None``.
    """

    started: bool
    spawn_error: BaseException | None
    returncode: int | None
    stdout: str
    stderr: str
    complete: bool
    became_zombie: bool
    reason: CliRunReason
    io_error: OSError | None = None


_RUNNER_TIMEOUT_REASONS = frozenset(
    {CliRunReason.DEADLINE_BEFORE_SPAWN, CliRunReason.CLEANUP_OVERRAN}
)


class CliLineChannel:
    """A bounded stream of complete stdout lines from one CLI run."""

    def __init__(self, maximum_lines: int) -> None:
        if maximum_lines < 1:
            raise ValueError("CLI line channel must hold at least one line")
        self._maximum_lines = maximum_lines
        self._lines: deque[bytes] = deque()
        self._outcome: CliRunOutcome | None = None
        self._abort_requested = threading.Event()
        self._condition = threading.Condition()

    def request_abort(self) -> None:
        self._abort_requested.set()
        with self._condition:
            self._lines.clear()
            self._condition.notify_all()

    def abort_requested(self) -> bool:
        return self._abort_requested.is_set()

    def receive(
        self,
        timeout: float | None = None,
        *,
        settle_by: float | None = None,
    ) -> bytes | CliRunOutcome:
        give_up_at = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                if self._lines:
                    return self._lines.popleft()
                if self._outcome is not None:
                    return self._outcome
                now = time.monotonic()
                if settle_by is not None and now >= settle_by:
                    return self._terminate_with(_cleanup_overran_outcome(started=True))
                if give_up_at is not None and now >= give_up_at:
                    raise TimeoutError("CLI line channel did not produce an item in time")
                wake_instants = [
                    instant for instant in (give_up_at, settle_by) if instant is not None
                ]
                self._condition.wait(timeout=min(wake_instants) - now if wake_instants else None)

    def _send(self, line: bytes) -> bool:
        with self._condition:
            if (
                self._outcome is not None
                or self._abort_requested.is_set()
                or len(self._lines) >= self._maximum_lines
            ):
                return False
            self._lines.append(line)
            self._condition.notify_all()
            return True

    def _close(self, outcome: CliRunOutcome) -> CliRunOutcome:
        with self._condition:
            return self._terminate_with(outcome)

    def _terminate_with(self, outcome: CliRunOutcome) -> CliRunOutcome:
        if self._outcome is None:
            if not outcome.complete:
                self._lines.clear()
            self._outcome = outcome
            if outcome.reason in _RUNNER_TIMEOUT_REASONS:
                self._abort_requested.set()
            self._condition.notify_all()
        return self._outcome


def _cleanup_overran_outcome(*, started: bool) -> CliRunOutcome:
    return CliRunOutcome(
        started=started,
        spawn_error=None,
        returncode=None,
        stdout="",
        stderr="",
        complete=False,
        became_zombie=False,
        reason=CliRunReason.CLEANUP_OVERRAN,
    )


@dataclass
class _BoundedRunState:
    started: threading.Event = field(default_factory=threading.Event)
    completed: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    outcome: CliRunOutcome | None = None

    def record(self, outcome: CliRunOutcome) -> CliRunOutcome:
        with self.lock:
            if self.outcome is None:
                self.outcome = outcome
            recorded = self.outcome
        self.completed.set()
        return recorded


class StepFailures:
    """The steps of a run that raised, kept by name and exception class only."""

    def __init__(self) -> None:
        self._failed: list[str] = []
        self._errors: dict[str, BaseException] = {}
        self._first_control_flow: BaseException | None = None

    @contextmanager
    def contained(self, step: str, *, reported: bool = True) -> Iterator[None]:
        try:
            yield
        except BaseException as error:  # NOSONAR(S5754) every exit publishes; callers re-raise
            self.record(step, error, reported=reported)

    def record(self, step: str, error: BaseException, *, reported: bool = True) -> None:
        self._errors[step] = error
        if self._first_control_flow is None and not isinstance(error, Exception):
            self._first_control_flow = error
        if reported:
            self._failed.append(f"{step} {type(error).__name__}")

    def failed(self, step: str) -> bool:
        return step in self._errors

    def error_raised_by(self, step: str) -> BaseException:
        return self._errors[step]

    def report(self) -> None:
        for failure in self._failed:
            with self.contained("report", reported=False):
                log.error("agent CLI runner step failed: %s", failure)

    def raise_control_flow(self) -> None:
        """Re-raise the first failure that is not an ``Exception`` (caller thread only)."""
        if self._first_control_flow is not None:
            raise self._first_control_flow


def _failed_runner_outcome() -> CliRunOutcome:
    return CliRunOutcome(
        started=False,
        spawn_error=None,
        returncode=None,
        stdout="",
        stderr="",
        complete=False,
        became_zombie=False,
        reason=CliRunReason.IO_ERROR,
    )


def run_publishing_failures(channel: CliLineChannel, runner_step: Callable[[], object]) -> None:
    """Run one runner step, containing every exception, then end the channel and name its class.

    A function rather than a context manager: the report must run outside the
    handler of the contained exception, or a failing log handler chains the
    exception's message into what it prints. Runs only on a runner thread,
    where neither ``KeyboardInterrupt`` nor ``SystemExit`` can be a signal,
    because Python delivers signals to the main thread. Never raises.
    """
    failures = StepFailures()
    with failures.contained("run"):
        runner_step()
    if failures.failed("run"):
        with failures.contained("publish"):
            channel._close(_failed_runner_outcome())
    failures.report()

LOGGED_OUT = CliLogin(logged_in=False, auth_method=None)

# A probe can spend its answer budget and then its reap: the termination grace,
# the group's count bound and the exit wait that follows a confirmed count. Give
# callers enough time to receive that outcome.
CLI_PROBE_CALLER_TIMEOUT_MARGIN_SECONDS = 0.1


def cli_settle_margin_seconds() -> float:
    return (
        CLI_TERMINATION_GRACE_SECONDS
        + 2 * CLI_GROUP_CONFIRMATION_SECONDS
        + CLI_PROBE_CALLER_TIMEOUT_MARGIN_SECONDS
    )


def cli_settle_deadline(deadline: float) -> float:
    """The instant by which a run with this deadline has published its outcome."""
    return deadline + cli_settle_margin_seconds()


CLI_PROBE_CALLER_TIMEOUT_SECONDS = PROBE_TIMEOUT_SECONDS + cli_settle_margin_seconds()
_BACKGROUND_REAP_LIMIT_SECONDS: Final = 60.0


@dataclass(frozen=True)
class _HeldGroup:
    process: BlockingAnchoredChild
    on_reaped: Callable[[int, bool], None] | None


_groups_held_until_exit: list[_HeldGroup] = []

log = logging.getLogger(__name__)

GROK_CLI_CREDENTIALS_INVALID_DETAIL: Final = "could not parse Grok CLI credentials"
CODEX_CLI_CREDENTIALS_INVALID_DETAIL: Final = "could not parse Codex CLI credentials"
CLAUDE_CLI_BINARY_MISSING_DETAIL: Final = (
    "the configured Claude CLI binary is not on the search path"
)
GROK_CLI_BINARY_MISSING_DETAIL: Final = "the configured Grok CLI binary is not on the search path"
CODEX_CLI_BINARY_MISSING_DETAIL: Final = "the configured Codex CLI binary is not on the search path"
RUN_TREE_PREFIX: Final = "agent-run-"
RUN_HOME_DIRECTORY_NAME: Final = "home"
RUN_WORK_DIRECTORY_NAME: Final = "work"
RUN_FILE_MODE: Final = 0o600
_RUN_DIRECTORY_MODE: Final = 0o700
_RUN_FILE_NAME_DETAIL: Final = "A run file needs a single path component as its name"
_CATALOG_HOME_PREFIX: Final = "agent-cli-catalog-"
_CATALOG_AUTH_FILE_PATH: Final = Path("auth.json")
CredentialCopy = Callable[[bytes], bytes | None]
_PRIVATE_HOME_COPY_MODE: Final = 0o400
_DESCRIPTOR_LINKS: Final = Path("/proc/self/fd")
_CREDENTIAL_COPY_FAILED_DETAIL: Final = "could not prepare the private CLI credential copy"
_PRIVATE_HOME_NOT_PREPARED_DETAIL: Final = "could not prepare the private CLI home"
_CREDENTIAL_FILE_READ_LIMIT_BYTES: Final = 256 * 1024
_CREDENTIAL_SYMLINK_DETAIL: Final = "CLI credential is a symbolic link"
_CREDENTIAL_NOT_REGULAR_DETAIL: Final = "CLI credential is not a regular file"
_CREDENTIAL_OVERSIZE_DETAIL: Final = "CLI credentials exceed the copy bound"
_CREDENTIAL_UNREADABLE_DETAIL: Final = "could not read CLI credentials"
_GROK_CREDENTIAL_HOME_VARIABLE: Final = "GROK_HOME"
_CODEX_CREDENTIAL_HOME_VARIABLE: Final = "CODEX_HOME"


def resolve_cli_binary(name_or_path: str) -> Path | None:
    """Resolve one configured CLI name against the host's named search path.

    Every child starts from an absolute binary, so a bare name is resolved
    here and nowhere against the parent's inherited ``PATH`` — that would put
    the choice of which binary runs back into the environment this layer
    closes.
    """
    candidate = Path(name_or_path)
    if candidate.is_absolute():
        return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None
    search_path = os.pathsep.join(
        str(directory) for directory in current_config().cli_binary_search_path
    )
    found = shutil.which(name_or_path, path=search_path)
    return Path(found) if found else None


def require_cli_binary(name_or_path: str, missing_detail: str) -> Path:
    """Resolve one configured CLI or say, as an environment fault, that it cannot run."""
    binary = resolve_cli_binary(name_or_path)
    if binary is None:
        raise CliEnvironmentUnavailableError(
            missing_detail, cause=CliEnvironmentCause.BINARY_MISSING,
        )
    return binary


class CachedProbe[T]:
    """A cached probe with a published future for each cold flight.

    The state lock only protects the cache and the future's publication.  The
    probe runs without it, so every caller waits at most its own answer budget
    instead of inheriting a predecessor's whole probe.
    """

    def __init__(self, probe: Callable[[], T]) -> None:
        self._probe = probe
        self._lock = threading.Lock()
        self._value: T | None = None
        self._failure: Exception | None = None
        self._answered_at = 0.0
        self._inflight: concurrent.futures.Future[T] | None = None
        self._generation = 0

    def get(self) -> T:
        with self._lock:
            if self._is_fresh():
                return self._answer()
        return self.refresh()

    def refresh(self) -> T:
        """Start or join one single-flight refresh."""
        with self._lock:
            if self._is_fresh():
                return self._answer()
            future = self._inflight
            if future is None:
                future = self._start_flight()

        deadline = time.monotonic() + CLI_PROBE_CALLER_TIMEOUT_SECONDS
        try:
            return future.result(timeout=max(deadline - time.monotonic(), 0))
        except concurrent.futures.TimeoutError as exc:
            raise CliProbeBudgetExceeded(
                "agent CLI probe did not answer within its caller budget",
            ) from exc

    def _start_flight(self) -> concurrent.futures.Future[T]:
        future: concurrent.futures.Future[T] = concurrent.futures.Future()
        worker = ClaimedStartThread(
            target=self._run_and_resolve,
            args=(future, self._generation),
            daemon=True,
        )
        try:
            worker.start()
        except BaseException:
            if worker.take_start_claim():
                raise
            self._inflight = future
            raise
        self._inflight = future
        return future

    def clear(self) -> None:
        with self._lock:
            self._value = None
            self._failure = None
            self._answered_at = 0.0
            self._generation += 1
            self._inflight = None

    def _run_and_resolve(
        self,
        future: concurrent.futures.Future[T],
        generation: int,
    ) -> None:
        try:
            result = self._probe()
        except Exception as exc:  # noqa: BLE001 - preserve a probe's failure for its TTL
            with self._lock:
                if generation == self._generation:
                    self._value = None
                    self._failure = exc
                    self._answered_at = time.monotonic()
                    if self._inflight is future:
                        self._inflight = None
            future.set_exception(exc)
        else:
            with self._lock:
                if generation == self._generation:
                    self._value = result
                    self._failure = None
                    self._answered_at = time.monotonic()
                    if self._inflight is future:
                        self._inflight = None
            future.set_result(result)

    def _is_fresh(self) -> bool:
        if self._value is None and self._failure is None:
            return False
        return time.monotonic() - self._answered_at < CLI_LOGIN_STATUS_CACHE_SECONDS

    def _answer(self) -> T:
        if self._failure is not None:
            raise self._failure
        if self._value is None:
            raise RuntimeError("A fresh CLI probe has no result")
        return self._value


class Reservation(Protocol):
    def release(self) -> None: ...


class RunnerReleases(TypedDict):
    on_spawn_failed: Callable[[], None]
    on_reaped: Callable[[int, bool], None]


def released_when_the_runner_ends(reservation: Reservation) -> RunnerReleases:
    return {
        "on_spawn_failed": reservation.release,
        "on_reaped": lambda _process_id, _became_zombie: reservation.release(),
    }


class HeldHome(Protocol):
    def reserve(self) -> Reservation: ...


def run_catalog_cli(
    child: ChildProcess,
    *,
    home: HeldHome | None = None,
    stderr: StderrPolicy = "capture",
    output_read_limit_bytes: int | None = None,
) -> CliRun | None:
    """Run one catalog probe under its own answer budget.

    A ``home`` stays reserved from here until the runner's callback frees it.
    """
    limit = (
        CLI_OUTPUT_READ_LIMIT_BYTES if output_read_limit_bytes is None else output_read_limit_bytes
    )
    reservation = None if home is None else home.reserve()
    outcome = run_cli_bounded(
        child,
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + PROBE_TIMEOUT_SECONDS,
        stderr=stderr,
        output_read_limit_bytes=limit,
        **({} if reservation is None else released_when_the_runner_ends(reservation)),
    )
    return _cli_run_from_outcome(outcome)


def run_claude_catalog_cli(
    binary: Path,
    args: tuple[str, ...],
    *,
    login: Mapping[str, str] | None = None,
    stderr: StderrPolicy = "capture",
    output_read_limit_bytes: int | None = None,
) -> CliRun | None:
    """Run a Claude catalog probe in an empty private home, ``login`` its only login.

    ``auth status`` passes the setup token as ``claude_token_environment()`` reads it;
    the model listing passes none, because the CLI lists its aliases without a login
    (A1b-0f, m4b). No login file is ever written into the home.
    """
    with closing(PrivateHome(prefix=_CATALOG_HOME_PREFIX)) as owner:
        return run_catalog_cli(
            ChildProcess(
                binary=binary,
                arguments=args,
                environment=closed_environment(owner.path, **(login or {}), **CLAUDE_RUN_SWITCHES),
                working_directory=owner.path,
            ),
            home=owner,
            stderr=stderr,
            output_read_limit_bytes=output_read_limit_bytes,
        )


def _cli_run_from_outcome(outcome: CliRunOutcome) -> CliRun | None:
    if outcome.reason in {
        CliRunReason.SPAWN_FAILED,
        CliRunReason.DEADLINE_BEFORE_SPAWN,
    }:
        return None
    return CliRun(
        returncode=outcome.returncode,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        complete=outcome.complete,
    )


class _CliRunKeywordArgs(TypedDict, total=False):
    stdin_payload: Required[bytes | None]
    read: Required[Literal["all", "first_line"]]
    deadline: Required[float]
    stderr: NotRequired[StderrPolicy]
    output_read_limit_bytes: NotRequired[int | None]
    on_spawned: NotRequired[Callable[[int], None] | None]
    on_spawn_failed: NotRequired[Callable[[], None] | None]
    on_reaped: NotRequired[Callable[[int, bool], None] | None]
    stdout_line_channel: NotRequired[CliLineChannel | None]
    prompt_file_bytes: NotRequired[bytes | None]
    prompt_file_arg_index: NotRequired[int | None]
    prompt_file_prefix: NotRequired[str | None]


@dataclass(frozen=True)
class _CliRunRequest:
    stdin_payload: bytes | None
    read: Literal["all", "first_line"]
    deadline: float
    stderr: StderrPolicy
    output_read_limit_bytes: int
    on_spawned: Callable[[int], None] | None
    on_spawn_failed: Callable[[], None] | None
    on_reaped: Callable[[int, bool], None] | None
    stdout_line_channel: CliLineChannel | None
    prompt_file_bytes: bytes | None
    prompt_file_arg_index: int | None
    prompt_file_prefix: str | None


class ClaimedStartThread(threading.Thread):
    """A thread whose starter and the thread itself race once for the right to act.

    ``Thread.start()`` can raise while the thread runs on. Whoever takes the
    claim first owns the thread's work; the other does nothing with it. The
    claim is never released and nothing waits on it, so a second interruption
    cannot strand an owner.
    """

    def __init__(self, *arguments, **keywords) -> None:
        super().__init__(*arguments, **keywords)
        self._start_claim = threading.Lock()

    def take_start_claim(self) -> bool:
        return self._start_claim.acquire(blocking=False)

    def run(self) -> None:
        if self.take_start_claim():
            super().run()
            return
        del self._target, self._args, self._kwargs


def run_cli_bounded(
    child: ChildProcess,
    **options: Unpack[_CliRunKeywordArgs],
) -> CliRunOutcome:
    """Run a described child with bounded input, output, and cleanup waits.

    The child deliberately receives no terminal stdin: its pipe is closed
    immediately when no input payload is supplied.
    """
    output_read_limit_bytes = options.get("output_read_limit_bytes")
    request = _CliRunRequest(
        stdin_payload=options["stdin_payload"],
        read=options["read"],
        deadline=options["deadline"],
        stderr=options.get("stderr", "capture"),
        output_read_limit_bytes=(
            CLI_OUTPUT_READ_LIMIT_BYTES
            if output_read_limit_bytes is None
            else output_read_limit_bytes
        ),
        on_spawned=options.get("on_spawned"),
        on_spawn_failed=options.get("on_spawn_failed"),
        on_reaped=options.get("on_reaped"),
        stdout_line_channel=options.get("stdout_line_channel"),
        prompt_file_bytes=options.get("prompt_file_bytes"),
        prompt_file_arg_index=options.get("prompt_file_arg_index"),
        prompt_file_prefix=options.get("prompt_file_prefix"),
    )
    state = _BoundedRunState()
    worker = ClaimedStartThread(
        target=_run_cli_bounded, args=(state, child, request), daemon=True
    )
    start_failure: BaseException | None = None
    try:
        worker.start()
    except BaseException as error:
        if not worker.take_start_claim():
            raise
        start_failure = error
    if start_failure is not None:
        return _settle_unstarted_run(state, request, start_failure)
    if state.completed.wait(timeout=max(request.deadline - time.monotonic(), 0)):
        return _recorded_outcome(state)
    if not state.started.is_set():
        return _settle_from_caller(state, request, _deadline_before_spawn_outcome())
    settle_wait = max(cli_settle_deadline(request.deadline) - time.monotonic(), 0)
    if state.completed.wait(timeout=settle_wait):
        return _recorded_outcome(state)
    return _settle_from_caller(state, request, _cleanup_overran_outcome(started=True))


def _settle_unstarted_run(
    state: _BoundedRunState,
    request: _CliRunRequest,
    start_failure: BaseException,
) -> CliRunOutcome:
    failures = StepFailures()
    failures.record("start", start_failure)
    with failures.contained("on_spawn_failed"):
        _notify_spawn_failed(request.on_spawn_failed)
    outcome = _settle(
        state,
        request.stdout_line_channel,
        _spawn_failure_outcome(start_failure),
        failures,
    )
    failures.report()
    failures.raise_control_flow()
    return outcome


def _recorded_outcome(state: _BoundedRunState) -> CliRunOutcome:
    with state.lock:
        return cast(CliRunOutcome, state.outcome)


def _settle_from_caller(
    state: _BoundedRunState,
    request: _CliRunRequest,
    outcome: CliRunOutcome,
) -> CliRunOutcome:
    failures = StepFailures()
    settled = _settle(state, request.stdout_line_channel, outcome, failures)
    failures.report()
    failures.raise_control_flow()
    return settled


def _settle(
    state: _BoundedRunState,
    channel: CliLineChannel | None,
    outcome: CliRunOutcome,
    failures: StepFailures,
) -> CliRunOutcome:
    if channel is not None:
        with failures.contained("close"):
            outcome = channel._close(outcome)
    return state.record(outcome)


def _deadline_before_spawn_outcome() -> CliRunOutcome:
    return CliRunOutcome(
        started=False,
        spawn_error=None,
        returncode=None,
        stdout="",
        stderr="",
        complete=False,
        became_zombie=False,
        reason=CliRunReason.DEADLINE_BEFORE_SPAWN,
    )


@dataclass(frozen=True)
class _CliOutput:
    stdout: bytearray
    stderr: bytearray
    complete: bool
    reason: CliRunReason
    io_error: OSError | None = None


@dataclass
class _CliExchange:
    stdout: bytearray
    stderr: bytearray
    pending_stdin: memoryview
    stdout_line_remainder: bytearray


def _run_cli_bounded(
    state: _BoundedRunState,
    child: ChildProcess,
    request: _CliRunRequest,
) -> None:
    failures = StepFailures()
    started, prompt_file_path = _start_bounded_cli(child, request, failures)
    background_reap_process: BlockingAnchoredChild | None = None
    if isinstance(started, CliRunOutcome):
        outcome = started
    else:
        state.started.set()
        output = _exchange_after_spawn(started, request, failures)
        outcome, handed_off = _reaped_outcome(started, output, request.on_reaped, failures)
        if handed_off:
            background_reap_process = started
    outcome = _outcome_after_prompt_file_cleanup(prompt_file_path, outcome, failures)
    _settle(state, request.stdout_line_channel, outcome, failures)
    failures.report()
    if background_reap_process is not None:
        _reap_in_background(background_reap_process, request.on_reaped)


def _start_bounded_cli(
    child: ChildProcess,
    request: _CliRunRequest,
    failures: StepFailures,
) -> tuple[BlockingAnchoredChild | CliRunOutcome, str | None]:
    prompt_file_path: str | None = None
    with failures.contained("spawn", reported=False):
        prompted_child, prompt_file_path = _with_private_prompt_file(
            child,
            request.prompt_file_bytes,
            request.prompt_file_arg_index,
            request.prompt_file_prefix,
        )
        return (
            open_blocking_anchored_pipes(prompted_child, stderr=request.stderr),
            prompt_file_path,
        )
    with failures.contained("on_spawn_failed"):
        _notify_spawn_failed(request.on_spawn_failed)
    spawn_failure = _spawn_failure_outcome(failures.error_raised_by("spawn"))
    return spawn_failure, prompt_file_path


def _spawn_failure_outcome(error: BaseException) -> CliRunOutcome:
    return CliRunOutcome(
        started=False,
        spawn_error=error,
        returncode=None,
        stdout="",
        stderr="",
        complete=False,
        became_zombie=False,
        reason=CliRunReason.SPAWN_FAILED,
    )


def _exchange_after_spawn(
    process: BlockingAnchoredChild,
    request: _CliRunRequest,
    failures: StepFailures,
) -> _CliOutput:
    output = _CliOutput(
        bytearray(),
        bytearray(),
        complete=False,
        reason=CliRunReason.IO_ERROR,
    )
    spawn_announced = False
    with failures.contained("on_spawned"):
        _notify_spawned(request.on_spawned, process.pid)
        spawn_announced = True
    if spawn_announced:
        with failures.contained("exchange"):
            output = _exchange_bounded(
                process,
                request.stdin_payload,
                request.read,
                request.deadline,
                request.output_read_limit_bytes,
                request.stdout_line_channel,
            )
    return output


def _reaped_outcome(
    process: BlockingAnchoredChild,
    output: _CliOutput,
    on_reaped: Callable[[int, bool], None] | None,
    failures: StepFailures,
) -> tuple[CliRunOutcome, bool]:
    became_zombie = _reap_or_hand_off(process, failures)
    if not became_zombie:
        with failures.contained("on_reaped"):
            _notify_reaped(on_reaped, process.pid, became_zombie=False)
    outcome = CliRunOutcome(
        started=True,
        spawn_error=None,
        returncode=process.returncode,
        stdout="" if output.reason in _DEADLINE_REASONS else _decode(output.stdout),
        stderr="" if output.reason in _DEADLINE_REASONS else _decode(output.stderr),
        complete=output.complete,
        became_zombie=became_zombie,
        reason=output.reason,
        io_error=output.io_error,
    )
    return outcome, became_zombie


def _reap_or_hand_off(process: BlockingAnchoredChild, failures: StepFailures) -> bool:
    """Reap the group; a reap that raises is handed to the background reap."""
    with failures.contained("reap"):
        return _reap_process_group(process)
    with failures.contained("hand-off close"):
        process.close_streams()
    return True


def _outcome_after_prompt_file_cleanup(
    prompt_file_path: str | None,
    outcome: CliRunOutcome,
    failures: StepFailures,
) -> CliRunOutcome:
    if prompt_file_path is None:
        return outcome
    prompt_file_removed = False
    with failures.contained("prompt_file"):
        _unlink_prompt_file(prompt_file_path)
        prompt_file_removed = True
    if prompt_file_removed:
        return outcome
    prompt_file_error = failures.error_raised_by("prompt_file")
    return CliRunOutcome(
        started=outcome.started,
        spawn_error=outcome.spawn_error,
        returncode=outcome.returncode,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        complete=False,
        became_zombie=outcome.became_zombie,
        reason=CliRunReason.IO_ERROR,
        io_error=prompt_file_error if isinstance(prompt_file_error, OSError) else None,
    )


def _with_private_prompt_file(
    child: ChildProcess,
    prompt_file_bytes: bytes | None,
    prompt_file_arg_index: int | None,
    prompt_file_prefix: str | None,
) -> tuple[ChildProcess, str | None]:
    if prompt_file_bytes is None:
        if prompt_file_arg_index is not None or prompt_file_prefix is not None:
            raise ValueError("A prompt file index and prefix require prompt bytes")
        return child, None
    if prompt_file_arg_index is None:
        raise ValueError("Prompt bytes require a prompt file index")
    if prompt_file_prefix is None:
        raise ValueError("Prompt bytes require a prompt file prefix")
    if prompt_file_arg_index < 0 or prompt_file_arg_index >= len(child.arguments):
        raise ValueError("Prompt file index is outside the CLI command")
    descriptor, path = tempfile.mkstemp(
        prefix=prompt_file_prefix,
        dir=current_config().cli_working_directory_root,
    )
    try:
        with os.fdopen(descriptor, "wb") as prompt_file:
            prompt_file.write(prompt_file_bytes)
        os.chmod(path, 0o600)
    except BaseException:
        _unlink_prompt_file(path)
        raise
    arguments = list(child.arguments)
    arguments[prompt_file_arg_index] = path
    return child.with_arguments(tuple(arguments)), path


def _unlink_prompt_file(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return


_DEADLINE_REASONS = frozenset(
    {
        CliRunReason.DEADLINE_WHILE_WRITING,
        CliRunReason.DEADLINE_WHILE_READING,
    }
)


def _notify_spawned(callback: Callable[[int], None] | None, process_id: int) -> None:
    if callback is not None:
        callback(process_id)


def _notify_spawn_failed(callback: Callable[[], None] | None) -> None:
    if callback is not None:
        callback()


def _notify_reaped(
    callback: Callable[[int, bool], None] | None,
    process_id: int,
    became_zombie: bool,
) -> None:
    if callback is not None:
        callback(process_id, became_zombie)


def _exchange_bounded(
    process: BlockingAnchoredChild,
    stdin_payload: bytes | None,
    read: Literal["all", "first_line"],
    deadline: float,
    output_read_limit_bytes: int,
    stdout_line_channel: CliLineChannel | None,
) -> _CliOutput:
    exchange = _CliExchange(
        stdout=bytearray(),
        stderr=bytearray(),
        pending_stdin=memoryview(stdin_payload) if stdin_payload else memoryview(b""),
        stdout_line_remainder=bytearray(),
    )
    try:
        with selectors.DefaultSelector() as selector:
            _register_exchange_streams(selector, process, exchange)
            return _exchange_until_complete(
                selector,
                exchange,
                read,
                deadline,
                output_read_limit_bytes,
                stdout_line_channel,
            )
    except OSError as error:
        return _CliOutput(
            exchange.stdout,
            exchange.stderr,
            complete=False,
            reason=CliRunReason.IO_ERROR,
            io_error=error,
        )


def _register_exchange_streams(
    selector: selectors.BaseSelector,
    process: BlockingAnchoredChild,
    exchange: _CliExchange,
) -> None:
    stdin = process.stdin
    if stdin is not None and not exchange.pending_stdin:
        _close_stdin(process)
    _register_output_streams(selector, process)
    if stdin is not None and exchange.pending_stdin:
        os.set_blocking(stdin.fileno(), False)
        selector.register(stdin, selectors.EVENT_WRITE, "stdin")


def _register_output_streams(
    selector: selectors.BaseSelector,
    process: BlockingAnchoredChild,
) -> None:
    for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        if stream is not None:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)


def _exchange_until_complete(
    selector: selectors.BaseSelector,
    exchange: _CliExchange,
    read: Literal["all", "first_line"],
    deadline: float,
    output_read_limit_bytes: int,
    stdout_line_channel: CliLineChannel | None,
) -> _CliOutput:
    while selector.get_map():
        terminal_output = _exchange_terminal_output(exchange, stdout_line_channel, deadline)
        if terminal_output is not None:
            return terminal_output
        events = selector.select(timeout=min(deadline - time.monotonic(), 0.05))
        if not events:
            if time.monotonic() >= deadline:
                return _deadline_output(exchange.stdout, exchange.stderr, exchange.pending_stdin)
            continue
        for key, _ in events:
            event_output = _exchange_event(
                selector,
                key,
                exchange,
                read,
                output_read_limit_bytes,
                stdout_line_channel,
            )
            if event_output is not None:
                return event_output
    return _CliOutput(
        exchange.stdout,
        exchange.stderr,
        complete=True,
        reason=CliRunReason.COMPLETE,
    )


def _exchange_terminal_output(
    exchange: _CliExchange,
    stdout_line_channel: CliLineChannel | None,
    deadline: float,
) -> _CliOutput | None:
    if stdout_line_channel is not None and stdout_line_channel.abort_requested():
        return _CliOutput(
            exchange.stdout,
            exchange.stderr,
            complete=False,
            reason=CliRunReason.CANCELLED,
        )
    if deadline - time.monotonic() <= 0:
        return _deadline_output(exchange.stdout, exchange.stderr, exchange.pending_stdin)
    return None


def _exchange_event(
    selector: selectors.BaseSelector,
    key: selectors.SelectorKey,
    exchange: _CliExchange,
    read: Literal["all", "first_line"],
    output_read_limit_bytes: int,
    stdout_line_channel: CliLineChannel | None,
) -> _CliOutput | None:
    if key.data == "stdin":
        return _write_pending_stdin(selector, key, exchange)
    return _read_cli_output(
        selector,
        key,
        exchange,
        read,
        output_read_limit_bytes,
        stdout_line_channel,
    )


def _write_pending_stdin(
    selector: selectors.BaseSelector,
    key: selectors.SelectorKey,
    exchange: _CliExchange,
) -> _CliOutput | None:
    written = os.write(key.fileobj.fileno(), exchange.pending_stdin)
    if written <= 0:
        return _CliOutput(
            exchange.stdout,
            exchange.stderr,
            complete=False,
            reason=CliRunReason.IO_ERROR,
        )
    exchange.pending_stdin = exchange.pending_stdin[written:]
    if exchange.pending_stdin:
        return None
    selector.unregister(key.fileobj)
    key.fileobj.close()
    return None


def _read_cli_output(
    selector: selectors.BaseSelector,
    key: selectors.SelectorKey,
    exchange: _CliExchange,
    read: Literal["all", "first_line"],
    output_read_limit_bytes: int,
    stdout_line_channel: CliLineChannel | None,
) -> _CliOutput | None:
    room = output_read_limit_bytes - len(exchange.stdout) - len(exchange.stderr)
    if room <= 0:
        return _output_limit_reached(exchange)
    chunk = os.read(key.fileobj.fileno(), room)
    if not chunk:
        return _finish_output_stream(selector, key, exchange, read, stdout_line_channel)
    if read == "first_line" and key.data == "stdout":
        first_line_output = _first_line_output(exchange.stdout, exchange.stderr, chunk)
        if first_line_output is not None:
            return first_line_output
    return _collect_output_chunk(
        key.data,
        exchange,
        chunk,
        output_read_limit_bytes,
        stdout_line_channel,
    )


def _finish_output_stream(
    selector: selectors.BaseSelector,
    key: selectors.SelectorKey,
    exchange: _CliExchange,
    read: Literal["all", "first_line"],
    stdout_line_channel: CliLineChannel | None,
) -> _CliOutput | None:
    selector.unregister(key.fileobj)
    if key.data == "stdout" and stdout_line_channel is not None:
        channel_output = _flush_stdout_line_remainder(exchange, stdout_line_channel)
        if channel_output is not None:
            return channel_output
    if read == "first_line" and key.data == "stdout":
        return _CliOutput(
            exchange.stdout,
            exchange.stderr,
            complete=True,
            reason=CliRunReason.COMPLETE,
        )
    return None


def _first_line_output(
    stdout: bytearray,
    stderr: bytearray,
    chunk: bytes,
) -> _CliOutput | None:
    newline = chunk.find(b"\n")
    if newline < 0:
        return None
    stdout.extend(chunk[: newline + 1])
    return _CliOutput(stdout, stderr, complete=True, reason=CliRunReason.COMPLETE)


def _collect_output_chunk(
    stream_name: str,
    exchange: _CliExchange,
    chunk: bytes,
    output_read_limit_bytes: int,
    stdout_line_channel: CliLineChannel | None,
) -> _CliOutput | None:
    collected = exchange.stdout if stream_name == "stdout" else exchange.stderr
    collected.extend(chunk)
    if stream_name == "stdout" and stdout_line_channel is not None:
        channel_output = _send_complete_stdout_lines(exchange, stdout_line_channel, chunk)
        if channel_output is not None:
            return channel_output
    if len(exchange.stdout) + len(exchange.stderr) >= output_read_limit_bytes:
        return _output_limit_reached(exchange)
    return None


def _send_complete_stdout_lines(
    exchange: _CliExchange,
    stdout_line_channel: CliLineChannel,
    chunk: bytes,
) -> _CliOutput | None:
    exchange.stdout_line_remainder.extend(chunk)
    while b"\n" in exchange.stdout_line_remainder:
        newline = exchange.stdout_line_remainder.index(b"\n") + 1
        line = bytes(exchange.stdout_line_remainder[:newline])
        del exchange.stdout_line_remainder[:newline]
        if not stdout_line_channel._send(line):
            return _stdout_channel_delivery_failure(exchange, stdout_line_channel)
    return None


def _flush_stdout_line_remainder(
    exchange: _CliExchange,
    stdout_line_channel: CliLineChannel,
) -> _CliOutput | None:
    if not exchange.stdout_line_remainder:
        return None
    line = bytes(exchange.stdout_line_remainder)
    exchange.stdout_line_remainder.clear()
    if stdout_line_channel._send(line):
        return None
    return _stdout_channel_delivery_failure(exchange, stdout_line_channel)


def _stdout_channel_delivery_failure(
    exchange: _CliExchange,
    stdout_line_channel: CliLineChannel,
) -> _CliOutput:
    if stdout_line_channel.abort_requested():
        reason = CliRunReason.CANCELLED
    else:
        reason = CliRunReason.OUTPUT_CHANNEL_FULL
    return _CliOutput(exchange.stdout, exchange.stderr, complete=False, reason=reason)


def _output_limit_reached(exchange: _CliExchange) -> _CliOutput:
    return _CliOutput(
        exchange.stdout,
        exchange.stderr,
        complete=False,
        reason=CliRunReason.OUTPUT_LIMIT_REACHED,
    )


def _close_stdin(process: BlockingAnchoredChild) -> None:
    if process.stdin is not None:
        process.stdin.close()


def _deadline_output(
    stdout: bytearray,
    stderr: bytearray,
    pending_stdin: memoryview,
) -> _CliOutput:
    reason = (
        CliRunReason.DEADLINE_WHILE_WRITING
        if pending_stdin
        else CliRunReason.DEADLINE_WHILE_READING
    )
    return _CliOutput(stdout, stderr, complete=False, reason=reason)


def _catalog_cli_output(
    binary_name: str,
    args: tuple[str, ...],
    *,
    credential_home_variable: str,
    auth_file: Path,
    missing_binary_detail: str,
    credential_copy: CredentialCopy | None = None,
) -> str | None:
    binary = require_cli_binary(binary_name, missing_binary_detail)
    run = _run_credentialed_catalog_cli(
        binary,
        args,
        credential_home_variable=credential_home_variable,
        auth_file=auth_file,
        credential_copy=credential_copy,
    )
    if run is None or not run.complete or run.returncode != 0:
        return None
    # Grok and Codex place status diagnostics on either stream across releases;
    # their line contracts must not become dependent on that presentation choice.
    return run.stdout + run.stderr


def _run_credentialed_catalog_cli(
    binary: Path,
    args: tuple[str, ...],
    *,
    credential_home_variable: str,
    auth_file: Path,
    credential_copy: CredentialCopy | None = None,
    stderr: StderrPolicy = "capture",
    output_read_limit_bytes: int | None = None,
) -> CliRun | None:
    with _catalog_credential_home(
        auth_file, _CATALOG_AUTH_FILE_PATH, credential_copy=credential_copy,
    ) as owner:
        # Grok resolves the invoking account's profile when HOME is absent,
        # even with GROK_HOME set (measured on grok 1.0.4). HOME is containment.
        environment = closed_environment(
            owner.path, **{credential_home_variable: str(owner.path)},
        )
        return run_catalog_cli(
            ChildProcess(
                binary=binary,
                arguments=args,
                environment=environment,
                working_directory=owner.path,
            ),
            home=owner,
            stderr=stderr,
            output_read_limit_bytes=output_read_limit_bytes,
        )


def _claude_output(binary: Path) -> str | None:
    try:
        run = _run_without_credential_remains(
            lambda: run_claude_catalog_cli(
                binary, CLAUDE_CLI_STATUS_ARGS, login=claude_token_environment(),
            ),
        )
    except CliLoginUnusableError as refusal:
        log.warning(
            "the configured Claude token file is refused (%s); "
            "this probe reports a logged-out provider",
            str(refusal),
        )
        return None
    if run is None or not run.complete or run.returncode != 0:
        return None
    return run.stdout


class PrivateHome:
    """One disposable CLI home whose cleanup follows its child processes.

    Removal never decides a call's outcome. A home that cannot be removed is
    logged by error class alone and stays owned: its finalizer retries when the
    home is collected or the process exits.

    Its bookkeeping waits on no lock a signal could leave held: state changes
    are attribute stores and single operations on identity-hashed containers.
    """

    def __init__(self, *, prefix: str) -> None:
        registry_key = object()
        created: str | None = None
        try:
            try:
                created = tempfile.mkdtemp(
                    prefix=prefix, dir=current_config().cli_working_directory_root,
                )
            except OSError:
                raise_without_chain(
                    CliEnvironmentUnavailableError(
                        "could not create a private CLI home",
                        cause=CliEnvironmentCause.HOME_NOT_CREATED,
                    )
                )
            self._finalizer = _live_home_finalizers[registry_key] = weakref.finalize(
                self, _remove_abandoned_tree, Path(created), os.getpid(), registry_key,
            )
            self._registry_key = registry_key
            self._home = Path(created)
            self._reservation_tokens: set[PrivateHomeReservation] = set()
            self._cleanup_requested = False
            self._cleaned = False
            self._removal_claim = threading.Lock()
            try:
                os.chmod(self._home, 0o700)
            except OSError:
                raise_without_chain(
                    CliEnvironmentUnavailableError(
                        _PRIVATE_HOME_NOT_PREPARED_DETAIL,
                        cause=CliEnvironmentCause.HOME_NOT_CREATED,
                    )
                )
        except BaseException:  # NOSONAR(S5754) removes the tree, re-raises
            _discard_failed_home(created, registry_key)
            raise

    @property
    def path(self) -> Path:
        return self._home

    def reserve(self) -> PrivateHomeReservation:
        token = PrivateHomeReservation(self)
        self._reservation_tokens.add(token)
        if self._cleanup_requested:
            self._reservation_tokens.discard(token)
            self._remove_if_unused()
            raise RuntimeError("private CLI home is closing")
        return token

    def close(self) -> None:
        """Remove the home once no child holds it; never raises for a failed removal."""
        self._cleanup_requested = True
        self._remove_if_unused()

    def _release(self, token: PrivateHomeReservation) -> None:
        self._reservation_tokens.discard(token)
        self._remove_if_unused()

    def _remove_if_unused(self) -> None:
        if not self._cleanup_requested or self._reservation_tokens:
            return
        if not self._removal_claim.acquire(blocking=False):
            return
        try:
            if not self._cleaned and _remove_private_tree(self._home):
                _live_home_finalizers.pop(self._registry_key, None)
                self._finalizer.detach()
                self._cleaned = True
        finally:
            self._removal_claim.release()


class PrivateCredentialHome(PrivateHome):
    def __init__(
        self,
        auth_file: Path,
        relative_destination: Path,
        *,
        prefix: str,
        missing_credential_is_error: bool,
        credential_copy: CredentialCopy | None = None,
        mode: int = _PRIVATE_HOME_COPY_MODE,
    ) -> None:
        super().__init__(prefix=prefix)
        try:
            install_credential_file(
                auth_file,
                self._home / relative_destination,
                mode=mode,
                credential_copy=credential_copy,
                missing_credential_is_error=missing_credential_is_error,
            )
        except BaseException:  # NOSONAR(S5754) removes the tree, re-raises
            _discard_failed_home(str(self._home), self._registry_key)
            raise


class PrivateHomeReservation:
    def __init__(self, home: PrivateHome) -> None:
        self._home = home

    def release(self) -> None:
        self._home._release(self)


class LastHolderRemoval:
    """One removal, made once by the last of a closure and its reservations.

    The closure, the outstanding reservations and the claim share one lock; the
    removal runs after it is released. The claim is never revoked, so a removal
    that raised is not retried, and nothing here finalizes, sweeps at exit or
    retries: an unclosed instance never removes.
    """

    def __init__(self, remove: Callable[[], None]) -> None:
        self._remove = remove
        self._lock = threading.Lock()
        self._closed = False
        self._reservations: set[HolderReservation] = set()
        self._removal_claimed = False

    def reserve(self) -> HolderReservation:
        with self._lock:
            if self._closed:
                raise RuntimeError("reservations are closed")
            reservation = HolderReservation(self)
            self._reservations.add(reservation)
            return reservation

    def close(self) -> None:
        with self._lock:
            self._closed = True
            claimed = self._claim_when_last()
        if claimed:
            self._remove()

    def _release(self, reservation: HolderReservation) -> None:
        with self._lock:
            self._reservations.discard(reservation)
            claimed = self._claim_when_last()
        if claimed:
            self._remove()

    def _claim_when_last(self) -> bool:
        if self._closed and not self._reservations and not self._removal_claimed:
            self._removal_claimed = True
            return True
        return False


class HolderReservation:
    def __init__(self, removal: LastHolderRemoval) -> None:
        self._removal = removal

    def release(self) -> None:
        self._removal._release(self)


class RunTreeRemovalRefusal(StrEnum):
    """Why a run's tree was left in place without an attempt to remove it."""

    MOUNT_POINT = "mount_point"
    MOUNT_TABLE_UNREADABLE = "mount_table_unreadable"


class RunDirectories:
    """One run's home and working directory; built only by :func:`run_directories`."""

    def __init__(self, tree: Path, removal: LastHolderRemoval) -> None:
        self._home = tree / RUN_HOME_DIRECTORY_NAME
        self._working_directory = tree / RUN_WORK_DIRECTORY_NAME
        self._removal = removal

    @property
    def home(self) -> Path:
        return self._home

    @property
    def working_directory(self) -> Path:
        return self._working_directory

    def reserve(self) -> HolderReservation:
        return self._removal.reserve()


@contextmanager
def run_directories(
    root: PrivateWorkingRoot, files: Mapping[str, bytes],
) -> Iterator[RunDirectories]:
    """One run's tree below ``root``, removed once by the last of this context and its reservations.

    A removal that fails or is refused is logged once and never retried. Setup
    makes its one removal attempt on any exception; an ``OSError`` becomes the
    closed environment error and every other exception propagates unchanged.
    """
    for name in files:
        if not _is_single_path_component(name):
            raise_without_chain(ChildProcessSpecificationError(_RUN_FILE_NAME_DETAIL))
    try:
        directories = _create_run_directories(root, files)
    except OSError:
        raise_without_chain(
            CliEnvironmentUnavailableError(
                "could not create a private CLI home",
                cause=CliEnvironmentCause.HOME_NOT_CREATED,
            )
        )
    try:
        yield directories
    finally:
        directories._removal.close()


def _is_single_path_component(name: str) -> bool:
    return name not in ("", ".", "..") and "/" not in name and "\0" not in name


def _create_run_directories(root: PrivateWorkingRoot, files: Mapping[str, bytes]) -> RunDirectories:
    with ExitStack() as rollback:
        tree = Path(tempfile.mkdtemp(prefix=RUN_TREE_PREFIX, dir=root.path))
        removal = LastHolderRemoval(functools.partial(_remove_run_tree, tree))
        rollback.callback(removal.close)
        os.chmod(tree, _RUN_DIRECTORY_MODE)
        directories = RunDirectories(tree, removal)
        for directory in (directories.home, directories.working_directory):
            directory.mkdir(mode=_RUN_DIRECTORY_MODE)
            os.chmod(directory, _RUN_DIRECTORY_MODE)
        for name, content in files.items():
            _write_new_private_file(directories.home / name, content, RUN_FILE_MODE)
        rollback.pop_all()
    return directories


def _remove_run_tree(tree: Path) -> None:
    refusal = _run_tree_removal_refusal(Path(os.path.realpath(tree.parent)) / tree.name)
    if refusal is None:
        _remove_private_tree(tree)
    else:
        log.error("run directory removal refused (%s)", refusal.value)


def _run_tree_removal_refusal(location: Path) -> RunTreeRemovalRefusal | None:
    try:
        mountinfo = procfs.RealProcView().mountinfo()
    except OSError:
        return RunTreeRemovalRefusal.MOUNT_TABLE_UNREADABLE
    if procfs.holds_mount_point(mountinfo, str(location)):
        return RunTreeRemovalRefusal.MOUNT_POINT
    return None


def _discard_failed_home(created: str | None, registry_key: object) -> None:
    if created is not None and _remove_private_tree(Path(created)):
        finalizer = _live_home_finalizers.pop(registry_key, None)
        if finalizer is not None:
            finalizer.detach()


def _remove_abandoned_tree(root: Path, creator_process_id: int, registry_key: object) -> None:
    _live_home_finalizers.pop(registry_key, None)
    if os.getpid() != creator_process_id:
        return
    if not _remove_private_tree(root):
        _homes_awaiting_exit_sweep.add((root, creator_process_id))


def _sweep_failed_homes_at_exit() -> None:
    for finalizer in _live_home_finalizers.copy().values():
        finalizer()
    for root, creator_process_id in tuple(_homes_awaiting_exit_sweep):
        if os.getpid() == creator_process_id:
            _remove_private_tree(root)


# The sweep finalizes the still-live homes itself, so it holds whichever of its
# and weakref's exit hooks atexit runs first; a finalizer runs at most once.
_live_home_finalizers: dict[object, weakref.finalize] = {}
_homes_awaiting_exit_sweep: set[tuple[Path, int]] = set()
atexit.register(_sweep_failed_homes_at_exit)


def _remove_private_tree(root: Path) -> bool:
    """Remove one private home; a failure is logged by its class alone, never its path."""
    last_error: OSError = FileExistsError()
    for make_removable in (False, True):
        try:
            if make_removable:
                _make_tree_removable(root)
            shutil.rmtree(root)
        except OSError as error:
            last_error = error
        if not os.path.lexists(root):
            return True
    log.error("private credential home removal failed (%s)", type(last_error).__name__)
    return False


def _make_tree_removable(root: Path) -> None:
    """Reopen directories a child made read-only so their entries can be unlinked.

    Every directory is acquired without following a link and repaired on the
    acquired object. This needs Linux's O_PATH and a mounted /proc/self/fd.
    Each level on the current path keeps one open listing descriptor and the
    names of its subdirectories, so the descriptors in use grow with the
    tree's depth and not its width. Each level's frame is on the stack before
    its listing descriptor is opened, so the stack's cleanup closes it from the
    statement that receives it. Every acquired descriptor is passed to
    os.close, and a close that raises does not stop the cleanup. The earliest
    exception propagates, whether it came from the traversal or from a cleanup
    close; every later close failure is attached to it as a note naming the
    failure's class only. An exception raised instead of the real close is not
    covered, because Linux releases the descriptor even when close fails. An
    asynchronous interruption between two statements is the host obligation
    the Interruptions paragraph of docs/host.md states, and no claim is made about it.
    """
    acquisition_flags = _directory_flags("O_PATH", "O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC")
    listing_flags = _directory_flags("O_DIRECTORY", "O_CLOEXEC") | os.O_RDONLY
    frames: list[_DirectoryFrame] = []
    try:
        _enter_directory(frames, str(root), acquisition_flags, listing_flags)
        while frames:
            if frames[-1].unvisited_subdirectories:
                _enter_directory(
                    frames, frames[-1].unvisited_subdirectories.pop(),
                    acquisition_flags, listing_flags,
                )
            else:
                frames.pop().close()
    except BaseException as failure:
        _close_all((frame.listing for frame in frames if frame.listing is not None), failure)
        raise


class _EarliestFailure:
    """Absorb failures inside its blocks: the earliest is kept, later ones become its notes."""

    def __init__(self, earlier: BaseException | None) -> None:
        self.failure = earlier

    def __enter__(self) -> "_EarliestFailure":
        return self

    def __exit__(self, kind: object, failure: BaseException | None, traceback: object) -> bool:
        if failure is None:
            return False
        if self.failure is None:
            self.failure = failure
        else:
            BaseException.add_note(
                self.failure, f"a descriptor close also failed ({type(failure).__name__})"
            )
        return True


def _close_all(descriptors: Iterable[int], earlier_failure: BaseException | None = None) -> None:
    """Close every descriptor, so none stays open whatever a close raises.

    With an earlier failure the caller re-raises it and later failures become
    its notes; without one, the first close failure is raised after the last
    descriptor is closed.
    """
    absorbed = _EarliestFailure(earlier_failure)
    for descriptor in descriptors:
        with absorbed:
            os.close(descriptor)
    if earlier_failure is None and absorbed.failure is not None:
        raise absorbed.failure


def _directory_flags(*names: str) -> int:
    missing = [name for name in names if not hasattr(os, name)]
    if missing:
        raise OSError(errno.ENOTSUP, f"the permission repair needs {', '.join(missing)}")
    return functools.reduce(operator.or_, (getattr(os, name) for name in names))


@dataclass
class _DirectoryFrame:
    listing: int | None = None
    unvisited_subdirectories: list[str] = field(default_factory=list)

    def close(self) -> None:
        if self.listing is not None:
            os.close(self.listing)


def _enter_directory(
    frames: list[_DirectoryFrame], name: str, acquisition_flags: int, listing_flags: int,
) -> None:
    """Push and fill the directory frame; skip a child that vanished or is no directory."""
    parent_listing = frames[-1].listing if frames else None
    try:
        handle = os.open(name, acquisition_flags, dir_fd=parent_listing)
    except (NotADirectoryError, FileNotFoundError):
        if not frames:
            raise
        return
    try:
        frame = _DirectoryFrame()
        frames.append(frame)
        link = _DESCRIPTOR_LINKS / str(handle)
        os.chmod(link, 0o700)
        frame.listing = os.open(link, listing_flags)
        frame.unvisited_subdirectories = _subdirectory_names(frame.listing)
    except BaseException as failure:
        _close_all([handle], failure)
        raise
    _close_all([handle])


def _subdirectory_names(listing: int) -> list[str]:
    with os.scandir(listing) as entries:
        return [entry.name for entry in entries if entry.is_dir(follow_symlinks=False)]


@contextmanager
def _catalog_credential_home(
    auth_file: Path,
    relative_destination: Path,
    *,
    credential_copy: CredentialCopy | None = None,
) -> Iterator[PrivateCredentialHome]:
    owner = PrivateCredentialHome(
        auth_file,
        relative_destination,
        prefix=_CATALOG_HOME_PREFIX,
        missing_credential_is_error=False,
        credential_copy=credential_copy,
    )
    try:
        yield owner
    finally:
        owner.close()


def claude_token_environment() -> dict[str, str]:
    """The environment that hands a Claude child its login: the setup token, read now.

    The token comes from ``claude.token_file`` through ``secret_file`` on every call,
    so a replaced file reaches the next child. A refused file raises
    ``CliLoginUnusableError`` in ``secret_file``'s words, never with the file's content.
    """
    return _run_without_credential_remains(_read_claude_token_environment)


def _read_claude_token_environment() -> dict[str, str]:
    try:
        token = read_token_file(current_claude_runtime().token_file)
    except UnsoundSecretFile as refusal:
        raise_without_chain(CliLoginUnusableError(str(refusal)))
    return {CLAUDE_OAUTH_TOKEN_VARIABLE: token}


def install_credential_file(
    source: Path,
    destination: Path,
    *,
    mode: int,
    credential_copy: CredentialCopy | None,
    missing_credential_is_error: bool,
) -> None:
    """Write the one private copy of a credential file; the only writer of one.

    The source is read through the hardened reader and the copy is created
    exclusively, so an existing name of any kind is refused. Every failure
    leaves with no chain and no credential byte in a frame or link.
    """
    _run_without_credential_remains(
        functools.partial(
            _install_credential,
            source,
            destination,
            mode=mode,
            credential_copy=credential_copy,
            missing_credential_is_error=missing_credential_is_error,
        ),
    )


def _install_credential(
    source: Path,
    destination: Path,
    *,
    mode: int,
    credential_copy: CredentialCopy | None,
    missing_credential_is_error: bool,
) -> None:
    payload = _read_credential_file(source)
    if payload is None:
        if missing_credential_is_error:
            raise_without_chain(
                CliLoginUnusableError("configured CLI credential is missing")
            )
        # A probe with no credential answers "logged out", which is
        # indistinguishable from a typo in the configured path. Name the path
        # so a mis-wired deployment is readable; never the file's contents.
        log.warning(
            "no agent CLI credential at the configured path %s; "
            "this probe will report a logged-out provider",
            source,
        )
        return
    if credential_copy is not None:
        payload = credential_copy(payload)
        if payload is None:
            if missing_credential_is_error:
                raise_without_chain(
                    CliLoginUnusableError("configured CLI credential is unusable")
                )
            log.warning(
                "the credential at the configured path %s is not a usable login; "
                "this probe will report a logged-out provider",
                source,
            )
            return
    try:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_new_private_file(destination, payload, mode)
    except OSError:
        raise_without_chain(
            CliEnvironmentUnavailableError(
                _CREDENTIAL_COPY_FAILED_DETAIL,
                cause=CliEnvironmentCause.CREDENTIAL_COPY_NOT_WRITTEN,
            )
        )


def _write_new_private_file(destination: Path, payload: bytes, mode: int) -> None:
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        _write_whole(descriptor, payload)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class _ExceptionLinks:
    """What an exception points at: its traceback and its three chain attributes."""

    exception: BaseException
    traceback: TracebackType | None
    context: BaseException | None
    cause: BaseException | None
    suppress_context: bool

    @classmethod
    def of(cls, exception: BaseException) -> "_ExceptionLinks":
        return cls(
            exception,
            exception.__traceback__,
            exception.__context__,
            exception.__cause__,
            exception.__suppress_context__,
        )

    def restore(self) -> None:
        restored = self.exception
        restored.__traceback__ = self.traceback
        restored.__context__ = self.context
        restored.__cause__ = self.cause  # NOSONAR(S5707) restores the snapshot of a real __cause__
        restored.__suppress_context__ = self.suppress_context


def _run_without_credential_remains[T](step: Callable[[], T]) -> T:
    """Run a step that touches credential bytes; no exception leaves it holding them.

    A traceback keeps every frame it passed through alive, locals included, and
    so does every exception chained beneath it: the decoder's error holds the
    document itself. This frame holds no bytes, so it clears the frames of the
    failure, cuts the links the step created off, and lets the same exception
    continue as if raised here, still chained to what the caller was handling.
    A refusal that deliberately carries no context keeps none. The caller's own
    exception is restored to exactly what it was at entry on every outcome, so
    the step can neither leave links in it nor clear the frames it already had.
    """
    handled_by_the_caller = sys.exception()
    callers_links = (
        _ExceptionLinks.of(handled_by_the_caller) if handled_by_the_caller is not None else None
    )
    callers_traceback = _traceback_entries_of(handled_by_the_caller)
    try:
        return step()
    except BaseException as failure:
        _clear_frames_and_cut_links(
            failure, failure.__traceback__.tb_next, callers_traceback, handled_by_the_caller,
        )
        raise
    finally:
        if callers_links is not None:
            callers_links.restore()


def _traceback_entries_of(exception: BaseException | None) -> list[TracebackType]:
    entries = []
    entry = exception.__traceback__ if exception is not None else None
    while entry is not None:
        entries.append(entry)
        entry = entry.tb_next
    return entries


def _clear_frames_and_cut_links(
    failure: BaseException,
    first_entry: TracebackType | None,
    callers_traceback: list[TracebackType],
    handled_by_the_caller: BaseException | None,
) -> None:
    """Clear the failure's finished frames and cut its inner links, group members included."""
    entry = first_entry
    while entry is not None and not any(entry is kept for kept in callers_traceback):
        try:
            entry.tb_frame.clear()
        except RuntimeError:
            break  # a frame still executing belongs to the caller; below it is the earlier raise
        entry = entry.tb_next
    failure.__cause__ = None
    if failure.__context__ is not None:
        failure.__context__ = handled_by_the_caller
    if isinstance(failure, BaseExceptionGroup):
        for member in failure.exceptions:
            _clear_frames_and_cut_links(
                member, member.__traceback__, callers_traceback, handled_by_the_caller,
            )


def _read_credential_file(path: Path) -> bytes | None:
    """Read a credential source: ``None`` when absent, an error when not a plain file.

    The open never follows a link and never waits on a FIFO; the kind is judged
    on the opened descriptor, so nothing can swap the file in between. No buffer
    is held in this frame while a refusal is raised.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        refusal = (
            _CREDENTIAL_SYMLINK_DETAIL
            if exc.errno == errno.ELOOP
            else _CREDENTIAL_UNREADABLE_DETAIL
        )
        raise_without_chain(CliLoginUnusableError(refusal))
    try:
        content, refusal = _read_regular_file(descriptor)
    finally:
        closed = _close_descriptor(descriptor)
    if not closed:
        content, refusal = None, _CREDENTIAL_UNREADABLE_DETAIL
    if refusal is not None:
        raise_without_chain(CliLoginUnusableError(refusal))
    return content


def _close_descriptor(descriptor: int) -> bool:
    try:
        os.close(descriptor)
    except OSError:
        return False
    return True


def _read_regular_file(descriptor: int) -> tuple[bytes | None, str | None]:
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None, _CREDENTIAL_NOT_REGULAR_DETAIL
        content = _read_within_bound(descriptor)
    except OSError:
        return None, _CREDENTIAL_UNREADABLE_DETAIL
    if content is None:
        return None, _CREDENTIAL_OVERSIZE_DETAIL
    return content, None


def _read_within_bound(descriptor: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(
            descriptor, min(65_536, _CREDENTIAL_FILE_READ_LIMIT_BYTES + 1 - size),
        )
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > _CREDENTIAL_FILE_READ_LIMIT_BYTES:
            return None
        chunks.append(chunk)


def _write_whole(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written < 1:
            raise OSError("private file write made no progress")
        view = view[written:]


def _decode(collected: bytearray) -> str:
    return collected.decode(errors="replace")


@dataclass(frozen=True)
class _GroupConfirmation:
    census: procfs.GroupCensus
    ended_in_time: bool
    exit_published: bool
    released: bool

    @property
    def diagnostic(self) -> str:
        if self.census is not procfs.GroupCensus.EMPTY:
            return self.census.value
        if not self.ended_in_time:
            return "empty, counted too late"
        if not self.exit_published:
            return "empty, exit not published"
        return "empty, exit published, leader not released"


def _confirm_and_release(process: BlockingAnchoredChild, *, within: float) -> _GroupConfirmation:
    # lost_groups imports ClaimedStartThread from this module, so only a call-time import fits
    from agent_runtime.lost_groups import release_leader_if_gone

    result = process.kill_until_gone(within=within)
    exit_published = result.confirmed and process.wait_for_exit(within)
    released = release_leader_if_gone(
        process, counted_empty=result.confirmed, exit_known=exit_published
    )
    return _GroupConfirmation(
        result.census,
        result.ended_in_time,
        exit_published=exit_published,
        released=released,
    )


def _reap_process_group(process: BlockingAnchoredChild) -> bool:
    process.close_streams()
    if process.returncode is None:
        process.signal_group(signal.SIGTERM)
        process.wait_for_exit(CLI_TERMINATION_GRACE_SECONDS)
    confirmation = _confirm_and_release(process, within=CLI_GROUP_CONFIRMATION_SECONDS)
    if confirmation.released:
        return False
    log.warning(
        "agent CLI process group %s was not confirmed gone (%s); "
        "its reap continues in the background",
        process.pid,
        confirmation.diagnostic,
    )
    return True


def _reap_in_background(
    process: BlockingAnchoredChild,
    on_reaped: Callable[[int, bool], None] | None,
) -> None:
    failures = StepFailures()
    released = False
    with failures.contained("background reap of agent CLI process group"):
        released = _confirm_and_release(process, within=_BACKGROUND_REAP_LIMIT_SECONDS).released
    if released:
        with failures.contained("on_reaped"):
            _notify_reaped(on_reaped, process.pid, became_zombie=True)
    else:
        _groups_held_until_exit.append(_HeldGroup(process, on_reaped))
        with failures.contained("report", reported=False):
            log.error("agent CLI process group %s outlived its background reap", process.pid)
    failures.report()


def _probe_claude_login(binary: Path) -> CliLogin:
    output = _claude_output(binary)
    if output is None:
        return LOGGED_OUT
    # Claude offers structured status, so accepting a near-match would turn a
    # changed authentication contract into a false logged-in report.
    try:
        payload: Any = json.loads(output)
    except ValueError:
        return LOGGED_OUT
    if not isinstance(payload, dict):
        return LOGGED_OUT
    logged_in = payload.get(CLAUDE_CLI_LOGGED_IN_FIELD)
    if not isinstance(logged_in, bool):
        return LOGGED_OUT
    auth_method = payload.get(CLAUDE_CLI_AUTH_METHOD_FIELD)
    return CliLogin(
        logged_in=logged_in,
        auth_method=auth_method if isinstance(auth_method, str) else None,
    )


def _probe_grok_status() -> GrokCliStatus:
    grok = current_grok_runtime()
    output = _catalog_cli_output(
        grok.binary,
        GROK_CLI_STATUS_ARGS,
        credential_home_variable=_GROK_CREDENTIAL_HOME_VARIABLE,
        auth_file=grok.token_file,
        credential_copy=grok_login.blanked_copy,
        missing_binary_detail=GROK_CLI_BINARY_MISSING_DETAIL,
    )
    if output is None:
        return GrokCliStatus(login=LOGGED_OUT, model_names=())
    login = _parse_grok_login(output)
    model_names = _parse_grok_model_names(output) if login.logged_in else ()
    return GrokCliStatus(login=login, model_names=model_names)


def grok_cli_token_is_present() -> bool:
    """Whether the Grok CLI credential mirror contains a non-empty access token."""
    try:
        document = json.loads(current_grok_runtime().token_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CliLoginUnusableError("could not read Grok CLI credentials") from exc
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise_without_chain(CliLoginUnusableError(GROK_CLI_CREDENTIALS_INVALID_DETAIL))
    if not isinstance(document, dict):
        raise CliLoginUnusableError(GROK_CLI_CREDENTIALS_INVALID_DETAIL)
    for realm in document.values():
        if not isinstance(realm, dict):
            raise CliLoginUnusableError(GROK_CLI_CREDENTIALS_INVALID_DETAIL)
        key = realm.get("key")
        if key is None:
            continue
        if not isinstance(key, str):
            raise CliLoginUnusableError(GROK_CLI_CREDENTIALS_INVALID_DETAIL)
        if key:
            return True
    return False


def codex_cli_login_is_usable() -> bool:
    """Whether the Codex CLI credential mirror holds a login that can start a child."""
    return _run_without_credential_remains(_codex_login_in_the_mirror_is_usable)


def _codex_login_in_the_mirror_is_usable() -> bool:
    document = _read_credential_file(current_codex_runtime().token_file)
    if document is None:
        return False
    verdict = assess_codex_login(document).verdict
    if verdict is CodexLoginVerdict.MALFORMED:
        raise_without_chain(CliLoginUnusableError(CODEX_CLI_CREDENTIALS_INVALID_DETAIL))
    return verdict is CodexLoginVerdict.USABLE


def _parse_grok_login(output: str) -> CliLogin:
    for line in output.splitlines():
        stripped = line.strip()
        # These exact markers reject prose changes rather than guessing a
        # subscription state from incidental account text.
        if stripped.startswith(GROK_CLI_LOGGED_IN_MARKER):
            account = stripped.removeprefix(GROK_CLI_LOGGED_IN_MARKER).rstrip(".")
            return CliLogin(logged_in=True, auth_method=account or None)
        if stripped == GROK_CLI_LOGGED_OUT_MARKER:
            return LOGGED_OUT
    raise CliLoginUnusableError("grok models did not report its login status")


def _parse_grok_model_names(output: str) -> tuple[str, ...]:
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != GROK_CLI_MODEL_LIST_MARKER:
            continue
        names = tuple(_grok_model_names_under(lines[index + 1 :]))
        if names:
            return names
        break
    raise AgentCliUnavailableError("grok models did not list a model name")


def _grok_model_names_under(lines: list[str]) -> list[str]:
    names: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or not stripped.startswith(GROK_CLI_MODEL_BULLETS):
            break
        name = stripped.split(maxsplit=1)[1].split(maxsplit=1)[0]
        names.append(name)
    return names


def _probe_codex_login() -> CliLogin:
    output = _catalog_cli_output(
        current_codex_runtime().binary,
        CODEX_CLI_STATUS_ARGS,
        credential_home_variable=_CODEX_CREDENTIAL_HOME_VARIABLE,
        auth_file=current_codex_runtime().token_file,
        missing_binary_detail=CODEX_CLI_BINARY_MISSING_DETAIL,
        credential_copy=blanked_copy,
    )
    if output is None:
        return LOGGED_OUT
    return _parse_codex_login(output)


def codex_cli_model_catalog() -> str:
    """Return the current JSON catalog emitted by ``codex debug models``."""
    binary = require_cli_binary(
        current_codex_runtime().binary, CODEX_CLI_BINARY_MISSING_DETAIL,
    )
    run = _run_credentialed_catalog_cli(
        binary,
        CODEX_CLI_MODELS_ARGS,
        credential_home_variable=_CODEX_CREDENTIAL_HOME_VARIABLE,
        auth_file=current_codex_runtime().token_file,
        credential_copy=blanked_copy,
        stderr="devnull",
        output_read_limit_bytes=CODEX_CLI_MODEL_CATALOG_OUTPUT_READ_LIMIT_BYTES,
    )
    if run is None or not run.complete or run.returncode != 0:
        raise AgentCliUnavailableError("codex debug models did not return a catalog")
    return run.stdout


def _parse_codex_login(output: str) -> CliLogin:
    for line in output.splitlines():
        stripped = line.strip()
        # Codex has no structured status output; its documented markers are
        # deliberately narrower than a heuristic that could forge a login.
        if stripped.startswith(CODEX_CLI_LOGGED_IN_MARKER):
            account = stripped.removeprefix(CODEX_CLI_LOGGED_IN_MARKER)
            return CliLogin(logged_in=True, auth_method=account or None)
        if stripped == CODEX_CLI_LOGGED_OUT_MARKER:
            return LOGGED_OUT
    raise CliLoginUnusableError("codex login status did not report its login status")


_grok_status_probe = CachedProbe(_probe_grok_status)
_codex_login_probe = CachedProbe(_probe_codex_login)
_claude_login_probes: dict[str, CachedProbe[CliLogin]] = {}
_claude_login_probes_lock = threading.Lock()


def claude_cli_login(binary: Path | None) -> CliLogin:
    if binary is None:
        raise CliEnvironmentUnavailableError(
            CLAUDE_CLI_BINARY_MISSING_DETAIL, cause=CliEnvironmentCause.BINARY_MISSING,
        )
    with _claude_login_probes_lock:
        probe = _claude_login_probes.setdefault(
            str(binary),
            CachedProbe(lambda: _probe_claude_login(binary)),
        )
    try:
        return probe.get()
    except CliProbeBudgetExceeded:
        return LOGGED_OUT


def grok_cli_status() -> GrokCliStatus:
    try:
        return _grok_status_probe.get()
    except CliProbeBudgetExceeded:
        return GrokCliStatus(login=LOGGED_OUT, model_names=())


def codex_cli_login() -> CliLogin:
    try:
        return _codex_login_probe.get()
    except CliProbeBudgetExceeded:
        return LOGGED_OUT


def clear_claude_cli_login_cache() -> None:
    with _claude_login_probes_lock:
        _claude_login_probes.clear()


def clear_agent_cli_caches() -> None:
    clear_claude_cli_login_cache()
    _grok_status_probe.clear()
    _codex_login_probe.clear()
