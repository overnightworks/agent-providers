"""Autouse fixtures for the ``agent_providers`` library tests.

These install a library-only provider runtime and the probe patches every test
needs, so the suite proves the provider layer against values the package owns —
never a host's own — with no host application on the path.

``agent_providers.config.configure()`` refuses a second, differing
installation, so ``_configure_agent_provider_runtime`` installs one sample
runtime and resets it after each test.
"""

from __future__ import annotations

import asyncio
import ctypes
import gc
import logging
import sys
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anchored_kernel import CensusRecorder
from anchored_leaders import (
    ALL_PHASES,
    Leaders,
    asyncio_errors,
    spawn_warnings,
    unclosed_resources,
)
from anchored_support import ChildCleanup, ZombieAnchor, child_cleanup_scope, zombie_anchor_session
from provider_test_support import MCP_TOOL_NAMES, close_fake_cli_pipes

from agent_providers import procfs
from agent_providers.config import (
    ClaudeCliRuntime,
    GrokCliRuntime,
    GrokTurnRuntime,
    McpServerSpec,
    ProviderRuntimeConfig,
    TurnRuntimeConfig,
    configure,
    reset_config,
)

PR_GET_DUMPABLE = 3

_SAMPLE_ROOT = Path("/tmp/agent-providers-tests")

_SAMPLE_MCP_SERVER = McpServerSpec(
    name="notes",
    command="/usr/bin/python3",
    args=("-m", "notes_mcp_server"),
    user_id_environment_variable="NOTES_MCP_USER_ID",
    config_file_prefix="notes-mcp-",
    tool_names=MCP_TOOL_NAMES,
)

_SAMPLE_TURNS = TurnRuntimeConfig(
    codex_code_mode_host_binary=_SAMPLE_ROOT / "codex" / "code-mode-host",
    codex_resources_directory=_SAMPLE_ROOT / "codex" / "resources",
    codex_max_concurrent_processes=4,
    codex_max_concurrent_image_runs=2,
    mcp_server=_SAMPLE_MCP_SERVER,
    grok=GrokTurnRuntime(
        prompt_file_prefix="notes-cli-prompt-",
        prompt_file_placeholder="<notes-private-prompt>",
    ),
)

