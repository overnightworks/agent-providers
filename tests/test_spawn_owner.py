"""Every child process in this library is created by one owner.

The rule is checked at *call* level, not as an import contract. An import
contract cannot see it: ``Popen(argv, env=None)`` inherits the whole host
environment without naming ``os.environ`` once, and ``subprocess`` is a
standard-library module every layer is free to import for its exception
types. ``.importlinter`` therefore cannot express this, and neither can a
grep for ``os.environ``.

What the call site does show is *who spawns*. If the only source line that
reaches a spawn primitive lives in :mod:`agent_providers.spawn`, then every
child in this library is described by a :class:`ChildProcess` first — and
that value refuses a relative binary, a missing working directory and an
unset ``HOME`` at construction, whatever the caller forgot.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

from agent_providers import spawn
from agent_providers.spawn import (
    ChildProcess,
    ChildProcessSpecificationError,
    closed_environment,
    run_capturing,
)

REPOSITORY_ROOT = Path(spawn.__file__).parents[2]
SOURCE_ROOTS = (
    REPOSITORY_ROOT / "src" / "agent_providers",
    *sorted((REPOSITORY_ROOT / "packages").glob("*/src")),
)
SPAWN_OWNER = Path(spawn.__file__)

# Every way this interpreter can turn a command line into a running child,
# named by the module that defines it.
SPAWN_PRIMITIVES = frozenset(
    {
        "subprocess.Popen",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "asyncio.subprocess.create_subprocess_exec",
        "asyncio.subprocess.create_subprocess_shell",
        "os.system",
        "os.popen",
        "os.fork",
        "os.forkpty",
        "pty.spawn",
        "pty.fork",
        "multiprocessing.Process",
        "multiprocessing.Pool",
    }
)
SPAWN_PRIMITIVE_PREFIXES = ("os.exec", "os.spawn", "os.posix_spawn")
EVENT_LOOP_SPAWN_METHODS = frozenset({"subprocess_exec", "subprocess_shell"})


def _import_bindings(tree: ast.Module) -> dict[str, str]:
    """The qualified name each locally bound import name stands for."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                bindings[bound] = alias.name if alias.asname else bound
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bindings


def _qualified_callee(callee: ast.expr, bindings: dict[str, str]) -> str | None:
    """Resolve ``alias.attr.attr`` through the imports; None for anything else."""
    attributes: list[str] = []
    while isinstance(callee, ast.Attribute):
        attributes.append(callee.attr)
        callee = callee.value
    if not isinstance(callee, ast.Name) or callee.id not in bindings:
        return None
    return ".".join([bindings[callee.id], *reversed(attributes)])


def _is_spawn_primitive(qualified_name: str) -> bool:
    return qualified_name in SPAWN_PRIMITIVES or qualified_name.startswith(
        SPAWN_PRIMITIVE_PREFIXES,
    )


def _spawn_calls(tree: ast.Module) -> list[tuple[str, ast.Call]]:
    """Every call in one module that reaches a process-creation primitive.

    The owner module is the one place where the rule still has to hold, so a
    bare ``Popen(command)`` there must not read as anything else.
    """
    bindings = _import_bindings(tree)
    calls: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        qualified = _qualified_callee(node.func, bindings)
        if qualified is not None and _is_spawn_primitive(qualified):
            calls.append((qualified, node))
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in EVENT_LOOP_SPAWN_METHODS
        ):
            calls.append((node.func.attr, node))
    return calls


def test_only_the_spawn_owner_creates_a_child_process() -> None:
    trespassers = [
        f"{module.relative_to(REPOSITORY_ROOT)}:{call.lineno} calls {name}"
        for source_root in SOURCE_ROOTS
        for module in sorted(source_root.rglob("*.py"))
        if module != SPAWN_OWNER
        for name, call in _spawn_calls(ast.parse(module.read_text(encoding="utf-8")))
    ]
    assert trespassers == [], (
        "These call sites start a child outside agent_providers.spawn, so nothing "
        f"makes them describe a complete environment: {trespassers}"
    )


def test_the_call_site_check_scans_the_client_and_the_service() -> None:
    scanned = {root.parent.name for root in SOURCE_ROOTS}

    assert {"agent-client", "agent-service"} <= scanned


