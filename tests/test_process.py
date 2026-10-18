"""Tests for bounded probes of mounted agent CLIs."""

from __future__ import annotations

import concurrent.futures
import contextlib
import errno
import gc
import json
import linecache
import logging
import os
import pickle
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from unittest.mock import patch

import pytest
from provider_test_support import (
    A_RUNNER_FAILURE_CANARY,
    ANOTHER_RUNNER_FAILURE_CANARY,
    CREDENTIAL_FILE_BOUND_BYTES,
    DESTINATION_FAULTS,
    GROK_LOGGED_IN,
    SECRET_ENV_KEYS,
    FailingLogHandler,
    RaisingLogHandler,
    RunnerFailureWatch,
    a_codex_login,
    a_credential_destination_that_fails,
    a_dangling_symlink,
    a_directory,
    a_fifo,
    a_path_through_a_regular_file,
    a_symlink_to_a_login,
    a_usable_login_over_the_bound,
    call_within_deadline,
    exception_chain,
    fail_mkdtemp,
    fail_the_directory_named,
    fail_the_home_chmod,
    fail_the_work_directory,
    fail_to_start_the_runner_thread,
    order_the_thread_start_claims,
    override_cli_binary,
    override_login_source,
    override_provider_runtime,
    pool_callbacks,
    shell_child,
    start_then_raise,
    traceback_frames_hold,
    tree_snapshot,
)

from agent_providers import process
from agent_providers.codex.pool import CodexProcessKind, CodexProcessPool
from agent_providers.codex.protocol import codex_child_process, create_codex_turn_home
from agent_providers.codex_login import _blanked_document, assess_codex_login, blanked_copy
from agent_providers.config import current_config
from agent_providers.constants import (
    CLAUDE_CLI_AUTH_METHOD_FIELD,
    CLAUDE_CLI_LOGGED_IN_FIELD,
    CLI_OUTPUT_READ_LIMIT_BYTES,
)
from agent_providers.process import (
    CLAUDE_CLI_BINARY_MISSING_DETAIL,
    CODEX_CLI_BINARY_MISSING_DETAIL,
    CODEX_CLI_CREDENTIALS_INVALID_DETAIL,
    GROK_CLI_BINARY_MISSING_DETAIL,
    GROK_CLI_CREDENTIALS_INVALID_DETAIL,
    AgentCliUnavailableError,
    CachedProbe,
    ClaimedStartThread,
    CliEnvironmentCause,
    CliEnvironmentUnavailableError,
    CliLineChannel,
    CliLogin,
    CliLoginUnusableError,
    CliProbeBudgetExceeded,
    CliRun,
    CliRunOutcome,
    CliRunReason,
    PrivateCredentialHome,
    _run_credentialed_catalog_cli,
    claude_cli_login,
    clear_agent_cli_caches,
    cli_settle_deadline,
    codex_cli_login,
    codex_cli_model_catalog,
    grok_cli_status,
    resolve_cli_binary,
    run_catalog_cli,
    run_claude_catalog_cli,
    run_cli_bounded,
)
from agent_providers.sandbox.paths import CODEX_TOOL_TURN_DIRECTORY_PREFIX
from agent_providers.spawn import INHERITED_MACHINE_VARIABLES, ChildProcess

GROK_LOGGED_OUT = """You are not authenticated.

Available models:
  * grok-4.6 (default)
"""
CODEX_LOGGED_IN = "Logged in using ChatGPT"
CODEX_LOGGED_OUT = "Not logged in"
_CATALOG_AUTH_PAYLOAD = '{"auth_mode":"catalog-probe"}'
_CATALOG_PROVIDERS = (
    ("GROK_HOME", "grok"),
    ("CODEX_HOME", "codex"),
)


@pytest.fixture(autouse=True)
def _clear_probe_caches():
    clear_agent_cli_caches()
    yield
    clear_agent_cli_caches()


def _a_cli_that_says(output: str | None):
    return patch("agent_providers.process._catalog_cli_output", return_value=output)


def _a_claude_cli_that_says(output: str | None):
    return patch("agent_providers.process._claude_output", return_value=output)


def _a_shell_pretending_to_be_a_cli():
    return patch("agent_providers.process.resolve_cli_binary", return_value=Path("/bin/sh"))


def test_claude_reports_the_account_its_json_status_names() -> None:
    payload = json.dumps({
        CLAUDE_CLI_LOGGED_IN_FIELD: True,
        CLAUDE_CLI_AUTH_METHOD_FIELD: "claude.ai",
    })
    with _a_claude_cli_that_says(payload):
        login = claude_cli_login(Path("/mounted/claude"))

    assert login.logged_in is True
    assert login.auth_method == "claude.ai"


@pytest.mark.parametrize("output", (None, "not-json", "{}"))
def test_claude_without_a_parseable_status_is_logged_out(output: str | None) -> None:
    with _a_claude_cli_that_says(output):
        login = claude_cli_login(Path("/mounted/claude"))

    assert login.logged_in is False
    assert login.auth_method is None


def test_the_probe_caller_budget_is_the_probe_timeout_plus_the_settle_margin() -> None:
    assert process.CLI_PROBE_CALLER_TIMEOUT_SECONDS == (
        process.PROBE_TIMEOUT_SECONDS + cli_settle_deadline(0.0)
    )


