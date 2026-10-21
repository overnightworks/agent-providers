"""The CLI runner carries a run's tool calls through the relay under the app's own names.

Every row serves the real app on a loopback socket, runs a fake Claude as a real child that
speaks to the run's relay, and answers its calls from a TLS fake app signed by a test CA. The
rows build ``Runners(cli=CliRunner(...))`` as production does; the entry rows go through
``cli.main`` instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import math
import signal
import socket
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from anchored_support import ChildCleanup
from bounds import TEST_WAIT_SECONDS
from capability_support import canary_token, capability_carrying
from cli_tools_support import (
    ChildCapture,
    ClaudeCall,
    ToolApp,
    a_relay_calling_claude,
    a_tool_app,
)
from descendant_support import DescendantRow
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
    ScriptedClaudeChild,
    a_fake_claude,
    an_init,
    drive_scripted,
    held_claude_slots,
    line,
)
from relay_support import (
    RawClient,
    RelayDoor,
    json_rpc,
    parked_at,
    port_of,
    post_message,
    yield_until,
)
from service_support import (
    LISTED_MODEL,
    PINNED_CLAUDE_VERSION,
    SteppedLoop,
    assert_rejected,
    decoded_events,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    post_run,
    run_body,
)
from test_service_cli_runner import (
    FIRST_LOGIN,
    LOGIN_FILES_GONE_BAD,
    SECOND_LOGIN,
    ScriptedRun,
    a_config,
    a_login_file,
    a_scripted_run,
    all_children_gone,
    assert_only_the_class_was_logged,
    drive_over_http,
    eventually,
    headers,
    login_the_child_saw,
    private_root_of,
    status_of,
    swap_login,
    through_the_service_entry,
    trusting,
)
from test_service_cli_runner import (
    post_a_run as post_a_run_naming,
)
from test_service_run_tools import Stall

import agent_service.cli_runner as cli_runner_module
from agent_client.mcp_wire import (
    LATEST_PROTOCOL_VERSION,
    JsonRpcErrorCode,
)
from agent_client.sse import RunStreamDecoder
from agent_client.wire import (
    MAX_EVENT_BYTES,
    ClientLoss,
    Failed,
    FailureCode,
    RejectionCode,
    RunEvent,
    RunState,
    RunStatus,
    Succeeded,
    ToolCall,
    ToolResult,
)
from agent_providers import spawn
from agent_providers.cli_run import CliRunAdapter, RelayAccess
from agent_providers.config import PrivateWorkingRoot
from agent_providers.events import FinalEvent, StreamEvent
from agent_providers.procfs import GroupCensus
from agent_service.capability import Capability
from agent_service.cli_runner import CliRunner, mcp_tls_context
from agent_service.config import ServiceConfig
from agent_service.run_events import RUNNER_FAILED_MESSAGE
from agent_service.run_liveness import LoopClock
from agent_service.run_tools import UPSTREAM_ABORT_SECONDS, RunTools
from agent_service.runners import Runners
from agent_service.tool_relay import ToolCallBarrier

SAVE_NOTE = ClaudeCall("save_note", {"text": "la la"})
READ_NOTE = ClaudeCall("read_note")
TOOLS = {"save_note": "side_effecting", "read_note": "read_only"}
SETTLE_SECONDS = 10.0
STUCK_APP_BOUND_SECONDS = 30.0
HELD_RUN_CLIENT_TIMEOUT_SECONDS = STUCK_APP_BOUND_SECONDS + SETTLE_SECONDS
CLIENT_LEAVES_WITHIN_SECONDS = TEST_WAIT_SECONDS / 2
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


def post_a_run(base_url: str, model: str | None = None) -> list[RunEvent]:
    return post_a_run_naming(base_url, TOOLS, model)


def a_plain_run(base_url: str, _app: ToolApp) -> list[RunEvent]:
    return post_a_run(base_url)


def a_tool_run(
    tmp_path: Path,
    authority: TestAuthority,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    *,
    calls: Sequence[ClaudeCall] = (),
    tools: Mapping[str, str] = TOOLS,
    offered: Sequence[str] | None = None,
    runs: dict[str, object] | None = None,
    overrides: Mapping[str, Responder] | None = None,
    serving_authority: TestAuthority | None = None,
    app_stays_up: bool = True,
    scenario: Callable[[str, ToolApp], list[RunEvent]] | None = None,
    before_stop: Callable[[ServiceConfig], None] = lambda _config: None,
    token_file: Path | None = None,
    **claude_options: Any,
) -> ToolRun:
    """One run of the real runner with its tools on, against a TLS fake app, over HTTP.

    The runner trusts ``authority``; the app is signed by ``serving_authority`` (default the
    same). The default scenario posts a run whose request grants exactly ``tools``; ``offered``
    is the list the app answers with. ``app_stays_up=False`` stops the app before the run, so its
    origin refuses.
    ``before_stop`` runs once the run has settled and the service still serves, because stopping
    the service cancels what a faulty close would have left behind.
    """
    binary = a_relay_calling_claude(
        tmp_path, version=PINNED_CLAUDE_VERSION, calls=calls, **claude_options
    )
    with ExitStack() as running:
        app = running.enter_context(
            a_tool_app(
                serving_authority or authority,
                list(tools if offered is None else offered),
                overrides=overrides,
            )
        )
        if not app_stays_up:
            running.close()
        config = a_config(
            tmp_path,
            binary,
            runs=runs,
            songmaker={"mcp_origin": app.origin},
            token_file=token_file,
        )
        runners = Runners(
            cli=CliRunner(config, tls_context=mcp_tls_context(authority.ca_file), clock=LoopClock())
        )

        def play(base_url: str) -> tuple[list[RunEvent], RunStatus]:
            events = (
                post_a_run_naming(base_url, tools)
                if scenario is None
                else scenario(base_url, app)
            )
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
CANCELLED = (RunState.CANCELLED, None)
INTERRUPTED = (RunState.FAILED, FailureCode.INTERRUPTED)


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
def test_each_calls_effect_follows_the_request(
    tmp_path, authority, child_cleanup, monkeypatch, call, effect
):
    run = a_tool_run(tmp_path, authority, child_cleanup, monkeypatch, calls=[call])

    assert [shape[3] for shape in wire_shape(run.events) if shape[0] == "tool_call"] == [effect]


def test_the_effect_a_request_names_changes_the_ending_and_nothing_the_child_is_given(
    tmp_path, authority, child_cleanup, monkeypatch
):
    def left_after_the_first_call(effect: str):
        def play(base_url: str, _app: ToolApp) -> list[RunEvent]:
            named = {**TOOLS, "save_note": effect}
            return leave_when(base_url, "tool_call", tools=named)

        (tmp_path / effect).mkdir()
        return a_tool_run(
            tmp_path / effect,
            authority,
            child_cleanup,
            monkeypatch,
            calls=[SAVE_NOTE],
            scenario=play,
        )

    read_only = left_after_the_first_call("read_only")
    side_effecting = left_after_the_first_call("side_effecting")

    assert [ending_of(run.status) for run in (read_only, side_effecting)] == [
        (RunState.CANCELLED, None),
        (RunState.FAILED, FailureCode.INTERRUPTED),
    ]
    assert read_only.capture.stdin
    assert read_only.capture.stdin == side_effecting.capture.stdin
    assert read_only.capture.listing_replies
    assert read_only.capture.listing_replies == side_effecting.capture.listing_replies
    assert read_only.capture.replies == side_effecting.capture.replies


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
        events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run_naming)

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


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        pytest.param(f"claude/{LISTED_MODEL}", [f"--model={LISTED_MODEL}"], id="M3-listed-model"),
        pytest.param(None, [], id="M4-no-model"),
    ],
)
def test_the_relay_run_gets_the_listed_model_as_one_argument_and_its_relay_arguments(
    tmp_path, authority, child_cleanup, monkeypatch, requested, expected
):
    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        scenario=lambda base_url, _app: post_a_run(base_url, requested),
    )

    arguments = run.capture.text_of("argv").splitlines()
    assert (
        [a for a in arguments if a == "--model" or a.startswith("--model=")],
        "--mcp-config" in arguments,
        arguments[arguments.index("--allowedTools") + 1],
    ) == (expected, True, "mcp__relay__*")


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


@pytest.mark.parametrize("with_origin", [False, True], ids=["no-origin", "no-tools"])
def test_an_app_without_granted_tools_gets_the_tool_free_run(
    tmp_path, authority, child_cleanup, monkeypatch, with_origin
):
    binary = a_fake_claude(tmp_path, version_line=f"{PINNED_CLAUDE_VERSION} (Claude Code)")
    with a_tool_app(authority) as app:
        songmaker = {"mcp_origin": app.origin if with_origin else None}
        config = a_config(tmp_path, binary, songmaker=songmaker)
        runners = Runners(
            cli=CliRunner(config, tls_context=mcp_tls_context(authority.ca_file), clock=LoopClock())
        )

        events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run_naming, runners)

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


def test_a_login_file_swapped_between_two_tool_runs_serves_the_next_run_without_a_restart(
    tmp_path, authority, child_cleanup, monkeypatch
):
    login_file = a_login_file(tmp_path, FIRST_LOGIN)
    capture = tmp_path / "capture"
    first_runs: list[tuple[tuple[RunState, FailureCode | None], str]] = []

    def two_runs_with_a_swap_between(base_url: str, _app: ToolApp) -> list[RunEvent]:
        first = post_a_run(base_url)
        first_runs.append(
            (ending_of(settled_status(base_url, first)), login_the_child_saw(capture))
        )
        swap_login(login_file, SECOND_LOGIN)
        return post_a_run(base_url)

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE],
        token_file=login_file,
        scenario=two_runs_with_a_swap_between,
    )

    assert [*first_runs, (ended_as(run), login_the_child_saw(capture))] == [
        (SUCCEEDED, FIRST_LOGIN),
        (SUCCEEDED, SECOND_LOGIN),
    ]
    assert len(run.app.calls) == 2


@pytest.mark.parametrize("spoil", LOGIN_FILES_GONE_BAD)
def test_a_login_file_gone_bad_fails_the_tool_run_before_any_tool_connection_or_child(
    tmp_path, authority, child_cleanup, monkeypatch, caplog, spoil
):
    login_file = a_login_file(tmp_path, FIRST_LOGIN)
    capture = tmp_path / "capture"
    failed_run: list[
        tuple[list[RunEvent], tuple[RunState, FailureCode | None], bool, int, int]
    ] = []

    def spoiled_then_repaired(base_url: str, app: ToolApp) -> list[RunEvent]:
        spoil(login_file)
        failed = post_a_run(base_url)
        failed_run.append(
            (
                failed,
                ending_of(settled_status(base_url, failed)),
                (capture / "argv").exists(),
                held_claude_slots(),
                app.fake.accepted_connections,
            )
        )
        swap_login(login_file, SECOND_LOGIN)
        return post_a_run(base_url)

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE],
        runs={"max_parallel_cli_runs": 1},
        token_file=login_file,
        scenario=spoiled_then_repaired,
    )

    [(failed_events, failed_ending, child_started, slots_held, connections_accepted)] = failed_run
    assert (
        failed_ending,
        [event.message for event in failed_events if isinstance(event, Failed)],
        child_started,
        slots_held,
        connections_accepted,
        ended_as(run),
        login_the_child_saw(capture),
        len(run.app.calls),
        held_claude_slots(),
    ) == (PROVIDER_ERROR, [RUNNER_FAILED_MESSAGE], False, 0, 0, SUCCEEDED, SECOND_LOGIN, 1, 0)
    assert list(private_root_of(run.config).iterdir()) == []
    assert_only_the_class_was_logged(caplog, failed_events, tmp_path)


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


@pytest.mark.parametrize(
    ("signed", "offered"),
    [
        pytest.param(["read_note"], ["edit_note"], id="none-signed-offered"),
        pytest.param(["read_note", "edit_note"], ["read_note"], id="partial-overlap"),
    ],
)
def test_a_signed_tool_the_app_does_not_offer_ends_the_run_before_any_child(
    tmp_path, authority, child_cleanup, monkeypatch, caplog, signed, offered
):
    caplog.set_level("DEBUG")

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[READ_NOTE],
        tools=dict.fromkeys(signed, "read_only"),
        offered=offered,
    )

    assert (ended_as(run), run.capture.started, held_claude_slots()) == (PROVIDER_ERROR, False, 0)
    assert "UnofferedTool" in [kind for _event, kind, _function in failure_lines(caplog)]
    assert not [name for name in signed if name in caplog.text]


def test_a_call_to_a_tool_outside_the_apps_grant_ends_the_run_and_never_reaches_the_app(
    tmp_path, authority, child_cleanup, monkeypatch
):
    run = a_tool_run(
        tmp_path, authority, child_cleanup, monkeypatch, calls=[ClaudeCall("delete_everything")]
    )

    assert (ended_as(run), run.app.calls) == (PROVIDER_ERROR, [])


def leave_when(
    base_url: str,
    event_type: str,
    ready: Callable[[], None] = lambda: None,
    *,
    tools: Mapping[str, str] = TOOLS,
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
                "v": 2,
                "purpose": "cowriter",
                "turn": {"system": "s", "messages": [{"role": "user", "text": "a"}]},
                "roles": ["admin", "user"],
                "tools": dict(tools),
            }
        ),
        timeout=HELD_RUN_CLIENT_TIMEOUT_SECONDS,
    ) as response:
        for chunk in response.iter_bytes():
            seen += [item for item in decoder.feed(chunk) if not hasattr(item, "kind")]
            if any(event.type == event_type for event in seen):
                ready()
                break
    return seen


def leave_after_the_first_tool_call(base_url: str, _app: ToolApp) -> list[RunEvent]:
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
    tmp_path, authority, child_cleanup, monkeypatch, caplog, ending, options
):
    closes: list[str] = []
    close = RunTools.close

    async def counted(self, reap):
        closes.append(ending)
        await close(self, reap)

    monkeypatch.setattr(RunTools, "close", counted)

    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        before_stop=nothing_left_running([ChildCapture(tmp_path / "capture")]),
        **options,
    )

    assert closes == [ending]
    if ending == "client-leaves":
        assert_the_departure_left_nothing_running(run, caplog, ending=INTERRUPTED)
    if ending == "success":
        assert_the_run_logged_no_failure(run.status, run.events, caplog)


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


@pytest.mark.parametrize(
    ("max_duration_s", "tool_timeout_ms"),
    [
        pytest.param(300.0005, 300001, id="a-fraction-rounds-up"),
        pytest.param(2147483.0, 2147483000, id="the-bound"),
        pytest.param(math.nextafter(2147483.0, -math.inf), 2147483000, id="just-below-the-bound"),
    ],
)
def test_the_child_of_a_tool_run_gets_the_configured_tool_timeout(
    tmp_path, authority, child_cleanup, monkeypatch, max_duration_s, tool_timeout_ms
):
    run = a_tool_run(
        tmp_path,
        authority,
        child_cleanup,
        monkeypatch,
        calls=[SAVE_NOTE],
        runs={"max_duration_s": max_duration_s},
    )

    environment = run.capture.text_of("environment").splitlines()
    assert environment.count(f"MCP_TOOL_TIMEOUT={tool_timeout_ms}") == 1


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
) -> Callable[[str, ToolApp], list[RunEvent]]:
    def play(base_url: str, _app: ToolApp) -> list[RunEvent]:
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
        before_stop=nothing_left_running([ChildCapture(tmp_path / "capture")]),
        **{"calls": [SAVE_NOTE], **options},
    )

    assert run.status.state == state
    assert run.status.failure_code == code
    assert run.status.client_lost == client_lost
    assert final_held.is_set()
    assert not any(isinstance(event, Succeeded) for event in run.events)
    assert len(run.app.calls) == 1


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
) -> Callable[[str, ToolApp], list[RunEvent]]:
    def play(base_url: str, _app: ToolApp) -> list[RunEvent]:
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


def assert_the_run_logged_no_failure(status: RunStatus, events: Sequence[RunEvent], caplog) -> None:
    assert status.events == len(events)
    assert failure_lines(caplog) == []


def assert_the_departure_left_nothing_running(
    run: ToolRun, caplog, ending: tuple[RunState, FailureCode | None] = CANCELLED
) -> None:
    assert (*ending_of(run.status), run.status.client_lost) == (*ending, ClientLoss.DISCONNECTED)
    assert_the_run_logged_no_failure(run.status, run.events, caplog)


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
    response = post_run(
        base_url,
        headers(),
        run_body(turn=turn, tools=TOOLS),
        timeout=HELD_RUN_CLIENT_TIMEOUT_SECONDS,
    )
    return decoded_events(response.content)


def five_runs_held_inside_their_calls_at_once(
    held: HeldRequests, outcomes: dict[str, tuple[list[RunEvent], RunStatus]]
) -> Callable[[str, ToolApp], list[RunEvent]]:
    def play(base_url: str, _app: ToolApp) -> list[RunEvent]:
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


@contextmanager
def owning_the_descendant(row: DescendantRow) -> Iterator[None]:
    """Complete the descendant's handshake beside the scenario, which cannot start without it."""
    with ThreadPoolExecutor(1) as handshaking:
        owned = handshaking.submit(row.own_the_descendant)
        try:
            yield
        finally:
            owned.result()


