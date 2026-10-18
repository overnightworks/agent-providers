"""``ClaudeRun`` is the Claude adapter of the neutral run entry; its decoder reads the stream."""

from __future__ import annotations

import json
import logging
import stat
import uuid
from pathlib import Path
from typing import Any

import pytest
from anchored_leaders import spawn_warnings
from anchored_support import GONE, ChildCleanup
from provider_test_support import (
    a_private_root,
    drive_real_children,
    held_claude_slots,
)
from pydantic import SecretStr

from agent_client.wire import TurnMessage, TurnRole
from agent_providers.claude.provider import (
    CLAUDE_CLI_LOGIN_REFUSED_DETAIL,
    CLAUDE_CLI_UNAVAILABLE_DETAIL,
    ClaudeRun,
    ClaudeStreamDecoder,
    CliLoginRefusedError,
    UnavailableError,
)
from agent_providers.cli_run import RunSurfaceRefused, SurfaceVerdict, astream_cli_run
from agent_providers.config import PrivateWorkingRoot, reset_config
from agent_providers.events import (
    AssistantTextEvent,
    FinalEvent,
    StreamEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_providers.spawn import INHERITED_MACHINE_VARIABLES, ChildProcessSpecificationError

CANARY_MARKER = "canary-" + uuid.uuid4().hex
CANARY_TOKEN = "sk-ant-oat01-" + CANARY_MARKER
HISTORY_MARKER = "history-" + uuid.uuid4().hex
CLI_MARKER = "cli-" + uuid.uuid4().hex
SYSTEM_TEXT = "Answer briefly."
OPERATOR_HOME = "/home/operator"
SYSTEM_PROMPT_NAME = "system-prompt"

HISTORY = (
    TurnMessage(role=TurnRole.USER, text="a"),
    TurnMessage(role=TurnRole.ASSISTANT, text="b"),
    TurnMessage(role=TurnRole.USER, text="c"),
)


HISTORY_STDIN_LINE = (
    json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": "User: a\n\nAssistant: b\n\nUser: c"},
        }
    ).encode()
    + b"\n"
)


@pytest.fixture
def no_ambient_machine_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in INHERITED_MACHINE_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def a_run(**overrides: object) -> ClaudeRun:
    fields: dict[str, object] = {
        "binary": Path("/opt/claude/claude"),
        "token": SecretStr(CANARY_TOKEN),
        "system": SYSTEM_TEXT,
        "max_tool_calls": 30,
        "history": HISTORY,
    }
    return ClaudeRun(**{**fields, **overrides})  # type: ignore[arg-type]


def started(tmp_path: Path, **overrides: object):
    return a_run(**overrides).start(tmp_path / "home", tmp_path / "work")


def expected_arguments(tmp_path: Path, max_turns: str = "31") -> tuple[str, ...]:
    return (
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        "",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--max-turns",
        max_turns,
        "--system-prompt-file",
        str(tmp_path / "home" / SYSTEM_PROMPT_NAME),
    )


FORBIDDEN_FLAGS = (
    "--model",
    "--permission-mode",
    "--dangerously-skip-permissions",
    "--resume",
    "--continue",
    "--session-id",
)


def test_a_tool_free_run_has_the_exact_isolated_command_line(tmp_path: Path) -> None:
    arguments = started(tmp_path).child.arguments

    assert arguments == expected_arguments(tmp_path)
    assert not set(FORBIDDEN_FLAGS) & set(arguments)
    assert "--mcp-config" not in arguments
    assert "--allowedTools" not in arguments


def test_a_run_with_a_relay_attaches_only_the_relay_server(tmp_path: Path) -> None:
    relay_config = tmp_path / "home" / "relay.json"

    arguments = started(tmp_path, relay_config=relay_config).child.arguments

    assert arguments == (
        *expected_arguments(tmp_path),
        "--mcp-config",
        str(relay_config),
        "--allowedTools",
        "mcp__relay__*",
    )
    assert not set(FORBIDDEN_FLAGS) & set(arguments)


def test_the_run_files_hold_only_the_system_text_and_never_the_token() -> None:
    assert a_run().run_files() == {SYSTEM_PROMPT_NAME: SYSTEM_TEXT.encode()}


@pytest.mark.parametrize("relay", [False, True])
def test_the_token_and_the_system_text_are_in_no_argument_and_not_in_stdin(
    tmp_path: Path,
    relay: bool,
) -> None:
    relay_config = tmp_path / "home" / "relay.json" if relay else None
    start = started(tmp_path, relay_config=relay_config)
    command = start.child.command

    assert not any(CANARY_MARKER in element for element in command)
    assert SYSTEM_TEXT not in " ".join(command)
    assert (CANARY_MARKER.encode() in start.stdin, SYSTEM_TEXT.encode() in start.stdin) == (
        False,
        False,
    )