def test_a_claude_probe_that_exceeds_its_caller_budget_is_logged_out(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def _hanging_output(_binary: str) -> str | None:
        started.set()
        release.wait(timeout=1)
        return None

    monkeypatch.setattr("agent_providers.process._claude_output", _hanging_output)
    monkeypatch.setattr("agent_providers.process.CLI_PROBE_CALLER_TIMEOUT_SECONDS", 0.05)
    try:
        login = claude_cli_login(Path("/mounted/claude"))
    finally:
        release.set()

    assert started.is_set()
    assert login.logged_in is False


def test_grok_reports_the_account_it_is_signed_in_with() -> None:
    with _a_cli_that_says(GROK_LOGGED_IN):
        status = grok_cli_status()

    assert status.login.logged_in is True
    assert status.login.auth_method == "grok.com"


def test_grok_lists_the_models_under_its_login() -> None:
    with _a_cli_that_says(GROK_LOGGED_IN):
        status = grok_cli_status()

    assert status.model_names == ("grok-4.6", "grok-4.5", "deepseek-v4-flash")


def test_grok_without_a_login_reports_no_models_even_though_it_prints_some() -> None:
    with _a_cli_that_says(GROK_LOGGED_OUT):
        status = grok_cli_status()

    assert status.login.logged_in is False
    assert status.model_names == ()


def test_grok_that_cannot_be_asked_counts_as_logged_out() -> None:
    with _a_cli_that_says(None):
        assert grok_cli_status().login.logged_in is False


def test_grok_answering_in_words_we_do_not_know_raises() -> None:
    with _a_cli_that_says("Session ready.\n"), pytest.raises(
        CliLoginUnusableError,
        match="login status",
    ):
        grok_cli_status()


def test_grok_that_is_signed_in_but_lists_nothing_raises() -> None:
    with _a_cli_that_says("You are logged in with grok.com.\n"), pytest.raises(
        AgentCliUnavailableError,
        match="model name",
    ) as raised:
        grok_cli_status()

    assert type(raised.value) is AgentCliUnavailableError


def test_codex_reports_the_account_it_is_signed_in_with() -> None:
    with _a_cli_that_says(CODEX_LOGGED_IN):
        login = codex_cli_login()

    assert login.logged_in is True
    assert login.auth_method == "ChatGPT"


def test_codex_without_a_login_is_logged_out() -> None:
    with _a_cli_that_says(CODEX_LOGGED_OUT):
        login = codex_cli_login()

    assert login.logged_in is False
    assert login.auth_method is None


def test_codex_that_cannot_be_asked_counts_as_logged_out() -> None:
    with _a_cli_that_says(None):
        assert codex_cli_login().logged_in is False


def test_codex_model_catalog_reads_past_the_login_probe_limit(monkeypatch) -> None:
    catalog_length = CLI_OUTPUT_READ_LIMIT_BYTES + 1
    monkeypatch.setattr(process, "resolve_cli_binary", lambda _binary: Path(sys.executable))
    monkeypatch.setattr(
        process,
        "CODEX_CLI_MODELS_ARGS",
        (
            "-c",
            (
                "import sys; "
                f"sys.stdout.write('x' * {catalog_length}); "
                "sys.stderr.write('not catalog output')"
            ),
        ),
    )

    assert codex_cli_model_catalog() == "x" * catalog_length


@pytest.mark.parametrize(
    "run",
    (
        None,
        CliRun(returncode=None, stdout="", stderr="", complete=False),
        CliRun(returncode=1, stdout="", stderr="", complete=True),
    ),
    ids=("no run", "incomplete", "non-zero"),
)
def test_codex_catalogue_that_does_not_come_back_raises_the_bare_base(
    monkeypatch, run: CliRun | None
) -> None:
    monkeypatch.setattr(process, "resolve_cli_binary", lambda _binary: Path(sys.executable))
    monkeypatch.setattr(process, "_run_credentialed_catalog_cli", lambda *_a, **_k: run)

    with pytest.raises(AgentCliUnavailableError, match="did not return a catalog") as raised:
        codex_cli_model_catalog()

    assert type(raised.value) is AgentCliUnavailableError


def test_codex_answering_in_words_we_do_not_know_raises() -> None:
    with _a_cli_that_says("auth: unknown"), pytest.raises(
        CliLoginUnusableError,
        match="login status",
    ):
        codex_cli_login()


@pytest.mark.parametrize(
    ("ask", "answer"),
    [(grok_cli_status, GROK_LOGGED_IN), (codex_cli_login, CODEX_LOGGED_IN)],
)
def test_a_second_ask_reuses_the_recent_answer(ask, answer) -> None:
    with _a_cli_that_says(answer) as spawn:
        first = ask()
        second = ask()

    assert first == second
    spawn.assert_called_once()


def test_a_second_claude_ask_reuses_the_recent_answer() -> None:
    with _a_claude_cli_that_says("{}") as spawn:
        first = claude_cli_login(Path("/mounted/claude"))
        second = claude_cli_login(Path("/mounted/claude"))

    assert first == second
    spawn.assert_called_once()


def test_expired_cache_asks_the_cli_again() -> None:
    with (
        _a_cli_that_says(CODEX_LOGGED_IN) as spawn,
        patch("agent_providers.process.CLI_LOGIN_STATUS_CACHE_SECONDS", 0),
    ):
        codex_cli_login()
        codex_cli_login()

    assert spawn.call_count == 2


def test_a_cli_we_cannot_read_is_not_re_asked_on_every_request() -> None:
    with _a_cli_that_says("nonsense the parser does not know") as spawn:
        for _ in range(3):
            with pytest.raises(AgentCliUnavailableError):
                grok_cli_status()

    spawn.assert_called_once()


def test_parallel_cold_asks_spawn_the_cli_once() -> None:
    started = threading.Event()
    release = threading.Event()
    answers: list[str] = []
    probe_calls = 0

    def probe() -> str:
        nonlocal probe_calls
        probe_calls += 1
        started.set()
        release.wait()
        return "answer"

    cached = CachedProbe(probe)
    first = threading.Thread(target=lambda: answers.append(cached.get()))
    second = threading.Thread(target=lambda: answers.append(cached.get()))
    first.start()
    assert started.wait(timeout=1)
    second.start()
    assert second.is_alive()
    release.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert answers == ["answer", "answer"]
    assert probe_calls == 1


def test_parallel_cold_asks_share_one_failure(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    follower_paused_after_miss = threading.Event()
    resume_follower = threading.Event()
    failures: list[Exception] = []
    probe_calls = 0

    def probe() -> str:
        nonlocal probe_calls
        probe_calls += 1
        started.set()
        release.wait()
        raise AgentCliUnavailableError("bad answer")

    cached = CachedProbe(probe)

    def ask() -> None:
        with pytest.raises(AgentCliUnavailableError) as raised:
            cached.get()
        failures.append(raised.value)

    first = threading.Thread(
        target=ask,
        name="single-flight leader caller",
        daemon=True,
    )
    second = threading.Thread(
        target=ask,
        name="single-flight follower caller",
    )
    real_refresh = cached.refresh

    def pause_follower_after_cache_miss() -> str:
        if threading.current_thread() is second:
            follower_paused_after_miss.set()
            assert resume_follower.wait(timeout=1)
        return real_refresh()

    monkeypatch.setattr(cached, "refresh", pause_follower_after_cache_miss)

    first_started = False
    second_started = False
    try:
        first.start()
        first_started = True
        assert started.wait(timeout=1)
        second.start()
        second_started = True
        assert follower_paused_after_miss.wait(timeout=1)
        release.set()
        first.join(timeout=1)
        assert not first.is_alive()
        resume_follower.set()
    finally:
        release.set()
        resume_follower.set()
        if first_started:
            first.join(timeout=1)
            assert not first.is_alive()
        if second_started:
            second.join(timeout=1)
            assert not second.is_alive()

    assert len(failures) == 2
    assert failures[0] is failures[1]
    assert probe_calls == 1


def test_a_follower_waits_only_its_own_single_flight_budget(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def probe() -> str:
        started.set()
        release.wait()
        return "answer"

    cached = CachedProbe(probe)
    monkeypatch.setattr("agent_providers.process.CLI_PROBE_CALLER_TIMEOUT_SECONDS", 0.05)

    def first_ask() -> None:
        try:
            cached.get()
        except CliProbeBudgetExceeded:
            pass

    first = threading.Thread(target=first_ask)
    first.start()
    assert started.wait(timeout=1)

    started_waiting = time.monotonic()
    with pytest.raises(CliProbeBudgetExceeded, match="caller budget"):
        cached.get()
    assert time.monotonic() - started_waiting < 0.2

    release.set()
    first.join(timeout=1)
    assert not first.is_alive()
    assert cached.get() == "answer"


def test_run_cli_returns_after_its_answer_budget_and_cleanup_grace(
    monkeypatch, failure_watch,
) -> None:
    answer_budget = 0.05
    cleanup_grace = 0.05
    monkeypatch.setattr("agent_providers.process.PROBE_TIMEOUT_SECONDS", answer_budget)
    monkeypatch.setattr("agent_providers.process.CLI_TERMINATION_GRACE_SECONDS", cleanup_grace)

    started_at = time.monotonic()
    run = run_catalog_cli(shell_child("-c", "trap '' TERM; while :; do :; done"))
    elapsed = time.monotonic() - started_at

    assert run is not None
    assert run.complete is False
    assert elapsed < answer_budget + (2 * cleanup_grace) + 0.15
    failure_watch.assert_nothing_leaked()


def test_clearing_a_probe_does_not_restore_its_pre_clear_result() -> None:
    started = threading.Event()
    release = threading.Event()
    results = iter(("before clear", "after clear"))

    def probe() -> str:
        started.set()
        release.wait(timeout=1)
        return next(results)

    cached = CachedProbe(probe)
    first_answer: list[str] = []
    first = threading.Thread(target=lambda: first_answer.append(cached.get()))
    first.start()
    assert started.wait(timeout=1)

    cached.clear()
    release.set()
    first.join(timeout=1)

    assert first_answer == ["before clear"]
    assert cached.get() == "after clear"


_PROBE_THREAD_BOUND_SECONDS = 5


class _ProbeUnderWatch:
    """A probe that waits on a gate and counts its calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> str:
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=_PROBE_THREAD_BOUND_SECONDS)
        return "answer"


def _probe_threads() -> list[threading.Thread]:
    return [
        thread for thread in threading.enumerate() if thread.name.endswith("(_run_and_resolve)")
    ]


class _WaitAnnouncements:
    """Which named threads wait on a flight, and on which future."""

    def __init__(self) -> None:
        self._waiting: dict[str, threading.Event] = {}
        self.futures: dict[str, concurrent.futures.Future] = {}

    def waiting(self, name: str) -> threading.Event:
        return self._waiting.setdefault(name, threading.Event())


def _announce_the_waiters(monkeypatch) -> _WaitAnnouncements:
    announcements = _WaitAnnouncements()

    class AnnouncingFuture(concurrent.futures.Future):
        def result(self, timeout=None):
            name = threading.current_thread().name
            announcements.futures[name] = self
            announcements.waiting(name).set()
            return super().result(timeout)

    monkeypatch.setattr(concurrent.futures, "Future", AnnouncingFuture)
    return announcements


def _answer_on_a_named_thread(
    cached: CachedProbe, answers: list[str], name: str,
) -> threading.Thread:
    worker = threading.Thread(target=lambda: answers.append(cached.get()), name=name, daemon=True)
    worker.start()
    return worker


def test_a_probe_thread_that_cannot_start_leaves_no_flight_behind(monkeypatch) -> None:
    probe = _ProbeUnderWatch()
    probe.release.set()
    cached = CachedProbe(probe)
    cannot_start = RuntimeError("can't start new thread")

    with monkeypatch.context() as failing:
        fail_to_start_the_runner_thread(failing, cannot_start, target=cached._run_and_resolve)
        with pytest.raises(RuntimeError) as raised:
            cached.get()

    assert raised.value is cannot_start
    assert probe.calls == 0
    monkeypatch.setattr(
        "agent_providers.process.CLI_PROBE_CALLER_TIMEOUT_SECONDS", _PROBE_THREAD_BOUND_SECONDS,
    )
    assert cached.get() == "answer"
    assert probe.calls == 1


def test_a_login_probe_that_cannot_start_a_thread_is_retried_by_the_next_call(
    monkeypatch,
) -> None:
    probed = CachedProbe(lambda: CliLogin(logged_in=True, auth_method="chatgpt"))
    monkeypatch.setattr(process, "_codex_login_probe", probed)
    monkeypatch.setattr(
        "agent_providers.process.CLI_PROBE_CALLER_TIMEOUT_SECONDS", _PROBE_THREAD_BOUND_SECONDS,
    )
    cannot_start = RuntimeError("can't start new thread")

    with monkeypatch.context() as failing:
        fail_to_start_the_runner_thread(failing, cannot_start, target=probed._run_and_resolve)
        with pytest.raises(RuntimeError) as raised:
            codex_cli_login()

    assert raised.value is cannot_start
    assert codex_cli_login().logged_in is True


@pytest.mark.parametrize("thread_claims_first", [False, True])
def test_a_probe_start_that_raises_leaves_one_owner_of_the_flight(
    monkeypatch, thread_claims_first: bool,
) -> None:
    probe = _ProbeUnderWatch()
    cached = CachedProbe(probe)
    announcements = _announce_the_waiters(monkeypatch)
    interruption = KeyboardInterrupt()
    follower_answers: list[str] = []
    follower: threading.Thread | None = None

    try:
        with monkeypatch.context() as failing:
            gate = start_then_raise(
                failing,
                interruption,
                target=cached._run_and_resolve,
                thread_claims_first=thread_claims_first,
            )
            threads_before = set(_probe_threads())
            try:
                with pytest.raises(KeyboardInterrupt) as raised:
                    cached.get()
                assert raised.value is interruption
                if thread_claims_first:
                    follower = _answer_on_a_named_thread(cached, follower_answers, "follower")
                    assert announcements.waiting("follower").wait(
                        timeout=_PROBE_THREAD_BOUND_SECONDS,
                    )
                    assert probe.entered.wait(timeout=_PROBE_THREAD_BOUND_SECONDS)
                    assert probe.calls == 1
                else:
                    owned_threads = set(_probe_threads()) - threads_before
                    assert len(owned_threads) == 1
                    gate.set()
                    for thread in owned_threads:
                        thread.join(timeout=_PROBE_THREAD_BOUND_SECONDS)
                        assert not thread.is_alive()
                    assert probe.calls == 0
            finally:
                gate.set()
    finally:
        probe.release.set()
        if follower is not None:
            follower.join(timeout=_PROBE_THREAD_BOUND_SECONDS)

    assert follower_answers == (["answer"] if thread_claims_first else [])
    assert cached.get() == "answer"
    assert probe.calls == 1


def test_a_started_probe_thread_owns_its_flight_before_it_claims(monkeypatch) -> None:
    probe = _ProbeUnderWatch()
    cached = CachedProbe(probe)
    announcements = _announce_the_waiters(monkeypatch)
    gate, _ = order_the_thread_start_claims(monkeypatch, thread_claims_first=False)
    answers: list[str] = []
    workers: list[threading.Thread] = []

    try:
        workers.append(_answer_on_a_named_thread(cached, answers, "starter"))
        assert announcements.waiting("starter").wait(timeout=_PROBE_THREAD_BOUND_SECONDS)
        workers.append(_answer_on_a_named_thread(cached, answers, "follower"))
        assert announcements.waiting("follower").wait(timeout=_PROBE_THREAD_BOUND_SECONDS)
        assert announcements.futures["follower"] is announcements.futures["starter"]
        assert probe.calls == 0
        gate.set()
    finally:
        gate.set()
        probe.release.set()
        for worker in workers:
            worker.join(timeout=_PROBE_THREAD_BOUND_SECONDS)

    assert answers == ["answer", "answer"]
    assert probe.calls == 1


def test_a_cli_that_floods_us_is_read_only_up_to_the_limit() -> None:
    flood = "while :; do printf stdout; printf stderr >&2; done"
    with _a_shell_pretending_to_be_a_cli():
        run = run_catalog_cli(shell_child("-c", flood))

    assert run is not None
    assert run.complete is False
    assert len(run.stdout) + len(run.stderr) <= CLI_OUTPUT_READ_LIMIT_BYTES


def test_a_cli_that_never_answers_is_given_up_on() -> None:
    with (
        _a_shell_pretending_to_be_a_cli(),
        patch("agent_providers.process.PROBE_TIMEOUT_SECONDS", 0.2),
    ):
        assert run_catalog_cli(shell_child("-c", "while :; do :; done")).complete is False


def test_a_cli_that_leaves_a_child_behind_is_terminated_with_its_group() -> None:
    command = "{ while :; do :; done; } & wait"
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        started.append(child)
        return child

    with (
        _a_shell_pretending_to_be_a_cli(),
        patch("agent_providers.process.PROBE_TIMEOUT_SECONDS", 0.2),
        patch("agent_providers.spawn.subprocess.Popen", side_effect=capture_process),
    ):
        assert run_catalog_cli(shell_child("-c", command)).complete is False

    assert started
    with pytest.raises(ProcessLookupError):
        os.killpg(started[0].pid, 0)


def test_a_sigterm_ignoring_cli_and_child_are_reaped_after_sigkill() -> None:
    command = "trap '' TERM; { trap '' TERM; while :; do :; done; } & while :; do :; done"
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        started.append(child)
        return child

    with (
        _a_shell_pretending_to_be_a_cli(),
        patch("agent_providers.process.PROBE_TIMEOUT_SECONDS", 0.05),
        patch("agent_providers.process.CLI_TERMINATION_GRACE_SECONDS", 0.1),
        patch("agent_providers.spawn.subprocess.Popen", side_effect=capture_process),
    ):
        assert run_catalog_cli(shell_child("-c", command)).complete is False

    assert started[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(started[0].pid, 0)


def test_a_spawn_that_returns_after_its_deadline_is_reaped() -> None:
    release_spawn = threading.Event()
    spawned = threading.Event()
    reaped = threading.Event()
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen
    real_reap = process._reap_process_group

    def late_process(*args, **kwargs):
        release_spawn.wait()
        child = real_popen(*args, **kwargs)
        started.append(child)
        spawned.set()
        return child

    def capture_reap(child: subprocess.Popen[bytes]) -> bool:
        try:
            return real_reap(child)
        finally:
            reaped.set()

    with (
        patch("agent_providers.process.PROBE_TIMEOUT_SECONDS", 0.1),
        patch("agent_providers.spawn.subprocess.Popen", side_effect=late_process),
        patch("agent_providers.process._reap_process_group", side_effect=capture_reap),
    ):
        assert run_catalog_cli(shell_child("-c", "while :; do :; done")) is None
        release_spawn.set()
        assert spawned.wait(timeout=1)
        assert reaped.wait(timeout=1)

    with pytest.raises(ProcessLookupError):
        os.killpg(started[0].pid, 0)


def test_bounded_runner_returns_on_a_stalled_spawn_and_reaps_its_late_process() -> None:
    release_spawn = threading.Event()
    spawned = threading.Event()
    reaped = threading.Event()
    callbacks_reaped = threading.Event()
    started: list[subprocess.Popen[bytes]] = []
    spawned_process_ids: list[int] = []
    reaped_processes: list[tuple[int, bool]] = []
    real_popen = subprocess.Popen
    real_reap = process._reap_process_group

    def late_process(*args, **kwargs):
        release_spawn.wait()
        child = real_popen(*args, **kwargs)
        started.append(child)
        spawned.set()
        return child

    def capture_reap(child: subprocess.Popen[bytes]) -> bool:
        try:
            return real_reap(child)
        finally:
            reaped.set()

    def record_reaped(process_id: int, became_zombie: bool) -> None:
        reaped_processes.append((process_id, became_zombie))
        callbacks_reaped.set()

    with (
        patch("agent_providers.spawn.subprocess.Popen", side_effect=late_process),
        patch("agent_providers.process._reap_process_group", side_effect=capture_reap),
    ):
        deadline = time.monotonic() + 0.05
        assert run_cli_bounded(
            shell_child("-c", "while :; do :; done"),
            stdin_payload=None,
            read="all",
            deadline=deadline,
            on_spawned=spawned_process_ids.append,
            on_reaped=record_reaped,
        ).reason is CliRunReason.DEADLINE_BEFORE_SPAWN
        release_spawn.set()
        assert spawned.wait(timeout=1)
        assert reaped.wait(timeout=1)
        assert callbacks_reaped.wait(timeout=1)

    assert spawned_process_ids == [started[0].pid]
    assert reaped_processes == [(started[0].pid, False)]
    with pytest.raises(ProcessLookupError):
        os.killpg(started[0].pid, 0)


def test_bounded_runner_reports_a_spawn_error() -> None:
    error = OSError("cannot start")
    spawned_process_ids: list[int] = []
    reaped_processes: list[tuple[int, bool]] = []
    with patch("agent_providers.spawn.subprocess.Popen", side_effect=error):
        outcome = run_cli_bounded(
            shell_child("-c", ":"),
            stdin_payload=None,
            read="all",
            deadline=time.monotonic() + 1,
            on_spawned=spawned_process_ids.append,
            on_reaped=lambda process_id, became_zombie: reaped_processes.append(
                (process_id, became_zombie),
            ),
        )

    assert outcome.started is False
    assert outcome.spawn_error is error
    assert outcome.returncode is None
    assert outcome.reason is CliRunReason.SPAWN_FAILED
    assert spawned_process_ids == []
    assert reaped_processes == []


def test_bounded_runner_reports_a_non_os_spawn_error_immediately() -> None:
    error = ValueError("empty argv")
    spawned_process_ids: list[int] = []
    reaped_processes: list[tuple[int, bool]] = []
    deadline = time.monotonic() + 1

    with patch("agent_providers.spawn.subprocess.Popen", side_effect=error):
        outcome = run_cli_bounded(
            shell_child("-c", ":"),
            stdin_payload=None,
            read="all",
            deadline=deadline,
            on_spawned=spawned_process_ids.append,
            on_reaped=lambda process_id, became_zombie: reaped_processes.append(
                (process_id, became_zombie),
            ),
        )

    assert outcome.reason is CliRunReason.SPAWN_FAILED
    assert outcome.spawn_error is error
    assert time.monotonic() < deadline - 0.5
    assert spawned_process_ids == []
    assert reaped_processes == []


def test_bounded_runner_carries_an_output_io_error(monkeypatch) -> None:
    error = OSError("cannot switch stream mode")

    def fail_to_set_blocking(_fd: int, _value: bool) -> None:
        raise error

    monkeypatch.setattr("agent_providers.process.os.set_blocking", fail_to_set_blocking)

    outcome = run_cli_bounded(
        shell_child("-c", "printf output"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
    )

    assert outcome.reason is CliRunReason.IO_ERROR
    assert outcome.io_error is error


def test_bounded_runner_reports_a_stdin_close_error_after_spawning(monkeypatch) -> None:
    error = OSError("cannot close stdin")
    spawned_process_ids: list[int] = []
    reaped_processes: list[tuple[int, bool]] = []

    def fail_to_close_stdin(_process: subprocess.Popen[bytes]) -> None:
        raise error

    def record_reaped(process_id: int, became_zombie: bool) -> None:
        reaped_processes.append((process_id, became_zombie))

    monkeypatch.setattr("agent_providers.process._close_stdin", fail_to_close_stdin)
    outcome = run_cli_bounded(
        shell_child("-c", "exec sleep 10"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
        on_spawned=spawned_process_ids.append,
        on_reaped=record_reaped,
    )

    assert outcome.reason is CliRunReason.IO_ERROR
    assert outcome.io_error is error
    assert len(spawned_process_ids) == 1
    assert reaped_processes == [(spawned_process_ids[0], False)]


def test_bounded_runner_returns_at_the_settle_instant_when_its_cleanup_overruns(
    monkeypatch, failure_watch,
) -> None:
    monkeypatch.setattr("agent_providers.process.CLI_TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr("agent_providers.process.CLI_PROBE_CALLER_TIMEOUT_MARGIN_SECONDS", 0.01)
    release_cleanup = threading.Event()
    reaped = threading.Event()
    real_reap = process._reap_process_group

    def delayed_reap(child: subprocess.Popen[bytes]) -> bool:
        release_cleanup.wait(timeout=1)
        try:
            return real_reap(child)
        finally:
            reaped.set()

    monkeypatch.setattr("agent_providers.process._reap_process_group", delayed_reap)
    deadline = time.monotonic() + 0.02
    outcome = run_cli_bounded(
        shell_child("-c", "exec sleep 10"),
        stdin_payload=None,
        read="all",
        deadline=deadline,
    )
    returned_at = time.monotonic()

    assert outcome.reason is CliRunReason.CLEANUP_OVERRAN
    assert cli_settle_deadline(deadline) <= returned_at < cli_settle_deadline(deadline) + 0.5
    release_cleanup.set()
    assert reaped.wait(timeout=1)
    failure_watch.assert_nothing_leaked()


class _LateWakingEvent(threading.Event):
    """An event whose first timed-out wait wakes late, as a loaded scheduler would."""

    lateness_seconds = 0.6

    def __init__(self) -> None:
        super().__init__()
        self._woke_late = False

    def wait(self, timeout: float | None = None) -> bool:
        occurred = super().wait(timeout)
        if not occurred and not self._woke_late:
            self._woke_late = True
            time.sleep(self.lateness_seconds)
        return occurred


def test_the_second_wait_ends_at_the_settle_instant_however_late_the_first_one_woke(
    monkeypatch,
) -> None:
    monkeypatch.setattr("agent_providers.process.CLI_TERMINATION_GRACE_SECONDS", 0.2)

    @dataclass
    class LateWakingState(process._BoundedRunState):
        completed: threading.Event = field(default_factory=_LateWakingEvent)

    release_cleanup = threading.Event()
    real_reap = process._reap_process_group
    monkeypatch.setattr(process, "_BoundedRunState", LateWakingState)
    monkeypatch.setattr(
        process,
        "_reap_process_group",
        lambda child: (release_cleanup.wait(timeout=2), real_reap(child))[1],
    )
    deadline = time.monotonic() + 0.05
    reaped = threading.Event()

    outcome = run_cli_bounded(
        shell_child("-c", "exec sleep 10"),
        stdin_payload=None,
        read="all",
        deadline=deadline,
        on_reaped=lambda _process_id, _became_zombie: reaped.set(),
    )
    returned_at = time.monotonic()
    release_cleanup.set()

    assert outcome.reason is CliRunReason.CLEANUP_OVERRAN
    assert returned_at < cli_settle_deadline(deadline) + _LateWakingEvent.lateness_seconds / 2
    assert reaped.wait(timeout=5)


def test_bounded_runner_notifies_a_zombie_reap_only_after_background_confirmation(
    monkeypatch,
) -> None:
    background_started = threading.Event()
    allow_background_reap = threading.Event()
    callback_finished = threading.Event()
    spawned_process_ids: list[int] = []
    callbacks: list[tuple[int, bool]] = []

    def await_background_reap(child, callback) -> None:
        background_started.set()
        assert allow_background_reap.wait(timeout=1)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()
        child.wait()
        process._notify_reaped(callback, child.pid, became_zombie=True)

    def record_reaped(process_id: int, became_zombie: bool) -> None:
        callbacks.append((process_id, became_zombie))
        callback_finished.set()

    monkeypatch.setattr("agent_providers.process._reap_process_group", lambda _process: True)
    monkeypatch.setattr("agent_providers.process._reap_in_background", await_background_reap)

    outcome = run_cli_bounded(
        shell_child("-c", "printf ready"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
        on_spawned=spawned_process_ids.append,
        on_reaped=record_reaped,
    )

    assert outcome.became_zombie is True
    assert background_started.wait(timeout=1)
    assert callbacks == []
    allow_background_reap.set()
    assert callback_finished.wait(timeout=1)
    assert callbacks == [(spawned_process_ids[0], True)]


def test_bounded_runner_stops_a_cli_that_never_reads_its_full_stdin_pipe() -> None:
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        started.append(child)
        return child

    with patch("agent_providers.spawn.subprocess.Popen", side_effect=capture_process):
        outcome = run_cli_bounded(
            shell_child("-c", "exec sleep 10"),
            stdin_payload=b"x" * (1024 * 1024),
            read="all",
            deadline=time.monotonic() + 0.05,
        )

    assert outcome is not None
    assert outcome.complete is False
    assert started[0].poll() is not None


def test_bounded_runner_stops_a_cli_that_never_writes_output() -> None:
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        started.append(child)
        return child

    with patch("agent_providers.spawn.subprocess.Popen", side_effect=capture_process):
        outcome = run_cli_bounded(
            shell_child("-c", "exec sleep 10"),
            stdin_payload=None,
            read="all",
            deadline=time.monotonic() + 0.05,
        )

    assert outcome is not None
    assert outcome.complete is False
    assert started[0].poll() is not None


def test_bounded_runner_marks_an_unconfirmed_sigkill_as_a_zombie(monkeypatch) -> None:
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr(
        "agent_providers.process._wait_for_process_group_exit",
        lambda _process, _timeout: False,
    )
    monkeypatch.setattr("agent_providers.process.CLI_TERMINATION_GRACE_SECONDS", 0.01)
    with patch("agent_providers.spawn.subprocess.Popen", side_effect=capture_process):
        outcome = run_cli_bounded(
            shell_child("-c", "trap '' TERM; while :; do :; done"),
            stdin_payload=None,
            read="all",
            deadline=time.monotonic() + 0.02,
        )

    assert outcome is not None
    assert outcome.became_zombie is True
    started[0].wait(timeout=1)


def test_run_cli_logs_a_sigkill_survivor_without_starting_a_background_reaper(
    monkeypatch, caplog,
) -> None:
    reaper_may_finish = threading.Event()
    threads_before = {thread.ident for thread in threading.enumerate()}
    monkeypatch.setattr(
        "agent_providers.process._wait_for_process_group_exit",
        lambda _process, _timeout: False,
    )
    monkeypatch.setattr(
        "agent_providers.process._process_group_exists",
        lambda _process_id: not reaper_may_finish.is_set(),
    )
    monkeypatch.setattr("agent_providers.process.CLI_TERMINATION_GRACE_SECONDS", 0.01)
    caplog.set_level("WARNING")

    try:
        run = run_catalog_cli(shell_child("-c", "printf ready"))
        threads_after = {thread.ident for thread in threading.enumerate()}
    finally:
        reaper_may_finish.set()

    assert run == CliRun(returncode=0, stdout="ready", stderr="", complete=True)
    assert threads_after == threads_before
    assert any("survived its SIGKILL grace period" in record.message for record in caplog.records)


def test_background_reap_notifies_when_process_group_check_fails(
    monkeypatch, caplog, capfd,
) -> None:
    watch = RunnerFailureWatch(monkeypatch, caplog, capfd)
    process_pool = CodexProcessPool(maximum_processes=2, maximum_image_runs=1)
    reservation = process_pool.reserve(CodexProcessKind.IMAGE)
    child = _a_session_leader("printf ready")
    process_pool.bind(reservation, child.pid)
    callbacks: list[tuple[int, bool]] = []
    reap = pool_callbacks(process_pool, reservation)["on_reaped"]

    def record_and_reap(process_id: int, became_zombie: bool) -> None:
        callbacks.append((process_id, became_zombie))
        reap(process_id, became_zombie)

    monkeypatch.setattr(
        "agent_providers.process._process_group_exists",
        lambda _process_id: (_ for _ in ()).throw(OSError(A_RUNNER_FAILURE_CANARY)),
    )

    process._reap_in_background(child, record_and_reap)
    child.communicate()

    assert callbacks == [(child.pid, True)]
    assert watch.reported_steps() == ["background reap of agent CLI process group OSError"]
    assert process_pool.reservation_count() == 0
    watch.assert_nothing_leaked()


def test_background_reap_reports_a_failing_release_and_keeps_the_reservation(
    monkeypatch, caplog, capfd,
) -> None:
    watch = RunnerFailureWatch(monkeypatch, caplog, capfd)
    process_pool = CodexProcessPool(maximum_processes=2, maximum_image_runs=1)
    reservation = process_pool.reserve(CodexProcessKind.IMAGE)
    child = _a_session_leader(":")
    process_pool.bind(reservation, child.pid)

    process._reap_in_background(
        child,
        lambda process_id, _became_zombie: process_pool.reap(reservation, process_id + 1),
    )

    assert watch.reported_steps() == ["on_reaped RuntimeError"]
    assert process_pool.reservation_count() == 1
    watch.assert_nothing_leaked()


def _a_session_leader(command: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ("/bin/sh", "-c", command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


_A_SHORT_REAP_BOUND_SECONDS = 0.2
_REAP_BOUND_SLACK_SECONDS = 0.5


@pytest.fixture
def a_background_reap_over_a_group_that_never_dies(monkeypatch):
    monkeypatch.setattr(process, "_BACKGROUND_REAP_LIMIT_SECONDS", _A_SHORT_REAP_BOUND_SECONDS)
    monkeypatch.setattr(process, "_BACKGROUND_REAP_POLL_SECONDS", 0.01)
    monkeypatch.setattr(process, "_process_group_exists", lambda _process_id: True)
    held: list = []
    monkeypatch.setattr(process, "_reap_callbacks_held_until_exit", held)
    return held


def _reap_in_background_on_a_bounded_thread(child, on_reaped) -> Exception | None:
    raised: list[Exception] = []

    def reap() -> None:
        try:
            process._reap_in_background(child, on_reaped)
        except Exception as error:
            raised.append(error)

    reaper = threading.Thread(target=reap)
    reaper.start()
    reaper.join(timeout=_A_SHORT_REAP_BOUND_SECONDS + _REAP_BOUND_SLACK_SECONDS)
    assert not reaper.is_alive()
    return raised[0] if raised else None


def test_a_background_reap_ends_at_its_bound_and_keeps_what_it_holds(
    tmp_path: Path, a_background_reap_over_a_group_that_never_dies, caplog,
) -> None:
    held = a_background_reap_over_a_group_that_never_dies
    process_pool = CodexProcessPool(maximum_processes=2, maximum_image_runs=1)
    pool_reservation = process_pool.reserve(CodexProcessKind.IMAGE)
    home = _build_home(_a_source_login(tmp_path))
    home_reservation = home.reserve()
    child = _a_session_leader(":")
    process_pool.bind(pool_reservation, child.pid)
    release_pool = pool_callbacks(process_pool, pool_reservation)["on_reaped"]

    def on_reaped(process_id: int, became_zombie: bool) -> None:
        release_pool(process_id, became_zombie)
        home_reservation.on_reaped(process_id, became_zombie)

    caplog.set_level(logging.ERROR, logger=process.__name__)
    started = time.monotonic()

    assert _reap_in_background_on_a_bounded_thread(child, on_reaped) is None

    assert time.monotonic() - started < _A_SHORT_REAP_BOUND_SECONDS + _REAP_BOUND_SLACK_SECONDS
    [record] = caplog.records
    assert record.getMessage() == (
        f"agent CLI process group {child.pid} outlived its background reap"
    )
    assert record.exc_info is None
    assert held[-1] is on_reaped
    assert process_pool.reservation_count() == 1
    home.close()
    assert home.path.exists()
    child.communicate()
    held[-1](child.pid, True)
    assert process_pool.reservation_count() == 0
    assert not home.path.exists()


class _FailingLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise OSError("log sink is gone")


def test_a_home_stays_held_when_logging_fails_at_the_reap_bound(
    tmp_path: Path, a_background_reap_over_a_group_that_never_dies, monkeypatch,
) -> None:
    held = a_background_reap_over_a_group_that_never_dies
    monkeypatch.setattr(process.log, "handlers", [_FailingLogHandler()])
    monkeypatch.setattr(process.log, "propagate", False)
    home = _build_home(_a_source_login(tmp_path))
    home_path = home.path
    child = _a_session_leader(":")

    failure = _reap_in_background_on_a_bounded_thread(child, home.reserve().on_reaped)
    child.communicate()
    home.close()
    del home
    gc.collect()

    assert isinstance(failure, OSError)
    assert len(held) == 1
    assert home_path.exists()
    held.clear()
    gc.collect()


_REAP_OUTLIVED_IN_A_SUBPROCESS_SCRIPT = """
import gc
import subprocess
import sys
from pathlib import Path

from agent_providers import process
from agent_providers.config import ProviderRuntimeConfig, configure
from agent_providers.process import PrivateCredentialHome

configure(ProviderRuntimeConfig.model_validate_json(sys.argv[1]))
process._BACKGROUND_REAP_LIMIT_SECONDS = 0.2
process._BACKGROUND_REAP_POLL_SECONDS = 0.01
process._process_group_exists = lambda _process_id: True
home = PrivateCredentialHome(
    Path(sys.argv[2]),
    Path(".claude/.credentials.json"),
    prefix="private-home-",
    missing_credential_is_error=True,
)
home_path = home.path
child = subprocess.Popen(("/bin/sh", "-c", ":"))
process._reap_in_background(child, home.reserve().on_reaped)
child.wait()
home.close()
del home, child
gc.collect()
print(home_path)
print(home_path.exists())
"""


def test_a_home_whose_reap_outlived_its_bound_stays_until_exit(tmp_path: Path) -> None:
    home_path, existed_after_collection = _run_home_script(
        _REAP_OUTLIVED_IN_A_SUBPROCESS_SCRIPT, tmp_path,
    )

    assert existed_after_collection == "True"
    assert not Path(home_path).exists()


def test_a_zombie_whose_group_never_dies_frees_its_runner_thread(
    monkeypatch, a_background_reap_over_a_group_that_never_dies,
) -> None:
    held = a_background_reap_over_a_group_that_never_dies
    monkeypatch.setattr(process, "_reap_process_group", lambda _process: True)
    threads_before = set(threading.enumerate())
    reaped: list[tuple[int, bool]] = []

    outcome = run_cli_bounded(
        shell_child("-c", "printf ready"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
        on_reaped=lambda process_id, became_zombie: reaped.append((process_id, became_zombie)),
    )
    settled_at = time.monotonic()
    runner_threads = set(threading.enumerate()) - threads_before
    for runner_thread in runner_threads:
        runner_thread.join(timeout=_A_SHORT_REAP_BOUND_SECONDS + _REAP_BOUND_SLACK_SECONDS)

    assert outcome.became_zombie is True
    assert not any(runner_thread.is_alive() for runner_thread in runner_threads)
    assert time.monotonic() - settled_at < _A_SHORT_REAP_BOUND_SECONDS + _REAP_BOUND_SLACK_SECONDS
    assert reaped == []
    assert len(held) == 1



def test_bounded_runner_stops_collecting_at_the_byte_limit(monkeypatch) -> None:
    monkeypatch.setattr("agent_providers.process.CLI_OUTPUT_READ_LIMIT_BYTES", 32)

    outcome = run_cli_bounded(
        shell_child("-c", "while :; do printf x; done"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
    )

    assert outcome is not None
    assert outcome.complete is False
    assert len(outcome.stdout) + len(outcome.stderr) == 32


def test_run_cli_discards_partial_output_when_its_read_deadline_expires(monkeypatch) -> None:
    monkeypatch.setattr("agent_providers.process.PROBE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr("agent_providers.process.CLI_TERMINATION_GRACE_SECONDS", 0.01)

    run = run_catalog_cli(shell_child("-c", "printf partial; exec sleep 10"))

    assert run == CliRun(returncode=-15, stdout="", stderr="", complete=False)


def test_run_cli_keeps_a_started_cli_result_when_cleanup_reports_a_zombie(monkeypatch) -> None:
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr("agent_providers.process.PROBE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr("agent_providers.process.CLI_TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        "agent_providers.process._wait_for_process_group_exit",
        lambda _process, _timeout: False,
    )
    with patch("agent_providers.spawn.subprocess.Popen", side_effect=capture_process):
        run = run_catalog_cli(shell_child("-c", "trap '' TERM; while :; do :; done"))

    assert run is not None
    assert run.complete is False
    started[0].wait(timeout=1)


def test_bounded_runner_delivers_stdin_without_a_blocking_write() -> None:
    outcome = run_cli_bounded(
        shell_child("-c", "IFS= read -r line; printf '<%s>' \"$line\""),
        stdin_payload=b"delivered\n",
        read="all",
        deadline=time.monotonic() + 1,
    )

    assert outcome is not None
    assert outcome.complete is True
    assert outcome.stdout == "<delivered>"


def test_a_catalog_probe_closes_stdin_so_the_child_observes_eof() -> None:
    run = run_catalog_cli(
        shell_child("-c", "if IFS= read -r line; then printf data; else printf eof; fi"),
    )

    assert run == CliRun(returncode=0, stdout="eof", stderr="", complete=True)


def test_bounded_runner_returns_only_the_first_stdout_line() -> None:
    outcome = run_cli_bounded(
        shell_child("-c", "printf 'first\\nsecond\\n'; exec sleep 10"),
        stdin_payload=None,
        read="first_line",
        deadline=time.monotonic() + 1,
    )

    assert outcome is not None
    assert outcome.complete is True
    assert outcome.stdout == "first\n"


def test_bounded_runner_returns_the_last_stdout_bytes_at_eof_in_first_line_mode() -> None:
    outcome = run_cli_bounded(
        shell_child("-c", "printf final"),
        stdin_payload=None,
        read="first_line",
        deadline=time.monotonic() + 1,
    )

    assert outcome is not None
    assert outcome.complete is True
    assert outcome.stdout == "final"


def test_bounded_runner_drains_both_output_streams_in_all_mode() -> None:
    outcome = run_cli_bounded(
        shell_child("-c", "printf stdout; printf stderr >&2"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
    )

    assert outcome is not None
    assert outcome.complete is True
    assert outcome.stdout == "stdout"
    assert outcome.stderr == "stderr"


def test_bounded_runner_applies_its_byte_limit_to_both_streams(monkeypatch) -> None:
    monkeypatch.setattr("agent_providers.process.CLI_OUTPUT_READ_LIMIT_BYTES", 4)

    outcome = run_cli_bounded(
        shell_child("-c", "printf 123; printf abc >&2"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
    )

    assert outcome.complete is False
    assert outcome.reason is CliRunReason.OUTPUT_LIMIT_REACHED
    assert len(outcome.stdout) + len(outcome.stderr) == 4


def test_bounded_runner_can_discard_stderr() -> None:
    outcome = run_cli_bounded(
        shell_child("-c", "printf stdout; printf stderr >&2"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
        stderr="devnull",
    )

    assert outcome.complete is True
    assert outcome.stdout == "stdout"
    assert outcome.stderr == ""


@pytest.mark.parametrize(
    "run",
    [
        CliRun(returncode=1, stdout=CODEX_LOGGED_IN, stderr="", complete=True),
        CliRun(returncode=0, stdout=CODEX_LOGGED_IN, stderr="", complete=False),
    ],
)
def test_an_incomplete_or_failed_run_is_not_accepted_as_authenticated(run: CliRun) -> None:
    with patch("agent_providers.process.run_claude_catalog_cli", return_value=run):
        login = codex_cli_login()

    assert login.logged_in is False


def test_claude_parses_only_stdout() -> None:
    stdout = json.dumps({CLAUDE_CLI_LOGGED_IN_FIELD: True})
    stderr = json.dumps({CLAUDE_CLI_LOGGED_IN_FIELD: False})
    run = CliRun(returncode=0, stdout=stdout, stderr=stderr, complete=True)
    with patch("agent_providers.process.run_claude_catalog_cli", return_value=run):
        login = claude_cli_login(Path("/mounted/claude"))

    assert login.logged_in is True


@pytest.mark.parametrize(
    "run",
    [
        CliRun(returncode=1, stdout="{}", stderr="", complete=True),
        CliRun(returncode=0, stdout="{}", stderr="", complete=False),
    ],
)
def test_claude_does_not_accept_a_failed_or_incomplete_run(run: CliRun) -> None:
    with patch("agent_providers.process.run_claude_catalog_cli", return_value=run):
        login = claude_cli_login(Path("/mounted/claude"))

    assert login.logged_in is False


def _codex_child_for_a_turn_home() -> None:
    current_config().codex_cli_auth_file.write_bytes(a_codex_login())
    turn_home = create_codex_turn_home(CODEX_TOOL_TURN_DIRECTORY_PREFIX)
    try:
        codex_child_process(("exec",), turn_home)
    finally:
        turn_home.home.close()


BINARY_SITES = (
    pytest.param(
        "claude", lambda: claude_cli_login(None), CLAUDE_CLI_BINARY_MISSING_DETAIL,
        id="claude-login",
    ),
    pytest.param("grok", grok_cli_status, GROK_CLI_BINARY_MISSING_DETAIL, id="grok-status"),
    pytest.param("codex", codex_cli_login, CODEX_CLI_BINARY_MISSING_DETAIL, id="codex-login"),
    pytest.param(
        "codex", codex_cli_model_catalog, CODEX_CLI_BINARY_MISSING_DETAIL,
        id="codex-model-catalog",
    ),
    pytest.param(
        "codex", _codex_child_for_a_turn_home, CODEX_CLI_BINARY_MISSING_DETAIL,
        id="codex-child-process",
    ),
)


@pytest.mark.parametrize(("provider", "ask", "detail"), BINARY_SITES)
def test_a_missing_binary_is_an_environment_fault_naming_the_provider(
    provider: str, ask: Callable[[], object], detail: str, tmp_path: Path,
) -> None:
    configured_value = "a-secret-binary-name"
    override_provider_runtime(cli_binary_search_path=(tmp_path,))
    override_cli_binary(provider, configured_value)

    with pytest.raises(CliEnvironmentUnavailableError) as raised:
        ask()

    assert raised.value.cause is CliEnvironmentCause.BINARY_MISSING
    assert str(raised.value) == detail
    assert provider.capitalize() in detail
    assert configured_value not in str(raised.value)
    assert raised.value.__cause__ is None


def _expected_closed_names(credential_home_variable: str) -> list[str]:
    """Every name a closed child may carry: its own plus the machine's."""
    return ["HOME", "TMPDIR", "PATH", credential_home_variable] + [
        name for name in INHERITED_MACHINE_VARIABLES if os.environ.get(name)
    ]


def _prepare_catalog_credential(
    tmp_path: Path,
    monkeypatch,
    *,
    provider: str,
    auth_payload: str = _CATALOG_AUTH_PAYLOAD,
) -> tuple[Path, Path]:
    original_home = tmp_path / "operator-home"
    original_home.mkdir()
    (original_home / "canary").write_text("untouched")
    credential = tmp_path / "host-named" / "auth.json"
    credential.parent.mkdir()
    credential.write_text(auth_payload)
    monkeypatch.setenv("HOME", str(original_home))
    monkeypatch.setenv("CATALOG_ENV_LEAK", "should-not-appear")
    override_login_source(provider, credential)
    return original_home, credential


def _run_catalog_python(
    tmp_path: Path,
    monkeypatch,
    *,
    credential_home_variable: str,
    provider: str,
    script: str,
    auth_payload: str = _CATALOG_AUTH_PAYLOAD,
) -> tuple[CliRun, Path, Path]:
    original_home, credential = _prepare_catalog_credential(
        tmp_path, monkeypatch, provider=provider, auth_payload=auth_payload,
    )
    run = _run_credentialed_catalog_cli(
        Path(sys.executable),
        ("-c", script),
        credential_home_variable=credential_home_variable,
        auth_file=credential,
    )
    assert run is not None
    return run, original_home, credential


def _parse_env_listing(listing: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in listing.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            parsed[name] = value
    return parsed


def test_private_credential_home_keeps_its_copy_until_the_last_reap(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text(_CATALOG_AUTH_PAYLOAD)
    home = PrivateCredentialHome(
        source,
        Path(".claude/.credentials.json"),
        prefix="private-home-",
        missing_credential_is_error=True,
    )
    reservation = home.reserve()
    copy = home.path / ".claude/.credentials.json"

    home.close()

    assert home.path.exists()
    assert copy.read_text() == _CATALOG_AUTH_PAYLOAD
    assert stat.S_IMODE(home.path.stat().st_mode) == 0o700
    assert stat.S_IMODE(copy.stat().st_mode) == 0o400
    assert stat.S_IMODE(copy.parent.stat().st_mode) == 0o700
    assert source.read_text() == _CATALOG_AUTH_PAYLOAD

    reservation.on_reaped(123, True)

    assert not home.path.exists()


def _credential_home(tmp_path: Path) -> PrivateCredentialHome:
    source = tmp_path / "source.json"
    source.write_text(_CATALOG_AUTH_PAYLOAD)
    return PrivateCredentialHome(
        source,
        Path(".claude/.credentials.json"),
        prefix="private-home-",
        missing_credential_is_error=True,
    )


@pytest.mark.parametrize("directory_mode", [0o500, 0o300, 0o100, 0o000])
def test_private_credential_home_removes_a_read_only_directory_a_child_left(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, directory_mode: int,
) -> None:
    home = _credential_home(tmp_path)
    child_directory = home.path / "child-made"
    child_directory.mkdir()
    (child_directory / "scratch.txt").write_text("scratch")
    child_directory.chmod(directory_mode)
    descriptors_before = _descriptor_count()
    caplog.set_level(logging.ERROR, logger=process.__name__)

    home.close()

    assert not home.path.exists()
    assert caplog.records == []
    assert _descriptor_count() == descriptors_before


@pytest.fixture
def outside(tmp_path: Path) -> Iterator[Path]:
    directory = tmp_path / "outside"
    (directory / "inner").mkdir(parents=True)
    secret = directory / "secret.txt"
    secret.write_text("outside")
    secret.chmod(0o400)
    (directory / "inner").chmod(0o500)
    directory.chmod(0o500)
    yield directory
    directory.chmod(0o700)
    (directory / "inner").chmod(0o700)


_WIDE_TREE_BREADTH = 200
_DEEP_TREE_DEPTH = 300


def _descriptor_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _held_directories_inside(home_path: Path) -> set[Path]:
    held: set[Path] = set()
    for descriptor in os.listdir("/proc/self/fd"):
        try:
            target = Path(os.readlink(Path("/proc/self/fd", descriptor)))
        except FileNotFoundError:
            continue
        if target.is_relative_to(home_path):
            held.add(target)
    return held


def _a_tree_needing_repair(home: PrivateCredentialHome, outside: Path) -> None:
    (home.path / "a" / "b" / "c").mkdir(parents=True)
    for sibling in ("d", "e", "f"):
        (home.path / sibling).mkdir()
    (home.path / "a" / "link").symlink_to(outside)
    leaves = [home.path / "a" / "b" / "c", *(home.path / sibling for sibling in ("d", "e", "f"))]
    for leaf in leaves:
        (leaf / "scratch.txt").write_text("scratch")
    for directory in (*leaves, home.path / "a" / "b", home.path / "a"):
        directory.chmod(0o500)


def _arm_after_the_first_removal_pass(
    monkeypatch: pytest.MonkeyPatch, home_path: Path,
) -> Callable[[], bool]:
    remove_tree = process.shutil.rmtree
    first_pass_failed: list[bool] = []

    def failing_first_pass(path) -> None:
        try:
            remove_tree(path)
        except OSError:
            if Path(path) == home_path:
                first_pass_failed.append(True)
            raise

    monkeypatch.setattr(process.shutil, "rmtree", failing_first_pass)
    return lambda: bool(first_pass_failed)


def test_private_credential_home_replaced_by_a_symlink_is_left_and_nothing_outside_changes(
    tmp_path: Path, outside: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    home = _credential_home(tmp_path)
    shutil.rmtree(home.path)
    home.path.symlink_to(outside)
    outside_before = tree_snapshot(outside)
    descriptors_before = _descriptor_count()
    caplog.set_level(logging.ERROR, logger=process.__name__)

    home.close()

    [record] = caplog.records
    assert record.getMessage() == "private credential home removal failed (NotADirectoryError)"
    assert home.path.is_symlink()
    assert tree_snapshot(outside) == outside_before
    assert _descriptor_count() == descriptors_before

    home.path.unlink()
    caplog.clear()
    home.close()

    assert not os.path.lexists(home.path)
    assert caplog.records == []


@pytest.mark.parametrize("swap_kind", ["before_acquisition", "after_acquisition", "vanished"])
def test_private_credential_home_never_follows_an_inner_directory_swapped_for_a_symlink(
    tmp_path: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    swap_kind: str,
) -> None:
    after_acquisition = swap_kind == "after_acquisition"
    home = _credential_home(tmp_path)
    child_directory = home.path / "child-made"
    for name in ("first", "second"):
        (child_directory / name).mkdir(parents=True)
        (child_directory / name / "scratch.txt").write_text("scratch")
        (child_directory / name).chmod(0o500)
    child_directory.chmod(0o500)
    outside_before = tree_snapshot(outside)
    descriptors_before = _descriptor_count()
    caplog.set_level(logging.ERROR, logger=process.__name__)
    remove_tree = process.shutil.rmtree
    is_armed = _arm_after_the_first_removal_pass(monkeypatch, home.path)
    real_open, real_chmod = os.open, os.chmod
    first_opened: list[Path] = []
    acquired: list[int] = []
    swapped: list[bool] = []

    def swap_for_a_symlink() -> None:
        swapped.append(True)
        [victim] = first_opened
        if after_acquisition:
            victim.rename(victim.with_name("moved-aside"))
        else:
            real_chmod(victim, 0o700)
            remove_tree(victim)
        if swap_kind != "vanished":
            victim.symlink_to(outside)

    def open_(path, flags, *args, dir_fd=None, **options):
        names_a_child = (
            is_armed()
            and dir_fd is not None
            and _resolved_inside(home.path, process._DESCRIPTOR_LINKS / str(dir_fd))
            == child_directory
        )
        if names_a_child and not first_opened:
            first_opened.append(child_directory / path)
        entry_is_victim = names_a_child and child_directory / path == first_opened[0]
        if entry_is_victim and not after_acquisition and not swapped:
            swap_for_a_symlink()
        descriptor = real_open(path, flags, *args, dir_fd=dir_fd, **options)
        if entry_is_victim and not swapped:
            acquired.append(descriptor)
        return descriptor

    def chmod(path, mode, **options):
        if is_armed() and not swapped and first_opened:
            names_the_victim_path = Path(path) == first_opened[0]
            names_the_acquired_victim = bool(acquired) and Path(path) == (
                process._DESCRIPTOR_LINKS / str(acquired[-1])
            )
            if names_the_victim_path or (after_acquisition and names_the_acquired_victim):
                swap_for_a_symlink()
        return real_chmod(path, mode, **options)

    monkeypatch.setattr(os, "open", open_)
    monkeypatch.setattr(os, "chmod", chmod)

    home.close()

    assert swapped == [True]
    assert tree_snapshot(outside) == outside_before
    assert not home.path.exists()
    assert caplog.records == []
    assert _descriptor_count() == descriptors_before


def _break_the_descriptor_links(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(process, "_DESCRIPTOR_LINKS", tmp_path / "missing")


def _break_o_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delattr(os, "O_PATH")


@pytest.mark.parametrize(
    ("break_the_route", "error_class"),
    [
        pytest.param(_break_the_descriptor_links, "FileNotFoundError", id="descriptor_links"),
        pytest.param(_break_o_path, "OSError", id="o_path"),
    ],
)
def test_private_credential_home_needing_repair_is_left_without_a_path_fallback(
    tmp_path: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    break_the_route: Callable[[pytest.MonkeyPatch, Path], None],
    error_class: str,
) -> None:
    home = _credential_home(tmp_path)
    _a_tree_needing_repair(home, outside)
    outside_before = tree_snapshot(outside)
    descriptors_before = _descriptor_count()
    caplog.set_level(logging.ERROR, logger=process.__name__)
    break_the_route(monkeypatch, tmp_path)

    home.close()

    [record] = caplog.records
    assert record.getMessage() == f"private credential home removal failed ({error_class})"
    assert home.path.exists()
    assert tree_snapshot(outside) == outside_before
    assert _descriptor_count() == descriptors_before

    monkeypatch.undo()
    caplog.clear()
    home.close()

    assert not home.path.exists()
    assert caplog.records == []
    assert tree_snapshot(outside) == outside_before
    assert _descriptor_count() == descriptors_before


def test_private_credential_home_repair_passes_a_link_in_a_read_only_directory_unfollowed(
    tmp_path: Path, outside: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    home = _credential_home(tmp_path)
    _a_tree_needing_repair(home, outside)
    outside_before = tree_snapshot(outside)
    descriptors_before = _descriptor_count()
    caplog.set_level(logging.ERROR, logger=process.__name__)

    home.close()

    assert not home.path.exists()
    assert tree_snapshot(outside) == outside_before
    assert caplog.records == []
    assert _descriptor_count() == descriptors_before


@pytest.mark.parametrize("flag", ["O_PATH", "O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC"])
def test_process_module_imports_where_os_lacks_a_repair_flag(flag: str) -> None:
    source_root = Path(process.__file__).parents[1]
    subprocess.run(
        [
            sys.executable, "-I", "-c",
            "import os, sys; sys.path.insert(0, sys.argv[1]); delattr(os, sys.argv[2]);"
            " import agent_providers.process",
            str(source_root), flag,
        ],
        check=True,
    )


def _resolved_inside(home_path: Path, descriptor: str | Path) -> Path | None:
    target = Path(os.readlink(descriptor))
    return target if target.is_relative_to(home_path) else None


def _fail_the_repair_at(
    monkeypatch: pytest.MonkeyPatch, home_path: Path, step: str, error: BaseException,
) -> list[set[Path]]:
    is_armed = _arm_after_the_first_removal_pass(monkeypatch, home_path)
    real_open, real_chmod, real_scandir = os.open, os.chmod, os.scandir
    held_at_fault: list[set[Path]] = []

    def is_descriptor_link(path) -> bool:
        return str(path).startswith(str(process._DESCRIPTOR_LINKS))

    def reach(kind: str, directory: Path | None) -> None:
        below_the_home = directory is not None and directory != home_path
        if is_armed() and not held_at_fault and kind == step and below_the_home:
            held_at_fault.append(_held_directories_inside(home_path))
            raise error

    def open_(path, flags, *args, dir_fd=None, **options):
        if dir_fd is not None:
            reach("child", _resolved_inside(home_path, process._DESCRIPTOR_LINKS / str(dir_fd)))
        elif is_descriptor_link(path) and not flags & os.O_PATH:
            reach("listing", _resolved_inside(home_path, path))
        return real_open(path, flags, *args, dir_fd=dir_fd, **options)

    def chmod(path, mode, **options):
        if is_descriptor_link(path):
            reach("chmod", _resolved_inside(home_path, path))
        return real_chmod(path, mode, **options)

    def scandir(path=".", *args, **options):
        if isinstance(path, int):
            reach("scan", _resolved_inside(home_path, process._DESCRIPTOR_LINKS / str(path)))
        return real_scandir(path, *args, **options)

    monkeypatch.setattr(os, "open", open_)
    monkeypatch.setattr(os, "chmod", chmod)
    monkeypatch.setattr(os, "scandir", scandir)
    return held_at_fault


@pytest.mark.parametrize("step", ["chmod", "listing", "scan", "child"])
@pytest.mark.parametrize(
    "error", [OSError(5, "io"), KeyboardInterrupt()], ids=["os_error", "interrupt"],
)
def test_private_credential_home_closes_every_handle_when_the_repair_fails_midway(
    tmp_path: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    step: str,
    error: BaseException,
) -> None:
    home = _credential_home(tmp_path)
    _a_tree_needing_repair(home, outside)
    outside_before = tree_snapshot(outside)
    caplog.set_level(logging.ERROR, logger=process.__name__)
    held_at_fault = _fail_the_repair_at(monkeypatch, home.path, step, error)
    descriptors_before = _descriptor_count()

    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt) as raised:
            home.close()
        assert raised.value is error
    else:
        home.close()
        [record] = caplog.records
        assert record.getMessage() == "private credential home removal failed (OSError)"
        assert home.path.exists()

    assert len(held_at_fault) == 1
    assert len(held_at_fault[0]) >= 2
    assert _descriptor_count() == descriptors_before
    assert tree_snapshot(outside) == outside_before

    monkeypatch.undo()
    home.close()

    assert not home.path.exists()


class _FalseValuedError(Exception):
    def __bool__(self) -> bool:
        return False


class _UntestableError(Exception):
    def __bool__(self) -> bool:
        raise AssertionError("an exception was truth-tested")



class _NoteDroppingError(Exception):
    def add_note(self, note: str) -> None:
        return None


class _NoteRaisingError(Exception):
    def add_note(self, note: str) -> None:
        raise RuntimeError("add_note overridden to raise")


@pytest.mark.parametrize(
    ("traversal_failure", "failing_closes", "later_classes"),
    [
        (RuntimeError, {4: KeyboardInterrupt}, ["KeyboardInterrupt"]),
        (None, {1: KeyboardInterrupt, 2: OSError}, ["OSError"]),
        (None, {4: KeyboardInterrupt, 5: OSError}, ["OSError"]),
        (None, {4: _FalseValuedError, 5: _UntestableError}, ["_UntestableError"]),
        (None, {4: _UntestableError, 5: _FalseValuedError}, ["_FalseValuedError"]),
        (None, {4: _NoteDroppingError, 5: OSError}, ["OSError"]),
        (None, {4: _NoteRaisingError, 5: OSError}, ["OSError"]),
    ],
    ids=[
        "traversal_then_cleanup_close",
        "handle_close_then_root_frame_close",
        "popped_frame_close_then_ancestor_close",
        "false_valued_then_untestable",
        "untestable_then_false_valued",
        "earliest_overrides_add_note_to_drop_it",
        "earliest_overrides_add_note_to_raise",
    ],
)
def test_the_earliest_failure_propagates_and_every_descriptor_is_still_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    traversal_failure: type[BaseException] | None,
    failing_closes: dict[int, type[BaseException]],
    later_classes: list[str],
) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    real_close, real_scandir = os.close, os.scandir
    raised_in_order: list[BaseException] = []
    listings_opened = 0
    closes_seen = 0

    def scandir(path=".", *args, **options):
        nonlocal listings_opened
        listings_opened += 1
        if traversal_failure is not None and listings_opened == 3:
            raised_in_order.append(traversal_failure("listing read failed"))
            raise raised_in_order[-1]
        return real_scandir(path, *args, **options)

    def close(descriptor: int) -> None:
        nonlocal closes_seen
        closes_seen += 1
        real_close(descriptor)
        if closes_seen in failing_closes:
            raised_in_order.append(failing_closes[closes_seen]("close failed"))
            raise raised_in_order[-1]

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "close", close)
    descriptors_before = _descriptor_count()

    with pytest.raises(
        (
            RuntimeError,
            KeyboardInterrupt,
            OSError,
            _FalseValuedError,
            _UntestableError,
            _NoteDroppingError,
            _NoteRaisingError,
        ),
    ) as raised:
        process._make_tree_removable(tmp_path)

    assert raised.value is raised_in_order[0]
    assert [note.rsplit("(", 1)[1].rstrip(")") for note in raised.value.__notes__] == later_classes
    assert _descriptor_count() == descriptors_before


@pytest.mark.parametrize(
    ("step", "child_is_skipped"),
    [("child", True), ("chmod", False), ("listing", False), ("scan", False)],
)
def test_private_credential_home_skips_a_vanished_child_only_at_its_acquisition(
    tmp_path: Path,
    outside: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    step: str,
    child_is_skipped: bool,
) -> None:
    home = _credential_home(tmp_path)
    _a_tree_needing_repair(home, outside)
    if child_is_skipped:
        for already_writable in (home.path / "a" / "b", home.path / "a" / "b" / "c"):
            already_writable.chmod(0o700)
    caplog.set_level(logging.ERROR, logger=process.__name__)
    held_at_fault = _fail_the_repair_at(monkeypatch, home.path, step, FileNotFoundError(2, "gone"))
    descriptors_before = _descriptor_count()

    home.close()

    assert len(held_at_fault) == 1
    assert _descriptor_count() == descriptors_before
    assert home.path.exists() is not child_is_skipped
    assert [record.getMessage() for record in caplog.records] == (
        [] if child_is_skipped
        else ["private credential home removal failed (FileNotFoundError)"]
    )


def test_private_credential_home_with_more_subdirectories_than_descriptors_is_removed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    home = _credential_home(tmp_path)
    for number in range(_WIDE_TREE_BREADTH):
        (home.path / f"sub{number}").mkdir()
    home.path.chmod(0o500)
    descriptors_before = _descriptor_count()
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    caplog.set_level(logging.ERROR, logger=process.__name__)
    lowered_limit = descriptors_before + _WIDE_TREE_BREADTH // 2
    resource.setrlimit(resource.RLIMIT_NOFILE, (lowered_limit, hard_limit))
    try:
        home.close()
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft_limit, hard_limit))

    assert not home.path.exists()
    assert caplog.records == []
    assert _descriptor_count() == descriptors_before


def test_private_credential_home_deeper_than_half_the_descriptor_limit_is_removed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    home = _credential_home(tmp_path)
    descriptors_before = _descriptor_count()
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    lowered_limit = descriptors_before + _DEEP_TREE_DEPTH * 5 // 3
    deepest = home.path
    for level in range(_DEEP_TREE_DEPTH):
        deepest = deepest / "d"
        deepest.mkdir()
    deepest.joinpath("scratch.txt").write_text("scratch")
    for directory in [deepest, *deepest.parents][: _DEEP_TREE_DEPTH + 1]:
        directory.chmod(0o500)
    caplog.set_level(logging.ERROR, logger=process.__name__)
    resource.setrlimit(resource.RLIMIT_NOFILE, (lowered_limit, hard_limit))
    try:
        home.close()
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft_limit, hard_limit))

    assert not home.path.exists()
    assert caplog.records == []
    assert _descriptor_count() == descriptors_before


def test_private_credential_home_logs_a_failed_removal_by_class_and_retries_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    home = _credential_home(tmp_path)
    remove_tree = process.shutil.rmtree

    def refuse_removal(_path) -> None:
        raise PermissionError(13, "denied", "secret-looking-name")

    monkeypatch.setattr(process.shutil, "rmtree", refuse_removal)
    caplog.set_level(logging.ERROR, logger=process.__name__)

    home.close()

    assert home.path.exists()
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert "PermissionError" in record.getMessage()
    assert "secret-looking-name" not in record.getMessage()
    assert str(home.path) not in record.getMessage()

    monkeypatch.setattr(process.shutil, "rmtree", remove_tree)
    home.close()

    assert not home.path.exists()


def test_private_credential_home_stays_open_when_only_a_descendant_vanished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _credential_home(tmp_path)
    remove_tree = process.shutil.rmtree

    def descendant_vanishes(_path) -> None:
        raise FileNotFoundError(2, "gone", "descendant")

    monkeypatch.setattr(process.shutil, "rmtree", descendant_vanishes)

    home.close()

    assert home.path.exists()

    monkeypatch.setattr(process.shutil, "rmtree", remove_tree)
    home.close()

    assert not home.path.exists()


def test_private_credential_home_removes_a_failed_setup(tmp_path: Path) -> None:
    with pytest.raises(CliLoginUnusableError, match="configured CLI credential is missing"):
        PrivateCredentialHome(
            tmp_path / "missing.json",
            Path(".grok/auth.json"),
            prefix="private-home-",
            missing_credential_is_error=True,
        )

    assert not list(tmp_path.glob("private-home-*"))


def test_an_unusable_login_is_an_error_where_a_missing_credential_is(tmp_path: Path) -> None:
    unusable = tmp_path / "unusable.json"
    unusable.write_text("not json")

    with pytest.raises(CliLoginUnusableError):
        PrivateCredentialHome(
            unusable,
            Path("auth.json"),
            prefix="private-home-",
            missing_credential_is_error=True,
            credential_copy=blanked_copy,
        )

    assert not list(tmp_path.glob("private-home-*"))


def test_private_credential_home_forgets_a_failed_setup_it_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[Path] = []
    make_directory = process.tempfile.mkdtemp

    def record_directory(**kwargs) -> str:
        created.append(Path(make_directory(**kwargs)))
        return str(created[-1])

    monkeypatch.setattr(process.tempfile, "mkdtemp", record_directory)
    with pytest.raises(AgentCliUnavailableError) as raised:
        PrivateCredentialHome(
            tmp_path / "missing.json",
            Path(".grok/auth.json"),
            prefix="private-home-",
            missing_credential_is_error=True,
        )
    reused_name = created[0]
    reused_name.mkdir()
    raised.value.__traceback__ = None
    del raised
    gc.collect()

    assert reused_name.exists()


_A_RAW_CREDENTIAL_CANARY = "synthetic-credential-canary"


def _install_a_copy(
    source: Path, destination: Path, *, copy=None, missing_credential_is_error: bool = True,
) -> None:
    process.install_credential_file(
        source,
        destination,
        mode=0o400,
        credential_copy=copy,
        missing_credential_is_error=missing_credential_is_error,
    )


@pytest.mark.parametrize(
    ("missing_credential_is_error", "refused"), ((True, True), (False, False)),
    ids=("an-error", "a-logged-out-probe"),
)
def test_an_unusable_copy_follows_missing_credential_is_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    missing_credential_is_error: bool, refused: bool,
) -> None:
    source = tmp_path / "source.json"
    source.write_text("{}")
    destination = tmp_path / "copy.json"
    caplog.set_level(logging.WARNING, logger=process.__name__)

    def install() -> None:
        _install_a_copy(
            source, destination, copy=lambda _document: None,
            missing_credential_is_error=missing_credential_is_error,
        )

    if refused:
        with pytest.raises(CliLoginUnusableError) as raised:
            install()
        assert str(raised.value) == "configured CLI credential is unusable"
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
    else:
        install()
        assert "is not a usable login" in caplog.text
    assert not destination.exists()