@pytest.mark.parametrize(
    ("source", "spawned"),
    [
        ("import subprocess as sp\nsp.run(['x'])", "subprocess.run"),
        ("from os import execv as replace_process\nreplace_process('x', [])", "os.execv"),
        (
            "import asyncio as aio\naio.create_subprocess_exec('x')",
            "asyncio.create_subprocess_exec",
        ),
        ("from multiprocessing import Process as Worker\nWorker()", "multiprocessing.Process"),
        ("import pty\npty.spawn(['x'])", "pty.spawn"),
        ("import os\nos.posix_spawn('x', [], {})", "os.posix_spawn"),
    ],
)
def test_the_call_site_check_finds_a_spawn_under_any_import_alias(
    source: str, spawned: str,
) -> None:
    assert [name for name, _ in _spawn_calls(ast.parse(source))] == [spawned]


def test_the_call_site_check_ignores_run_calls_that_start_no_child() -> None:
    source = "import uvicorn\nuvicorn.run(app)\nrunner.run()\n"

    assert _spawn_calls(ast.parse(source)) == []


def test_the_spawn_owner_hands_every_primitive_an_explicit_environment() -> None:
    """No spawn in the owner may fall back to the inherited environment."""
    tree = ast.parse(SPAWN_OWNER.read_text(encoding="utf-8"))
    incomplete = [
        f"{name} at line {call.lineno}"
        for name, call in _spawn_calls(tree)
        if not _passes_explicit(call, "env") or not _passes_explicit(call, "cwd")
    ]
    assert incomplete == [], f"These spawns leave env or cwd to the host process: {incomplete}"


def _passes_explicit(call: ast.Call, keyword: str) -> bool:
    argument = next((kw.value for kw in call.keywords if kw.arg == keyword), None)
    return argument is not None and not (
        isinstance(argument, ast.Constant) and argument.value is None
    )


def test_a_child_needs_an_absolute_binary(tmp_path: Path) -> None:
    environment = closed_environment(tmp_path)
    with pytest.raises(ChildProcessSpecificationError, match="absolute binary"):
        ChildProcess(
            binary=Path("claude"),
            arguments=(),
            environment=environment,
            working_directory=tmp_path,
        )


def test_a_child_needs_an_absolute_working_directory(tmp_path: Path) -> None:
    environment = closed_environment(tmp_path)
    binary = Path("/usr/bin/claude")
    with pytest.raises(ChildProcessSpecificationError, match="absolute working directory"):
        ChildProcess(
            binary=binary,
            arguments=(),
            environment=environment,
            working_directory=Path("work"),
        )


def test_a_child_needs_home_set_rather_than_absent(tmp_path: Path) -> None:
    with pytest.raises(ChildProcessSpecificationError, match="HOME"):
        ChildProcess(
            binary=Path("/usr/bin/claude"),
            arguments=(),
            environment={"PATH": "/usr/bin"},
            working_directory=tmp_path,
        )


def test_a_described_child_cannot_be_changed_through_its_environment(tmp_path: Path) -> None:
    child = ChildProcess(
        binary=Path("/usr/bin/claude"),
        arguments=("-p",),
        environment=closed_environment(tmp_path),
        working_directory=tmp_path,
    )
    with pytest.raises(TypeError):
        child.environment["HOME"] = "/root"  # type: ignore[index]


def test_a_closed_environment_carries_no_secret_from_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "value-the-child-must-not-see")
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))

    environment = closed_environment(tmp_path / "private", GROK_HOME=str(tmp_path / "private"))

    assert "ANTHROPIC_API_KEY" not in environment
    assert environment["HOME"] == str(tmp_path / "private")
    assert environment["GROK_HOME"] == str(tmp_path / "private")


def test_a_closed_environment_points_temporary_files_at_the_private_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path / "operator-tmp"))

    environment = closed_environment(tmp_path / "private")

    assert environment["TMPDIR"] == str(tmp_path / "private")


def test_a_closed_environment_carries_the_machine_settings_a_cli_needs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/certs/ca-bundle.crt")

    environment = closed_environment(tmp_path)

    assert environment["HTTPS_PROXY"] == "http://proxy.invalid:3128"
    assert environment["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-bundle.crt"
    assert environment["PATH"]


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc/self/fd")
def test_a_child_inherits_no_descriptor_the_parent_holds_open(tmp_path: Path) -> None:
    probe = (
        "import os, sys\n"
        "print(os.path.exists(f'/proc/self/fd/{sys.argv[1]}'))\n"
    )
    read_end, write_end = os.pipe()
    os.set_inheritable(read_end, True)
    try:
        completed = run_capturing(
            ChildProcess(
                binary=Path(sys.executable),
                arguments=("-c", probe, str(read_end)),
                environment=closed_environment(tmp_path),
                working_directory=tmp_path,
            ),
            stdin_text="",
            timeout_seconds=30,
        )
    finally:
        os.close(read_end)
        os.close(write_end)

    assert completed.stdout.strip() == "False"