def in_the_descendants_company(
    row: DescendantRow, scenario: Callable[[str, ToolApp], list[RunEvent]]
) -> Callable[[str, ToolApp], list[RunEvent]]:
    def play(base_url: str, app: ToolApp) -> list[RunEvent]:
        with owning_the_descendant(row):
            return scenario(base_url, app)

    return play


@pytest.mark.parametrize(
    ("options", "ending"),
    [
        pytest.param({"scenario": a_plain_run}, SUCCEEDED, id="success"),
        pytest.param(
            {"scenario": leave_after_the_first_tool_call, "linger_seconds": 60.0},
            (RunState.CANCELLED, None),
            id="client-leaves",
        ),
    ],
)
def test_a_descendant_of_a_tool_run_is_dead_when_the_run_ends(
    tmp_path, authority, child_cleanup, monkeypatch, handshake, caplog, options, ending
):
    scenario = options.pop("scenario")
    descendant_dead_at_the_end: list[bool] = []

    with DescendantRow(handshake, child_cleanup) as row:
        checking_the_end = nothing_left_running([ChildCapture(tmp_path / "capture")])

        def check_the_end(config: ServiceConfig) -> None:
            descendant_dead_at_the_end.append(row.descendant_is_dead())
            checking_the_end(config)

        run = a_tool_run(
            tmp_path,
            authority,
            child_cleanup,
            monkeypatch,
            calls=[READ_NOTE],
            descendant=handshake,
            scenario=in_the_descendants_company(row, scenario),
            before_stop=check_the_end,
            **options,
        )

    assert (ended_as(run), descendant_dead_at_the_end) == (ending, [True])
    if ending == CANCELLED:
        assert_the_departure_left_nothing_running(run, caplog)
    else:
        assert_the_run_logged_no_failure(run.status, run.events, caplog)


