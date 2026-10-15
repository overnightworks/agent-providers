"""The agent service's streamed run contract, owned by ``agent_client``.

These tests pin decision record 0001, H6 and its A0b sub-ruling: the closed
wire union, the run-stream rules, and the tool effect class that decides how
a cut-short run ends.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from wire_builders import (
    OTHER_RUN_ID,
    RUN_ID,
    call,
    cancelled,
    heartbeat,
    interrupted,
    result,
    started,
    succeeded,
    wire,
)

from agent_client.run_stream import RunStream, StreamViolation, ViolationKind, parse_event
from agent_client.tools import ToolDeclaration, ToolEffect
from agent_client.wire import (
    MAX_MESSAGE_LENGTH,
    PROTOCOL_VERSION,
    FailureCode,
    RejectionCode,
    RunEvent,
    RunRejection,
    RunRequest,
    TurnRole,
)
from agent_providers.text_tool_protocol import render_tool_catalog
from agent_providers.tools import ToolCatalog, anthropic_tool_schemas, openai_tool_schemas


def _declaration(name: str, **fields: object) -> ToolDeclaration:
    return ToolDeclaration(
        name=name,
        description=f"{name} does one thing",
        parameters={"type": "object", "properties": {}},
        **fields,
    )


def test_a_tool_declared_without_an_effect_counts_as_side_effecting() -> None:
    assert _declaration("write_lyrics").effect is ToolEffect.SIDE_EFFECTING


@pytest.mark.parametrize(
    "project", [anthropic_tool_schemas, openai_tool_schemas, render_tool_catalog]
)
def test_the_effect_class_never_reaches_a_model(project) -> None:
    catalog = ToolCatalog(
        tools=(
            _declaration("read_song", effect=ToolEffect.READ_ONLY),
            _declaration("write_lyrics", effect=ToolEffect.SIDE_EFFECTING),
        )
    )

    seen_by_the_model = json.dumps(project(catalog))

    for leak in ("effect", ToolEffect.READ_ONLY, ToolEffect.SIDE_EFFECTING):
        assert leak not in seen_by_the_model


WIRE_VARIANTS = {
    "run_started": wire("run_started", 0, heartbeat_interval_s=15),
    "assistant_text": wire("assistant_text", 1, text="A first verse."),
    "tool_call": wire(
        "tool_call",
        2,
        tool_use_id="call-1",
        name="write_lyrics",
        input={"verse": 1},
        effect="side_effecting",
    ),
    "tool_result": wire("tool_result", 3, tool_use_id="call-1", content="saved", is_error=False),
    "heartbeat": wire("heartbeat", 4),
    "succeeded": wire("succeeded", 5, text="Done."),
    "failed": wire("failed", 5, code="provider_error", message="The provider refused."),
    "cancelled": wire("cancelled", 5),
}


@pytest.mark.parametrize("raw", WIRE_VARIANTS.values(), ids=WIRE_VARIANTS.keys())
def test_every_variant_round_trips_through_its_json(raw: dict[str, object]) -> None:
    sent = json.dumps(raw)

    event = parse_event(json.loads(sent))

    assert not isinstance(event, StreamViolation), event
    assert json.loads(event.model_dump_json()) == raw


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        pytest.param(
            wire("thinking", 1, text="hm"), ViolationKind.UNKNOWN_TYPE, id="unknown type"
        ),
        pytest.param(
            {**wire("heartbeat", 1), "v": 2}, ViolationKind.UNSUPPORTED_VERSION, id="newer version"
        ),
        pytest.param(
            {**wire("assistant_text", 1, text="hi", tone="warm"), "v": 2},
            ViolationKind.UNSUPPORTED_VERSION,
            id="newer version with a field this client does not know",
        ),
        pytest.param(
            {**wire("heartbeat", 1), "v": True},
            ViolationKind.UNSUPPORTED_VERSION,
            id="version true",
        ),
        pytest.param(
            {**wire("heartbeat", 1), "v": 1.0},
            ViolationKind.UNSUPPORTED_VERSION,
            id="version as a float",
        ),
        pytest.param(
            wire("assistant_text", 1, text="hi", tone="warm"),
            ViolationKind.SCHEMA_MISMATCH,
            id="extra field",
        ),
        pytest.param(wire("heartbeat", "1"), ViolationKind.SCHEMA_MISMATCH, id="seq as string"),
        pytest.param(
            wire("run_started", 3, heartbeat_interval_s=15),
            ViolationKind.SCHEMA_MISMATCH,
            id="run_started after seq 0",
        ),
        pytest.param(
            {
                "v": PROTOCOL_VERSION,
                "run_id": str(RUN_ID),
                "type": "run_started",
                "heartbeat_interval_s": 15,
            },
            ViolationKind.SCHEMA_MISMATCH,
            id="run_started without seq",
        ),
        pytest.param(
            wire("run_started", False, heartbeat_interval_s=15),
            ViolationKind.SCHEMA_MISMATCH,
            id="run_started with seq false",
        ),
        pytest.param(
            wire("failed", 5, code="crashed", message="x"),
            ViolationKind.SCHEMA_MISMATCH,
            id="failure code outside the closed set",
        ),
        pytest.param(
            wire("failed", 5, code="internal_error", message="x" * (MAX_MESSAGE_LENGTH + 1)),
            ViolationKind.SCHEMA_MISMATCH,
            id="message too long",
        ),
        pytest.param(
            {**WIRE_VARIANTS["tool_call"], "tool_use_id": ""},
            ViolationKind.SCHEMA_MISMATCH,
            id="tool call with an empty tool_use_id",
        ),
        pytest.param(
            {**WIRE_VARIANTS["tool_result"], "tool_use_id": ""},
            ViolationKind.SCHEMA_MISMATCH,
            id="tool result with an empty tool_use_id",
        ),
        pytest.param(
            {**WIRE_VARIANTS["succeeded"], "text": ""},
            ViolationKind.SCHEMA_MISMATCH,
            id="succeeded with an empty text",
        ),
    ],
)
def test_an_event_off_the_contract_names_its_violation(
    raw: dict[str, object], kind: ViolationKind
) -> None:
    violation = parse_event(raw)

    assert isinstance(violation, StreamViolation)
    assert violation.kind is kind


def test_an_unknown_type_is_named_without_an_empty_location() -> None:
    violation = parse_event(wire("thinking", 1, text="hm"))

    assert isinstance(violation, StreamViolation)
    assert "'thinking'" in violation.detail
    assert not violation.detail.startswith(":")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="NaN"),
        pytest.param(float("inf"), id="+Inf"),
        pytest.param(float("-inf"), id="-Inf"),
        pytest.param({"chorus"}, id="a set"),
        pytest.param(datetime(2026, 10, 15, tzinfo=UTC), id="a datetime"),
        pytest.param("\ud800", id="a lone surrogate"),
        pytest.param({"\ud800": "x"}, id="a lone surrogate in a key"),
        pytest.param([{"bar": float("nan")}], id="NaN nested in a list"),
    ],
)
def test_tool_input_that_is_not_json_is_refused(value: object) -> None:
    with pytest.raises(ValidationError):
        call(1, "call-1", tool_input={"value": value})


def test_nested_json_tool_input_is_accepted_unchanged() -> None:
    nested = {
        "verse": 1,
        "tempo": 92.5,
        "draft": True,
        "note": None,
        "lines": ["Über Grün", {"rhyme": ["a", "b"]}],
    }

    tool_call = call(1, "call-1", tool_input=nested)

    assert json.loads(tool_call.model_dump_json())["input"] == nested


REJECTION = {"v": PROTOCOL_VERSION, "code": "busy", "message": "Another run is active."}


@pytest.mark.parametrize("code", RejectionCode)
def test_a_rejection_body_round_trips_through_its_json(code: RejectionCode) -> None:
    body = {**REJECTION, "code": code.value}

    rejection = RunRejection.model_validate_json(json.dumps(body))

    assert rejection.code is code
    assert json.loads(rejection.model_dump_json()) == body


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({**REJECTION, "v": True}, id="version true"),
        pytest.param({**REJECTION, "v": 1.0}, id="version as a float"),
        pytest.param({"code": "busy", "message": "Another run is active."}, id="no version"),
        pytest.param(
            {**REJECTION, "message": "x" * (MAX_MESSAGE_LENGTH + 1)}, id="message too long"
        ),
    ],
)
def test_a_rejection_body_off_the_contract_is_refused(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RunRejection.model_validate_json(json.dumps(body))


REQUEST = {
    "v": PROTOCOL_VERSION,
    "purpose": "cowriter",
    "turn": {
        "system": "You are a co-writer.",
        "messages": [
            {"role": "user", "text": "Write a first verse."},
            {"role": "assistant", "text": "Ein erster Vers."},
            {"role": "user", "text": "Now a chorus."},
        ],
    },
}


def test_a_run_request_round_trips_through_its_json() -> None:
    request = RunRequest.model_validate_json(json.dumps(REQUEST))

    assert request.turn.messages[1].role is TurnRole.ASSISTANT
    assert json.loads(request.model_dump_json()) == REQUEST


@pytest.mark.parametrize(
    "field", ["provider", "model", "route", "user_id", "mcp_url", "mcp_token", "tools"]
)
def test_a_run_request_naming_what_the_service_decides_is_refused(field: str) -> None:
    with pytest.raises(ValidationError):
        RunRequest.model_validate_json(json.dumps({**REQUEST, field: "chosen by the client"}))


_SHORT_DETAIL_LENGTH = 80


@pytest.mark.parametrize(
    ("raw", "named_as"),
    [
        pytest.param(
            {**wire("heartbeat", 1), "v": "9" * 2000}, "protocol version '9999", id="oversized"
        ),
        pytest.param(
            {key: value for key, value in wire("heartbeat", 1).items() if key != "v"},
            "no protocol version",
            id="absent",
        ),
    ],
)
def test_an_unsupported_version_is_named_in_a_short_detail(
    raw: dict[str, object], named_as: str
) -> None:
    violation = parse_event(raw)

    assert isinstance(violation, StreamViolation)
    assert violation.kind is ViolationKind.UNSUPPORTED_VERSION
    assert violation.detail.startswith(named_as)
    assert len(violation.detail) < _SHORT_DETAIL_LENGTH


def _accept_all(stream: RunStream, events: list[RunEvent]) -> list[ViolationKind | None]:
    return [_kind(stream.accept(event)) for event in events]


def _kind(violation: StreamViolation | None) -> ViolationKind | None:
    return None if violation is None else violation.kind


def test_a_well_formed_run_breaks_no_rule() -> None:
    stream = RunStream()
    events = [
        started(),
        heartbeat(1),
        call(2, "call-1", ToolEffect.READ_ONLY),
        result(3, "call-1"),
        call(4, "call-2"),
        succeeded(5),
    ]

    assert _accept_all(stream, events) == [None] * len(events)
    assert stream.close() is None


@pytest.mark.parametrize(
    ("events", "kind"),
    [
        pytest.param([heartbeat(0)], ViolationKind.MISSING_RUN_STARTED, id="missing run_started"),
        pytest.param(
            [started(), heartbeat(1, run_id=OTHER_RUN_ID)],
            ViolationKind.RUN_ID_CHANGED,
            id="run id changed",
        ),
        pytest.param([started(), heartbeat(2)], ViolationKind.SEQUENCE_GAP, id="seq skipped"),
        pytest.param(
            [started(), heartbeat(1), heartbeat(1)],
            ViolationKind.SEQUENCE_GAP,
            id="seq repeated",
        ),
        pytest.param([started(), started()], ViolationKind.SEQUENCE_GAP, id="second run_started"),
        pytest.param(
            [started(), succeeded(1), heartbeat(2)],
            ViolationKind.EVENT_AFTER_TERMINAL,
            id="event after terminal",
        ),
        pytest.param(
            [started(), succeeded(1), cancelled(2)],
            ViolationKind.EVENT_AFTER_TERMINAL,
            id="second terminal",
        ),
        pytest.param(
            [started(), call(1, "call-1", ToolEffect.READ_ONLY), call(2, "call-1")],
            ViolationKind.DUPLICATE_TOOL_USE_ID,
            id="duplicate tool_use_id",
        ),
        pytest.param(
            [started(), result(1, "call-1")],
            ViolationKind.UNKNOWN_TOOL_USE_ID,
            id="result without a call",
        ),
        pytest.param(
            [started(), call(1, "call-1"), cancelled(2)],
            ViolationKind.CANCELLED_AFTER_SIDE_EFFECT,
            id="cancelled after a side-effecting call",
        ),
        pytest.param(
            [started(), call(1, "call-1"), result(2, "call-1"), result(3, "call-1")],
            ViolationKind.DUPLICATE_TOOL_RESULT,
            id="second result for one call",
        ),
        pytest.param(
            [started(), call(1, "call-1", ToolEffect.READ_ONLY), interrupted(2)],
            ViolationKind.INTERRUPTED_WITHOUT_SIDE_EFFECT,
            id="interrupted without a side-effecting call",
        ),
    ],
)
def test_the_event_that_breaks_a_stream_rule_names_it(
    events: list[RunEvent], kind: ViolationKind
) -> None:
    *leading, breaking = events
    stream = RunStream()

    assert _accept_all(stream, leading) == [None] * len(leading)
    assert _accept_all(stream, [breaking]) == [kind]


def test_a_stream_that_ends_without_a_terminal_is_truncated() -> None:
    stream = RunStream()
    _accept_all(stream, [started(), call(1, "call-1")])

    assert _kind(stream.close()) is ViolationKind.TRUNCATED


CANCELLED = {"type": "cancelled"}
INTERRUPTED = {"type": "failed", "code": FailureCode.INTERRUPTED.value}


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        pytest.param([], CANCELLED, id="no tool call"),
        pytest.param([ToolEffect.READ_ONLY], CANCELLED, id="only read-only calls"),
        pytest.param(
            [ToolEffect.READ_ONLY, ToolEffect.SIDE_EFFECTING],
            INTERRUPTED,
            id="a side-effecting call",
        ),
    ],
)
def test_a_cut_short_run_ends_by_the_effect_of_its_calls(
    calls: list[ToolEffect], expected: dict[str, str]
) -> None:
    stream = RunStream()
    stream.accept(started())
    for seq, effect in enumerate(calls, start=1):
        stream.accept(call(seq, f"call-{seq}", effect))

    terminal = stream.cancellation_terminal()

    assert terminal.model_dump(mode="json", include=set(expected)) == expected
    assert stream.accept(terminal) is None
    assert stream.close() is None


@pytest.mark.parametrize(
    "events",
    [
        pytest.param([], id="a run that never started"),
        pytest.param([started(), succeeded(1)], id="a run that already ended"),
    ],
)
def test_only_a_running_run_can_be_cut_short(events: list[RunEvent]) -> None:
    stream = RunStream()
    _accept_all(stream, events)

    with pytest.raises(RuntimeError):
        stream.cancellation_terminal()
