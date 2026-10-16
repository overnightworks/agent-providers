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
from pathlib import Path

import pytest

from agent_providers import spawn
from agent_providers.spawn import ChildProcess, ChildProcessSpecificationError, closed_environment

SOURCE_ROOT = Path(spawn.__file__).parent
SPAWN_OWNER = Path(spawn.__file__)

# Every way this interpreter can turn a command line into a running child.
SPAWN_PRIMITIVES = frozenset(
    {
        "Popen",
        "run",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "subprocess_exec",
        "subprocess_shell",
        "system",
        "popen",
        "posix_spawn",
        "posix_spawnp",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "fork",
        "forkpty",
        "spawn",
        "Process",
        "Pool",
    }
)

SPAWNING_MODULES = frozenset({"subprocess", "os", "asyncio", "pty", "multiprocessing"})


def _imported_primitive_names(tree: ast.Module) -> set[str]:
    """The local names a ``from … import`` bound to a spawn primitive."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.split(".")[0] in SPAWNING_MODULES
        for alias in node.names
        if alias.name in SPAWN_PRIMITIVES
    }


def _spawn_calls(tree: ast.Module) -> list[tuple[str, ast.Call]]:
    """Every call in one module that reaches a process-creation primitive.

    Both call shapes count, and both tests below resolve them the same way:
    the owner module is the one place where the rule still has to hold, so a
    bare ``Popen(command)`` there must not read as anything else.
    """
    imported = _imported_primitive_names(tree)
    calls: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Attribute) and callee.attr in SPAWN_PRIMITIVES:
            calls.append((callee.attr, node))
        elif isinstance(callee, ast.Name) and callee.id in imported:
            calls.append((callee.id, node))
    return calls


def test_only_the_spawn_owner_creates_a_child_process() -> None:
    trespassers = [
        f"{module.relative_to(SOURCE_ROOT)}:{call.lineno} calls {name}"
        for module in sorted(SOURCE_ROOT.rglob("*.py"))
        if module != SPAWN_OWNER
        for name, call in _spawn_calls(ast.parse(module.read_text(encoding="utf-8")))
    ]
    assert trespassers == [], (
        "These call sites start a child outside agent_providers.spawn, so nothing "
        f"makes them describe a complete environment: {trespassers}"
    )


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
    with pytest.raises(ChildProcessSpecificationError, match="absolute binary"):
        ChildProcess(
            binary=Path("claude"),
            arguments=(),
            environment=closed_environment(tmp_path),
            working_directory=tmp_path,
        )


def test_a_child_needs_an_absolute_working_directory(tmp_path: Path) -> None:
    with pytest.raises(ChildProcessSpecificationError, match="absolute working directory"):
        ChildProcess(
            binary=Path("/usr/bin/claude"),
            arguments=(),
            environment=closed_environment(tmp_path),
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
