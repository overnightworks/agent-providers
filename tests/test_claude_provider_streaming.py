"""Tests for the streaming Claude provider path."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_providers.claude import provider
from agent_providers.claude.provider import (
    CLAUDE_CLI_LOGIN_REFUSED_DETAIL,
    CLAUDE_CLI_UNAVAILABLE_DETAIL,
    CliLoginRefusedError,
    UnavailableError,
    _parse_stream_event,
    _safe_json_loads,
    acall_claude_with_mcp_stream,
)
from agent_providers.errors import ProviderUnavailableError, SafeRouteReasonCode
from agent_providers.events import (
    AssistantTextEvent,
    FinalEvent,
    StreamEvent,
    ToolCallEvent,
    ToolResultEvent,
)

A_CORRELATION_ID = "job-42"
_ASYNCIO_DEFAULT_LIMIT = 64 * 1024


@pytest.fixture(autouse=True)
def _no_real_killpg(monkeypatch):
    monkeypatch.setattr(provider.os, "killpg", MagicMock())


class FakeStreamReader:
    """Minimal stand-in for asyncio.StreamReader.readline()."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self, size: int = -1) -> bytes:
        if not self._lines:
            return b""
        if size < 0:
            out = b"".join(self._lines)
            self._lines = []
            return out
        out = self._lines.pop(0)
        if len(out) <= size:
            return out
        self._lines.insert(0, out[size:])
        return out[:size]


def _make_fake_proc(
    stdout_lines: list[bytes],
    *,
    returncode: int = 0,
    stderr_bytes: bytes = b"",
) -> Any:
    proc = MagicMock()
    proc.stdout = FakeStreamReader(stdout_lines)
    proc.stderr = FakeStreamReader([stderr_bytes] if stderr_bytes else [])
    proc.returncode = returncode

    async def _wait() -> None:
        return None

    proc.wait = _wait
    proc.kill = MagicMock()
    proc.pid = 4242
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()

    async def _drain() -> None:
        return None

    proc.stdin.drain = _drain
    proc.stdin.close = MagicMock()
    return proc


def _assistant_line(text: str) -> bytes:
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }).encode() + b"\n"


def _tool_use_line(name: str, use_id: str, inp: dict) -> bytes:
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": use_id, "name": name, "input": inp,
        }]},
    }).encode() + b"\n"


def _tool_result_line(use_id: str, content: str) -> bytes:
    return json.dumps({
        "type": "user",
        "message": {"content": [{
            "type": "tool_result",
            "tool_use_id": use_id,
            "content": [{"type": "text", "text": content}],
        }]},
    }).encode() + b"\n"


def _result_line(text: str) -> bytes:
    return json.dumps({
        "type": "result", "subtype": "success", "result": text,
    }).encode() + b"\n"


async def _collect(agen) -> list[StreamEvent]:
    out: list[StreamEvent] = []
    async for ev in agen:
        out.append(ev)
    return out


# ── _safe_json_loads ──────────────────────────────────────────────────


def test_safe_json_loads_blank_line_returns_none() -> None:
    assert _safe_json_loads(b"   \n") is None


def test_safe_json_loads_valid_object() -> None:
    assert _safe_json_loads(b'{"a": 1}') == {"a": 1}


def test_safe_json_loads_malformed_skipped(caplog) -> None:
    caplog.set_level("WARNING")
    assert _safe_json_loads(b"not-json\n") is None
    assert any("malformed" in r.message.lower() for r in caplog.records)


def test_safe_json_loads_non_object_skipped(caplog) -> None:
    caplog.set_level("WARNING")
    assert _safe_json_loads(b"42") is None
    assert any("non-object" in r.message.lower() for r in caplog.records)


# ── _parse_stream_event ──────────────────────────────────────────────