class FirstReapGate:
    """A gate in front of the first run's real group reap; waiting for it fails on a timeout."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.released = threading.Event()
        self.reaps = 0

    def wait_until_reached(self) -> None:
        assert self.reached.wait(STUCK_APP_BOUND_SECONDS), "the first run was never reaped"


@pytest.fixture(name="first_reap")
def a_gate_in_front_of_the_first_real_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FirstReapGate]:
    gate = FirstReapGate()
    reap = spawn.AnchoredChild.kill_until_gone

    async def gated(child: spawn.AnchoredChild, *, within: float) -> spawn.KillUntilGoneResult:
        gate.reaps += 1
        if gate.reaps == 1:
            gate.reached.set()
            assert await asyncio.to_thread(gate.released.wait, STUCK_APP_BOUND_SECONDS)
        return await reap(child, within=within)

    monkeypatch.setattr(spawn.AnchoredChild, "kill_until_gone", gated)
    try:
        yield gate
    finally:
        gate.released.set()


@dataclass(frozen=True)
class RunsAcrossAStop:
    placed_while_reaping: tuple[RunState, bool]
    second_run: httpx.Response
    first_status: RunStatus
    third_status: RunStatus
    descendant_dead_after: bool


def test_a_real_group_keeps_its_place_through_the_service_until_the_reap_ends(
    tmp_path,
    authority,
    child_cleanup,
    monkeypatch,
    logging_state_restored,
    handshake,
    first_reap,
    caplog,
):
    held = HeldRequests(bound_seconds=STUCK_APP_BOUND_SECONDS)
    binary = a_relay_calling_claude(
        tmp_path,
        version=PINNED_CLAUDE_VERSION,
        calls=[READ_NOTE],
        linger_seconds=60.0,
        descendant=handshake,
    )
    overrides = {"tools/call": held.responder(saved)}
    with (
        a_tool_app(authority, list(TOOLS), overrides=overrides) as app,
        DescendantRow(handshake, child_cleanup) as row,
    ):
        songmaker = {"mcp_origin": app.origin}
        runs = {"max_parallel_cli_runs": 1}
        config = trusting(a_config(tmp_path, binary, runs=runs, songmaker=songmaker), authority)
        forwards: list[HeldRequest] = []

        def leave_during_the_first_forward(base_url: str) -> list[RunEvent]:
            return leave_when(base_url, "tool_call", lambda: forwards.extend(held.wait_for(1)))

        def run_to_its_end_with_the_call_released(base_url: str) -> list[RunEvent]:
            with ThreadPoolExecutor(1) as posting:
                third = posting.submit(post_a_run, base_url)
                held.wait_for(1)
                held.release_all()
                return third.result()

        def across_the_stop(base_url: str) -> RunsAcrossAStop:
            with owning_the_descendant(row):
                try:
                    first = leave_during_the_first_forward(base_url)
                    first_reap.wait_until_reached()
                    while_reaping = (
                        status_of(base_url, first).state,
                        row.descendant_is_dead(),
                    )
                    second = post_run(base_url, headers(), run_body(tools=TOOLS))
                    first_reap.released.set()
                    [forward] = forwards
                    forward.wait_for_peer_left()
                    settled = settled_status(base_url, first)
                    dead_after = row.descendant_is_dead()
                    assert_the_run_logged_no_failure(settled, first, caplog)
                    nothing_left_running([ChildCapture(tmp_path / "capture")])(config)
                    third = run_to_its_end_with_the_call_released(base_url)
                    third_status = settled_status(base_url, third)
                    assert_the_run_logged_no_failure(third_status, third, caplog)
                    nothing_left_running([ChildCapture(tmp_path / "capture")])(config)
                finally:
                    first_reap.released.set()
                    held.release_all()
            return RunsAcrossAStop(while_reaping, second, settled, third_status, dead_after)

        across = through_the_service_entry(
            tmp_path, config, child_cleanup, monkeypatch, across_the_stop
        )

    assert across.placed_while_reaping == (RunState.RUNNING, False)
    assert_rejected(across.second_run, RejectionCode.BUSY, 429)
    assert (ending_of(across.first_status), across.first_status.client_lost) == (
        (RunState.CANCELLED, None),
        ClientLoss.DISCONNECTED,
    )
    assert (across.descendant_dead_after, ending_of(across.third_status)) == (True, SUCCEEDED)


RELAY_INIT_LINE = an_init(
    tools=[f"mcp__relay__{name}" for name in TOOLS],
    mcp_servers=[{"name": "relay", "status": "connected"}],
)


class ChildHeldAtItsReap(ScriptedClaudeChild):
    """A parked scripted leader whose group reap waits, in the test's loop, for ``reap_gate``."""

    def __init__(self, **script: Any) -> None:
        parked = {
            "returncode": None,
            "natural_exit": None,
            "stdout_stalls": True,
            "stream_lines": [RELAY_INIT_LINE, TEXT_LINE],
        }
        super().__init__(2468, **{**parked, **script})
        self.reap_reached = asyncio.Event()
        self.reap_gate = asyncio.Event()

    async def kill_until_gone(self, *, within: float) -> spawn.KillUntilGoneResult:
        self.reap_reached.set()
        await self.reap_gate.wait()
        return await super().kill_until_gone(within=within)


