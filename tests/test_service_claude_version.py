"""The service starts only when the Claude program reports exactly the version the config pins."""

from __future__ import annotations

import errno
import fnmatch
import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import provider_test_support
import pytest
import uvicorn
from provider_test_support import (
    Hold,
    RunnerFailureWatch,
    fail_the_directory_named,
    fake_cli_executable,
    fake_cli_process,
    install_cli_door,
)
from service_support import (
    PINNED_CLAUDE_VERSION,
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
)
from test_run_directories import (
    REMOVAL_FAILED,
    removal_errors,
    remove_the_leftover,
    rmtree_refused,
    the_only_tree,
)
from test_service_config import (
    CLAUDE_VERSION_LINE,
    CLI_SECTION,
    NO_PROFILE,
    VALID_CONFIG,
    _refused_on_one_line,
    _written,
    cli_section,
)

import agent_providers.process as core_process
from agent_providers.config import reset_config
from agent_providers.procfs import GroupCensus
from agent_providers.spawn import KillUntilGoneResult
from agent_service.claude_version import parse_version_output
from agent_service.cli import EXIT_BAD_CONFIG, main

PRINTS_THE_PIN = f"{PINNED_CLAUDE_VERSION} (Claude Code)\n"
WORKING_ROOT_NAME = "working-root"
RUN_TREE_PATTERN = "agent-run-*"
A_LIVE_GROUP = KillUntilGoneResult(GroupCensus.LIVE, ended_in_time=True)
AN_EMPTY_GROUP = KillUntilGoneResult(GroupCensus.EMPTY, ended_in_time=True)
A_SHORT_PROBE_DEADLINE_SECONDS = 0.05
PROVIDER_TOKEN_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "XAI_API_KEY",
    "OPENAI_API_KEY",
)


failure_watch = provider_test_support.failure_watch


def _working_root_of(tmp_path: Path) -> Path:
    root = tmp_path / WORKING_ROOT_NAME
    root.mkdir(exist_ok=True)
    root.chmod(0o700)
    return root


@pytest.fixture(autouse=True)
def working_root(tmp_path: Path) -> Iterator[Path]:
    root = _working_root_of(tmp_path)
    yield root
    for leftover in root.iterdir():
        remove_the_leftover(leftover)


@pytest.fixture
def served(monkeypatch, logging_state_restored):
    runs = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **where: runs.append(where))
    return runs


def _script(directory: Path, body: str) -> Path:
    program = directory / "claude-script"
    program.write_text(f"#!/bin/sh\n{body}\n")
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    return program


def _printing(directory: Path, stdout: str) -> Path:
    return fake_cli_executable(directory, {("--version",): stdout})


def _document_naming(program: Path, root: Path, base: str = VALID_CONFIG) -> str:
    binary_line = f"claude_binary = {json.dumps(str(program))}"
    root_line = f"working_root = {json.dumps(str(root))}"
    return base.replace(CLI_SECTION, cli_section(binary_line, CLAUDE_VERSION_LINE, root_line))


def _config_file_naming(tmp_path: Path, program: Path, base: str = VALID_CONFIG) -> Path:
    return _written(tmp_path, _document_naming(program, _working_root_of(tmp_path), base))


def _refused_leaving_the_root_empty(
    tmp_path: Path, capsys, program: Path, base: str = VALID_CONFIG
) -> str:
    refusal = _refused_on_one_line(_config_file_naming(tmp_path, program, base), capsys)
    assert list(_working_root_of(tmp_path).iterdir()) == []
    return refusal


def _no_version_line(program: Path, reason: str) -> str:
    return (
        f"cli.claude_version: {program} did not print a version ({reason});"
        f" the pinned version is {PINNED_CLAUDE_VERSION}"
    )


@pytest.fixture
def probe_log(tmp_path) -> Path:
    return tmp_path / "probe-starts"


def _logged_program(tmp_path: Path, probe_log: Path) -> Path:
    return fake_cli_executable(tmp_path, {("--version",): PRINTS_THE_PIN}, calls_log=probe_log)


def test_a_program_reporting_the_pinned_version_starts_the_service_without_a_core_runtime(
    tmp_path, working_root, served
):
    reset_config()
    program = _script(tmp_path, f'printf "{PRINTS_THE_PIN}"')

    assert main(["--config", str(_config_file_naming(tmp_path, program))]) == 0

    assert served == [{"host": "127.0.0.1", "port": 8099}]
    assert list(working_root.iterdir()) == []