def _assistant_payload(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _user_payload(*blocks: dict) -> dict:
    return {"type": "user", "message": {"content": list(blocks)}}


def _a_tool_use_block(use_id: str, name: str = "foo", **arguments: Any) -> dict:
    return {"type": "tool_use", "id": use_id, "name": name, "input": arguments}


def _a_tool_result_block(use_id: str, content: Any = "ok", **extra: Any) -> dict:
    return {"type": "tool_result", "tool_use_id": use_id, "content": content, **extra}


def test_parse_assistant_text_event() -> None:
    chunks: list[str] = []
    [ev] = _parse_stream_event(
        _assistant_payload({"type": "text", "text": "hello"}), chunks, A_CORRELATION_ID,
    )
    assert isinstance(ev, AssistantTextEvent)
    assert ev.text == "hello"
    assert ev.correlation_id == A_CORRELATION_ID
    assert chunks == ["hello"]


def test_parse_assistant_tool_use_event() -> None:
    [ev] = _parse_stream_event(
        _assistant_payload(_a_tool_use_block("tu-1", x=1)), [], A_CORRELATION_ID,
    )
    assert isinstance(ev, ToolCallEvent)
    assert ev.tool_use_id == "tu-1"
    assert ev.name == "foo"
    assert ev.input == {"x": 1}


def test_parse_assistant_text_and_two_tool_uses_yield_three_events_in_order() -> None:
    chunks: list[str] = []
    events = _parse_stream_event(
        _assistant_payload(
            {"type": "text", "text": "looking"},
            _a_tool_use_block("tu-1"),
            _a_tool_use_block("tu-2"),
        ),
        chunks,
        A_CORRELATION_ID,
    )
    assert [e.type for e in events] == ["assistant_text", "tool_call", "tool_call"]
    assert [e.tool_use_id for e in events[1:]] == ["tu-1", "tu-2"]
    assert chunks == ["looking"]


def test_parse_user_two_tool_results_yield_two_events_in_order() -> None:
    events = _parse_stream_event(
        _user_payload(_a_tool_result_block("tu-1", "a"), _a_tool_result_block("tu-2", "b")),
        [],
        A_CORRELATION_ID,
    )
    assert [(e.type, e.tool_use_id, e.content) for e in events] == [
        ("tool_result", "tu-1", "a"),
        ("tool_result", "tu-2", "b"),
    ]


def test_parse_user_tool_result_event() -> None:
    [ev] = _parse_stream_event(
        _user_payload(_a_tool_result_block(
            "tu-1", [{"type": "text", "text": "ok"}], is_error=False,
        )),
        [],
        A_CORRELATION_ID,
    )
    assert isinstance(ev, ToolResultEvent)
    assert ev.tool_use_id == "tu-1"
    assert ev.content == "ok"
    assert ev.is_error is False


@pytest.mark.parametrize(
    ("content", "expected"),
    [("plain", "plain"), (None, "")],
    ids=["string-content", "missing-content"],
)
def test_parse_user_tool_result_content_shapes(content, expected) -> None:
    [ev] = _parse_stream_event(
        _user_payload(_a_tool_result_block("tu-1", content)), [], A_CORRELATION_ID,
    )
    assert ev.content == expected


def test_parse_result_event_returns_final() -> None:
    [ev] = _parse_stream_event({"type": "result", "result": "done"}, [], A_CORRELATION_ID)
    assert isinstance(ev, FinalEvent)
    assert ev.text == "done"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "result"},
        _assistant_payload(),
        _user_payload(),
        {"type": "system"},
    ],
    ids=["result-without-text", "empty-assistant", "empty-user", "unknown-type"],
)
def test_parse_payload_without_content_yields_no_events(payload) -> None:
    assert _parse_stream_event(payload, [], A_CORRELATION_ID) == []


# ── acall_claude_with_mcp_stream integration ─────────────────────────


