"""Codex subscription CLI tool turn transport."""

from __future__ import annotations

import asyncio
import errno
import gc
import json
import logging
import os
import shlex
import signal
import stat
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image
from provider_test_support import (
    A_CALLER_CANARY,
    A_RUNNER_FAILURE_CANARY,
    A_TOOL_FAILURE_MESSAGE,
    CALLER_CONTEXTS,
    CANARY_ACCESS_TOKEN,
    CREDENTIAL_FILE_BOUND_BYTES,
    DEEPLY_NESTED_JSON,
    SAMPLE_TOOL_CATALOG,
    CallerCanaryError,
    FailingLogHandler,
    RaisingLogHandler,
    RunnerFailureWatch,
    a_call_nested_too_deeply_to_decode,
    a_call_to_an_undeclared_tool_carrying,
    a_codex_login,
    a_credential_destination_that_fails,
    a_dangling_symlink,
    a_directory,
    a_fifo,
    a_path_through_a_regular_file,
    a_symlink_to_a_login,
    a_usable_login_over_the_bound,
    assert_a_multi_result_batch_is_rejected,
    assert_the_canary_reaches_only_the_host,
    call_within_deadline,
    drive_turn,
    exception_chain,
    fail_mkdtemp,
    fail_the_directory_named,
    fail_the_home_chmod,
    fail_the_work_directory,
    fail_to_start_the_runner_thread,
    formatted_log_text,
    live_frames_holding,
    override_provider_runtime,
    override_turn_runtime,
    shell_child,
    traceback_frames_hold,
    use_codex_process_pool,
)

from agent_providers import process
from agent_providers.catalog import ProviderRoute, list_provider_models
from agent_providers.codex import image as codex_image
from agent_providers.codex import protocol as codex_protocol
from agent_providers.codex import transport as codex_transport
from agent_providers.codex.pool import CodexProcessKind, CodexProcessPool
from agent_providers.codex_login import blanked_copy
from agent_providers.errors import (
    CodexProcessPoolSaturatedError,
    ProviderModelCatalogUnavailableError,
    ProviderUnavailableError,
    SafeRouteReasonCode,
    TextToolCallRejectedError,
    normalize_route_failure,
)
from agent_providers.events import AssistantTextEvent, FinalEvent, ToolCallEvent
from agent_providers.images import ImagePolicy
from agent_providers.process import (
    AgentCliUnavailableError,
    CliRunOutcome,
    CliRunReason,
    clear_agent_cli_caches,
)
from agent_providers.sandbox.paths import (
    CODEX_HOME_DIRECTORY_NAME,
    CODEX_IMAGE_TURN_DIRECTORY_PREFIX,
    CODEX_TOOL_TURN_DIRECTORY_PREFIX,
)
from agent_providers.text_tool_protocol import TextToolRejectionRule
from agent_providers.tool_loop import (
    InitialTurn,
    ToolCallBatch,
    ToolOutcome,
    ToolResult,
    ToolResultBatch,
    stream_tool_loop,
)

AN_IMAGE_POLICY = ImagePolicy(
    maximum_source_bytes=8 * 1024 * 1024,
    maximum_pixels=20_000_000,
    output_edge_pixels=1024,
    output_format="PNG",
    output_signature=b"\x89PNG\r\n\x1a\n",
)


def _transport() -> codex_transport.CodexCliToolTransport:
    """Build the transport under test with a host's own tool catalog."""
    return codex_transport.CodexCliToolTransport(
        model="codex-test", catalog=SAMPLE_TOOL_CATALOG,
    )

_A_CODEX_LOGIN = json.loads(a_codex_login())
_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("missing", ("cli", "code_mode_host", "resources"))
def test_image_capability_requires_every_codex_mount(
    tmp_path: Path, missing: str,
) -> None:
    cli = tmp_path / "codex"
    code_mode_host = tmp_path / "codex-code-mode-host"
    resources = tmp_path / "codex-resources"
    for binary in (cli, code_mode_host):
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    resources.mkdir()
    override_provider_runtime(codex_cli_binary=str(cli))
    override_turn_runtime(
        codex_code_mode_host_binary=code_mode_host,
        codex_resources_directory=resources,
    )

    assert codex_image.codex_image_turn_is_available()

    if missing == "cli":
        cli.unlink()
    elif missing == "code_mode_host":
        code_mode_host.unlink()
    else:
        resources.rmdir()

    assert not codex_image.codex_image_turn_is_available()


def test_image_capability_requires_the_installed_image_encoder(
    tmp_path: Path, monkeypatch,
) -> None:
    cli = tmp_path / "codex"
    code_mode_host = tmp_path / "codex-code-mode-host"
    resources = tmp_path / "codex-resources"
    for binary in (cli, code_mode_host):
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    resources.mkdir()
    override_provider_runtime(codex_cli_binary=str(cli))
    override_turn_runtime(
        codex_code_mode_host_binary=code_mode_host,
        codex_resources_directory=resources,
    )
    monkeypatch.setattr(codex_image, "image_encoder_is_installed", lambda: False)

    assert not codex_image.codex_image_turn_is_available()


def test_a_deployment_without_the_image_encoder_refuses_before_it_spawns(
    monkeypatch,
) -> None:
    spawns: list[tuple[str, ...]] = []

    def run_cli_bounded(child, **_kwargs):
        spawns.append(child.command)
        raise AssertionError("an unencodable turn must not reach the CLI")

    monkeypatch.setattr(codex_protocol, "run_cli_bounded", run_cli_bounded)
    monkeypatch.setattr(codex_image, "image_encoder_is_installed", lambda: False)

    with pytest.raises(codex_image.CodexImageEncoderUnavailableError):
        codex_image.generate_codex_image(
            "prompt", policy=AN_IMAGE_POLICY, deadline=10_000_000,
        )

    assert spawns == []


@pytest.fixture(autouse=True)
def codex_login_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "auth.json"
    source.write_text(json.dumps(_A_CODEX_LOGIN))
    override_provider_runtime(codex_cli_auth_file=source)
    process_pool = CodexProcessPool(maximum_processes=8, maximum_image_runs=1)
    use_codex_process_pool(monkeypatch, process_pool)
    return source


def _outcome(
    *, returncode: int = 0, complete: bool = True, stdout: str = "", stderr: str = "",
    reason: CliRunReason = CliRunReason.COMPLETE,
) -> CliRunOutcome:
    return CliRunOutcome(
        started=True,
        spawn_error=None,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        complete=complete,
        became_zombie=False,
        reason=reason,
    )


def _runner(lines: list[bytes], outcome: CliRunOutcome, calls: list) -> object:
    def run_cli_bounded(child, **kwargs):
        calls.append((child.command, kwargs))
        for line in lines:
            if not kwargs["stdout_line_channel"]._send(line):
                break
        kwargs["stdout_line_channel"]._close(outcome)
        kwargs["on_spawned"](1)
        kwargs["on_reaped"](1, False)
        return outcome

    return run_cli_bounded


def _tool_round_lines(text: str) -> list[bytes]:
    return [
        b'{"type":"thread.started","thread_id":"52700000-0000-4000-8000-000000000000"}\n',
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": text,
        }}).encode() + b"\n",
        b'{"type":"turn.completed","usage":{}}\n',
    ]


def _fixture_lines(name: str) -> list[bytes]:
    return [line.encode() + b"\n" for line in (_FIXTURES / name).read_text().splitlines()]


_PNG_FIXTURE_SIZE = (300, 100)
_PNG_FIXTURE_PIXELS = _PNG_FIXTURE_SIZE[0] * _PNG_FIXTURE_SIZE[1]


def test_image_tool_block_reasons_are_closed() -> None:
    assert {reason.value for reason in codex_image.ImageToolBlockedReason} == {
        "unexpected_event",
        "blocked_item",
        "unexpected_item",
    }


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", _PNG_FIXTURE_SIZE, (20, 80, 160)).save(output, format="PNG")
    return output.getvalue()


_MEASURED_STREAM = (_FIXTURES / "codex-imagegen-0.163.0-events.jsonl").read_text()


def _quota_failure(message: str) -> CliRunOutcome:
    event = {"type": "turn.failed", "error": {"message": message}}
    return _outcome(returncode=1, stdout=json.dumps(event) + "\n")


def _image_runner(
    outcome: CliRunOutcome,
    create_artifacts: Callable[[Path], None] | None = None,
):
    def run_cli_bounded(child, **kwargs):
        codex_home = Path(child.environment["CODEX_HOME"])
        if create_artifacts is not None:
            create_artifacts(codex_home)
        kwargs["on_spawned"](1)
        kwargs["on_reaped"](1, False)
        kwargs["stdout_line_channel"]._close(outcome)
        return outcome

    return run_cli_bounded


def _codex_tool_events(transport, executor):
    return stream_tool_loop(
        provider="codex",
        route="cli",
        system="system",
        messages=[{"role": "user", "content": "hello"}],
        transport=transport,
        executor=executor,
        tool_failure_message=A_TOOL_FAILURE_MESSAGE,
    )


async def _collect_tool_events(stream) -> list[object]:
    return [event async for event in stream]


def test_codex_tool_command_pins_read_only_isolation_for_start_and_resume() -> None:
    model = "codex-test"
    thread_id = "52700000-0000-4000-8000-000000000000"
    common = (
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "-c", "approval_policy=\"never\"",
        "-c", "mcp_servers={}",
        "-c", "features.shell_tool=false",
        "-c", "features.unified_exec=false",
        "-c", "features.browser_use=false",
        "-c", "features.computer_use=false",
        "-c", "features.multi_agent=false",
        "-c", "features.image_generation=false",
        "-c", "features.plugins=false",
        "-c", "features.hooks=false",
        "-c", 'web_search="disabled"',
        "-c", "features.code_mode_host=false",
        "-c", "features.code_mode=false",
        "-c", "features.code_mode_only=false",
        "-c", 'sandbox_mode="read-only"',
        "--model", model,
    )

    assert codex_transport._codex_tool_arguments(model) == (
        "exec", "--sandbox", "read-only", *common, "-",
    )
    assert codex_transport._codex_tool_arguments(
        model,
        thread_id=thread_id,
    ) == ("exec", "resume", *common, thread_id, "-")


def test_codex_tool_transport_uses_an_empty_private_work_directory_on_resume(monkeypatch) -> None:
    calls: list = []
    prompts: list[bytes] = []
    thread_id = "52700000-0000-4000-8000-000000000000"
    rounds = iter([
        [
            json.dumps({"type": "thread.started", "thread_id": thread_id}).encode() + b"\n",
            b'{"type":"item.completed","item":{"type":"agent_message","text":"<notes_tool_call>\\n{\\"name\\":\\"list_notes\\",\\"arguments\\":{}}\\n</notes_tool_call>"}}\n',
            b'{"type":"turn.completed","usage":{}}\n',
        ],
        [
            json.dumps({"type": "thread.started", "thread_id": thread_id}).encode() + b"\n",
            b'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n',
            b'{"type":"turn.completed","usage":{}}\n',
        ],
    ])

    def run_cli_bounded(child, **kwargs):
        calls.append((child, kwargs))
        work_directory = child.working_directory
        codex_home = Path(child.environment["CODEX_HOME"])
        assert work_directory.name == "work"
        assert work_directory.parent == codex_home.parent
        assert work_directory != codex_home
        assert work_directory.stat().st_mode & 0o777 == 0o700
        assert list(work_directory.iterdir()) == []
        assert (codex_home / "auth.json").is_file()
        prompts.append(kwargs["stdin_payload"])
        for line in next(rounds):
            assert kwargs["stdout_line_channel"]._send(line)
        outcome = _outcome()
        kwargs["stdout_line_channel"]._close(outcome)
        kwargs["on_spawned"](len(calls))
        kwargs["on_reaped"](len(calls), False)
        return outcome

    monkeypatch.setattr(codex_protocol, "run_cli_bounded", run_cli_bounded)
    transport = _transport()
    events = asyncio.run(_collect_tool_events(_codex_tool_events(
        transport,
        lambda _name, _arguments: ToolOutcome('{"notes":[]}', False),
    )))

    assert isinstance(events[0], ToolCallEvent)
    assert events[-1] == FinalEvent(text="done")
    (first_child, first_kwargs), (second_child, second_kwargs) = calls
    first_command, second_command = first_child.command, second_child.command
    assert first_command[1:5] == ("exec", "--sandbox", "read-only", "--json")
    assert second_command[1:3] == ("exec", "resume")
    assert second_command[-2:] == (thread_id, "-")
    for child, kwargs in calls:
        assert "--ephemeral" not in child.command
        assert kwargs["stdin_payload"] in prompts
        assert kwargs["output_read_limit_bytes"] == (
            codex_protocol.CODEX_CLI_TURN_OUTPUT_READ_LIMIT_BYTES
        )
        assert kwargs["deadline"] == first_kwargs["deadline"]
        assert child.environment["CODEX_HOME"].endswith("/codex-home")
        assert child.environment["HOME"] == str(child.working_directory.parent)
        for config in (*codex_transport._CODEX_TOOL_ISOLATION_CONFIGS,
                       'sandbox_mode="read-only"'):
            assert config in child.command
    assert prompts == [
        b"system\n\nUser: hello",
        b'<notes_tool_result>\n{"notes":[]}\n</notes_tool_result>',
    ]
    assert not first_child.working_directory.exists()
    assert first_child.environment == second_child.environment


