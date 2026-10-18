"""A catalog probe's private home stays until the runner confirms the probe's group gone."""

from __future__ import annotations

import gc
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import provider_test_support
import pytest
from provider_test_support import (
    Hold,
    RunnerFailureWatch,
    fake_cli_process,
    install_cli_door,
)

from agent_providers import process
from agent_providers.config import current_config
from agent_providers.process import (
    CliRun,
    CliRunOutcome,
    CliRunReason,
    _run_credentialed_catalog_cli,
    run_claude_catalog_cli,
)
from agent_providers.procfs import GroupCensus
from agent_providers.spawn import KillUntilGoneResult

failure_watch = provider_test_support.failure_watch
held_bounded_groups = provider_test_support.held_bounded_groups

_BINARY = Path("/usr/bin/probe")
_A_PROCESS_ID = 4343
_A_LIVE_GROUP = KillUntilGoneResult(GroupCensus.LIVE, ended_in_time=True)
_AN_EMPTY_GROUP = KillUntilGoneResult(GroupCensus.EMPTY, ended_in_time=True)
_A_SHORT_REAP_BOUND_SECONDS = 0.2


@dataclass(frozen=True)
class CatalogEntry:
    call: Callable[[Path], CliRun | None]
    credential_copy: Path


def _claude_entry(binary: Path) -> CliRun | None:
    return run_claude_catalog_cli(binary, ())


def _credentialed_entry(variable: str, token_file: Callable[[], Path]):
    def call(binary: Path) -> CliRun | None:
        return _run_credentialed_catalog_cli(
            binary,
            (),
            credential_home_variable=variable,
            auth_file=token_file(),
        )

    return call


_ENTRIES = {
    "claude": CatalogEntry(_claude_entry, process._CLAUDE_CATALOG_CREDENTIAL_PATH),
    "codex": CatalogEntry(
        _credentialed_entry("CODEX_HOME", lambda: current_config().codex.token_file),
        process._CATALOG_AUTH_FILE_PATH,
    ),
    "grok": CatalogEntry(
        _credentialed_entry("GROK_HOME", lambda: current_config().grok.token_file),
        process._CATALOG_AUTH_FILE_PATH,
    ),
}


@pytest.fixture(params=_ENTRIES)
def entry(request: pytest.FixtureRequest) -> CatalogEntry:
    return _ENTRIES[request.param]


def _outcome(
    reason: CliRunReason = CliRunReason.COMPLETE,
    *,
    became_zombie: bool = False,
) -> CliRunOutcome:
    return CliRunOutcome(
        started=reason is not CliRunReason.DEADLINE_BEFORE_SPAWN,
        spawn_error=None,
        returncode=0,
        stdout="",
        stderr="",
        complete=reason is CliRunReason.COMPLETE,
        became_zombie=became_zombie,
        reason=reason,
    )