def test_stream_yields_ordered_events(monkeypatch) -> None:
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )

    lines = [
        _assistant_line("Hi, "),
        _tool_use_line("mcp__notes__get_note", "tu-1", {"note_id": "s1"}),
        _tool_result_line("tu-1", "note data"),
        _assistant_line("done."),
        _result_line("Hi, done."),
    ]

    async def fake_exec(*_cmd, **_kw):
        return _make_fake_proc(lines)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    events = asyncio.run(_collect(
        acall_claude_with_mcp_stream(prompt="hi", user_id="u"),
    ))
    kinds = [e.type for e in events]
    assert kinds == [
        "assistant_text", "tool_call", "tool_result",
        "assistant_text", "final",
    ]
    assert isinstance(events[-1], FinalEvent)
    assert events[-1].text == "Hi, done."


def test_stream_skips_malformed_lines(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )
    caplog.set_level("WARNING")

    lines = [
        b"not-json-at-all\n",
        _assistant_line("hello"),
        _result_line("hello"),
    ]

    async def fake_exec(*_cmd, **_kw):
        return _make_fake_proc(lines)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    events = asyncio.run(_collect(
        acall_claude_with_mcp_stream(prompt="hi", user_id="u"),
    ))
    assert [e.type for e in events] == ["assistant_text", "final"]
    assert any("malformed" in r.message.lower() for r in caplog.records)


def test_stream_regression_matches_non_streaming_final_text(monkeypatch) -> None:
    """Final event must equal result.result, matching non-streaming json path."""
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )
    expected = "The quick brown fox"
    lines = [
        _assistant_line("The quick "),
        _assistant_line("brown fox"),
        _result_line(expected),
    ]

    async def fake_exec(*_cmd, **_kw):
        return _make_fake_proc(lines)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    events = asyncio.run(_collect(
        acall_claude_with_mcp_stream(prompt="hi", user_id="u"),
    ))
    final = [e for e in events if isinstance(e, FinalEvent)]
    assert len(final) == 1
    assert final[0].text == expected


def test_stream_falls_back_to_assembled_text_when_no_result(monkeypatch) -> None:
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )
    lines = [
        _assistant_line("piece one "),
        _assistant_line("piece two"),
    ]

    async def fake_exec(*_cmd, **_kw):
        return _make_fake_proc(lines)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    events = asyncio.run(_collect(
        acall_claude_with_mcp_stream(prompt="hi", user_id="u"),
    ))
    final = [e for e in events if isinstance(e, FinalEvent)][0]
    assert final.text == "piece one piece two"


def test_stream_nonzero_exit_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )

    async def fake_exec(*_cmd, **_kw):
        return _make_fake_proc([], returncode=2, stderr_bytes=b"boom")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    stream = acall_claude_with_mcp_stream(prompt="hi", user_id="u")
    collected = _collect(stream)
    with pytest.raises(UnavailableError):
        asyncio.run(collected)


def test_stream_binary_missing_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )

    async def fake_exec(*_cmd, **_kw):
        raise FileNotFoundError()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    stream = acall_claude_with_mcp_stream(prompt="hi", user_id="u")
    collected = _collect(stream)
    with pytest.raises(UnavailableError):
        asyncio.run(collected)


def test_stream_timeout_kills_subprocess(monkeypatch) -> None:
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )

    killed = {"value": False}

    class StallingReader(FakeStreamReader):
        async def readline(self) -> bytes:
            await asyncio.sleep(10)
            return b""

    async def fake_exec(*_cmd, **_kw):
        proc = _make_fake_proc([])
        proc.returncode = None
        proc.stdout = StallingReader([])
        return proc

    def _killpg(_pid, _sig):
        killed["value"] = True

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(provider.os, "killpg", _killpg)

    stream = acall_claude_with_mcp_stream(
        prompt="hi", user_id="u", timeout_seconds=0.1,
    )
    collected = _collect(stream)
    with pytest.raises(UnavailableError):
        asyncio.run(collected)
    assert killed["value"] is True