@pytest.mark.parametrize(
    "thread_id",
    ("fixture-codex-thread-527", "--dangerously-bypass-approvals-and-sandbox"),
)
def test_codex_tool_transport_rejects_non_uuid_thread_ids(thread_id: str) -> None:
    with pytest.raises(codex_protocol.CodexCliStreamFailure):
        codex_transport._thread_started_id({"thread_id": thread_id})


def test_codex_tool_transport_rejects_a_multi_result_batch_without_a_resume(
    monkeypatch, caplog,
) -> None:
    calls: list = []
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _runner([
        b'{"type":"thread.started","thread_id":"52700000-0000-4000-8000-000000000000"}\n',
        b'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n',
        b'{"type":"turn.completed","usage":{}}\n',
    ], _outcome(), calls))
    transport = _transport()

    error = assert_a_multi_result_batch_is_rejected(transport)
    assert not isinstance(error, TextToolCallRejectedError)
    assert len(calls) == 1
    assert caplog.records == []


@pytest.mark.parametrize("fixture_name", (
    "codex-tool-code-mode-host-disabled.jsonl",
    "codex-tool-code-mode-host-disabled-without-remediation.jsonl",
))
def test_codex_tool_transport_ignores_its_code_mode_host_isolation_notice(
    monkeypatch, caplog,
    fixture_name: str,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        codex_protocol,
        "run_cli_bounded",
        _runner(
            _fixture_lines(fixture_name),
            _outcome(),
            calls,
        ),
    )
    caplog.set_level("INFO", logger="agent_providers.codex.transport")
    transport = _transport()

    events = asyncio.run(_collect_tool_events(_codex_tool_events(
        transport,
        lambda _name, _arguments: ToolOutcome("unreachable", False),
    )))

    assert events == [
        AssistantTextEvent(text="The open song is Midnight Drive."),
        FinalEvent(text="The open song is Midnight Drive."),
    ]
    assert len(calls) == 1
    assert "ignored its code-mode-host isolation notice" in caplog.text


def test_codex_tool_transport_aborts_for_an_unrelated_completed_error_item(monkeypatch) -> None:
    aborted = threading.Event()

    def run_cli_bounded(child, **kwargs):
        channel = kwargs["stdout_line_channel"]
        for line in _fixture_lines("codex-tool-unrelated-error.jsonl"):
            assert channel._send(line)
        while not channel.abort_requested():
            time.sleep(0.001)
        aborted.set()
        outcome = _outcome(complete=False)
        channel._close(outcome)
        kwargs["on_spawned"](1)
        kwargs["on_reaped"](1, False)
        return outcome

    monkeypatch.setattr(codex_protocol, "run_cli_bounded", run_cli_bounded)
    transport = _transport()

    async def collect() -> None:
        turn = transport.stream(InitialTurn("system", []))
        with pytest.raises(ProviderUnavailableError) as raised:
            async for _ in turn:
                pass
        assert raised.value.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR

    asyncio.run(collect())
    assert aborted.is_set()


@pytest.mark.parametrize("item_type", sorted(codex_protocol.BLOCKED_ITEM_TYPES))
def test_codex_tool_transport_aborts_native_tools_before_the_loop_executes(
    monkeypatch,
    item_type,
) -> None:
    aborted = threading.Event()

    def run_cli_bounded(child, **kwargs):
        channel = kwargs["stdout_line_channel"]
        assert channel._send(json.dumps({
            "type": "item.started", "item": {"type": item_type},
        }).encode() + b"\n")
        while not channel.abort_requested():
            time.sleep(0.001)
        aborted.set()
        outcome = _outcome(complete=False)
        channel._close(outcome)
        kwargs["on_spawned"](1)
        kwargs["on_reaped"](1, False)
        return outcome

    monkeypatch.setattr(codex_protocol, "run_cli_bounded", run_cli_bounded)
    executed = False

    def executor(_name, _arguments):
        nonlocal executed
        executed = True
        return ToolOutcome("unreachable", False)

    async def collect() -> None:
        transport = _transport()
        with pytest.raises(ProviderUnavailableError) as raised:
            async for _ in _codex_tool_events(transport, executor):
                pass
        assert raised.value.reason.code is SafeRouteReasonCode.TOOL_EXECUTION_FAILED

    asyncio.run(collect())
    assert aborted.is_set()
    assert not executed


def test_codex_tool_transport_cleans_its_home_and_does_not_log_protocol_text(
    monkeypatch,
    caplog,
) -> None:
    calls: list = []
    text = "private text"
    note_id = "note-private"
    protocol = (
        "<notes_tool_call>\n"
        f'{{"name":"update_note_text","arguments":{{"note_id":"{note_id}","text":"{text}"}}}}\n'
        "</notes_tool_call>"
    )

    def run_cli_bounded(child, **kwargs):
        calls.append((child, kwargs))
        home = Path(child.environment["CODEX_HOME"])
        (home / "sessions").mkdir()
        (home / "sessions" / "private.jsonl").write_text(protocol)
        for line in (
            b'{"type":"thread.started","thread_id":"52700000-0000-4000-8000-000000000000"}\n',
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": protocol,
            }}).encode() + b"\n",
            b'{"type":"turn.completed","usage":{}}\n',
        ):
            assert kwargs["stdout_line_channel"]._send(line)
        outcome = _outcome(stderr="private stderr")
        kwargs["stdout_line_channel"]._close(outcome)
        kwargs["on_spawned"](1)
        kwargs["on_reaped"](1, False)
        return outcome

    monkeypatch.setattr(codex_protocol, "run_cli_bounded", run_cli_bounded)
    caplog.set_level("INFO", logger="agent_providers.codex.transport")
    transport = _transport()

    async def collect_and_close() -> None:
        assert isinstance(
            [item async for item in transport.stream(InitialTurn("system", []))][0],
            ToolCallBatch,
        )
        await transport.aclose()

    asyncio.run(collect_and_close())
    assert not calls[0][0].working_directory.exists()
    assert "Codex CLI tool round provider=codex route=cli round=1 duration_ms=" in caplog.text
    for forbidden in (text, note_id, protocol, "private stderr", "private.jsonl"):
        assert forbidden not in caplog.text


def test_a_codex_tool_turn_whose_spawn_raises_a_base_exception_ends_with_a_named_failure(
    monkeypatch, caplog, capfd,
) -> None:
    watch = RunnerFailureWatch(monkeypatch, caplog, capfd)
    process_pool = CodexProcessPool(maximum_processes=2, maximum_image_runs=1)
    use_codex_process_pool(monkeypatch, process_pool)

    def spawn_that_raises(*_arguments, **_keywords):
        raise BaseException(A_RUNNER_FAILURE_CANARY)

    monkeypatch.setattr(process, "open_pipes", spawn_that_raises)
    events = _codex_tool_events(_transport(), lambda _name, _arguments: ToolOutcome("{}", False))
    turn = asyncio.wait_for(_collect_tool_events(events), timeout=5)

    with pytest.raises(ProviderUnavailableError) as failure:
        asyncio.run(turn)

    assert process_pool.reservation_count() == 0
    assert watch.reported_steps() == []
    watch.assert_nothing_leaked(failure.value)


def test_deadline_before_spawn_keeps_the_codex_slot_until_late_reap(monkeypatch) -> None:
    process_pool = CodexProcessPool(maximum_processes=1, maximum_image_runs=1)
    use_codex_process_pool(monkeypatch, process_pool)
    callbacks: dict[str, object] = {}

    def fake_runner(child, **kwargs):
        callbacks.update(kwargs)
        return CliRunOutcome(
            started=False,
            spawn_error=None,
            returncode=None,
            stdout="",
            stderr="",
            complete=False,
            became_zombie=False,
            reason=CliRunReason.DEADLINE_BEFORE_SPAWN,
        )

    monkeypatch.setattr(codex_protocol, "run_cli_bounded", fake_runner)
    reservation = process_pool.reserve(CodexProcessKind.TEXT)
    turn_home = codex_protocol.create_codex_turn_home(CODEX_TOOL_TURN_DIRECTORY_PREFIX)

    codex_protocol.run_reserved_codex_cli(
        reservation,
        turn_home.home.reserve(),
        shell_child("-c", ":"),
        stdin_payload=b"prompt",
        deadline=10_000_000,
        channel=process.CliLineChannel(1),
    )
    turn_home.home.close()

    with pytest.raises(CodexProcessPoolSaturatedError):
        process_pool.reserve(CodexProcessKind.TEXT)
    assert turn_home.home.path.exists()
    callbacks["on_spawned"](41)
    callbacks["on_reaped"](41, True)
    assert process_pool.reservation_count() == 0
    assert not turn_home.home.path.exists()


def _generated_png_runner() -> object:
    """A Codex run that leaves exactly one generated PNG in its private home."""
    def run_cli_bounded(child, **kwargs):
        codex_home = Path(child.environment["CODEX_HOME"])
        artifact = codex_home / "generated_images" / "thread" / "image.png"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(_png_bytes())
        outcome = _outcome(stdout=_MEASURED_STREAM)
        kwargs["on_spawned"](1)
        kwargs["on_reaped"](1, False)
        kwargs["stdout_line_channel"]._close(outcome)
        return outcome

    return run_cli_bounded


def _host_image_policy(
    *,
    maximum_source_bytes: int = 8 * 1024 * 1024,
    maximum_pixels: int = 20_000_000,
    output_edge_pixels: int = 256,
) -> ImagePolicy:
    """A host's own image bounds."""
    return ImagePolicy(
        maximum_source_bytes=maximum_source_bytes,
        maximum_pixels=maximum_pixels,
        output_edge_pixels=output_edge_pixels,
        output_format="PNG",
        output_signature=b"\x89PNG\r\n\x1a\n",
    )


def test_codex_image_accepts_the_measured_0_163_stream(monkeypatch) -> None:
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _generated_png_runner())

    assert codex_image.generate_codex_image(
        "prompt", policy=AN_IMAGE_POLICY, deadline=10_000_000,
    ).startswith(b"\x89PNG")


def test_a_generated_image_is_returned_in_the_shape_the_host_asked_for(monkeypatch) -> None:
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _generated_png_runner())

    payload = codex_image.generate_codex_image(
        "prompt",
        policy=_host_image_policy(output_edge_pixels=64),
        deadline=10_000_000,
    )

    with Image.open(BytesIO(payload)) as normalized:
        assert (normalized.format, normalized.size) == ("PNG", (64, 64))


@pytest.mark.parametrize(
    "policy",
    (
        _host_image_policy(maximum_pixels=_PNG_FIXTURE_PIXELS - 1),
        _host_image_policy(maximum_source_bytes=1),
    ),
    ids=("more-pixels-than-the-host-allows", "more-bytes-than-the-host-allows"),
)
def test_a_generated_image_outside_the_hosts_bounds_is_refused(
    monkeypatch, policy: ImagePolicy,
) -> None:
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _generated_png_runner())

    with pytest.raises(codex_image.CodexImageArtifactError):
        codex_image.generate_codex_image(
            "prompt", policy=policy, deadline=10_000_000,
        )


