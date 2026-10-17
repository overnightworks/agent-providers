"""Library-local test support for the ``agent_providers`` package tests.

A sibling ``conftest.py`` in this directory would make ``from conftest import``
ambiguous under pytest's prepend import mode — the root ``tests/conftest.py``
and this package's own ``conftest.py`` both import as ``conftest``. The lib
tests therefore reach their shared helpers here, by basename, which resolves
the same way wherever the suite runs.

Self-contained by design: it imports only ``agent_providers`` and the standard
library, never a host application. The deployment facts below are the package's
own sample — a notes host with its twelve tools — not
an import of the application's constants, so the tests prove the provider layer
against values this package owns.
"""

from __future__ import annotations

import asyncio
import errno
import gc
import json
import logging
import os
import shlex
import stat
import tempfile
import threading
import traceback
import types
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_client.tools import ToolDeclaration
from agent_providers import process
from agent_providers.catalog import ProviderRoute
from agent_providers.claude import provider as claude_provider
from agent_providers.config import (
    ProviderRuntimeConfig,
    configure,
    current_config,
    reset_config,
)
from agent_providers.dispatch import stream_turn
from agent_providers.errors import (
    ProviderUnavailableError,
    SafeRouteReasonCode,
    TextToolCallRejectedError,
    ToolProtocolRejectionCode,
    normalize_route_failure,
)
from agent_providers.events import AssistantTextEvent, StreamEvent
from agent_providers.process import CliRunOutcome, CliRunReason
from agent_providers.spawn import ChildProcess, closed_environment
from agent_providers.text_tool_protocol import TextToolRejectionRule
from agent_providers.tool_loop import (
    InitialTurn,
    ToolOutcome,
    ToolResult,
    ToolResultBatch,
)
from agent_providers.tools import ToolCatalog, ToolProtocolMarkup

_fake_cli_processes: list[MagicMock] = []

CANARY_ACCESS_TOKEN = "canary-access-token"
CANARY_ID_TOKEN = "canary-id-token"
A_CODEX_LOGIN_WITH_INVALID_UTF8 = b'{"auth_mode": "\xff"}'


def a_codex_login(*, tokens: dict[str, Any] | None = None, **top_level: Any) -> bytes:
    document = {
        "auth_mode": "chatgpt",
        "last_refresh": "2026-09-04T19:20:00Z",
        "tokens": {
            "access_token": CANARY_ACCESS_TOKEN,
            "id_token": CANARY_ID_TOKEN,
            "account_id": "account",
            "refresh_token": "canary-refresh-token",
            **(tokens or {}),
        },
        **top_level,
    }
    return json.dumps(document).encode()


CREDENTIAL_FILE_BOUND_BYTES: Final = 256 * 1024


def a_fifo(directory: Path) -> Path:
    path = directory / "source"
    os.mkfifo(path)
    return path


def a_directory(directory: Path) -> Path:
    path = directory / "source"
    path.mkdir()
    return path


def a_symlink_to_a_login(directory: Path) -> Path:
    target = directory / "target"
    target.write_bytes(a_codex_login())
    link = directory / "source"
    link.symlink_to(target)
    return link


def a_dangling_symlink(directory: Path) -> Path:
    link = directory / "source"
    link.symlink_to(directory / "gone")
    return link


def a_usable_login_over_the_bound(directory: Path) -> Path:
    login = a_codex_login()
    path = directory / "source"
    path.write_bytes(login + b" " * (CREDENTIAL_FILE_BOUND_BYTES + 1 - len(login)))
    return path


DESTINATION_FAULTS: Final = ("write", "fchmod", "close", "close-after-failed-write")