def test_completed_stream_does_not_signal_process_group(monkeypatch) -> None:
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )

    async def fake_exec(*_cmd, **_kw):
        return _make_fake_proc([_result_line("ok")])

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    events = asyncio.run(_collect(
        acall_claude_with_mcp_stream(prompt="hi", user_id="u"),
    ))

    assert isinstance(events[-1], FinalEvent)
    provider.os.killpg.assert_not_called()


def test_malformed_stream_log_does_not_include_line_content(caplog) -> None:
    caplog.set_level("WARNING")

    assert _safe_json_loads(b"secret-user-content") is None

    assert "secret-user-content" not in caplog.text


def test_stream_cmd_uses_stream_json_and_verbose() -> None:
    from agent_providers.claude.provider import _mcp_cli_arguments
    from agent_providers.config import current_turn_config

    cmd = _mcp_cli_arguments(
        "opus", "/tmp/mcp.json", current_turn_config().mcp_server, stream=True,
    )
    assert "--output-format" in cmd
    idx = cmd.index("--output-format")
    assert cmd[idx + 1] == "stream-json"
    assert "--verbose" in cmd


def test_stream_cmd_non_stream_keeps_json() -> None:
    from agent_providers.claude.provider import _mcp_cli_arguments
    from agent_providers.config import current_turn_config

    cmd = _mcp_cli_arguments(
        "opus", "/tmp/mcp.json", current_turn_config().mcp_server,
    )
    idx = cmd.index("--output-format")
    assert cmd[idx + 1] == "json"
    assert "--verbose" not in cmd


def test_iter_lines_returns_immediately_when_stdout_missing() -> None:
    from agent_providers.claude.provider import _iter_lines

    async def _run() -> list[bytes]:
        out: list[bytes] = []
        async for line in _iter_lines(None, 1):
            out.append(line)
        return out

    assert asyncio.run(_run()) == []


def test_stream_consumes_oversized_tool_result_line(monkeypatch) -> None:
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )

    huge_payload = "x" * (256 * 1024)
    lines = [
        _tool_use_line("mcp__notes__get_note", "tu-1", {"note_id": "s1"}),
        _tool_result_line("tu-1", huge_payload),
        _result_line("done"),
    ]

    async def fake_exec(*_cmd, **kwargs):
        proc = _make_fake_proc([])
        proc.stdout = asyncio.StreamReader(limit=kwargs.get("limit", _ASYNCIO_DEFAULT_LIMIT))
        for line in lines:
            proc.stdout.feed_data(line)
        proc.stdout.feed_eof()
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    events = asyncio.run(_collect(
        acall_claude_with_mcp_stream(prompt="hi", user_id="u"),
    ))
    tool_results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_results) == 1
    assert tool_results[0].content == huge_payload
    assert isinstance(events[-1], FinalEvent)


# ── error messages and error results ─────────────────────────────────

LOGIN_REFUSED_TEXT = "Not logged in · Please run /login"


def _api_error_assistant_line(error: str, text: str = LOGIN_REFUSED_TEXT) -> bytes:
    return json.dumps({
        "type": "assistant",
        "error": error,
        "is_api_error_message": True,
        "message": {"content": [{"type": "text", "text": text}]},
    }).encode() + b"\n"


def _error_result_line(text: str = LOGIN_REFUSED_TEXT) -> bytes:
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "terminal_reason": "api_error",
        "result": text,
    }).encode() + b"\n"


def _stream_outcome(monkeypatch, lines: list[bytes], returncode: int = 0):
    monkeypatch.setattr(provider, "_require_claude_binary", lambda: "/bin/claude")

    async def fake_exec(*_cmd, **_kw):
        return _make_fake_proc(lines, returncode=returncode)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    seen: list[StreamEvent] = []

    async def drive() -> None:
        async for event in acall_claude_with_mcp_stream(prompt="hi", user_id="u"):
            seen.append(event)

    try:
        asyncio.run(drive())
    except UnavailableError as failure:
        return seen, failure
    return seen, None


def _chain_text(failure: BaseException | None) -> str:
    texts = []
    while failure is not None:
        texts.append(f"{failure!r} {failure}")
        failure = failure.__cause__ or failure.__context__
    return " ".join(texts)