@pytest.mark.parametrize(
    ("outcome", "expected_error", "expected_message", "expected_retry_at"),
    (
        (_outcome(stderr="401 Unauthorized"), codex_image.CodexImageLoginError, None, None),
        (
            _outcome(
                complete=False,
                reason=CliRunReason.DEADLINE_WHILE_READING,
            ),
            codex_image.CodexImageTimeoutError,
            None,
            None,
        ),
        (
            _outcome(returncode=1, complete=False),
            codex_image.CodexImageCliError,
            None,
            None,
        ),
        (
            _outcome(
                returncode=1,
                stdout=(
                    '{"type":"turn.failed",'
                    '"error":{"message":"401 Unauthorized: token expired"}}\n'
                ),
            ),
            codex_image.CodexImageLoginError,
            None,
            None,
        ),
        (
            _outcome(
                returncode=1,
                stdout=(_FIXTURES / "codex-image-quota-exceeded.jsonl").read_text(),
            ),
            codex_image.CodexImageQuotaError,
            "usage limit",
            "Sep 7th, 2026 8:45 PM",
        ),
        (
            _quota_failure("usage limit, try again at Sep 7th, 2026 8:45 PM.\nSee billing."),
            codex_image.CodexImageQuotaError,
            "usage limit",
            "Sep 7th, 2026 8:45 PM",
        ),
        (
            _quota_failure("usage limit, try again at Sep 7th.."),
            codex_image.CodexImageQuotaError,
            "usage limit",
            "Sep 7th.",
        ),
        (
            _quota_failure("usage limit, try again at ."),
            codex_image.CodexImageQuotaError,
            "usage limit",
            None,
        ),
        (
            _outcome(
                returncode=1,
                stdout=(
                    '{"type":"turn.started"}\n'
                    '{"type":"turn.failed",'
                    '"error":{"message":"The requested model is unavailable."}}\n'
                ),
            ),
            codex_image.CodexImageCliError,
            "The requested model is unavailable.",
            None,
        ),
        (
            _outcome(stdout='{"type":"thread.started","thread_id":"thread"}\n'),
            codex_image.CodexImageCliError,
            None,
            None,
        ),
    ),
    ids=(
        "login",
        "timeout",
        "nonzero-exit",
        "turn-failed-names-auth",
        "turn-failed-names-usage-limit",
        "usage-limit-hint-followed-by-a-later-line",
        "usage-limit-hint-keeps-all-but-one-trailing-dot",
        "usage-limit-hint-without-a-time",
        "turn-failed-names-generic-message",
        "turn-never-completed",
    ),
)
def test_codex_image_names_terminal_cli_failures_without_logging_the_gate(
    monkeypatch,
    caplog,
    outcome: CliRunOutcome,
    expected_error: type[Exception],
    expected_message: str | None,
    expected_retry_at: str | None,
) -> None:
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _image_runner(outcome))
    caplog.set_level("WARNING", logger="agent_providers.codex.image")

    with pytest.raises(expected_error) as raised:
        _generate_image()

    assert _warnings(caplog) == []
    if expected_message is not None:
        assert expected_message in str(raised.value)
    if expected_error is codex_image.CodexImageQuotaError:
        assert raised.value.retry_at == expected_retry_at


_A_PRIVATE_EVENT_MARKER = "private-event-marker"
_A_PRIVATE_PROMPT_MARKER = "private-prompt-marker"
_THREAD_OPENING = [{"type": "thread.started", "thread_id": "thread"}, {"type": "turn.started"}]
_OPENING_TOKENS = ("thread.started", "turn.started")
_TURN_COMPLETED = {"type": "turn.completed", "usage": {}}
_MEASURED_TOKENS = (*_OPENING_TOKENS, "item.completed:agent_message*3", "turn.completed")


def _item_event(event_type: str, kind: str) -> dict:
    item = {"id": "item", "type": kind, "private": _A_PRIVATE_EVENT_MARKER}
    return {"type": event_type, "item": item}


def _jsonl(events: list[dict]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _write_generated_file(name: str, content: bytes):
    def create_artifacts(codex_home: Path) -> None:
        artifact = codex_home / "generated_images" / name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(content)

    return create_artifacts


def _write_png_beside_a_text_file(codex_home: Path) -> None:
    _write_generated_file("image.png", _png_bytes())(codex_home)
    (codex_home / "generated_images" / "notes.txt").write_text("not an image")


def _write_generated_directory(codex_home: Path) -> None:
    (codex_home / "generated_images" / "thread").mkdir(parents=True)


def _write_two_generated_pngs(codex_home: Path) -> None:
    _write_generated_file("a.png", _png_bytes())(codex_home)
    _write_generated_file("b.png", _png_bytes())(codex_home)


def _symlink_generated_images_root(codex_home: Path) -> None:
    real_root = codex_home / "elsewhere"
    real_root.mkdir()
    (real_root / "image.png").write_bytes(_png_bytes())
    (codex_home / "generated_images").symlink_to(real_root, target_is_directory=True)


def _stdout_line(event: dict | bytes) -> bytes:
    return event if isinstance(event, bytes) else json.dumps(event).encode() + b"\n"


_StreamedEvents = list[dict | bytes] | Callable[[Path], list[dict]]


def _streaming_runner(events: _StreamedEvents, state: dict[str, bool]):
    """A Codex run that streams its events (or raw lines) and records its abort and reap.

    Events given as a callable are built from the run's resolved CODEX_HOME.
    """
    def run_cli_bounded(child, **kwargs):
        streamed = (
            events(Path(child.environment["CODEX_HOME"])) if callable(events) else events
        )
        channel = kwargs["stdout_line_channel"]
        abort_observed = threading.Event()
        request_abort = channel.request_abort

        def observe_abort() -> None:
            request_abort()
            abort_observed.set()

        channel.request_abort = observe_abort
        kwargs["on_spawned"](2468)
        for event in streamed:
            if not channel._send(_stdout_line(event)):
                break
        state["aborted"] = abort_observed.wait(timeout=1)
        kwargs["on_reaped"](2468, False)
        state["reaped"] = True
        outcome = _outcome(stdout=b"".join(map(_stdout_line, streamed)).decode())
        channel._close(outcome)
        return outcome

    return run_cli_bounded


_DEFAULT_IMAGE_SECONDS = 30


def _generate_image(
    prompt: str = "prompt",
    deadline: float | None = None,
    abort_signal: threading.Event | None = None,
) -> bytes:
    return codex_image.generate_codex_image(
        prompt,
        policy=AN_IMAGE_POLICY,
        deadline=time.monotonic() + _DEFAULT_IMAGE_SECONDS if deadline is None else deadline,
        abort_signal=abort_signal,
    )


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "agent_providers.codex.image" and record.levelname == "WARNING"
    ]


def _types_line(tokens: tuple[str, ...]) -> str:
    return "types=[" + ", ".join(tokens) + "]"


class _HomeReleases:
    """Counts the home tokens and turn reservations released, on whichever thread releases them."""

    def __init__(self, monkeypatch) -> None:
        self._released = threading.Semaphore(0)
        self.token_releases = 0
        release_token = process.PrivateCredentialHomeReservation.release
        release_turn = codex_protocol.release_turn_reservations

        def count_a_token_release(token) -> None:
            self.token_releases += 1
            release_token(token)

        def release_and_signal(home_reservation, release_slot) -> None:
            try:
                release_turn(home_reservation, release_slot)
            finally:
                self._released.release()

        monkeypatch.setattr(
            process.PrivateCredentialHomeReservation, "release", count_a_token_release,
        )
        monkeypatch.setattr(codex_protocol, "release_turn_reservations", release_and_signal)

    def wait_for_one(self) -> None:
        assert self._released.acquire(timeout=_HANG_BOUND_SECONDS), "no reservation was released"


_TURN_SECONDS = 0.3
_HANG_BOUND_SECONDS = 20
_ALLOWANCE_SECONDS = 0.5
_TINY_GRACE_SECONDS = 0.01


@dataclass(frozen=True)
class _ImageTurn:
    value: bytes | None
    error: BaseException | None
    elapsed: float


@dataclass(frozen=True)
class _ImageTurnFrame:
    process_pool: CodexProcessPool
    working_root: Path
    watch: RunnerFailureWatch
    caplog: pytest.LogCaptureFixture

    def drive(
        self,
        seconds: float,
        *,
        prompt: str = "prompt",
        abort_signal: threading.Event | None = None,
    ) -> _ImageTurn:
        started_at = time.monotonic()
        deadline = started_at + seconds
        results: list[_ImageTurn] = []

        def run() -> None:
            value, error = None, None
            try:
                value = _generate_image(prompt, deadline, abort_signal)
            except BaseException as raised:
                error = raised
            results.append(_ImageTurn(value, error, time.monotonic() - started_at))

        turn = threading.Thread(target=run, daemon=True)
        turn.start()
        turn.join(timeout=_HANG_BOUND_SECONDS)
        assert not turn.is_alive(), "the image turn hung"
        return results[0]

    def assert_ended(
        self,
        turn: _ImageTurn,
        *,
        runner_steps: tuple[str, ...] = (),
        reservations: int = 0,
        holdings_checked: bool = True,
    ) -> None:
        assert self.watch.reported_steps() == list(runner_steps)
        if holdings_checked:
            assert self.process_pool.image_reservation_count() == reservations
            assert list(self.working_root.iterdir()) == []
        self.watch.assert_nothing_leaked(*(error for error in (turn.error,) if error))


@pytest.fixture
def image_turn(monkeypatch, caplog, capfd, tmp_path) -> _ImageTurnFrame:
    process_pool = CodexProcessPool(maximum_processes=8, maximum_image_runs=1)
    use_codex_process_pool(monkeypatch, process_pool)
    working_root = tmp_path / "turn-homes"
    working_root.mkdir()
    override_provider_runtime(cli_working_directory_root=working_root)
    return _ImageTurnFrame(
        process_pool, working_root, RunnerFailureWatch(monkeypatch, caplog, capfd), caplog,
    )


def _the_child_plays(monkeypatch, script: str) -> None:
    played = shell_child("-c", script)
    for turn_module in (codex_image, codex_transport):
        monkeypatch.setattr(
            turn_module, "codex_child_process", lambda *_arguments, **_keywords: played,
        )


def _the_child_prints_then_sleeps(monkeypatch, events: list[dict]) -> None:
    _the_child_plays(monkeypatch, f"printf %s {shlex.quote(_jsonl(events))}; exec sleep 10")


def _settle_within_a_few_hundredths(monkeypatch) -> None:
    monkeypatch.setattr(process, "CLI_TERMINATION_GRACE_SECONDS", _TINY_GRACE_SECONDS)
    monkeypatch.setattr(process, "CLI_PROBE_CALLER_TIMEOUT_MARGIN_SECONDS", _TINY_GRACE_SECONDS)


def _record_channels(monkeypatch) -> list[process.CliLineChannel]:
    channels: list[process.CliLineChannel] = []

    def build(maximum_lines: int) -> process.CliLineChannel:
        channels.append(process.CliLineChannel(maximum_lines))
        return channels[-1]

    monkeypatch.setattr(codex_image, "CliLineChannel", build)
    return channels


@pytest.mark.parametrize(
    "spawn_error",
    (OSError(A_RUNNER_FAILURE_CANARY), BaseException(A_RUNNER_FAILURE_CANARY)),
    ids=("os-error", "base-exception"),
)
def test_a_failed_spawn_ends_the_image_turn_with_a_cli_error(
    monkeypatch, image_turn, spawn_error: BaseException,
) -> None:
    monkeypatch.setattr(process, "open_pipes", Mock(side_effect=spawn_error))
    _the_child_plays(monkeypatch, ":")

    turn = image_turn.drive(_TURN_SECONDS)

    assert type(turn.error) is codex_image.CodexImageCliError
    image_turn.assert_ended(turn)


def test_a_channel_that_cannot_publish_is_ended_by_the_consumers_own_settle_instant(
    monkeypatch, image_turn,
) -> None:
    _settle_within_a_few_hundredths(monkeypatch)
    monkeypatch.setattr(
        process.CliLineChannel, "_close", Mock(side_effect=RuntimeError(A_RUNNER_FAILURE_CANARY)),
    )
    _the_child_plays(monkeypatch, ":")

    turn = image_turn.drive(_TURN_SECONDS)

    assert type(turn.error) is codex_image.CodexImageTimeoutError
    settle_instant = process.cli_settle_deadline(_TURN_SECONDS)
    assert settle_instant <= turn.elapsed <= settle_instant + _ALLOWANCE_SECONDS
    image_turn.assert_ended(turn, runner_steps=("close RuntimeError",))


def test_a_channel_whose_publication_blocks_is_ended_by_the_consumers_own_settle_instant(
    monkeypatch, image_turn,
) -> None:
    _settle_within_a_few_hundredths(monkeypatch)
    channels = _record_channels(monkeypatch)
    release_publication = threading.Event()
    real_close = process.CliLineChannel._close

    def close_once_released(channel, outcome):
        release_publication.wait(timeout=_HANG_BOUND_SECONDS)
        return real_close(channel, outcome)

    monkeypatch.setattr(process.CliLineChannel, "_close", close_once_released)
    _the_child_plays(monkeypatch, ":")
    try:
        turn = image_turn.drive(_TURN_SECONDS)
    finally:
        release_publication.set()

    assert type(turn.error) is codex_image.CodexImageTimeoutError
    settle_instant = process.cli_settle_deadline(_TURN_SECONDS)
    assert settle_instant <= turn.elapsed <= settle_instant + _ALLOWANCE_SECONDS
    image_turn.assert_ended(turn)
    held = channels[0].receive(timeout=0)
    assert isinstance(held, CliRunOutcome)
    assert held.reason is CliRunReason.CLEANUP_OVERRAN


