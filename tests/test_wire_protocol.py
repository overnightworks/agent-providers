"""The agent service's streamed run contract, owned by ``agent_client``.

These tests pin decision record 0001, H6 and its A0b sub-ruling: the closed
wire union, the run-stream rules, and the tool effect class that decides how
a cut-short run ends.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_client.run_stream import RunStream, StreamViolation, ViolationKind, parse_event
from agent_client.tools import ToolDeclaration, ToolEffect
from agent_client.wire import (
    MAX_MESSAGE_LENGTH,
    PROTOCOL_VERSION,
    Cancelled,
    Failed,
    FailureCode,
    Heartbeat,
    RejectionCode,
    RunEvent,
    RunRejection,
    RunStarted,
    Succeeded,
    ToolCall,
    ToolResult,
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


RUN_ID = UUID("8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01")
OTHER_RUN_ID = UUID("0d5e7f90-1a2b-4c3d-8e4f-5a6b7c8d9e02")


def _wire(type_: str, seq: int, **payload: object) -> dict[str, object]:
    return {"v": PROTOCOL_VERSION, "run_id": str(RUN_ID), "seq": seq, "type": type_, **payload}


WIRE_VARIANTS = {
    "run_started": _wire("run_started", 0, heartbeat_interval_s=15),
    "assistant_text": _wire("assistant_text", 1, text="A first verse."),
    "tool_call": _wire(
        "tool_call",
        2,
        tool_use_id="call-1",
        name="write_lyrics",
        input={"verse": 1},
        effect="side_effecting",
    ),
    "tool_result": _wire("tool_result", 3, tool_use_id="call-1", content="saved", is_error=False),
    "heartbeat": _wire("heartbeat", 4),
    "succeeded": _wire("succeeded", 5, text="Done."),
    "failed": _wire("failed", 5, code="provider_error", message="The provider refused."),
    "cancelled": _wire("cancelled", 5),
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
            _wire("thinking", 1, text="hm"), ViolationKind.UNKNOWN_TYPE, id="unknown type"
        ),
        pytest.param(
            {**_wire("heartbeat", 1), "v": 2}, ViolationKind.UNSUPPORTED_VERSION, id="newer version"
        ),
        pytest.param(
            {**_wire("assistant_text", 1, text="hi", tone="warm"), "v": 2},
            ViolationKind.UNSUPPORTED_VERSION,
            id="newer version with a field this client does not know",
        ),
        pytest.param(
            {**_wire("heartbeat", 1), "v": True},
            ViolationKind.UNSUPPORTED_VERSION,
            id="version true",
        ),
        pytest.param(
            {**_wire("heartbeat", 1), "v": 1.0},
            ViolationKind.UNSUPPORTED_VERSION,
            id="version as a float",
        ),
        pytest.param(
            _wire("assistant_text", 1, text="hi", tone="warm"),
            ViolationKind.SCHEMA_MISMATCH,
            id="extra field",
        ),
        pytest.param(_wire("heartbeat", "1"), ViolationKind.SCHEMA_MISMATCH, id="seq as string"),
        pytest.param(
            _wire("run_started", 3, heartbeat_interval_s=15),
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
            _wire("run_started", False, heartbeat_interval_s=15),
            ViolationKind.SCHEMA_MISMATCH,
            id="run_started with seq false",
        ),
        pytest.param(
            _wire("failed", 5, code="crashed", message="x"),
            ViolationKind.SCHEMA_MISMATCH,
            id="failure code outside the closed set",
        ),
        pytest.param(
            _wire("failed", 5, code="internal_error", message="x" * (MAX_MESSAGE_LENGTH + 1)),
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
    violation = parse_event(_wire("thinking", 1, text="hm"))

    assert isinstance(violation, StreamViolation)
    assert "'thinking'" in violation.detail
    assert not violation.detail.startswith(":")


REJECTION = {"v": PROTOCOL_VERSION, "code": "busy", "message": "Another run is active."}


def test_a_rejection_body_round_trips_through_its_json() -> None:
    rejection = RunRejection.model_validate_json(json.dumps(REJECTION))

    assert rejection.code is RejectionCode.BUSY
    assert json.loads(rejection.model_dump_json()) == REJECTION


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


_SHORT_DETAIL_LENGTH = 80


@pytest.mark.parametrize(
    ("raw", "named_as"),
    [
        pytest.param(
            {**_wire("heartbeat", 1), "v": "9" * 2000}, "protocol version '9999", id="oversized"
        ),
        pytest.param(
            {key: value for key, value in _wire("heartbeat", 1).items() if key != "v"},
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


def _started() -> RunStarted:
    return RunStarted(v=PROTOCOL_VERSION, run_id=RUN_ID, seq=0, heartbeat_interval_s=15)


def _call(seq: int, tool_use_id: str, effect: ToolEffect = ToolEffect.SIDE_EFFECTING) -> ToolCall:
    return ToolCall(
        v=PROTOCOL_VERSION,
        run_id=RUN_ID,
        seq=seq,
        tool_use_id=tool_use_id,
        name="tool",
        input={},
        effect=effect,
    )


def _result(seq: int, tool_use_id: str) -> ToolResult:
    return ToolResult(
        v=PROTOCOL_VERSION,
        run_id=RUN_ID,
        seq=seq,
        tool_use_id=tool_use_id,
        content="ok",
        is_error=False,
    )


def _heartbeat(seq: int, run_id: UUID = RUN_ID) -> Heartbeat:
    return Heartbeat(v=PROTOCOL_VERSION, run_id=run_id, seq=seq)


def _succeeded(seq: int) -> Succeeded:
    return Succeeded(v=PROTOCOL_VERSION, run_id=RUN_ID, seq=seq, text="Done.")


def _cancelled(seq: int) -> Cancelled:
    return Cancelled(v=PROTOCOL_VERSION, run_id=RUN_ID, seq=seq)


def _interrupted(seq: int) -> Failed:
    return Failed(
        v=PROTOCOL_VERSION,
        run_id=RUN_ID,
        seq=seq,
        code=FailureCode.INTERRUPTED,
        message="The run was cut short.",
    )


def _accept_all(stream: RunStream, events: list[RunEvent]) -> list[ViolationKind | None]:
    return [_kind(stream.accept(event)) for event in events]


def _kind(violation: StreamViolation | None) -> ViolationKind | None:
    return None if violation is None else violation.kind


def test_a_well_formed_run_breaks_no_rule() -> None:
    stream = RunStream()
    events = [
        _started(),
        _heartbeat(1),
        _call(2, "call-1", ToolEffect.READ_ONLY),
        _result(3, "call-1"),
        _call(4, "call-2"),
        _succeeded(5),
    ]

    assert _accept_all(stream, events) == [None] * len(events)
    assert stream.close() is None


@pytest.mark.parametrize(
    ("events", "kind"),
    [
        pytest.param([_heartbeat(0)], ViolationKind.MISSING_RUN_STARTED, id="missing run_started"),
        pytest.param(
            [_started(), _heartbeat(1, run_id=OTHER_RUN_ID)],
            ViolationKind.RUN_ID_CHANGED,
            id="run id changed",
        ),
        pytest.param([_started(), _heartbeat(2)], ViolationKind.SEQUENCE_GAP, id="seq skipped"),
        pytest.param(
            [_started(), _heartbeat(1), _heartbeat(1)],
            ViolationKind.SEQUENCE_GAP,
            id="seq repeated",
        ),
        pytest.param([_started(), _started()], ViolationKind.SEQUENCE_GAP, id="second run_started"),
        pytest.param(
            [_started(), _succeeded(1), _heartbeat(2)],
            ViolationKind.EVENT_AFTER_TERMINAL,
            id="event after terminal",
        ),
        pytest.param(
            [_started(), _succeeded(1), _cancelled(2)],
            ViolationKind.EVENT_AFTER_TERMINAL,
            id="second terminal",
        ),
        pytest.param(
            [_started(), _call(1, "call-1", ToolEffect.READ_ONLY), _call(2, "call-1")],
            ViolationKind.DUPLICATE_TOOL_USE_ID,
            id="duplicate tool_use_id",
        ),
        pytest.param(
            [_started(), _result(1, "call-1")],
            ViolationKind.UNKNOWN_TOOL_USE_ID,
            id="result without a call",
        ),
        pytest.param(
            [_started(), _call(1, "call-1"), _cancelled(2)],
            ViolationKind.CANCELLED_AFTER_SIDE_EFFECT,
            id="cancelled after a side-effecting call",
        ),
        pytest.param(
            [_started(), _call(1, "call-1"), _result(2, "call-1"), _result(3, "call-1")],
            ViolationKind.DUPLICATE_TOOL_RESULT,
            id="second result for one call",
        ),
        pytest.param(
            [_started(), _call(1, "call-1", ToolEffect.READ_ONLY), _interrupted(2)],
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
    _accept_all(stream, [_started(), _call(1, "call-1")])

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
    stream.accept(_started())
    for seq, effect in enumerate(calls, start=1):
        stream.accept(_call(seq, f"call-{seq}", effect))

    terminal = stream.cancellation_terminal()

    assert terminal.model_dump(mode="json", include=set(expected)) == expected
    assert stream.accept(terminal) is None
    assert stream.close() is None


@pytest.mark.parametrize(
    "events",
    [
        pytest.param([], id="a run that never started"),
        pytest.param([_started(), _succeeded(1)], id="a run that already ended"),
    ],
)
def test_only_a_running_run_can_be_cut_short(events: list[RunEvent]) -> None:
    stream = RunStream()
    _accept_all(stream, events)

    with pytest.raises(RuntimeError):
        stream.cancellation_terminal()
