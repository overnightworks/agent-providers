"""Codex subscription CLI co-writer transport."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from provider_test_support import (
    COWRITER_TOOL_CATALOG,
    override_provider_runtime,
    override_turn_runtime,
    use_codex_process_pool,
)

from agent_providers.codex import image as codex_image
from agent_providers.codex import protocol as codex_protocol
from agent_providers.codex import transport as codex_transport
from agent_providers.codex.pool import CodexProcessKind, CodexProcessPool
from agent_providers.errors import (
    CodexProcessPoolSaturatedError,
    ProviderUnavailableError,
    SafeRouteReasonCode,
)
from agent_providers.events import AssistantTextEvent, FinalEvent, ToolCallEvent
from agent_providers.images import ImagePolicy
from agent_providers.process import CliRunOutcome, CliRunReason
from agent_providers.tool_loop import (
    InitialTurn,
    ToolCallBatch,
    ToolOutcome,
    ToolResult,
    ToolResultBatch,
    stream_tool_loop,
)

A_TOOL_FAILURE_MESSAGE = "Co-Writer tool failed."
A_COVER_POLICY = ImagePolicy(
    maximum_source_bytes=8 * 1024 * 1024,
    maximum_pixels=20_000_000,
    output_edge_pixels=1024,
    output_format="PNG",
    output_signature=b"\x89PNG\r\n\x1a\n",
)


def _transport() -> codex_transport.CodexCliToolTransport:
    """Build the transport under test with songmaker's own tool catalog."""
    return codex_transport.CodexCliToolTransport(
        model="codex-test", catalog=COWRITER_TOOL_CATALOG,
    )