def test_a_cli_section_without_a_profile_serves_after_probing_the_program(
    tmp_path, served, probe_log
):
    program = _logged_program(tmp_path, probe_log)

    assert main(["--config", str(_config_file_naming(tmp_path, program, NO_PROFILE))]) == 0

    assert len(served) == 1
    assert probe_log.read_text() == "--version\n"


def test_an_installation_with_neither_profile_nor_cli_section_serves(tmp_path, served):
    text = NO_PROFILE.replace(CLI_SECTION, "")

    assert main(["--config", str(_written(tmp_path, text))]) == 0

    assert len(served) == 1


def test_the_probe_runs_in_a_run_tree_below_the_working_root_with_no_token_and_no_login(
    tmp_path, working_root, served, monkeypatch
):
    reset_config()
    parent_home = tmp_path / "parent-home"
    (parent_home / ".claude").mkdir(parents=True)
    (parent_home / ".claude" / ".credentials.json").write_text("login-marker")
    monkeypatch.setenv("HOME", str(parent_home))
    for variable in PROVIDER_TOKEN_VARIABLES:
        monkeypatch.setenv(variable, "token-marker")
    inspecting = tmp_path / "inspection"
    inspecting.mkdir()
    program = _script(
        tmp_path,
        f'env > "{inspecting}/environment"; pwd -P > "{inspecting}/cwd";'
        f' ls -A "$HOME" > "{inspecting}/home-listing";'
        f' ls -A . > "{inspecting}/work-listing"; echo "$HOME" > "{inspecting}/home";'
        f' printf "{PRINTS_THE_PIN}"',
    )

    assert main(["--config", str(_config_file_naming(tmp_path, program))]) == 0

    home = Path((inspecting / "home").read_text().strip())
    working_directory = Path((inspecting / "cwd").read_text().strip())
    assert home.parent.parent == working_root.resolve()
    assert fnmatch.fnmatch(home.parent.name, RUN_TREE_PATTERN)
    assert (home.name, working_directory) == ("home", home.parent / "work")
    assert (inspecting / "home-listing").read_text() == ""
    assert (inspecting / "work-listing").read_text() == ""
    seen = {line.split("=", 1)[0] for line in (inspecting / "environment").read_text().splitlines()}
    assert seen.isdisjoint(PROVIDER_TOKEN_VARIABLES)
    assert "marker" not in (inspecting / "environment").read_text()
    assert list(working_root.iterdir()) == []


def test_a_program_reporting_another_version_refuses_the_start_naming_both(
    tmp_path, capsys, served
):
    program = _printing(tmp_path, "2.1.303 (Claude Code)\n")

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == (
        f"cli.claude_version: {program} reports 2.1.303, not the pinned {PINNED_CLAUDE_VERSION}"
    )
    assert served == []


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        pytest.param("hello\n", "printed hello", id="another-text"),
        pytest.param("2.1.302\n", "printed 2.1.302", id="without-the-product-suffix"),
        pytest.param(
            f"{PRINTS_THE_PIN}banner\n", f"printed {PRINTS_THE_PIN.strip()}", id="two-lines"
        ),
        pytest.param("", "printed nothing", id="empty"),
        pytest.param("   \n", "printed    ", id="only-spaces"),
        pytest.param("\nhello\n", "printed ", id="blank-first-line"),
        pytest.param("x" * 100 + "\n", "printed " + "x" * 60, id="long-line-cut"),
    ],
)
def test_output_that_is_not_the_version_line_refuses_the_start_showing_what_was_printed(
    tmp_path, capsys, served, stdout, reason
):
    program = _printing(tmp_path, stdout)

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(program, reason)
    assert served == []


def test_a_program_printing_its_home_refuses_the_start_without_the_working_root(
    tmp_path, working_root, capsys, served
):
    program = _script(tmp_path, 'echo "$HOME"')

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert str(working_root) not in refusal
    assert "printed <working root>/agent-run-" in refusal
    assert served == []


def test_a_nonzero_exit_refuses_the_start_even_when_the_output_looks_valid(
    tmp_path, capsys, served
):
    program = _script(tmp_path, f'printf "{PRINTS_THE_PIN}"; exit 3')

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(program, "exit status 3")
    assert served == []