def capability_for(app: ToolApp) -> Capability:
    template = capability_carrying(canary_token(), tools=tuple(TOOLS))
    return Capability(template.claims.model_copy(update={"aud": app.origin}), template.token)


def a_scripted_tool_run(tmp_path: Path, authority: TestAuthority, app: ToolApp) -> ScriptedRun:
    songmaker = {"mcp_origin": app.origin}
    config = a_config(tmp_path, a_fake_claude(tmp_path), songmaker=songmaker)
    return a_scripted_run(
        tmp_path,
        config=config,
        tls_context=mcp_tls_context(authority.ca_file),
        capability=capability_for(app),
    )


@dataclass(frozen=True)
class RelayDoorOfTheRun(RelayDoor):
    """Where the scripted child would have connected: the relay the run's tools opened."""

    @classmethod
    def in_the_run_of(cls, run: ScriptedRun) -> RelayDoorOfTheRun:
        [relay_file] = run.root.glob("**/relay-mcp.json")
        relay = json.loads(relay_file.read_text())["mcpServers"]["relay"]
        secret = relay["headers"]["Authorization"].removeprefix("Bearer ")
        return cls(RelayAccess(relay["url"], secret))

    async def connect(self) -> RawClient:
        return await RawClient.connect(port_of(self))

    def call(self, request_id: int, name: str = "read_note") -> bytes:
        params = {"name": name, "arguments": {}}
        body = json_rpc("tools/call", request_id, params)
        return post_message(self, body, LATEST_PROTOCOL_VERSION.value)


