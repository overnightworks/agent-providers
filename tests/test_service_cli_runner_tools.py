"""The CLI runner carries a run's tool calls through the relay under the app's own names.

Every row serves the real app on a loopback socket, runs a fake Claude as a real child that
speaks to the run's relay, and answers its calls from a TLS fake app signed by a test CA. The
rows inject ``Runners(cli_offers_tools=True)`` because production keeps tool apps on
``provider_unavailable`` until A1b-11b; the entry row goes through ``cli.main`` instead.
"""

from __future__ import annotations

import dataclasses
import gc
import json
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from anchored_support import ChildCleanup
from cli_tools_support import (
    ChildCapture,
    ClaudeCall,
    ToolApp,
    a_relay_calling_claude,
    a_tool_app,
)
from mcp_app_fake import (
    RecordedRequest,
    Responder,
    TestAuthority,
    rpc_result,
)
from provider_test_support import (
    INIT_LINE,
    SUCCESS_LINE,
    TEXT_LINE,
    a_fake_claude,
    held_claude_slots,
    line,
    own_real_children,
)
from service_support import (
    PINNED_CLAUDE_VERSION,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    served,
)
from test_service_cli_runner import (
    MINT,
    PLAIN_APP,
    a_config,
    drive_over_http,
    headers,
    post_a_run,
    private_root_of,
    status_of,
    trusting,
)

import agent_service.app as app_module
import agent_service.config as config_module
from agent_client.sse import RunStreamDecoder
from agent_client.wire import (
    MAX_EVENT_BYTES,
    Failed,
    FailureCode,
    RunEvent,
    RunState,
    RunStatus,
    Succeeded,
    ToolCall,
    ToolResult,
)
from agent_service.cli import main
from agent_service.cli_runner import CliRunner, mcp_tls_context
from agent_service.config import ServiceConfig
from agent_service.run_liveness import LoopClock
from agent_service.run_tools import RunTools
from agent_service.runners import Runners

SAVE_NOTE = ClaudeCall("save_note", {"text": "la la"})
READ_NOTE = ClaudeCall("read_note")
TOOLS = {"save_note": "side_effecting", "read_note": "read_only"}
SETTLE_SECONDS = 10.0
RELAY_TASK_PREFIXES = ("relay:", "upstream:", "run-tools:")


@pytest.fixture(name="other_authority")
def another_certificate_authority(tmp_path: Path) -> TestAuthority:
    directory = tmp_path / "other-ca"
    directory.mkdir()
    return TestAuthority(directory)


@dataclass(frozen=True)
class ToolRun:
    events: list[RunEvent]
    status: RunStatus
    app: ToolApp
    capture: ChildCapture
    config: ServiceConfig


def wire_shape(events: Sequence[RunEvent]) -> list[tuple[Any, ...]]:
    shape: list[tuple[Any, ...]] = []
    for event in events:
        match event:
            case ToolCall():
                shape.append(("tool_call", event.name, event.tool_use_id, event.effect.value))
            case ToolResult():
                shape.append(("tool_result", event.tool_use_id, event.content, event.is_error))
            case Failed():
                shape.append(("failed", event.code))
            case Succeeded():
                shape.append(("succeeded", event.text))
    return shape


def settled_status(base_url: str, events: Sequence[RunEvent]) -> RunStatus:
    deadline = time.monotonic() + SETTLE_SECONDS
    while (status := status_of(base_url, list(events))).state is RunState.RUNNING:
        assert time.monotonic() < deadline, "the run never ended"
        time.sleep(0.02)
    return status


def a_tool_run(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    *,
    calls: Sequence[ClaudeCall] = (),
    tools: Mapping[str, str] = TOOLS,
    runs: dict[str, object] | None = None,
    overrides: Mapping[str, Responder] | None = None,
    serving_authority: TestAuthority | None = None,
    app_stays_up: bool = True,
    scenario: Callable[[str], list[RunEvent]] = post_a_run,
    **claude_options: Any,
) -> ToolRun:
    """One run of the real runner with its tools on, against a TLS fake app, over HTTP.

    The runner trusts ``authority``; the app is signed by ``serving_authority`` (default the
    same). ``app_stays_up=False`` stops the app before the run, so its origin refuses.
    """
    binary = a_relay_calling_claude(
        tmp_path, version=PINNED_CLAUDE_VERSION, calls=calls, **claude_options
    )
    with ExitStack() as running:
        app = running.enter_context(
            a_tool_app(serving_authority or authority, list(tools), overrides=overrides)
        )
        if not app_stays_up:
            running.close()
        config = a_config(
            tmp_path,
            binary,
            runs=runs,
            songmaker={"tools": dict(tools), "mcp_origin": app.origin},
        )
        runners = Runners(
            cli=CliRunner(
                config, tls_context=mcp_tls_context(authority.ca_file), clock=LoopClock()
            ),
            cli_offers_tools=True,
        )

        def play(base_url: str) -> tuple[list[RunEvent], RunStatus]:
            events = scenario(base_url)
            return events, settled_status(base_url, events)

        events, status = drive_over_http(child_cleanup, monkeypatch, config, play, runners)
    return ToolRun(events, status, app, ChildCapture(tmp_path / "capture"), config)


