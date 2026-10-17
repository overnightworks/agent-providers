"""The service refuses root and makes itself non-dumpable before it reads any secret."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from test_service_claude_version import _config_file_naming, _logged_program
from test_service_config import CLI_SECTION, NO_PROFILE, _written

import agent_service.process_hardening as hardening
from agent_service.cli import main

REAL_SET_NOT_DUMPABLE = hardening.set_not_dumpable

MISSING_CONFIG_ARGUMENTS = ["--config", "/nonexistent/agent-service.toml"]
ROOT_REFUSAL = (
    "agent-service: refusing to start: running as root (effective uid 0);"
    " run the service as an unprivileged user\n"
)
PERMISSION_REFUSAL = (
    "agent-service: refusing to start: cannot make the process non-dumpable:"
    " Operation not permitted\n"
)
PLATFORM_REFUSAL = (
    "agent-service: refusing to start: needs Linux (/proc and prctl); this platform is darwin\n"
)
EXPECTED_PRCTL_CALL = (hardening.PR_SET_DUMPABLE, 0, 0, 0, 0)


@pytest.fixture
def served(monkeypatch):
    runs = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **where: runs.append(where))
    return runs


@pytest.fixture
def boundary_calls(monkeypatch):
    calls = []

    def recording(name, fake):
        def call(*arguments):
            calls.append(name)
            return fake(*arguments)

        monkeypatch.setattr(hardening, name, call)

    recording(
        "load_libc", lambda: pytest.fail("libc is opened only after the platform and root checks")
    )
    return calls, recording


def _fake_libc_whose_prctl_fails_with(error_number: int) -> SimpleNamespace:
    def prctl(*_arguments) -> int:
        ctypes.set_errno(error_number)
        return -1

    return SimpleNamespace(prctl=prctl)


def _fake_libc_recording_into(calls: list) -> SimpleNamespace:
    def prctl(*arguments) -> int:
        calls.append(arguments)
        return 0

    return SimpleNamespace(prctl=prctl)


def _one_line_refusal(capsys) -> str:
    refusal = capsys.readouterr().err
    assert refusal.count("\n") == 1
    return refusal


@pytest.mark.parametrize(
    ("running_platform", "effective_user", "refusal"),
    [
        pytest.param("linux", 0, ROOT_REFUSAL, id="N1-root"),
        pytest.param("darwin", 1000, PLATFORM_REFUSAL, id="N4-not-linux"),
    ],
)
def test_a_root_or_non_linux_start_ends_in_one_line_before_the_config_is_read(
    monkeypatch, served, capsys, boundary_calls, running_platform, effective_user, refusal
):
    calls, recording = boundary_calls
    recording("running_platform", lambda: running_platform)
    recording("effective_user_id", lambda: effective_user)
    recording("set_not_dumpable", lambda: None)

    assert main(MISSING_CONFIG_ARGUMENTS) == 2

    assert _one_line_refusal(capsys) == refusal
    assert served == []
    assert "load_libc" not in calls
    assert "set_not_dumpable" not in calls


def test_a_non_linux_start_does_not_ask_for_the_user(monkeypatch, served, capsys, boundary_calls):
    calls, recording = boundary_calls
    recording("running_platform", lambda: "darwin")
    recording("effective_user_id", lambda: 1000)

    assert main(MISSING_CONFIG_ARGUMENTS) == 2

    assert "effective_user_id" not in calls


def test_an_unprivileged_start_hardens_before_it_reads_the_config(monkeypatch, served, capsys):
    hardened = []
    monkeypatch.setattr(hardening, "set_not_dumpable", lambda: hardened.append(True))

    assert main(MISSING_CONFIG_ARGUMENTS) == 2

    assert hardened == [True]
    assert "/nonexistent/agent-service.toml" in capsys.readouterr().err


@pytest.mark.parametrize("with_cli_section", [False, True], ids=["no-cli", "with-cli"])
def test_an_unprivileged_start_serves_and_still_probes_the_program(
    tmp_path, served, with_cli_section
):
    if with_cli_section:
        probe_log = tmp_path / "probe-starts"
        program = _logged_program(tmp_path, probe_log)
        config_file = _config_file_naming(tmp_path, program, NO_PROFILE)
    else:
        config_file = _written(tmp_path, NO_PROFILE.replace(CLI_SECTION, ""))

    assert main(["--config", str(config_file)]) == 0

    assert len(served) == 1
    if with_cli_section:
        assert probe_log.read_text() == "--version\n"


def test_a_failing_prctl_ends_in_one_line_with_the_error_text_before_the_config_is_read(
    monkeypatch, served, capsys
):
    monkeypatch.setattr(hardening, "set_not_dumpable", REAL_SET_NOT_DUMPABLE)
    monkeypatch.setattr(
        hardening, "load_libc", lambda: _fake_libc_whose_prctl_fails_with(errno.EPERM)
    )
    errno_before = ctypes.get_errno()
    try:
        assert main(MISSING_CONFIG_ARGUMENTS) == 2
    finally:
        ctypes.set_errno(errno_before)

    assert _one_line_refusal(capsys) == PERMISSION_REFUSAL
    assert served == []


def test_the_wrapper_calls_prctl_with_native_width_arguments(monkeypatch):
    recorded = []
    libc = _fake_libc_recording_into(recorded)
    monkeypatch.setattr(hardening, "load_libc", lambda: libc)

    REAL_SET_NOT_DUMPABLE()

    assert recorded == [EXPECTED_PRCTL_CALL]
    assert libc.prctl.argtypes == [ctypes.c_int] + [ctypes.c_ulong] * 4
    assert libc.prctl.restype is ctypes.c_int


PR_SET_PTRACER = 0x59616D61
PTRACER_ANY = ctypes.c_ulong(-1).value
ENVIRONMENT_CANARY = "environment-canary-6f1d2c"
MEMORY_CANARY = b"memory-canary-9b3e7a"
BOUND_SECONDS = 60

READER = textwrap.dedent(
    """
    import json, os, sys

    address, environment_canary, memory_canary = sys.argv[1], sys.argv[2], sys.argv[3]
    parent = os.getppid()

    def reads(path, matches):
        try:
            with open(path, "rb") as stream:
                return "match" if matches(stream) else "mismatch"
        except PermissionError:
            return "denied"

    def environment_matches(stream):
        return environment_canary.encode() in stream.read()

    def memory_matches(stream):
        stream.seek(int(address))
        return stream.read(len(memory_canary)) == memory_canary.encode()

    print(json.dumps({
        "environ": reads(f"/proc/{parent}/environ", environment_matches),
        "mem": reads(f"/proc/{parent}/mem", memory_matches),
    }))
    """
)

SERVICE_UNDER_READER = textwrap.dedent(
    """
    import ctypes, json, subprocess, sys

    import uvicorn

    from agent_service.cli import main

    reader, environment_canary, memory_canary, bound = sys.argv[1:5]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    assert libc.prctl(%d, %d, 0, 0, 0) == 0
    canary = ctypes.create_string_buffer(memory_canary.encode())
    address = ctypes.addressof(canary)

    def read_from_a_child():
        done = subprocess.run(
            [sys.executable, "-c", reader, str(address), environment_canary, memory_canary],
            capture_output=True, text=True, timeout=float(bound),
        )
        return json.loads(done.stdout)

    report = {"control": read_from_a_child()}
    uvicorn.run = lambda app, **where: report.update(treatment=read_from_a_child())
    report["exit"] = main(["--config", sys.argv[5]])
    print(json.dumps(report))
    """
    % (PR_SET_PTRACER, PTRACER_ANY)
)


def _yama_ptrace_scope() -> int:
    scope_file = Path("/proc/sys/kernel/yama/ptrace_scope")
    return int(scope_file.read_text()) if scope_file.exists() else 0


@pytest.mark.skipif(os.geteuid() == 0, reason="the child must run unprivileged to be held out")
@pytest.mark.skipif(
    _yama_ptrace_scope() >= 2,
    reason=f"ptrace_scope {_yama_ptrace_scope()} forbids the unprivileged ptracer grant",
)
def test_a_same_user_child_granted_ptracer_permission_cannot_read_the_service_after_hardening(
    tmp_path,
):
    config_file = _written(tmp_path, NO_PROFILE.replace(CLI_SECTION, ""))
    arguments = [READER, ENVIRONMENT_CANARY, MEMORY_CANARY.decode(), str(BOUND_SECONDS // 3)]

    done = subprocess.run(
        [sys.executable, "-c", SERVICE_UNDER_READER, *arguments, str(config_file)],
        capture_output=True,
        text=True,
        timeout=BOUND_SECONDS,
        env={**os.environ, "SERVICE_ENVIRONMENT_CANARY": ENVIRONMENT_CANARY},
    )

    report = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
    assert report.get("control") == {"environ": "match", "mem": "match"}, (done.stdout, done.stderr)
    assert report.get("treatment") == {"environ": "denied", "mem": "denied"}, (
        done.stdout,
        done.stderr,
    )
    assert report["exit"] == 0
