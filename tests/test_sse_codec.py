"""The SSE framing of a run's events, owned by ``agent_client.sse``.

These tests pin the A0b sub-ruling of decision record 0001: one frame per
event, decoded the same however the bytes arrive, and the first frame or
event off the contract named once, with nothing decoded after it.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from wire_builders import call, heartbeat, result, started, succeeded, text

from agent_client.run_stream import StreamViolation, ViolationKind
from agent_client.sse import (
    Decoded,
    DecoderClosed,
    EventTooLarge,
    RunStreamDecoder,
    encode_event,
)
from agent_client.wire import (
    MAX_EVENT_BYTES,
    MAX_TOOL_INPUT_DEPTH,
    PROTOCOL_VERSION,
    AssistantText,
    Heartbeat,
    RejectionCode,
    RunEvent,
    RunRejection,
)

A_RUN: list[RunEvent] = [
    started(),
    text(1, "Ein erster Vers – über Grün."),
    call(2, "call-1", name="write_lyrics", tool_input={"verse": 1}),
    result(3, "call-1", content="saved\nline two"),
    heartbeat(4),
    succeeded(5),
]
ENCODED = b"".join(encode_event(event) for event in A_RUN)
OPENING = encode_event(A_RUN[0])
HEARTBEAT = encode_event(heartbeat(1))


def _tool_call_frame_with(constant: str) -> bytes:
    frame = encode_event(call(1, "call-1", tool_input={"x": 0}))
    return frame.replace(b'"x":0', b'"x":' + constant.encode())


def _decode(*chunks: bytes) -> list[Decoded]:
    decoder = RunStreamDecoder()
    decoded = [item for chunk in chunks for item in decoder.feed(chunk)]
    return decoded + decoder.close()


SPELLINGS = {
    "LF as encoded": ENCODED,
    "CRLF": ENCODED.replace(b"\n", b"\r\n"),
    "CR": ENCODED.replace(b"\n", b"\r"),
    "byte order mark": b"\xef\xbb\xbf" + ENCODED,
    "comments": b": opened\n\n" + ENCODED.replace(b"\n\n", b"\n: keep-alive\n\n"),
    "retry": b"retry: 3000\n\n" + ENCODED.replace(b"event:", b"retry: 1000\nevent:"),
}

UMLAUT_TEXT_FRAME = (
    b"id: 1\n"
    b"event: assistant_text\n"
    b'data: {"v":1,"run_id":"8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01","seq":1,'
    b'"type":"assistant_text","text":"Ein erster Vers \xe2\x80\x93 \xc3\xbcber Gr\xc3\xbcn."}\n'
    b"\n"
)


def test_an_event_travels_as_one_frame_of_compact_utf8_json() -> None:
    assert encode_event(A_RUN[1]) == UMLAUT_TEXT_FRAME
    assert _decode(OPENING, UMLAUT_TEXT_FRAME)[1] == A_RUN[1]


class _HeartbeatWithAnExtraField(Heartbeat):
    injected: str = "off the v1 contract"


@pytest.mark.parametrize(
    "not_an_event",
    [
        pytest.param(None, id="None"),
        pytest.param(
            RunRejection(v=PROTOCOL_VERSION, code=RejectionCode.BUSY, message="Busy."),
            id="a rejection",
        ),
        pytest.param(
            _HeartbeatWithAnExtraField(v=PROTOCOL_VERSION, run_id=A_RUN[0].run_id, seq=1),
            id="a subclass of an event",
        ),
    ],
)
def test_encoding_something_other_than_a_run_event_is_a_type_error(not_an_event: object) -> None:
    with pytest.raises(TypeError):
        encode_event(not_an_event)


@pytest.mark.parametrize("spelled", SPELLINGS.values(), ids=SPELLINGS.keys())
def test_a_run_decodes_the_same_wherever_its_bytes_split(spelled: bytes) -> None:
    for offset in range(len(spelled) + 1):
        assert _decode(spelled[:offset], spelled[offset:]) == A_RUN, offset


@pytest.mark.parametrize(
    ("frame", "kind"),
    [
        pytest.param(
            HEARTBEAT.replace(b"event:", b"name: beat\nevent:"),
            ViolationKind.MALFORMED_FRAME,
            id="unknown field",
        ),
        pytest.param(
            HEARTBEAT.replace(b"\n\n", b"\ndata: {}\n\n"),
            ViolationKind.MALFORMED_FRAME,
            id="second data line",
        ),
        pytest.param(HEARTBEAT.replace(b"id: 1\n", b""), ViolationKind.MALFORMED_FRAME, id="no id"),
        pytest.param(
            b"id: 1\nevent: heartbeat\ndata: \xff\n\n",
            ViolationKind.MALFORMED_FRAME,
            id="line not UTF-8",
        ),
        pytest.param(
            b"data: " + b"x" * MAX_EVENT_BYTES + b"\n\n",
            ViolationKind.OVERSIZED_FRAME,
            id="frame over the limit",
        ),
        pytest.param(
            HEARTBEAT.replace(b"data: {", b"data: {{"), ViolationKind.INVALID_JSON, id="not JSON"
        ),
        *(
            pytest.param(
                _tool_call_frame_with(constant),
                ViolationKind.INVALID_JSON,
                id=f"{constant} inside a well-formed tool call",
            )
            for constant in ("NaN", "Infinity", "-Infinity")
        ),
        pytest.param(
            b'id: 1\nevent: heartbeat\ndata: ["heartbeat"]\n\n',
            ViolationKind.INVALID_JSON,
            id="JSON array instead of an object",
        ),
        pytest.param(
            HEARTBEAT.replace(b"event: heartbeat", b"event: cancelled"),
            ViolationKind.EVENT_TYPE_MISMATCH,
            id="event field differs from type",
        ),
        pytest.param(
            HEARTBEAT.replace(b"id: 1", b"id: 7"),
            ViolationKind.ID_MISMATCH,
            id="id differs from seq",
        ),
        pytest.param(
            HEARTBEAT.replace(b'"v":1', b'"v":2'),
            ViolationKind.UNSUPPORTED_VERSION,
            id="event off the wire contract",
        ),
        pytest.param(
            encode_event(heartbeat(2)),
            ViolationKind.SEQUENCE_GAP,
            id="event breaking a stream rule",
        ),
    ],
)
def test_a_broken_frame_is_named_and_nothing_is_decoded_after_it(
    frame: bytes, kind: ViolationKind
) -> None:
    decoder = RunStreamDecoder()

    opening, violation = decoder.feed(OPENING + frame + HEARTBEAT)

    assert opening == A_RUN[0]
    assert isinstance(violation, StreamViolation)
    assert violation.kind is kind
    with pytest.raises(DecoderClosed):
        decoder.feed(HEARTBEAT)
    assert decoder.close() == []
    assert decoder.close() == []


def test_feeding_text_instead_of_bytes_is_a_type_error_naming_feed() -> None:
    decoder = RunStreamDecoder()

    with pytest.raises(TypeError, match="feed takes bytes"):
        decoder.feed("id: 1\n\n")  # type: ignore[arg-type]


def _tool_input_nested(depth: int) -> dict[str, Any]:
    tool_input: dict[str, Any] = {"a": 1}
    for _ in range(depth - 1):
        tool_input = {"a": tool_input}
    return tool_input


def test_tool_input_at_the_depth_limit_survives_encoding_and_decoding() -> None:
    deep_call = call(1, "call-1", tool_input=_tool_input_nested(MAX_TOOL_INPUT_DEPTH))

    opening, decoded = RunStreamDecoder().feed(OPENING + encode_event(deep_call))

    assert opening == A_RUN[0]
    assert decoded == deep_call


def test_tool_input_deeper_than_the_limit_is_not_built() -> None:
    too_deep = _tool_input_nested(MAX_TOOL_INPUT_DEPTH + 1)

    with pytest.raises(ValidationError):
        call(1, "call-1", tool_input=too_deep)


@pytest.mark.parametrize(
    ("depth", "kind"),
    [
        pytest.param(MAX_TOOL_INPUT_DEPTH + 1, ViolationKind.SCHEMA_MISMATCH, id="past the limit"),
        pytest.param(100_000, ViolationKind.INVALID_JSON, id="past the JSON parser's own limit"),
    ],
)
def test_a_frame_whose_tool_input_nests_too_deep_is_named(depth: int, kind: ViolationKind) -> None:
    shallow_call = encode_event(call(1, "call-1", tool_input={"a": 1}))
    deep_frame = shallow_call.replace(b'{"a":1}', b'{"a":' * depth + b"1" + b"}" * depth)
    assert len(deep_frame) < MAX_EVENT_BYTES

    opening, violation = RunStreamDecoder().feed(OPENING + deep_frame)

    assert opening == A_RUN[0]
    assert isinstance(violation, StreamViolation)
    assert violation.kind is kind


@pytest.mark.parametrize(
    "stream",
    [
        pytest.param(ENCODED, id="a complete run"),
        pytest.param(OPENING, id="a run cut before its terminal event"),
    ],
)
def test_a_closed_decoder_refuses_a_second_close(stream: bytes) -> None:
    decoder = RunStreamDecoder()
    decoder.feed(stream)
    decoder.close()

    with pytest.raises(DecoderClosed):
        decoder.close()


@pytest.mark.parametrize(
    "cut",
    [
        pytest.param(len(encode_event(A_RUN[-1])), id="before the terminal frame"),
        pytest.param(1, id="inside the terminal frame"),
    ],
)
def test_a_stream_cut_before_its_terminal_event_is_truncated(cut: int) -> None:
    *decoded, violation = _decode(ENCODED[:-cut])

    assert decoded == A_RUN[:-1]
    assert isinstance(violation, StreamViolation)
    assert violation.kind is ViolationKind.TRUNCATED


def _text_of_frame_size(size: int) -> AssistantText:
    overhead = len(encode_event(text(1, "x"))) - 1
    return text(1, "x" * (size - overhead))


def test_a_frame_at_the_limit_travels_both_ways() -> None:
    event = _text_of_frame_size(MAX_EVENT_BYTES)
    frame = encode_event(event)

    assert len(frame) == MAX_EVENT_BYTES
    assert RunStreamDecoder().feed(OPENING + frame) == [A_RUN[0], event]


def test_a_frame_one_byte_over_the_limit_is_refused_both_ways() -> None:
    at_limit = encode_event(_text_of_frame_size(MAX_EVENT_BYTES))
    over_limit = at_limit.replace(b'"text":"', b'"text":"x', 1)

    oversized_event = _text_of_frame_size(MAX_EVENT_BYTES + 1)

    with pytest.raises(EventTooLarge):
        encode_event(oversized_event)
    *_, violation = RunStreamDecoder().feed(OPENING + over_limit)
    assert isinstance(violation, StreamViolation)
    assert violation.kind is ViolationKind.OVERSIZED_FRAME


def test_the_decoder_refuses_a_line_before_it_outgrows_the_limit() -> None:
    decoder = RunStreamDecoder()
    decoder.feed(OPENING)

    assert decoder.feed(b"data: " + b"x" * (MAX_EVENT_BYTES - len(b"data: "))) == []
    (violation,) = decoder.feed(b"x")
    assert violation.kind is ViolationKind.OVERSIZED_FRAME
