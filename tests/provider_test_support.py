"""Library-local test support for the ``agent_providers`` package tests.

A sibling ``conftest.py`` in this directory would make ``from conftest import``
ambiguous under pytest's prepend import mode — the root ``tests/conftest.py``
and this package's own ``conftest.py`` both import as ``conftest``. The lib
tests therefore reach their shared helpers here, by basename, which resolves
the same way both inside songmaker today and inside the extracted
``agent_providers`` repository tomorrow (issue #825, S1).

Self-contained by design: it imports only ``agent_providers`` and the standard
library, never ``songmaker_cli``. The deployment facts below are the package's
own sample — a host named ``songmaker`` with its twelve co-writer tools — not
an import of the application's constants, so the tests prove the provider layer
against values this package owns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import stat
import tempfile
import threading
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from unittest.mock import MagicMock

import pytest

from agent_client.tools import ToolDeclaration
from agent_providers.catalog import ProviderRoute
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


def a_fifo(directory: Path) -> Path:
    path = directory / "source"
    os.mkfifo(path)
    return path

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
                    "prompt": _STRING,
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
            parameters=_object({"note_id": _STRING, "prompt": _STRING}, ["note_id", "prompt"]),
        ),
        ToolDeclaration(
            name="update_note_tags",
            description="Update bpm, key_scale, and/or audio_duration tags.",
            parameters=_object(
                {
                    "note_id": _STRING,
                    "bpm": _INT,
                    "key_scale": _STRING,
                    "audio_duration": _INT,
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
                route=ProviderRoute.CLI,
                model=f"{provider}-test",
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


def traceback_frames_hold(error: BaseException, text: str) -> bool:
    for link in exception_chain(error):
        frame_holder = link.__traceback__
        while frame_holder is not None:
            for value in frame_holder.tb_frame.f_locals.values():
                shown = value.decode(errors="replace") if isinstance(value, bytes) else str(value)
                if text in shown:
                    return True
            frame_holder = frame_holder.tb_next
    return False
