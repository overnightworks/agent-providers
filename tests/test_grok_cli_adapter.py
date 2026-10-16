"""Grok subscription CLI transport for the shared co-writer tool loop."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from provider_test_support import (
    CALLER_CONTEXTS,
    COWRITER_TOOL_CATALOG,
    DEEPLY_NESTED_JSON,
    GROK_SESSION_ID,
    a_call_nested_too_deeply_to_decode,
    a_call_to_an_undeclared_tool_carrying,
    assert_a_multi_result_batch_is_rejected,
    assert_private_credential_child,
    assert_the_canary_reaches_only_the_host,
    drive_cowriter_turn,
    formatted_log_text,
    grok_cli_outcome,
    grok_cli_runner,
    grok_tool_round_lines,
    override_provider_runtime,
)

from agent_providers.config import current_config, current_turn_config
from agent_providers.errors import (
    ProviderUnavailableError,
    SafeRouteReasonCode,
    TextToolCallRejectedError,
)
from agent_providers.events import AssistantTextEvent, FinalEvent, ToolCallEvent
from agent_providers.grok import transport as grok_cli_adapter
from agent_providers.process import CliRunOutcome, CliRunReason
from agent_providers.text_tool_protocol import TextToolRejectionRule
from agent_providers.tool_loop import (
    InitialTurn,
    ToolCallBatch,
    ToolOutcome,
    ToolResult,
    ToolResultBatch,
    stream_tool_loop,
)

A_TOOL_FAILURE_MESSAGE = "Co-Writer tool failed."


def _transport() -> grok_cli_adapter.GrokCliToolTransport:
    """Build the transport under test with songmaker's own tool catalog."""
    return grok_cli_adapter.GrokCliToolTransport(
        model="grok-test", catalog=COWRITER_TOOL_CATALOG,
    )


def test_grok_turn_without_a_configured_credential_is_logged_out(tmp_path: Path) -> None:
    override_provider_runtime(grok_cli_auth_file=tmp_path / "missing.json")

    with pytest.raises(ProviderUnavailableError) as raised:
        _transport()

    assert raised.value.reason.code is SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED


class _OneWorkerExecutor(ThreadPoolExecutor):
    """The loop's only worker, reporting when the job queued behind the busy one arrives."""

    def __init__(self) -> None:
        super().__init__(max_workers=1)
        self.second_job_queued = asyncio.Event()
        self._submitted = 0

    def submit(self, fn, /, *args, **kwargs):
        future = super().submit(fn, *args, **kwargs)
        self._submitted += 1
        if self._submitted == 2:
            self.second_job_queued.set()
        return future


def test_grok_credential_home_is_removed_when_aclose_is_cancelled_while_queued() -> None:
    root = current_config().cli_working_directory_root
    before = set(root.iterdir())
    transport = _transport()
    homes = set(root.iterdir()) - before
    assert homes
    worker_may_continue = threading.Event()

    async def cancel_while_the_close_is_queued() -> None:
        executor = _OneWorkerExecutor()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(executor)
        busy_worker = loop.run_in_executor(None, worker_may_continue.wait)
        closing = asyncio.create_task(transport.aclose())
        await executor.second_job_queued.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        worker_may_continue.set()
        await busy_worker
        await loop.run_in_executor(None, lambda: None)
        assert not any(home.exists() for home in homes)
        await transport.aclose()

    asyncio.run(cancel_while_the_close_is_queued())

    assert not any(home.exists() for home in homes)


def _tool_call_text() -> str:
    return (
        '<songmaker_tool_call>\n'
        '{"name":"list_songs","arguments":{}}\n'
        '</songmaker_tool_call>'
    )


def _recorded_grok_tool_round_lines() -> list[bytes]:
    fixture = Path(__file__).with_name("fixtures") / "grok_cli_real_tool_stream.jsonl"
    return [line.encode() + b"\n" for line in fixture.read_text().splitlines()]