def ended_as(run: ToolRun) -> tuple[RunState, FailureCode | None]:
    return run.status.state, run.status.failure_code


PROVIDER_ERROR = (RunState.FAILED, FailureCode.PROVIDER_ERROR)
SUCCEEDED = (RunState.SUCCEEDED, None)


def test_a_relay_call_reaches_the_wire_under_the_apps_own_tool_name(
    tmp_path, authority, child_cleanup, monkeypatch
):
    run = a_tool_run(
        tmp_path, authority, child_cleanup, monkeypatch, calls=[SAVE_NOTE], stray_tool_lines=True
    )

    assert wire_shape(run.events) == [
        ("tool_call", "save_note", "call-1", "side_effecting"),
        ("tool_result", "call-1", "save_note", False),
        ("succeeded", "done"),
    ]
    assert [call.message["params"]["arguments"] for call in run.app.calls] == [{"text": "la la"}]


@pytest.mark.parametrize(
    ("call", "effect"),
    [
        pytest.param(READ_NOTE, "read_only", id="read-only"),
        pytest.param(SAVE_NOTE, "side_effecting", id="side-effecting"),
    ],
)
def test_each_calls_effect_follows_the_apps_list(
    tmp_path, authority, child_cleanup, monkeypatch, call, effect
):
    run = a_tool_run(tmp_path, authority, child_cleanup, monkeypatch, calls=[call])

    assert [shape[3] for shape in wire_shape(run.events) if shape[0] == "tool_call"] == [effect]


def a_fake_claude_printing_its_own_tool_lines(tmp_path: Path) -> Path:
    stray_call = line(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}},
                ]
            },
        }
    )
    stray_result = line(
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "x"}]
            },
        }
    )
    return a_fake_claude(
        tmp_path,
        (INIT_LINE, stray_call, stray_result, TEXT_LINE, SUCCESS_LINE),
        version_line=f"{PINNED_CLAUDE_VERSION} (Claude Code)",
    )


@pytest.mark.parametrize("with_relay", [True, False], ids=["with-relay", "without-relay"])
def test_the_clis_own_tool_lines_never_reach_the_wire(
    tmp_path, authority, child_cleanup, monkeypatch, with_relay
):
    if with_relay:
        run = a_tool_run(
            tmp_path,
            authority,
            child_cleanup,
            monkeypatch,
            calls=[SAVE_NOTE],
            stray_tool_lines=True,
        )
        events = run.events
    else:
        config = a_config(tmp_path, a_fake_claude_printing_its_own_tool_lines(tmp_path))
        events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    assert [shape[1] for shape in wire_shape(events) if shape[0] == "tool_call"] == (
        ["save_note"] if with_relay else []
    )
    assert [event.type for event in events if event.type == "tool_result"] == (
        ["tool_result"] if with_relay else []
    )


def test_a_call_past_the_tool_budget_never_reaches_the_app(
    tmp_path, authority, child_cleanup, monkeypatch
):
    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE, SAVE_NOTE],
        runs={"max_tool_calls": 1},
    )

    assert (ended_as(run), len(run.app.calls), len(run.capture.replies)) == (
        (RunState.FAILED, FailureCode.LIMIT_EXCEEDED),
        1,
        1,
    )


def test_a_result_past_the_byte_budget_never_reaches_the_model(
    tmp_path, authority, child_cleanup, monkeypatch
):
    too_big = "r" * 4_000

    def big_result(request: RecordedRequest):
        return rpc_result(request, {"content": [{"type": "text", "text": too_big}]})

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE],
        runs={"max_bytes": 3_000},
        overrides={"tools/call": big_result},
    )

    assert (ended_as(run), len(run.app.calls), run.capture.replies) == (
        (RunState.FAILED, FailureCode.LIMIT_EXCEEDED),
        1,
        [],
    )
    sent = json.dumps([event.model_dump(mode="json") for event in run.events])
    assert sent.count(too_big) == 0