def test_a_runner_thread_that_cannot_start_reports_by_class_through_a_failing_log_handler(
    monkeypatch, image_turn, capfd,
) -> None:
    fail_to_start_the_runner_thread(monkeypatch, SystemExit(A_RUNNER_FAILURE_CANARY))
    handler = FailingLogHandler()
    library_logger = logging.getLogger("agent_providers")
    library_logger.addHandler(handler)
    _the_child_plays(monkeypatch, ":")
    try:
        turn = image_turn.drive(_TURN_SECONDS)
        image_turn.watch.wait_for_the_runner_threads_to_end()
    finally:
        library_logger.removeHandler(handler)

    assert type(turn.error) is codex_image.CodexImageCliError
    assert len(handler.handled_records) == 2
    image_turn.assert_ended(turn, runner_steps=("start SystemExit", "run SystemExit"))


def test_a_failed_runner_whose_outcome_cannot_be_published_leaks_nothing_through_the_thread(
    monkeypatch, image_turn,
) -> None:
    _settle_within_a_few_hundredths(monkeypatch)
    fail_to_start_the_runner_thread(monkeypatch, SystemExit(A_RUNNER_FAILURE_CANARY))
    monkeypatch.setattr(
        process.CliLineChannel, "_close", Mock(side_effect=RuntimeError(A_RUNNER_FAILURE_CANARY)),
    )
    _the_child_plays(monkeypatch, ":")

    turn = image_turn.drive(_TURN_SECONDS)

    assert type(turn.error) is codex_image.CodexImageTimeoutError
    image_turn.watch.wait_for_the_runner_threads_to_end()
    image_turn.assert_ended(
        turn,
        runner_steps=(
            "start SystemExit", "close RuntimeError", "run SystemExit", "publish RuntimeError",
        ),
    )


def test_a_failed_runner_whose_report_raises_leaks_nothing_through_the_thread(
    monkeypatch, image_turn,
) -> None:
    monkeypatch.setattr(
        codex_protocol, "run_cli_bounded", Mock(side_effect=OSError(A_RUNNER_FAILURE_CANARY)),
    )
    handler = RaisingLogHandler([RuntimeError(A_RUNNER_FAILURE_CANARY)])
    process_logger = logging.getLogger("agent_providers.process")
    process_logger.addHandler(handler)
    try:
        turn = image_turn.drive(_TURN_SECONDS)
        image_turn.watch.wait_for_the_runner_threads_to_end()
    finally:
        process_logger.removeHandler(handler)

    assert type(turn.error) is codex_image.CodexImageCliError
    image_turn.watch.assert_nothing_leaked(turn.error)


@pytest.mark.parametrize(
    "runner_error",
    (OSError, BaseException, KeyboardInterrupt, SystemExit),
    ids=("os-error", "base-exception", "keyboard-interrupt", "system-exit"),
)
def test_a_runner_that_raises_ends_the_image_turn_and_names_only_its_class(
    monkeypatch, image_turn, runner_error: type[BaseException],
) -> None:
    monkeypatch.setattr(
        codex_protocol, "run_cli_bounded", Mock(side_effect=runner_error(A_RUNNER_FAILURE_CANARY)),
    )

    turn = image_turn.drive(_TURN_SECONDS)

    assert type(turn.error) is codex_image.CodexImageCliError
    assert turn.elapsed < 1
    image_turn.assert_ended(
        turn, runner_steps=(f"run {runner_error.__name__}",), holdings_checked=False,
    )


def test_an_image_thread_that_cannot_start_abandons_its_reservation_and_raises_unchained(
    monkeypatch, image_turn,
) -> None:
    fail_to_start_the_runner_thread(
        monkeypatch, RuntimeError(A_RUNNER_FAILURE_CANARY), codex_protocol.run_reserved_codex_cli,
    )

    turn = image_turn.drive(_TURN_SECONDS)

    assert type(turn.error) is codex_image.CodexImageCliError
    assert turn.error.__cause__ is None
    assert turn.error.__context__ is None
    image_turn.assert_ended(turn)


@pytest.mark.parametrize(
    "control_flow",
    (SystemExit(A_RUNNER_FAILURE_CANARY), KeyboardInterrupt(A_RUNNER_FAILURE_CANARY)),
    ids=("system-exit", "keyboard-interrupt"),
)
def test_control_flow_from_an_image_thread_that_cannot_start_passes_after_abandoning_its_slot(
    monkeypatch, image_turn, control_flow: BaseException,
) -> None:
    fail_to_start_the_runner_thread(
        monkeypatch, control_flow, codex_protocol.run_reserved_codex_cli,
    )

    turn = image_turn.drive(_TURN_SECONDS)

    assert turn.error is control_flow
    assert image_turn.process_pool.image_reservation_count() == 0
    assert list(image_turn.working_root.iterdir()) == []
    image_turn.watch.assert_nothing_leaked()


def test_a_blocked_turn_returns_by_the_settle_instant_with_its_child_reaped(
    monkeypatch, image_turn,
) -> None:
    _the_child_prints_then_sleeps(
        monkeypatch, [*_THREAD_OPENING, _item_event("item.started", "command_execution")],
    )

    turn = image_turn.drive(_HANG_BOUND_SECONDS)
    reservations_at_return = image_turn.process_pool.image_reservation_count()

    assert reservations_at_return == 0
    assert type(turn.error) is codex_image.ImageToolBlockedError
    assert turn.elapsed < process.cli_settle_deadline(0)
    assert [warning.split(" code=")[0] for warning in _warnings(image_turn.caplog)] == [
        "Codex image tool rejected route=cli",
    ]
    image_turn.assert_ended(turn)


def test_an_abort_signal_set_after_the_first_line_ends_the_turn_early(
    monkeypatch, image_turn,
) -> None:
    abort_signal = threading.Event()
    real_accept = codex_image._CodexImageEventGate.accept

    def accept_then_abort(gate, line: bytes) -> None:
        real_accept(gate, line)
        abort_signal.set()

    monkeypatch.setattr(codex_image._CodexImageEventGate, "accept", accept_then_abort)
    _the_child_prints_then_sleeps(monkeypatch, _THREAD_OPENING[:1])

    turn = image_turn.drive(_HANG_BOUND_SECONDS, abort_signal=abort_signal)

    assert type(turn.error) is codex_image.CodexImageCliError
    assert turn.elapsed < process.cli_settle_deadline(0)
    image_turn.assert_ended(turn)


def test_a_generated_image_is_returned_with_no_reservation_and_no_report(
    monkeypatch, image_turn,
) -> None:
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _generated_png_runner())

    turn = image_turn.drive(_TURN_SECONDS)

    assert turn.error is None
    assert turn.value is not None
    assert turn.value.startswith(b"\x89PNG")
    image_turn.assert_ended(turn)


def test_a_child_that_outlasts_a_short_deadline_ends_the_turn_by_the_settle_instant(
    monkeypatch, image_turn,
) -> None:
    _the_child_plays(monkeypatch, "exec sleep 10")

    turn = image_turn.drive(_TURN_SECONDS)
    reservations_at_return = image_turn.process_pool.image_reservation_count()

    assert reservations_at_return == 0
    assert type(turn.error) is codex_image.CodexImageTimeoutError
    assert turn.elapsed <= process.cli_settle_deadline(_TURN_SECONDS) + _ALLOWANCE_SECONDS
    image_turn.assert_ended(turn)


def test_a_zombie_child_ends_the_turn_while_its_reservation_waits_for_the_background_reap(
    monkeypatch, image_turn,
) -> None:
    releases = _HomeReleases(monkeypatch)
    release_background_reap = threading.Event()
    real_background_reap = process._reap_in_background

    def reap_once_released(child, on_reaped) -> None:
        release_background_reap.wait(timeout=_HANG_BOUND_SECONDS)
        os.killpg(child.pid, signal.SIGKILL)
        real_background_reap(child, on_reaped)

    monkeypatch.setattr(process, "_reap_process_group", lambda _child: True)
    monkeypatch.setattr(process, "_reap_in_background", reap_once_released)
    _the_child_plays(monkeypatch, "exec sleep 10")
    try:
        turn = image_turn.drive(_TURN_SECONDS)
        held_at_return = image_turn.process_pool.image_reservation_count()
    finally:
        release_background_reap.set()

    assert type(turn.error) is codex_image.CodexImageTimeoutError
    assert held_at_return == 1
    releases.wait_for_one()
    image_turn.assert_ended(turn)


def test_a_prompt_that_cannot_be_encoded_is_refused_before_anything_is_reserved(
    monkeypatch, image_turn,
) -> None:
    runner = Mock(side_effect=AssertionError("no runner may start"))
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", runner)
    only_image_slot = image_turn.process_pool.reserve(CodexProcessKind.IMAGE)

    turn = image_turn.drive(_TURN_SECONDS, prompt="a lone surrogate \ud800")

    assert type(turn.error) is codex_image.CodexImageCliError
    runner.assert_not_called()
    image_turn.assert_ended(turn, reservations=1)
    image_turn.process_pool.abandon_unspawned(only_image_slot)


_WITH_IMAGE_GEN_ITEMS = [
    *_THREAD_OPENING,
    _item_event("item.started", "image_gen"),
    _item_event("item.completed", "image_gen"),
    _TURN_COMPLETED,
]


@pytest.mark.parametrize(
    ("stdout", "create_artifacts"),
    (
        (_MEASURED_STREAM, _write_generated_file("thread/image.png", _png_bytes())),
        (_MEASURED_STREAM, _write_png_beside_a_text_file),
        (_jsonl(_WITH_IMAGE_GEN_ITEMS), _write_generated_file("image.png", _png_bytes())),
    ),
    ids=(
        "png-nested-in-a-directory",
        "png-beside-a-non-png-file",
        "image-gen-items-around-the-png",
    ),
)
def test_codex_image_returns_the_generated_png_of_a_completed_turn(
    monkeypatch, stdout: str, create_artifacts: Callable[[Path], None],
) -> None:
    monkeypatch.setattr(
        codex_protocol, "run_cli_bounded", _image_runner(_outcome(stdout=stdout), create_artifacts),
    )

    assert _generate_image().startswith(b"\x89PNG")


@pytest.mark.parametrize(
    ("stdout", "create_artifacts", "expected_tokens"),
    (
        (_MEASURED_STREAM, _write_generated_directory, _MEASURED_TOKENS),
        (_MEASURED_STREAM, _write_generated_file("notes.txt", b"not an image"), _MEASURED_TOKENS),
        (_MEASURED_STREAM, None, _MEASURED_TOKENS),
        (
            (_FIXTURES / "codex-image-no-image-events.jsonl").read_text(),
            None,
            (
                "thread.started",
                "item.completed:error",
                "turn.started",
                "item.completed:agent_message*2",
                "turn.completed",
            ),
        ),
        (_jsonl([_TURN_COMPLETED]), None, ("turn.completed",)),
    ),
    ids=(
        "only-an-empty-directory",
        "only-a-non-png-file",
        "no-generated-images-directory",
        "the-recorded-no-image-turn",
        "only-a-completed-turn",
    ),
)
def test_codex_image_reports_a_completed_turn_without_a_png_as_not_created(
    monkeypatch,
    caplog,
    stdout: str,
    create_artifacts: Callable[[Path], None] | None,
    expected_tokens: tuple[str, ...],
) -> None:
    monkeypatch.setattr(
        codex_protocol, "run_cli_bounded", _image_runner(_outcome(stdout=stdout), create_artifacts),
    )
    caplog.set_level("WARNING", logger="agent_providers.codex.image")

    with pytest.raises(codex_image.CodexImageNotCreatedError):
        _generate_image(_A_PRIVATE_PROMPT_MARKER)

    expected_line = f"Codex image not created route=cli {_types_line(expected_tokens)}"
    assert _warnings(caplog) == [expected_line]
    assert all(record.exc_info is None for record in caplog.records)
    assert _A_PRIVATE_PROMPT_MARKER not in caplog.text


@pytest.mark.parametrize(
    "create_artifacts",
    (_write_two_generated_pngs, _symlink_generated_images_root),
    ids=("two-pngs", "symlinked-root"),
)
def test_codex_image_refuses_ambiguous_artifacts_without_logging_the_gate(
    monkeypatch, caplog, create_artifacts: Callable[[Path], None],
) -> None:
    monkeypatch.setattr(
        codex_protocol,
        "run_cli_bounded",
        _image_runner(_outcome(stdout=_MEASURED_STREAM), create_artifacts),
    )
    caplog.set_level("WARNING", logger="agent_providers.codex.image")

    with pytest.raises(codex_image.CodexImageArtifactError):
        _generate_image()

    assert _warnings(caplog) == []