def test_the_environment_is_the_closed_one_plus_the_token_and_the_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_ambient_machine_variables: None
) -> None:
    monkeypatch.setenv("HOME", OPERATOR_HOME)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-auth")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", f"{OPERATOR_HOME}/.claude")

    child = started(tmp_path).child

    assert dict(child.environment) == {
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "home"),
        "PATH": "/usr/bin",
        "HTTPS_PROXY": "http://proxy:3128",
        "LANG": "C.UTF-8",
        "CLAUDE_CODE_OAUTH_TOKEN": CANARY_TOKEN,
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
    }
    assert child.working_directory == tmp_path / "work"
    assert child.binary == Path("/opt/claude/claude")


def test_a_run_builds_with_no_runtime_configuration_installed(tmp_path: Path) -> None:
    reset_config()
    start = started(tmp_path)

    assert (start.child.arguments, start.stdin) == (
        expected_arguments(tmp_path),
        HISTORY_STDIN_LINE,
    )


def test_the_token_and_the_history_stay_out_of_every_repr(tmp_path: Path) -> None:
    history = (TurnMessage(role=TurnRole.USER, text=HISTORY_MARKER),)
    run = a_run(history=history)
    start = run.start(tmp_path / "home", tmp_path / "work")
    shown = repr(run) + str(run) + repr(start) + str(start) + repr(start.child)

    assert isinstance(run.token, SecretStr)
    assert CANARY_MARKER not in shown
    assert HISTORY_MARKER not in shown
    assert SYSTEM_TEXT not in shown
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in repr(start.child)


def test_a_token_that_is_a_plain_string_is_refused_by_field_name() -> None:
    with pytest.raises(ChildProcessSpecificationError, match="token") as refusal:
        a_run(token=CANARY_TOKEN)

    assert CANARY_MARKER not in str(refusal.value)


@pytest.mark.parametrize("field", ["binary", "relay_config"])
def test_a_relative_path_is_refused_by_field_name_only(field: str) -> None:
    with pytest.raises(ChildProcessSpecificationError, match=field) as refusal:
        a_run(**{field: Path("relative-marker-path")})

    assert "relative-marker-path" not in str(refusal.value)


@pytest.mark.parametrize(("tool_calls", "turns"), [(1, "2"), (5, "6"), (30, "31")])
def test_the_turn_limit_is_one_beyond_the_tool_call_limit(
    tmp_path: Path, tool_calls: int, turns: str
) -> None:
    arguments = started(tmp_path, max_tool_calls=tool_calls).child.arguments

    assert arguments == expected_arguments(tmp_path, max_turns=turns)


@pytest.mark.parametrize("tool_calls", [0, -1])
def test_fewer_than_one_tool_call_is_refused(tool_calls: int) -> None:
    with pytest.raises(ChildProcessSpecificationError, match="max_tool_calls"):
        a_run(max_tool_calls=tool_calls)


def test_the_history_is_one_transcript_line_in_order(tmp_path: Path) -> None:
    assert started(tmp_path).stdin == HISTORY_STDIN_LINE


def test_an_empty_history_is_refused() -> None:
    with pytest.raises(ChildProcessSpecificationError, match="history"):
        a_run(history=())


def line(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode() + b"\n"


RELAY_ENTRY = {"name": "relay", "status": "connected"}


def an_init(**fields: Any) -> bytes:
    announcement = {
        "type": "system",
        "subtype": "init",
        "tools": [],
        "mcp_servers": [],
        "apiKeySource": "none",
    }
    return line({**announcement, **fields})


INIT_LINE = an_init()
RELAY_INIT = an_init(tools=["mcp__relay__lookup"], mcp_servers=[RELAY_ENTRY])
TEXT_LINE = line({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
TOOL_CALL_LINE = line(
    {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": "t1", "name": "lookup", "input": {"q": 1}}]
        },
    }
)
TOOL_RESULT_LINE = line(
    {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "out"}]},
    }
)
SUCCESS_LINE = line(
    {"type": "result", "subtype": "success", "is_error": False, "result": " done \n"}
)


def events_of_lines(decoder: ClaudeStreamDecoder, *lines: bytes) -> list[Any]:
    return [event for each in lines for event in decoder.events_of(each)]