_REDACTED_CODEX_LOGIN = {
    "auth_mode": "chatgpt",
    "OPENAI_API_KEY": None,
    "last_refresh": "2026-09-04T19:20:00Z",
    "tokens": {
        "id_token": "id-token",
        "access_token": "access-token",
        "account_id": "account",
        "refresh_token": "",
    },
}
_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("missing", ("cli", "code_mode_host", "resources"))
def test_cover_image_capability_requires_every_codex_mount(
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

    assert codex_image.codex_cover_image_capability_is_available()

    if missing == "cli":
        cli.unlink()
    elif missing == "code_mode_host":
        code_mode_host.unlink()
    else:
        resources.rmdir()

    assert not codex_image.codex_cover_image_capability_is_available()


def test_cover_image_capability_requires_the_installed_image_encoder(
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

    assert not codex_image.codex_cover_image_capability_is_available()


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
        codex_image.generate_codex_cover_image(
            "prompt", policy=A_COVER_POLICY, deadline=10_000_000,
        )

    assert spawns == []


@pytest.fixture(autouse=True)
def codex_login_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    mirror = tmp_path / "auth.json"
    mirror.write_text(json.dumps(_REDACTED_CODEX_LOGIN))
    override_provider_runtime(codex_cli_auth_file=mirror)
    process_pool = CodexProcessPool(maximum_processes=8, maximum_image_runs=1)
    use_codex_process_pool(monkeypatch, process_pool)
    return mirror


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


def _fixture_lines(name: str) -> list[bytes]:
    return [line.encode() + b"\n" for line in (_FIXTURES / name).read_text().splitlines()]


_PNG_FIXTURE_SIZE = (300, 100)
_PNG_FIXTURE_PIXELS = _PNG_FIXTURE_SIZE[0] * _PNG_FIXTURE_SIZE[1]


def test_image_tool_block_reasons_are_closed() -> None:
    assert {reason.value for reason in codex_image.ImageToolBlockedReason} == {
        "unexpected_event",
        "blocked_item",
        "unexpected_item",
        "bootstrap_command_mismatch",
        "bootstrap_cwd_present",
        "bootstrap_sequence_invalid",
        "bootstrap_missing",
    }


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", _PNG_FIXTURE_SIZE, (20, 80, 160)).save(output, format="PNG")
    return output.getvalue()


def _image_event_stream(codex_home: Path) -> str:
    return (_FIXTURES / "codex-imagegen-real-stream.jsonl").read_text().replace(
        "{CODEX_HOME}", str(codex_home.resolve()),
    )


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


@pytest.mark.acceptance("ACC-COWRITER-12")
def test_codex_tool_transport_uses_an_empty_private_work_directory_on_resume(monkeypatch) -> None:
    calls: list = []
    prompts: list[bytes] = []
    thread_id = "52700000-0000-4000-8000-000000000000"
    rounds = iter([
        [
            json.dumps({"type": "thread.started", "thread_id": thread_id}).encode() + b"\n",
            b'{"type":"item.completed","item":{"type":"agent_message","text":"<songmaker_tool_call>\\n{\\"name\\":\\"list_songs\\",\\"arguments\\":{}}\\n</songmaker_tool_call>"}}\n',
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
        lambda _name, _arguments: ToolOutcome('{"songs":[]}', False),
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
        b'<songmaker_tool_result>\n{"songs":[]}\n</songmaker_tool_result>',
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


def test_codex_tool_transport_rejects_a_multi_result_batch_without_a_resume(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _runner([
        b'{"type":"thread.started","thread_id":"52700000-0000-4000-8000-000000000000"}\n',
        b'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n',
        b'{"type":"turn.completed","usage":{}}\n',
    ], _outcome(), calls))
    transport = _transport()

    async def reject_batch() -> None:
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

    asyncio.run(reject_batch())
    assert len(calls) == 1


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
    lyrics = "private lyrics"
    song_id = "song-private"
    protocol = (
        "<songmaker_tool_call>\n"
        f'{{"name":"update_song_lyrics","arguments":{{"song_id":"{song_id}","lyrics":"{lyrics}"}}}}\n'
        "</songmaker_tool_call>"
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
    for forbidden in (lyrics, song_id, protocol, "private stderr", "private.jsonl"):
        assert forbidden not in caplog.text


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

    codex_protocol.run_reserved_codex_cli(
        reservation,
        ("codex", "exec"),
        stdin_payload=b"prompt",
        read="all",
        deadline=10_000_000,
    )

    with pytest.raises(CodexProcessPoolSaturatedError):
        process_pool.reserve(CodexProcessKind.TEXT)
    callbacks["on_spawned"](41)
    callbacks["on_reaped"](41, True)
    assert process_pool.reservation_count() == 0


def _generated_png_runner() -> object:
    """A Codex run that leaves exactly one generated PNG in its private home."""
    def run_cli_bounded(child, **kwargs):
        codex_home = Path(child.environment["CODEX_HOME"])
        artifact = codex_home / "generated_images" / "thread" / "cover.png"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(_png_bytes())
        outcome = _outcome(stdout=_image_event_stream(codex_home))
        kwargs["on_spawned"](1)
        kwargs["on_reaped"](1, False)
        return outcome

    return run_cli_bounded


def _host_image_policy(
    *,
    maximum_source_bytes: int = 8 * 1024 * 1024,
    maximum_pixels: int = 20_000_000,
    output_edge_pixels: int = 256,
) -> ImagePolicy:
    """A host's own image bounds, stated without songmaker's cover constants."""
    return ImagePolicy(
        maximum_source_bytes=maximum_source_bytes,
        maximum_pixels=maximum_pixels,
        output_edge_pixels=output_edge_pixels,
        output_format="PNG",
        output_signature=b"\x89PNG\r\n\x1a\n",
    )


def test_codex_cover_image_accepts_the_recorded_imagegen_stream(monkeypatch) -> None:
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _generated_png_runner())

    assert codex_image.generate_codex_cover_image(
        "prompt", policy=A_COVER_POLICY, deadline=10_000_000,
    ).startswith(b"\x89PNG")


def test_a_generated_image_is_returned_in_the_shape_the_host_asked_for(monkeypatch) -> None:
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _generated_png_runner())

    payload = codex_image.generate_codex_cover_image(
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
        codex_image.generate_codex_cover_image(
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
                stdout=(_FIXTURES / "codex-cover-quota-exceeded.jsonl").read_text(),
            ),
            codex_image.CodexImageQuotaError,
            "usage limit",
            "Sep 7th, 2026 8:45 PM",
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
    ),
    ids=(
        "login",
        "timeout",
        "nonzero-exit",
        "turn-failed-names-auth",
        "turn-failed-names-usage-limit",
        "turn-failed-names-generic-message",
    ),
)
def test_codex_cover_image_names_terminal_cli_failures(
    monkeypatch,
    outcome: CliRunOutcome,
    expected_error: type[Exception],
    expected_message: str | None,
    expected_retry_at: str | None,
) -> None:
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _image_runner(outcome))

    with pytest.raises(expected_error) as raised:
        codex_image.generate_codex_cover_image("prompt", policy=A_COVER_POLICY, deadline=10_000_000)

    if expected_message is not None:
        assert expected_message in str(raised.value)
    if expected_retry_at is not None:
        assert raised.value.retry_at == expected_retry_at


@pytest.mark.parametrize(
    ("artifact_count", "expected_error"),
    (
        (0, codex_image.CodexImageNotCreatedError),
        (
            2,
            codex_image.CodexImageArtifactError,
        ),
    ),
    ids=("missing-png", "ambiguous-pngs"),
)
def test_codex_cover_image_rejects_missing_or_ambiguous_generated_artifacts(
    monkeypatch,
    artifact_count: int,
    expected_error: type[Exception],
) -> None:
    def run_cli_bounded(child, **kwargs):
        codex_home = Path(child.environment["CODEX_HOME"])
        for index in range(artifact_count):
            artifact = codex_home / "generated_images" / f"cover-{index}.png"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(_png_bytes())
        outcome = _outcome(stdout=_image_event_stream(codex_home))
        kwargs["on_spawned"](1)
        kwargs["on_reaped"](1, False)
        return outcome

    monkeypatch.setattr(codex_protocol, "run_cli_bounded", run_cli_bounded)

    with pytest.raises(expected_error):
        codex_image.generate_codex_cover_image("prompt", policy=A_COVER_POLICY, deadline=10_000_000)


def test_codex_cover_image_rejects_the_recorded_no_image_turn(monkeypatch, caplog) -> None:
    outcome = _outcome(stdout=(_FIXTURES / "codex-cover-no-image-events.jsonl").read_text())
    monkeypatch.setattr(codex_protocol, "run_cli_bounded", _image_runner(outcome))

    caplog.set_level("WARNING", logger="agent_providers.codex.image")
    with pytest.raises(codex_image.ImageToolBlockedError) as raised:
        codex_image.generate_codex_cover_image("prompt", policy=A_COVER_POLICY, deadline=10_000_000)

    assert raised.value.reason is codex_image.ImageToolBlockedReason.BOOTSTRAP_MISSING
    assert caplog.text.count("Codex image tool rejected") == 1
    assert "bootstrap_missing" in caplog.text
    assert "prompt" not in caplog.text


@pytest.mark.parametrize(
    ("scenario", "expected_reason", "rejected_while_streaming"),
    (
        ("unexpected-event", codex_image.ImageToolBlockedReason.UNEXPECTED_EVENT, True),
        ("blocked-item", codex_image.ImageToolBlockedReason.BLOCKED_ITEM, True),
        ("unexpected-item", codex_image.ImageToolBlockedReason.UNEXPECTED_ITEM, True),
        (
            "bootstrap-command-mismatch",
            codex_image.ImageToolBlockedReason.BOOTSTRAP_COMMAND_MISMATCH,
            True,
        ),
        (
            "bootstrap-cwd-present",
            codex_image.ImageToolBlockedReason.BOOTSTRAP_CWD_PRESENT,
            True,
        ),
        (
            "bootstrap-sequence-invalid",
            codex_image.ImageToolBlockedReason.BOOTSTRAP_SEQUENCE_INVALID,
            True,
        ),
        ("bootstrap-missing", codex_image.ImageToolBlockedReason.BOOTSTRAP_MISSING, False),
    ),
)
def test_codex_cover_image_reports_every_closed_block_reason_without_private_input(
    monkeypatch,
    caplog,
    scenario: str,
    expected_reason: codex_image.ImageToolBlockedReason,
    rejected_while_streaming: bool,
) -> None:
    prompt_marker = "private-prompt-marker"
    event_marker = "private-event-marker"
    runner_state = {"aborted": False, "reaped": False}

    def run_cli_bounded(child, **kwargs):
        codex_home = Path(child.environment["CODEX_HOME"])
        expected_command = codex_image._expected_image_skill_command(codex_home)
        command_item = {
            "type": "command_execution",
            "id": "bootstrap",
            "command": expected_command,
            "cwd": None,
            "status": "in_progress",
            "exit_code": None,
            "private": event_marker,
        }
        events = {
            "unexpected-event": {"type": event_marker},
            "blocked-item": {
                "type": "item.started",
                "item": {"type": "web_search", "private": event_marker},
            },
            "unexpected-item": {
                "type": "item.started",
                "item": {"type": "future_item", "private": event_marker},
            },
            "bootstrap-command-mismatch": {
                "type": "item.started",
                "item": {**command_item, "command": event_marker},
            },
            "bootstrap-cwd-present": {
                "type": "item.started",
                "item": {**command_item, "cwd": event_marker},
            },
            "bootstrap-sequence-invalid": {
                "type": "item.started",
                "item": {**command_item, "status": "completed"},
            },
            "bootstrap-missing": {
                "type": "turn.completed",
                "usage": {"private": event_marker},
            },
        }
        line = json.dumps(events[scenario]).encode() + b"\n"
        channel = kwargs["stdout_line_channel"]
        abort_observed = threading.Event()
        request_abort = channel.request_abort

        def observe_abort() -> None:
            request_abort()
            abort_observed.set()

        channel.request_abort = observe_abort
        kwargs["on_spawned"](2468)
        assert channel._send(line)
        if rejected_while_streaming:
            assert abort_observed.wait(timeout=1)
            assert channel.abort_requested()
            runner_state["aborted"] = True
        outcome = _outcome(stdout=line.decode())
        kwargs["on_reaped"](2468, False)
        runner_state["reaped"] = True
        return outcome

    monkeypatch.setattr(codex_protocol, "run_cli_bounded", run_cli_bounded)
    caplog.set_level("WARNING", logger="agent_providers.codex.image")

    with pytest.raises(codex_image.ImageToolBlockedError) as raised:
        codex_image.generate_codex_cover_image(
            prompt_marker,
            policy=A_COVER_POLICY,
            deadline=10_000_000,
        )

    assert raised.value.reason is expected_reason
    assert caplog.text.count("Codex image tool rejected") == 1
    assert expected_reason.value in caplog.text
    assert prompt_marker not in caplog.text
    assert event_marker not in caplog.text
    assert runner_state == {
        "aborted": rejected_while_streaming,
        "reaped": True,
    }


@pytest.mark.parametrize(
    "document",
    (
        None,
        {**_REDACTED_CODEX_LOGIN, "tokens": {"id_token": "id-token"}},
    ),
    ids=("missing", "incomplete"),
)
def test_codex_public_adapters_reject_unusable_login_mirrors(
    tmp_path: Path,
    document: dict | None,
) -> None:
    auth_file = tmp_path / "invalid-auth.json"
    if document is not None:
        auth_file.write_text(json.dumps(document))
    override_provider_runtime(codex_cli_auth_file=auth_file)

    with pytest.raises(codex_image.CodexImageLoginError):
        codex_image.generate_codex_cover_image("prompt", policy=A_COVER_POLICY, deadline=10_000_000)
    with pytest.raises(ProviderUnavailableError) as raised:
        _transport()

    assert raised.value.reason.code is SafeRouteReasonCode.CLI_AUTH_REJECTED