@contextmanager
def a_credential_destination_that_fails(fault: str) -> Iterator[list[int]]:
    """Fail one call on the descriptor of the file a credential copy is created in.

    The destination is the descriptor ``os.open`` returns for an ``O_CREAT``
    open; every other descriptor passes through. A failing ``os.close`` closes
    the descriptor for real before it raises, as Linux does. It yields every
    close made on a destination descriptor, so a test can assert exactly one.
    """
    destinations: set[int] = set()
    closed_destinations: set[int] = set()
    closes_of_destinations: list[int] = []
    real_open, real_write, real_fchmod, real_close = os.open, os.write, os.fchmod, os.close

    def open_destination(path, flags, *arguments, **keywords):
        descriptor = real_open(path, flags, *arguments, **keywords)
        closed_destinations.discard(descriptor)
        if flags & os.O_CREAT:
            destinations.add(descriptor)
        return descriptor

    def write(descriptor, data):
        if descriptor in destinations and fault in ("write", "close-after-failed-write"):
            raise OSError(errno.EIO, "synthetic write failure")
        return real_write(descriptor, data)

    def fchmod(descriptor, mode):
        if descriptor in destinations and fault == "fchmod":
            raise OSError(errno.EIO, "synthetic fchmod failure")
        real_fchmod(descriptor, mode)

    def close(descriptor):
        was_destination = descriptor in destinations
        if was_destination or descriptor in closed_destinations:
            closes_of_destinations.append(descriptor)
        destinations.discard(descriptor)
        if was_destination:
            closed_destinations.add(descriptor)
        real_close(descriptor)
        if was_destination and fault in ("close", "close-after-failed-write"):
            raise OSError(errno.EIO, "synthetic close failure")

    with (
        patch.object(os, "open", open_destination),
        patch.object(os, "write", write),
        patch.object(os, "fchmod", fchmod),
        patch.object(os, "close", close),
    ):
        yield closes_of_destinations


def a_path_through_a_regular_file(directory: Path) -> Path:
    (directory / "file").write_text("{}")
    return directory / "file" / "auth.json"

GROK_LOGGED_IN = """You are logged in with grok.com.

Available models:
  * grok-4.6 (default)
  - grok-4.5
  - deepseek-v4-flash
"""
CLAUDE_MODEL_CATALOG_STDOUT = (
    "Current model: `Opus 5 (1M context)` (effort: high)\n"
    "Usage: /model <name>. Available: sonnet, opus, haiku, fable, best, "
    "sonnet[1m], opus[1m], fable[1m], opusplan, default, or a full model ID.\n"
)