def test_a_program_that_does_not_exist_refuses_the_start_as_unstarted(tmp_path, capsys, served):
    program = tmp_path / "absent-claude"

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(
        program, "probe could not start or its start deadline expired"
    )


def test_a_program_that_never_finishes_is_stopped_and_refuses_the_start(
    tmp_path, capsys, served, monkeypatch
):
    monkeypatch.setattr(core_process, "PROBE_TIMEOUT_SECONDS", 1)
    program = _script(tmp_path, f'echo $$ > "{tmp_path}/pid"; exec sleep 60')

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(program, "probe did not complete")
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_output_beyond_the_runner_limit_refuses_the_start_as_incomplete(tmp_path, capsys, served):
    program = _script(tmp_path, "head -c 70000 /dev/zero | tr '\\0' x")

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(program, "probe did not complete")


def test_valid_output_with_stderr_beyond_the_runner_limit_still_refuses_as_incomplete(
    tmp_path, capsys, served
):
    program = _script(
        tmp_path, f"printf '{PRINTS_THE_PIN}'; head -c 70000 /dev/zero | tr '\\0' x >&2"
    )

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(program, "probe did not complete")
    assert served == []


def test_a_binary_path_holding_a_newline_is_refused_on_one_line_with_the_newline_escaped(
    tmp_path, capsys, served
):
    program = tmp_path / "line\nbreak" / "claude"

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == (
        f"cli.claude_version: {str(program).replace(chr(10), '\\n')} did not print a version"
        f" (probe could not start or its start deadline expired);"
        f" the pinned version is {PINNED_CLAUDE_VERSION}"
    )


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(FileNotFoundError(errno.ENOENT, "no temporary directory"), id="missing"),
        pytest.param(PermissionError(errno.EACCES, "not writable"), id="permission-denied"),
        pytest.param(OSError(errno.ENOSPC, "no space left on device"), id="disk-full"),
    ],
)
def test_a_run_tree_that_cannot_be_made_refuses_the_start_as_unstarted(
    tmp_path, capsys, served, monkeypatch, failure
):
    program = _printing(tmp_path, PRINTS_THE_PIN)

    def cannot_make_a_directory(*_arguments, **_keywords):
        raise failure

    monkeypatch.setattr(tempfile, "mkdtemp", cannot_make_a_directory)

    refusal = _refused_leaving_the_root_empty(tmp_path, capsys, program)

    assert refusal == _no_version_line(
        program, "probe could not start or its start deadline expired"
    )
    assert served == []


@pytest.mark.parametrize(
    ("stdout", "version"),
    [
        pytest.param("2.1.302 (Claude Code)\n", "2.1.302", id="one-terminal-newline"),
        pytest.param("2.1.302 (Claude Code)", "2.1.302", id="no-newline"),
        pytest.param("0.0.0 (Claude Code)\n", "0.0.0", id="zeros"),
        pytest.param("2.1.302 (Claude Code)\n\n", None, id="two-newlines"),
        pytest.param(" 2.1.302 (Claude Code)\n", None, id="leading-space"),
        pytest.param("2.1.302 (Claude Code) \n", None, id="trailing-space"),
        pytest.param("2.1.302 (Claude Code)\r\n", None, id="carriage-return"),
        pytest.param("2.1.302\n", None, id="no-suffix"),
        pytest.param("v2.1.302 (Claude Code)\n", None, id="v-prefix"),
        pytest.param("2.01.302 (Claude Code)\n", None, id="leading-zero"),
        pytest.param("2.1.302-beta (Claude Code)\n", None, id="prerelease"),
        pytest.param("banner\n2.1.302 (Claude Code)\n", None, id="banner-line"),
        pytest.param("", None, id="empty"),
        pytest.param("2.1.30٢ (Claude Code)\n", None, id="non-ascii-digit"),
    ],
)
def test_only_the_exact_version_line_is_parsed(stdout, version):
    assert parse_version_output(stdout) == version


def _the_unstarted_refusal(program: Path) -> str:
    return _no_version_line(program, "probe could not start or its start deadline expired")


def _the_mismatch_refusal(program: Path) -> str:
    return f"cli.claude_version: {program} reports 2.1.303, not the pinned {PINNED_CLAUDE_VERSION}"