def _a_live_symlink(directory: Path) -> Path:
    (directory / "target").write_text("the target")
    link = directory / "copy.json"
    link.symlink_to(directory / "target")
    return link


def _a_symlink_to_nothing(directory: Path) -> Path:
    link = directory / "copy.json"
    link.symlink_to(directory / "target")
    return link


def _a_regular_file(directory: Path) -> Path:
    path = directory / "copy.json"
    path.write_text("the existing file")
    return path


@pytest.mark.parametrize(
    "plant", (_a_regular_file, _a_live_symlink, _a_symlink_to_nothing),
    ids=("regular-file", "live-symlink", "dangling-symlink"),
)
def test_the_credential_writer_never_replaces_a_file(tmp_path: Path, plant) -> None:
    source = tmp_path / "source.json"
    source.write_text(_A_RAW_CREDENTIAL_CANARY)
    planted = plant(tmp_path)
    kept = planted.read_text() if planted.exists() else None

    with pytest.raises(CliEnvironmentUnavailableError) as raised:
        _install_a_copy(source, planted)

    assert raised.value.cause is CliEnvironmentCause.CREDENTIAL_COPY_NOT_WRITTEN
    assert str(raised.value) == "could not prepare the private CLI credential copy"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert str(planted) not in _exception_chain_text(raised.value)
    assert (planted.read_text() if planted.exists() else None) == kept
    assert not (tmp_path / "target").exists() or (tmp_path / "target").read_text() == "the target"