class RunnerRecorder:
    """Stands in for ``run_cli_bounded``: captures the call, acts as scripted, returns."""

    def __init__(
        self,
        copy_path: Path,
        outcome: CliRunOutcome,
        *,
        before_returning: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._copy_path = copy_path
        self._outcome = outcome
        self._before_returning = before_returning
        self.keywords: dict[str, Any] = {}
        self.home: Path | None = None
        self.credential_copy_existed_during_the_call = False

    def __call__(self, child, **keywords: Any) -> CliRunOutcome:
        self.keywords = keywords
        self.home = child.working_directory
        self.credential_copy_existed_during_the_call = (self.home / self._copy_path).is_file()
        if self._before_returning is not None:
            self._before_returning(keywords)
        return self._outcome

    def callback(self, name: str, *arguments: Any) -> None:
        self.keywords[name](*arguments)


def _install_recorder(monkeypatch, entry: CatalogEntry, outcome, **options) -> RunnerRecorder:
    recorder = RunnerRecorder(entry.credential_copy, outcome, **options)
    monkeypatch.setattr(process, "run_cli_bounded", recorder)
    return recorder


def _reap(keywords: dict[str, Any]) -> None:
    keywords["on_reaped"](_A_PROCESS_ID, False)


def _catalog_homes(root: Path) -> list[Path]:
    return sorted(root.glob(f"{process._CATALOG_HOME_PREFIX}*"))


def test_a_confirmed_probe_removes_its_home_when_the_call_returns(
    monkeypatch, entry: CatalogEntry
) -> None:
    recorder = _install_recorder(monkeypatch, entry, _outcome(), before_returning=_reap)

    entry.call(_BINARY)

    assert recorder.credential_copy_existed_during_the_call is True
    assert recorder.home.exists() is False


def test_an_unconfirmed_probe_keeps_its_home_until_the_background_confirms(
    monkeypatch, entry: CatalogEntry
) -> None:
    recorder = _install_recorder(monkeypatch, entry, _outcome(became_zombie=True))

    entry.call(_BINARY)
    present_after_the_return = (
        recorder.home.exists(),
        (recorder.home / entry.credential_copy).is_file(),
    )
    recorder.callback("on_reaped", _A_PROCESS_ID, True)

    assert present_after_the_return == (True, True)
    assert recorder.home.exists() is False


@pytest.mark.parametrize(
    ("reason", "late_callback", "late_arguments", "returns_a_run"),
    (
        (CliRunReason.DEADLINE_BEFORE_SPAWN, "on_spawn_failed", (), False),
        (CliRunReason.DEADLINE_BEFORE_SPAWN, "on_reaped", (_A_PROCESS_ID, False), False),
        (CliRunReason.CLEANUP_OVERRAN, "on_reaped", (_A_PROCESS_ID, False), True),
    ),
    ids=("late-spawn-failure", "late-reap-of-a-delayed-spawn", "late-reap-of-an-overran-settle"),
)
def test_a_late_start_or_an_overran_settle_keeps_the_home_until_its_callback(
    monkeypatch,
    entry: CatalogEntry,
    reason,
    late_callback,
    late_arguments,
    returns_a_run: bool,
) -> None:
    recorder = _install_recorder(monkeypatch, entry, _outcome(reason))

    run = entry.call(_BINARY)
    present_after_the_return = recorder.home.exists()
    recorder.callback(late_callback, *late_arguments)

    assert isinstance(run, CliRun) is returns_a_run
    if returns_a_run:
        assert run.complete is False
    assert present_after_the_return is True
    assert recorder.home.exists() is False


def test_a_probe_start_that_raises_leaves_the_home_to_the_runner_callback(
    monkeypatch, entry: CatalogEntry
) -> None:
    def fail(_keywords: dict[str, Any]) -> None:
        raise RuntimeError("the claimed start failed")

    recorder = _install_recorder(monkeypatch, entry, _outcome(), before_returning=fail)

    with pytest.raises(RuntimeError, match="the claimed start failed"):
        entry.call(_BINARY)
    present_after_the_raise = recorder.home.exists()
    recorder.callback("on_reaped", _A_PROCESS_ID, False)

    assert present_after_the_raise is True
    assert recorder.home.exists() is False


def test_a_catalog_probe_that_cannot_start_removes_its_home(tmp_path: Path) -> None:
    run = run_claude_catalog_cli(tmp_path / "no-such-claude", ())

    assert run is None
    assert _catalog_homes(tmp_path) == []


def _a_probe_on_a_fake_door(monkeypatch, *, background: KillUntilGoneResult, hold: Hold | None):
    child = fake_cli_process(b"ready", gone=(_A_LIVE_GROUP, background), hold=hold)
    homes: list[Path] = []

    def door(started_child, *, stderr):
        homes.append(started_child.working_directory)
        return child

    install_cli_door(monkeypatch, door)
    return child, homes


def test_a_probe_home_is_removed_once_the_background_reap_confirms_the_group(
    monkeypatch,
    failure_watch: RunnerFailureWatch,
) -> None:
    hold = Hold()
    child, homes = _a_probe_on_a_fake_door(monkeypatch, background=_AN_EMPTY_GROUP, hold=hold)

    run_claude_catalog_cli(_BINARY, ())
    [home] = homes
    present_at_the_return = home.exists()
    hold.proceed.set()
    failure_watch.wait_for_the_runner_threads_to_end()

    assert present_at_the_return is True
    assert (home.exists(), child.released) == (False, True)


def test_a_probe_home_stays_when_the_background_never_confirms_the_group(
    monkeypatch,
    failure_watch: RunnerFailureWatch,
    held_bounded_groups,
) -> None:
    monkeypatch.setattr(process, "_BACKGROUND_REAP_LIMIT_SECONDS", _A_SHORT_REAP_BOUND_SECONDS)
    child, homes = _a_probe_on_a_fake_door(monkeypatch, background=_A_LIVE_GROUP, hold=None)

    run_claude_catalog_cli(_BINARY, ())
    failure_watch.wait_for_the_runner_threads_to_end()
    gc.collect()
    [home] = homes

    assert (home.exists(), (home / process._CLAUDE_CATALOG_CREDENTIAL_PATH).is_file()) == (
        True,
        True,
    )
    assert [group.process for group in held_bounded_groups] == [child]
    assert held_bounded_groups[0].on_reaped is not None
