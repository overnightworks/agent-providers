"""An admitted run's wire stream: the core events it maps, and how it always ends once.

S5: run_started, the mapped events, then exactly one terminal, decoded by the
client with no violation. S6: tool input that is not JSON, or cannot be
written, never reaches the wire. S7: a route with no runner fails as
``provider_unavailable``, never as a success.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid

import pytest
from provider_test_support import (
    A_CANARY,
    a_call_to_an_undeclared_tool_carrying,
    drive_turn,
    formatted_log_text,
    grok_cli_outcome,
    grok_cli_runner,
    grok_tool_round_lines,
)
from service_support import (
    ScriptedRunner,
    decoded_events,
    get_run,
    reaches_text,
    run_body,
    run_headers,
    served,
    service_config,
    with_placeholder_run_id,
)
from test_service_entrance import MINT

from agent_client.tools import ToolEffect
from agent_client.wire import (
    MAX_EVENT_BYTES,
    MAX_TOOL_INPUT_DEPTH,
    Failed,
    FailureCode,
    RunEvent,
    RunRequest,
    RunState,
    RunStatus,
    Succeeded,
    ToolCall,
)
from agent_providers.errors import TextToolCallRejectedError
from agent_providers.events import (
    AssistantTextEvent,
    FinalEvent,
    StreamEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_providers.grok import transport as grok_cli_adapter
from agent_service import run_events
from agent_service.app import create_app
from agent_service.config import RunConfig
from agent_service.identity import Principal
from agent_service.routing import Cli
from agent_service.run_events import run_frames
from agent_service.run_liveness import LoopClock
from agent_service.run_records import RunRecords
from agent_service.runners import AdmittedRun, Runner

SONGMAKER = service_config().apps["songmaker"]


def _streamed(runner: Runner | None, records: RunRecords | None = None) -> bytes:
    admitted = AdmittedRun(
        run_id=uuid.uuid4(),
        app="songmaker",
        principal=Principal("https://keycloak.test/realms/agent-test", "user-alice"),
        request=RunRequest.model_validate(run_body()),
        route=Cli("claude-main"),
        capability=None,
    )

    async def collected() -> bytes:
        store = records or RunRecords()
        frames = run_frames(admitted, runner, SONGMAKER, RunConfig(), LoopClock(), store)
        return b"".join([frame async for frame in frames])

    return asyncio.run(collected())


def _scripted(*events: StreamEvent | Exception) -> bytes:
    return _streamed(ScriptedRunner(events))


def _terminal(raw: bytes) -> RunEvent:
    return decoded_events(raw)[-1]


def _failed_with(raw: bytes, code: FailureCode) -> None:
    terminal = _terminal(raw)
    assert isinstance(terminal, Failed), terminal
    assert terminal.code is code


SCRIPT = (
    AssistantTextEvent(text="Let me read the song."),
    ToolCallEvent(tool_use_id="call-1", name="read_song", input={"song_id": 7}),
    ToolResultEvent(tool_use_id="call-1", content="Verse one"),
    FinalEvent(text="Here is a second verse."),
)

RUN = "00000000-0000-4000-8000-000000000000"
SCRIPT_FRAMES = (
    "id: 0\nevent: run_started\n"
    f'data: {{"v":1,"run_id":"{RUN}","seq":0,"type":"run_started","heartbeat_interval_s":15}}\n\n'
    "id: 1\nevent: assistant_text\n"
    f'data: {{"v":1,"run_id":"{RUN}","seq":1,"type":"assistant_text",'
    '"text":"Let me read the song."}\n\n'
    "id: 2\nevent: tool_call\n"
    f'data: {{"v":1,"run_id":"{RUN}","seq":2,"type":"tool_call","tool_use_id":"call-1",'
    '"name":"read_song","input":{"song_id":7},"effect":"read_only"}\n\n'
    "id: 3\nevent: tool_result\n"
    f'data: {{"v":1,"run_id":"{RUN}","seq":3,"type":"tool_result","tool_use_id":"call-1",'
    '"content":"Verse one","is_error":false}\n\n'
    "id: 4\nevent: succeeded\n"
    f'data: {{"v":1,"run_id":"{RUN}","seq":4,"type":"succeeded",'
    '"text":"Here is a second verse."}\n\n'
).encode()


def test_a_scripted_run_streams_its_events_as_the_frozen_frames():
    raw = _scripted(*SCRIPT)

    assert with_placeholder_run_id(raw) == SCRIPT_FRAMES


def test_a_scripted_run_decodes_without_violation_and_ends_once():
    events = decoded_events(_scripted(*SCRIPT))

    assert [event.type for event in events] == [
        "run_started",
        "assistant_text",
        "tool_call",
        "tool_result",
        "succeeded",
    ]


def test_events_after_the_final_answer_never_reach_the_wire():
    raw = _scripted(
        FinalEvent(text="Done."), FinalEvent(text="Again."), AssistantTextEvent(text="x")
    )

    terminal = _terminal(raw)
    assert isinstance(terminal, Succeeded)
    assert terminal.text == "Done."


@pytest.mark.parametrize(
    ("tool", "effect"),
    [
        ("read_song", ToolEffect.READ_ONLY),
        ("edit_song", ToolEffect.SIDE_EFFECTING),
        ("unlisted_tool", ToolEffect.SIDE_EFFECTING),
    ],
)
def test_a_tool_calls_effect_comes_from_the_apps_tool_list(tool, effect):
    events = decoded_events(
        _scripted(ToolCallEvent(tool_use_id="c", name=tool, input={}), FinalEvent(text="Done."))
    )

    call = next(event for event in events if isinstance(event, ToolCall))
    assert call.effect is effect


def test_a_valid_tool_input_reaches_the_wire_unchanged():
    tool_input = {"title": "Ünïcode", "lines": [1, 2.5, None, True], "nested": {"deep": ["x"]}}
    events = decoded_events(
        _scripted(
            ToolCallEvent(tool_use_id="c", name="read_song", input=tool_input),
            FinalEvent(text="Done."),
        )
    )

    call = next(event for event in events if isinstance(event, ToolCall))
    assert call.input == tool_input


def _nested(depth: int) -> dict:
    value: dict = {}
    for _ in range(depth - 1):
        value = {"a": value}
    return value


@pytest.mark.parametrize(
    "tool_input",
    [
        pytest.param({"score": math.nan}, id="nan"),
        pytest.param({"score": math.inf}, id="infinity"),
        pytest.param({"when": object()}, id="not-a-json-value"),
        pytest.param(_nested(MAX_TOOL_INPUT_DEPTH + 1), id="too-deep"),
        pytest.param({"lyrics": "x" * MAX_EVENT_BYTES}, id="too-large"),
    ],
)
def test_tool_input_the_wire_cannot_carry_fails_the_run_as_a_provider_error(tool_input):
    raw = _scripted(
        ToolCallEvent(tool_use_id="c", name="edit_song", input=tool_input),
        FinalEvent(text="never sent"),
    )

    _failed_with(raw, FailureCode.PROVIDER_ERROR)
    assert b'"tool_call"' not in raw


@pytest.mark.parametrize(
    "script",
    [
        pytest.param((AssistantTextEvent(text="half an answer"),), id="ended-without-final"),
        pytest.param((RuntimeError("the CLI died"),), id="runner-raised"),
        pytest.param((FinalEvent(text=""),), id="empty-final-answer"),
        pytest.param(
            (ToolResultEvent(tool_use_id="nobody-asked", content="x"), FinalEvent(text="x")),
            id="result-for-no-call",
        ),
        pytest.param(
            (
                ToolCallEvent(tool_use_id="c", name="read_song"),
                ToolCallEvent(tool_use_id="c", name="read_song"),
                FinalEvent(text="x"),
            ),
            id="call-id-twice",
        ),
        pytest.param((StreamEvent(type="x" * 600),), id="unmappable-event-with-a-long-type"),
    ],
)
def test_a_runner_that_breaks_the_run_fails_it_as_a_provider_error(script):
    _failed_with(_scripted(*script), FailureCode.PROVIDER_ERROR)


def test_an_empty_text_fragment_is_not_sent():
    events = decoded_events(_scripted(AssistantTextEvent(text=""), FinalEvent(text="Done.")))

    assert [event.type for event in events] == ["run_started", "succeeded"]


def test_a_route_without_a_runner_fails_as_provider_unavailable_never_succeeds():
    raw = _streamed(None)

    assert [event.type for event in decoded_events(raw)] == ["run_started", "failed"]
    _failed_with(raw, FailureCode.PROVIDER_UNAVAILABLE)


def test_a_refused_tool_call_reaches_neither_the_frames_nor_the_service_log(monkeypatch, caplog):
    monkeypatch.setattr(
        grok_cli_adapter,
        "run_cli_bounded",
        grok_cli_runner(
            grok_tool_round_lines(a_call_to_an_undeclared_tool_carrying()),
            grok_cli_outcome(),
            [],
        ),
    )
    refused = drive_turn("grok").error
    assert isinstance(refused, TextToolCallRejectedError)
    caplog.set_level(logging.DEBUG)

    raw = _scripted(refused)

    assert [event.type for event in decoded_events(raw)] == ["run_started", "failed"]
    terminal = _terminal(raw)
    assert isinstance(terminal, Failed)
    assert (terminal.code, terminal.message) == (
        FailureCode.PROVIDER_ERROR, "The provider failed during the run.",
    )
    assert A_CANARY.encode() not in raw
    service_records = [record for record in caplog.records if record.name == run_events.__name__]
    assert [record.levelno for record in service_records] == [logging.ERROR]
    assert A_CANARY not in formatted_log_text(service_records)


def test_the_run_record_of_a_refused_tool_call_holds_the_failed_ending_but_not_the_refused_text(
    monkeypatch,
):
    monkeypatch.setattr(
        grok_cli_adapter,
        "run_cli_bounded",
        grok_cli_runner(
            grok_tool_round_lines(a_call_to_an_undeclared_tool_carrying()),
            grok_cli_outcome(),
            [],
        ),
    )
    refused = drive_turn("grok").error
    assert isinstance(refused, TextToolCallRejectedError)
    records = RunRecords()

    run_id = decoded_events(_streamed(ScriptedRunner([refused]), records))[0].run_id

    app = create_app(service_config(), records=records, fetch_key_set=MINT.fetch_key_set)
    with served(app) as base_url:
        response = get_run(base_url, run_headers(MINT.workload(), MINT.user()), str(run_id))
    status = RunStatus.model_validate_json(response.content)
    assert (status.state, status.failure_code) == (RunState.FAILED, FailureCode.PROVIDER_ERROR)
    assert A_CANARY.encode() not in response.content
    assert not reaches_text(records, [A_CANARY])