def _recorded_0_147_events(codex_home: Path) -> list[dict]:
    recorded = (_FIXTURES / "codex-imagegen-real-stream.jsonl").read_text()
    return [
        json.loads(line)
        for line in recorded.replace("{CODEX_HOME}", str(codex_home)).splitlines()
    ]


_BLOCKED_TURNS = (
    pytest.param(
        _recorded_0_147_events,
        codex_image.ImageToolBlockedReason.BLOCKED_ITEM,
        (*_OPENING_TOKENS, "item.completed:agent_message", "item.started:command_execution"),
        id="the-0.147-skill-read-through-bash",
    ),
    *(
        pytest.param(
            [*_THREAD_OPENING, _item_event(event_type, kind)],
            codex_image.ImageToolBlockedReason.BLOCKED_ITEM,
            (*_OPENING_TOKENS, f"{event_type}:{kind}"),
            id=f"blocked-item-{kind}-on-{event_type}",
        )
        for kind in sorted(codex_protocol.BLOCKED_ITEM_TYPES)
        for event_type in sorted(codex_protocol.ITEM_EVENT_TYPES)
    ),
    pytest.param(
        [
            *_THREAD_OPENING,
            f"{_A_PRIVATE_EVENT_MARKER}, not JSON\n".encode(),
            _item_event("item.started", "web_search"),
        ],
        codex_image.ImageToolBlockedReason.BLOCKED_ITEM,
        (*_OPENING_TOKENS, "other", "item.started:web_search"),
        id="an-unparsable-line-is-named-other",
    ),
    pytest.param(
        [*_THREAD_OPENING, {"type": _A_PRIVATE_EVENT_MARKER}],
        codex_image.ImageToolBlockedReason.UNEXPECTED_EVENT,
        (*_OPENING_TOKENS, "other"),
        id="unknown-event-type-is-named-other",
    ),
    pytest.param(
        [*_THREAD_OPENING, _item_event("item.started", "future_item")],
        codex_image.ImageToolBlockedReason.UNEXPECTED_ITEM,
        (*_OPENING_TOKENS, "item.started:other"),
        id="unknown-item-kind-is-named-other",
    ),
    pytest.param(
        [
            *_THREAD_OPENING,
            {"type": "turn.failed", "error": {"message": "failed"}},
            _item_event("item.started", "web_search"),
        ],
        codex_image.ImageToolBlockedReason.BLOCKED_ITEM,
        (*_OPENING_TOKENS, "turn.failed", "item.started:web_search"),
        id="blocked-item-after-a-failed-turn",
    ),
    pytest.param(
        [
            *_THREAD_OPENING,
            {"type": "error", "message": "failed"},
            _item_event("item.started", "web_search"),
        ],
        codex_image.ImageToolBlockedReason.BLOCKED_ITEM,
        (*_OPENING_TOKENS, "error", "item.started:web_search"),
        id="blocked-item-after-a-top-level-error",
    ),
)


@pytest.mark.parametrize(("events", "expected_reason", "expected_tokens"), _BLOCKED_TURNS)
def test_codex_image_aborts_a_blocked_turn_and_logs_only_closed_names(
    monkeypatch,
    caplog,
    events: _StreamedEvents,
    expected_reason: codex_image.ImageToolBlockedReason,
    expected_tokens: tuple[str, ...],
) -> None:
    state = {"aborted": False, "reaped": False}
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _streaming_runner(events, state))
    caplog.set_level("WARNING", logger="agent_providers.codex.image")

    with pytest.raises(codex_image.ImageToolBlockedError) as raised:
        _generate_image(_A_PRIVATE_PROMPT_MARKER)

    assert (raised.value.reason, raised.value.observed) == (expected_reason, expected_tokens)
    expected_line = (
        f"Codex image tool rejected route=cli code={expected_reason.value} "
        f"{_types_line(expected_tokens)}"
    )
    assert _warnings(caplog) == [expected_line]
    assert all(record.exc_info is None for record in caplog.records)
    for private in (_A_PRIVATE_PROMPT_MARKER, _A_PRIVATE_EVENT_MARKER):
        assert private not in caplog.text
        assert private not in repr(raised.value) + str(raised.value)
    assert state == {"aborted": True, "reaped": True}


_ALTERNATING_ITEM_EVENTS = ("item.started", "item.completed")


def _alternating_agent_messages(count: int) -> list[dict]:
    return [
        _item_event(_ALTERNATING_ITEM_EVENTS[index % 2], "agent_message") for index in range(count)
    ]


@pytest.mark.parametrize(
    ("events", "expected_tokens"),
    (
        (
            [*_THREAD_OPENING, *_alternating_agent_messages(3), _TURN_COMPLETED],
            (
                *_OPENING_TOKENS,
                "item.started:agent_message",
                "item.completed:agent_message",
                "item.started:agent_message",
                "turn.completed",
            ),
        ),
        (
            [
                *_THREAD_OPENING,
                *[_item_event("item.completed", "agent_message")] * 100,
                _TURN_COMPLETED,
            ],
            (*_OPENING_TOKENS, "item.completed:agent_message*100", "turn.completed"),
        ),
        (
            [
                *[event for message in _alternating_agent_messages(40) for event in (message,) * 2],
                _TURN_COMPLETED,
            ],
            (
                "...+9",
                *(
                    f"{_ALTERNATING_ITEM_EVENTS[index % 2]}:agent_message*2"
                    for index in range(9, 40)
                ),
                "turn.completed",
            ),
        ),
        (
            [*_alternating_agent_messages(40), _TURN_COMPLETED],
            (
                "...+9",
                *(f"{_ALTERNATING_ITEM_EVENTS[index % 2]}:agent_message" for index in range(9, 40)),
                "turn.completed",
            ),
        ),
    ),
    ids=(
        "single-occurrences-stay-bare",
        "repeats-collapse-before-the-window",
        "dropped-count-counts-collapsed-tokens-not-lines",
        "a-long-turn-keeps-the-last-32-behind-a-dropped-count",
    ),
)
def test_a_validated_transcript_returns_its_bounded_closed_tokens(
    tmp_path: Path, events: list[dict], expected_tokens: tuple[str, ...],
) -> None:
    assert codex_image._validate_codex_image_events(
        _jsonl(events), codex_home=tmp_path,
    ) == expected_tokens


def test_a_deviating_transcript_raises_the_blocked_error_with_its_tokens(tmp_path: Path) -> None:
    transcript = _jsonl([*_THREAD_OPENING, _item_event("item.started", "command_execution")])

    with pytest.raises(codex_image.ImageToolBlockedError) as raised:
        codex_image._validate_codex_image_events(transcript, codex_home=tmp_path)

    assert raised.value.observed == (*_OPENING_TOKENS, "item.started:command_execution")


@pytest.mark.parametrize(
    "document",
    (
        None,
        {**_A_CODEX_LOGIN, "tokens": {"id_token": "id-token"}},
        {
            **_A_CODEX_LOGIN,
            "tokens": {**_A_CODEX_LOGIN["tokens"], "account_id": 7},
        },
    ),
    ids=("missing", "incomplete", "wrong-typed"),
)
def test_codex_public_adapters_reject_unusable_logins(
    tmp_path: Path,
    document: dict | None,
) -> None:
    auth_file = tmp_path / "invalid-auth.json"
    if document is not None:
        auth_file.write_text(json.dumps(document))
    turns = _an_empty_working_root(tmp_path)
    override_provider_runtime(codex_cli_auth_file=auth_file)

    with pytest.raises(codex_image.CodexImageLoginError):
        codex_image.generate_codex_image("prompt", policy=AN_IMAGE_POLICY, deadline=10_000_000)
    with pytest.raises(ProviderUnavailableError) as raised:
        _transport()

    assert raised.value.reason.code is SafeRouteReasonCode.CLI_AUTH_REJECTED
    assert list(turns.iterdir()) == []


@CALLER_CONTEXTS
def test_a_refused_codex_call_hands_its_text_only_to_the_host(
    monkeypatch, caplog, while_the_caller_handles_an_exception,
) -> None:
    monkeypatch.setattr(
        codex_protocol,
        "run_cli_bounded",
        _runner(_tool_round_lines(a_call_to_an_undeclared_tool_carrying()), _outcome(), []),
    )
    caplog.set_level(logging.DEBUG)

    turn = drive_turn(
        "codex",
        correlation_id="job-41",
        while_the_caller_handles_an_exception=while_the_caller_handles_an_exception,
    )

    assert_the_canary_reaches_only_the_host(
        turn,
        caplog.records,
        provider="codex",
        rule=TextToolRejectionRule.UNKNOWN_TOOL,
        correlation_id="job-41",
    )


@CALLER_CONTEXTS
def test_a_codex_call_nested_too_deeply_is_refused_not_a_failed_route(
    monkeypatch, caplog, while_the_caller_handles_an_exception,
) -> None:
    monkeypatch.setattr(
        codex_protocol,
        "run_cli_bounded",
        _runner(_tool_round_lines(a_call_nested_too_deeply_to_decode()), _outcome(), []),
    )
    caplog.set_level(logging.DEBUG)

    turn = drive_turn(
        "codex",
        while_the_caller_handles_an_exception=while_the_caller_handles_an_exception,
    )

    assert isinstance(turn.error, TextToolCallRejectedError)
    assert turn.error.reason.code is SafeRouteReasonCode.TOOL_PROTOCOL_ERROR
    assert turn.error.rejection.rule is TextToolRejectionRule.CALL_NOT_JSON
    assert "RecursionError" not in formatted_log_text(caplog.records)


async def _drain_first_round(transport) -> None:
    async for _ in transport.stream(InitialTurn("system", [])):
        pass


def test_a_codex_event_line_nested_too_deeply_is_refused_as_a_cli_protocol_error(
    monkeypatch,
) -> None:
    calls: list = []
    lines = [DEEPLY_NESTED_JSON.encode() + b"\n"]
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _runner(lines, _outcome(), calls))
    transport = _transport()

    async def collect() -> None:
        with pytest.raises(ProviderUnavailableError) as raised:
            await _drain_first_round(transport)
        assert raised.value.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
        await transport.aclose()

    asyncio.run(collect())

    assert len(calls) == 1


def test_a_codex_tool_result_nested_too_deeply_is_passed_on_in_the_next_prompt(
    monkeypatch,
) -> None:
    calls: list = []
    lines = _tool_round_lines('<notes_tool_call>\n{"name":"list_notes","arguments":{}}\n'
                              "</notes_tool_call>")
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _runner(lines, _outcome(), calls))
    transport = _transport()

    async def collect() -> None:
        await _drain_first_round(transport)
        nested_result = ToolResultBatch((ToolResult("one", DEEPLY_NESTED_JSON, False),))
        assert [item async for item in transport.stream(nested_result)]
        await transport.aclose()

    asyncio.run(collect())

    assert DEEPLY_NESTED_JSON.encode() in calls[1][1]["stdin_payload"]


_UNDECODABLE_EVENT_LINES = pytest.mark.parametrize(
    "event_line",
    [
        pytest.param(b'{"type":"item.completed","text":"event-line-canary\n', id="malformed-json"),
        pytest.param(b"\xff\xfe event-line-canary\n", id="invalid-utf-8"),
    ],
)


@_UNDECODABLE_EVENT_LINES
def test_the_codex_line_reader_refuses_an_undecodable_line_without_chaining_it(event_line):
    with pytest.raises(codex_protocol.CodexCliStreamFailure) as raised:
        codex_protocol.parse_codex_line(event_line)

    assert raised.value.code == "codex_cli_stream_protocol_error"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


@_UNDECODABLE_EVENT_LINES
@CALLER_CONTEXTS
def test_a_codex_event_line_the_cli_cannot_decode_leaves_no_chain_to_its_text(
    monkeypatch, event_line, while_the_caller_handles_an_exception,
) -> None:
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _runner([event_line], _outcome(), []))
    transport = _transport()

    async def stream_the_turn() -> BaseException:
        try:
            async for _ in transport.stream(InitialTurn("system", [])):
                pass
        except ProviderUnavailableError as error:
            return error
        raise AssertionError("the undecodable line was accepted")

    async def stream_while_handling_an_exception() -> BaseException:
        try:
            raise CallerCanaryError(A_CALLER_CANARY)
        except CallerCanaryError:
            return await stream_the_turn()

    stream = (
        stream_while_handling_an_exception
        if while_the_caller_handles_an_exception
        else stream_the_turn
    )
    error = asyncio.run(stream())

    assert isinstance(error, ProviderUnavailableError)
    assert error.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
    assert str(error) == "CLI returned an invalid response."
    assert error.__context__ is None
    assert error.__cause__ is None
    assert "event-line-canary" not in "".join(traceback.format_exception(error))


