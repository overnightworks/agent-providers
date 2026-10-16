"""Tests for bounded probes of mounted agent CLIs."""

from __future__ import annotations

import gc
import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from provider_test_support import (
    A_RUNNER_FAILURE_CANARY,
    ANOTHER_RUNNER_FAILURE_CANARY,
    GROK_LOGGED_IN,
    SECRET_ENV_KEYS,
    RunnerFailureWatch,
    fail_to_start_the_runner_thread,
    override_provider_runtime,
    pool_callbacks,
    shell_child,
)

from agent_providers import process
from agent_providers.codex.pool import CodexProcessKind, CodexProcessPool
from agent_providers.config import current_config
from agent_providers.constants import (
    CLAUDE_CLI_AUTH_METHOD_FIELD,
    CLAUDE_CLI_LOGGED_IN_FIELD,
    CLI_OUTPUT_READ_LIMIT_BYTES,
)
from agent_providers.process import (
    CODEX_CLI_CREDENTIALS_INVALID_DETAIL,
    GROK_CLI_CREDENTIALS_INVALID_DETAIL,
    AgentCliUnavailableError,
    CachedProbe,
    CliCredentialNotConfiguredError,
    CliLineChannel,
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
from agent_providers.spawn import INHERITED_MACHINE_VARIABLES, ChildProcess

GROK_LOGGED_OUT = """You are not authenticated.

Available models:
  * grok-4.6 (default)
"""
CODEX_LOGGED_IN = "Logged in using ChatGPT"
CODEX_LOGGED_OUT = "Not logged in"
_CATALOG_AUTH_PAYLOAD = '{"auth_mode":"catalog-probe"}'
_CATALOG_PROVIDERS = (
    ("GROK_HOME", "grok_cli_auth_file"),
    ("CODEX_HOME", "codex_cli_auth_file"),
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
        AgentCliUnavailableError,
        match="login status",
    ):
        grok_cli_status()


def test_grok_that_is_signed_in_but_lists_nothing_raises() -> None:
    with _a_cli_that_says("You are logged in with grok.com.\n"), pytest.raises(
        AgentCliUnavailableError,
        match="model name",
    ):
        grok_cli_status()


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


def test_codex_model_catalog_without_a_binary_raises(monkeypatch) -> None:
    monkeypatch.setattr(process, "resolve_cli_binary", lambda _binary: None)

    with pytest.raises(
        AgentCliUnavailableError,
        match="did not return a catalog",
    ):
        codex_cli_model_catalog()


def test_codex_answering_in_words_we_do_not_know_raises() -> None:
    with _a_cli_that_says("auth: unknown"), pytest.raises(
        AgentCliUnavailableError,
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


def test_a_cli_that_is_not_installed_cannot_be_asked() -> None:
    with patch("agent_providers.process.resolve_cli_binary", return_value=None):
        assert grok_cli_status().login.logged_in is False


def _expected_closed_names(credential_home_variable: str) -> list[str]:
    """Every name a closed child may carry: its own plus the machine's."""
    return ["HOME", "TMPDIR", "PATH", credential_home_variable] + [
        name for name in INHERITED_MACHINE_VARIABLES if os.environ.get(name)
    ]


def _prepare_catalog_credential(
    tmp_path: Path,
    monkeypatch,
    *,
    auth_field: str,
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
    override_provider_runtime(**{auth_field: credential})
    return original_home, credential


def _run_catalog_python(
    tmp_path: Path,
    monkeypatch,
    *,
    credential_home_variable: str,
    auth_field: str,
    script: str,
    auth_payload: str = _CATALOG_AUTH_PAYLOAD,
) -> tuple[CliRun, Path, Path]:
    original_home, credential = _prepare_catalog_credential(
        tmp_path, monkeypatch, auth_field=auth_field, auth_payload=auth_payload,
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


def test_private_credential_home_removes_a_read_only_directory_a_child_left(
    tmp_path: Path,
) -> None:
    home = _credential_home(tmp_path)
    child_directory = home.path / "child-made"
    child_directory.mkdir()
    (child_directory / "scratch.txt").write_text("scratch")
    child_directory.chmod(0o500)

    home.close()

    assert not home.path.exists()


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
    with pytest.raises(AgentCliUnavailableError, match="configured CLI credential is missing"):
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

    with pytest.raises(CliCredentialNotConfiguredError):
        PrivateCredentialHome(
            unusable,
            Path("auth.json"),
            prefix="private-home-",
            missing_credential_is_error=True,
            credential_copy=process.blank_codex_refresh_token,
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

    with pytest.raises(AgentCliUnavailableError, match="configured CLI credential is missing"):
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


_FORK_WHILE_REGISTRY_LOCKED_SCRIPT = """
import os
import signal
import sys
import threading
from pathlib import Path

from agent_providers import process
from agent_providers.config import ProviderRuntimeConfig, configure
from agent_providers.process import PrivateCredentialHome

configure(ProviderRuntimeConfig.model_validate_json(sys.argv[1]))
lock_held = threading.Event()
release_lock = threading.Event()


def hold_registry_lock():
    with process._live_homes_lock:
        lock_held.set()
        release_lock.wait()


worker = threading.Thread(target=hold_registry_lock)
worker.start()
lock_held.wait()
child = os.fork()
if child == 0:
    home = PrivateCredentialHome(
        Path(sys.argv[2]),
        Path(".claude/.credentials.json"),
        prefix="private-home-",
        missing_credential_is_error=True,
    )
    process._sweep_failed_homes_at_exit()
    os._exit(0)
failure_guard = threading.Timer(15, os.kill, (child, signal.SIGKILL))
failure_guard.start()
_, status = os.waitpid(child, 0)
failure_guard.cancel()
release_lock.set()
worker.join()
print(os.waitstatus_to_exitcode(status))
"""


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_forked_child_creates_and_sweeps_homes_while_the_parent_holds_the_registry_lock(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.json"
    source.write_text(_CATALOG_AUTH_PAYLOAD)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _FORK_WHILE_REGISTRY_LOCKED_SCRIPT,
            current_config().model_dump_json(),
            str(source),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert completed.stdout.split() == ["0"]


@pytest.mark.parametrize(("credential_home_variable", "auth_field"), _CATALOG_PROVIDERS)
def test_catalog_child_sees_only_the_closed_environment_variables(
    tmp_path, monkeypatch, credential_home_variable: str, auth_field: str,
) -> None:
    original_home, credential = _prepare_catalog_credential(
        tmp_path, monkeypatch, auth_field=auth_field,
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


@pytest.mark.parametrize(("credential_home_variable", "auth_field"), _CATALOG_PROVIDERS)
def test_catalog_child_reads_the_host_named_credential_file(
    tmp_path, monkeypatch, credential_home_variable: str, auth_field: str,
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
        auth_field=auth_field,
        script=script,
    )

    assert run.complete is True
    assert run.returncode == 0
    assert run.stdout == _CATALOG_AUTH_PAYLOAD
    assert credential.read_text() == _CATALOG_AUTH_PAYLOAD


@pytest.mark.parametrize(("credential_home_variable", "auth_field"), _CATALOG_PROVIDERS)
def test_catalog_child_cannot_write_the_original_credential_directory(
    tmp_path, monkeypatch, credential_home_variable: str, auth_field: str,
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
        auth_field=auth_field,
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
        tmp_path, monkeypatch, auth_field="claude_cli_auth_file",
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
        tmp_path, monkeypatch, auth_field="claude_cli_auth_file",
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
    _prepare_catalog_credential(tmp_path, monkeypatch, auth_field="claude_cli_auth_file")
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
    _prepare_catalog_credential(tmp_path, monkeypatch, auth_field="claude_cli_auth_file")
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
    override_provider_runtime(claude_cli_auth_file=missing)
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
    working_root.mkdir()
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
        else process.codex_cli_access_token_is_present
    )
    field = "grok_cli_auth_file" if checker == "grok" else "codex_cli_auth_file"
    override_provider_runtime(**{field: credential_file})

    with pytest.raises(AgentCliUnavailableError, match=detail):
        function()


@pytest.mark.parametrize(
    "payload",
    (
        None,
        "not json",
        "[]",
        '{"tokens": []}',
        '{"auth_mode": "chatgpt", "tokens": {}}',
        '{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-synthetic"}',
    ),
    ids=(
        "missing", "not-json", "not-an-object", "tokens-not-an-object", "no-access-token",
        "api-key-only",
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

    monkeypatch.setattr(process, "_reap_process_group", _raising(
        PermissionError(A_RUNNER_FAILURE_CANARY),
    ))
    monkeypatch.setattr(process, "_reap_in_background", capture_background_reap)
    deadline = time.monotonic() + 0.05

    outcome = _run_pooled("exec sleep 10", callbacks, deadline=deadline)
    returned_at = time.monotonic()

    assert outcome.became_zombie is True
    assert outcome.reason in process._DEADLINE_REASONS
    assert returned_at <= cli_settle_deadline(deadline)
    assert background_reap_ended.wait(timeout=2)
    assert handed_off[0].returncode == -9
    assert held_at_release == [1]
    assert process_pool.reservation_count() == 0
    assert failure_watch.reported_steps() == ["reap PermissionError"]
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
    handler_errors: list[logging.LogRecord] = []

    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                raise RuntimeError("log handler failed")
            except RuntimeError:
                self.handleError(record)

        def handleError(self, record: logging.LogRecord) -> None:
            handler_errors.append(record)
            super().handleError(record)

    handler = FailingHandler()
    process_logger = logging.getLogger("agent_providers.process")
    process_logger.addHandler(handler)
    monkeypatch.setattr(process, "_exchange_bounded", _raising(ValueError(A_RUNNER_FAILURE_CANARY)))
    try:
        outcome = _run_pooled("exec sleep 10", callbacks)
    finally:
        process_logger.removeHandler(handler)

    assert outcome.reason is CliRunReason.IO_ERROR
    assert failure_watch.reported_steps() == ["exchange ValueError"]
    assert len(handler_errors) == 1
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

    class RaisingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise OSError("log handler cannot write")

    handler = RaisingHandler()
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

    with pytest.raises(KeyboardInterrupt):
        _run_pooled(":", callbacks, stdout_line_channel=channel)

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
    assert channel.receive(timeout=0) is outcome
    assert process_pool.reservation_count() == 0
    failure_watch.assert_nothing_leaked()