async def error_code_of(client: RawClient) -> int:
    return json.loads((await client.answer()).body)["error"]["code"]


def test_a_call_during_the_reap_is_refused_and_never_forwarded(
    tmp_path, authority, monkeypatch, logging_state_restored
):
    child = ChildHeldAtItsReap()
    with a_tool_app(authority, list(TOOLS), keep_alive=True) as app:
        run = a_scripted_tool_run(tmp_path, authority, app)

        async def scenario() -> None:
            await anext(run.frames)
            await anext(run.frames)
            door = RelayDoorOfTheRun.in_the_run_of(run)
            idle = await door.connect()
            closing = asyncio.create_task(run.frames.aclose())
            try:
                async with asyncio.timeout(TEST_WAIT_SECONDS):
                    await child.reap_reached.wait()
                with pytest.raises(ConnectionRefusedError):
                    await door.connect()
                await idle.send(door.call(2))
                assert await error_code_of(idle) == JsonRpcErrorCode.INVALID_PARAMS
            finally:
                child.reap_gate.set()
            async with asyncio.timeout(TEST_WAIT_SECONDS):
                await closing
            assert await idle.at_eof()
            await the_app_saw_the_client_leave(app)
            assert pending_tool_tasks() == []

        drive_scripted(monkeypatch, child, scenario(), loop_factory=SteppedLoop)

    assert app.calls == []