def test_a_run_tree_whose_rollback_fails_still_refuses_the_start_as_unstarted(
    tmp_path, working_root, capsys, served, monkeypatch, caplog
):
    program = _printing(tmp_path, PRINTS_THE_PIN)
    config_file = _config_file_naming(tmp_path, program)
    fail_the_directory_named(monkeypatch, "home", OSError(errno.EIO, "synthetic mkdir failure"))

    with rmtree_refused(monkeypatch):
        refusal = _refused_on_one_line(config_file, capsys)

    the_only_tree(working_root)
    assert refusal == _the_unstarted_refusal(program)
    assert removal_errors(caplog) == [REMOVAL_FAILED]
    assert str(working_root) not in caplog.text
    assert served == []


def test_a_probe_tree_that_cannot_be_removed_is_logged_once_and_the_start_still_serves(
    tmp_path, working_root, served, caplog, monkeypatch
):
    program = _printing(tmp_path, PRINTS_THE_PIN)
    config_file = _config_file_naming(tmp_path, program)

    with rmtree_refused(monkeypatch):
        exit_status = main(["--config", str(config_file)])

    the_only_tree(working_root)
    assert exit_status == 0
    assert removal_errors(caplog) == [REMOVAL_FAILED]
    assert str(working_root) not in caplog.text
    assert len(served) == 1


@pytest.mark.parametrize(
    ("program_name", "stdout", "expected_refusal"),
    [
        pytest.param("absent-claude", None, _the_unstarted_refusal, id="unstarted"),
        pytest.param("claude", "2.1.303 (Claude Code)\n", _the_mismatch_refusal, id="mismatch"),
    ],
)
def test_a_probe_tree_that_cannot_be_removed_never_replaces_the_probe_refusal(
    tmp_path, working_root, capsys, served, monkeypatch, program_name, stdout, expected_refusal
):
    program = tmp_path / program_name if stdout is None else _printing(tmp_path, stdout)
    config_file = _config_file_naming(tmp_path, program)

    with rmtree_refused(monkeypatch):
        printed = _refused_on_one_line(config_file, capsys)

    the_only_tree(working_root)
    assert printed == expected_refusal(program)
    assert served == []


def test_a_probe_tree_stays_below_the_working_root_until_its_group_is_confirmed_gone(
    tmp_path, working_root, served, monkeypatch, failure_watch: RunnerFailureWatch
):
    hold = Hold()
    group_answers = (A_LIVE_GROUP, AN_EMPTY_GROUP)
    child = fake_cli_process(PRINTS_THE_PIN.encode(), gone=group_answers, hold=hold)
    install_cli_door(monkeypatch, lambda _child, *, stderr: child)
    program = tmp_path / "claude"
    config_file = _config_file_naming(tmp_path, program)

    exit_status = main(["--config", str(config_file)])
    trees_at_the_return = list(working_root.glob(RUN_TREE_PATTERN))
    hold.proceed.set()
    failure_watch.wait_for_the_runner_threads_to_end()

    assert exit_status == 0
    assert len(trees_at_the_return) == 1
    assert list(working_root.iterdir()) == []
    assert len(served) == 1


def test_a_probe_refused_before_its_spawn_keeps_its_tree_until_the_late_spawn_is_confirmed(
    tmp_path, working_root, capfd, served, monkeypatch, failure_watch: RunnerFailureWatch
):
    program = _printing(tmp_path, PRINTS_THE_PIN)
    config_file = _config_file_naming(tmp_path, program)
    release_spawn = threading.Event()

    def held_door(child, *, stderr):
        assert release_spawn.wait(timeout=provider_test_support.TEST_WAIT_SECONDS)
        return real_door(child, stderr=stderr)

    real_door = install_cli_door(monkeypatch, held_door)
    monkeypatch.setattr(core_process, "PROBE_TIMEOUT_SECONDS", A_SHORT_PROBE_DEADLINE_SECONDS)

    try:
        exit_status = main(["--config", str(config_file)])
        trees_after_main = list(working_root.glob(RUN_TREE_PATTERN))
    finally:
        release_spawn.set()
    failure_watch.wait_for_the_runner_threads_to_end()

    assert exit_status == EXIT_BAD_CONFIG
    assert capfd.readouterr().err.endswith(f"{_the_unstarted_refusal(program)}\n")
    assert len(trees_after_main) == 1
    assert list(working_root.iterdir()) == []
    assert served == []