def test_the_decoder_maps_each_block_and_ends_with_the_result() -> None:
    decoder = ClaudeStreamDecoder(relay_attached=False)

    steps = events_of_lines(
        decoder, INIT_LINE, TEXT_LINE, TOOL_CALL_LINE, TOOL_RESULT_LINE, SUCCESS_LINE
    )

    assert steps == [
        AssistantTextEvent(text="hi"),
        ToolCallEvent(tool_use_id="t1", name="lookup", input={"q": 1}),
        ToolResultEvent(tool_use_id="t1", content="out"),
    ]
    assert decoder.ending(0) == FinalEvent(text="done")


def test_a_run_without_a_result_line_fails() -> None:
    decoder = ClaudeStreamDecoder(relay_attached=False)
    events_of_lines(decoder, INIT_LINE, TEXT_LINE)

    with pytest.raises(UnavailableError) as refusal:
        decoder.ending(0)

    assert str(refusal.value) == CLAUDE_CLI_UNAVAILABLE_DETAIL


@pytest.mark.parametrize(
    "error_line",
    [
        pytest.param(
            line(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "result": CLI_MARKER,
                }
            ),
            id="error-result",
        ),
        pytest.param(
            line({"type": "result", "subtype": "error_max_turns", "is_error": True}),
            id="turn-limit",
        ),
    ],
)
def test_an_error_result_fails_at_its_line(error_line: bytes) -> None:
    decoder = ClaudeStreamDecoder(relay_attached=False)
    decoder.events_of(INIT_LINE)

    with pytest.raises(UnavailableError) as refusal:
        decoder.events_of(error_line)

    assert str(refusal.value) == CLAUDE_CLI_UNAVAILABLE_DETAIL


def test_a_refused_login_fails_with_the_closed_cause_and_no_cli_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    decoder = ClaudeStreamDecoder(relay_attached=False)
    decoder.events_of(INIT_LINE)
    refused = line(
        {
            "type": "assistant",
            "is_api_error_message": True,
            "error": "authentication_failed",
            "message": {"content": [{"type": "text", "text": f"Not logged in {CLI_MARKER}"}]},
        }
    )

    with pytest.raises(CliLoginRefusedError) as refusal:
        decoder.events_of(refused)

    assert (str(refusal.value), CLI_MARKER in caplog.text) == (
        CLAUDE_CLI_LOGIN_REFUSED_DETAIL,
        False,
    )


def test_a_non_zero_exit_fails_after_a_success_result() -> None:
    decoder = ClaudeStreamDecoder(relay_attached=False)
    events_of_lines(decoder, INIT_LINE, SUCCESS_LINE)

    with pytest.raises(UnavailableError):
        decoder.ending(1)


def test_a_malformed_line_before_the_result_is_skipped_without_cli_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    decoder = ClaudeStreamDecoder(relay_attached=False)

    steps = events_of_lines(decoder, INIT_LINE, b"\xff" + CLI_MARKER.encode() + b"\n", SUCCESS_LINE)

    assert (steps, decoder.ending(0), CLI_MARKER in caplog.text) == (
        [],
        FinalEvent(text="done"),
        False,
    )


def test_an_object_as_a_text_block_fails_closed_with_no_cli_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    decoder = ClaudeStreamDecoder(relay_attached=False)
    decoder.events_of(INIT_LINE)
    object_as_text = line(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": {"marker": CLI_MARKER}}]},
        }
    )

    with pytest.raises(UnavailableError) as refusal:
        decoder.events_of(object_as_text)

    assert (
        str(refusal.value),
        refusal.value.__cause__,
        refusal.value.__context__,
        CLI_MARKER in caplog.text,
    ) == (CLAUDE_CLI_UNAVAILABLE_DETAIL, None, None, False)