def core_events_seen(monkeypatch: pytest.MonkeyPatch) -> list[StreamEvent]:
    """Record each event the core's iterator hands the CLI runner, as the core yields it."""
    seen: list[StreamEvent] = []
    core = cli_runner_module.astream_cli_run

    async def recording(root: PrivateWorkingRoot, adapter: CliRunAdapter):
        async for event in core(root, adapter):
            seen.append(event)
            yield event

    monkeypatch.setattr(cli_runner_module, "astream_cli_run", recording)
    return seen


async def collected(frames: AsyncGenerator[bytes, None]) -> list[bytes]:
    return [frame async for frame in frames]


async def loop_settled() -> None:
    loop = asyncio.get_running_loop()
    assert isinstance(loop, SteppedLoop)
    await loop.settled()


async def no_app_handler_lingers(app: ToolApp) -> None:
    await yield_until(lambda: app.fake.ended_connections == app.fake.accepted_connections)


async def the_app_saw_the_client_leave(app: ToolApp) -> None:
    """Bounded below the fake's own idle timeout, which would otherwise end an idle connection."""
    async with asyncio.timeout(CLIENT_LEAVES_WITHIN_SECONDS):
        await no_app_handler_lingers(app)


@contextlib.asynccontextmanager
async def reap_released_and_reading_settled(
    child: ChildHeldAtItsReap, reading: asyncio.Task[Any]
) -> AsyncIterator[None]:
    """Whatever the observations do, the parked reap is released and the reading task ends."""
    try:
        yield
    finally:
        child.reap_gate.set()
        reading.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            async with asyncio.timeout(TEST_WAIT_SECONDS):
                await reading