_CANARY_REFRESH_TOKEN = "canary-refresh-token-of-the-operator"
_OPERATORS_LIVE_LOGIN = {
    **_A_CODEX_LOGIN,
    "tokens": {
        **_A_CODEX_LOGIN["tokens"],
        "refresh_token": _CANARY_REFRESH_TOKEN,
        "session_token": _CANARY_REFRESH_TOKEN,
    },
}


def _recording_runner(
    copies: list[tuple[tuple[str, ...], str]], runner, every_file_content: list[bytes],
) -> object:
    def run_cli_bounded(child, **kwargs):
        codex_home = Path(child.environment["CODEX_HOME"])
        login = codex_home / "auth.json"
        copies.append((child.command, login.read_text() if login.is_file() else ""))
        every_file_content.extend(
            child_file.read_bytes() for child_file in filter(Path.is_file, codex_home.rglob("*"))
        )
        return runner(child, **kwargs)

    return run_cli_bounded


def _probe_runner(stdout: str) -> object:
    def run_cli_bounded(_child, **_kwargs):
        return _outcome(stdout=stdout)

    return run_cli_bounded


def _drive_the_catalog_probe(monkeypatch, copies, files) -> None:
    runner = _probe_runner("[]")
    monkeypatch.setattr(process, "run_cli_bounded", _recording_runner(copies, runner, files))
    process.codex_cli_model_catalog()


def _drive_the_login_probe(monkeypatch, copies, files) -> None:
    runner = _probe_runner("Logged in using ChatGPT")
    monkeypatch.setattr(process, "run_cli_bounded", _recording_runner(copies, runner, files))
    assert process.codex_cli_login().logged_in is True


def _drive_the_tool_turn(monkeypatch, copies, files) -> None:
    runner = _runner(_tool_round_lines("done"), _outcome(), [])
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _recording_runner(copies, runner, files))
    asyncio.run(_collect_tool_events(_codex_tool_events(
        _transport(), lambda _name, _arguments: ToolOutcome("{}", False),
    )))


def _drive_the_image_turn(monkeypatch, copies, files) -> None:
    runner = _recording_runner(copies, _generated_png_runner(), files)
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", runner)
    codex_image.generate_codex_image("prompt", policy=AN_IMAGE_POLICY, deadline=10_000_000)


@pytest.mark.parametrize(
    "drive",
    (
        _drive_the_catalog_probe,
        _drive_the_login_probe,
        _drive_the_tool_turn,
        _drive_the_image_turn,
    ),
    ids=("catalog", "login-status", "tool-turn", "image-turn"),
)
def test_no_codex_child_is_handed_the_operators_live_refresh_token(
    monkeypatch, caplog, codex_login_source: Path, drive,
) -> None:
    codex_login_source.write_text(json.dumps(_OPERATORS_LIVE_LOGIN))
    operators_file = codex_login_source.read_bytes()
    monkeypatch.setattr(process, "resolve_cli_binary", lambda _binary: Path("/usr/bin/codex"))
    clear_agent_cli_caches()
    caplog.set_level(logging.DEBUG)
    copies: list[tuple[tuple[str, ...], str]] = []
    files: list[bytes] = []

    drive(monkeypatch, copies, files)

    (command, child_login), = copies
    assert json.loads(child_login)["tokens"]["refresh_token"] == ""
    assert json.loads(child_login)["tokens"]["access_token"] == CANARY_ACCESS_TOKEN
    assert codex_login_source.read_bytes() == operators_file
    assert _CANARY_REFRESH_TOKEN not in child_login
    assert not any(_CANARY_REFRESH_TOKEN.encode() in content for content in files)
    assert not any(_CANARY_REFRESH_TOKEN in argument for argument in command)
    assert _CANARY_REFRESH_TOKEN not in formatted_log_text(caplog.records)


def _drive_the_image_path(_codex_login_source: Path) -> BaseException:
    with pytest.raises(codex_image.CodexImageLoginError) as raised:
        codex_image.generate_codex_image("prompt", policy=AN_IMAGE_POLICY, deadline=10_000_000)
    return raised.value


def _drive_the_tool_turn_path(_codex_login_source: Path) -> BaseException:
    with pytest.raises(ProviderUnavailableError) as raised:
        _transport()
    return raised.value


_MALFORMED_LOGIN_WITH_CANARY = f'{{"refresh_token": "{_CANARY_REFRESH_TOKEN}", '.encode()


def _drive_the_reader_with_an_oversize_source(codex_login_source: Path) -> BaseException:
    codex_login_source.write_bytes(
        _CANARY_REFRESH_TOKEN.encode() + b" " * CREDENTIAL_FILE_BOUND_BYTES,
    )
    with pytest.raises(AgentCliUnavailableError) as raised:
        process._read_credential_file(codex_login_source)
    return raised.value


def _drive_the_credential_seam(codex_login_source: Path) -> BaseException:
    with pytest.raises(process.CliCredentialNotConfiguredError) as raised:
        process.PrivateCredentialHome(
            codex_login_source,
            Path("auth.json"),
            prefix="seam-",
            missing_credential_is_error=True,
            credential_copy=blanked_copy,
        )
    return raised.value


@pytest.mark.parametrize(
    "drive",
    (
        _drive_the_tool_turn_path,
        _drive_the_image_path,
        _drive_the_credential_seam,
        _drive_the_reader_with_an_oversize_source,
    ),
    ids=("tool-turn", "image", "seam", "reader-oversize"),
)
def test_a_malformed_login_leaves_no_credential_content_in_the_error_chain(
    monkeypatch, codex_login_source: Path, drive,
) -> None:
    codex_login_source.write_bytes(_MALFORMED_LOGIN_WITH_CANARY)
    monkeypatch.setattr(process, "resolve_cli_binary", lambda _binary: Path("/usr/bin/codex"))

    error = drive(codex_login_source)

    chain = exception_chain(error)
    assert not any(
        _CANARY_REFRESH_TOKEN in "".join(traceback.format_exception(link)) for link in chain
    )
    assert all(not isinstance(link, ValueError) for link in chain)
    assert not traceback_frames_hold(error, _CANARY_REFRESH_TOKEN)


def _a_refused_symbolic_link(codex_login_source: Path) -> BaseException:
    link = codex_login_source.with_name("link.json")
    link.symlink_to(codex_login_source)
    with pytest.raises(AgentCliUnavailableError) as raised:
        process._read_credential_file(link)
    return raised.value


def _a_malformed_login_judged_usable_or_not(codex_login_source: Path) -> BaseException:
    codex_login_source.write_bytes(_MALFORMED_LOGIN_WITH_CANARY)
    with pytest.raises(AgentCliUnavailableError) as raised:
        process.codex_cli_login_is_usable()
    return raised.value


def _a_turn_refused_for_a_login_source(
    turn, codex_login_source: Path, make_source,
) -> BaseException:
    override_provider_runtime(codex_cli_auth_file=make_source(codex_login_source.parent))
    return turn()


def _the_tool_turn_refusal() -> BaseException:
    with pytest.raises(ProviderUnavailableError) as raised:
        _transport()
    return raised.value


def _the_image_turn_refusal() -> BaseException:
    with pytest.raises(codex_image.CodexImageLoginError) as raised:
        _generate_image()
    return raised.value


def _a_login_source_that_is_malformed(directory: Path) -> Path:
    path = directory / "malformed.json"
    path.write_bytes(_MALFORMED_LOGIN_WITH_CANARY)
    return path


@pytest.mark.parametrize(
    "refuse",
    (
        _a_refused_symbolic_link,
        _a_malformed_login_judged_usable_or_not,
        lambda source: _a_turn_refused_for_a_login_source(
            _the_tool_turn_refusal, source, a_symlink_to_a_login,
        ),
        lambda source: _a_turn_refused_for_a_login_source(
            _the_image_turn_refusal, source, a_symlink_to_a_login,
        ),
        lambda source: _a_turn_refused_for_a_login_source(
            _the_tool_turn_refusal, source, _a_login_source_that_is_malformed,
        ),
        lambda source: _a_turn_refused_for_a_login_source(
            _the_image_turn_refusal, source, _a_login_source_that_is_malformed,
        ),
    ),
    ids=(
        "reader-symlink", "decider-malformed", "tool-turn-symlink", "image-turn-symlink",
        "tool-turn-malformed", "image-turn-malformed",
    ),
)
def test_a_credential_refusal_carries_no_chain_inside_a_handler(
    codex_login_source: Path, refuse,
) -> None:
    try:
        raise RuntimeError("the caller's own failure")
    except RuntimeError:
        refusal = refuse(codex_login_source)

    assert refusal.__cause__ is None
    assert refusal.__context__ is None
    assert not traceback_frames_hold(refusal, _CANARY_REFRESH_TOKEN)


@pytest.mark.parametrize(
    "document",
    (
        {**_A_CODEX_LOGIN, "tokens": {"access_token": "access-token"}},
        {**_A_CODEX_LOGIN, "tokens": {**_A_CODEX_LOGIN["tokens"], "account_id": 7}},
    ),
    ids=("incomplete", "wrong-typed"),
)
def test_the_codex_catalog_probe_starts_without_an_unusable_login(
    monkeypatch, codex_login_source: Path, document: dict,
) -> None:
    codex_login_source.write_text(json.dumps(document))
    monkeypatch.setattr(process, "resolve_cli_binary", lambda _binary: Path("/usr/bin/codex"))
    clear_agent_cli_caches()
    copies: list[tuple[tuple[str, ...], str]] = []
    failing_child = _recording_runner(
        copies, lambda _child, **_kwargs: _outcome(returncode=1), [],
    )
    monkeypatch.setattr(process, "run_cli_bounded", failing_child)

    with pytest.raises(ProviderModelCatalogUnavailableError) as raised:
        list_provider_models("codex", ProviderRoute.CLI)

    (_command, child_login), = copies
    assert child_login == ""
    assert raised.value.reason.code is SafeRouteReasonCode.CATALOGUE_PROTOCOL_ERROR


def _the_tool_turn_is_started(monkeypatch, seen: list) -> None:
    runner = _runner(_tool_round_lines("done"), _outcome(), [])
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _stating_runner(seen, runner))
    asyncio.run(_collect_tool_events(_codex_tool_events(
        _transport(), lambda _name, _arguments: ToolOutcome("{}", False),
    )))


def _the_image_turn_is_started(monkeypatch, seen: list) -> None:
    monkeypatch.setattr(
        codex_protocol, "run_cli_bounded", _stating_runner(seen, _generated_png_runner()),
    )
    codex_image.generate_codex_image("prompt", policy=AN_IMAGE_POLICY, deadline=10_000_000)


@dataclass(frozen=True)
class _SeenHome:
    name: str
    login_mode: int
    login_bytes: bytes
    directory_modes: dict[str, int]


def _stating_runner(seen: list, runner) -> object:
    def run_cli_bounded(child, **kwargs):
        codex_home = Path(child.environment["CODEX_HOME"])
        login = codex_home / "auth.json"
        home = codex_home.parent
        seen.append(_SeenHome(
            name=home.name,
            login_mode=stat.S_IMODE(login.stat().st_mode),
            login_bytes=login.read_bytes(),
            directory_modes={
                relative: stat.S_IMODE((home / relative).stat().st_mode)
                for relative in (".", *(entry.name for entry in home.iterdir()))
            },
        ))
        return runner(child, **kwargs)

    return run_cli_bounded


@pytest.mark.parametrize(
    ("start_the_turn", "prefix"),
    (
        (_the_tool_turn_is_started, CODEX_TOOL_TURN_DIRECTORY_PREFIX),
        (_the_image_turn_is_started, CODEX_IMAGE_TURN_DIRECTORY_PREFIX),
    ),
    ids=("tool-turn", "image-turn"),
)
def test_the_codex_turn_copy_is_private_writable_and_blanked(
    monkeypatch, codex_login_source: Path, start_the_turn, prefix: str,
) -> None:
    seen: list[_SeenHome] = []

    start_the_turn(monkeypatch, seen)

    [home] = seen
    assert home.login_mode == 0o600
    assert home.login_bytes == blanked_copy(codex_login_source.read_bytes())
    assert json.loads(home.login_bytes)["tokens"]["refresh_token"] == ""
    assert home.name.startswith(prefix)
    assert home.directory_modes == {
        ".": 0o700, "work": 0o700, CODEX_HOME_DIRECTORY_NAME: 0o700,
    }


