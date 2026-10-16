"""Every child process in this library is created by one owner.

The rule is an import-and-name policy, not an import contract and not a call
analysis. An import contract cannot see it: ``Popen(argv, env=None)`` inherits
the whole host environment without naming ``os.environ`` once. A call analysis
would have to rebuild Python's scoping. The policy needs neither: outside
:mod:`agent_providers.spawn` nothing imports a spawning module (``subprocess``,
``pty``, ``multiprocessing``, ``posix``, ``_posixsubprocess``, AnyIO's
``to_process``, asyncio's ``base_subprocess`` and ``unix_events``,
``concurrent.futures.process``) in any form or scope, nothing re-imports one
under its name from elsewhere, nothing star-imports from anywhere, nothing
imports a spawn name from ``os``, ``asyncio`` or AnyIO, and nothing reads a spawn
name as an attribute: not a spawning module under any spelling of its owner
(``spawn.subprocess``, ``asyncio.subprocess``), not a spawning ``os`` name read
from ``os``, from an alias of it or from any ``<x>.os``, not an AnyIO
``run_process`` or ``open_process``. The one name outside :mod:`agent_providers.spawn`
that types a child is ``spawn.AsyncPipedProcess``. So every child is described
by a :class:`ChildProcess` first, and that value refuses a relative binary, a
missing working directory and an unset ``HOME``.

Accepted limit (head ruling 16.10.2026; owner: review): dynamic indirection
(``getattr``, ``importlib``, ``__import__``, ``sys.modules``); aliases created
inside functions, or by non-import bindings other than a module-level assignment
of ``os``; rebinding one module under another module's name; introspection of
the exported type aliases (``__value__``); stdlib facades that start children
internally (``webbrowser``, ``socketserver.Forking*``, ``ctypes.util``,
``imaplib.IMAP4_stream``).
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_providers import spawn
from agent_providers.spawn import (
    ChildProcess,
    ChildProcessSpecificationError,
    closed_environment,
    open_async_pipes,
    open_pipes,
    run_capturing,
)

REPOSITORY_ROOT = Path(spawn.__file__).parents[2]
SOURCE_ROOTS = (
    REPOSITORY_ROOT / "src" / "agent_providers",
    *sorted((REPOSITORY_ROOT / "packages").glob("*/src")),
)
SPAWN_OWNER = Path(spawn.__file__)

SPAWNING_MODULE_NAMES = frozenset({
    "subprocess",
    "pty",
    "multiprocessing",
    "posix",
    "_posixsubprocess",
    "to_process",
    "base_subprocess",
    "unix_events",
})
SPAWNING_MODULES = SPAWNING_MODULE_NAMES | {"concurrent.futures.process"}
SPAWNING_NAMES = frozenset({"ProcessPoolExecutor", "run_process", "open_process"})
SPAWNING_ASYNCIO_NAMES = frozenset({"create_subprocess_exec", "create_subprocess_shell"})
SPAWNING_ATTRIBUTES = SPAWNING_ASYNCIO_NAMES | SPAWNING_NAMES | SPAWNING_MODULE_NAMES | {
    "subprocess_exec",
    "subprocess_shell",
    "_make_subprocess_transport",
}
SPAWNING_OS_PREFIXES = ("exec", "spawn", "posix_spawn")
SPAWNING_OS_NAMES = frozenset({"fork", "forkpty", "system", "popen"})


def _is_spawning_os_name(name: str) -> bool:
    return name in SPAWNING_OS_NAMES or name.startswith(SPAWNING_OS_PREFIXES)


def _names_a_spawning_module(module: str) -> bool:
    return bool(SPAWNING_MODULE_NAMES.intersection(module.split("."))) or any(
        module == package or module.startswith(f"{package}.") for package in SPAWNING_MODULES
    )


def _imports_a_spawn_route(node: ast.ImportFrom, name: str) -> bool:
    if name == "*" or name in SPAWNING_MODULE_NAMES or name in SPAWNING_NAMES:
        return True
    if node.level or not node.module:
        return False
    module = node.module
    if _names_a_spawning_module(module) or _names_a_spawning_module(f"{module}.{name}"):
        return True
    if module == "os":
        return _is_spawning_os_name(name)
    return module.split(".")[0] == "asyncio" and name in SPAWNING_ASYNCIO_NAMES


def _is_os_read_from_attribute(node: ast.Attribute) -> bool:
    return (
        isinstance(node.value, ast.Attribute)
        and node.value.attr == "os"
        and _is_spawning_os_name(node.attr)
    )


def _is_os_reference(value: ast.expr | None, os_names: set[str]) -> bool:
    return (isinstance(value, ast.Name) and value.id in os_names) or (
        isinstance(value, ast.Attribute) and value.attr == "os"
    )


def _os_names(tree: ast.Module) -> set[str]:
    names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
        if alias.name == "os"
    } | {"os"}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and _is_os_reference(statement.value, names):
            names.update(t.id for t in statement.targets if isinstance(t, ast.Name))
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and _is_os_reference(statement.value, names)
        ):
            names.add(statement.target.id)
    return names


def _spawn_policy_violations(tree: ast.Module) -> list[str]:
    """What in one module breaks the spawn policy, each as ``line: what``."""
    os_names = _os_names(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations += [
                f"{node.lineno}: import {alias.name}"
                for alias in node.names
                if _names_a_spawning_module(alias.name)
            ]
        elif isinstance(node, ast.ImportFrom):
            violations += [
                f"{node.lineno}: from {node.module} import {alias.name}"
                for alias in node.names
                if _imports_a_spawn_route(node, alias.name)
            ]
        elif isinstance(node, ast.Attribute):
            if node.attr in SPAWNING_ATTRIBUTES:
                violations.append(f"{node.lineno}: .{node.attr}")
            elif _is_os_read_from_attribute(node) or (
                isinstance(node.value, ast.Name)
                and node.value.id in os_names
                and _is_spawning_os_name(node.attr)
            ):
                violations.append(f"{node.lineno}: os.{node.attr}")
    return violations


def test_only_the_spawn_owner_creates_a_child_process() -> None:
    trespassers = [
        f"{module.relative_to(REPOSITORY_ROOT)}:{violation}"
        for source_root in SOURCE_ROOTS
        for module in sorted(source_root.rglob("*.py"))
        if module != SPAWN_OWNER
        for violation in _spawn_policy_violations(
            ast.parse(module.read_text(encoding="utf-8")),
        )
    ]
    assert trespassers == [], (
        "These sites reach a process-creation primitive outside agent_providers.spawn, "
        f"so nothing makes them describe a complete environment: {trespassers}"
    )


def test_the_call_site_check_scans_the_client_and_the_service() -> None:
    scanned = {root.parent.name for root in SOURCE_ROOTS}

    assert {"agent-client", "agent-service"} <= scanned


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import subprocess as sp",
        "import subprocess.extra",
        "from subprocess import Popen",
        "from subprocess import TimeoutExpired as Expired",
        "import pty",
        "from pty import spawn",
        "import multiprocessing",
        "from multiprocessing.pool import Pool",
        "def helper():\n    import subprocess",
        "class Holder:\n    from subprocess import run",
        "async def helper():\n    from multiprocessing import Process",
        "from os import execv",
        "from os import fork as split",
        "from os import posix_spawnp",
        "from asyncio import create_subprocess_exec",
        "from asyncio import create_subprocess_shell as shell",
        "import asyncio\nasyncio.create_subprocess_exec('x')",
        "import asyncio as aio\naio.subprocess.create_subprocess_shell('x')",
        "loop.subprocess_exec(factory, 'x')",
        "loop.subprocess_shell(factory, 'x')",
        "import os\nos.system('x')",
        "import os\nos.popen('x')",
        "import os\nos.fork()",
        "import os\nos.forkpty()",
        "import os\nos.execvp('x', [])",
        "import os\nos.spawnl(0, 'x')",
        "import os\nos.posix_spawn('x', [], {})",
        "import os as operating\noperating.system('x')",
        "def helper():\n    import os\n    os.fork()",
        "from os import *\nsystem('x')",
        "from asyncio import *",
        "from subprocess import *",
        "from posix import *",
        "from multiprocessing import *",
        "from concurrent.futures import *",
        "from asyncio.subprocess import create_subprocess_exec",
        "from asyncio.subprocess import create_subprocess_shell as shell",
        "import concurrent.futures\nconcurrent.futures.ProcessPoolExecutor()",
        "from concurrent.futures import ProcessPoolExecutor",
        "import concurrent.futures.process",
        "from concurrent.futures import process",
        "from concurrent.futures.process import _ExecutorManagerThread",
        "from agent_providers.spawn import subprocess",
        "from asyncio import subprocess",
        "from . import subprocess",
        "import posix",
        "from posix import fork",
        "import _posixsubprocess",
        "if True:\n    import os as operating\noperating.system('x')",
        "try:\n    import os as operating\nexcept ImportError:\n    pass\noperating.fork()",
        "from agent_providers import os as operating\noperating.execv('x', [])",
        "from agent_providers import spawn\nspawn.subprocess.Popen(['x'])",
        "import agent_providers.spawn\nagent_providers.spawn.subprocess.run(['x'])",
        "import agent_providers.spawn as s\ns.os.system('x')",
        "from agent_providers import process\nprocess.os.system('x')",
        "from agent_providers.spawn import *",
        "from json import *",
        "from . import *",
        "import asyncio\nasyncio.subprocess.subprocess.Popen(['x'])",
        "from anyio import run_process",
        "from anyio import open_process as opener",
        "import anyio\nanyio.run_process(['x'])",
        "import anyio\nanyio.open_process(['x'])",
        "import asyncio\ndef f(p: asyncio.subprocess.Process) -> None: ...",
        "from agent_providers import spawn as asyncio\nasyncio.subprocess.Popen(['x'])",
        "import agent_providers.spawn as asyncio\nasyncio.subprocess.Popen(['x'])",
        "import asyncio.subprocess as asyncio\nasyncio.create_subprocess_exec('x')",
        "import anyio.to_process",
        "from anyio import to_process",
        "from anyio.to_process import run_sync",
        "import anyio\nanyio.to_process.run_sync(print)",
        "import anyio.to_process as workers\nworkers.run_sync(print)",
        "import os\noperating = os\noperating.system('x')",
        "import os\nfirst = os\nsecond = first\nsecond.system('x')",
        "import os\noperating: object = os\noperating.system('x')",
        "from agent_providers import process\no = process.os\no.system('x')",
        "import asyncio.base_subprocess",
        "import asyncio.unix_events",
        "from asyncio import unix_events",
        "from asyncio.base_subprocess import BaseSubprocessTransport",
        "import asyncio\nasyncio.unix_events.SafeChildWatcher()",
        "loop._make_subprocess_transport(factory, ['x'])",
        "import x\nx.pty.fork()",
        "import x\nx.multiprocessing.Process()",
        "import x\nx.posix.fork()",
        "import x\nx._posixsubprocess.fork_exec()",
    ],
)
def test_the_spawn_policy_flags_every_way_to_reach_a_process_primitive(source: str) -> None:
    assert _spawn_policy_violations(ast.parse(source)) != []


@pytest.mark.parametrize(
    "source",
    [
        "import uvicorn\nuvicorn.run(app)",
        "runner.run()",
        "import platform\nplatform.system()",
        "import asyncio\nasyncio.run(main())",
        "import os\nos.getpid()\nos.environ",
        "from os import getpid",
        "from agent_providers.spawn import PipedProcess",
        "from agent_providers.spawn import AsyncPipedProcess",
        "from agent_providers import spawn\ndef f(p: spawn.AsyncPipedProcess) -> None: ...",
        "import os\noperating = os\noperating.getpid()",
        "from concurrent.futures import ThreadPoolExecutor",
        "import concurrent.futures\nconcurrent.futures.Future()",
        "import os.path\nos.path.join('a', 'b')",
    ],
)
def test_the_spawn_policy_accepts_code_that_starts_no_child(source: str) -> None:
    assert _spawn_policy_violations(ast.parse(source)) == []


def test_the_spawn_owners_process_type_starts_nothing() -> None:
    with pytest.raises(TypeError):
        spawn.PipedProcess(["/bin/true"])


OWNER_SPAWN_CALLS = frozenset({"Popen", "run", "create_subprocess_exec"})


def test_the_spawn_owner_hands_every_primitive_an_explicit_environment() -> None:
    """No spawn in the owner may fall back to the inherited environment."""
    tree = ast.parse(SPAWN_OWNER.read_text(encoding="utf-8"))
    spawns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in OWNER_SPAWN_CALLS
    ]
    incomplete = [
        f"{call.func.attr} at line {call.lineno}"
        for call in spawns
        if not _passes_explicit(call, "env") or not _passes_explicit(call, "cwd")
    ]
    assert spawns
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


def _stdout_of_run_capturing(child: ChildProcess) -> str:
    return run_capturing(child, stdin_text="", timeout_seconds=30).stdout


def _stdout_of_open_pipes(child: ChildProcess) -> str:
    process = open_pipes(child, stderr="devnull")
    stdout, _ = process.communicate(b"")
    return stdout.decode()


def _stdout_of_open_async_pipes(child: ChildProcess) -> str:
    async def run() -> str:
        process = await open_async_pipes(child)
        stdout, _ = await process.communicate(b"")
        return stdout.decode()

    return asyncio.run(run())


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc/self/fd")
@pytest.mark.parametrize(
    "stdout_of",
    [_stdout_of_run_capturing, _stdout_of_open_pipes, _stdout_of_open_async_pipes],
    ids=["run_capturing", "open_pipes", "open_async_pipes"],
)
def test_a_child_inherits_no_descriptor_the_parent_holds_open(
    tmp_path: Path, stdout_of: Callable[[ChildProcess], str],
) -> None:
    probe = (
        "import os, sys\n"
        "print(os.path.exists(f'/proc/self/fd/{sys.argv[1]}'))\n"
    )
    read_end, write_end = os.pipe()
    os.set_inheritable(read_end, True)
    try:
        output = stdout_of(
            ChildProcess(
                binary=Path(sys.executable),
                arguments=("-c", probe, str(read_end)),
                environment=closed_environment(tmp_path),
                working_directory=tmp_path,
            ),
        )
    finally:
        os.close(read_end)
        os.close(write_end)

    assert output.strip() == "False"