@pytest.mark.parametrize("fault", DESTINATION_FAULTS)
@pytest.mark.parametrize(
    "inside_a_handler", (False, True), ids=("plain-call", "inside-a-handler"),
)
def test_a_failed_credential_copy_keeps_no_credential_in_its_error(
    tmp_path: Path, fault: str, inside_a_handler: bool,
) -> None:
    source = tmp_path / "source.json"
    source.write_text(_A_RAW_CREDENTIAL_CANARY)
    open_descriptors = len(os.listdir("/proc/self/fd"))

    destination_closes: list[list[int]] = []

    def install_failing() -> CliEnvironmentUnavailableError:
        with a_credential_destination_that_fails(fault) as closes:
            destination_closes.append(closes)
            with pytest.raises(CliEnvironmentUnavailableError) as raised:
                PrivateCredentialHome(
                    source,
                    Path(".claude/.credentials.json"),
                    prefix="private-home-",
                    missing_credential_is_error=True,
                )
        return raised.value

    if inside_a_handler:
        try:
            raise RuntimeError("the caller's own failure")
        except RuntimeError:
            refusal = install_failing()
    else:
        refusal = install_failing()

    assert refusal.cause is CliEnvironmentCause.CREDENTIAL_COPY_NOT_WRITTEN
    assert str(refusal) == "could not prepare the private CLI credential copy"
    assert refusal.__cause__ is None
    assert refusal.__context__ is None
    assert not traceback_frames_hold(refusal, _A_RAW_CREDENTIAL_CANARY)
    assert not list(tmp_path.glob("private-home-*"))
    assert len(os.listdir("/proc/self/fd")) == open_descriptors
    assert [len(closes) for closes in destination_closes] == [1]


@pytest.mark.parametrize("destination_directory", (".claude", ".codex"))
def test_a_destination_directory_that_cannot_be_made_is_a_copy_environment_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination_directory: str,
) -> None:
    source = tmp_path / "source.json"
    source.write_text(_A_RAW_CREDENTIAL_CANARY)
    fail_the_directory_named(monkeypatch, destination_directory, OSError(errno.ENOSPC, "full"))
    open_descriptors = len(os.listdir("/proc/self/fd"))

    with pytest.raises(CliEnvironmentUnavailableError) as raised:
        PrivateCredentialHome(
            source,
            Path(destination_directory) / "credentials.json",
            prefix="private-home-",
            missing_credential_is_error=True,
        )

    assert raised.value.cause is CliEnvironmentCause.CREDENTIAL_COPY_NOT_WRITTEN
    assert str(raised.value) == "could not prepare the private CLI credential copy"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert not list(tmp_path.glob("private-home-*"))
    assert len(os.listdir("/proc/self/fd")) == open_descriptors


def test_an_unreadable_grok_token_mirror_is_a_login_fault_chained_from_the_read_error(
    tmp_path: Path,
) -> None:
    override_provider_runtime(grok_cli_auth_file=tmp_path)

    with pytest.raises(CliLoginUnusableError) as raised:
        process.grok_cli_token_is_present()

    assert isinstance(raised.value.__cause__, OSError)


def test_the_cli_fault_classes_split_login_from_environment() -> None:
    environment = CliEnvironmentUnavailableError(
        "no room", cause=CliEnvironmentCause.CREDENTIAL_COPY_NOT_WRITTEN,
    )
    restored = pickle.loads(pickle.dumps(environment))

    assert issubclass(CliLoginUnusableError, AgentCliUnavailableError)
    assert issubclass(CliEnvironmentUnavailableError, AgentCliUnavailableError)
    assert not issubclass(CliLoginUnusableError, CliEnvironmentUnavailableError)
    assert not issubclass(CliEnvironmentUnavailableError, CliLoginUnusableError)
    assert issubclass(CliProbeBudgetExceeded, AgentCliUnavailableError)
    assert {cause.value for cause in CliEnvironmentCause} == {
        "binary_missing", "home_not_created", "credential_copy_not_written",
    }
    assert (type(restored), str(restored), restored.cause) == (
        CliEnvironmentUnavailableError, "no room", CliEnvironmentCause.CREDENTIAL_COPY_NOT_WRITTEN,
    )


def _remove_the_source(monkeypatch: pytest.MonkeyPatch, source: Path) -> None:
    source.unlink()


@pytest.mark.parametrize(
    ("prepare", "error_class", "detail"),
    (
        (
            fail_the_home_chmod,
            CliEnvironmentUnavailableError,
            "could not prepare the private CLI credential copy",
        ),
        (fail_mkdtemp, CliEnvironmentUnavailableError, "could not create a private CLI home"),
        (
            _remove_the_source,
            CliLoginUnusableError,
            "configured CLI credential is missing",
        ),
    ),
    ids=("home-chmod", "mkdtemp", "missing-credential"),
)
def test_a_private_home_refusal_inside_a_handler_carries_no_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepare,
    error_class: type[Exception],
    detail: str,
) -> None:
    source = tmp_path / "source.json"
    source.write_text(_A_RAW_CREDENTIAL_CANARY)
    prepare(monkeypatch, source)

    try:
        raise RuntimeError("the caller's own failure")
    except RuntimeError:
        with pytest.raises(error_class) as raised:
            PrivateCredentialHome(
                source,
                Path(".claude/.credentials.json"),
                prefix="private-home-",
                missing_credential_is_error=True,
            )

    assert type(raised.value) is error_class
    if issubclass(error_class, CliEnvironmentUnavailableError):
        assert raised.value.cause is CliEnvironmentCause.HOME_NOT_CREATED
    assert str(raised.value) == detail
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert not list(tmp_path.glob("private-home-*"))


_A_NON_ASCII_CREDENTIAL_CANARY: str = "credential-canary-\u00e9"


def test_the_frame_check_reads_credential_views() -> None:
    def hold_in_a_view() -> None:
        payload = _A_NON_ASCII_CREDENTIAL_CANARY.encode()
        view = memoryview(payload)
        del payload
        raise RuntimeError(len(view))

    def hold_in_a_buffer() -> None:
        payload = _A_NON_ASCII_CREDENTIAL_CANARY.encode()
        buffer = bytearray(payload)
        del payload
        raise RuntimeError(len(buffer))

    for hold in (hold_in_a_view, hold_in_a_buffer):
        with pytest.raises(RuntimeError) as raised:
            hold()
        assert traceback_frames_hold(raised.value, _A_NON_ASCII_CREDENTIAL_CANARY)


def test_private_credential_home_reports_a_failed_copy_without_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.json"
    source.write_text(_CATALOG_AUTH_PAYLOAD)
    created: list[Path] = []
    make_directory = process.tempfile.mkdtemp

    def directory_with_a_blocking_file(**kwargs) -> str:
        home = Path(make_directory(**kwargs))
        (home / ".claude").write_text("not a directory")
        created.append(home)
        return str(home)

    monkeypatch.setattr(process.tempfile, "mkdtemp", directory_with_a_blocking_file)

    with pytest.raises(AgentCliUnavailableError) as raised:
        PrivateCredentialHome(
            source,
            Path(".claude/.credentials.json"),
            prefix="private-home-",
            missing_credential_is_error=True,
        )

    assert str(created[0]) not in _exception_chain_text(raised.value)
    assert not created[0].exists()


def _exception_chain_text(error: BaseException | None) -> str:
    texts: list[str] = []
    while error is not None:
        texts.append(f"{error!r} {error}")
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return " ".join(texts)


def test_private_credential_home_treats_an_already_removed_home_as_closed(
    tmp_path: Path,
) -> None:
    home = _credential_home(tmp_path)
    process.shutil.rmtree(home.path)

    home.close()
    home.close()

    assert not home.path.exists()


def test_private_credential_home_keeps_the_setup_error_when_its_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_removal(_path) -> None:
        raise PermissionError(13, "denied", "secret-looking-name")

    monkeypatch.setattr(process.shutil, "rmtree", refuse_removal)

    with pytest.raises(CliLoginUnusableError, match="configured CLI credential is missing"):
        PrivateCredentialHome(
            tmp_path / "missing.json",
            Path(".grok/auth.json"),
            prefix="private-home-",
            missing_credential_is_error=True,
        )


def test_private_credential_home_is_removed_when_collected_unclosed(tmp_path: Path) -> None:
    home = _credential_home(tmp_path)
    home_path = home.path

    del home
    gc.collect()

    assert not home_path.exists()


_ABANDONED_HOME_SCRIPT = """
import sys
from pathlib import Path

from agent_providers.config import ProviderRuntimeConfig, configure
from agent_providers.process import PrivateCredentialHome

configure(ProviderRuntimeConfig.model_validate_json(sys.argv[1]))
home = PrivateCredentialHome(
    Path(sys.argv[2]),
    Path(".claude/.credentials.json"),
    prefix="private-home-",
    missing_credential_is_error=True,
)
child_still_running = home.reserve()
home.close()
print(home.path)
"""