def test_a_natural_exit_reaps_before_the_final_and_the_relay_closes_after_the_terminal(
    tmp_path, authority, monkeypatch, logging_state_restored
):
    child = ChildHeldAtItsReap(
        natural_exit=0, stdout_stalls=False, stream_lines=[RELAY_INIT_LINE, TEXT_LINE, SUCCESS_LINE]
    )
    seen = core_events_seen(monkeypatch)
    with a_tool_app(authority, list(TOOLS)) as app:
        run = a_scripted_tool_run(tmp_path, authority, app)

        async def scenario() -> None:
            reading = asyncio.create_task(collected(run.frames))
            async with reap_released_and_reading_settled(child, reading):
                async with asyncio.timeout(TEST_WAIT_SECONDS):
                    await child.reap_reached.wait()
                door = RelayDoorOfTheRun.in_the_run_of(run)
                await loop_settled()
                final_before_the_reap = [event for event in seen if isinstance(event, FinalEvent)]
                (await door.connect()).disconnect()
                child.reap_gate.set()
                async with asyncio.timeout(TEST_WAIT_SECONDS):
                    frames = await reading
                assert final_before_the_reap == []
                assert [type(event) for event in seen if isinstance(event, FinalEvent)] != []
                assert decoded_events(b"".join(frames))[-1].type == "succeeded"
                with pytest.raises(ConnectionRefusedError):
                    await door.connect()
                assert pending_tool_tasks() == []

        drive_scripted(monkeypatch, child, scenario(), loop_factory=SteppedLoop)

    assert app.calls == []


def an_unconfirmed_group(**script: Any) -> ChildHeldAtItsReap:
    return ChildHeldAtItsReap(
        survives={signal.SIGTERM, signal.SIGKILL},
        censuses=(GroupCensus.LIVE, GroupCensus.EMPTY),
        stderr_stalls=True,
        **script,
    )


@dataclass(frozen=True)
class RunWithAForwardInFlight:
    """A scripted run read by its own task, one call held inside the app, its caller waiting."""

    reading: asyncio.Task[list[bytes]]
    door: RelayDoorOfTheRun
    caller: RawClient
    forward: HeldRequest


async def run_with_a_forward_in_flight(
    run: ScriptedRun, held: HeldRequests
) -> RunWithAForwardInFlight:
    reading = asyncio.create_task(collected(run.frames), name="test:reading")
    await yield_until(lambda: any(run.root.glob("**/relay-mcp.json")))
    door = RelayDoorOfTheRun.in_the_run_of(run)
    caller = await door.connect()
    await caller.send(door.call(1))
    [forward] = await asyncio.to_thread(held.wait_for, 1)
    return RunWithAForwardInFlight(reading, door, caller, forward)


async def assert_the_tools_are_closed(app: ToolApp, flight: RunWithAForwardInFlight) -> None:
    with pytest.raises(ConnectionRefusedError):
        await flight.door.connect()
    assert await flight.caller.at_eof()
    await asyncio.to_thread(flight.forward.wait_for_peer_left)
    await no_app_handler_lingers(app)
    assert pending_tool_tasks() == []


def test_an_unconfirmed_group_with_tools_closes_them_and_holds_the_place(
    tmp_path, authority, monkeypatch, logging_state_restored
):
    child = an_unconfirmed_group()
    child.reap_gate.set()
    held = HeldRequests(bound_seconds=STUCK_APP_BOUND_SECONDS)
    with a_tool_app(authority, list(TOOLS), overrides={"tools/call": held.responder(saved)}) as app:
        run = a_scripted_tool_run(tmp_path, authority, app)

        async def scenario() -> None:
            flight = await run_with_a_forward_in_flight(run, held)
            flight.reading.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                async with asyncio.timeout(TEST_WAIT_SECONDS):
                    await flight.reading
            status = run.status()
            assert (status.state, status.failure_code) == PROVIDER_ERROR
            assert run.held_state() == (True, 1, True)
            await assert_the_tools_are_closed(app, flight)
            await run.frees_its_place_once_the_group_is_confirmed(child)

        drive_scripted(monkeypatch, child, scenario(), loop_factory=SteppedLoop)


