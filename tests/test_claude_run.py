"""``ClaudeRun`` describes one subscription run of the pinned CLI, purely."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent_client.wire import TurnMessage, TurnRole
from agent_providers.claude.provider import ClaudeRun
from agent_providers.config import reset_config
from agent_providers.spawn import ChildProcessSpecificationError

CANARY_MARKER = "canary-" + uuid.uuid4().hex
CANARY_TOKEN = "sk-ant-oat01-" + CANARY_MARKER
HISTORY_MARKER = "history-" + uuid.uuid4().hex
OPERATOR_HOME = "/home/operator"

HISTORY = (
    TurnMessage(role=TurnRole.USER, text="a"),
    TurnMessage(role=TurnRole.ASSISTANT, text="b"),
    TurnMessage(role=TurnRole.USER, text="c"),
)


def a_run(tmp_path: Path, **overrides: object) -> ClaudeRun:
    fields: dict[str, object] = {
        "binary": Path("/opt/claude/claude"),
        "token": SecretStr(CANARY_TOKEN),
        "home": tmp_path / "home",
        "working_directory": tmp_path / "work",
        "system_prompt_file": tmp_path / "home" / "system.txt",
        "max_tool_calls": 30,
        "history": HISTORY,
    }
    return ClaudeRun(**{**fields, **overrides})  # type: ignore[arg-type]


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
        str(tmp_path / "home" / "system.txt"),
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
    arguments = a_run(tmp_path).child().arguments

    assert arguments == expected_arguments(tmp_path)
    assert not set(FORBIDDEN_FLAGS) & set(arguments)
    assert "--mcp-config" not in arguments
    assert "--allowedTools" not in arguments


def test_a_run_with_a_relay_attaches_only_the_relay_server(tmp_path: Path) -> None:
    relay_config = tmp_path / "home" / "relay.json"

    arguments = a_run(tmp_path, relay_config=relay_config).child().arguments

    assert arguments == (
        *expected_arguments(tmp_path),
        "--mcp-config",
        str(relay_config),
        "--allowedTools",
        "mcp__relay__*",
    )
    assert not set(FORBIDDEN_FLAGS) & set(arguments)


@pytest.mark.parametrize("relay", [False, True])
def test_the_token_is_in_no_argument_and_not_in_stdin(tmp_path: Path, relay: bool) -> None:
    relay_config = tmp_path / "home" / "relay.json" if relay else None
    run = a_run(tmp_path, relay_config=relay_config)
    command = run.child().command

    assert not any(CANARY_MARKER in element for element in command)
    assert CANARY_MARKER not in " ".join(command)
    assert CANARY_MARKER.encode() not in run.stdin_message()


def test_the_environment_is_the_closed_one_plus_the_token_and_the_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("HOME", "PATH", "HTTPS_PROXY", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", OPERATOR_HOME)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-auth")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", f"{OPERATOR_HOME}/.claude")

    child = a_run(tmp_path).child()

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
    run = a_run(tmp_path)

    assert run.child().arguments == expected_arguments(tmp_path)
    assert run.stdin_message().endswith(b"\n")


def test_the_token_and_the_history_stay_out_of_every_repr(tmp_path: Path) -> None:
    history = (TurnMessage(role=TurnRole.USER, text=HISTORY_MARKER),)
    run = a_run(tmp_path, history=history)
    shown = repr(run) + str(run) + repr(run.child())

    assert isinstance(run.token, SecretStr)
    assert CANARY_MARKER not in shown
    assert HISTORY_MARKER not in shown
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in repr(run.child())


def test_a_token_that_is_a_plain_string_is_refused_by_field_name(tmp_path: Path) -> None:
    with pytest.raises(ChildProcessSpecificationError, match="token") as refusal:
        a_run(tmp_path, token=CANARY_TOKEN)

    assert CANARY_MARKER not in str(refusal.value)


@pytest.mark.parametrize(
    "field",
    ["binary", "home", "working_directory", "system_prompt_file", "relay_config"],
)
def test_a_relative_path_is_refused_by_field_name_only(tmp_path: Path, field: str) -> None:
    with pytest.raises(ChildProcessSpecificationError, match=field) as refusal:
        a_run(tmp_path, **{field: Path("relative-marker-path")})

    assert "relative-marker-path" not in str(refusal.value)


def test_a_working_directory_equal_to_the_home_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ChildProcessSpecificationError, match="working_directory"):
        a_run(tmp_path, working_directory=tmp_path / "home")


@pytest.mark.parametrize(("tool_calls", "turns"), [(1, "2"), (5, "6"), (30, "31")])
def test_the_turn_limit_is_one_beyond_the_tool_call_limit(
    tmp_path: Path, tool_calls: int, turns: str
) -> None:
    arguments = a_run(tmp_path, max_tool_calls=tool_calls).child().arguments

    assert arguments == expected_arguments(tmp_path, max_turns=turns)


@pytest.mark.parametrize("tool_calls", [0, -1])
def test_fewer_than_one_tool_call_is_refused(tmp_path: Path, tool_calls: int) -> None:
    with pytest.raises(ChildProcessSpecificationError, match="max_tool_calls"):
        a_run(tmp_path, max_tool_calls=tool_calls)


def test_the_history_is_one_transcript_line_in_order(tmp_path: Path) -> None:
    expected = (
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "User: a\n\nAssistant: b\n\nUser: c"},
            }
        ).encode()
        + b"\n"
    )

    assert a_run(tmp_path).stdin_message() == expected


def test_the_system_text_is_in_neither_the_arguments_nor_the_stdin(tmp_path: Path) -> None:
    system_file = tmp_path / "home" / "system.txt"
    system_file.parent.mkdir()
    system_file.write_text("SYSTEM-MARKER-TEXT")
    run = a_run(tmp_path)

    assert "SYSTEM-MARKER-TEXT" not in " ".join(run.child().command)
    assert b"SYSTEM-MARKER-TEXT" not in run.stdin_message()


def test_an_empty_history_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ChildProcessSpecificationError, match="history"):
        a_run(tmp_path, history=())