def test_private_credential_home_is_removed_when_its_process_ends_with_cleanup_pending(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.json"
    source.write_text(_CATALOG_AUTH_PAYLOAD)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _ABANDONED_HOME_SCRIPT,
            current_config().model_dump_json(),
            str(source),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert not Path(completed.stdout.strip()).exists()


_REFUSE_REMOVAL_TWICE_PRELUDE = """
import gc
import shutil
import sys
from pathlib import Path

from agent_providers.config import ProviderRuntimeConfig, configure
from agent_providers.process import PrivateCredentialHome

configure(ProviderRuntimeConfig.model_validate_json(sys.argv[1]))
real_rmtree = shutil.rmtree
refusals_left = 2


def rmtree_refusing_the_first_two_attempts(path):
    global refusals_left
    if refusals_left:
        refusals_left -= 1
        raise PermissionError
    real_rmtree(path)


shutil.rmtree = rmtree_refusing_the_first_two_attempts
home = PrivateCredentialHome(
    Path(sys.argv[2]),
    Path(".claude/.credentials.json"),
    prefix="private-home-",
    missing_credential_is_error=True,
)
home_path = home.path
"""
_REFUSED_THEN_ALLOWED_REMOVAL_SCRIPT = _REFUSE_REMOVAL_TWICE_PRELUDE + """
del home
gc.collect()
print(home_path)
print(home_path.exists())
"""
_HOME_STILL_ALIVE_AT_EXIT_SCRIPT = _REFUSE_REMOVAL_TWICE_PRELUDE + """
print(home_path)
"""


def _run_home_script(script: str, tmp_path: Path) -> list[str]:
    source = tmp_path / "source.json"
    source.write_text(_CATALOG_AUTH_PAYLOAD)
    completed = subprocess.run(
        [sys.executable, "-c", script, current_config().model_dump_json(), str(source)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return completed.stdout.split()


def test_private_credential_home_is_removed_at_exit_after_its_finalizer_failed(
    tmp_path: Path,
) -> None:
    home_path, existed_after_finalizer = _run_home_script(
        _REFUSED_THEN_ALLOWED_REMOVAL_SCRIPT, tmp_path,
    )

    assert existed_after_finalizer == "True"
    assert not Path(home_path).exists()


@pytest.mark.parametrize(
    "prelude",
    ["", "import tempfile\n_held = tempfile.TemporaryDirectory()\n"],
    ids=["no finalizer before the import", "a weakref finalizer created before the import"],
)
def test_private_credential_home_still_alive_at_exit_is_removed_after_its_finalizer_failed(
    tmp_path: Path, prelude: str,
) -> None:
    (home_path,) = _run_home_script(prelude + _HOME_STILL_ALIVE_AT_EXIT_SCRIPT, tmp_path)

    assert not Path(home_path).exists()


_FORKED_HOME_SCRIPT = """
import os
import sys
from pathlib import Path

from agent_providers.config import ProviderRuntimeConfig, configure
from agent_providers.process import PrivateCredentialHome

configure(ProviderRuntimeConfig.model_validate_json(sys.argv[1]))
home = PrivateCredentialHome(
    Path(sys.argv[2]),
    Path(".claude/.credentials.json"),
    prefix="private-home-",
    missing_credential_is_error=True,
)
reservation = home.reserve()
child = os.fork()
if child == 0:
    sys.exit(0)
os.waitpid(child, 0)
print(home.path.exists())
reservation.release()
home.close()
print(home.path.exists())
"""


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_private_credential_home_survives_the_exit_of_a_forked_child(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text(_CATALOG_AUTH_PAYLOAD)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _FORKED_HOME_SCRIPT,
            current_config().model_dump_json(),
            str(source),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert completed.stdout.split() == ["True", "False"]


_RESERVE_CODE_OF_A_HOME = (
    PrivateCredentialHome.reserve,
    PrivateCredentialHome._remove_if_unused,
    process._remove_private_tree,
)
_HOME_CODES = {
    "construction": (
        PrivateCredentialHome.__init__,
        process._discard_failed_home,
        process._remove_private_tree,
    ),
    "reserve": _RESERVE_CODE_OF_A_HOME,
    "refused reserve": _RESERVE_CODE_OF_A_HOME,
    "release": (
        process.PrivateHomeReservation.release,
        PrivateCredentialHome._release,
        PrivateCredentialHome._remove_if_unused,
        process._remove_private_tree,
    ),
    "close": (
        PrivateCredentialHome.close,
        PrivateCredentialHome._remove_if_unused,
        process._remove_private_tree,
    ),
    "removal attempt": (
        PrivateCredentialHome._remove_if_unused,
        process._remove_private_tree,
    ),
}


@dataclass(frozen=True)
class _HomeRun:
    operation: Callable[[], object]
    settle_after_an_interruption: Callable[[], None] | None


def _closing_a_home(source: Path) -> _HomeRun:
    home = _build_home(source)
    return _HomeRun(home.close, home.close)


def _closing_a_home_left_to_collection(source: Path) -> _HomeRun:
    return _HomeRun(_build_home(source).close, None)


def _reserving_from_a_home(source: Path) -> _HomeRun:
    home = _build_home(source)
    return _HomeRun(home.reserve, home.close)


def _reserving_from_a_closing_home(source: Path) -> _HomeRun:
    home = _a_closing_home_whose_removal_failed(source)

    def reserve_and_accept_the_refusal() -> None:
        with contextlib.suppress(RuntimeError):
            home.reserve()

    return _HomeRun(reserve_and_accept_the_refusal, home.close)


def _releasing_the_last_reservation(source: Path) -> _HomeRun:
    reservation = _a_closing_home_holding_a_reservation(source)
    return _HomeRun(reservation.release, reservation.release)


_HOME_RUNS = {
    "construction": lambda source: _HomeRun(lambda: _build_home(source), None),
    "reserve": _reserving_from_a_home,
    "refused reserve": _reserving_from_a_closing_home,
    "release": _releasing_the_last_reservation,
    "close": _closing_a_home_left_to_collection,
    "removal attempt": _closing_a_home,
}


def _home_code(operation: str) -> frozenset:
    return frozenset(function.__code__ for function in _HOME_CODES[operation])


def _a_source_login(directory: Path) -> Path:
    source = directory / "source.json"
    source.write_text(_CATALOG_AUTH_PAYLOAD)
    return source


def _build_home(source: Path) -> PrivateCredentialHome:
    return PrivateCredentialHome(
        source,
        Path(".claude/.credentials.json"),
        prefix="private-home-",
        missing_credential_is_error=True,
    )


def _a_closing_home_whose_removal_failed(source: Path) -> PrivateCredentialHome:
    home = _build_home(source)
    with patch.object(process.shutil, "rmtree", side_effect=PermissionError):
        home.close()
    return home


def _a_closing_home_holding_a_reservation(source: Path):
    home = _build_home(source)
    reservation = home.reserve()
    home.close()
    return reservation


def _interrupted_by_a_keyboard_interrupt_at(position: int, codes: frozenset, operation) -> bool:
    tracer_found_at_entry = sys.gettrace()
    gc.disable()
    sys.settrace(_interrupt_at_every_line(position, codes))
    try:
        completed_with = operation()
    except KeyboardInterrupt:
        return True
    finally:
        sys.settrace(tracer_found_at_entry)
        gc.enable()
    del completed_with
    return False


def _interrupt_at_every_line(position: int, codes: frozenset):
    seen_lines = 0

    def on_line(frame, event, argument):
        nonlocal seen_lines
        if event == "line":
            seen_lines += 1
            if seen_lines == position:
                raise KeyboardInterrupt
        return on_line

    def on_call(frame, event, argument):
        return on_line if frame.f_code in codes else None

    return on_call


def _finishes_within_a_second(operation: Callable[[], object]) -> None:
    worker = threading.Thread(target=operation, daemon=True)
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive(), "the interrupted home blocked"


def _a_fresh_home_is_used_and_removed_by_another_thread(source: Path) -> None:
    result: list[Path] = []

    def use_a_fresh_home() -> None:
        home = _build_home(source)
        home.reserve().release()
        home.close()
        result.append(home.path)

    worker = threading.Thread(target=use_a_fresh_home)
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert not result[0].exists()


@pytest.mark.parametrize("operation", _HOME_CODES)
def test_an_interruption_at_any_line_of_a_private_home_never_blocks_the_next(
    tmp_path: Path, operation: str,
) -> None:
    source = _a_source_login(tmp_path)
    gc.collect()
    registered_before = len(process._live_home_finalizers)
    position = 1
    while True:
        run = _HOME_RUNS[operation](source)
        interrupted = _interrupted_by_a_keyboard_interrupt_at(
            position, _home_code(operation), run.operation,
        )
        if not interrupted:
            break
        if run.settle_after_an_interruption is not None:
            _finishes_within_a_second(run.settle_after_an_interruption)
        _a_fresh_home_is_used_and_removed_by_another_thread(source)
        del run
        gc.collect()
        assert not list(tmp_path.glob("private-home-*")), f"line event {position}"
        assert len(process._live_home_finalizers) == registered_before, f"line event {position}"
        position += 1
    assert position > 1


def test_an_interrupted_construction_removes_its_home_once_and_forgets_it(tmp_path: Path) -> None:
    source = _a_source_login(tmp_path)
    gc.collect()
    registered_before = len(process._live_home_finalizers)
    real_remove = process._remove_private_tree
    removals_of_a_gone_tree = 0

    def remove_and_note_a_gone_tree(root: Path) -> bool:
        nonlocal removals_of_a_gone_tree
        removals_of_a_gone_tree += not os.path.lexists(root)
        return real_remove(root)

    position = 1
    with patch.object(process, "_remove_private_tree", remove_and_note_a_gone_tree):
        while _interrupted_by_a_keyboard_interrupt_at(
            position, _code_of(PrivateCredentialHome.__init__), lambda: _build_home(source),
        ):
            gc.collect()
            assert not list(tmp_path.glob("private-home-*")), f"line event {position}"
            assert len(process._live_home_finalizers) == registered_before, f"line event {position}"
            position += 1

    assert position > 1
    assert removals_of_a_gone_tree == 0


class _LedgerOfRemovals:
    """Counts removals and keeps the reservations it handed out that are not yet released."""

    def __init__(self) -> None:
        self.removals = 0
        self.tokens_held_at_a_removal: list[bool] = []
        self._unreleased: set = set()
        real_remove = process._remove_private_tree

        def remove_and_record(root: Path) -> bool:
            self.removals += 1
            self.tokens_held_at_a_removal.append(bool(self._unreleased))
            return real_remove(root)

        self._patch = patch.object(process, "_remove_private_tree", remove_and_record)

    def reserve_from(self, home: PrivateCredentialHome):
        token = home.reserve()
        self._unreleased.add(token)
        return token

    def release(self, token) -> None:
        self._unreleased.discard(token)
        token.release()

    def __enter__(self) -> _LedgerOfRemovals:
        self._patch.start()
        return self

    def __exit__(self, *_exception) -> None:
        self._patch.stop()


class _ThreadedOperation:
    """Runs an operation on a thread, stopping it at its k-th line event inside the given code."""

    _BOUND_SECONDS = 5

    def __init__(self, operation, *, pause_in: frozenset = frozenset(), at_line: int = 0) -> None:
        self._operation = operation
        self._pause_in = pause_in
        self._pause_at_line = at_line
        self._seen_lines = 0
        self._stopped_or_ended = threading.Event()
        self._resume = threading.Event()
        self.paused = False
        self.outcome = None
        self.refusal: RuntimeError | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self._stopped_or_ended.wait(self._BOUND_SECONDS), "the operation hung"

    def finish(self) -> None:
        self._resume.set()
        self._thread.join(self._BOUND_SECONDS)
        assert not self._thread.is_alive()

    def _run(self) -> None:
        tracer_found_at_entry = sys.gettrace()
        sys.settrace(self._on_call)
        try:
            self.outcome = self._operation()
        except RuntimeError as refusal:
            self.refusal = refusal
        finally:
            sys.settrace(tracer_found_at_entry)
            self._stopped_or_ended.set()

    def _on_call(self, frame, event, argument):
        return self._on_line if frame.f_code in self._pause_in else None

    def _on_line(self, frame, event, argument):
        if event == "line":
            self._seen_lines += 1
            if self._seen_lines == self._pause_at_line:
                self.paused = True
                self._stopped_or_ended.set()
                self._resume.wait(self._BOUND_SECONDS)
        return self._on_line


def _code_of(*functions) -> frozenset:
    return frozenset(function.__code__ for function in functions)


_RESERVING_CODE = _code_of(*_HOME_CODES["reserve"])
_CLOSING_CODE = _code_of(*_HOME_CODES["close"])
_RELEASING_CODE = _code_of(*_HOME_CODES["release"])


def _run_interleaved_at_every_line(
    source: Path, *, paused_code: frozenset, operations_of, outcome_holds, build_home=_build_home,
) -> None:
    position = 1
    while True:
        home = build_home(source)
        with _LedgerOfRemovals() as ledger:
            paused_operation, other_operation = operations_of(home, ledger)
            paused = _ThreadedOperation(
                paused_operation, pause_in=paused_code, at_line=position,
            )
            paused.start()
            other = _ThreadedOperation(other_operation)
            other.start()
            paused.finish()
            outcome_holds(home, ledger, paused, other)
        if not paused.paused:
            break
        position += 1
    assert position > 1


def _assert_a_reservation_attempt_kept_or_refused_its_home(home, ledger, attempt) -> None:
    if attempt.refusal is not None:
        assert str(attempt.refusal) == "private credential home is closing"
        assert ledger.removals == 1
    else:
        assert ledger.removals == 0
        assert home.path.exists()
        ledger.release(attempt.outcome)
        assert ledger.removals == 1
    assert not home.path.exists()
    assert ledger.tokens_held_at_a_removal == [False]


@pytest.mark.parametrize(
    ("paused_operation", "build_home"),
    (
        ("reserve", _build_home),
        ("close", _build_home),
        ("reserve", _a_closing_home_whose_removal_failed),
    ),
    ids=("reserve", "close", "reserve-after-a-failed-removal"),
)
def test_reserve_and_close_interleaved_at_every_line_remove_a_home_once(
    tmp_path: Path, paused_operation: str, build_home,
) -> None:
    reserving_is_paused = paused_operation == "reserve"

    def operations_of(home, ledger):
        def reserve():
            return ledger.reserve_from(home)

        return (reserve, home.close) if reserving_is_paused else (home.close, reserve)

    def outcome_holds(home, ledger, paused, other) -> None:
        reserving = paused if reserving_is_paused else other
        _assert_a_reservation_attempt_kept_or_refused_its_home(home, ledger, reserving)

    _run_interleaved_at_every_line(
        _a_source_login(tmp_path),
        paused_code=_RESERVING_CODE if reserving_is_paused else _CLOSING_CODE,
        operations_of=operations_of,
        outcome_holds=outcome_holds,
        build_home=build_home,
    )


@pytest.mark.parametrize(
    ("paused_operation", "closed_before_the_run"),
    (("close", False), ("release", True), ("release", False)),
    ids=("close", "release-of-a-closed-home", "release-while-the-first-close-runs"),
)
def test_close_and_the_last_release_interleaved_at_every_line_remove_a_home_once(
    tmp_path: Path, paused_operation: str, closed_before_the_run: bool,
) -> None:
    closing_is_paused = paused_operation == "close"

    def operations_of(home, ledger):
        held = ledger.reserve_from(home)
        if closed_before_the_run:
            home.close()

        def release() -> None:
            ledger.release(held)

        return (home.close, release) if closing_is_paused else (release, home.close)

    def outcome_holds(home, ledger, _paused, _other) -> None:
        assert ledger.removals == 1
        assert ledger.tokens_held_at_a_removal == [False]
        assert not home.path.exists()

    _run_interleaved_at_every_line(
        _a_source_login(tmp_path),
        paused_code=_CLOSING_CODE if closing_is_paused else _RELEASING_CODE,
        operations_of=operations_of,
        outcome_holds=outcome_holds,
    )


@pytest.mark.parametrize("holding_a_reservation", (False, True))
def test_a_closing_home_refuses_a_new_reservation(
    tmp_path: Path, holding_a_reservation: bool,
) -> None:
    home = _build_home(_a_source_login(tmp_path))
    held = home.reserve() if holding_a_reservation else None
    home.close()

    with pytest.raises(RuntimeError, match="private credential home is closing"):
        home.reserve()

    assert home.path.exists() is holding_a_reservation
    if held is not None:
        held.release()
    assert not home.path.exists()


@pytest.mark.parametrize(
    "callbacks",
    (
        ("release", "release"),
        ("on_spawn_failed", "on_reaped"),
        ("on_reaped", "on_spawn_failed"),
    ),
)
def test_a_reservation_is_released_once(tmp_path: Path, callbacks: tuple[str, str]) -> None:
    home = _build_home(_a_source_login(tmp_path))
    other_reservation = home.reserve()
    reservation = home.reserve()
    home.close()
    with _LedgerOfRemovals() as ledger:
        getattr(reservation, callbacks[0])(*_CALLBACK_ARGUMENTS[callbacks[0]])
        getattr(reservation, callbacks[1])(*_CALLBACK_ARGUMENTS[callbacks[1]])

        assert ledger.removals == 0
        other_reservation.release()

    assert ledger.removals == 1
    assert not home.path.exists()


_CALLBACK_ARGUMENTS = {"release": (), "on_spawn_failed": (), "on_reaped": (1, True)}


def test_the_home_registry_forgets_every_closed_or_collected_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _a_source_login(tmp_path)
    gc.collect()
    registered_before = len(process._live_home_finalizers)
    closed = [_build_home(source) for _ in range(10)]
    for home in closed:
        home.close()
    unclosed = [_build_home(source) for _ in range(10)]
    assert len(process._live_home_finalizers) == registered_before + 10
    del closed, unclosed, home
    gc.collect()
    with pytest.raises(CliLoginUnusableError):
        PrivateCredentialHome(
            tmp_path / "missing.json",
            Path(".claude/.credentials.json"),
            prefix="private-home-",
            missing_credential_is_error=True,
        )
    real_rmtree = process.shutil.rmtree

    def refuse_removal(_path) -> None:
        raise PermissionError

    monkeypatch.setattr(process.shutil, "rmtree", refuse_removal)
    unremovable = _build_home(source)
    unremovable_path = unremovable.path
    unremovable.close()
    del unremovable
    gc.collect()
    monkeypatch.setattr(process.shutil, "rmtree", real_rmtree)

    assert len(process._live_home_finalizers) == registered_before
    awaiting_entry = (unremovable_path, os.getpid())
    assert awaiting_entry in process._homes_awaiting_exit_sweep
    process._homes_awaiting_exit_sweep.discard(awaiting_entry)
    real_rmtree(unremovable_path)
    assert not list(tmp_path.glob("private-home-*"))


_FORK_WHILE_A_HOME_IS_MID_REMOVAL_SCRIPT = """
import os
import signal
import sys
import threading
from pathlib import Path

from agent_providers import process
from agent_providers.config import ProviderRuntimeConfig, configure
from agent_providers.process import PrivateCredentialHome

configure(ProviderRuntimeConfig.model_validate_json(sys.argv[1]))


def a_home():
    return PrivateCredentialHome(
        Path(sys.argv[2]),
        Path(".claude/.credentials.json"),
        prefix="private-home-",
        missing_credential_is_error=True,
    )


removal_started = threading.Event()
release_removal = threading.Event()
parent_home = a_home()
parent_home_path = parent_home.path
remove_tree = process._remove_private_tree


def remove_blocking_for_the_parent_home(root):
    if root == parent_home_path:
        removal_started.set()
        release_removal.wait()
    return remove_tree(root)


process._remove_private_tree = remove_blocking_for_the_parent_home
worker = threading.Thread(target=parent_home.close)
worker.start()
removal_started.wait()
child = os.fork()
if child == 0:
    fresh_home = a_home()
    fresh_home.reserve().release()
    fresh_home.close()
    print(not fresh_home.path.exists(), flush=True)
    process._sweep_failed_homes_at_exit()
    os._exit(0)
failure_guard = threading.Timer(15, os.kill, (child, signal.SIGKILL))
failure_guard.start()
_, status = os.waitpid(child, 0)
failure_guard.cancel()
release_removal.set()
worker.join()
print(os.waitstatus_to_exitcode(status))
"""


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_forked_child_uses_a_fresh_home_while_a_parent_thread_is_mid_removal(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.json"
    source.write_text(_CATALOG_AUTH_PAYLOAD)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _FORK_WHILE_A_HOME_IS_MID_REMOVAL_SCRIPT,
            current_config().model_dump_json(),
            str(source),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert completed.stdout.split() == ["True", "0"]


@pytest.mark.parametrize(("credential_home_variable", "provider"), _CATALOG_PROVIDERS)
def test_catalog_child_sees_only_the_closed_environment_variables(
    tmp_path, monkeypatch, credential_home_variable: str, provider: str,
) -> None:
    original_home, credential = _prepare_catalog_credential(
        tmp_path, monkeypatch, provider=provider,
    )
    monkeypatch.setenv("TMPDIR", str(tmp_path / "operator-tmp"))

    run = _run_credentialed_catalog_cli(
        Path("/usr/bin/env"),
        (),
        credential_home_variable=credential_home_variable,
        auth_file=credential,
    )

    assert run is not None
    assert run.complete is True
    assert run.returncode == 0
    observed = _parse_env_listing(run.stdout)
    assert sorted(observed) == sorted(_expected_closed_names(credential_home_variable))
    assert observed["HOME"] == observed[credential_home_variable]
    assert observed["HOME"] != str(original_home)
    assert not Path(observed["HOME"]).is_relative_to(original_home)
    assert observed["PATH"] == os.environ.get("PATH", os.defpath)
    assert observed["TMPDIR"] != str(tmp_path / "operator-tmp")
    assert Path(observed["TMPDIR"]).is_relative_to(current_config().cli_working_directory_root)
    assert "CATALOG_ENV_LEAK" not in observed
    assert not Path(observed["HOME"]).exists()


@pytest.mark.parametrize(("credential_home_variable", "provider"), _CATALOG_PROVIDERS)
def test_catalog_child_reads_the_host_named_credential_file(
    tmp_path, monkeypatch, credential_home_variable: str, provider: str,
) -> None:
    script = (
        "import os, sys\n"
        "sys.stdout.write("
        "open(os.path.join(os.environ['HOME'], 'auth.json')).read()"
        ")\n"
    )
    run, _original_home, credential = _run_catalog_python(
        tmp_path,
        monkeypatch,
        credential_home_variable=credential_home_variable,
        provider=provider,
        script=script,
    )

    assert run.complete is True
    assert run.returncode == 0
    assert run.stdout == _CATALOG_AUTH_PAYLOAD
    assert credential.read_text() == _CATALOG_AUTH_PAYLOAD


@pytest.mark.parametrize(("credential_home_variable", "provider"), _CATALOG_PROVIDERS)
def test_catalog_child_cannot_write_the_original_credential_directory(
    tmp_path, monkeypatch, credential_home_variable: str, provider: str,
) -> None:
    script = (
        "import os, sys\n"
        "path = os.path.join(os.environ['HOME'], 'auth.json')\n"
        "try:\n"
        "    os.open(path, os.O_WRONLY)\n"
        "    sys.stdout.write('writable')\n"
        "except OSError:\n"
        "    sys.stdout.write('not-writable')\n"
    )
    run, original_home, credential = _run_catalog_python(
        tmp_path,
        monkeypatch,
        credential_home_variable=credential_home_variable,
        provider=provider,
        script=script,
    )

    assert run.complete is True
    assert run.returncode == 0
    assert run.stdout == "not-writable"
    assert credential.read_text() == _CATALOG_AUTH_PAYLOAD
    assert (original_home / "canary").read_text() == "untouched"
    assert set(original_home.iterdir()) == {original_home / "canary"}


def test_claude_catalog_child_never_sees_the_operator_home(tmp_path, monkeypatch) -> None:
    original_home, _credential = _prepare_catalog_credential(
        tmp_path, monkeypatch, provider="claude",
    )

    run = run_claude_catalog_cli(Path("/usr/bin/env"), ())

    assert run is not None
    assert run.complete is True
    assert run.returncode == 0
    observed = _parse_env_listing(run.stdout)
    assert observed["HOME"] != str(original_home)
    assert not Path(observed["HOME"]).is_relative_to(original_home)
    assert observed["PATH"] == os.environ.get("PATH", os.defpath)
    assert "CATALOG_ENV_LEAK" not in observed
    assert "GROK_HOME" not in observed
    assert "CODEX_HOME" not in observed
    assert "CLAUDE_CONFIG_DIR" not in observed
    assert not Path(observed["HOME"]).exists()


def test_claude_catalog_child_writes_stay_in_the_private_home(tmp_path, monkeypatch) -> None:
    original_home, _credential = _prepare_catalog_credential(
        tmp_path, monkeypatch, provider="claude",
    )
    script = (
        "import os, pathlib, sys\n"
        "path = pathlib.Path(os.environ['HOME']) / 'wrote'\n"
        "path.write_text('private')\n"
        "sys.stdout.write(str(path))\n"
    )

    run = run_claude_catalog_cli(Path(sys.executable), ("-c", script))

    assert run is not None
    assert run.complete is True
    assert run.returncode == 0
    written = Path(run.stdout)
    assert not written.is_relative_to(original_home)
    assert not written.exists()
    assert (original_home / "canary").read_text() == "untouched"
    assert set(original_home.iterdir()) == {original_home / "canary"}


def test_the_claude_catalog_child_reads_the_host_named_credential(tmp_path, monkeypatch) -> None:
    _prepare_catalog_credential(tmp_path, monkeypatch, provider="claude")
    script = (
        "import os, pathlib, sys\n"
        "credential = pathlib.Path(os.environ['HOME']) / '.claude' / '.credentials.json'\n"
        "sys.stdout.write(credential.read_text())\n"
    )

    run = run_claude_catalog_cli(Path(sys.executable), ("-c", script))

    assert run is not None
    assert run.returncode == 0
    assert run.stdout == _CATALOG_AUTH_PAYLOAD


def test_a_direct_overwrite_of_the_credential_copy_fails(tmp_path, monkeypatch) -> None:
    """The README hands hosts this sentence; only this much of it is true."""
    _prepare_catalog_credential(tmp_path, monkeypatch, provider="claude")
    script = (
        "import os, pathlib, sys\n"
        "copy = pathlib.Path(os.environ['HOME']) / '.claude' / '.credentials.json'\n"
        "try:\n"
        "    copy.open('w')\n"
        "except PermissionError:\n"
        "    sys.stdout.write('refused')\n"
    )

    run = run_claude_catalog_cli(Path(sys.executable), ("-c", script))

    assert run is not None
    assert run.stdout == "refused"


def test_a_credential_path_that_names_nothing_is_reported_not_swallowed(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    """A typo in the configured path must not read as a logged-out provider."""
    missing = tmp_path / "typo" / "auth.json"
    override_login_source("claude", missing)
    caplog.set_level("WARNING", logger="agent_providers.process")

    run = run_claude_catalog_cli(Path("/bin/sh"), ("-c", ":"))

    assert run is not None
    assert str(missing) in caplog.text


def test_a_bare_binary_name_resolves_only_against_the_named_search_path(
    tmp_path,
    monkeypatch,
) -> None:
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    binary = mounted / "grok"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "codex").write_text("#!/bin/sh\n")
    (elsewhere / "codex").chmod(0o755)
    monkeypatch.setenv("PATH", str(elsewhere))
    override_provider_runtime(cli_binary_search_path=(mounted,))

    assert resolve_cli_binary("grok") == binary
    assert resolve_cli_binary("codex") is None


def test_an_absolute_binary_is_accepted_only_when_it_is_executable(tmp_path) -> None:
    unreadable = tmp_path / "not-executable"
    unreadable.write_text("#!/bin/sh\n")

    assert resolve_cli_binary(str(unreadable)) is None
    assert resolve_cli_binary("/bin/sh") == Path("/bin/sh")


def test_a_private_prompt_file_is_written_below_the_host_named_root(
    tmp_path,
    monkeypatch,
) -> None:
    working_root = tmp_path / "host-root"
    working_root.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "somewhere-else"))
    override_provider_runtime(cli_working_directory_root=working_root)

    outcome = run_cli_bounded(
        shell_child("-c", 'printf %s "$1"', "unused", "placeholder"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
        prompt_file_bytes=b"private prompt",
        prompt_file_arg_index=3,
    )

    assert outcome.complete is True
    assert Path(outcome.stdout).parent == working_root


def test_catalog_spawn_refuses_an_environment_without_home(tmp_path) -> None:
    with pytest.raises(ValueError, match="must set HOME"):
        ChildProcess(
            binary=Path("/usr/bin/env"),
            arguments=(),
            environment={"PATH": "/bin"},
            working_directory=tmp_path,
        )


def test_catalog_spawn_uses_the_given_environment_without_a_host_baseline(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("CATALOG_ENV_LEAK", "should-not-appear")
    home = tmp_path / "private-home"
    home.mkdir()

    run = run_catalog_cli(
        ChildProcess(
            binary=Path("/usr/bin/env"),
            arguments=(),
            environment={"HOME": str(home), "PATH": "/bin"},
            working_directory=home,
        ),
    )

    assert run is not None
    assert run.complete is True
    assert run.returncode == 0
    observed = _parse_env_listing(run.stdout)
    assert observed == {"HOME": str(home), "PATH": "/bin"}


def test_a_child_gets_the_home_it_was_described_with_not_the_hosts(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("HOME", "/inherited/operator-home")

    outcome = run_cli_bounded(
        shell_child("-c", "printf %s \"$HOME\"", home=tmp_path),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
    )

    assert outcome.complete is True
    assert outcome.returncode == 0
    assert outcome.stdout == str(tmp_path)


def test_a_variable_the_recipe_does_not_name_never_reaches_the_child(monkeypatch) -> None:
    monkeypatch.setenv("GROK_HOME", "/outside/profile")

    outcome = run_cli_bounded(
        shell_child("-c", 'test -z "${GROK_HOME+x}"'),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
    )

    assert outcome.complete is True
    assert outcome.returncode == 0


def test_bounded_runner_does_not_pass_secrets_to_the_spawned_cli(monkeypatch) -> None:
    for key in SECRET_ENV_KEYS:
        monkeypatch.setenv(key, "leaked-value")
    secret_checks = " && ".join(f'test -z "${{{key}}}"' for key in SECRET_ENV_KEYS)

    outcome = run_cli_bounded(
        shell_child("-c", secret_checks),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
    )

    assert outcome.complete is True
    assert outcome.returncode == 0


def test_bounded_runner_sends_complete_stdout_lines_and_then_its_outcome() -> None:
    channel = CliLineChannel(maximum_lines=2)

    outcome = run_cli_bounded(
        shell_child("-c", "printf 'one\\ntwo\\n'"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
        stdout_line_channel=channel,
    )

    assert channel.receive(timeout=1) == b"one\n"
    assert channel.receive(timeout=1) == b"two\n"
    assert channel.receive(timeout=0) is outcome


def test_bounded_runner_sends_a_final_stdout_line_without_a_newline() -> None:
    channel = CliLineChannel(maximum_lines=1)

    outcome = run_cli_bounded(
        shell_child("-c", 'printf \'{"type":"end","stopReason":"stop"}\''),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
        stdout_line_channel=channel,
    )

    assert channel.receive(timeout=1) == b'{"type":"end","stopReason":"stop"}'
    assert channel.receive(timeout=0) is outcome


def test_bounded_runner_names_a_full_stdout_line_channel() -> None:
    channel = CliLineChannel(maximum_lines=1)

    outcome = run_cli_bounded(
        shell_child("-c", "printf 'first\\nsecond\\n'"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
        stdout_line_channel=channel,
    )

    assert outcome.reason is CliRunReason.OUTPUT_CHANNEL_FULL
    assert outcome.complete is False
    assert channel.receive(timeout=0) is outcome


def test_bounded_runner_reaps_after_a_line_consumer_requests_cancellation() -> None:
    channel = CliLineChannel(maximum_lines=2)
    outcomes = []

    runner = threading.Thread(
        target=lambda: outcomes.append(run_cli_bounded(
            shell_child("-c", "printf 'first\\n'; exec sleep 10"),
            stdin_payload=None,
            read="all",
            deadline=time.monotonic() + 1,
            stdout_line_channel=channel,
        )),
    )
    runner.start()

    assert channel.receive(timeout=1) == b"first\n"
    channel.request_abort()
    runner.join(timeout=2)

    assert not runner.is_alive()
    assert outcomes[0].reason is CliRunReason.CANCELLED
    assert outcomes[0].complete is False
    assert channel.receive(timeout=1) == outcomes[0]


def test_bounded_runner_uses_and_removes_a_private_prompt_file() -> None:
    observed = []
    real_popen = subprocess.Popen

    def capture_process(command, **kwargs):
        prompt_path = command[4]
        observed.append((prompt_path, os.stat(prompt_path).st_mode & 0o777))
        return real_popen(command, **kwargs)

    with patch("agent_providers.spawn.subprocess.Popen", side_effect=capture_process):
        outcome = run_cli_bounded(
            shell_child("-c", "cat \"$1\"", "unused", "placeholder"),
            stdin_payload=None,
            read="all",
            deadline=time.monotonic() + 1,
            prompt_file_bytes=b"private prompt",
            prompt_file_arg_index=3,
        )

    assert outcome.stdout == "private prompt"
    assert observed[0][1] == 0o600
    assert not os.path.exists(observed[0][0])


def test_bounded_runner_treats_a_missing_prompt_file_as_already_removed(monkeypatch) -> None:
    prompt_paths: list[str] = []
    real_popen = subprocess.Popen
    real_unlink = os.unlink

    def capture_process(command, **kwargs):
        prompt_paths.append(command[4])
        return real_popen(command, **kwargs)

    def report_missing_file(_path: str) -> None:
        raise FileNotFoundError

    try:
        with patch("agent_providers.spawn.subprocess.Popen", side_effect=capture_process):
            monkeypatch.setattr(process.os, "unlink", report_missing_file)
            outcome = run_cli_bounded(
                shell_child("-c", ":", "unused", "placeholder"),
                stdin_payload=None,
                read="all",
                deadline=time.monotonic() + 1,
                prompt_file_bytes=b"private prompt",
                prompt_file_arg_index=3,
            )
    finally:
        for prompt_path in prompt_paths:
            real_unlink(prompt_path)

    assert outcome.complete is True
    assert outcome.reason is CliRunReason.COMPLETE


def test_bounded_runner_publishes_prompt_unlink_errors_to_waiting_consumers(monkeypatch) -> None:
    channel = CliLineChannel(maximum_lines=1)
    prompt_paths: list[str] = []
    received: list[CliRunOutcome] = []
    ready_to_receive = threading.Event()
    real_popen = subprocess.Popen
    real_unlink = os.unlink

    def capture_process(command, **kwargs):
        prompt_paths.append(command[4])
        return real_popen(command, **kwargs)

    def wait_for_outcome() -> None:
        ready_to_receive.set()
        received.append(channel.receive(timeout=1))

    def fail_to_unlink(_path: str) -> None:
        raise OSError("prompt cleanup failed")

    consumer = threading.Thread(target=wait_for_outcome)
    consumer.start()
    assert ready_to_receive.wait(timeout=1)
    try:
        with patch("agent_providers.spawn.subprocess.Popen", side_effect=capture_process):
            monkeypatch.setattr(process.os, "unlink", fail_to_unlink)
            outcome = run_cli_bounded(
                shell_child("-c", ":", "unused", "placeholder"),
                stdin_payload=None,
                read="all",
                deadline=time.monotonic() + 1,
                stdout_line_channel=channel,
                prompt_file_bytes=b"private prompt",
                prompt_file_arg_index=3,
            )
    finally:
        for prompt_path in prompt_paths:
            real_unlink(prompt_path)
    consumer.join(timeout=1)

    assert not consumer.is_alive()
    assert outcome.reason is CliRunReason.IO_ERROR
    assert outcome.complete is False
    assert isinstance(outcome.io_error, OSError)
    assert received == [outcome]


def test_bounded_runner_creates_no_prompt_file_without_prompt_bytes() -> None:
    with patch("agent_providers.process.tempfile.mkstemp") as create_prompt_file:
        outcome = run_cli_bounded(
            shell_child("-c", "printf ready"),
            stdin_payload=None,
            read="all",
            deadline=time.monotonic() + 1,
        )

    assert outcome.stdout == "ready"
    create_prompt_file.assert_not_called()


@pytest.mark.parametrize(
    ("prompt_file_bytes", "prompt_file_arg_index", "message"),
    (
        (None, 0, "requires prompt bytes"),
        (b"private prompt", None, "require a prompt file index"),
        (b"private prompt", -1, "outside the CLI command"),
        (b"private prompt", 3, "outside the CLI command"),
    ),
)
def test_bounded_runner_rejects_an_invalid_private_prompt_contract(
    prompt_file_bytes: bytes | None,
    prompt_file_arg_index: int | None,
    message: str,
) -> None:
    outcome = run_cli_bounded(
        shell_child("-c", ":"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
        prompt_file_bytes=prompt_file_bytes,
        prompt_file_arg_index=prompt_file_arg_index,
    )

    assert outcome.started is False
    assert outcome.reason is CliRunReason.SPAWN_FAILED
    assert isinstance(outcome.spawn_error, ValueError)
    assert message in str(outcome.spawn_error)


def test_bounded_runner_removes_a_private_prompt_file_when_writing_it_fails(
    monkeypatch,
    tmp_path,
) -> None:
    created_paths: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def create_prompt_file(*, prefix: str, dir: Path) -> tuple[int, str]:
        descriptor, path = real_mkstemp(dir=tmp_path, prefix=prefix)
        created_paths.append(path)
        return descriptor, path

    def fail_to_open(descriptor: int, *_args, **_kwargs):
        os.close(descriptor)
        raise OSError("cannot write private prompt")

    monkeypatch.setattr(process.tempfile, "mkstemp", create_prompt_file)
    monkeypatch.setattr(process.os, "fdopen", fail_to_open)

    outcome = run_cli_bounded(
        shell_child("-c", ":", "placeholder"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 1,
        prompt_file_bytes=b"private prompt",
        prompt_file_arg_index=2,
    )

    assert outcome.started is False
    assert outcome.reason is CliRunReason.SPAWN_FAILED
    assert isinstance(outcome.spawn_error, OSError)
    assert created_paths
    assert not os.path.exists(created_paths[0])


def test_cancelling_a_line_channel_discards_unread_output() -> None:
    channel = CliLineChannel(maximum_lines=1)
    outcome = CliRunOutcome(
        started=True,
        spawn_error=None,
        returncode=-15,
        stdout="",
        stderr="",
        complete=False,
        became_zombie=False,
        reason=CliRunReason.CANCELLED,
    )

    assert channel._send(b"discard\n")
    channel.request_abort()
    channel._close(outcome)

    assert channel.receive(timeout=1) == outcome


@pytest.mark.parametrize(
    ("checker", "payload", "detail"),
    [
        ("grok", "not json", GROK_CLI_CREDENTIALS_INVALID_DETAIL),
        ("grok", "[]", GROK_CLI_CREDENTIALS_INVALID_DETAIL),
        ("grok", '{"realm": []}', GROK_CLI_CREDENTIALS_INVALID_DETAIL),
        ("grok", '{"realm": {"key": 3}}', GROK_CLI_CREDENTIALS_INVALID_DETAIL),
        ("codex", "not json", CODEX_CLI_CREDENTIALS_INVALID_DETAIL),
        ("codex", "[]", CODEX_CLI_CREDENTIALS_INVALID_DETAIL),
        ("codex", '{"tokens": []}', CODEX_CLI_CREDENTIALS_INVALID_DETAIL),
        ("codex", '{"tokens": {"access_token": 3}}', CODEX_CLI_CREDENTIALS_INVALID_DETAIL),
        ("codex", '{"tokens": {"account_id": 7}}', CODEX_CLI_CREDENTIALS_INVALID_DETAIL),
        ("codex", '{"tokens": null}', CODEX_CLI_CREDENTIALS_INVALID_DETAIL),
    ],
)
def test_credential_probe_names_malformed_mounted_credentials(
    tmp_path, checker: str, payload: str, detail: str,
) -> None:
    credential_file = tmp_path / f"{checker}.json"
    credential_file.write_text(payload)
    function = (
        process.grok_cli_token_is_present
        if checker == "grok"
        else process.codex_cli_login_is_usable
    )
    field = "grok_cli_auth_file" if checker == "grok" else "codex_cli_auth_file"
    override_provider_runtime(**{field: credential_file})

    with pytest.raises(CliLoginUnusableError, match=detail):
        function()


def test_a_grok_credential_that_is_not_valid_utf8_is_unusable_without_a_chain(tmp_path) -> None:
    credential_file = tmp_path / "grok.json"
    credential_file.write_bytes(b'{"realm": {"key": "\xff\xfe"}}')
    override_provider_runtime(grok_cli_auth_file=credential_file)

    with pytest.raises(CliLoginUnusableError, match=GROK_CLI_CREDENTIALS_INVALID_DETAIL) as refusal:
        process.grok_cli_token_is_present()

    assert refusal.value.__cause__ is None
    assert refusal.value.__context__ is None


def test_a_grok_credential_is_read_as_utf8_whatever_the_locale_default_is(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_file = tmp_path / "grok.json"
    credential_file.write_bytes(b'{"realm": {"key": "caf\xe9"}}')
    override_provider_runtime(grok_cli_auth_file=credential_file)
    read_text = Path.read_text

    def read_with_a_latin1_default(self: Path, encoding: str | None = None, **options):
        return read_text(self, encoding or "latin-1", **options)

    monkeypatch.setattr(Path, "read_text", read_with_a_latin1_default)

    with pytest.raises(CliLoginUnusableError, match=GROK_CLI_CREDENTIALS_INVALID_DETAIL):
        process.grok_cli_token_is_present()


def _a_credential_document_holding(canary: str) -> str:
    return '{"realm": {"key": "' + canary + '"'


@pytest.mark.parametrize("inside_active_handler", [False, True], ids=["plainly", "from-a-handler"])
def test_a_malformed_grok_credential_refusal_holds_no_text_of_the_document(
    tmp_path, inside_active_handler: bool,
) -> None:
    canary = "grok-credential-canary-5d2e"
    credential_file = tmp_path / "grok.json"
    credential_file.write_text(_a_credential_document_holding(canary))
    override_provider_runtime(grok_cli_auth_file=credential_file)

    def refusal() -> CliLoginUnusableError:
        with pytest.raises(CliLoginUnusableError) as raised:
            process.grok_cli_token_is_present()
        return raised.value

    if inside_active_handler:
        try:
            raise RuntimeError("unrelated")
        except RuntimeError:
            held = refusal()
    else:
        held = refusal()

    assert exception_chain(held) == [held]
    assert not traceback_frames_hold(held, canary)
    for link in exception_chain(held):
        assert canary not in repr(vars(link))


_A_COMPLETE_CODEX_LOGIN = json.loads(a_codex_login())


@pytest.mark.parametrize(
    ("document", "usable"),
    [
        pytest.param(None, False, id="missing"),
        pytest.param(_A_COMPLETE_CODEX_LOGIN, True, id="complete"),
        pytest.param({"tokens": {"access_token": "a"}}, False, id="only-an-access-token"),
        pytest.param({"tokens": {}}, False, id="no-access-token"),
        pytest.param(
            {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-synthetic"}, False, id="api-key",
        ),
    ],
)
def test_a_codex_login_is_usable_only_when_a_child_can_start_with_it(
    tmp_path, document: dict | None, usable: bool,
) -> None:
    auth_file = tmp_path / "codex-auth.json"
    if document is not None:
        auth_file.write_text(json.dumps(document))
    override_provider_runtime(codex_cli_auth_file=auth_file)

    assert process.codex_cli_login_is_usable() is usable


@pytest.mark.parametrize(
    "payload",
    (
        None,
        "not json",
        "[]",
        '{"tokens": []}',
        '{"auth_mode": "chatgpt", "tokens": {}}',
        '{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-synthetic"}',
        a_codex_login(tokens={"account_id": 7}).decode(),
    ),
    ids=(
        "missing", "not-json", "not-an-object", "tokens-not-an-object", "no-access-token",
        "api-key-only", "numeric-account-id",
    ),
)
def test_an_unusable_codex_login_makes_the_probe_answer_logged_out(
    tmp_path, monkeypatch, payload: str | None,
) -> None:
    auth_file = tmp_path / "codex-auth.json"
    if payload is not None:
        auth_file.write_text(payload)
    override_provider_runtime(codex_cli_auth_file=auth_file)
    monkeypatch.setattr(process, "resolve_cli_binary", lambda _binary: Path(sys.executable))
    monkeypatch.setattr(
        process,
        "CODEX_CLI_STATUS_ARGS",
        (
            "-c",
            "import os, sys; "
            "sys.stdout.write('Logged in using ChatGPT' "
            "if os.path.exists(os.path.join(os.environ['CODEX_HOME'], 'auth.json')) "
            "else 'Not logged in')",
        ),
    )

    assert codex_cli_login().logged_in is False


def test_the_grok_probe_hands_its_child_the_login_byte_for_byte(tmp_path, monkeypatch) -> None:
    auth_file = tmp_path / "grok-auth.json"
    auth_file.write_bytes(b'{"realm": {"key": "grok-synthetic-key"}}')
    override_provider_runtime(grok_cli_auth_file=auth_file)
    monkeypatch.setattr(process, "resolve_cli_binary", lambda _binary: Path("/usr/bin/grok"))
    copies: list[bytes] = []

    def run_cli_bounded(child, **_kwargs):
        copies.append((Path(child.environment["GROK_HOME"]) / "auth.json").read_bytes())
        return CliRunOutcome(
            started=True, spawn_error=None, returncode=0, stdout=GROK_LOGGED_IN, stderr="",
            complete=True, became_zombie=False, reason=CliRunReason.COMPLETE,
        )

    monkeypatch.setattr(process, "run_cli_bounded", run_cli_bounded)
    clear_agent_cli_caches()

    assert grok_cli_status().login.logged_in is True
    assert copies == [auth_file.read_bytes()]


_NOT_A_PLAIN_FILE = [
    pytest.param(a_fifo, "CLI credential is not a regular file", id="fifo"),
    pytest.param(a_directory, "CLI credential is not a regular file", id="directory"),
    pytest.param(lambda _: Path("/dev/null"), "CLI credential is not a regular file", id="device"),
    pytest.param(a_symlink_to_a_login, "CLI credential is a symbolic link", id="symlink"),
    pytest.param(a_dangling_symlink, "CLI credential is a symbolic link", id="dangling-symlink"),
    pytest.param(
        a_usable_login_over_the_bound, "CLI credentials exceed the copy bound", id="over-the-bound",
    ),
    pytest.param(
        a_path_through_a_regular_file, "could not read CLI credentials", id="enotdir",
    ),
]


@pytest.mark.parametrize(("make_source", "detail"), _NOT_A_PLAIN_FILE)
def test_the_credential_reader_refuses_what_is_not_a_plain_file(
    tmp_path: Path, make_source, detail: str,
) -> None:
    source = make_source(tmp_path)
    open_descriptors = len(os.listdir("/proc/self/fd"))

    with pytest.raises(CliLoginUnusableError) as refusal:
        call_within_deadline(process._read_credential_file, source)

    assert str(refusal.value) == detail
    assert len(os.listdir("/proc/self/fd")) == open_descriptors


def _read_while_close_fails(source: Path) -> CliLoginUnusableError:
    real_close = os.close

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError(errno.EIO, "synthetic close failure")

    with patch.object(process.os, "close", close_then_fail):
        with pytest.raises(CliLoginUnusableError) as refusal:
            process._read_credential_file(source)
    return refusal.value


def _read_while_close_fails_inside_a_handler(source: Path) -> CliLoginUnusableError:
    try:
        raise RuntimeError("an error the caller is already handling")
    except RuntimeError:
        return _read_while_close_fails(source)


@pytest.mark.parametrize(
    "read",
    (_read_while_close_fails, _read_while_close_fails_inside_a_handler),
    ids=("plain-call", "inside-a-handler"),
)
def test_the_credential_reader_discards_the_content_when_the_close_fails(
    tmp_path: Path, read,
) -> None:
    source = tmp_path / "source"
    source.write_text("synthetic-credential-canary")
    open_descriptors = len(os.listdir("/proc/self/fd"))

    refusal = read(source)

    assert str(refusal) == "could not read CLI credentials"
    assert refusal.__context__ is None
    assert refusal.__cause__ is None
    assert not traceback_frames_hold(refusal, "synthetic-credential-canary")
    assert len(os.listdir("/proc/self/fd")) == open_descriptors


def test_the_credential_reader_reads_the_opened_file_not_the_path_after_the_open(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"the opened file")
    real_open = os.open

    def open_then_replace_the_path_with_a_fifo(path, flags, *arguments):
        descriptor = real_open(path, flags, *arguments)
        source.unlink()
        os.mkfifo(source)
        return descriptor

    monkeypatch.setattr(process.os, "open", open_then_replace_the_path_with_a_fifo)

    assert process._read_credential_file(source) == b"the opened file"


@pytest.mark.parametrize(
    "size", (0, CREDENTIAL_FILE_BOUND_BYTES), ids=("empty", "exactly-the-bound"),
)
def test_the_credential_reader_returns_a_plain_file_within_the_bound(
    tmp_path: Path, size: int,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"x" * size)

    assert process._read_credential_file(source) == b"x" * size


def test_the_credential_reader_reports_a_missing_file_as_absent(tmp_path: Path) -> None:
    assert process._read_credential_file(tmp_path / "missing") is None


def _assert_no_link_holds_the_credential(error: BaseException) -> None:
    # Pytest's assertion rewriting would keep the literal in the test's own frame,
    # which the handled exception's traceback reaches.
    assert not traceback_frames_hold(error, "canary-")
    for link in exception_chain(error):
        attributes = f"{link.args} {vars(link)}"
        assert "canary-" not in attributes
    assert error.__cause__ is None


def _assert_nothing_reachable_holds_the_credential(
    error: BaseException, *, handled_by_the_caller: BaseException | None,
) -> None:
    _assert_no_link_holds_the_credential(error)
    assert error.__context__ is handled_by_the_caller
    if handled_by_the_caller is not None:
        callers_frame = handled_by_the_caller.__traceback__.tb_next.tb_frame
        assert callers_frame.f_locals == {"callers_own_state": "intact"}


def _enter_through_the_login_decider(tmp_path: Path) -> bool:
    return process.codex_cli_login_is_usable()


def _enter_through_a_private_credential_home(tmp_path: Path) -> str:
    PrivateCredentialHome(
        current_config().codex_cli_auth_file,
        Path(".codex/auth.json"),
        prefix="private-home-",
        missing_credential_is_error=True,
        credential_copy=blanked_copy,
    )
    return "installed"


def _private_homes_under(root: Path) -> list[Path]:
    prefixes = ("private-home-", CODEX_TOOL_TURN_DIRECTORY_PREFIX)
    return [entry for entry in root.iterdir() if entry.name.startswith(prefixes)]


def _enter_through_a_codex_turn_home(tmp_path: Path) -> str:
    create_codex_turn_home(CODEX_TOOL_TURN_DIRECTORY_PREFIX).home.close()
    return "installed"


_CREDENTIAL_HANDLING_CODE = frozenset(
    function.__code__
    for function in (
        create_codex_turn_home,
        process._read_credential_file,
        process._read_regular_file,
        process._read_within_bound,
        process._install_credential,
        process._write_new_credential_file,
        process._write_credential,
        blanked_copy,
        _blanked_document,
        process._codex_login_in_the_mirror_is_usable,
        assess_codex_login,
    )
)


_LINES_THAT_ACQUIRE_OR_RELEASE_A_DESCRIPTOR = (
    "try:", "os.open(", "os.close(", "_close_descriptor(",
)


def _acquires_or_releases_a_descriptor(frame) -> bool:
    source = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
    return any(marker in source for marker in _LINES_THAT_ACQUIRE_OR_RELEASE_A_DESCRIPTOR)


def _interrupt_at_line(position: int, codes=_CREDENTIAL_HANDLING_CODE):
    seen_lines = 0

    def on_line(frame, event, argument):
        nonlocal seen_lines
        # An interruption delivered before the line that closes a descriptor, or
        # between an open and its try, leaves it open in any Python code.
        if event == "line" and not _acquires_or_releases_a_descriptor(frame):
            seen_lines += 1
            if seen_lines == position:
                raise KeyboardInterrupt
        return on_line

    def on_call(frame, event, argument):
        return on_line if frame.f_code in codes else None

    return on_call


@contextmanager
def _a_read_that_fails_after_one_chunk():
    real_read = os.read
    reads = 0

    def read(descriptor: int, length: int) -> bytes:
        nonlocal reads
        reads += 1
        if reads > 1:
            raise OSError(errno.EIO, "input/output error")
        return real_read(descriptor, 16)

    with patch.object(process.os, "read", read):
        yield


@contextmanager
def _a_stored_login_read_whole():
    yield


_LOGIN_SCENARIOS = {
    "usable": (
        a_codex_login(),
        _a_stored_login_read_whole,
        {
            _enter_through_the_login_decider: True,
            _enter_through_a_private_credential_home: "installed",
            _enter_through_a_codex_turn_home: "installed",
        },
    ),
    "malformed": (
        b'{"token":"canary-access-token",',
        _a_stored_login_read_whole,
        {
            _enter_through_the_login_decider: CliLoginUnusableError,
            _enter_through_a_private_credential_home: CliLoginUnusableError,
            _enter_through_a_codex_turn_home: CliLoginUnusableError,
        },
    ),
    "incomplete": (
        a_codex_login(tokens={"id_token": ""}),
        _a_stored_login_read_whole,
        {
            _enter_through_the_login_decider: False,
            _enter_through_a_private_credential_home: CliLoginUnusableError,
            _enter_through_a_codex_turn_home: CliLoginUnusableError,
        },
    ),
    "read-fails-mid-way": (
        a_codex_login(),
        _a_read_that_fails_after_one_chunk,
        {
            _enter_through_the_login_decider: CliLoginUnusableError,
            _enter_through_a_private_credential_home: CliLoginUnusableError,
            _enter_through_a_codex_turn_home: CliLoginUnusableError,
        },
    ),
}


@pytest.fixture
def a_stored_codex_login(tmp_path: Path) -> None:
    store_codex_login(tmp_path, a_codex_login())


def store_codex_login(tmp_path: Path, document: bytes) -> None:
    auth_file = tmp_path / "codex-auth.json"
    auth_file.write_bytes(document)
    override_provider_runtime(codex_cli_auth_file=auth_file, cli_working_directory_root=tmp_path)


_EVERY_ENTRY_POINT = pytest.mark.parametrize(
    "enter",
    (
        _enter_through_the_login_decider,
        _enter_through_a_private_credential_home,
        _enter_through_a_codex_turn_home,
    ),
    ids=("login-decider", "private-credential-home", "codex-turn-home"),
)


@_EVERY_ENTRY_POINT
@pytest.mark.usefixtures("a_stored_codex_login")
def test_a_decoder_failure_propagates_unchanged_holding_no_credential_bytes(
    tmp_path: Path, enter,
) -> None:
    open_descriptors = len(os.listdir("/proc/self/fd"))

    with patch.object(json.decoder.JSONDecoder, "raw_decode", side_effect=MemoryError):
        with pytest.raises(MemoryError) as raised:
            enter(tmp_path)

    _assert_nothing_reachable_holds_the_credential(raised.value, handled_by_the_caller=None)
    assert len(os.listdir("/proc/self/fd")) == open_descriptors

def _fail_as_the_caller_does() -> None:
    callers_own_state = "intact"
    try:
        raise KeyError("the caller's earlier failure")
    except KeyError as earlier:
        raise RuntimeError(callers_own_state) from earlier


def _drive_in_the_caller(inside_a_handler: bool, call):
    """Call with the caller's handled error (or None); return what the call returned."""
    if not inside_a_handler:
        return call(None)
    try:
        _fail_as_the_caller_does()
    except RuntimeError as handled:
        return call(handled)


_INSIDE_A_HANDLER = pytest.mark.parametrize(
    "inside_a_handler", (False, True), ids=("plain-call", "inside-a-handler"),
)


@_EVERY_ENTRY_POINT
@_INSIDE_A_HANDLER
@pytest.mark.parametrize("scenario", _LOGIN_SCENARIOS)
def test_an_interruption_at_any_line_propagates_holding_no_credential_bytes(
    tmp_path: Path, enter, inside_a_handler: bool, scenario: str,
) -> None:
    document, around_the_read, endings = _LOGIN_SCENARIOS[scenario]
    store_codex_login(tmp_path, document)
    open_descriptors = len(os.listdir("/proc/self/fd"))
    interrupted_lines = 0
    tracer_found_at_entry = sys.gettrace()

    def enter_interrupted_at(position: int, handled: BaseException | None):
        with around_the_read():
            sys.settrace(_interrupt_at_line(position))
            try:
                return enter(tmp_path), None, handled
            except KeyboardInterrupt as interruption:
                return None, interruption, handled
            except Exception as refusal:
                return None, refusal, handled
            finally:
                sys.settrace(tracer_found_at_entry)

    position = 1
    while True:
        ending, failure, handled = _drive_in_the_caller(
            inside_a_handler, lambda handled: enter_interrupted_at(position, handled),
        )
        if failure is not None:
            _assert_no_link_holds_the_credential(failure)
        assert len(os.listdir("/proc/self/fd")) == open_descriptors
        if not isinstance(failure, KeyboardInterrupt):
            break
        _assert_nothing_reachable_holds_the_credential(
            failure, handled_by_the_caller=handled,
        )
        assert not _private_homes_under(tmp_path)
        interrupted_lines += 1
        position += 1

    assert interrupted_lines > 5
    assert sys.gettrace() is tracer_found_at_entry
    expected_ending = endings[enter]
    if isinstance(expected_ending, type):
        assert type(failure) is expected_ending, (ending, failure)
    else:
        assert failure is None
        assert ending == expected_ending


@_EVERY_ENTRY_POINT
@_INSIDE_A_HANDLER
def test_a_memory_error_inside_the_malformed_handler_holds_no_credential_bytes(
    tmp_path: Path, enter, inside_a_handler: bool,
) -> None:
    store_codex_login(tmp_path, _LOGIN_SCENARIOS["malformed"][0])
    open_descriptors = len(os.listdir("/proc/self/fd"))

    def enter_failing(handled: BaseException | None):
        with patch("agent_providers.codex_login.CodexLogin", side_effect=MemoryError):
            with pytest.raises(MemoryError) as raised:
                enter(tmp_path)
        return raised.value, handled

    gc.collect()
    gc.disable()
    try:
        failure, handled = _drive_in_the_caller(inside_a_handler, enter_failing)
        decoder_errors_left = [
            item for item in gc.get_objects() if isinstance(item, json.JSONDecodeError)
        ]
    finally:
        gc.enable()
    assert not decoder_errors_left

    _assert_nothing_reachable_holds_the_credential(failure, handled_by_the_caller=handled)
    assert len(os.listdir("/proc/self/fd")) == open_descriptors


@pytest.mark.usefixtures("a_stored_codex_login")
@pytest.mark.parametrize(
    ("fault", "refusal"),
    (
        (KeyboardInterrupt(), KeyboardInterrupt),
        (OSError(errno.ENOSPC, "synthetic"), CliEnvironmentUnavailableError),
    ),
    ids=("interruption", "oserror"),
)
def test_a_failed_codex_turn_home_layout_removes_the_home_and_leaves_no_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: BaseException,
    refusal: type[BaseException],
) -> None:
    fail_the_work_directory(monkeypatch, fault)

    with pytest.raises(refusal) as raised:
        create_codex_turn_home(CODEX_TOOL_TURN_DIRECTORY_PREFIX)

    assert not _private_homes_under(tmp_path)
    if refusal is CliEnvironmentUnavailableError:
        assert raised.value.cause is CliEnvironmentCause.HOME_NOT_CREATED
        assert str(raised.value) == "could not create a private CLI home"
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None


def _enter_a_private_home_copying_with(credential_copy) -> None:
    PrivateCredentialHome(
        current_config().codex_cli_auth_file,
        Path(".codex/auth.json"),
        prefix="private-home-",
        missing_credential_is_error=True,
        credential_copy=credential_copy,
    )


def _bare_re_raise(source_document: bytes) -> bytes:
    raise


def _re_raise_the_handled_exception(source_document: bytes) -> bytes:
    raise sys.exception()


def _decode_failing_then(source_document: bytes, what_follows) -> bytes:
    handled = sys.exception()
    try:
        json.loads(b"{" + source_document)
    except ValueError as decoder_error:
        return what_follows(handled, decoder_error)
    return source_document


def _re_raise_the_handled_exception_from_the_decoder_error(source_document: bytes) -> bytes:
    def what_follows(handled, decoder_error):
        raise handled from decoder_error

    return _decode_failing_then(source_document, what_follows)


def _re_raise_the_handled_exception_inside_the_decoder_handler(source_document: bytes) -> bytes:
    def what_follows(handled, decoder_error):
        raise handled

    return _decode_failing_then(source_document, what_follows)


def _chain_the_handled_exception_then_raise_another(source_document: bytes) -> bytes:
    def what_follows(handled, decoder_error):
        try:
            raise handled from decoder_error
        except RuntimeError:
            raise ValueError("another failure")

    return _decode_failing_then(source_document, what_follows)


def _chain_the_handled_exception_then_return(source_document: bytes) -> bytes:
    def what_follows(handled, decoder_error):
        try:
            raise handled from decoder_error
        except RuntimeError:
            return source_document

    return _decode_failing_then(source_document, what_follows)


def _raise_another_exception_wearing_the_callers_frames(source_document: bytes) -> bytes:
    handled = sys.exception()
    raise ValueError("different").with_traceback(handled.__traceback__.tb_next)


def _raise_a_group_whose_member_follows_the_decoder_error(source_document: bytes) -> bytes:
    def what_follows(handled, decoder_error):
        try:
            raise RuntimeError("member") from decoder_error
        except RuntimeError as member:
            member.__context__ = decoder_error
            raise ExceptionGroup("group", [member])

    return _decode_failing_then(source_document, what_follows)


def _raise_a_group_whose_member_frame_holds_the_document(source_document: bytes) -> bytes:
    def member_holding_the_document() -> None:
        held_document = source_document
        raise ValueError(len(held_document))

    try:
        member_holding_the_document()
    except ValueError as member:
        raise BaseExceptionGroup("group", [member])
    return source_document


def _fail_holding_the_callers_earlier_state() -> None:
    callers_earlier_state = "intact"
    raise ValueError(callers_earlier_state)


def _the_earlier_failure_caught_by_the_running_caller() -> ValueError:
    frame = sys._getframe()
    while frame.f_code.co_name != "enter_with_the_copy":
        frame = frame.f_back
    return frame.f_locals["earlier_failure"]


def _raise_an_exception_caught_earlier_in_a_running_frame(source_document: bytes) -> bytes:
    raise _the_earlier_failure_caught_by_the_running_caller()


def _raise_a_group_of_that_exception_and_a_member_holding_the_document(
    source_document: bytes,
) -> bytes:
    caught_earlier = _the_earlier_failure_caught_by_the_running_caller()
    try:
        _raise_a_group_whose_member_frame_holds_the_document(source_document)
    except ExceptionGroup as group:
        raise ExceptionGroup("group", [caught_earlier, *group.exceptions])
    return source_document


def _raise_the_earlier_exception_from_the_decoder_error(source_document: bytes) -> bytes:
    def what_follows(handled, decoder_error):
        raise _the_earlier_failure_caught_by_the_running_caller() from decoder_error

    return _decode_failing_then(source_document, what_follows)


def _re_raise_the_handled_exception_from_none(source_document: bytes) -> bytes:
    raise sys.exception() from None


def _traceback_entries(error: BaseException) -> Iterator[types.TracebackType]:
    entry = error.__traceback__
    while entry is not None:
        yield entry
        entry = entry.tb_next


_CREDENTIAL_COPIES = {
    "bare-raise": _bare_re_raise,
    "raise-sys-exception": _re_raise_the_handled_exception,
    "raise-from-the-decoder-error": _re_raise_the_handled_exception_from_the_decoder_error,
    "raise-inside-the-decoder-handler": _re_raise_the_handled_exception_inside_the_decoder_handler,
    "chain-then-raise-another": _chain_the_handled_exception_then_raise_another,
    "chain-then-return": _chain_the_handled_exception_then_return,
    "another-wearing-the-callers-frames": _raise_another_exception_wearing_the_callers_frames,
    "group-member-after-the-decoder-error": _raise_a_group_whose_member_follows_the_decoder_error,
    "group-member-frame-holds-the-document": _raise_a_group_whose_member_frame_holds_the_document,
    "caught-earlier-in-a-running-frame": _raise_an_exception_caught_earlier_in_a_running_frame,
    "group-of-a-running-frame-exception-and-a-member-holding-the-document": (
        _raise_a_group_of_that_exception_and_a_member_holding_the_document
    ),
    "raise-sys-exception-from-none": _re_raise_the_handled_exception_from_none,
    "raise-the-earlier-exception-from-the-decoder-error": (
        _raise_the_earlier_exception_from_the_decoder_error
    ),
}


_COPIES_RAISING_THE_EARLIER_FAILURE = (
    _raise_an_exception_caught_earlier_in_a_running_frame,
    _raise_the_earlier_exception_from_the_decoder_error,
)


def _links_of(error: BaseException) -> tuple:
    return (error.__context__, error.__cause__, error.__suppress_context__)


@pytest.mark.usefixtures("a_stored_codex_login")
@pytest.mark.parametrize("credential_copy", _CREDENTIAL_COPIES.values(), ids=_CREDENTIAL_COPIES)
def test_a_credential_copy_leaves_the_callers_exception_untouched_and_drops_the_credential(
    credential_copy,
) -> None:
    def enter_with_the_copy(handled: BaseException):
        handled.__suppress_context__ = False
        links_at_entry = _links_of(handled)
        traceback_at_entry = handled.__traceback__
        try:
            _fail_holding_the_callers_earlier_state()
        except ValueError as caught_earlier:
            earlier_failure = caught_earlier
        try:
            _enter_a_private_home_copying_with(credential_copy)
        except BaseException as propagated:
            return propagated, handled, links_at_entry, traceback_at_entry, earlier_failure
        return None, handled, links_at_entry, traceback_at_entry, earlier_failure

    gc.collect()
    gc.disable()
    try:
        propagated, handled, links_at_entry, traceback_at_entry, earlier_failure = (
            _drive_in_the_caller(True, enter_with_the_copy)
        )
        decoder_errors_left = [
            item for item in gc.get_objects() if isinstance(item, json.JSONDecodeError)
        ]
    finally:
        gc.enable()

    assert all(now is then for now, then in zip(_links_of(handled), links_at_entry))
    assert any(entry is traceback_at_entry for entry in _traceback_entries(handled))
    assert handled.__context__ is not handled
    assert handled.__cause__ is not None
    assert not decoder_errors_left
    if credential_copy in _COPIES_RAISING_THE_EARLIER_FAILURE:
        assert propagated is earlier_failure
    for error in (handled, propagated):
        if error is not None:
            assert not traceback_frames_hold(error, "canary-")
            for link in exception_chain(error):
                assert "canary-" not in f"{link.args} {vars(link)}"
                assert "payload" not in {
                    name for entry in _traceback_entries(link)
                    for name in entry.tb_frame.f_locals
                }
    callers_frame = list(_traceback_entries(handled))[-1].tb_frame
    assert callers_frame.f_locals == {"callers_own_state": "intact"}
    earlier_state_frame = list(_traceback_entries(earlier_failure))[-1].tb_frame
    assert earlier_state_frame.f_locals == {"callers_earlier_state": "intact"}


@pytest.mark.usefixtures("a_stored_codex_login")
def test_a_credential_copy_failure_chained_from_a_decoder_error_drops_the_cause() -> None:
    def copy_failing_from_a_decoder_error(source_document: bytes) -> bytes:
        try:
            json.loads(b"{" + source_document)
        except ValueError as error:
            raise RuntimeError("copy failed") from error
        return source_document

    gc.collect()
    gc.disable()
    try:
        with pytest.raises(RuntimeError) as raised:
            _enter_a_private_home_copying_with(copy_failing_from_a_decoder_error)
        decoder_errors_left = [
            item for item in gc.get_objects() if isinstance(item, json.JSONDecodeError)
        ]
    finally:
        gc.enable()

    assert raised.value.__cause__ is None
    _assert_no_link_holds_the_credential(raised.value)
    assert not decoder_errors_left

# ── the runner's contract: one terminal outcome, one settle instant ─────────


def _outcome(reason: CliRunReason) -> CliRunOutcome:
    return CliRunOutcome(
        started=True,
        spawn_error=None,
        returncode=None,
        stdout="",
        stderr="",
        complete=reason is CliRunReason.COMPLETE,
        became_zombie=False,
        reason=reason,
    )


def test_a_channel_keeps_the_first_outcome_it_is_closed_with() -> None:
    channel = CliLineChannel(maximum_lines=1)
    first = _outcome(CliRunReason.COMPLETE)

    assert channel._close(first) is first
    assert channel._close(_outcome(CliRunReason.IO_ERROR)) is first
    assert channel.receive(timeout=0) is first


def test_a_closed_channel_refuses_further_lines() -> None:
    channel = CliLineChannel(maximum_lines=2)
    closed_with = _outcome(CliRunReason.IO_ERROR)
    channel._close(closed_with)

    assert channel._send(b"late\n") is False
    assert channel.receive(timeout=0) is closed_with


def test_a_channel_publishes_its_own_overrun_when_the_settle_instant_passes() -> None:
    channel = CliLineChannel(maximum_lines=1)
    settle_by = time.monotonic() + 0.1
    give_up_at = settle_by + 1
    while True:
        assert time.monotonic() < give_up_at, "the channel never published its overrun"
        try:
            received = channel.receive(timeout=0.05, settle_by=settle_by)
        except TimeoutError:
            continue
        break
    received_at = time.monotonic()

    assert isinstance(received, CliRunOutcome)
    assert received.reason is CliRunReason.CLEANUP_OVERRAN
    assert settle_by <= received_at < settle_by + 0.5
    assert channel._close(_outcome(CliRunReason.COMPLETE)) is received


def test_a_channel_without_a_settle_instant_still_times_out() -> None:
    channel = CliLineChannel(maximum_lines=1)

    with pytest.raises(TimeoutError):
        channel.receive(timeout=0.01)


@pytest.fixture
def failure_watch(monkeypatch, caplog, capfd) -> RunnerFailureWatch:
    return RunnerFailureWatch(monkeypatch, caplog, capfd)


def test_the_failure_watch_hands_a_foreign_thread_error_on_to_the_previous_hook(
    monkeypatch, caplog, capfd,
) -> None:
    forwarded: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", forwarded.append)
    release = threading.Event()

    def fail_when_released() -> None:
        release.wait(timeout=5)
        raise RuntimeError(A_RUNNER_FAILURE_CANARY)

    leaked_thread = threading.Thread(target=fail_when_released)
    leaked_thread.start()
    watch = RunnerFailureWatch(monkeypatch, caplog, capfd)

    release.set()
    leaked_thread.join(timeout=5)

    assert [error.thread for error in forwarded] == [leaked_thread]
    watch.assert_nothing_leaked()


@pytest.fixture
def pooled_run() -> tuple[CodexProcessPool, dict]:
    process_pool = CodexProcessPool(maximum_processes=2, maximum_image_runs=1)
    reservation = process_pool.reserve(CodexProcessKind.IMAGE)
    return process_pool, pool_callbacks(process_pool, reservation)


def _raising(error: BaseException):
    def raise_it(*_arguments, **_keywords):
        raise error

    return raise_it


def _run_pooled(command: str, callbacks: dict, **options) -> CliRunOutcome:
    options.setdefault("deadline", time.monotonic() + 2)
    return run_cli_bounded(
        shell_child("-c", command),
        stdin_payload=None,
        read="all",
        **callbacks,
        **options,
    )


@pytest.mark.parametrize("error_type", [OSError, BaseException])
def test_a_spawn_failure_of_any_class_is_published_and_frees_the_reservation(
    monkeypatch, failure_watch, pooled_run, error_type,
) -> None:
    process_pool, callbacks = pooled_run
    monkeypatch.setattr(process, "open_pipes", _raising(error_type(A_RUNNER_FAILURE_CANARY)))

    outcome = _run_pooled(":", callbacks)

    assert outcome.reason is CliRunReason.SPAWN_FAILED
    assert process_pool.reservation_count() == 0
    assert failure_watch.reported_steps() == []
    failure_watch.assert_nothing_leaked()


def test_a_failing_spawn_failure_callback_is_reported_and_keeps_the_reservation(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    spawn_error = OSError("spawn refused")
    monkeypatch.setattr(process, "open_pipes", _raising(spawn_error))
    callbacks["on_spawn_failed"] = _raising(RuntimeError(ANOTHER_RUNNER_FAILURE_CANARY))

    outcome = _run_pooled(":", callbacks)

    assert outcome.reason is CliRunReason.SPAWN_FAILED
    assert outcome.spawn_error is spawn_error
    assert spawn_error.__context__ is None
    assert failure_watch.reported_steps() == ["on_spawn_failed RuntimeError"]
    assert process_pool.reservation_count() == 1
    failure_watch.assert_nothing_leaked()


def test_a_failing_spawn_callback_still_reaps_the_child_and_reports_the_step(
    failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    spawned: list[int] = []
    bind = callbacks["on_spawned"]

    def bind_then_fail(process_id: int) -> None:
        spawned.append(process_id)
        bind(process_id)
        raise RuntimeError(A_RUNNER_FAILURE_CANARY)

    callbacks["on_spawned"] = bind_then_fail

    outcome = _run_pooled("exec sleep 10", callbacks)

    assert outcome.reason is CliRunReason.IO_ERROR
    assert outcome.started is True
    assert failure_watch.reported_steps() == ["on_spawned RuntimeError"]
    assert process_pool.reservation_count() == 0
    with pytest.raises(ProcessLookupError):
        os.killpg(spawned[0], 0)
    failure_watch.assert_nothing_leaked()


def test_a_failing_exchange_is_published_as_an_io_error_and_reaped(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    monkeypatch.setattr(process, "_exchange_bounded", _raising(ValueError(A_RUNNER_FAILURE_CANARY)))

    outcome = _run_pooled("exec sleep 10", callbacks)

    assert outcome.reason is CliRunReason.IO_ERROR
    assert failure_watch.reported_steps() == ["exchange ValueError"]
    assert process_pool.reservation_count() == 0
    failure_watch.assert_nothing_leaked()


def test_a_reap_that_raises_is_handed_off_with_a_sigkill_and_a_background_reap(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    held_at_release: list[int] = []
    reap = callbacks["on_reaped"]

    def count_then_reap(process_id: int, became_zombie: bool) -> None:
        held_at_release.append(process_pool.reservation_count())
        reap(process_id, became_zombie)

    callbacks["on_reaped"] = count_then_reap
    handed_off: list[subprocess.Popen[bytes]] = []
    background_reap_ended = threading.Event()
    real_background_reap = process._reap_in_background

    def capture_background_reap(child, on_reaped) -> None:
        real_background_reap(child, on_reaped)
        handed_off.append(child)
        background_reap_ended.set()

    class StdinWhoseCloseFails:
        def __init__(self, stdin) -> None:
            self._stdin = stdin

        @property
        def closed(self) -> bool:
            return self._stdin.closed

        def close(self) -> None:
            self._stdin.close()
            raise OSError(ANOTHER_RUNNER_FAILURE_CANARY)

    def fail_the_reap_and_the_stdin_close(child) -> bool:
        child.stdin = StdinWhoseCloseFails(child.stdin)
        raise PermissionError(A_RUNNER_FAILURE_CANARY)

    monkeypatch.setattr(process, "_reap_process_group", fail_the_reap_and_the_stdin_close)
    monkeypatch.setattr(process, "_reap_in_background", capture_background_reap)
    deadline = time.monotonic() + 0.05

    outcome = _run_pooled("exec sleep 10", callbacks, deadline=deadline)
    returned_at = time.monotonic()

    assert outcome.became_zombie is True
    assert outcome.reason in process._DEADLINE_REASONS
    assert returned_at <= cli_settle_deadline(deadline)
    assert background_reap_ended.wait(timeout=2)
    assert handed_off[0].returncode == -9
    handed_off_streams = (handed_off[0].stdin, handed_off[0].stdout, handed_off[0].stderr)
    assert all(stream.closed for stream in handed_off_streams)
    assert held_at_release == [1]
    assert process_pool.reservation_count() == 0
    assert failure_watch.reported_steps() == ["reap PermissionError", "hand-off close OSError"]
    failure_watch.assert_nothing_leaked()


def test_a_failing_release_callback_is_reported_once_and_not_retried(
    failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    spawned: list[int] = []
    bind = callbacks["on_spawned"]
    callbacks["on_spawned"] = lambda process_id: (spawned.append(process_id), bind(process_id))
    callbacks["on_reaped"] = lambda process_id, _zombie: process_pool.reap(
        next(iter(process_pool._reservations)), process_id + 1,
    )

    outcome = _run_pooled(":", callbacks)

    assert outcome.reason is CliRunReason.COMPLETE
    assert failure_watch.reported_steps() == ["on_reaped RuntimeError"]
    assert process_pool.reservation_count() == 1
    assert next(iter(process_pool._reservations)).pid == spawned[0]
    failure_watch.assert_nothing_leaked()


def test_a_failing_prompt_file_cleanup_is_published_as_an_io_error(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    prompt_paths: list[str] = []
    real_unlink = process._unlink_prompt_file

    def fail_to_unlink(path: str) -> None:
        prompt_paths.append(path)
        raise RuntimeError(A_RUNNER_FAILURE_CANARY)

    monkeypatch.setattr(process, "_unlink_prompt_file", fail_to_unlink)

    try:
        outcome = run_cli_bounded(
            shell_child("-c", ":", "unused", "placeholder"),
            stdin_payload=None,
            read="all",
            deadline=time.monotonic() + 2,
            prompt_file_bytes=b"private prompt",
            prompt_file_arg_index=3,
            **callbacks,
        )
    finally:
        for prompt_path in prompt_paths:
            real_unlink(prompt_path)

    assert outcome.reason is CliRunReason.IO_ERROR
    assert outcome.io_error is None
    assert failure_watch.reported_steps() == ["prompt_file RuntimeError"]
    assert process_pool.reservation_count() == 0
    failure_watch.assert_nothing_leaked()


def test_a_channel_that_cannot_close_does_not_delay_the_returned_outcome(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    channel = CliLineChannel(maximum_lines=1)
    monkeypatch.setattr(CliLineChannel, "_close", _raising(RuntimeError(A_RUNNER_FAILURE_CANARY)))
    started_at = time.monotonic()

    outcome = _run_pooled(":", callbacks, stdout_line_channel=channel)

    assert outcome.reason is CliRunReason.COMPLETE
    assert time.monotonic() - started_at < 1
    assert failure_watch.reported_steps() == ["close RuntimeError"]
    assert process_pool.reservation_count() == 0
    with pytest.raises(TimeoutError):
        channel.receive(timeout=0)
    failure_watch.assert_nothing_leaked()


def test_a_report_made_while_the_log_handler_fails_never_prints_the_failure_text(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    handler = FailingLogHandler()
    process_logger = logging.getLogger("agent_providers.process")
    process_logger.addHandler(handler)
    monkeypatch.setattr(process, "_exchange_bounded", _raising(ValueError(A_RUNNER_FAILURE_CANARY)))
    try:
        outcome = _run_pooled("exec sleep 10", callbacks)
    finally:
        process_logger.removeHandler(handler)

    assert outcome.reason is CliRunReason.IO_ERROR
    assert failure_watch.reported_steps() == ["exchange ValueError"]
    assert len(handler.handled_records) == 1
    assert process_pool.reservation_count() == 0
    failure_watch.assert_nothing_leaked()


def test_a_log_handler_that_raises_does_not_strand_the_hand_off_or_the_background_reap(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    released = threading.Event()
    release_reservation = callbacks["on_reaped"]

    def release_and_signal(process_id: int, became_zombie: bool) -> None:
        release_reservation(process_id, became_zombie)
        released.set()

    callbacks["on_reaped"] = release_and_signal

    handler = RaisingLogHandler(then=OSError)
    process_logger = logging.getLogger("agent_providers.process")
    process_logger.addHandler(handler)
    monkeypatch.setattr(process, "_reap_process_group", _raising(
        PermissionError(A_RUNNER_FAILURE_CANARY),
    ))
    try:
        outcome = _run_pooled(":", callbacks)
        assert released.wait(timeout=5)
    finally:
        process_logger.removeHandler(handler)

    assert outcome.reason is CliRunReason.COMPLETE
    assert outcome.became_zombie is True
    assert process_pool.reservation_count() == 0
    failure_watch.assert_nothing_leaked()


def test_a_cleanup_that_outlasts_the_settle_instant_is_published_by_the_channel_itself(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    monkeypatch.setattr("agent_providers.process.CLI_TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr("agent_providers.process.CLI_PROBE_CALLER_TIMEOUT_MARGIN_SECONDS", 0.01)
    channel = CliLineChannel(maximum_lines=1)
    release_reap = threading.Event()
    released = threading.Event()
    reaped_calls: list[tuple[int, bool]] = []
    real_reap = process._reap_process_group
    release_reservation = callbacks["on_reaped"]

    def delayed_reap(child) -> bool:
        release_reap.wait(timeout=2)
        return real_reap(child)

    def record_release(process_id: int, became_zombie: bool) -> None:
        reaped_calls.append((process_id, became_zombie))
        release_reservation(process_id, became_zombie)
        released.set()

    callbacks["on_reaped"] = record_release
    monkeypatch.setattr(process, "_reap_process_group", delayed_reap)
    deadline = time.monotonic() + 0.05
    consumed: list[bytes | CliRunOutcome] = []
    consumer = threading.Thread(
        target=lambda: consumed.append(channel.receive(settle_by=cli_settle_deadline(deadline))),
    )
    consumer.start()

    outcome = _run_pooled(
        "exec sleep 10", callbacks, deadline=deadline, stdout_line_channel=channel,
    )
    returned_at = time.monotonic()
    consumer.join(timeout=1)

    assert outcome.reason is CliRunReason.CLEANUP_OVERRAN
    assert cli_settle_deadline(deadline) <= returned_at < cli_settle_deadline(deadline) + 0.5
    assert consumed[0] is outcome
    assert channel.abort_requested()
    assert process_pool.reservation_count() == 1
    release_reap.set()
    assert released.wait(timeout=2)
    assert len(reaped_calls) == 1
    assert process_pool.reservation_count() == 0
    assert channel.receive(timeout=0) is outcome
    failure_watch.assert_nothing_leaked()


def test_the_returned_outcome_is_the_overrun_a_consumer_already_published(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    monkeypatch.setattr("agent_providers.process.CLI_TERMINATION_GRACE_SECONDS", 0.2)
    channel = CliLineChannel(maximum_lines=1)
    release_reap = threading.Event()
    real_reap = process._reap_process_group
    monkeypatch.setattr(
        process,
        "_reap_process_group",
        lambda child: (release_reap.wait(timeout=2), real_reap(child))[1],
    )
    deadline = time.monotonic() + 0.05
    consumed: list[bytes | CliRunOutcome] = []
    consumer = threading.Thread(
        target=lambda: consumed.append(channel.receive(settle_by=deadline + 0.01)),
    )
    consumer.start()

    outcome = _run_pooled(
        "exec sleep 10", callbacks, deadline=deadline, stdout_line_channel=channel,
    )
    release_reap.set()
    consumer.join(timeout=1)

    assert outcome is consumed[0]
    assert channel.abort_requested()
    assert channel.receive(timeout=0) is outcome
    failure_watch.assert_nothing_leaked()


def test_a_spawn_that_returns_after_the_deadline_is_published_and_reaped_late(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    channel = CliLineChannel(maximum_lines=1)
    release_spawn = threading.Event()
    reaped = threading.Event()
    spawned: list[int] = []
    release_reservation = callbacks["on_reaped"]
    bind = callbacks["on_spawned"]
    real_open_pipes = process.open_pipes

    def late_open_pipes(*arguments, **keywords):
        release_spawn.wait(timeout=2)
        return real_open_pipes(*arguments, **keywords)

    def record_spawn(process_id: int) -> None:
        spawned.append(process_id)
        bind(process_id)

    def record_release(process_id: int, became_zombie: bool) -> None:
        release_reservation(process_id, became_zombie)
        reaped.set()

    callbacks["on_spawned"] = record_spawn
    callbacks["on_reaped"] = record_release
    monkeypatch.setattr(process, "open_pipes", late_open_pipes)
    deadline = time.monotonic() + 0.05

    outcome = _run_pooled("true", callbacks, deadline=deadline, stdout_line_channel=channel)
    returned_at = time.monotonic()

    assert outcome.reason is CliRunReason.DEADLINE_BEFORE_SPAWN
    assert deadline <= returned_at < deadline + 0.5
    assert channel.receive(timeout=0) is outcome
    assert channel.abort_requested()
    assert process_pool.reservation_count() == 1
    release_spawn.set()
    assert reaped.wait(timeout=2)
    assert len(spawned) == 1
    assert process_pool.reservation_count() == 0
    assert channel.receive(timeout=0) is outcome
    failure_watch.assert_nothing_leaked()


def test_a_runner_thread_that_cannot_start_publishes_a_spawn_failure(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    start_error = RuntimeError(A_RUNNER_FAILURE_CANARY)
    fail_to_start_the_runner_thread(monkeypatch, start_error)
    channel = CliLineChannel(maximum_lines=1)

    outcome = _run_pooled(":", callbacks, stdout_line_channel=channel)

    assert outcome.reason is CliRunReason.SPAWN_FAILED
    assert outcome.spawn_error is start_error
    assert channel.receive(timeout=0) is outcome
    assert process_pool.reservation_count() == 0
    assert failure_watch.reported_steps() == ["start RuntimeError"]
    failure_watch.assert_nothing_leaked()


def test_a_runner_thread_stopped_by_system_exit_publishes_before_the_exit_propagates(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    fail_to_start_the_runner_thread(monkeypatch, SystemExit(A_RUNNER_FAILURE_CANARY))
    channel = CliLineChannel(maximum_lines=1)

    with pytest.raises(SystemExit):
        _run_pooled(":", callbacks, stdout_line_channel=channel)

    published = channel.receive(timeout=0)
    assert isinstance(published, CliRunOutcome)
    assert published.reason is CliRunReason.SPAWN_FAILED
    assert process_pool.reservation_count() == 0
    assert failure_watch.reported_steps() == ["start SystemExit"]
    failure_watch.assert_nothing_leaked()


def test_control_flow_raised_while_settling_an_unstarted_run_propagates_after_publishing(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    fail_to_start_the_runner_thread(monkeypatch, RuntimeError(A_RUNNER_FAILURE_CANARY))
    callbacks["on_spawn_failed"] = _raising(KeyboardInterrupt())
    channel = CliLineChannel(maximum_lines=1)

    with pytest.raises(KeyboardInterrupt) as raised:
        _run_pooled(":", callbacks, stdout_line_channel=channel)

    assert raised.value.__context__ is None
    published = channel.receive(timeout=0)
    assert isinstance(published, CliRunOutcome)
    assert published.reason is CliRunReason.SPAWN_FAILED
    assert failure_watch.reported_steps() == [
        "start RuntimeError", "on_spawn_failed KeyboardInterrupt",
    ]
    failure_watch.assert_nothing_leaked()


def _settle_in_order(monkeypatch, *, runner_thread_first: bool):
    """Hold the two settling threads so that one provably settles before the other."""
    caller = threading.current_thread()
    real_settle = process._settle
    real_reap = process._reap_process_group
    caller_arrived = threading.Event()
    caller_settled = threading.Event()
    runner_settled = threading.Event()
    returned: dict[str, CliRunOutcome] = {}

    def ordered_settle(state, channel, outcome, failures):
        is_caller = threading.current_thread() is caller
        if is_caller:
            caller_arrived.set()
            if runner_thread_first:
                assert runner_settled.wait(timeout=2)
        elif not runner_thread_first:
            assert caller_settled.wait(timeout=2)
        settled = real_settle(state, channel, outcome, failures)
        returned["caller" if is_caller else "runner"] = settled
        (caller_settled if is_caller else runner_settled).set()
        return settled

    def reap_once_the_caller_gave_up(child) -> bool:
        if runner_thread_first:
            assert caller_arrived.wait(timeout=2)
        return real_reap(child)

    monkeypatch.setattr(process, "_settle", ordered_settle)
    monkeypatch.setattr(process, "_reap_process_group", reap_once_the_caller_gave_up)
    return returned, runner_settled


@pytest.mark.parametrize(
    ("runner_thread_first", "expected_reason"),
    [(True, CliRunReason.COMPLETE), (False, CliRunReason.CLEANUP_OVERRAN)],
    ids=["the-runner-thread-settles-first", "the-caller-settles-first"],
)
def test_whichever_thread_settles_first_decides_the_return_and_the_channel(
    monkeypatch, failure_watch, pooled_run, runner_thread_first, expected_reason,
) -> None:
    process_pool, callbacks = pooled_run
    monkeypatch.setattr("agent_providers.process.CLI_TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr("agent_providers.process.CLI_PROBE_CALLER_TIMEOUT_MARGIN_SECONDS", 0.01)
    returned, runner_settled = _settle_in_order(
        monkeypatch, runner_thread_first=runner_thread_first,
    )
    channel = CliLineChannel(maximum_lines=1)

    outcome = _run_pooled(
        "printf 'hello\\n'",
        callbacks,
        deadline=time.monotonic() + 0.05,
        stdout_line_channel=channel,
    )
    assert runner_settled.wait(timeout=2)

    assert outcome.reason is expected_reason
    assert returned["caller"] is outcome
    assert returned["runner"] is outcome
    if runner_thread_first:
        assert outcome.stdout == "hello\n"
        assert channel.receive(timeout=0) == b"hello\n"
        assert not channel.abort_requested()
    assert channel.receive(timeout=0) is outcome
    assert process_pool.reservation_count() == 0
    failure_watch.assert_nothing_leaked()


@pytest.mark.parametrize(
    "spawn_held_past_the_deadline",
    [
        pytest.param(True, id="caller-settles-after-the-deadline"),
        pytest.param(False, id="runner-thread-completes-first"),
    ],
)
def test_a_caller_that_adopts_a_consumers_overrun_still_requests_abort(
    monkeypatch, failure_watch, pooled_run, spawn_held_past_the_deadline,
) -> None:
    process_pool, callbacks = pooled_run
    channel = CliLineChannel(maximum_lines=1)
    release_spawn = threading.Event()
    reaped = threading.Event()
    release_reservation = callbacks["on_reaped"]
    real_open_pipes = process.open_pipes

    def late_open_pipes(*arguments, **keywords):
        if spawn_held_past_the_deadline:
            release_spawn.wait(timeout=2)
        return real_open_pipes(*arguments, **keywords)

    def record_release(process_id: int, became_zombie: bool) -> None:
        release_reservation(process_id, became_zombie)
        reaped.set()

    callbacks["on_reaped"] = record_release
    monkeypatch.setattr(process, "open_pipes", late_open_pipes)
    consumed = channel.receive(settle_by=time.monotonic())
    deadline = time.monotonic() + (0.05 if spawn_held_past_the_deadline else 5)

    outcome = _run_pooled("true", callbacks, deadline=deadline, stdout_line_channel=channel)

    assert outcome.reason is CliRunReason.CLEANUP_OVERRAN
    assert outcome is consumed
    assert channel.abort_requested()
    release_spawn.set()
    assert reaped.wait(timeout=2)
    failure_watch.assert_nothing_leaked()


def test_control_flow_raised_by_the_channel_close_on_the_callers_thread_propagates_after_the_abort(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    monkeypatch.setattr("agent_providers.process.CLI_TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr("agent_providers.process.CLI_PROBE_CALLER_TIMEOUT_MARGIN_SECONDS", 0.01)
    channel = CliLineChannel(maximum_lines=1)
    caller = threading.current_thread()
    release_reap = threading.Event()
    released = threading.Event()
    real_reap = process._reap_process_group
    real_close = CliLineChannel._close
    release_reservation = callbacks["on_reaped"]

    def delayed_reap(child) -> bool:
        release_reap.wait(timeout=2)
        return real_reap(child)

    def close_that_exits_on_the_callers_thread(self, outcome):
        published = real_close(self, outcome)
        if threading.current_thread() is caller:
            raise SystemExit(A_RUNNER_FAILURE_CANARY)
        return published

    def record_release(process_id: int, became_zombie: bool) -> None:
        release_reservation(process_id, became_zombie)
        released.set()

    callbacks["on_reaped"] = record_release
    monkeypatch.setattr(process, "_reap_process_group", delayed_reap)
    monkeypatch.setattr(CliLineChannel, "_close", close_that_exits_on_the_callers_thread)

    deadline = time.monotonic() + 0.05

    with pytest.raises(SystemExit):
        _run_pooled("exec sleep 10", callbacks, deadline=deadline, stdout_line_channel=channel)

    assert channel.abort_requested()
    assert failure_watch.reported_steps() == ["close SystemExit"]
    release_reap.set()
    assert released.wait(timeout=2)
    failure_watch.assert_nothing_leaked()


@pytest.mark.parametrize(
    "raised_by_records",
    [
        pytest.param([SystemExit, RuntimeError], id="control-flow-first"),
        pytest.param([RuntimeError, SystemExit], id="exception-first"),
        pytest.param([SystemExit, KeyboardInterrupt], id="first-control-flow-wins"),
    ],
)
def test_a_later_failing_report_never_displaces_earlier_control_flow(
    monkeypatch, failure_watch, pooled_run, raised_by_records,
) -> None:
    process_pool, callbacks = pooled_run
    fail_to_start_the_runner_thread(monkeypatch, RuntimeError(A_RUNNER_FAILURE_CANARY))
    handler = RaisingLogHandler(raised_by_records)
    process_logger = logging.getLogger("agent_providers.process")
    process_logger.addHandler(handler)
    callbacks["on_spawn_failed"] = _raising(RuntimeError(ANOTHER_RUNNER_FAILURE_CANARY))
    channel = CliLineChannel(maximum_lines=1)
    try:
        with pytest.raises((SystemExit, KeyboardInterrupt)) as raised:
            _run_pooled(":", callbacks, stdout_line_channel=channel)
    finally:
        process_logger.removeHandler(handler)
    assert raised.type is SystemExit
    failure_watch.assert_nothing_leaked()


def test_control_flow_raised_by_the_log_handler_while_reporting_propagates_after_publishing(
    monkeypatch, failure_watch, pooled_run,
) -> None:
    process_pool, callbacks = pooled_run
    fail_to_start_the_runner_thread(monkeypatch, RuntimeError(A_RUNNER_FAILURE_CANARY))
    channel = CliLineChannel(maximum_lines=1)

    handler = RaisingLogHandler(then=SystemExit)
    process_logger = logging.getLogger("agent_providers.process")
    process_logger.addHandler(handler)
    try:
        with pytest.raises(SystemExit):
            _run_pooled(":", callbacks, stdout_line_channel=channel)
    finally:
        process_logger.removeHandler(handler)

    published = channel.receive(timeout=0)
    assert isinstance(published, CliRunOutcome)
    assert published.reason is CliRunReason.SPAWN_FAILED
    failure_watch.assert_nothing_leaked()


def _returned_or_raised(call: Callable[[], object]) -> object:
    try:
        return call()
    except BaseException as raised:
        return raised


def _returned_or_raised_within_bound(call: Callable[[], object]) -> object:
    results: list[object] = []
    caller = threading.Thread(target=lambda: results.append(_returned_or_raised(call)), daemon=True)
    caller.start()
    caller.join(timeout=5)
    assert not caller.is_alive(), "the call never returned"
    return results[0]


def _with_recorded_calls(callbacks: dict) -> tuple[dict, list[str]]:
    calls: list[str] = []

    def recording(name: str, callback: Callable[..., object]) -> Callable[..., object]:
        def call(*arguments: object) -> object:
            calls.append(name)
            return callback(*arguments)

        return call

    return {name: recording(name, callback) for name, callback in callbacks.items()}, calls


def _releasing_both(
    token: process.PrivateHomeReservation, release_slot: Callable[[], None],
) -> Callable[[], None]:
    def release() -> None:
        token.release()
        release_slot()

    return release


def _record_pipe_openings(monkeypatch) -> list[object]:
    openings: list[object] = []
    open_pipes = process.open_pipes

    def recording(*arguments, **keywords):
        openings.append(arguments)
        return open_pipes(*arguments, **keywords)

    monkeypatch.setattr(process, "open_pipes", recording)
    return openings


@pytest.mark.parametrize(
    ("error", "propagates"),
    [
        pytest.param(KeyboardInterrupt(), True, id="keyboard-interrupt"),
        pytest.param(SystemExit(), True, id="system-exit"),
        pytest.param(TimeoutError(A_RUNNER_FAILURE_CANARY), False, id="host-handler-timeout"),
    ],
)
def test_a_start_that_raises_while_the_worker_waits_leaves_the_runner_the_owner(
    monkeypatch, failure_watch, pooled_run, error, propagates,
) -> None:
    process_pool, pooled = pooled_run
    callbacks, calls = _with_recorded_calls(pooled)
    pipe_openings = _record_pipe_openings(monkeypatch)
    gate = start_then_raise(monkeypatch, error, thread_claims_first=False)
    channel = CliLineChannel(maximum_lines=1)

    result = _returned_or_raised(
        lambda: _run_pooled(":", callbacks, stdout_line_channel=channel)
    )
    gate.set()

    if propagates:
        assert result is error
        assert (error.__context__, error.__cause__) == (None, None)
    else:
        assert isinstance(result, CliRunOutcome)
        assert result.reason is CliRunReason.SPAWN_FAILED
    published = channel.receive(timeout=0)
    assert isinstance(published, CliRunOutcome)
    assert published.reason is CliRunReason.SPAWN_FAILED
    assert calls == ["on_spawn_failed"]
    assert process_pool.reservation_count() == 0
    assert failure_watch.reported_steps() == [f"start {type(error).__name__}"]
    assert pipe_openings == []
    failure_watch.assert_nothing_leaked()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(KeyboardInterrupt(), id="keyboard-interrupt"),
        pytest.param(SystemExit(), id="system-exit"),
        pytest.param(TimeoutError(A_RUNNER_FAILURE_CANARY), id="host-handler-timeout"),
    ],
)
def test_a_start_that_raises_after_the_worker_claimed_leaves_the_worker_the_owner(
    monkeypatch, failure_watch, pooled_run, error,
) -> None:
    process_pool, pooled = pooled_run
    callbacks, calls = _with_recorded_calls(pooled)
    start_then_raise(monkeypatch, error, thread_claims_first=True)
    channel = CliLineChannel(maximum_lines=1)

    result = _returned_or_raised_within_bound(
        lambda: _run_pooled(":", callbacks, stdout_line_channel=channel)
    )

    assert result is error
    assert (error.__context__, error.__cause__) == (None, None)
    published = channel.receive(timeout=5)
    assert isinstance(published, CliRunOutcome)
    assert published.reason is CliRunReason.COMPLETE
    assert calls == ["on_spawned", "on_reaped"]
    assert process_pool.reservation_count() == 0
    assert failure_watch.reported_steps() == []
    failure_watch.assert_nothing_leaked()


def test_a_start_claim_is_taken_once_and_never_waits() -> None:
    target_calls: list[str] = []
    thread = ClaimedStartThread(
        target=lambda: target_calls.append("ran"), daemon=True
    )

    first = thread.take_start_claim()
    second: list[bool] = []
    asker = threading.Thread(target=lambda: second.append(thread.take_start_claim()), daemon=True)
    asker.start()
    asker.join(timeout=5)
    assert not asker.is_alive(), "the second claim waited"
    thread.start()
    thread.join(timeout=5)

    assert (first, second) == (True, [False])
    assert target_calls == []


def test_the_start_injector_raises_its_error_after_the_thread_already_ended(monkeypatch) -> None:
    real_start = threading.Thread.start

    def start_and_wait_for_the_end(worker: threading.Thread) -> None:
        real_start(worker)
        worker.join(timeout=5)

    def nothing() -> None:
        pass

    monkeypatch.setattr(threading.Thread, "start", start_and_wait_for_the_end)
    error = KeyboardInterrupt()
    start_then_raise(monkeypatch, error, nothing, thread_claims_first=True)

    thread = ClaimedStartThread(target=nothing, daemon=True)

    with pytest.raises(KeyboardInterrupt) as raised:
        thread.start()

    assert raised.value is error


def test_an_interruption_inside_the_starters_claim_leaks_only_what_no_child_uses(
    monkeypatch, failure_watch, pooled_run, tmp_path,
) -> None:
    process_pool, pooled = pooled_run
    home = _credential_home(tmp_path)
    home_path = home.path
    token = home.reserve()

    callbacks, calls = _with_recorded_calls(
        {**pooled, "on_spawn_failed": _releasing_both(token, pooled["on_spawn_failed"])}
    )
    pipe_openings = _record_pipe_openings(monkeypatch)
    start_failure = RuntimeError()
    gate = start_then_raise(monkeypatch, start_failure, thread_claims_first=False)
    claim_ordered_by_the_gate = ClaimedStartThread.take_start_claim
    starter = threading.current_thread()

    def interrupted_after_claiming(thread: ClaimedStartThread) -> bool:
        won = claim_ordered_by_the_gate(thread)
        if threading.current_thread() is starter:
            raise KeyboardInterrupt
        return won

    monkeypatch.setattr(ClaimedStartThread, "take_start_claim", interrupted_after_claiming)

    result = _returned_or_raised(partial(_run_pooled, ":", callbacks))
    gate.set()
    failure_watch.wait_for_the_runner_threads_to_end()

    assert isinstance(result, KeyboardInterrupt)
    assert calls == []
    assert pipe_openings == []
    assert process_pool.reservation_count() == 1
    home.close()
    assert home_path.exists()
    start_failure.with_traceback(None)
    del home, token, callbacks, pooled, result
    gc.collect()
    assert not home_path.exists()


class _GivenToAThread:
    pass


@pytest.mark.parametrize("starter_claimed_first", [False, True], ids=["won", "lost"])
def test_a_finished_claimed_thread_keeps_nothing_it_was_given(starter_claimed_first) -> None:
    given = _GivenToAThread()
    thread = ClaimedStartThread(
        target=lambda argument: None, args=(given,), daemon=True
    )
    if starter_claimed_first:
        thread.take_start_claim()
    reference = weakref.ref(given)

    thread.start()
    thread.join(timeout=5)
    del given
    gc.collect()

    assert reference() is None