def test_a_call_the_wire_cannot_carry_never_reaches_the_app(
    tmp_path, authority, child_cleanup, monkeypatch
):
    too_wide = ClaudeCall("save_note", {"text": "w" * (MAX_EVENT_BYTES - 150)})

    run = a_tool_run(tmp_path, authority, child_cleanup, monkeypatch, calls=[too_wide])

    assert (ended_as(run), run.app.calls) == (PROVIDER_ERROR, [])


def test_a_capability_never_reaches_the_child_of_a_tool_run(
    tmp_path, authority, child_cleanup, monkeypatch, caplog
):
    caplog.set_level("DEBUG")

    run = a_tool_run(tmp_path, authority, child_cleanup, monkeypatch, calls=[SAVE_NOTE])

    capability, secret = run.app.capability, run.capture.relay_secret
    visible_to_the_service_outputs = "".join(
        [
            json.dumps([event.model_dump(mode="json") for event in run.events]),
            run.status.model_dump_json(),
            caplog.text,
        ]
    )
    assert capability.count(".") == 2
    assert capability not in run.capture.everything_it_was_given
    assert secret not in visible_to_the_service_outputs
    assert capability not in visible_to_the_service_outputs
    holders_of_the_secret = [name for name, text in run.capture.run_files.items() if secret in text]
    assert (holders_of_the_secret, secret in run.capture.text_of("argv")) == (
        ["relay-mcp.json"],
        False,
    )
    assert secret not in run.capture.text_of("environment") + run.capture.stdin


def test_a_capability_reflected_in_an_upstream_result_fails_the_run(
    tmp_path, authority, child_cleanup, monkeypatch, caplog
):
    caplog.set_level("DEBUG")

    def reflects_the_capability(request: RecordedRequest):
        bearer = request.headers["authorization"].removeprefix("Bearer ")
        return rpc_result(request, {"content": [{"type": "text", "text": bearer}]})

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE],
        overrides={"tools/call": reflects_the_capability},
    )

    everything_seen = json.dumps([event.model_dump(mode="json") for event in run.events])
    everything_seen += run.status.model_dump_json() + caplog.text
    assert (ended_as(run), run.capture.replies) == (PROVIDER_ERROR, [])
    assert run.app.capability not in everything_seen
    assert not [shape for shape in wire_shape(run.events) if shape[0] == "tool_result"]


@pytest.mark.parametrize("with_origin", [False, True], ids=["no-origin", "empty-table"])
def test_an_app_without_granted_tools_gets_the_tool_free_run(
    tmp_path, authority, child_cleanup, monkeypatch, with_origin
):
    binary = a_fake_claude(tmp_path, version_line=f"{PINNED_CLAUDE_VERSION} (Claude Code)")
    with a_tool_app(authority) as app:
        songmaker = {"tools": {}, "mcp_origin": app.origin if with_origin else None}
        config = a_config(tmp_path, binary, songmaker=songmaker or PLAIN_APP)
        runners = Runners(
            cli=CliRunner(
                config, tls_context=mcp_tls_context(authority.ca_file), clock=LoopClock()
            ),
            cli_offers_tools=True,
        )

        events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run, runners)

        assert (wire_shape(events), app.fake.accepted_connections) == ([("succeeded", "done")], 0)


@pytest.mark.parametrize("refusal", ["refused-connection", "unknown-ca"])
def test_an_app_that_cannot_be_opened_ends_the_run_before_any_child(
    tmp_path, authority, other_authority, child_cleanup, monkeypatch, refusal
):
    unreachable = refusal == "refused-connection"

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE],
        serving_authority=authority if unreachable else other_authority,
        app_stays_up=not unreachable,
    )

    assert (ended_as(run), run.capture.started, held_claude_slots()) == (PROVIDER_ERROR, False, 0)
    assert list(private_root_of(run.config).iterdir()) == []


def test_a_cli_that_never_connects_the_relay_ends_at_the_start_check(
    tmp_path, authority, child_cleanup, monkeypatch
):
    run = a_tool_run(
        tmp_path, authority, child_cleanup, monkeypatch, calls=[SAVE_NOTE], relay_status="failed"
    )

    assert (ended_as(run), wire_shape(run.events), run.app.calls) == (
        PROVIDER_ERROR,
        [("failed", FailureCode.PROVIDER_ERROR)],
        [],
    )


def test_a_call_to_a_tool_outside_the_apps_grant_ends_the_run_and_never_reaches_the_app(
    tmp_path, authority, child_cleanup, monkeypatch
):
    run = a_tool_run(
        tmp_path, authority, child_cleanup, monkeypatch, calls=[ClaudeCall("delete_everything")]
    )

    assert (ended_as(run), run.app.calls) == (PROVIDER_ERROR, [])