def _an_empty_working_root(tmp_path: Path) -> Path:
    turns = tmp_path / "turns"
    turns.mkdir()
    override_provider_runtime(cli_working_directory_root=turns)
    return turns


def _fail_the_work_directory(monkeypatch: pytest.MonkeyPatch, source: Path) -> None:
    fail_the_work_directory(monkeypatch, OSError(errno.ENOSPC, "synthetic work directory failure"))


def _fail_the_codex_home_directory(monkeypatch: pytest.MonkeyPatch, source: Path) -> None:
    fail_the_directory_named(
        monkeypatch, CODEX_HOME_DIRECTORY_NAME,
        OSError(errno.ENOSPC, "synthetic codex home directory failure"),
    )


def _the_tool_turn_is_started_bare() -> None:
    _transport()


def _the_image_turn_is_started_bare() -> None:
    _generate_image()


def _assert_the_tool_turn_found_the_cli_unavailable(refusal: BaseException) -> None:
    assert isinstance(refusal, ProviderUnavailableError)
    assert refusal.reason.code is SafeRouteReasonCode.CLI_BINARY_UNAVAILABLE
    assert str(refusal) == "CLI is unavailable."


def _assert_the_image_turn_could_not_make_its_home(refusal: BaseException) -> None:
    assert type(refusal) is codex_image.CodexImageCliError
    assert str(refusal) == "could not create a private CLI home"


def _assert_the_tool_turn_found_the_login_rejected(refusal: BaseException) -> None:
    assert isinstance(refusal, ProviderUnavailableError)
    assert refusal.reason.code is SafeRouteReasonCode.CLI_AUTH_REJECTED


def _assert_the_image_turn_found_the_login_rejected(refusal: BaseException) -> None:
    assert type(refusal) is codex_image.CodexImageLoginError


_TOOL_TURN_CANNOT_MAKE_ITS_HOME = (
    _the_tool_turn_is_started_bare, ProviderUnavailableError,
    _assert_the_tool_turn_found_the_cli_unavailable,
)
_IMAGE_TURN_CANNOT_MAKE_ITS_HOME = (
    _the_image_turn_is_started_bare, codex_image.CodexImageCliError,
    _assert_the_image_turn_could_not_make_its_home,
)
_TOOL_TURN_FINDS_THE_LOGIN_REJECTED = (
    _the_tool_turn_is_started_bare, ProviderUnavailableError,
    _assert_the_tool_turn_found_the_login_rejected,
)
_IMAGE_TURN_FINDS_THE_LOGIN_REJECTED = (
    _the_image_turn_is_started_bare, codex_image.CodexImageLoginError,
    _assert_the_image_turn_found_the_login_rejected,
)


@CALLER_CONTEXTS
@pytest.mark.parametrize(
    ("make_the_home_fail", "start_the_turn", "expected_class", "assert_the_refusal"),
    (
        pytest.param(fail_mkdtemp, *_TOOL_TURN_CANNOT_MAKE_ITS_HOME, id="mkdtemp-tool-turn"),
        pytest.param(fail_mkdtemp, *_IMAGE_TURN_CANNOT_MAKE_ITS_HOME, id="mkdtemp-image-turn"),
        pytest.param(
            fail_the_home_chmod, *_TOOL_TURN_CANNOT_MAKE_ITS_HOME, id="home-chmod-tool-turn",
        ),
        pytest.param(
            fail_the_home_chmod, *_IMAGE_TURN_CANNOT_MAKE_ITS_HOME, id="home-chmod-image-turn",
        ),
        pytest.param(
            _fail_the_work_directory, *_TOOL_TURN_CANNOT_MAKE_ITS_HOME,
            id="work-directory-tool-turn",
        ),
        pytest.param(
            _fail_the_work_directory, *_IMAGE_TURN_CANNOT_MAKE_ITS_HOME,
            id="work-directory-image-turn",
        ),
        pytest.param(
            _fail_the_codex_home_directory, *_TOOL_TURN_FINDS_THE_LOGIN_REJECTED,
            id="codex-home-directory-tool-turn",
        ),
        pytest.param(
            _fail_the_codex_home_directory, *_IMAGE_TURN_FINDS_THE_LOGIN_REJECTED,
            id="codex-home-directory-image-turn",
        ),
    ),
)
def test_a_codex_turn_home_that_fails_is_refused_unchained_and_leaves_nothing(
    tmp_path: Path, monkeypatch, codex_login_source: Path, make_the_home_fail,
    start_the_turn, expected_class, assert_the_refusal, while_the_caller_handles_an_exception,
) -> None:
    turns = _an_empty_working_root(tmp_path)
    make_the_home_fail(monkeypatch, codex_login_source)

    if while_the_caller_handles_an_exception:
        try:
            raise RuntimeError("the caller's own failure")
        except RuntimeError:
            refusal = _refusal_of(start_the_turn, expected_class)
    else:
        refusal = _refusal_of(start_the_turn, expected_class)

    assert_the_refusal(refusal)
    assert refusal.__cause__ is None
    assert refusal.__context__ is None
    assert list(turns.iterdir()) == []


def _refusal_of(start_the_turn, expected_class: type[BaseException]) -> BaseException:
    with pytest.raises(expected_class) as raised:
        start_the_turn()
    return raised.value


