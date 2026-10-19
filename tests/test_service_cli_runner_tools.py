"""The CLI runner carries a run's tool calls through the relay under the app's own names.

Every row serves the real app on a loopback socket, runs a fake Claude as a real child that
speaks to the run's relay, and answers its calls from a TLS fake app signed by a test CA. The
rows inject ``Runners(cli_offers_tools=True)`` because production keeps tool apps on
``provider_unavailable`` until A1b-11b; the entry row goes through ``cli.main`` instead.
"""

from __future__ import annotations

import asyncio
import dataclasses
import gc
import json
import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
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
    HeldRequest,
    HeldRequests,
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
    decoded_events,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    post_run,
    run_body,
    served,
)
from test_service_cli_runner import (
    MINT,
    PLAIN_APP,
    a_config,
    all_children_gone,
    drive_over_http,
    eventually,
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
    ClientLoss,
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
from agent_service.run_events import RUNNER_FAILED_MESSAGE
from agent_service.run_liveness import LoopClock
from agent_service.run_tools import RunTools
from agent_service.runners import Runners
from agent_service.tool_relay import ToolCallBarrier

SAVE_NOTE = ClaudeCall("save_note", {"text": "la la"})
READ_NOTE = ClaudeCall("read_note")
TOOLS = {"save_note": "side_effecting", "read_note": "read_only"}
SETTLE_SECONDS = 10.0
STUCK_APP_BOUND_SECONDS = 30.0
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
    eventually(lambda: status_of(base_url, list(events)).state is not RunState.RUNNING)
    return status_of(base_url, list(events))


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
    before_stop: Callable[[ServiceConfig], None] = lambda _config: None,
    **claude_options: Any,
) -> ToolRun:
    """One run of the real runner with its tools on, against a TLS fake app, over HTTP.

    The runner trusts ``authority``; the app is signed by ``serving_authority`` (default the
    same). ``app_stays_up=False`` stops the app before the run, so its origin refuses.
    ``before_stop`` runs once the run has settled and the service still serves, because stopping
    the service cancels what a faulty close would have left behind.
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
            status = settled_status(base_url, events)
            before_stop(config)
            return events, status

        events, status = drive_over_http(child_cleanup, monkeypatch, config, play, runners)
    return ToolRun(events, status, app, ChildCapture(tmp_path / "capture"), config)


def ending_of(status: RunStatus) -> tuple[RunState, FailureCode | None]:
    return status.state, status.failure_code


def ended_as(run: ToolRun) -> tuple[RunState, FailureCode | None]:
    return ending_of(run.status)


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
    failed = [event for event in run.events if isinstance(event, Failed)]
    assert (ended_as(run), run.capture.replies) == (PROVIDER_ERROR, [])
    assert [event.message for event in failed] == [RUNNER_FAILED_MESSAGE]
    assert "CapabilityReflected" in [kind for _event, kind, _function in failure_lines(caplog)]
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


def leave_when(
    base_url: str, event_type: str, ready: Callable[[], None] = lambda: None
) -> list[RunEvent]:
    """Start a run, read its stream up to ``event_type``, wait until ``ready``, then leave."""
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
            if any(event.type == event_type for event in seen):
                ready()
                break
    return seen


def leave_after_the_first_tool_call(base_url: str) -> list[RunEvent]:
    return leave_when(base_url, "tool_call")


def pending_tool_tasks() -> list[str]:
    gc.collect()
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

    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", run.capture.relay_port), timeout=SETTLE_SECONDS)
    assert (closes, pending_tool_tasks()) == ([ending], [])


@pytest.fixture(name="final_held")
def the_merger_holding_a_final_for_a_running_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> threading.Event:
    held = threading.Event()
    has_running_forward = ToolCallBarrier.has_running_forward

    def noting_a_running_forward(self: ToolCallBarrier) -> bool:
        running = has_running_forward(self)
        if running:
            held.set()
        return running

    monkeypatch.setattr(ToolCallBarrier, "has_running_forward", noting_a_running_forward)
    return held


def lists_the_capability_as_a_description(request: RecordedRequest):
    bearer = request.headers["authorization"].removeprefix("Bearer ")
    tools = [{"name": name, "description": bearer, "inputSchema": {}} for name in TOOLS]
    return rpc_result(request, {"tools": tools})


def test_a_capability_in_the_tool_list_fails_the_run(
    tmp_path, authority, child_cleanup, monkeypatch, caplog
):
    caplog.set_level("DEBUG")

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE],
        overrides={"tools/list": lists_the_capability_as_a_description},
    )

    everything_seen = json.dumps([event.model_dump(mode="json") for event in run.events])
    everything_seen += run.status.model_dump_json() + caplog.text
    assert (ended_as(run), run.app.calls, run.capture.replies) == (PROVIDER_ERROR, [], [])
    assert "CapabilityReflected" in [kind for _event, kind, _function in failure_lines(caplog)]
    assert run.app.capability not in everything_seen + run.capture.everything_it_was_given


def test_the_child_of_a_tool_run_gets_the_configured_tool_timeout(
    tmp_path, authority, child_cleanup, monkeypatch
):
    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE],
        runs={"max_duration_s": 300.0005},
    )

    assert run.capture.text_of("environment").splitlines().count("MCP_TOOL_TIMEOUT=300001") == 1


def saved(request: RecordedRequest):
    return rpc_result(request, {"content": [{"type": "text", "text": "saved"}]})


def note_the_call(tmp_path: Path) -> None:
    noted = tmp_path / "capture" / "app_has_call"
    noted.parent.mkdir(exist_ok=True)
    noted.touch()


def noting_the_call_then(tmp_path: Path, wait: Callable[[], None]) -> Mapping[str, Responder]:
    def answer(request: RecordedRequest):
        note_the_call(tmp_path)
        wait()
        return saved(request)

    return {"tools/call": answer}


def noting_the_call_then_holding(tmp_path: Path, held: HeldRequests) -> Mapping[str, Responder]:
    holding = held.responder(saved)

    def answer(request: RecordedRequest):
        note_the_call(tmp_path)
        return holding(request)

    return {"tools/call": answer}


def test_a_final_after_a_cancelled_call_follows_its_result(
    tmp_path, authority, child_cleanup, monkeypatch, final_held
):
    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE],
        give_up_when_present=tmp_path / "capture" / "app_has_call",
        overrides=noting_the_call_then(tmp_path, lambda: final_held.wait(STUCK_APP_BOUND_SECONDS)),
    )

    assert wire_shape(run.events) == [
        ("tool_call", "save_note", "call-1", "side_effecting"),
        ("tool_result", "call-1", "saved", False),
        ("succeeded", "done"),
    ]
    assert (ended_as(run), len(run.app.calls), run.capture.replies) == (SUCCEEDED, 1, [])
    assert final_held.is_set()


def settling_before_the_app_is_released(
    release: threading.Event, scenario: Callable[[str], list[RunEvent]]
) -> Callable[[str], list[RunEvent]]:
    def play(base_url: str) -> list[RunEvent]:
        try:
            events = scenario(base_url)
            settled_status(base_url, events)
            return events
        finally:
            release.set()

    return play


PROMPT_SUPERVISION = {"heartbeat_interval_s": 1, "stall_timeout_s": 1.5}


@pytest.mark.parametrize(
    ("options", "state", "code", "client_lost"),
    [
        pytest.param(
            {"runs": {**PROMPT_SUPERVISION, "idle_timeout_s": 2.0, "max_duration_s": 30.0}},
            RunState.FAILED,
            FailureCode.IDLE_TIMEOUT,
            None,
            id="idle-expiry",
        ),
        pytest.param(
            {
                "runs": {**PROMPT_SUPERVISION, "idle_timeout_s": 4.0, "max_duration_s": 4.5},
                "linger_before_calls_seconds": 1.0,
            },
            RunState.FAILED,
            FailureCode.LIMIT_EXCEEDED,
            None,
            id="maximum-duration",
        ),
        pytest.param(
            {
                "runs": {**PROMPT_SUPERVISION, "idle_timeout_s": 20.0, "max_duration_s": 30.0},
                "scenario_when_held": lambda base_url, held: leave_when(
                    base_url, "tool_call", lambda: eventually(held.is_set)
                ),
                "calls": [READ_NOTE],
            },
            RunState.CANCELLED,
            None,
            ClientLoss.DISCONNECTED,
            id="client-departure",
        ),
    ],
)
def test_a_held_final_with_a_stuck_app_ends_by_supervision(
    tmp_path, authority, child_cleanup, monkeypatch, final_held, options, state, code, client_lost
):
    release = threading.Event()
    scenario = options.pop("scenario_when_held", lambda base_url, _held: post_a_run(base_url))

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        give_up_when_present=tmp_path / "capture" / "app_has_call",
        overrides=noting_the_call_then(tmp_path, lambda: release.wait(STUCK_APP_BOUND_SECONDS)),
        scenario=settling_before_the_app_is_released(
            release, lambda base_url: scenario(base_url, final_held)
        ),
        **{"calls": [SAVE_NOTE], **options},
    )

    assert run.status.state == state
    assert run.status.failure_code == code
    assert run.status.client_lost == client_lost
    assert final_held.is_set()
    assert not any(isinstance(event, Succeeded) for event in run.events)
    assert (len(run.app.calls), pending_tool_tasks()) == (1, [])
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", run.capture.relay_port), timeout=SETTLE_SECONDS)


def relay_refuses(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=SETTLE_SECONDS).close()
    except ConnectionRefusedError:
        return True
    return False


def nothing_left_running(
    captures: Sequence[ChildCapture], *, private_root_left_empty: bool = False
) -> Callable[[ServiceConfig], None]:
    def check(config: ServiceConfig) -> None:
        eventually(lambda: (held_claude_slots(), pending_tool_tasks()) == (0, []))
        for capture in captures:
            eventually(lambda: relay_refuses(capture.relay_port))
        if private_root_left_empty:
            eventually(lambda: list(private_root_of(config).iterdir()) == [])

    return check


def leaving_beside_the_held_request(
    held: HeldRequests, event_type: str, also_ready: Callable[[], None] = lambda: None
) -> Callable[[str], list[RunEvent]]:
    def play(base_url: str) -> list[RunEvent]:
        holding: list[HeldRequest] = []

        def ready() -> None:
            holding.extend(held.wait_for(1))
            also_ready()

        try:
            events = leave_when(base_url, event_type, ready)
            [request] = holding
            request.wait_for_peer_left()
            return events
        finally:
            held.release_all()

    return play


def assert_the_departure_left_nothing_running(run: ToolRun, caplog) -> None:
    assert (run.status.state, run.status.client_lost, run.status.failure_code) == (
        RunState.CANCELLED,
        ClientLoss.DISCONNECTED,
        None,
    )
    assert not any(isinstance(event, (ToolResult, Succeeded)) for event in run.events)
    assert failure_lines(caplog) == []


def test_a_client_leaving_during_a_forward_cancels_it_upstream(
    tmp_path, authority, child_cleanup, monkeypatch, caplog
):
    held = HeldRequests()

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[READ_NOTE],
        overrides={"tools/call": held.responder(saved)},
        scenario=leaving_beside_the_held_request(held, "tool_call"),
        before_stop=nothing_left_running([ChildCapture(tmp_path / "capture")]),
    )

    assert_the_departure_left_nothing_running(run, caplog)
    assert (len(run.app.calls), run.capture.replies) == (1, [])


def test_a_client_leaving_beside_a_held_final_cancels_the_forward(
    tmp_path, authority, child_cleanup, monkeypatch, final_held, caplog
):
    held = HeldRequests()

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[READ_NOTE],
        give_up_when_present=tmp_path / "capture" / "app_has_call",
        overrides=noting_the_call_then_holding(tmp_path, held),
        scenario=leaving_beside_the_held_request(
            held, "tool_call", lambda: eventually(final_held.is_set)
        ),
        before_stop=nothing_left_running([ChildCapture(tmp_path / "capture")]),
    )

    assert_the_departure_left_nothing_running(run, caplog)
    assert (final_held.is_set(), len(run.app.calls), run.capture.replies) == (True, 1, [])


def test_a_client_leaving_during_the_open_starts_no_child(
    tmp_path, authority, child_cleanup, monkeypatch, caplog
):
    held = HeldRequests()

    def initialized(request: RecordedRequest):
        return rpc_result(request, {"protocolVersion": "2025-11-25"})

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE],
        overrides={"initialize": held.responder(initialized)},
        scenario=leaving_beside_the_held_request(held, "run_started"),
        before_stop=nothing_left_running([], private_root_left_empty=True),
    )

    assert_the_departure_left_nothing_running(run, caplog)
    assert (run.capture.started, run.app.calls) == (False, [])


PARALLEL_MARKERS = [f"run-{number}" for number in range(5)]


def marker_of(request: RecordedRequest) -> str:
    return request.message["params"]["arguments"]["marker"]


def answered_with_its_marker(request: RecordedRequest):
    return rpc_result(request, {"content": [{"type": "text", "text": marker_of(request)}]})


def post_a_run_marked(base_url: str, marker: str) -> list[RunEvent]:
    turn = {"system": marker, "messages": [{"role": "user", "text": "a"}]}
    return decoded_events(post_run(base_url, headers(), run_body(turn=turn)).content)


def five_runs_held_inside_their_calls_at_once(
    held: HeldRequests, outcomes: dict[str, tuple[list[RunEvent], RunStatus]]
) -> Callable[[str], list[RunEvent]]:
    def play(base_url: str) -> list[RunEvent]:
        with ThreadPoolExecutor(len(PARALLEL_MARKERS)) as clients:
            try:
                posted = {
                    marker: clients.submit(post_a_run_marked, base_url, marker)
                    for marker in PARALLEL_MARKERS
                }
                forwards = held.wait_for(len(PARALLEL_MARKERS))
                assert len({forward.request.identity for forward in forwards}) == 5
                assert sorted(marker_of(forward.request) for forward in forwards) == (
                    PARALLEL_MARKERS
                )
            finally:
                held.release_all()
            for marker, future in posted.items():
                events = future.result()
                outcomes[marker] = (events, settled_status(base_url, events))
        return outcomes[PARALLEL_MARKERS[0]][0]

    return play


def test_five_parallel_tool_runs_stay_apart(tmp_path, authority, child_cleanup, monkeypatch):
    held = HeldRequests(bound_seconds=STUCK_APP_BOUND_SECONDS)
    outcomes: dict[str, tuple[list[RunEvent], RunStatus]] = {}
    captures = {marker: ChildCapture(tmp_path / "capture" / marker) for marker in PARALLEL_MARKERS}

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE],
        marked_by_system_prompt=True,
        overrides={"tools/call": held.responder(answered_with_its_marker)},
        scenario=five_runs_held_inside_their_calls_at_once(held, outcomes),
        before_stop=nothing_left_running(list(captures.values())),
    )

    assert {marker: wire_shape(events) for marker, (events, _status) in outcomes.items()} == {
        marker: [
            ("tool_call", "save_note", "call-1", "side_effecting"),
            ("tool_result", "call-1", marker, False),
            ("succeeded", "done"),
        ]
        for marker in PARALLEL_MARKERS
    }
    assert {ending_of(status) for _events, status in outcomes.values()} == {SUCCEEDED}
    assert {
        marker: [marker in reply["body"] for reply in capture.replies]
        for marker, capture in captures.items()
    } == {marker: [True] for marker in captures}
    assert len({capture.relay_secret for capture in captures.values()}) == 5
    assert len({call.headers["authorization"] for call in run.app.calls}) == 5
    assert all_children_gone(child_cleanup) == [True] * 5


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

    assert ending_of(status) == (SUCCEEDED if signed_by_the_configured_ca else PROVIDER_ERROR)