def a_fake_claude(
    directory: Path,
    stream: tuple[bytes, ...] = (INIT_LINE, TEXT_LINE, SUCCESS_LINE),
    then: str = "exit 0",
) -> Path:
    stream_text = b"".join(stream).decode()
    binary = directory / "claude"
    binary.write_text(
        "#!/bin/sh\n"
        f'case " $* " in *" --system-prompt-file $HOME/{SYSTEM_PROMPT_NAME} "*) ;;'
        " *) exit 11;; esac\n"
        f'[ "$(cat "$HOME/{SYSTEM_PROMPT_NAME}")" = \'{SYSTEM_TEXT}\' ] || exit 12\n'
        "IFS= read -r stdin_line\n"
        f"[ \"$stdin_line\" = '{HISTORY_STDIN_LINE.decode().strip()}' ] || exit 13\n"
        '[ -n "$CLAUDE_CODE_OAUTH_TOKEN" ] || exit 14\n'
        '[ -z "$(ls -A)" ] || exit 15\n'
        f"cat <<'EOF'\n{stream_text}EOF\n"
        f"{then}\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def test_a_claude_run_streams_through_the_neutral_entry(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    thread_failures: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_root = a_private_root(tmp_path)
    run = a_run(binary=a_fake_claude(tmp_path))
    seen: list[StreamEvent] = []

    async def scenario() -> None:
        async for event in astream_cli_run(PrivateWorkingRoot(private_root), run):
            seen.append(event)

    drive_real_children(child_cleanup, monkeypatch, scenario())

    assert (
        seen,
        list(private_root.iterdir()),
        held_claude_slots(),
        [child_cleanup.state(owned) is GONE for owned in child_cleanup.listed()],
        thread_failures,
        spawn_warnings(caplog),
    ) == (
        [AssistantTextEvent(text="hi"), FinalEvent(text="done")],
        [],
        0,
        [True],
        [],
        [],
    )


def refusal_of(decoder: ClaudeStreamDecoder, *lines: bytes) -> RunSurfaceRefused:
    with pytest.raises(RunSurfaceRefused) as refusal:
        events_of_lines(decoder, *lines)
    return refusal.value


@pytest.mark.parametrize(
    "relay_attached",
    [pytest.param(True, id="with-a-relay"), pytest.param(False, id="without-a-relay")],
)
def test_a_built_in_tool_in_init_refuses_the_run(relay_attached: bool) -> None:
    relay_servers = [RELAY_ENTRY] if relay_attached else []
    decoder = ClaudeStreamDecoder(relay_attached=relay_attached)

    refusal = refusal_of(decoder, an_init(tools=["Bash"], mcp_servers=relay_servers))

    assert refusal.verdict is SurfaceVerdict.TOOL_BESIDE_RELAY


def test_a_foreign_mcp_server_in_init_refuses_the_run() -> None:
    decoder = ClaudeStreamDecoder(relay_attached=True)
    servers = [RELAY_ENTRY, {"name": "notes", "status": "connected"}]

    refusal = refusal_of(decoder, an_init(mcp_servers=servers))

    assert refusal.verdict is SurfaceVerdict.SERVER_BESIDE_RELAY


@pytest.mark.parametrize(
    "servers",
    [
        [{"name": "relay", "status": "failed"}],
        [{"name": "relay", "status": "pending"}],
        [],
    ],
    ids=["failed", "pending", "no-relay-entry"],
)
def test_a_relay_that_is_not_connected_refuses_the_run(servers: list[dict[str, str]]) -> None:
    decoder = ClaudeStreamDecoder(relay_attached=True)

    refusal = refusal_of(decoder, an_init(mcp_servers=servers))

    assert refusal.verdict is SurfaceVerdict.RELAY_NOT_CONNECTED


@pytest.mark.parametrize(
    "source",
    ["ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key"],
)
def test_an_api_key_source_refuses_the_run(source: str) -> None:
    decoder = ClaudeStreamDecoder(relay_attached=False)

    refusal = refusal_of(decoder, an_init(apiKeySource=source))

    assert refusal.verdict is SurfaceVerdict.FOREIGN_CREDENTIAL


NOT_JUDGED_LISTS = an_init(
    slash_commands=["compact"], skills=["debug"], agents=["general-purpose"], plugins=["p"]
)


@pytest.mark.parametrize(
    ("relay_attached", "init"),
    [
        pytest.param(False, INIT_LINE, id="without-a-relay"),
        pytest.param(True, RELAY_INIT, id="with-a-relay"),
        pytest.param(False, NOT_JUDGED_LISTS, id="unjudged-lists"),
        pytest.param(False, b"\xff\n" + INIT_LINE, id="malformed-line-before-init"),
    ],
)
def test_a_valid_init_lets_the_run_continue(relay_attached: bool, init: bytes) -> None:
    decoder = ClaudeStreamDecoder(relay_attached=relay_attached)

    steps = events_of_lines(
        decoder, *init.splitlines(keepends=True), TEXT_LINE, TOOL_CALL_LINE, SUCCESS_LINE
    )

    assert (steps, decoder.ending(0)) == (
        [
            AssistantTextEvent(text="hi"),
            ToolCallEvent(tool_use_id="t1", name="lookup", input={"q": 1}),
        ],
        FinalEvent(text="done"),
    )


@pytest.mark.parametrize(
    "first_object",
    [
        TEXT_LINE,
        SUCCESS_LINE,
        line({"type": "system", "subtype": "api_retry"}),
    ],
    ids=["assistant", "result", "api-retry"],
)
def test_an_event_before_init_refuses_the_run(first_object: bytes) -> None:
    decoder = ClaudeStreamDecoder(relay_attached=False)

    refusal = refusal_of(decoder, first_object)

    assert refusal.verdict is SurfaceVerdict.NOT_ANNOUNCED


@pytest.mark.parametrize(
    "init",
    [
        an_init(tools="x"),
        an_init(tools=[1]),
        an_init(mcp_servers=[1]),
        an_init(mcp_servers=[{"name": 1, "status": "connected"}]),
        an_init(mcp_servers=[{"name": "relay", "status": None}]),
        an_init(mcp_servers=[RELAY_ENTRY, RELAY_ENTRY]),
        line({"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}),
        an_init(apiKeySource=1),
    ],
    ids=[
        "tools-not-a-list",
        "tool-not-a-string",
        "server-not-an-object",
        "server-name-not-a-string",
        "server-status-null",
        "two-relay-entries",
        "api-key-source-absent",
        "api-key-source-not-a-string",
    ],
)
def test_an_unreadable_init_refuses_the_run(init: bytes) -> None:
    decoder = ClaudeStreamDecoder(relay_attached=False)

    refusal = refusal_of(decoder, init)

    assert refusal.verdict is SurfaceVerdict.UNREADABLE


@pytest.mark.parametrize(
    ("relay_attached", "fields"),
    [
        (True, {"tools": [CANARY_MARKER], "mcp_servers": [RELAY_ENTRY]}),
        (False, {"tools": [CANARY_MARKER]}),
        (False, {"mcp_servers": [{"name": CANARY_MARKER, "status": "connected"}]}),
        (True, {"mcp_servers": [{"name": "relay", "status": CANARY_MARKER}]}),
        (False, {"apiKeySource": CANARY_MARKER}),
    ],
    ids=[
        "tool-name-with-a-relay",
        "tool-name-without-a-relay",
        "server-name",
        "relay-status",
        "api-key-source",
    ],
)
def test_no_announced_string_reaches_the_refusal_or_the_log(
    relay_attached: bool, fields: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    decoder = ClaudeStreamDecoder(relay_attached=relay_attached)
    refused = None

    try:
        raise UnicodeDecodeError("utf-8", CLI_MARKER.encode(), 0, 1, "invalid start byte")
    except UnicodeDecodeError:
        refused = refusal_of(decoder, an_init(**fields))

    carried = [str(refused), repr(refused), repr(refused.args), repr(refused.__dict__)]
    leaked = [
        text for text in (*carried, caplog.text) if CANARY_MARKER in text or CLI_MARKER in text
    ]
    assert leaked == []
    assert (refused.__cause__, refused.__context__) == (None, None)


def test_every_init_line_is_judged() -> None:
    decoder = ClaudeStreamDecoder(relay_attached=False)

    refusal = refusal_of(decoder, INIT_LINE, an_init(tools=["Bash"]), TEXT_LINE)

    assert refusal.verdict is SurfaceVerdict.TOOL_BESIDE_RELAY


def test_a_valid_init_repeated_yields_no_event_and_the_run_ends() -> None:
    decoder = ClaudeStreamDecoder(relay_attached=False)

    steps = events_of_lines(decoder, INIT_LINE, INIT_LINE, TEXT_LINE, SUCCESS_LINE)

    assert (steps, decoder.ending(0)) == ([AssistantTextEvent(text="hi")], FinalEvent(text="done"))


def test_a_refused_init_ends_the_run_when_it_is_read(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    thread_failures: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = a_private_root(tmp_path)
    binary = a_fake_claude(
        tmp_path, stream=(an_init(tools=["Bash"]), TEXT_LINE), then="exec sleep 600"
    )
    run = a_run(binary=binary)
    seen: list[StreamEvent] = []
    refusals: list[RunSurfaceRefused] = []

    async def scenario() -> None:
        with pytest.raises(RunSurfaceRefused) as refusal:
            async for event in astream_cli_run(PrivateWorkingRoot(private_root), run):
                seen.append(event)
        refusals.append(refusal.value)

    drive_real_children(child_cleanup, monkeypatch, scenario())

    assert (
        [refusal.verdict for refusal in refusals],
        seen,
        list(private_root.iterdir()),
        held_claude_slots(),
        [child_cleanup.state(owned) is GONE for owned in child_cleanup.listed()],
        thread_failures,
    ) == ([SurfaceVerdict.TOOL_BESIDE_RELAY], [], [], 0, [True], [])