def test_a_loss_beside_a_cancelled_close_ends_provider_error_with_tools_open(
    tmp_path, authority, monkeypatch, logging_state_restored
):
    child = an_unconfirmed_group()
    held = HeldRequests(bound_seconds=STUCK_APP_BOUND_SECONDS)
    with a_tool_app(authority, list(TOOLS), overrides={"tools/call": held.responder(saved)}) as app:
        run = a_scripted_tool_run(tmp_path, authority, app)

        async def scenario() -> None:
            flight = await run_with_a_forward_in_flight(run, held)
            async with reap_released_and_reading_settled(child, flight.reading):
                flight.reading.cancel()
                async with asyncio.timeout(TEST_WAIT_SECONDS):
                    await child.reap_reached.wait()
                flight.reading.cancel()
                await loop_settled()
                child.reap_gate.set()
                with contextlib.suppress(asyncio.CancelledError):
                    async with asyncio.timeout(TEST_WAIT_SECONDS):
                        await flight.reading
                status = run.status()
                assert (status.state, status.failure_code) == PROVIDER_ERROR
                assert run.held_state() == (True, 1, True)
                await assert_the_tools_are_closed(app, flight)
                await run.frees_its_place_once_the_group_is_confirmed(child)

        drive_scripted(monkeypatch, child, scenario(), loop_factory=SteppedLoop)


def test_an_overrunning_upstream_cleanup_does_not_hold_the_place(
    tmp_path, authority, monkeypatch, caplog, logging_state_restored
):
    child = ChildHeldAtItsReap()
    child.reap_gate.set()
    with a_tool_app(authority, list(TOOLS)) as app:
        run = a_scripted_tool_run(tmp_path, authority, app)

        async def scenario() -> None:
            await anext(run.frames)
            await anext(run.frames)
            stall = Stall(monkeypatch, httpx.AsyncClient, "aclose")
            closing = asyncio.create_task(run.frames.aclose())
            try:
                await yield_until(lambda: stall.pending)
                await parked_at(run.clock, UPSTREAM_ABORT_SECONDS)
                run.clock.advance_to(UPSTREAM_ABORT_SECONDS)
                closed, _ = await asyncio.wait({closing}, timeout=TEST_WAIT_SECONDS)
                assert closed, "the close waited for the parked cleanup"
                assert (run.place_is_held(), held_claude_slots()) == (False, 0)
                assert [kind for _event, kind, _function in failure_lines(caplog)] == [
                    "UpstreamCleanupIncomplete"
                ]
                assert stall.pending
            finally:
                stall.release()
            await yield_until(lambda: not stall.pending and pending_tool_tasks() == [])

        drive_scripted(monkeypatch, child, scenario(), loop_factory=SteppedLoop)


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
    ("signed_by_the_configured_ca", "wire", "calls_at_the_app"),
    [
        pytest.param(
            True,
            [
                ("tool_call", "save_note", "call-1", "side_effecting"),
                ("tool_result", "call-1", "save_note", False),
                ("succeeded", "done"),
            ],
            1,
            id="the-configured-ca",
        ),
        pytest.param(False, [("failed", FailureCode.PROVIDER_ERROR)], 0, id="another-ca"),
    ],
)
def test_a_run_trusts_exactly_the_configured_bundle(
    tmp_path,
    authority,
    other_authority,
    child_cleanup,
    monkeypatch,
    logging_state_restored,
    signed_by_the_configured_ca,
    wire,
    calls_at_the_app,
):
    binary = a_relay_calling_claude(tmp_path, version=PINNED_CLAUDE_VERSION, calls=[SAVE_NOTE])
    serving_authority = authority if signed_by_the_configured_ca else other_authority
    with a_tool_app(serving_authority, list(TOOLS)) as app:
        songmaker = {"mcp_origin": app.origin}
        config = trusting(a_config(tmp_path, binary, songmaker=songmaker), authority)

        captures = [ChildCapture(tmp_path / "capture")] if signed_by_the_configured_ca else []

        def post_and_settle(base_url: str) -> list[RunEvent]:
            events = post_a_run(base_url)
            settled_status(base_url, events)
            nothing_left_running(captures)(config)
            return events

        events = through_the_service_entry(
            tmp_path, config, child_cleanup, monkeypatch, post_and_settle
        )

    assert (wire_shape(events), len(app.calls)) == (wire, calls_at_the_app)