def leave_after_the_first_tool_call(base_url: str) -> list[RunEvent]:
    decoder = RunStreamDecoder()
    seen: list[RunEvent] = []
    with httpx.stream(
        "POST",
        f"{base_url}/runs",
        headers={"Content-Type": "application/json", **headers()},
        content=json.dumps(
            {
                "v": 1,
                "purpose": "songmaker.cowriter",
                "turn": {"system": "s", "messages": [{"role": "user", "text": "a"}]},
            }
        ),
        timeout=SETTLE_SECONDS,
    ) as response:
        for chunk in response.iter_bytes():
            seen += [item for item in decoder.feed(chunk) if not hasattr(item, "kind")]
            if any(event.type == "tool_call" for event in seen):
                break
    return seen


def pending_tool_tasks() -> list[str]:
    gc.collect()
    import asyncio

    return sorted(
        task.get_name()
        for task in (obj for obj in gc.get_objects() if isinstance(obj, asyncio.Task))
        if not task.done() and task.get_name().startswith(RELAY_TASK_PREFIXES)
    )


@pytest.mark.parametrize(
    ("ending", "options"),
    [
        pytest.param("success", {"calls": [SAVE_NOTE]}, id="success"),
        pytest.param("cli-exit", {"calls": [SAVE_NOTE], "exit_code": 1}, id="cli-exit-1"),
        pytest.param(
            "client-leaves",
            {
                "calls": [SAVE_NOTE],
                "linger_seconds": 60.0,
                "scenario": leave_after_the_first_tool_call,
            },
            id="client-leaves",
        ),
        pytest.param(
            "refused-event",
            {"calls": [SAVE_NOTE, SAVE_NOTE], "runs": {"max_tool_calls": 1}},
            id="refused-event",
        ),
    ],
)
def test_every_ending_closes_the_runs_tools_once(
    tmp_path, authority, child_cleanup, monkeypatch, ending, options
):
    closes: list[str] = []
    close = RunTools.close

    async def counted(self, reap):
        closes.append(ending)
        await close(self, reap)

    monkeypatch.setattr(RunTools, "close", counted)

    run = a_tool_run(tmp_path, authority, child_cleanup, monkeypatch, **options)

    import socket

    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", run.capture.relay_port), timeout=SETTLE_SECONDS)
    assert (closes, pending_tool_tasks()) == ([ending], [])


def test_the_mcp_trust_context_ignores_the_environment(
    tmp_path, authority, other_authority, monkeypatch
):
    key_log = tmp_path / "keys.log"
    monkeypatch.setenv("SSL_CERT_FILE", str(other_authority.ca_file))
    monkeypatch.setenv("SSLKEYLOGFILE", str(key_log))

    context = mcp_tls_context(authority.ca_file)

    assert [certificate["subject"] for certificate in context.get_ca_certs()] == [
        ((("commonName", "fake app test CA"),),)
    ]
    assert context.keylog_filename is None


@pytest.mark.parametrize(
    "signed_by_the_configured_ca",
    [pytest.param(True, id="the-configured-ca"), pytest.param(False, id="another-ca")],
)
def test_a_run_trusts_exactly_the_configured_bundle(
    tmp_path,
    authority,
    other_authority,
    child_cleanup,
    monkeypatch,
    logging_state_restored,
    signed_by_the_configured_ca,
):
    binary = a_relay_calling_claude(tmp_path, version=PINNED_CLAUDE_VERSION, calls=[SAVE_NOTE])
    serving_authority = authority if signed_by_the_configured_ca else other_authority
    with a_tool_app(serving_authority, list(TOOLS)) as app:
        songmaker = {"tools": TOOLS, "mcp_origin": app.origin}
        config = trusting(a_config(tmp_path, binary, songmaker=songmaker), authority)
        served_apps = []
        create_app_for_the_test = app_module.create_app

        def create_app_with_tools_on(built: ServiceConfig, **keywords: Any):
            keywords["runners"] = dataclasses.replace(keywords["runners"], cli_offers_tools=True)
            return create_app_for_the_test(built, fetch_key_set=MINT.fetch_key_set, **keywords)

        monkeypatch.setattr(uvicorn, "run", lambda built, **_where: served_apps.append(built))
        monkeypatch.setattr(config_module, "load_config", lambda _path: config)
        monkeypatch.setattr(app_module, "create_app", create_app_with_tools_on)

        def start_and_post() -> RunStatus:
            assert main(["--config", str(tmp_path / "agent-service.toml")]) == 0
            with served(served_apps[0]) as base_url:
                events = post_a_run(base_url)
                return settled_status(base_url, events)

        status = own_real_children(child_cleanup, monkeypatch, start_and_post)

    assert (status.state, status.failure_code) == (
        SUCCEEDED if signed_by_the_configured_ca else PROVIDER_ERROR
    )