class _RemovalThatFailsOnce:
    """Refuses both removal tries of the next home's first removal attempt."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._remove_tree = process.shutil.rmtree
        self._refusals_left = 2
        monkeypatch.setattr(process.shutil, "rmtree", self._rmtree)

    def _rmtree(self, path) -> None:
        if self._refusals_left:
            self._refusals_left -= 1
            raise PermissionError(errno.EACCES, "denied", "secret-looking-name")
        self._remove_tree(path)


def _the_tool_turn_is_closed_and_its_home_stays_owned(turns: Path) -> None:
    transport = _transport()

    asyncio.run(transport.aclose())

    assert len(list(turns.iterdir())) == 1
    del transport


def _the_image_turn_returns_its_png(turns: Path) -> None:
    assert _generate_image().startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "run_the_turn",
    (_the_tool_turn_is_closed_and_its_home_stays_owned, _the_image_turn_returns_its_png),
    ids=("tool-turn", "image-turn"),
)
def test_a_failed_codex_home_removal_never_changes_the_turn(
    tmp_path: Path, monkeypatch, caplog, run_the_turn,
) -> None:
    turns = _an_empty_working_root(tmp_path)
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _generated_png_runner())
    _RemovalThatFailsOnce(monkeypatch)
    caplog.set_level(logging.ERROR, logger=process.__name__)

    run_the_turn(turns)

    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "private credential home removal failed (PermissionError)"
    gc.collect()
    assert list(turns.iterdir()) == []


def _a_login_through_a_dev_null_device(_directory: Path) -> Path:
    return Path("/dev/null")


_SOURCES_A_TURN_REFUSES = pytest.mark.parametrize(
    "make_source",
    (
        a_symlink_to_a_login,
        a_dangling_symlink,
        a_usable_login_over_the_bound,
        a_fifo,
        a_directory,
        _a_login_through_a_dev_null_device,
        a_path_through_a_regular_file,
    ),
    ids=(
        "symlink", "dangling-symlink", "over-the-bound", "fifo", "directory", "device", "enotdir",
    ),
)


@pytest.mark.parametrize(
    "turn", (_the_tool_turn_refusal, _the_image_turn_refusal), ids=("tool-turn", "image-turn"),
)
@_SOURCES_A_TURN_REFUSES
def test_codex_turns_refuse_a_login_that_is_not_a_plain_file(
    tmp_path: Path, monkeypatch, make_source, turn,
) -> None:
    turns = tmp_path / "turns"
    turns.mkdir()
    sources = tmp_path / "sources"
    sources.mkdir()
    override_provider_runtime(
        codex_cli_auth_file=make_source(sources), cli_working_directory_root=turns,
    )
    children: list = []
    monkeypatch.setattr(
        codex_protocol, "run_cli_bounded", _runner([], _outcome(), children),
    )

    refusal = call_within_deadline(turn)

    assert children == []
    assert list(turns.iterdir()) == []
    assert refusal.__cause__ is None
    assert refusal.__context__ is None
    if isinstance(refusal, ProviderUnavailableError):
        assert refusal.reason.code is SafeRouteReasonCode.CLI_AUTH_REJECTED


@pytest.mark.parametrize(
    "turn", (_the_tool_turn_refusal, _the_image_turn_refusal), ids=("tool-turn", "image-turn"),
)
def test_a_failed_turn_copy_keeps_no_credential_in_its_error(
    tmp_path: Path, monkeypatch, codex_login_source: Path, turn,
) -> None:
    turns = tmp_path / "turns"
    turns.mkdir()
    override_provider_runtime(
        codex_cli_auth_file=codex_login_source, cli_working_directory_root=turns,
    )
    children: list = []
    monkeypatch.setattr(
        codex_protocol, "run_cli_bounded", _runner([], _outcome(), children),
    )
    open_descriptors = len(os.listdir("/proc/self/fd"))

    gc.collect()
    gc.disable()
    try:
        with a_credential_destination_that_fails("write"):
            refusal = turn()
        frames_left_holding_it = live_frames_holding(CANARY_ACCESS_TOKEN)
    finally:
        gc.enable()

    assert refusal.__cause__ is None
    assert refusal.__context__ is None
    assert not traceback_frames_hold(refusal, CANARY_ACCESS_TOKEN)
    assert not any(
        CANARY_ACCESS_TOKEN in "".join(traceback.format_exception(link))
        for link in exception_chain(refusal)
    )
    assert not frames_left_holding_it
    assert children == []
    assert list(turns.iterdir()) == []
    assert len(os.listdir("/proc/self/fd")) == open_descriptors


@dataclass(frozen=True)
class _TurnHomes:
    error: BaseException | None
    at_return: list[str]
    after_close: list[str]


def _homes_in(working_root: Path) -> list[str]:
    return sorted(entry.name for entry in working_root.iterdir())


def _run_the_image_turn(monkeypatch, frame: _ImageTurnFrame, seconds: float) -> _TurnHomes:
    turn = frame.drive(seconds)
    homes = _homes_in(frame.working_root)
    return _TurnHomes(turn.error, homes, homes)


def _run_the_tool_turn(monkeypatch, frame: _ImageTurnFrame, seconds: float) -> _TurnHomes:
    monkeypatch.setattr(codex_transport, "TURN_TIMEOUT_SECONDS", seconds)

    async def stream_then_close() -> _TurnHomes:
        transport = _transport()
        error = None
        try:
            await asyncio.wait_for(_collect_tool_responses(transport), timeout=_HANG_BOUND_SECONDS)
        except Exception as raised:
            error = raised
        at_return = _homes_in(frame.working_root)
        await transport.aclose()
        return _TurnHomes(error, at_return, _homes_in(frame.working_root))

    return asyncio.run(stream_then_close())


async def _collect_tool_responses(transport) -> list[object]:
    return [item async for item in transport.stream(InitialTurn("system", []))]


_BOTH_CODEX_TURNS = pytest.mark.parametrize(
    ("run_the_turn", "home_waits_for_close"),
    ((_run_the_image_turn, False), (_run_the_tool_turn, True)),
    ids=("image-turn", "tool-turn"),
)


def _hold_until_opened(monkeypatch, module, name: str, *, then=None) -> Callable[[], None]:
    """Make ``module.name`` wait for the returned gate, then run ``then`` or the real function."""
    gate = threading.Event()
    real = getattr(module, name)

    def held(*arguments, **keywords):
        gate.wait(timeout=_HANG_BOUND_SECONDS)
        return (then or real)(*arguments, **keywords)

    monkeypatch.setattr(module, name, held)
    return gate.set


def _a_child_that_exits(monkeypatch) -> Callable[[], None]:
    _the_child_plays(monkeypatch, ":")
    return lambda: None


def _a_reap_that_outlasts_the_settle_instant(monkeypatch) -> Callable[[], None]:
    _settle_within_a_few_hundredths(monkeypatch)
    _the_child_plays(monkeypatch, "exec sleep 10")
    return _hold_until_opened(monkeypatch, process, "_reap_process_group")


def _a_zombie_child(monkeypatch) -> Callable[[], None]:
    gate = threading.Event()
    real_background_reap = process._reap_in_background

    def reap_once_opened(child, on_reaped) -> None:
        gate.wait(timeout=_HANG_BOUND_SECONDS)
        os.killpg(child.pid, signal.SIGKILL)
        real_background_reap(child, on_reaped)

    monkeypatch.setattr(process, "_reap_process_group", lambda _child: True)
    monkeypatch.setattr(process, "_reap_in_background", reap_once_opened)
    _the_child_plays(monkeypatch, "exec sleep 10")
    return gate.set


def _a_spawn_held_past_the_deadline(monkeypatch) -> Callable[[], None]:
    _the_child_plays(monkeypatch, ":")
    return _hold_until_opened(monkeypatch, process, "open_pipes")


def _a_spawn_that_raises(monkeypatch) -> Callable[[], None]:
    monkeypatch.setattr(process, "open_pipes", Mock(side_effect=OSError(errno.ENOENT, "gone")))
    return lambda: None


def _a_spawn_held_past_the_deadline_that_then_raises(monkeypatch) -> Callable[[], None]:
    return _hold_until_opened(
        monkeypatch, process, "open_pipes", then=Mock(side_effect=OSError(errno.ENOENT, "gone")),
    )


@_BOTH_CODEX_TURNS
@pytest.mark.parametrize(
    ("arrange", "child_outlives_the_turn"),
    (
        (_a_child_that_exits, False),
        (_a_reap_that_outlasts_the_settle_instant, True),
        (_a_zombie_child, True),
        (_a_spawn_held_past_the_deadline, True),
        (_a_spawn_that_raises, False),
        (_a_spawn_held_past_the_deadline_that_then_raises, True),
    ),
    ids=("normal", "reap-overran", "zombie", "late-spawn", "spawn-raises", "late-spawn-raises"),
)
def test_a_codex_turn_home_waits_for_its_child(
    monkeypatch, image_turn, run_the_turn, home_waits_for_close, arrange,
    child_outlives_the_turn,
) -> None:
    releases = _HomeReleases(monkeypatch)
    open_gate = arrange(monkeypatch)
    try:
        turn = run_the_turn(monkeypatch, image_turn, _TURN_SECONDS)
    finally:
        open_gate()

    assert bool(turn.at_return) == (child_outlives_the_turn or home_waits_for_close)
    assert bool(turn.after_close) == child_outlives_the_turn
    releases.wait_for_one()
    assert releases.token_releases == 1
    assert _homes_in(image_turn.working_root) == []
    assert image_turn.process_pool.reservation_count() == 0
    image_turn.watch.assert_nothing_leaked()


def _hold_the_reaps_once_armed(monkeypatch) -> tuple[Callable[[], None], Callable[[], None]]:
    armed, opened = threading.Event(), threading.Event()
    real = process._reap_process_group

    def held(*arguments, **keywords):
        if armed.is_set():
            opened.wait(timeout=_HANG_BOUND_SECONDS)
        return real(*arguments, **keywords)

    monkeypatch.setattr(process, "_reap_process_group", held)
    return armed.set, opened.set


def test_a_tool_turn_home_outlives_its_last_round(monkeypatch, image_turn) -> None:
    releases = _HomeReleases(monkeypatch)
    monkeypatch.setattr(codex_transport, "TURN_TIMEOUT_SECONDS", _TURN_SECONDS)
    children = iter((shell_child("-c", ":"), shell_child("-c", "exec sleep 10")))
    monkeypatch.setattr(codex_transport, "codex_child_process", lambda *_a, **_k: next(children))
    _settle_within_a_few_hundredths(monkeypatch)
    hold_the_next_reap, release_the_reap = _hold_the_reaps_once_armed(monkeypatch)

    async def two_rounds_then_close() -> tuple[list[str], list[str]]:
        transport = _transport()
        for round_number in (1, 2):
            responses = asyncio.wait_for(
                _collect_tool_responses(transport), timeout=_HANG_BOUND_SECONDS,
            )
            with pytest.raises(ProviderUnavailableError):
                await responses
            if round_number == 1:
                await asyncio.to_thread(releases.wait_for_one)
                hold_the_next_reap()
        after_second_round = _homes_in(image_turn.working_root)
        await transport.aclose()
        return after_second_round, _homes_in(image_turn.working_root)

    try:
        after_second_round, after_close = asyncio.run(two_rounds_then_close())
    finally:
        release_the_reap()

    assert len(after_second_round) == 1
    assert after_close == after_second_round
    releases.wait_for_one()
    assert _homes_in(image_turn.working_root) == []
    assert releases.token_releases == 2
    assert image_turn.process_pool.reservation_count() == 0
    image_turn.watch.assert_nothing_leaked()


@pytest.mark.parametrize(
    ("run_the_turn", "home_waits_for_close", "expected_error", "expected_reason"),
    (
        (_run_the_image_turn, False, codex_image.CodexImageTimeoutError, None),
        (
            _run_the_tool_turn, True, ProviderUnavailableError,
            normalize_route_failure(SafeRouteReasonCode.CLI_PROTOCOL_ERROR),
        ),
    ),
    ids=("image-turn", "tool-turn"),
)
def test_a_failed_removal_on_the_late_path_never_changes_the_turn(
    monkeypatch, image_turn, caplog, run_the_turn, home_waits_for_close,
    expected_error, expected_reason,
) -> None:
    releases = _HomeReleases(monkeypatch)
    open_gate = _a_reap_that_outlasts_the_settle_instant(monkeypatch)
    _RemovalThatFailsOnce(monkeypatch)
    caplog.set_level(logging.ERROR, logger=process.__name__)
    try:
        turn = run_the_turn(monkeypatch, image_turn, _TURN_SECONDS)
    finally:
        open_gate()
    releases.wait_for_one()

    assert type(turn.error) is expected_error
    assert getattr(turn.error, "reason", None) == expected_reason
    assert [
        record.getMessage() for record in caplog.records if record.name == process.__name__
    ] == ["private credential home removal failed (PermissionError)"]
    assert len(_homes_in(image_turn.working_root)) == 1
    image_turn.watch.assert_nothing_leaked(turn.error)
    del turn
    gc.collect()
    assert _homes_in(image_turn.working_root) == []


@pytest.mark.parametrize(
    "runner_error",
    (OSError, BaseException, KeyboardInterrupt, SystemExit),
    ids=("os-error", "base-exception", "keyboard-interrupt", "system-exit"),
)
def test_a_tool_round_that_raises_ends_the_turn_and_names_only_its_class(
    monkeypatch, image_turn, runner_error: type[BaseException],
) -> None:
    monkeypatch.setattr(
        codex_protocol, "run_cli_bounded", Mock(side_effect=runner_error(A_RUNNER_FAILURE_CANARY)),
    )
    started_at = time.monotonic()

    turn = _run_the_tool_turn(monkeypatch, image_turn, _HANG_BOUND_SECONDS)

    assert isinstance(turn.error, ProviderUnavailableError)
    assert turn.error.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
    assert time.monotonic() - started_at < 1
    assert image_turn.watch.reported_steps() == [f"run {runner_error.__name__}"]
    image_turn.watch.assert_nothing_leaked(turn.error)


@pytest.mark.parametrize("pool_is_saturated", (False, True), ids=("pool-free", "pool-saturated"))
def test_a_tool_turn_without_a_binary_reserves_nothing(
    monkeypatch, image_turn, caplog, pool_is_saturated: bool,
) -> None:
    override_provider_runtime(codex_cli_binary="no-such-codex-binary-7c1d")
    runner = Mock()
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", runner)
    held_slots = [image_turn.process_pool.reserve(CodexProcessKind.TEXT) for _ in range(8)]
    if not pool_is_saturated:
        image_turn.process_pool.abandon_unspawned(held_slots.pop())
    expected_holdings = len(held_slots)

    turn = _run_the_tool_turn(monkeypatch, image_turn, _TURN_SECONDS)

    assert isinstance(turn.error, ProviderUnavailableError)
    assert turn.error.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
    assert turn.error.__cause__ is None
    assert turn.error.__context__ is None
    assert "Codex CLI failed" not in caplog.text
    assert image_turn.process_pool.reservation_count() == expected_holdings
    assert turn.after_close == []
    runner.assert_not_called()
    image_turn.watch.assert_nothing_leaked(turn.error)


def test_a_tool_round_whose_worker_cannot_start_releases_its_home(
    monkeypatch, image_turn,
) -> None:
    releases = _HomeReleases(monkeypatch)
    fail_to_start_the_runner_thread(monkeypatch, SystemExit(A_RUNNER_FAILURE_CANARY))
    _the_child_plays(monkeypatch, ":")

    turn = _run_the_tool_turn(monkeypatch, image_turn, _TURN_SECONDS)

    assert isinstance(turn.error, ProviderUnavailableError)
    releases.wait_for_one()
    assert releases.token_releases == 1
    assert turn.after_close == []
    assert image_turn.process_pool.reservation_count() == 0
    assert image_turn.watch.reported_steps() == ["start SystemExit", "run SystemExit"]
    image_turn.watch.assert_nothing_leaked(turn.error)


@pytest.mark.parametrize(
    ("run_the_turn", "slot_kind", "refusal_is_expected"),
    (
        (
            _run_the_image_turn, CodexProcessKind.IMAGE,
            lambda error: type(error) is CodexProcessPoolSaturatedError,
        ),
        (
            _run_the_tool_turn, CodexProcessKind.TEXT,
            lambda error: (
                isinstance(error, ProviderUnavailableError)
                and error.reason.code is SafeRouteReasonCode.CLI_CAPACITY_EXHAUSTED
            ),
        ),
    ),
    ids=("image-turn", "tool-turn"),
)
def test_a_saturated_pool_returns_the_turns_home_token(
    monkeypatch, image_turn, run_the_turn, slot_kind, refusal_is_expected,
) -> None:
    pool = CodexProcessPool(maximum_processes=1, maximum_image_runs=1)
    use_codex_process_pool(monkeypatch, pool)
    pool.reserve(slot_kind)
    runner = Mock()
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", runner)
    _the_child_plays(monkeypatch, ":")

    turn = run_the_turn(monkeypatch, image_turn, _TURN_SECONDS)

    assert refusal_is_expected(turn.error)
    runner.assert_not_called()
    assert pool.reservation_count() == 1
    assert turn.after_close == []
    image_turn.watch.assert_nothing_leaked(turn.error)


@_BOTH_CODEX_TURNS
def test_a_refused_home_reservation_reserves_no_slot(
    monkeypatch, image_turn, run_the_turn, home_waits_for_close,
) -> None:
    refusal = RuntimeError("private credential home is closing")
    monkeypatch.setattr(process.PrivateCredentialHome, "reserve", Mock(side_effect=refusal))
    runner = Mock()
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", runner)
    _the_child_plays(monkeypatch, ":")

    turn = run_the_turn(monkeypatch, image_turn, _TURN_SECONDS)

    assert turn.error is refusal
    runner.assert_not_called()
    assert image_turn.process_pool.reservation_count() == 0
    assert turn.after_close == []
    image_turn.watch.assert_nothing_leaked(turn.error)


@pytest.mark.parametrize("home_release_raises", (True, False), ids=("raises", "releases"))
def test_the_turn_release_frees_the_slot_even_when_the_home_release_raises(
    home_release_raises: bool,
) -> None:
    slot_releases: list[str] = []

    def release_the_home() -> None:
        if home_release_raises:
            raise RuntimeError(A_RUNNER_FAILURE_CANARY)

    home_reservation = SimpleNamespace(release=release_the_home)

    if home_release_raises:
        with pytest.raises(RuntimeError):
            codex_protocol.release_turn_reservations(
                home_reservation, lambda: slot_releases.append("slot"),
            )
    else:
        codex_protocol.release_turn_reservations(
            home_reservation, lambda: slot_releases.append("slot"),
        )

    assert slot_releases == ["slot"]


def test_a_tool_consumer_ends_at_the_settle_instant(monkeypatch, image_turn) -> None:
    _settle_within_a_few_hundredths(monkeypatch)
    monkeypatch.setattr(codex_transport, "TURN_TIMEOUT_SECONDS", 1.0)
    override_provider_runtime(codex_cli_binary="/bin/true")
    monkeypatch.setattr(
        process.CliLineChannel, "_close", Mock(side_effect=RuntimeError(A_RUNNER_FAILURE_CANARY)),
    )
    built_at = time.monotonic()
    transport = _transport()
    built_by = time.monotonic()

    async def stream_and_close() -> BaseException:
        responses = asyncio.wait_for(_collect_tool_responses(transport), timeout=10)
        with pytest.raises(ProviderUnavailableError) as raised:
            await responses
        await transport.aclose()
        return raised.value

    failed = asyncio.run(stream_and_close())
    ended_at = time.monotonic()

    assert failed.reason.code is SafeRouteReasonCode.CLI_PROTOCOL_ERROR
    assert process.cli_settle_deadline(built_at + 1.0) <= ended_at
    assert ended_at <= process.cli_settle_deadline(built_by + 1.0) + _ALLOWANCE_SECONDS
    assert image_turn.watch.reported_steps() == ["close RuntimeError"]
    image_turn.watch.assert_nothing_leaked(failed)
