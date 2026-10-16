"""A run that makes too many tool calls or sends too many bytes ends with a typed failure.

Decision record 0001, H6 (A1a3 piece 2). Every scenario drives ``run_frames``
through ``Drive`` with a ``GatedRunner`` whose events the test releases; no
limit needs the clock to move for its own sake, so nothing sleeps.
"""

from __future__ import annotations

import pytest
from service_support import (
    Drive,
    ScriptedRunner,
    TokenMint,
    decoded_events,
    driven,
    in_loop,
    post_run,
    run_headers,
    served,
    service_config,
)

from agent_client.wire import MAX_EVENT_BYTES, Failed, FailureCode, Succeeded
from agent_providers.events import (
    AssistantTextEvent,
    FinalEvent,
    StreamEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_service.app import create_app
from agent_service.config import RunConfig
from agent_service.run_events import TOO_MANY_TOOL_CALLS_MESSAGE, TOO_MUCH_DATA_MESSAGE
from agent_service.run_liveness import LIMIT_EXCEEDED_MESSAGE
from agent_service.runners import Runners

TEXT = AssistantTextEvent(text="Still here.")
FINAL = FinalEvent(text="Done.")
BYTE_COUNTED_TYPES = {"assistant_text", "tool_call", "tool_result", "succeeded"}


def call(tool_use_id: str, name: str = "read_song", **tool_input: object) -> ToolCallEvent:
    return ToolCallEvent(tool_use_id=tool_use_id, name=name, input=tool_input)


def result(tool_use_id: str, content: str = "ok") -> ToolResultEvent:
    return ToolResultEvent(tool_use_id=tool_use_id, content=content)


def calls_with_results(count: int) -> list[StreamEvent]:
    return [event for n in range(count) for event in (call(f"c{n}"), result(f"c{n}"))]


async def run_released(events: list[StreamEvent], runs: RunConfig) -> Drive:
    async with driven(runs=runs) as drive:
        for event in events:
            drive.runner.release(event)
        await drive.finished()
    return drive


def counted_bytes(drive: Drive) -> int:
    """What the budget counts: the runner-caused frames, SSE framing included."""
    sizes = zip(drive.types, drive.frame_sizes, strict=True)
    return sum(size for event_type, size in sizes if event_type in BYTE_COUNTED_TYPES)


def failure_of(drive: Drive) -> Failed:
    terminal = drive.terminal[1]
    assert isinstance(terminal, Failed), terminal
    return terminal


def sent_tool_use_ids(drive: Drive) -> list[str]:
    return [event.tool_use_id for _, event in drive.events if event.type == "tool_call"]


@in_loop
async def test_the_call_past_the_limit_is_not_sent_and_ends_the_run_as_limit_exceeded():
    events = [*calls_with_results(3), call("past"), TEXT, FINAL]

    drive = await run_released(events, RunConfig(max_tool_calls=3))

    assert drive.types == ["run_started", *["tool_call", "tool_result"] * 3, "failed"]
    assert drive.seqs == list(range(8))
    assert "past" not in sent_tool_use_ids(drive)
    assert failure_of(drive).code is FailureCode.LIMIT_EXCEEDED
    assert failure_of(drive).message == TOO_MANY_TOOL_CALLS_MESSAGE


@in_loop
async def test_calls_within_the_limit_with_text_and_a_final_answer_succeed():
    events = [*calls_with_results(3), TEXT, TEXT, FINAL]

    drive = await run_released(events, RunConfig(max_tool_calls=3))

    assert isinstance(drive.terminal[1], Succeeded)


@pytest.mark.parametrize(
    ("calls", "succeeds"),
    [pytest.param(30, True, id="the-thirtieth"), pytest.param(31, False, id="the-thirty-first")],
)
@in_loop
async def test_the_default_limit_is_thirty_tool_calls(calls, succeeds):
    drive = await run_released([*calls_with_results(calls), FINAL], RunConfig())

    assert isinstance(drive.terminal[1], Succeeded) is succeeds
    if not succeeds:
        assert failure_of(drive).code is FailureCode.LIMIT_EXCEEDED


@in_loop
async def test_a_call_over_the_limit_after_a_side_effect_is_still_limit_exceeded():
    events = [call("edit", "edit_song"), result("edit"), call("past"), FINAL]

    drive = await run_released(events, RunConfig(max_tool_calls=1))

    assert drive.types == ["run_started", "tool_call", "tool_result", "failed"]
    assert failure_of(drive).code is FailureCode.LIMIT_EXCEEDED


BYTE_SCRIPT = [TEXT, call("c0"), result("c0"), FINAL]


@pytest.mark.parametrize(
    ("margin", "succeeds"),
    [pytest.param(0, True, id="exact-fit"), pytest.param(-1, False, id="one-byte-over")],
)
@in_loop
async def test_the_byte_budget_counts_frames_up_to_its_exact_size(margin, succeeds):
    reference = await run_released(BYTE_SCRIPT, RunConfig())

    drive = await run_released(BYTE_SCRIPT, RunConfig(max_bytes=counted_bytes(reference) + margin))

    assert isinstance(drive.terminal[1], Succeeded) is succeeds
    if not succeeds:
        assert drive.types == [
            "run_started",
            "assistant_text",
            "tool_call",
            "tool_result",
            "failed",
        ]
        assert failure_of(drive).code is FailureCode.LIMIT_EXCEEDED
        assert failure_of(drive).message == TOO_MUCH_DATA_MESSAGE


@in_loop
async def test_a_frame_under_the_frame_cap_still_breaches_a_smaller_byte_budget():
    events = [call("c0"), result("c0", "x" * 200_000), FINAL]

    drive = await run_released(events, RunConfig(max_bytes=100_000))

    assert drive.types == ["run_started", "tool_call", "failed"]
    assert failure_of(drive).message == TOO_MUCH_DATA_MESSAGE


async def heartbeats_then_the_script(max_bytes: int | None) -> Drive:
    runs = (
        RunConfig(heartbeat_interval_s=1)
        if max_bytes is None
        else RunConfig(heartbeat_interval_s=1, max_bytes=max_bytes)
    )
    async with driven(runs=runs) as drive:
        await drive.run_to(12)
        for event in BYTE_SCRIPT:
            drive.runner.release(event)
        await drive.finished()
    return drive


@pytest.mark.parametrize(
    ("margin", "succeeds"),
    [pytest.param(0, True, id="exact-fit"), pytest.param(-1, False, id="one-byte-over")],
)
@in_loop
async def test_heartbeats_are_not_counted_even_when_they_lengthen_a_later_frame(margin, succeeds):
    reference = await heartbeats_then_the_script(None)
    heartbeat_free = await run_released(BYTE_SCRIPT, RunConfig())
    assert counted_bytes(reference) > counted_bytes(heartbeat_free)

    drive = await heartbeats_then_the_script(counted_bytes(reference) + margin)

    assert isinstance(drive.terminal[1], Succeeded) is succeeds
    assert drive.types.count("heartbeat") == 12


@pytest.mark.parametrize(
    ("past_the_limit", "expected_code"),
    [
        pytest.param(call("c0"), FailureCode.LIMIT_EXCEEDED, id="repeated-id"),
        pytest.param(
            call("c1", payload="x" * MAX_EVENT_BYTES), FailureCode.PROVIDER_ERROR, id="oversized"
        ),
    ],
)
@in_loop
async def test_a_call_past_the_limit_is_judged_by_the_frame_before_the_budget_before_the_rules(
    past_the_limit, expected_code
):
    drive = await run_released(
        [*calls_with_results(1), past_the_limit], RunConfig(max_tool_calls=1)
    )

    assert drive.seqs == [0, 1, 2, 3]
    assert failure_of(drive).code is expected_code


@in_loop
async def test_a_breach_stops_the_runner_before_a_waiting_client_takes_anything():
    async with driven(runs=RunConfig(max_tool_calls=1)) as drive:
        await drive.run_to(0)
        drive.stall()
        for event in [TEXT, call("c0"), call("past")]:
            drive.runner.release(event)
        await drive.run_to(0)
        assert drive.runner.closes == 1
        drive.resume()
        await drive.finished()

    assert drive.types == ["run_started", "assistant_text", "tool_call", "failed"]
    assert drive.runner.log.index("closed") < drive.runner.log.index("taken:tool_call")
    assert drive.seqs == [0, 1, 2, 3]


@in_loop
async def test_a_breach_behind_a_pending_frame_stays_unobserved_until_the_maximum():
    async with driven(runs=RunConfig(max_tool_calls=1)) as drive:
        await drive.run_to(0)
        drive.stall()
        for event in [TEXT, call("c0"), TEXT]:
            drive.runner.release(event)
        await drive.run_to(0)
        steps_before = drive.runner.steps
        drive.runner.release(call("past"))
        await drive.run_to(599)
        assert (drive.runner.steps, drive.runner.closes) == (steps_before, 0)
        drive.clock.advance_to(600)
        drive.resume()
        await drive.finished()

    assert failure_of(drive).message == LIMIT_EXCEEDED_MESSAGE
    assert "past" not in sent_tool_use_ids(drive)


@pytest.mark.parametrize(
    ("max_tool_calls", "event_at_599", "calls_sent", "expected_message"),
    [
        pytest.param(
            1,
            call("past"),
            ["c0"],
            TOO_MANY_TOOL_CALLS_MESSAGE,
            id="breaching-call-at-the-maximum",
        ),
        pytest.param(
            2,
            call("c1"),
            ["c0", "c1"],
            LIMIT_EXCEEDED_MESSAGE,
            id="call-within-budget-at-the-maximum",
        ),
    ],
)
@in_loop
async def test_an_event_yielded_at_the_maximum_is_judged_before_the_maximum(
    max_tool_calls, event_at_599, calls_sent, expected_message
):
    async with driven(runs=RunConfig(max_tool_calls=max_tool_calls)) as drive:
        await drive.speak_quietly_every(100, 400)
        await drive.run_to(500)
        drive.runner.release(call("c0"))
        drive.runner.release(result("c0"))
        await drive.run_to(599)
        drive.runner.release(event_at_599)
        drive.clock.advance_to(600)
        await drive.finished()

    assert sent_tool_use_ids(drive) == calls_sent
    assert failure_of(drive).message == expected_message
    assert drive.terminal[0] == 600


@in_loop
async def test_a_call_that_breaches_both_budgets_is_reported_as_too_many_calls():
    reference = await run_released([*calls_with_results(1), FINAL], RunConfig())
    two_frames = sum(reference.frame_sizes[1:3])

    drive = await run_released(
        [*calls_with_results(1), call("past")], RunConfig(max_tool_calls=1, max_bytes=two_frames)
    )

    assert failure_of(drive).message == TOO_MANY_TOOL_CALLS_MESSAGE


def test_a_served_run_refuses_the_call_past_the_configured_limit():
    mint = TokenMint()
    app = create_app(
        service_config(runs={"max_tool_calls": 1}),
        runners=Runners(cli=ScriptedRunner([*calls_with_results(1), call("past"), FINAL])),
        fetch_key_set=mint.fetch_key_set,
    )
    with served(app) as base_url:
        response = post_run(base_url, run_headers(mint.workload(), mint.user()))

    events = decoded_events(response.content)
    assert [event.type for event in events] == ["run_started", "tool_call", "tool_result", "failed"]
    assert isinstance(events[-1], Failed)
    assert events[-1].code is FailureCode.LIMIT_EXCEEDED