def _drive_over_the_turn_transport(monkeypatch, lines: list[bytes], returncode: int):
    from agent_providers.claude.adapter import stream_claude_turn

    monkeypatch.setattr(provider, "_require_claude_binary", lambda: "/bin/claude")

    async def fake_exec(*_cmd, **_kw):
        return _make_fake_proc(lines, returncode=returncode)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    seen: list[StreamEvent] = []

    async def drive() -> None:
        async for event in stream_claude_turn(
            user_id="u", system="s", model="claude-test", messages=[],
        ):
            seen.append(event)

    try:
        asyncio.run(drive())
    except ProviderUnavailableError as failure:
        return seen, failure
    return seen, None


@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.parametrize(
    ("error", "expected_class", "expected_detail", "expected_code"),
    [
        (
            "authentication_failed",
            CliLoginRefusedError,
            CLAUDE_CLI_LOGIN_REFUSED_DETAIL,
            SafeRouteReasonCode.CLI_AUTH_REJECTED,
        ),
        (
            "rate_limit",
            UnavailableError,
            CLAUDE_CLI_UNAVAILABLE_DETAIL,
            SafeRouteReasonCode.CLI_PROTOCOL_ERROR,
        ),
    ],
    ids=["login-refused", "other-api-error"],
)
def test_api_error_message_raises_before_any_text_is_yielded(
    monkeypatch, caplog, error, expected_class, expected_detail, expected_code, returncode,
) -> None:
    caplog.set_level("DEBUG")
    lines = [_api_error_assistant_line(error), _error_result_line()]

    seen, failure = _stream_outcome(monkeypatch, lines, returncode=returncode)

    assert type(failure) is expected_class
    assert str(failure) == expected_detail
    assert seen == []
    assert LOGIN_REFUSED_TEXT not in f"{_chain_text(failure)} {caplog.text}"

    turn_seen, turn_failure = _drive_over_the_turn_transport(monkeypatch, lines, returncode)

    assert turn_failure.reason.code is expected_code
    assert turn_seen == []
    assert LOGIN_REFUSED_TEXT not in _chain_text(turn_failure)


@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.parametrize(
    "lines",
    [
        [_error_result_line(LOGIN_REFUSED_TEXT)],
        [_assistant_line("partial "), _error_result_line(LOGIN_REFUSED_TEXT)],
    ],
    ids=["error-result-only", "error-result-after-partial-text"],
)
def test_error_result_raises_a_plain_unavailable_error_without_its_text(
    monkeypatch, caplog, lines, returncode,
) -> None:
    caplog.set_level("DEBUG")

    seen, failure = _stream_outcome(monkeypatch, lines, returncode=returncode)

    assert type(failure) is UnavailableError
    assert str(failure) == CLAUDE_CLI_UNAVAILABLE_DETAIL
    assert not any(isinstance(e, FinalEvent) for e in seen)
    assert LOGIN_REFUSED_TEXT not in f"{_chain_text(failure)} {caplog.text}"


def test_stream_delivers_every_block_of_a_message_in_order(monkeypatch) -> None:
    lines = [
        json.dumps(_assistant_payload(
            {"type": "text", "text": "looking"},
            _a_tool_use_block("tu-1"),
            _a_tool_use_block("tu-2"),
        )).encode() + b"\n",
        json.dumps(_user_payload(
            _a_tool_result_block("tu-1", "a"), _a_tool_result_block("tu-2", "b"),
        )).encode() + b"\n",
        _result_line("done"),
    ]

    seen, failure = _stream_outcome(monkeypatch, lines)

    assert failure is None
    assert [e.type for e in seen] == [
        "assistant_text", "tool_call", "tool_call", "tool_result", "tool_result", "final",
    ]
    assert [e.tool_use_id for e in seen[1:5]] == ["tu-1", "tu-2", "tu-1", "tu-2"]