def fake_cli_executable(
    directory: Path,
    answers: Mapping[tuple[str, ...], str],
    *,
    calls_log: Path | None = None,
) -> Path:
    """An executable that prints the stored answer for the arguments it is started with.

    Any other arguments exit 1. With ``calls_log`` every start appends one line
    holding its arguments, so a test can count how often the CLI was started.
    """
    answer_files: dict[tuple[str, ...], Path] = {}
    for index, (arguments, stdout) in enumerate(answers.items()):
        answer_files[arguments] = directory / f"answer-{index}"
        answer_files[arguments].write_text(stdout)
    cases = "".join(
        f"  {shlex.quote(' '.join(arguments))}) cat {shlex.quote(str(answer_file))} ;;\n"
        for arguments, answer_file in answer_files.items()
    )
    log_line = f'echo "$*" >> {shlex.quote(str(calls_log))}\n' if calls_log else ""
    executable = directory / "fake-cli"
    executable.write_text(f'#!/bin/sh\n{log_line}case "$*" in\n{cases}  *) exit 1 ;;\nesac\n')
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def assert_private_credential_child(
    child: ChildProcess,
    *,
    root: Path,
    source: Path,
    destination: Path,
) -> Path:
    """Assert the disposable credential home visible at a real spawn boundary."""
    home = Path(child.environment["HOME"])
    copied_credential = home / destination
    assert child.working_directory == home
    assert home.parent == root
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE(copied_credential.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(copied_credential.stat().st_mode) == 0o400
    assert copied_credential.read_bytes() == source.read_bytes()
    return home


def fake_cli_process(
    first_line: bytes | None, *, still_running: bool = False, stdin_blocked: bool = False,
) -> MagicMock:
    """Build a pipe-backed stand-in for the ``subprocess.Popen`` CLI handle."""
    proc = MagicMock()
    proc.pid = 4343
    proc.poll.return_value = None if still_running else 0
    stdin_reader, stdin_writer = os.pipe()
    stdout_reader, stdout_writer = os.pipe()
    stderr_reader, stderr_writer = os.pipe()
    proc.stdin = os.fdopen(stdin_writer, "wb", buffering=0)
    proc.stdout = os.fdopen(stdout_reader, "rb", buffering=0)
    proc.stderr = os.fdopen(stderr_reader, "rb", buffering=0)
    proc._stdin_reader = os.fdopen(stdin_reader, "rb", buffering=0)
    proc._stdout_writer = os.fdopen(stdout_writer, "wb", buffering=0)
    proc._stderr_writer = os.fdopen(stderr_writer, "wb", buffering=0)
    proc._stderr_writer.close()
    if stdin_blocked:
        os.set_blocking(proc.stdin.fileno(), False)
        try:
            while True:
                os.write(proc.stdin.fileno(), b"x" * 65536)
        except BlockingIOError:
            pass
    if first_line is not None:
        proc._stdout_writer.write(first_line)
        proc._stdout_writer.close()
    proc.wait.return_value = None
    _fake_cli_processes.append(proc)
    return proc


def close_fake_cli_pipes() -> None:
    """Close every pipe-backed fake CLI stream built during a test module."""
    for proc in _fake_cli_processes:
        for stream_name in (
            "stdin", "stdout", "stderr", "_stdin_reader", "_stdout_writer", "_stderr_writer",
        ):
            stream = getattr(proc, stream_name)
            if not stream.closed:
                stream.close()
    _fake_cli_processes.clear()


class ClaudeCliStreamReader:
    """The stand-in for the ``asyncio.StreamReader`` of a Claude child pipe."""

    def __init__(
        self, lines: Sequence[bytes], *, limit: int | None = None, stalls: bool = False,
    ) -> None:
        self._lines = list(lines)
        self._limit = limit
        self._stalls = stalls

    async def readline(self) -> bytes:
        if not self._lines:
            return await self._end_of_stream()
        line = self._lines.pop(0)
        if self._limit is not None and len(line) > self._limit:
            raise ValueError("Separator is not found, and chunk exceed the limit")
        return line

    async def read(self, size: int = -1) -> bytes:
        if not self._lines:
            return await self._end_of_stream()
        payload = b"".join(self._lines)
        if size >= 0:
            self._lines = [payload[size:]] if len(payload) > size else []
            return payload[:size]
        self._lines = []
        return payload

    async def _end_of_stream(self) -> bytes:
        if self._stalls:
            await asyncio.get_running_loop().create_future()
        return b""


@dataclass(frozen=True)
class EventGate:
    """Holds one child call open until the test sets ``release``."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def pass_through(self) -> None:
        self.started.set()
        await self.release.wait()


def _gated_answer(
    answer: object, gate: EventGate | None,
) -> Callable[..., Awaitable[object]]:
    async def respond(*_arguments: object) -> object:
        if gate is not None:
            await gate.pass_through()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return respond


CLAUDE_CLI_CHILD_SURFACE: Final = (
    "pid", "returncode", "communicate", "wait", "stdin", "stdout", "stderr",
)
CLAUDE_CLI_STDIN_SURFACE: Final = ("write", "drain", "close")


def a_claude_cli_child(
    *,
    pid: int = 2468,
    returncode: int | None = 0,
    communicate: tuple[bytes, bytes] | BaseException = (b"", b""),
    communicate_gate: EventGate | None = None,
    stream_lines: Sequence[bytes] = (),
    stderr: bytes = b"",
    stdout_limit: int | None = None,
    stdout_stalls: bool = False,
    wait_outcome: int | BaseException = 0,
    wait_gate: EventGate | None = None,
    has_stdin: bool = True,
) -> MagicMock:
    """Build the async twin of ``fake_cli_process``: an ``asyncio.subprocess.Process`` stand-in."""
    child = MagicMock(spec_set=CLAUDE_CLI_CHILD_SURFACE, pid=pid, returncode=returncode)
    child.communicate = AsyncMock(side_effect=_gated_answer(communicate, communicate_gate))
    child.wait = AsyncMock(side_effect=_gated_answer(wait_outcome, wait_gate))
    child.stdout = ClaudeCliStreamReader(
        stream_lines, limit=stdout_limit, stalls=stdout_stalls,
    )
    child.stderr = ClaudeCliStreamReader([stderr] if stderr else [])
    if has_stdin:
        child.stdin = MagicMock(spec_set=CLAUDE_CLI_STDIN_SURFACE)
        child.stdin.drain = AsyncMock()
    else:
        child.stdin = None
    return child


@dataclass(frozen=True)
class ClaudeCliSpawns:
    """What the provider asked the async door to start, in order."""

    children: list[ChildProcess] = field(default_factory=list)
    stream_buffer_limits: list[int | None] = field(default_factory=list)


def install_claude_cli_children(
    monkeypatch: pytest.MonkeyPatch,
    *queued_children: MagicMock,
    factory: Callable[[ChildProcess, int | None], MagicMock] | None = None,
) -> ClaudeCliSpawns:
    """Answer every async Claude spawn with a queued child, then with ``factory``'s.

    The one place that names the async door the provider spawns through.
    """
    spawns = ClaudeCliSpawns()
    waiting = list(queued_children)

    async def open_async_pipes(
        child: ChildProcess, *, stream_buffer_limit: int | None = None,
    ) -> MagicMock:
        spawns.children.append(child)
        spawns.stream_buffer_limits.append(stream_buffer_limit)
        if waiting:
            return waiting.pop(0)
        if factory is None:
            pytest.fail(f"a Claude child was spawned with none queued: {child.command}")
        return factory(child, stream_buffer_limit)

    monkeypatch.setattr(claude_provider, "open_async_pipes", open_async_pipes)
    return spawns


def override_provider_runtime(**deployment_facts: Any) -> None:
    """Replace named provider deployment facts for the current test only.

    Deliberately replaces the installation rather than adding to it, because
    ``configure()`` refuses a differing second value; ``reset_config`` first,
    then install the merged replacement.
    """
    replacement = ProviderRuntimeConfig(
        **{**current_config().model_dump(), **deployment_facts},
    )
    reset_config()
    configure(replacement)


def call_within_deadline(
    function: Callable[..., Any], *arguments: Any, seconds: float = 5.0,
) -> Any:
    """Return what ``function`` returns or raise what it raises; fail if it blocks.

    A call that waits on a FIFO would hang the suite, so it runs in a thread
    joined with a timeout, and a regression fails the test instead.
    """
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = function(*arguments)
        except BaseException as error:
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "the call was still blocked at its deadline"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def shell_child(
    *arguments: str,
    home: Path | None = None,
    working_directory: Path | None = None,
    **provider_variables: str,
) -> ChildProcess:
    """One described child running the system shell, for runner behaviour tests."""
    root = home or Path(tempfile.gettempdir())
    return ChildProcess(
        binary=Path("/bin/sh"),
        arguments=arguments,
        environment=closed_environment(root, **provider_variables),
        working_directory=working_directory or root,
    )


def override_turn_runtime(**turn_facts: Any) -> None:
    """Replace named turn facts for the current test only."""
    turns = current_config().turns
    assert turns is not None, "the sample runtime configures turns"
    override_provider_runtime(turns=turns.model_copy(update=turn_facts))


def use_codex_process_pool(monkeypatch, process_pool) -> None:
    """Give every Codex module that admits a process the same test pool."""
    from agent_providers.codex import image, protocol, transport

    for module in (image, protocol, transport):
        monkeypatch.setattr(module, "get_codex_process_pool", lambda: process_pool)


SECRET_ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "OPENAI_API_KEY",
    "SESSION_SECRET",
    "NOTES_INTERNAL_TOKEN",
    "DATABASE_URL",
    "REDIS_URL",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "HF_TOKEN",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "GRAFANA_USER",
    "GRAFANA_PASSWORD",
)

MCP_TOOL_NAMES: frozenset[str] = frozenset({
    "list_folders",
    "list_notes",
    "search_notes",
    "get_note",
    "get_revision",
    "get_attachment",
    "create_note",
    "update_note_text",
    "update_note_summary",
    "update_note_tags",
    "rename_note",
    "suggest_folder_icon",
})

_STRING = {"type": "string"}
_INT = {"type": "integer"}


def _object(
    properties: dict[str, Any], required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


SAMPLE_TOOL_MARKUP: Final = ToolProtocolMarkup(
    tag_namespace="notes", product_name="Notes"
)

SAMPLE_TOOL_CATALOG: ToolCatalog = ToolCatalog(
    tools=(
        ToolDeclaration(
            name="list_folders",
            description="List all folders owned by the current user.",
            parameters=_object({}),
        ),
        ToolDeclaration(
            name="list_notes",
            description=(
                "List notes. Without folder_id, every owned note; "
                "with folder_id, that folder."
            ),
            parameters=_object({"folder_id": _STRING}),
        ),
        ToolDeclaration(
            name="search_notes",
            description="Search notes by title substring. Limit defaults to 20, max 50.",
            parameters=_object({"query": _STRING, "limit": _INT}, ["query"]),
        ),
        ToolDeclaration(
            name="get_note",
            description="Read the full state of a note.",
            parameters=_object({"note_id": _STRING}, ["note_id"]),
        ),
        ToolDeclaration(
            name="get_revision",
            description="Read a specific revision snapshot of a note.",
            parameters=_object(
                {"note_id": _STRING, "revision_id": _STRING}, ["note_id", "revision_id"],
            ),
        ),
        ToolDeclaration(
            name="get_attachment",
            description="Read an attachment's metadata, size, preview text, and rating.",
            parameters=_object({"attachment_id": _STRING}, ["attachment_id"]),
        ),
        ToolDeclaration(
            name="create_note",
            description="Create a new note in a folder.",
            parameters=_object(
                {
                    "folder_id": _STRING,
                    "title": _STRING,
                    "text": _STRING,
                    "summary": _STRING,
                },
                ["folder_id", "title"],
            ),
        ),
        ToolDeclaration(
            name="update_note_text",
            description="Replace the note's current text.",
            parameters=_object({"note_id": _STRING, "text": _STRING}, ["note_id", "text"]),
        ),
        ToolDeclaration(
            name="update_note_summary",
            description="Replace the note's current summary.",
            parameters=_object({"note_id": _STRING, "summary": _STRING}, ["note_id", "summary"]),
        ),
        ToolDeclaration(
            name="update_note_tags",
            description="Update word_count, language, and/or reading_minutes tags.",
            parameters=_object(
                {
                    "note_id": _STRING,
                    "word_count": _INT,
                    "language": _STRING,
                    "reading_minutes": _INT,
                },
                ["note_id"],
            ),
        ),
        ToolDeclaration(
            name="rename_note",
            description="Rename a note's title.",
            parameters=_object({"note_id": _STRING, "title": _STRING}, ["note_id", "title"]),
        ),
        ToolDeclaration(
            name="suggest_folder_icon",
            description=(
                "Request three folder icon suggestions. "
                "Returns the queued job ID and status."
            ),
            parameters=_object({"folder_id": _STRING}, ["folder_id"]),
        ),
    ),
    markup=SAMPLE_TOOL_MARKUP,
)


def assert_a_multi_result_batch_is_rejected(transport: Any) -> ProviderUnavailableError:
    """Drive a tool transport past its first turn, then hand it two results at once.

    Returns the refusal, so a caller can say what kind of error it was.
    """

    async def reject_batch() -> ProviderUnavailableError:
        assert [item async for item in transport.stream(InitialTurn("system", []))]
        batch = ToolResultBatch((
            ToolResult("one", "1", False),
            ToolResult("two", "2", False),
        ))
        with pytest.raises(ProviderUnavailableError) as raised:
            async for _ in transport.stream(batch):
                pass
        assert raised.value.reason.code is SafeRouteReasonCode.TOOL_PROTOCOL_ERROR
        await transport.aclose()
        return raised.value

    return asyncio.run(reject_batch())


GROK_SESSION_ID: Final = "3e04bf5b-4e1c-4f26-8e1e-2f17c5f6d9cf"


def grok_cli_outcome(
    *, returncode: int = 0, complete: bool = True, stderr: str = "",
) -> CliRunOutcome:
    return CliRunOutcome(
        started=True,
        spawn_error=None,
        returncode=returncode,
        stdout="",
        stderr=stderr,
        complete=complete,
        became_zombie=False,
        reason=CliRunReason.COMPLETE,
    )


def grok_cli_runner(lines, outcome, calls):
    def run_cli_bounded(child, **kwargs):
        calls.append((child.command, kwargs))
        for line in lines:
            assert kwargs["stdout_line_channel"]._send(line)
        kwargs["stdout_line_channel"]._close(outcome)
        return outcome

    return run_cli_bounded


def grok_tool_round_lines(text: str, session_id: str = GROK_SESSION_ID) -> list[bytes]:
    return [
        json.dumps({"type": "text", "data": text}).encode() + b"\n",
        json.dumps({
            "type": "end", "stopReason": "stop", "sessionId": session_id,
        }).encode() + b"\n",
    ]


A_CANARY: Final = "canary-text-3f9c71"
A_TOOL_FAILURE_MESSAGE: Final = "The sample tool failed."
A_CALLER_CANARY: Final = "caller-canary-8b21e5"
PROSE_BEFORE_THE_CALL: Final = "Looking that up.\n"
DEEPLY_NESTED_JSON: Final = "[" * 100_000
CALLER_CONTEXTS: Final = pytest.mark.parametrize(
    "while_the_caller_handles_an_exception",
    [
        pytest.param(False, id="no-active-caller-exception"),
        pytest.param(True, id="caller-handling-a-canary-exception"),
    ],
)


def a_call_to_an_undeclared_tool_carrying(canary: str = A_CANARY) -> str:
    return (
        f"{PROSE_BEFORE_THE_CALL}<notes_tool_call>\n"
        f'{{"name":"delete_everything","arguments":{{"text":"{canary}"}}}}\n'
        "</notes_tool_call>"
    )


def a_call_nested_too_deeply_to_decode() -> str:
    return f"<notes_tool_call>\n{DEEPLY_NESTED_JSON}\n</notes_tool_call>"


class CallerCanaryError(Exception):
    """The error a caller is already handling when a refused call arrives."""


@dataclass(frozen=True)
class DrivenTurn:
    events: list[StreamEvent]
    error: BaseException | None
    executed: list[str]


def drive_turn(
    provider: str,
    *,
    route: ProviderRoute = ProviderRoute.CLI,
    model: str | None | types.EllipsisType = ...,
    correlation_id: str | None = None,
    while_the_caller_handles_an_exception: bool = False,
) -> DrivenTurn:
    """Run a turn through ``stream_turn`` and return what the host sees.

    The error is caught where the host catches it, inside the caller's own
    handler when ``while_the_caller_handles_an_exception`` is set.
    """
    events: list[StreamEvent] = []
    executed: list[str] = []

    def executor(name: str, _arguments: dict[str, Any]) -> ToolOutcome:
        executed.append(name)
        return ToolOutcome("unreachable", False)

    async def turn() -> BaseException | None:
        try:
            async for event in stream_turn(
                provider=provider,
                route=route,
                model=f"{provider}-test" if model is ... else model,
                user_id="user-1",
                system="system",
                messages=[{"role": "user", "content": "hello"}],
                executor=executor,
                catalog=SAMPLE_TOOL_CATALOG,
                correlation_id=correlation_id,
            ):
                events.append(event)
        except ProviderUnavailableError as error:
            return error
        return None

    async def turn_while_handling_an_exception() -> BaseException | None:
        try:
            raise CallerCanaryError(A_CALLER_CANARY)
        except CallerCanaryError:
            return await turn()

    run = turn_while_handling_an_exception if while_the_caller_handles_an_exception else turn
    return DrivenTurn(events, asyncio.run(run()), executed)


def formatted_log_text(records: Sequence[logging.LogRecord]) -> str:
    """Every record as the standard formatter writes it, ``exc_info`` included."""
    formatter = logging.Formatter()
    return "\n".join(formatter.format(record) for record in records)


def assert_the_canary_reaches_only_the_host(
    turn: DrivenTurn,
    records: Sequence[logging.LogRecord],
    *,
    provider: str,
    rule: TextToolRejectionRule,
    correlation_id: str,
) -> None:
    """The refused call's canary is on the host's error and nowhere else."""
    error = turn.error
    assert isinstance(error, TextToolCallRejectedError)
    assert isinstance(error, ProviderUnavailableError)
    assert error.reason == normalize_route_failure(SafeRouteReasonCode.TOOL_PROTOCOL_ERROR)
    assert (error.provider, error.route) == (provider, "cli")
    assert error.rejection.rule is rule
    assert error.__context__ is None
    assert error.__cause__ is None
    assert turn.executed == []
    assert "".join(
        event.text for event in turn.events if isinstance(event, AssistantTextEvent)
    ) == PROSE_BEFORE_THE_CALL

    surfaces = {
        "log records": formatted_log_text(records),
        "str(error)": str(error),
        "error.args": repr(error.args),
        "traceback": "".join(traceback.format_exception(error)),
        "error.reason": error.reason.model_dump_json(),
        "events": repr(turn.events),
    }
    for surface, text in surfaces.items():
        assert A_CANARY not in text, surface
        assert A_CALLER_CANARY not in text, surface
    assert A_CANARY in error.rejection.excerpt.get_secret_value()

    refusal_lines = [r for r in records if "text_tool_response_invalid" in r.getMessage()]
    assert [(r.name, r.levelno, r.exc_info) for r in refusal_lines] == [
        ("agent_providers.tool_loop", logging.WARNING, None),
    ]
    rejection = error.rejection
    assert refusal_lines[0].getMessage() == (
        f"Tool call rejected provider={provider} route=cli round=0 "
        f"correlation_id={correlation_id} "
        f"code={ToolProtocolRejectionCode.TEXT_TOOL_RESPONSE_INVALID} "
        f"rule={rule} offset={rejection.offset} length={rejection.length}"
    )


def exception_chain(error: BaseException) -> list[BaseException]:
    chain, pending = [], [error]
    while pending:
        current = pending.pop()
        if current in chain:
            continue
        chain.append(current)
        pending.extend(link for link in (current.__cause__, current.__context__) if link)
        pending.extend(getattr(current, "exceptions", ()))
    return chain


def fail_the_home_chmod(monkeypatch: pytest.MonkeyPatch, source: Path) -> None:
    def refuse(*_arguments) -> None:
        raise OSError(errno.EIO, "synthetic chmod failure")

    monkeypatch.setattr(process.os, "chmod", refuse)


def fail_mkdtemp(monkeypatch: pytest.MonkeyPatch, source: Path) -> None:
    def refuse(**_arguments) -> str:
        raise OSError(errno.EIO, "synthetic mkdtemp failure")

    monkeypatch.setattr(process.tempfile, "mkdtemp", refuse)


def fail_the_directory_named(
    monkeypatch: pytest.MonkeyPatch, name: str, error: BaseException,
) -> None:
    make_directory = Path.mkdir

    def refuse(directory: Path, *arguments, **keywords) -> None:
        if directory.name == name:
            raise error
        make_directory(directory, *arguments, **keywords)

    monkeypatch.setattr(Path, "mkdir", refuse)


def fail_the_work_directory(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    fail_the_directory_named(monkeypatch, "work", error)


def _shown(value: object) -> str:
    if isinstance(value, memoryview | bytearray):
        value = bytes(value)
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def traceback_frames_hold(error: BaseException, text: str) -> bool:
    for link in exception_chain(error):
        frame_holder = link.__traceback__
        while frame_holder is not None:
            for value in frame_holder.tb_frame.f_locals.values():
                shown = _shown(value)
                if text in shown:
                    return True
            frame_holder = frame_holder.tb_next
    return False

def live_frames_holding(text: str) -> list[types.FrameType]:
    return [
        item
        for item in gc.get_objects()
        if isinstance(item, types.FrameType)
        and any(text in _shown(value) for value in item.f_locals.values())
    ]


A_RUNNER_FAILURE_CANARY: Final = "runner-failure-canary-7c1d"
ANOTHER_RUNNER_FAILURE_CANARY: Final = "second-runner-failure-canary-2e8b"
RUNNER_STEP_FAILURE_PREFIX: Final = "agent CLI runner step failed: "


_RUNNER_THREAD_TARGET_SUFFIXES: Final = ("(_run_cli_bounded)", "(run_reserved_codex_cli)")


class RunnerFailureWatch:
    """What a failed runner step may leave behind: nothing but its named class.

    Install it before the run; it catches thread errors that nobody handled,
    keeps the log, and decides whether a canary reached any visible surface.
    """

    def __init__(self, monkeypatch, caplog, capfd) -> None:
        self._caplog = caplog
        self._capfd = capfd
        self._threads_of_earlier_tests = set(threading.enumerate()) - {threading.current_thread()}
        self.unhandled_thread_errors: list[threading.ExceptHookArgs] = []
        self._previous_excepthook = threading.excepthook
        monkeypatch.setattr(threading, "excepthook", self._record_unhandled_thread_error)
        caplog.set_level(logging.DEBUG)

    def _record_unhandled_thread_error(self, error: threading.ExceptHookArgs) -> None:
        if error.thread in self._threads_of_earlier_tests:
            self._previous_excepthook(error)
        else:
            self.unhandled_thread_errors.append(error)

    def wait_for_the_runner_threads_to_end(self) -> None:
        for thread in threading.enumerate():
            if (
                thread not in self._threads_of_earlier_tests
                and thread.name.endswith(_RUNNER_THREAD_TARGET_SUFFIXES)
            ):
                thread.join(timeout=5)
                assert not thread.is_alive(), "a runner thread outlived its test"

    def reported_steps(self) -> list[str]:
        self.wait_for_the_runner_threads_to_end()
        return [
            record.getMessage().removeprefix(RUNNER_STEP_FAILURE_PREFIX)
            for record in self._caplog.records
            if record.getMessage().startswith(RUNNER_STEP_FAILURE_PREFIX)
            and record.threadName not in self._names_of_earlier_threads()
        ]

    def _names_of_earlier_threads(self) -> set[str]:
        return {thread.name for thread in self._threads_of_earlier_tests}

    def assert_nothing_leaked(self, *surfaces: BaseException | str) -> None:
        self.wait_for_the_runner_threads_to_end()
        captured = self._capfd.readouterr()
        visible = "\n".join([
            formatted_log_text(self._caplog.records),
            captured.out,
            captured.err,
            *(_text_of(surface) for surface in surfaces),
        ])
        for canary in (A_RUNNER_FAILURE_CANARY, ANOTHER_RUNNER_FAILURE_CANARY):
            assert canary not in visible
        assert self.unhandled_thread_errors == [], [
            (error.thread.name, repr(error.exc_value)) for error in self.unhandled_thread_errors
        ]


def _text_of(surface: BaseException | str) -> str:
    if isinstance(surface, str):
        return surface
    linked = [surface.__cause__, surface.__context__]
    return "\n".join([
        str(surface),
        repr(surface),
        *(_text_of(error) for error in linked if error is not None),
    ])


def pool_callbacks(process_pool, reservation) -> dict[str, Any]:
    """The reservation callbacks, wired as the Codex protocol wires them."""
    return {
        "on_spawned": lambda process_id: process_pool.bind(reservation, process_id),
        "on_spawn_failed": lambda: process_pool.abandon_unspawned(reservation),
        "on_reaped": lambda process_id, _became_zombie: process_pool.reap(
            reservation, process_id,
        ),
    }


def fail_to_start_the_runner_thread(
    monkeypatch, error: BaseException, target: Callable[..., Any] = process._run_cli_bounded,
) -> None:
    """Make only the worker thread running ``target`` fail to start."""
    real_start = threading.Thread.start

    def start(worker: threading.Thread) -> None:
        if worker._target is target:
            raise error
        real_start(worker)

    monkeypatch.setattr(threading.Thread, "start", start)


_START_CLAIM_WAIT_SECONDS: Final = 5


def start_then_raise(
    monkeypatch,
    error: BaseException,
    target: Callable[..., Any] = process._run_cli_bounded,
    *,
    thread_claims_first: bool,
) -> threading.Event:
    """Make the start of the thread running ``target`` raise after the real start ran.

    Orders the two claims on ``ClaimedStartThread.take_start_claim``. With
    ``thread_claims_first`` the started thread claims before ``start()`` raises.
    Otherwise it waits at the returned gate, so the starter claims first until
    the test sets the gate.
    """
    real_start = threading.Thread.start
    real_claim = process.ClaimedStartThread.take_start_claim
    thread_has_claimed = threading.Event()
    gate = threading.Event()
    if thread_claims_first:
        gate.set()

    def claim(thread: process.ClaimedStartThread) -> bool:
        if threading.current_thread() is not thread:
            return real_claim(thread)
        assert gate.wait(timeout=_START_CLAIM_WAIT_SECONDS), "the start claim gate never opened"
        won = real_claim(thread)
        thread_has_claimed.set()
        return won

    def start(worker: threading.Thread) -> None:
        is_target = worker._target is target
        real_start(worker)
        if is_target:
            if thread_claims_first:
                assert thread_has_claimed.wait(timeout=_START_CLAIM_WAIT_SECONDS), (
                    "the started thread never took its claim"
                )
            raise error

    monkeypatch.setattr(process.ClaimedStartThread, "take_start_claim", claim)
    monkeypatch.setattr(threading.Thread, "start", start)
    return gate


class FailingLogHandler(logging.Handler):
    """A log handler whose every emit fails and is handled, keeping the records it failed on."""

    def __init__(self) -> None:
        super().__init__()
        self.handled_records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            raise RuntimeError("log handler failed")
        except RuntimeError:
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        self.handled_records.append(record)
        super().handleError(record)


class RaisingLogHandler(logging.Handler):
    """A log handler whose emit raises the given errors in turn, then `then`."""

    def __init__(
        self,
        errors: Sequence[BaseException] = (),
        then: type[BaseException] | BaseException = RuntimeError,
    ) -> None:
        super().__init__()
        self._errors = iter(errors)
        self._then = then

    def emit(self, record: logging.LogRecord) -> None:
        raise next(self._errors, self._then)