def _tool_transport_events(transport, executor):
    return stream_tool_loop(
        provider="grok",
        route="cli",
        system="system",
        messages=[{"role": "user", "content": "hello"}],
        transport=transport,
        executor=executor,
        tool_failure_message=A_TOOL_FAILURE_MESSAGE,
    )


async def _collect_transport_responses(transport) -> list[object]:
    return [item async for item in transport.stream(InitialTurn("system", []))]


def test_grok_tool_command_pins_native_tool_and_web_isolation() -> None:
    model = "grok-test"
    session_id = "3e04bf5b-4e1c-4f26-8e1e-2f17c5f6d9cf"
    common = (
        "--prompt-file",
        current_turn_config().cli_prompt_file_placeholder,
        "--output-format",
        "streaming-json",
        "--deny",
        "*",
        "--max-turns",
        "1",
        "--no-subagents",
        "--disable-web-search",
        "--model",
        model,
    )

    assert grok_cli_adapter._grok_cli_tool_arguments(model) == common
    assert grok_cli_adapter._grok_cli_tool_arguments(model, session_id) == (
        *common,
        "--resume",
        session_id,
    )


def test_grok_tool_transport_starts_then_resumes_in_one_private_credential_home(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls = []
    homes: list[Path] = []
    source = current_config().grok_cli_auth_file
    rounds = iter([
        grok_tool_round_lines(_tool_call_text()),
        grok_tool_round_lines("done"),
        grok_tool_round_lines("another turn"),
    ])

    def run_cli_bounded(child, **kwargs):
        calls.append((child, kwargs))
        homes.append(assert_private_credential_child(
            child,
            root=tmp_path,
            source=source,
            destination=Path(".grok/auth.json"),
        ))
        for line in next(rounds):
            assert kwargs["stdout_line_channel"]._send(line)
        outcome = grok_cli_outcome()
        kwargs["stdout_line_channel"]._close(outcome)
        kwargs["on_reaped"](1, False)
        return outcome

    monkeypatch.setattr(grok_cli_adapter, "run_cli_bounded", run_cli_bounded)
    transport = _transport()

    async def collect_and_close() -> list[object]:
        events = await _collect_tool_events(
            _tool_transport_events(
                transport, lambda _name, _arguments: ToolOutcome('{"songs":[]}', False),
            ),
        )
        await transport.aclose()
        return events

    events = asyncio.run(collect_and_close())

    another_transport = _transport()

    async def collect_another_turn() -> list[object]:
        responses = await _collect_transport_responses(another_transport)
        await another_transport.aclose()
        return responses

    assert asyncio.run(collect_another_turn())[-1] == grok_cli_adapter.FinalText("")

    assert isinstance(events[0], ToolCallEvent)
    assert events[-1] == FinalEvent(text="done")
    (first_child, first_kwargs), (second_child, second_kwargs), (third_child, _) = calls
    first_command, second_command = first_child.command, second_child.command
    assert "--session-id" not in first_command
    assert "--resume" not in first_command
    assert second_command[-2:] == ("--resume", GROK_SESSION_ID)
    for child, kwargs in calls:
        assert child.command.count("--deny") == 1
        assert child.command[child.command.index("--deny") + 1] == "*"
        assert kwargs["stdin_payload"] is None
        assert kwargs["prompt_file_arg_index"] == 1
        assert kwargs["output_read_limit_bytes"] == (
            grok_cli_adapter.GROK_CLI_TURN_OUTPUT_READ_LIMIT_BYTES
        )
    assert first_kwargs["prompt_file_bytes"] == b"system\n\nUser: hello"
    assert second_kwargs["prompt_file_bytes"] == (
        b'<songmaker_tool_result>\n{"songs":[]}\n</songmaker_tool_result>'
    )
    assert first_kwargs["deadline"] == second_kwargs["deadline"]
    assert "GROK_HOME" not in first_child.environment
    first_home = homes[0]
    second_home = second_child.working_directory
    third_home = third_child.working_directory
    assert second_home == first_home
    assert third_home != first_home
    assert homes == [first_home, first_home, third_home]
    assert not first_child.working_directory.exists()
    assert not third_home.exists()
    assert source.read_text() == "{}"


@pytest.mark.parametrize(
    ("reason", "callback"),
    (
        (CliRunReason.DEADLINE_BEFORE_SPAWN, "on_spawn_failed"),
        (CliRunReason.CLEANUP_OVERRAN, "on_reaped"),
    ),
    ids=("late-spawn-failure", "late-reap"),
)
def test_grok_turn_keeps_its_private_home_until_late_runner_confirmation(
    monkeypatch,
    tmp_path: Path,
    reason: CliRunReason,
    callback: str,
) -> None:
    captured: dict[str, object] = {}

    def run_cli_bounded(child, **kwargs):
        captured.update(kwargs)
        captured["home"] = assert_private_credential_child(
            child,
            root=tmp_path,
            source=current_config().grok_cli_auth_file,
            destination=Path(".grok/auth.json"),
        )
        outcome = CliRunOutcome(
            started=reason is CliRunReason.CLEANUP_OVERRAN,
            spawn_error=None,
            returncode=None,
            stdout="",
            stderr="",
            complete=False,
            became_zombie=False,
            reason=reason,
        )
        kwargs["stdout_line_channel"]._close(outcome)
        return outcome

    monkeypatch.setattr(grok_cli_adapter, "run_cli_bounded", run_cli_bounded)
    transport = _transport()

    async def fail_and_close() -> None:
        with pytest.raises(ProviderUnavailableError):
            await _collect_transport_responses(transport)
        await transport.aclose()

    asyncio.run(fail_and_close())
    home = captured["home"]
    assert isinstance(home, Path)
    assert home.exists()
    if callback == "on_spawn_failed":
        captured[callback]()
    else:
        captured[callback](2468, True)
    assert not home.exists()

def test_recorded_grok_tool_stream_executes_then_resumes_without_streaming_protocol(
    monkeypatch,
) -> None:
    calls = []
    rounds = iter([
        _recorded_grok_tool_round_lines(),
        grok_tool_round_lines(
            "The fictional song is 68 BPM.", "01a06e35-a690-72b1-889d-804843d68226",
        ),
    ])

    def run_cli_bounded(child, **kwargs):
        calls.append((child.command, kwargs))
        for line in next(rounds):
            assert kwargs["stdout_line_channel"]._send(line)
        outcome = grok_cli_outcome()
        kwargs["stdout_line_channel"]._close(outcome)
        return outcome

    monkeypatch.setattr(grok_cli_adapter, "run_cli_bounded", run_cli_bounded)
    transport = _transport()
    executed: list[tuple[str, dict[str, object]]] = []

    def executor(name: str, arguments: dict[str, object]) -> ToolOutcome:
        executed.append((name, arguments))
        return ToolOutcome('{"song_id":"fixture-song-527","bpm":68}', False)

    events = asyncio.run(_collect_tool_events(_tool_transport_events(transport, executor)))

    streamed_text = "".join(
        event.text for event in events if isinstance(event, AssistantTextEvent)
    )
    assert executed == [("get_song", {"song_id": "fixture-song-527"})]
    assert "<songmaker_tool_call>" not in streamed_text
    assert events[-1] == FinalEvent(text="The fictional song is 68 BPM.")
    assert calls[1][0][-2:] == ("--resume", "01a06e35-a690-72b1-889d-804843d68226")


def test_grok_tool_stream_executes_a_write_after_prose_and_exposes_its_result_next_round(
    monkeypatch,
) -> None:
    calls = []
    state = {"bpm": 68}
    tool_call = (
        "I'll change the fictional song now.\n"
        "<songmaker_tool_call>\n"
        '{"name":"update_song_style","arguments":{"song_id":"fixture-song-527","bpm":69}}\n'
        "</songmaker_tool_call>"
    )

    def run_cli_bounded(child, **kwargs):
        calls.append((child.command, kwargs))
        lines = (
            grok_tool_round_lines(tool_call)
            if len(calls) == 1
            else grok_tool_round_lines(f"The fictional song is now {state['bpm']} BPM.")
        )
        for line in lines:
            assert kwargs["stdout_line_channel"]._send(line)
        outcome = grok_cli_outcome()
        kwargs["stdout_line_channel"]._close(outcome)
        return outcome

    def executor(name: str, arguments: dict[str, object]) -> ToolOutcome:
        assert name == "update_song_style"
        state["bpm"] = arguments["bpm"]
        return ToolOutcome('{"song_id":"fixture-song-527","bpm":69}', False)

    monkeypatch.setattr(grok_cli_adapter, "run_cli_bounded", run_cli_bounded)
    events = asyncio.run(_collect_tool_events(_tool_transport_events(
        _transport(), executor,
    )))

    streamed_text = "".join(
        event.text for event in events if isinstance(event, AssistantTextEvent)
    )
    assert state == {"bpm": 69}
    assert "<songmaker_tool_call>" not in streamed_text
    assert events[-1] == FinalEvent(
        text="I'll change the fictional song now.\nThe fictional song is now 69 BPM.",
    )
    assert calls[1][0][-2:] == ("--resume", GROK_SESSION_ID)


def test_grok_tool_transport_rejects_a_multi_result_batch(monkeypatch, caplog) -> None:
    calls = []
    monkeypatch.setattr(
        grok_cli_adapter,
        "run_cli_bounded",
        grok_cli_runner(grok_tool_round_lines(_tool_call_text()), grok_cli_outcome(), calls),
    )
    transport = _transport()

    error = assert_a_multi_result_batch_is_rejected(transport)
    assert not isinstance(error, TextToolCallRejectedError)
    assert len(calls) == 1
    assert (
        "Tool protocol rejection provider=grok route=cli code=tool_result_batch_invalid"
        in caplog.text
    )


@pytest.mark.parametrize("session_id", (None, "not-a-uuid"))
def test_grok_tool_transport_rejects_missing_or_invalid_session_id(
    monkeypatch, caplog, session_id,
) -> None:
    calls = []
    end_event = {"type": "end", "stopReason": "stop"}
    if session_id is not None:
        end_event["sessionId"] = session_id
    lines = [
        json.dumps({"type": "text", "data": "done"}).encode() + b"\n",
        json.dumps(end_event).encode() + b"\n",
    ]
    monkeypatch.setattr(
        grok_cli_adapter,
        "run_cli_bounded",
        grok_cli_runner(lines, grok_cli_outcome(), calls),
    )
    transport = _transport()

    async def collect() -> None:
        turn = transport.stream(InitialTurn("system", []))
        with pytest.raises(ProviderUnavailableError) as raised:
            async for _ in turn:
                pass
        assert raised.value.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
        await transport.aclose()

    caplog.set_level("WARNING", logger="agent_providers.grok.transport")
    asyncio.run(collect())
    assert "cli_stream_response_invalid" in caplog.text
    assert "text_tool_response_invalid" not in caplog.text


def test_grok_tool_transport_normalizes_malformed_json_without_its_document(
    monkeypatch,
    caplog,
) -> None:
    document = '{"type":"text","data":"private lyrics"'
    calls = []
    monkeypatch.setattr(
        grok_cli_adapter,
        "run_cli_bounded",
        grok_cli_runner([document.encode() + b"\n"], grok_cli_outcome(), calls),
    )
    transport = _transport()

    async def collect() -> None:
        with pytest.raises(ProviderUnavailableError) as raised:
            await _collect_transport_responses(transport)
        assert raised.value.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
        assert not isinstance(raised.value, TextToolCallRejectedError)
        assert raised.value.__cause__ is None
        assert not hasattr(raised.value.__context__, "doc")
        assert document not in str(raised.value)
        await transport.aclose()

    caplog.set_level("WARNING", logger="agent_providers.grok.transport")
    asyncio.run(collect())
    assert "cli_stream_response_invalid" in caplog.text
    assert "text_tool_response_invalid" not in caplog.text


def test_grok_text_tool_parser_rejection_carries_its_rule_and_writes_no_line(
    monkeypatch,
    caplog,
) -> None:
    marker = "private-parser-marker"
    malformed_tool_call = (
        "<songmaker_tool_call>\n"
        f'{{"name":"list_songs","arguments":{{"value":"{marker}"}}\n'
        "</songmaker_tool_call>"
    )
    calls = []
    monkeypatch.setattr(
        grok_cli_adapter,
        "run_cli_bounded",
        grok_cli_runner(grok_tool_round_lines(malformed_tool_call), grok_cli_outcome(), calls),
    )
    caplog.set_level("WARNING", logger="agent_providers.grok.transport")
    transport = _transport()

    async def collect() -> None:
        with pytest.raises(TextToolCallRejectedError) as raised:
            await _collect_transport_responses(transport)
        assert raised.value.reason.code is SafeRouteReasonCode.TOOL_PROTOCOL_ERROR
        assert raised.value.rejection.rule is TextToolRejectionRule.CALL_NOT_JSON
        assert marker not in str(raised.value)
        await transport.aclose()

    asyncio.run(collect())
    assert caplog.records == []


@CALLER_CONTEXTS
def test_a_refused_grok_call_hands_its_text_only_to_the_host(
    monkeypatch, caplog, while_the_caller_handles_an_exception,
) -> None:
    monkeypatch.setattr(
        grok_cli_adapter,
        "run_cli_bounded",
        grok_cli_runner(
            grok_tool_round_lines(a_call_to_an_undeclared_tool_carrying()),
            grok_cli_outcome(),
            [],
        ),
    )
    caplog.set_level(logging.DEBUG)

    turn = drive_cowriter_turn(
        "grok",
        correlation_id="job-41",
        while_the_caller_handles_an_exception=while_the_caller_handles_an_exception,
    )

    assert_the_canary_reaches_only_the_host(
        turn,
        caplog.records,
        provider="grok",
        rule=TextToolRejectionRule.UNKNOWN_TOOL,
        correlation_id="job-41",
    )


@CALLER_CONTEXTS
def test_a_grok_call_nested_too_deeply_is_refused_not_a_failed_route(
    monkeypatch, caplog, while_the_caller_handles_an_exception,
) -> None:
    monkeypatch.setattr(
        grok_cli_adapter,
        "run_cli_bounded",
        grok_cli_runner(
            grok_tool_round_lines(a_call_nested_too_deeply_to_decode()), grok_cli_outcome(), [],
        ),
    )
    caplog.set_level(logging.DEBUG)

    turn = drive_cowriter_turn(
        "grok",
        while_the_caller_handles_an_exception=while_the_caller_handles_an_exception,
    )

    assert isinstance(turn.error, TextToolCallRejectedError)
    assert turn.error.reason.code is SafeRouteReasonCode.TOOL_PROTOCOL_ERROR
    assert turn.error.rejection.rule is TextToolRejectionRule.CALL_NOT_JSON
    assert "RecursionError" not in formatted_log_text(caplog.records)


def test_a_grok_event_line_nested_too_deeply_is_refused_as_a_cli_protocol_error(
    monkeypatch, caplog,
) -> None:
    calls = []
    nested_line = DEEPLY_NESTED_JSON.encode() + b"\n"
    monkeypatch.setattr(
        grok_cli_adapter, "run_cli_bounded",
        grok_cli_runner([nested_line], grok_cli_outcome(), calls),
    )
    caplog.set_level("WARNING", logger="agent_providers.grok.transport")
    transport = _transport()

    async def collect() -> None:
        with pytest.raises(ProviderUnavailableError) as raised:
            await _collect_transport_responses(transport)
        assert raised.value.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
        await transport.aclose()

    asyncio.run(collect())

    assert "cli_stream_response_invalid" in caplog.text


def test_a_grok_tool_result_nested_too_deeply_is_passed_on_in_the_next_prompt(
    monkeypatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        grok_cli_adapter, "run_cli_bounded",
        grok_cli_runner(grok_tool_round_lines(_tool_call_text()), grok_cli_outcome(), calls),
    )
    transport = _transport()

    async def collect() -> None:
        await _collect_transport_responses(transport)
        nested_result = ToolResultBatch((ToolResult("one", DEEPLY_NESTED_JSON, False),))
        assert [item async for item in transport.stream(nested_result)]
        await transport.aclose()

    asyncio.run(collect())

    assert DEEPLY_NESTED_JSON.encode() in calls[1][1]["prompt_file_bytes"]


def test_grok_tool_transport_rejects_unknown_events_without_logging_the_protocol(
    monkeypatch, caplog,
) -> None:
    calls = []
    event_type = "unexpected"
    monkeypatch.setattr(
        grok_cli_adapter,
        "run_cli_bounded",
        grok_cli_runner(
            [json.dumps({"type": event_type}).encode() + b"\n"], grok_cli_outcome(), calls,
        ),
    )
    caplog.set_level("WARNING")
    transport = _transport()

    async def collect() -> None:
        with pytest.raises(ProviderUnavailableError) as raised:
            await _collect_transport_responses(transport)
        assert raised.value.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
        await transport.aclose()

    asyncio.run(collect())
    assert "cli_stream_response_invalid" in caplog.text
    assert "text_tool_response_invalid" not in caplog.text
    assert event_type not in caplog.text


@pytest.mark.parametrize("event_type", ("thought", "usage", "available_commands", "plan"))
def test_grok_tool_transport_accepts_ignored_observations_without_data(
    monkeypatch, event_type,
) -> None:
    calls = []
    lines = [
        json.dumps({"type": event_type}).encode() + b"\n",
        *grok_tool_round_lines("done"),
    ]
    monkeypatch.setattr(
        grok_cli_adapter,
        "run_cli_bounded",
        grok_cli_runner(lines, grok_cli_outcome(), calls),
    )
    transport = _transport()

    async def collect() -> None:
        assert await _collect_transport_responses(transport) == [
            grok_cli_adapter.TextDelta("done"),
            grok_cli_adapter.FinalText(""),
        ]
        await transport.aclose()

    asyncio.run(collect())


def test_grok_tool_transport_rejects_a_second_end_event(monkeypatch) -> None:
    calls = []
    lines = [*grok_tool_round_lines("done"), grok_tool_round_lines("ignored")[1]]
    monkeypatch.setattr(
        grok_cli_adapter,
        "run_cli_bounded",
        grok_cli_runner(lines, grok_cli_outcome(), calls),
    )
    transport = _transport()

    async def collect() -> None:
        with pytest.raises(ProviderUnavailableError) as raised:
            await _collect_transport_responses(transport)
        assert raised.value.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
        await transport.aclose()

    asyncio.run(collect())


@pytest.mark.parametrize(
    ("lines", "outcome", "code"),
    (
        (
            [b'{"type":"error","message":"login problem"}\n'],
            grok_cli_outcome(stderr="OIDC 401"),
            SafeRouteReasonCode.CLI_AUTH_REJECTED,
        ),
        (
            [b'{"type":"error","message":"unauthenticated"}\n'],
            grok_cli_outcome(),
            SafeRouteReasonCode.CLI_AUTH_REJECTED,
        ),
        (
            [b'{"type":"text","data":"partial"}\n'],
            grok_cli_outcome(complete=False, stderr="OIDC 401"),
            SafeRouteReasonCode.CLI_AUTH_REJECTED,
        ),
        ([], grok_cli_outcome(complete=False), SafeRouteReasonCode.CLI_PROTOCOL_ERROR),
        (
            [b'{"type":"text","data":"partial"}\n'],
            grok_cli_outcome(complete=False),
            SafeRouteReasonCode.CLI_PROTOCOL_ERROR,
        ),
        (
            [b'{"type":"end","stopReason":"stop","sessionId":"' + GROK_SESSION_ID.encode()
             + b'"}\n'],
            grok_cli_outcome(returncode=1),
            SafeRouteReasonCode.CLI_PROTOCOL_ERROR,
        ),
    ),
)
def test_grok_tool_transport_names_failed_or_incomplete_runs_without_leaking_stderr(
    monkeypatch, caplog, lines, outcome, code,
) -> None:
    calls = []
    monkeypatch.setattr(
        grok_cli_adapter,
        "run_cli_bounded",
        grok_cli_runner(lines, outcome, calls),
    )
    caplog.set_level("WARNING")
    transport = _transport()

    async def collect() -> None:
        with pytest.raises(ProviderUnavailableError) as raised:
            await _collect_transport_responses(transport)
        assert raised.value.reason.code is code
        await transport.aclose()

    asyncio.run(collect())
    assert "OIDC 401" not in caplog.text


def test_grok_tool_transport_rejects_a_changed_resume_session_id(monkeypatch) -> None:
    calls = []
    changed_session_id = "4ee93ca6-9a08-4d8a-8539-e113a8d677ed"
    rounds = iter([
        grok_tool_round_lines("first"),
        grok_tool_round_lines("second", changed_session_id),
    ])

    def run_cli_bounded(child, **kwargs):
        calls.append((child.command, kwargs))
        for line in next(rounds):
            assert kwargs["stdout_line_channel"]._send(line)
        outcome = grok_cli_outcome()
        kwargs["stdout_line_channel"]._close(outcome)
        return outcome

    monkeypatch.setattr(grok_cli_adapter, "run_cli_bounded", run_cli_bounded)
    transport = _transport()

    async def collect() -> None:
        assert [item async for item in transport.stream(InitialTurn("system", []))] == [
            grok_cli_adapter.TextDelta("first"),
            grok_cli_adapter.FinalText(""),
        ]
        batch = ToolResultBatch((ToolResult("one", "1", False),))
        with pytest.raises(ProviderUnavailableError) as raised:
            async for _ in transport.stream(batch):
                pass
        assert raised.value.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
        await transport.aclose()

    asyncio.run(collect())
    assert calls[1][0][-2:] == ("--resume", GROK_SESSION_ID)


@pytest.mark.parametrize("event_type", ("tool_call", "tool_call_update"))
def test_grok_tool_transport_aborts_native_calls_before_the_loop_executes(
    monkeypatch, event_type, caplog,
) -> None:
    aborted = threading.Event()

    def run_cli_bounded(_child, **kwargs):
        channel = kwargs["stdout_line_channel"]
        assert channel._send(json.dumps({"type": event_type}).encode() + b"\n")
        while not channel.abort_requested():
            time.sleep(0.001)
        aborted.set()
        outcome = grok_cli_outcome(complete=False)
        channel._close(outcome)
        return outcome

    monkeypatch.setattr(grok_cli_adapter, "run_cli_bounded", run_cli_bounded)
    executed = False

    def executor(_name, _arguments):
        nonlocal executed
        executed = True
        return ToolOutcome("unreachable", False)

    async def collect() -> None:
        transport = _transport()
        with pytest.raises(ProviderUnavailableError) as raised:
            async for _ in _tool_transport_events(transport, executor):
                pass
        assert raised.value.reason.code is SafeRouteReasonCode.TOOL_PROTOCOL_ERROR
        assert not isinstance(raised.value, TextToolCallRejectedError)

    caplog.set_level("WARNING", logger="agent_providers.grok.transport")
    asyncio.run(collect())
    assert aborted.is_set()
    assert not executed
    assert "native_tool_blocked" in caplog.text
    assert event_type not in caplog.text


def test_closing_the_tool_loop_keeps_the_grok_home_until_confirmed_reap(
    monkeypatch,
    tmp_path: Path,
) -> None:
    started = threading.Event()
    aborted = threading.Event()
    captured: dict[str, object] = {}

    def run_cli_bounded(child, **kwargs):
        captured.update(kwargs)
        captured["home"] = assert_private_credential_child(
            child,
            root=tmp_path,
            source=current_config().grok_cli_auth_file,
            destination=Path(".grok/auth.json"),
        )
        channel = kwargs["stdout_line_channel"]
        abort_observed = threading.Event()
        request_abort = channel.request_abort

        def observe_abort() -> None:
            request_abort()
            abort_observed.set()

        channel.request_abort = observe_abort
        assert channel._send(b'{"type":"text","data":"partial"}\n')
        assert started.wait(timeout=1)
        assert abort_observed.wait(timeout=1)
        assert channel.abort_requested()
        aborted.set()
        channel._close(grok_cli_outcome(complete=False))
        return grok_cli_outcome(complete=False)

    monkeypatch.setattr(grok_cli_adapter, "run_cli_bounded", run_cli_bounded)
    transport = _transport()

    async def close_turn() -> None:
        turn = _tool_transport_events(
            transport, lambda _name, _arguments: ToolOutcome("unused", False),
        )
        assert await anext(turn) == AssistantTextEvent(text="partial")
        started.set()
        await turn.aclose()

    asyncio.run(close_turn())
    assert aborted.is_set()
    home = captured["home"]
    assert isinstance(home, Path)
    assert home.exists()
    captured["on_reaped"](2468, True)
    assert not home.exists()


def test_grok_tool_transport_removes_its_private_session_tree_and_redacts_logs(
    monkeypatch, tmp_path, caplog,
) -> None:
    session_root = tmp_path / ".grok" / "sessions"
    external_session = session_root / "external" / GROK_SESSION_ID
    external_session.mkdir(parents=True)
    (external_session / "history.jsonl").write_text("leave me")
    calls = []
    lyrics = "private lyrics"
    song_id = "song-private"
    call_json = f'{{"lyrics":"{lyrics}","song_id":"{song_id}"}}'
    stderr_document = "private stderr document"
    tool_call = (
        f"<songmaker_tool_call>\n"
        f'{{"name":"update_song_lyrics","arguments":{call_json}}}\n'
        "</songmaker_tool_call>"
    )

    def run_cli_bounded(child, **kwargs):
        calls.append((child, kwargs))
        session_tree = child.working_directory / ".grok" / "sessions" / GROK_SESSION_ID
        session_tree.mkdir(parents=True)
        (session_tree / "prompt_history.jsonl").write_text(f"{lyrics} {song_id}")
        for line in grok_tool_round_lines(tool_call):
            assert kwargs["stdout_line_channel"]._send(line)
        outcome = grok_cli_outcome(stderr=stderr_document)
        kwargs["stdout_line_channel"]._close(outcome)
        kwargs["on_reaped"](1, False)
        return outcome

    monkeypatch.setattr(grok_cli_adapter, "run_cli_bounded", run_cli_bounded)
    caplog.set_level("INFO", logger="agent_providers.grok.transport")
    transport = _transport()

    async def collect_and_close() -> None:
        assert isinstance(
            [item async for item in transport.stream(InitialTurn("system", []))][0],
            ToolCallBatch,
        )
        await transport.aclose()

    asyncio.run(collect_and_close())
    working_directory = str(calls[0][0].working_directory)
    assert not Path(working_directory).exists()
    assert (external_session / "history.jsonl").read_text() == "leave me"
    assert "Grok CLI tool round provider=grok route=cli round=1 session=" in caplog.text
    for forbidden in (lyrics, song_id, call_json, stderr_document, "prompt_history"):
        assert forbidden not in caplog.text


async def _collect_tool_events(stream) -> list[object]:
    return [event async for event in stream]