_SAMPLE_RUNTIME = ProviderRuntimeConfig(
    # An absolute path that exists on every host, so binary resolution is a
    # real answer in the suite rather than a patched one.
    codex_cli_binary="/bin/sh",
    cli_binary_search_path=(Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin")),
    codex_cli_auth_file=_SAMPLE_ROOT / "codex" / "auth.json",
    cli_working_directory_root=_SAMPLE_ROOT / "working-root",
    claude=ClaudeCliRuntime(
        binary="/bin/sh", token_file=_SAMPLE_ROOT / "claude" / "credentials.json",
    ),
    grok=GrokCliRuntime(binary="/bin/sh", token_file=_SAMPLE_ROOT / "grok" / "auth.json"),
    turns=_SAMPLE_TURNS,
)


@pytest.fixture(autouse=True)
def _configure_agent_provider_runtime(tmp_path: Path):
    """Install the library's own sample runtime for every test, then drop it."""
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    claude_auth = credentials / "claude.json"
    grok_auth = credentials / "grok.json"
    codex_auth = credentials / "codex.json"
    for auth_file in (claude_auth, grok_auth, codex_auth):
        auth_file.write_text("{}")
    configure(
        _SAMPLE_RUNTIME.model_copy(
            update={
                "claude": _SAMPLE_RUNTIME.claude.model_copy(update={"token_file": claude_auth}),
                "grok": _SAMPLE_RUNTIME.grok.model_copy(update={"token_file": grok_auth}),
                "codex_cli_auth_file": codex_auth,
                "cli_working_directory_root": tmp_path,
            }
        )
    )
    yield
    reset_config()


def _worker_dumpable_flag() -> int:
    prctl = ctypes.CDLL(None).prctl
    prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    return prctl(PR_GET_DUMPABLE, 0, 0, 0, 0)


@pytest.fixture(scope="session")
def zombie_anchor():
    yield from zombie_anchor_session()


@pytest.fixture
def child_cleanup(zombie_anchor: ZombieAnchor):
    with child_cleanup_scope(zombie_anchor) as cleanup:
        yield cleanup


@pytest.fixture
def leaders(tmp_path: Path, child_cleanup: ChildCleanup) -> Leaders:
    return Leaders(tmp_path, child_cleanup)


@pytest.fixture
def thread_failures(monkeypatch: pytest.MonkeyPatch) -> list[threading.ExceptHookArgs]:
    forwarded: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", forwarded.append)
    return forwarded


@pytest.fixture(autouse=True)
def _service_hardening_never_touches_the_kernel():
    """Run the real hardening policy as an unprivileged user over a no-op kernel call.

    Uses ``patch`` rather than ``monkeypatch``: a shared ``monkeypatch`` set up this early would
    be undone after the other autouse patches and restore their mocks into later tests.
    """
    with (
        patch("agent_service.process_hardening.set_not_dumpable", return_value=None),
        patch("agent_service.process_hardening.effective_user_id", return_value=1000),
    ):
        yield
    if sys.platform == "linux":
        assert _worker_dumpable_flag() == 1, "a test made the pytest worker non-dumpable"


@pytest.fixture(autouse=True)
def _isolate_codex_process_pool():
    """Keep each test independent of Codex CLI process reservations."""
    import agent_providers.codex.pool as pool_mod

    pool_mod._process_pool = None
    yield
    pool_mod._process_pool = None


@pytest.fixture(autouse=True)
def _no_claude_cli_tool_surface_probe():
    """Never let a test spawn the real Claude CLI to read its tool surface."""
    fake_binary = Path("/usr/bin/claude")
    with (
        patch(
            "agent_providers.claude.provider.verify_cli_tool_surface",
            AsyncMock(return_value=fake_binary),
        ),
        patch(
            "agent_providers.claude.provider.averify_no_builtin_cli_tools",
            AsyncMock(return_value=fake_binary),
        ),
        patch(
            "agent_providers.claude.provider.verify_no_builtin_cli_tools",
            MagicMock(return_value=fake_binary),
        ),
    ):
        yield


@pytest.fixture(scope="module", autouse=True)
def _close_fake_cli_pipes():
    """Close the pipe-backed fake Claude CLI streams after each test module."""
    yield
    close_fake_cli_pipes()


@pytest.fixture
def census_calls(monkeypatch: pytest.MonkeyPatch) -> CensusRecorder:
    recorder = CensusRecorder()
    monkeypatch.setattr(procfs, "group_census", recorder)
    return recorder


@pytest.fixture
def door_runner(
    anchored_door_ends_clean: None, child_cleanup: ChildCleanup
) -> Iterator[asyncio.Runner]:
    with asyncio.Runner() as runner:
        runner.get_loop()
        yield runner


@pytest.fixture
def expected_spawn_warnings() -> list[str]:
    return []


@pytest.fixture
def expected_resource_warnings() -> list[str]:
    return []


@pytest.fixture
def anchored_door_ends_clean(
    thread_failures: list[threading.ExceptHookArgs],
    caplog: pytest.LogCaptureFixture,
    expected_spawn_warnings: list[str],
    expected_resource_warnings: list[str],
) -> Iterator[None]:
    with caplog.at_level(logging.WARNING), warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always", ResourceWarning)
        yield
        gc.collect()
    assert sorted(unclosed_resources(raised)) == sorted(expected_resource_warnings)
    assert thread_failures == []
    assert asyncio_errors(caplog, ALL_PHASES) == []
    assert spawn_warnings(caplog, ALL_PHASES) == expected_spawn_warnings
