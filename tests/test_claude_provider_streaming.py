"""Tests for the streaming Claude provider path."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest
from provider_test_support import (
    a_claude_cli_child,
    held_claude_slots,
    install_claude_cli_children,
    operations,
    reap_without_waiting,
    run_bounded,
)

from agent_providers.claude import provider
from agent_providers.claude.provider import (
    _STREAM_BUFFER_LIMIT,
    CLAUDE_CLI_LOGIN_REFUSED_DETAIL,
    CLAUDE_CLI_UNAVAILABLE_DETAIL,
    CliLoginRefusedError,
    UnavailableError,
    _parse_announced_surface,
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


@pytest.fixture(autouse=True)
def _no_real_killpg(monkeypatch):
    monkeypatch.setattr(provider.os, "killpg", MagicMock())


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
    [ev] = _parse_stream_event(
        _assistant_payload({"type": "text", "text": "hello"}), A_CORRELATION_ID,
    )
    assert isinstance(ev, AssistantTextEvent)
    assert ev.text == "hello"
    assert ev.correlation_id == A_CORRELATION_ID


def test_parse_assistant_tool_use_event() -> None:
    [ev] = _parse_stream_event(
        _assistant_payload(_a_tool_use_block("tu-1", x=1)), A_CORRELATION_ID,
    )
    assert isinstance(ev, ToolCallEvent)
    assert ev.tool_use_id == "tu-1"
    assert ev.name == "foo"
    assert ev.input == {"x": 1}


def test_parse_assistant_text_and_two_tool_uses_yield_three_events_in_order() -> None:
    events = _parse_stream_event(
        _assistant_payload(
            {"type": "text", "text": "looking"},
            _a_tool_use_block("tu-1"),
            _a_tool_use_block("tu-2"),
        ),
        A_CORRELATION_ID,
    )
    assert [e.type for e in events] == ["assistant_text", "tool_call", "tool_call"]
    assert [e.tool_use_id for e in events[1:]] == ["tu-1", "tu-2"]


def test_parse_user_two_tool_results_yield_two_events_in_order() -> None:
    events = _parse_stream_event(
        _user_payload(_a_tool_result_block("tu-1", "a"), _a_tool_result_block("tu-2", "b")),
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
        _user_payload(_a_tool_result_block("tu-1", content)), A_CORRELATION_ID,
    )
    assert ev.content == expected


def test_parse_result_event_returns_final() -> None:
    [ev] = _parse_stream_event({"type": "result", "result": "done"}, A_CORRELATION_ID)
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
    assert _parse_stream_event(payload, A_CORRELATION_ID) == []


A_MARKER = "MARKER-7f3a9c"


def _a_json_object_with_marker(encoding: str) -> bytes:
    return json.dumps({"type": "system", "note": A_MARKER}).encode(encoding)


A_TOO_LONG_INTEGER_LINE = b'{"' + A_MARKER.encode() + b'": ' + b"9" * 5000 + b"}"


def assert_no_marker_in_failure(error: BaseException, caplog) -> None:
    carried = [str(error), repr(error), repr(error.args)]
    carried.append(repr(getattr(error, "obj", "")))
    carried.append(repr(getattr(error, "object", "")))
    assert [A_MARKER in text for text in carried] == [False] * len(carried)
    assert (error.__cause__, error.__context__) == (None, None)
    assert A_MARKER not in caplog.text


@pytest.mark.parametrize(
    ("line", "error_class"),
    [
        (b"\xff" + _a_json_object_with_marker("utf-8"), "UnicodeDecodeError"),
        (_a_json_object_with_marker("utf-16"), "UnicodeDecodeError"),
        (_a_json_object_with_marker("utf-32"), "UnicodeDecodeError"),
        (_a_json_object_with_marker("utf-16-le"), "JSONDecodeError"),
        (_a_json_object_with_marker("utf-32-le"), "JSONDecodeError"),
        (A_TOO_LONG_INTEGER_LINE, "ValueError"),
    ],
    ids=["non-utf8", "utf16-bom", "utf32-bom", "utf16le", "utf32le", "int-digits"],
)
def test_a_line_that_is_not_utf8_json_is_skipped_with_a_class_only_warning(
    line, error_class, caplog,
) -> None:
    caplog.set_level("WARNING")

    assert _safe_json_loads(line + b"\n") is None

    [record] = caplog.records
    assert "skipping malformed JSON line" in record.message
    assert error_class in record.message
    assert "bytes" in record.message
    assert "9999" not in caplog.text
    assert A_MARKER not in caplog.text


def test_a_decoder_recursion_error_is_skipped_with_a_class_only_warning_and_never_its_message(
    monkeypatch, caplog,
) -> None:
    def too_deep(_text: str) -> None:
        raise RecursionError(A_MARKER)

    monkeypatch.setattr(provider.json, "loads", too_deep)
    caplog.set_level("WARNING")

    assert _safe_json_loads(_a_json_object_with_marker("utf-8") + b"\n") is None

    [record] = caplog.records
    assert "skipping malformed JSON line" in record.message
    assert "RecursionError" in record.message
    assert A_MARKER not in caplog.text


def test_a_line_nested_deeper_than_the_decoder_can_parse_is_skipped_with_a_class_only_warning(
    caplog,
) -> None:
    depth = 1_000_000
    line = b"[" * depth + b"]" * depth + b"\n"
    caplog.set_level("WARNING")
    outcome: list[dict | None] = []
    previous_stack_size = threading.stack_size(16 << 20)
    try:
        worker = threading.Thread(target=lambda: outcome.append(_safe_json_loads(line)))
        worker.start()
    finally:
        threading.stack_size(previous_stack_size)
    worker.join()

    assert outcome == [None]
    [record] = caplog.records
    assert "RecursionError" in record.message
    assert "[[[" not in caplog.text


@pytest.mark.parametrize(
    ("payload", "error_class"),
    [
        ({"type": "assistant", "message": A_MARKER}, "AttributeError"),
        (_assistant_payload(A_MARKER), "AttributeError"),
        (_user_payload(A_MARKER), "AttributeError"),
        ({"type": "user", "message": {"content": 7}}, "TypeError"),
        (_assistant_payload({"type": "text", "text": {"k": A_MARKER}}), "ValidationError"),
        (
            _assistant_payload(_a_tool_use_block("tu-1") | {"input": A_MARKER}),
            "ValidationError",
        ),
        (_user_payload(_a_tool_result_block({"k": A_MARKER})), "ValidationError"),
        (_user_payload(_a_tool_result_block("tu-1", [{"text": 7.5}])), "TypeError"),
    ],
    ids=[
        "message-string", "assistant-block-string", "user-block-string",
        "content-integer", "text-object", "tool-input-string",
        "tool-use-id-object", "result-item-text-number",
    ],
)
def test_a_refused_field_shape_fails_closed_and_unchained(payload, error_class, caplog) -> None:
    caplog.set_level("WARNING")

    with pytest.raises(UnavailableError) as refused:
        _parse_stream_event(payload, A_CORRELATION_ID)

    assert str(refused.value) == CLAUDE_CLI_UNAVAILABLE_DETAIL
    assert_no_marker_in_failure(refused.value, caplog)
    [record] = caplog.records
    assert "refusing a malformed event" in record.message
    assert error_class in record.message


def _parse_an_error_result() -> None:
    _parse_stream_event({"type": "result", "is_error": True}, A_CORRELATION_ID)


def _parse_an_api_error() -> None:
    _parse_stream_event({"type": "assistant", "is_api_error_message": True}, A_CORRELATION_ID)


def _parse_a_login_refusal() -> None:
    _parse_stream_event(
        {"type": "assistant", "is_api_error_message": True, "error": "authentication_failed"},
        A_CORRELATION_ID,
    )


def _parse_a_malformed_event() -> None:
    _parse_stream_event({"type": "assistant", "message": A_MARKER}, A_CORRELATION_ID)


def _parse_a_probe_init() -> None:
    _parse_announced_surface(b"not json\n", mcp=None)


@pytest.mark.parametrize(
    "parse",
    [
        _parse_a_malformed_event,
        _parse_an_error_result,
        _parse_an_api_error,
        _parse_a_login_refusal,
        _parse_a_probe_init,
    ],
    ids=["malformed-event", "error-result", "api-error", "login-refused", "probe-init"],
)
def test_a_refusal_raised_while_the_caller_handles_a_marker_error_keeps_no_chain(
    parse, caplog,
) -> None:
    caplog.set_level("WARNING")
    marker_error = UnicodeDecodeError("utf-8", A_MARKER.encode(), 0, 1, "invalid start byte")
    refusal: UnavailableError | None = None

    try:
        raise marker_error
    except UnicodeDecodeError:
        try:
            parse()
        except UnavailableError as refused:
            refusal = refused

    assert refusal is not None
    assert_no_marker_in_failure(refusal, caplog)


def _payload_of(event: StreamEvent) -> Any:
    return {
        "assistant_text": lambda: event.text,
        "tool_call": lambda: event.input,
        "tool_result": lambda: event.content,
    }[event.type]()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"type": "assistant", "message": 0}, []),
        (_assistant_payload({"type": "text", "text": {}}), [("assistant_text", "")]),
        (
            _assistant_payload({"type": "tool_use", "id": "t", "name": "n", "input": []}),
            [("tool_call", {})],
        ),
        (_user_payload(_a_tool_result_block("t", {"k": "v"})), [("tool_result", "")]),
        ({"type": "result", "result": {"k": "v"}}, []),
    ],
    ids=["message-zero", "text-empty-object", "input-empty-list", "result-content-object",
         "result-object"],
)
def test_a_tolerated_field_shape_is_normalized_as_before(payload, expected) -> None:
    events = _parse_stream_event(payload, A_CORRELATION_ID)

    assert [(e.type, _payload_of(e)) for e in events] == expected


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

    install_claude_cli_children(monkeypatch, a_claude_cli_child(stream_lines=lines))

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

    install_claude_cli_children(monkeypatch, a_claude_cli_child(stream_lines=lines))

    events = asyncio.run(_collect(
        acall_claude_with_mcp_stream(prompt="hi", user_id="u"),
    ))
    assert [e.type for e in events] == ["assistant_text", "final"]
    assert any("malformed" in r.message.lower() for r in caplog.records)


@pytest.mark.parametrize(
    ("lines", "outcome"),
    [
        (
            [b"\xff" + _a_json_object_with_marker("utf-8") + b"\n",
             _assistant_line("hello"), _result_line("hello")],
            ["assistant_text", "final"],
        ),
        (
            [_assistant_line("hello"),
             json.dumps(_assistant_payload({"type": "text", "text": {"k": A_MARKER}})).encode()
             + b"\n"],
            ["assistant_text", UnavailableError],
        ),
    ],
    ids=["undecodable-line-skipped", "refused-shape-ends-the-stream"],
)
def test_the_old_stream_keeps_cli_bytes_out_of_a_malformed_lines_outcome(
    lines, outcome, monkeypatch, caplog,
) -> None:
    monkeypatch.setattr(provider, "_require_claude_binary", lambda: "/bin/claude")
    caplog.set_level("WARNING")
    install_claude_cli_children(monkeypatch, a_claude_cli_child(stream_lines=lines))
    seen: list[StreamEvent] = []

    async def drain() -> None:
        async for event in acall_claude_with_mcp_stream(prompt="hi", user_id="u"):
            seen.append(event)

    failure: UnavailableError | None = None
    try:
        asyncio.run(drain())
    except UnavailableError as error:
        failure = error

    assert [e.type for e in seen] == [o for o in outcome if isinstance(o, str)]
    assert A_MARKER not in repr(seen)
    assert A_MARKER not in caplog.text
    assert held_claude_slots() == 0
    if UnavailableError in outcome:
        assert failure is not None
        assert str(failure) == CLAUDE_CLI_UNAVAILABLE_DETAIL
        assert_no_marker_in_failure(failure, caplog)
    else:
        assert failure is None
        assert seen[-1].text == "hello"


def test_stream_regression_matches_non_streaming_final_text(monkeypatch) -> None:
    """Final event must equal result.result, matching non-streaming json path."""
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )
    expected = "The quick brown fox"
    lines = [
        _assistant_line("The quick "),
        _assistant_line("brown fox"),
        _result_line(f" {expected}\n"),
    ]

    install_claude_cli_children(monkeypatch, a_claude_cli_child(stream_lines=lines))

    events = asyncio.run(_collect(
        acall_claude_with_mcp_stream(prompt="hi", user_id="u"),
    ))
    final = [e for e in events if isinstance(e, FinalEvent)]
    assert len(final) == 1
    assert final[0].text == expected


def test_a_stream_without_a_result_line_fails(monkeypatch) -> None:
    lines = [_assistant_line("piece one "), _assistant_line("piece two")]

    seen, failure = _stream_outcome(monkeypatch, lines)

    assert (type(failure), str(failure)) == (UnavailableError, CLAUDE_CLI_UNAVAILABLE_DETAIL)
    assert [e.type for e in seen] == ["assistant_text", "assistant_text"]


def test_stream_nonzero_exit_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )

    install_claude_cli_children(monkeypatch, a_claude_cli_child(returncode=2, stderr=b"boom"))

    stream = acall_claude_with_mcp_stream(prompt="hi", user_id="u")
    collected = _collect(stream)
    with pytest.raises(UnavailableError):
        asyncio.run(collected)


def test_stream_binary_missing_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )

    def binary_vanishes(_child, _limit):
        raise FileNotFoundError

    install_claude_cli_children(monkeypatch, factory=binary_vanishes)

    stream = acall_claude_with_mcp_stream(prompt="hi", user_id="u")
    collected = _collect(stream)
    with pytest.raises(UnavailableError):
        asyncio.run(collected)


def test_stream_timeout_ends_the_leader_through_its_anchor(monkeypatch) -> None:
    reap_without_waiting(monkeypatch)
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )
    child = a_claude_cli_child(returncode=None, stdout_stalls=True)
    install_claude_cli_children(monkeypatch, child)

    stream = acall_claude_with_mcp_stream(
        prompt="hi", user_id="u", timeout_seconds=0.1,
    )
    collected = _collect(stream)
    with pytest.raises(UnavailableError):
        run_bounded(collected)

    assert operations(child.log) == [
        ("SIGTERM", child.pid), ("kill_until_gone", child.pid), ("release", child.pid),
    ]
    provider.os.killpg.assert_not_called()


def test_a_completed_stream_kills_and_counts_its_group_before_releasing_its_leader(
    monkeypatch,
) -> None:
    reap_without_waiting(monkeypatch)
    monkeypatch.setattr(
        provider, "_require_claude_binary", lambda: "/bin/claude",
    )
    child = a_claude_cli_child(stream_lines=[_result_line("ok")])
    install_claude_cli_children(monkeypatch, child)

    events = run_bounded(_collect(
        acall_claude_with_mcp_stream(prompt="hi", user_id="u"),
    ))

    assert isinstance(events[-1], FinalEvent)
    assert operations(child.log) == [("kill_until_gone", child.pid), ("release", child.pid)]
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

    spawns = install_claude_cli_children(
        monkeypatch,
        factory=lambda _child, limit: a_claude_cli_child(
            stream_lines=lines, stdout_limit=limit,
        ),
    )

    events = asyncio.run(_collect(
        acall_claude_with_mcp_stream(prompt="hi", user_id="u"),
    ))
    assert spawns.stream_buffer_limits == [_STREAM_BUFFER_LIMIT]
    assert _STREAM_BUFFER_LIMIT >= len(lines[1])
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


def _stream_outcome(monkeypatch, lines: list[bytes], returncode: int = 0, **script):
    monkeypatch.setattr(provider, "_require_claude_binary", lambda: "/bin/claude")

    install_claude_cli_children(
        monkeypatch, a_claude_cli_child(stream_lines=lines, returncode=returncode, **script),
    )
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


def _drive_over_the_turn_transport(monkeypatch, lines: list[bytes], returncode: int, **script):
    from agent_providers.claude.adapter import stream_claude_turn

    monkeypatch.setattr(provider, "_require_claude_binary", lambda: "/bin/claude")

    install_claude_cli_children(
        monkeypatch, a_claude_cli_child(stream_lines=lines, returncode=returncode, **script),
    )
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


def test_the_api_error_flag_decides_not_the_error_field(monkeypatch) -> None:
    flagged_without_error_field = json.dumps({
        "type": "assistant",
        "is_api_error_message": True,
        "message": {"content": [{"type": "text", "text": LOGIN_REFUSED_TEXT}]},
    }).encode() + b"\n"

    seen, failure = _stream_outcome(monkeypatch, [flagged_without_error_field])

    assert type(failure) is UnavailableError
    assert seen == []


def test_an_error_field_without_the_api_error_flag_still_yields_its_text(
    monkeypatch,
) -> None:
    unflagged_with_error_field = json.dumps({
        "type": "assistant",
        "error": "authentication_failed",
        "is_api_error_message": False,
        "message": {"content": [{"type": "text", "text": "fine"}]},
    }).encode() + b"\n"

    seen, failure = _stream_outcome(monkeypatch, [unflagged_with_error_field, _result_line("fine")])

    assert failure is None
    assert [e.type for e in seen] == ["assistant_text", "final"]


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


def test_every_event_and_the_ending_carry_the_turns_correlation_id(monkeypatch) -> None:
    monkeypatch.setattr(provider, "_require_claude_binary", lambda: "/bin/claude")
    lines = [
        _assistant_line("Hi"),
        _tool_use_line("mcp__notes__get_note", "tu-1", {"note_id": "s1"}),
        _tool_result_line("tu-1", "note data"),
        _result_line("Hi."),
    ]
    install_claude_cli_children(monkeypatch, a_claude_cli_child(stream_lines=lines))

    events = asyncio.run(_collect(
        acall_claude_with_mcp_stream(
            prompt="hi", user_id="u", correlation_id=A_CORRELATION_ID,
        ),
    ))

    assert events == [
        AssistantTextEvent(text="Hi", correlation_id=A_CORRELATION_ID),
        ToolCallEvent(
            tool_use_id="tu-1",
            name="mcp__notes__get_note",
            input={"note_id": "s1"},
            correlation_id=A_CORRELATION_ID,
        ),
        ToolResultEvent(
            tool_use_id="tu-1", content="note data", correlation_id=A_CORRELATION_ID,
        ),
        FinalEvent(text="Hi.", correlation_id=A_CORRELATION_ID),
    ]


def test_the_old_stream_accepts_an_init_that_announces_the_hosts_own_server(
    monkeypatch,
) -> None:
    init = json.dumps({
        "type": "system",
        "subtype": "init",
        "tools": ["mcp__notes__get_note"],
        "mcp_servers": [{"name": "notes", "status": "connected"}],
        "apiKeySource": "none",
    }).encode() + b"\n"

    seen, failure = _stream_outcome(
        monkeypatch, [init, _assistant_line("hi"), _result_line("hi")],
    )

    assert (failure, [e.type for e in seen]) == (None, ["assistant_text", "final"])


def test_a_stream_that_exits_non_zero_after_its_result_fails(monkeypatch) -> None:
    seen, failure = _stream_outcome(
        monkeypatch, [_assistant_line("hi"), _result_line("hi")], returncode=3,
    )

    assert (type(failure), str(failure)) == (UnavailableError, CLAUDE_CLI_UNAVAILABLE_DETAIL)
    assert [e.type for e in seen] == ["assistant_text"]


def test_a_stream_without_a_result_line_ends_the_transport_with_a_protocol_error(
    monkeypatch,
) -> None:
    seen, failure = _drive_over_the_turn_transport(
        monkeypatch, [_assistant_line("partial")], returncode=0,
    )

    assert failure.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
    assert not any(isinstance(e, FinalEvent) for e in seen)


A_TOO_LONG_LINE_LIMIT = 64


def _an_over_long_marker_line() -> bytes:
    return _assistant_line(A_MARKER + "x" * (2 * A_TOO_LONG_LINE_LIMIT))


def test_an_over_long_stream_line_ends_the_turn_closed_and_without_its_bytes(
    monkeypatch, caplog,
) -> None:
    caplog.set_level("DEBUG")

    seen, failure = _stream_outcome(
        monkeypatch, [_an_over_long_marker_line()], stdout_limit=A_TOO_LONG_LINE_LIMIT,
    )

    assert (type(failure), str(failure), seen) == (
        UnavailableError, CLAUDE_CLI_UNAVAILABLE_DETAIL, [],
    )
    assert_no_marker_in_failure(failure, caplog)
    assert held_claude_slots() == 0


def test_an_over_long_stream_line_ends_the_transport_with_a_protocol_error_and_a_clean_chain(
    monkeypatch, caplog,
) -> None:
    caplog.set_level("DEBUG")

    seen, failure = _drive_over_the_turn_transport(
        monkeypatch, [_an_over_long_marker_line()], returncode=0,
        stdout_limit=A_TOO_LONG_LINE_LIMIT,
    )

    assert failure.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
    assert seen == []
    assert A_MARKER not in f"{_chain_text(failure)} {caplog.text}"


def _run_stream_scenario(monkeypatch, child, scenario) -> None:
    reap_without_waiting(monkeypatch)
    monkeypatch.setattr(provider, "_require_claude_binary", lambda: "/bin/claude")
    install_claude_cli_children(monkeypatch, child)
    run_bounded(scenario())


def _only_the_scenario_runs() -> bool:
    return asyncio.all_tasks() == {asyncio.current_task()}


def _unretrieved_task_errors(caplog) -> list[logging.LogRecord]:
    gc.collect()
    return [r for r in caplog.records if "never retrieved" in r.getMessage()]


def test_a_stderr_that_never_closes_does_not_hold_the_stream_after_its_exit(
    monkeypatch,
) -> None:
    child = a_claude_cli_child(
        stream_lines=[_assistant_line("hi"), _result_line("hi")], stderr_stalls=True,
    )
    ended: list[bool] = []

    async def scenario() -> None:
        events = await _collect(acall_claude_with_mcp_stream(prompt="hi", user_id="u"))
        ended.append(isinstance(events[-1], FinalEvent) and _only_the_scenario_runs())

    _run_stream_scenario(monkeypatch, child, scenario)

    assert ended == [True]


def test_a_failed_stderr_read_does_not_fail_the_stream(monkeypatch, caplog) -> None:
    child = a_claude_cli_child(
        stream_lines=[_assistant_line("hi"), _result_line("hi")],
        stderr_failure=OSError("stderr read failed"),
    )
    seen: list[StreamEvent] = []
    drained: list[bool] = []

    async def scenario() -> None:
        stream = acall_claude_with_mcp_stream(prompt="hi", user_id="u")
        seen.append(await anext(stream))
        await child.stderr_failed.wait()
        async for event in stream:
            seen.append(event)
        drained.append(_only_the_scenario_runs())

    _run_stream_scenario(monkeypatch, child, scenario)

    assert ([e.type for e in seen], drained) == (["assistant_text", "final"], [True])
    assert _unretrieved_task_errors(caplog) == []


def test_a_parser_refusal_survives_a_completed_stderr_failure(monkeypatch, caplog) -> None:
    stderr_failure = OSError("stderr-read-failed-marker")
    refused_shape = json.dumps(
        _assistant_payload({"type": "text", "text": {"k": A_MARKER}}),
    ).encode() + b"\n"
    child = a_claude_cli_child(
        stream_lines=[_assistant_line("hi"), refused_shape], stderr_failure=stderr_failure,
    )
    refusals: list[tuple[str, str]] = []

    async def scenario() -> None:
        stream = acall_claude_with_mcp_stream(prompt="hi", user_id="u")
        await anext(stream)
        await child.stderr_failed.wait()
        try:
            await anext(stream)
        except UnavailableError as refusal:
            refusals.append((str(refusal), _chain_text(refusal)))

    _run_stream_scenario(monkeypatch, child, scenario)

    [(message, chain)] = refusals
    assert message == CLAUDE_CLI_UNAVAILABLE_DETAIL
    assert "stderr-read-failed-marker" not in chain
    assert _unretrieved_task_errors(caplog) == []


def test_the_final_event_arrives_before_the_leader_is_released(monkeypatch) -> None:
    child = a_claude_cli_child(stream_lines=[_result_line("ok")])
    held_after_final: list[tuple[list[tuple[str, int]], int]] = []
    held_after_close: list[tuple[list[tuple[str, int]], int]] = []

    async def scenario() -> None:
        stream = acall_claude_with_mcp_stream(prompt="hi", user_id="u")
        final = await anext(stream)
        assert isinstance(final, FinalEvent)
        held_after_final.append((operations(child.log), held_claude_slots()))
        await stream.aclose()
        held_after_close.append((operations(child.log), held_claude_slots()))

    _run_stream_scenario(monkeypatch, child, scenario)

    assert held_after_final == [([], 1)]
    assert held_after_close == [
        ([("kill_until_gone", child.pid), ("release", child.pid)], 0),
    ]
