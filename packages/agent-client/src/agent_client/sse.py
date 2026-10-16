"""The server-sent-event framing of a run's events.

``encode_event`` writes one event as one frame; ``RunStreamDecoder`` reads a
byte stream of frames back, checks every frame and every event against the
contract, and names the first thing that breaks it. Both are pure: the
transport that writes and reads the bytes lives elsewhere.

A frame is an ``id`` line holding the event's ``seq``, an ``event`` line
holding its ``type``, and one ``data`` line of compact JSON holding the whole
event, ended by a blank line. A frame's size is the bytes of its lines plus
one per line end, so an encoded frame's size is its length. No frame exceeds
``MAX_EVENT_BYTES`` in either direction.
"""

from __future__ import annotations

import json
import re
from enum import Enum, auto
from typing import Any, Final

from agent_client.run_stream import RunStream, StreamViolation, ViolationKind, parse_event
from agent_client.wire import MAX_EVENT_BYTES, RunEvent, is_run_event, refuse_non_json_constant

_BYTE_ORDER_MARK: Final = b"\xef\xbb\xbf"
_LINE_END: Final = re.compile(rb"[\r\n]")
_CARRIAGE_RETURN: Final = ord("\r")
_LINE_FEED: Final = ord("\n")
_FRAME_FIELDS: Final = frozenset({"id", "event", "data"})
_IGNORED_FIELD: Final = "retry"
_COMMENT_MARK: Final = ":"

Decoded = RunEvent | StreamViolation


class _DecoderState(Enum):
    OPEN = auto()
    ENDED = auto()
    BROKE_CONTRACT = auto()


class EventTooLarge(ValueError):
    """An event whose frame would exceed ``MAX_EVENT_BYTES``."""


class DecoderClosed(RuntimeError):
    """A decoder used after its stream ended or broke the contract."""


def encode_event(event: RunEvent) -> bytes:
    """Write one event as one UTF-8 frame with LF line ends."""
    if not is_run_event(event):
        raise TypeError(f"encode_event takes a run event, not a {type(event).__name__}")
    frame = f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n".encode()
    if len(frame) > MAX_EVENT_BYTES:
        raise EventTooLarge(
            f"a {event.type} frame of {len(frame)} bytes exceeds {MAX_EVENT_BYTES} bytes"
        )
    return frame


class RunStreamDecoder:
    """Reads one run's frames back into its events, as the bytes arrive.

    ``feed`` takes chunks split anywhere and returns the events they
    complete; a violation is always the last item, and it closes the
    decoder. ``close`` ends the stream: an unfinished frame is dropped and a
    run without its terminal event is reported as truncated; after ``feed``
    returned a violation it returns nothing, so a consumer may feed every
    chunk and close at the end of its input. A second ``close`` raises
    ``DecoderClosed`` unless ``feed`` returned a violation. Lines may end in
    LF, CRLF or CR, the stream may open with a byte order mark, and comments
    and ``retry`` fields are ignored.
    """

    def __init__(self) -> None:
        self._stream = RunStream()
        self._state = _DecoderState.OPEN
        self._first_line = True
        self._line_feed_may_follow = False
        self._unfinished_line = bytearray()
        self._frame_bytes = 0
        self._fields: dict[str, str] = {}

    def feed(self, chunk: bytes) -> list[Decoded]:
        if isinstance(chunk, str):
            raise TypeError("feed takes bytes, not str")
        self._refuse_if_closed()
        decoded: list[Decoded] = []
        position = self._skip_line_feed_of_split_crlf(chunk)
        while self._state is _DecoderState.OPEN:
            line_end = _LINE_END.search(chunk, position)
            if line_end is None:
                self._hold(chunk[position:], decoded)
                break
            terminator = line_end.start()
            if self._exceeds_limit(terminator - position + 1):
                self._emit(_oversized(), decoded)
                break
            line = bytes(self._unfinished_line) + chunk[position:terminator]
            self._unfinished_line.clear()
            self._frame_bytes += len(line) + 1
            self._emit(self._read_line(line), decoded)
            position = self._after_line_end(chunk, terminator)
        return decoded

    def close(self) -> list[Decoded]:
        if self._state is _DecoderState.BROKE_CONTRACT:
            return []
        self._refuse_if_closed()
        self._state = _DecoderState.ENDED
        violation = self._stream.close()
        return [] if violation is None else [violation]

    def _refuse_if_closed(self) -> None:
        if self._state is not _DecoderState.OPEN:
            raise DecoderClosed("the stream already ended or broke the contract")

    def _skip_line_feed_of_split_crlf(self, chunk: bytes) -> int:
        skip = self._line_feed_may_follow and chunk[:1] == b"\n"
        if chunk:
            self._line_feed_may_follow = False
        return 1 if skip else 0

    def _after_line_end(self, chunk: bytes, terminator: int) -> int:
        following = terminator + 1
        if chunk[terminator] != _CARRIAGE_RETURN:
            return following
        if following == len(chunk):
            self._line_feed_may_follow = True
            return following
        return following + 1 if chunk[following] == _LINE_FEED else following

    def _hold(self, rest: bytes, decoded: list[Decoded]) -> None:
        if self._exceeds_limit(len(rest)):
            self._emit(_oversized(), decoded)
            return
        self._unfinished_line.extend(rest)

    def _exceeds_limit(self, more_bytes: int) -> bool:
        return self._frame_bytes + len(self._unfinished_line) + more_bytes > MAX_EVENT_BYTES

    def _emit(self, outcome: Decoded | None, decoded: list[Decoded]) -> None:
        if outcome is None:
            return
        decoded.append(outcome)
        if isinstance(outcome, StreamViolation):
            self._state = _DecoderState.BROKE_CONTRACT

    def _read_line(self, line: bytes) -> Decoded | None:
        if self._first_line:
            line = line.removeprefix(_BYTE_ORDER_MARK)
            self._first_line = False
        if not line:
            return self._dispatch()
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            return _malformed("a line that is not UTF-8")
        if text.startswith(_COMMENT_MARK):
            return None
        name, _, value = text.partition(":")
        return self._record_field(name, value.removeprefix(" "))

    def _record_field(self, name: str, value: str) -> StreamViolation | None:
        if name == _IGNORED_FIELD:
            return None
        if name not in _FRAME_FIELDS:
            return _malformed("a field outside id, event, data and retry")
        if name in self._fields:
            return _malformed(f"a second {name} field in one frame")
        self._fields[name] = value
        return None

    def _dispatch(self) -> Decoded | None:
        fields, self._fields = self._fields, {}
        self._frame_bytes = 0
        if not fields:
            return None
        missing = sorted(_FRAME_FIELDS - fields.keys())
        if missing:
            return _malformed(f"a frame without {', '.join(missing)}")
        return self._event_from(fields["id"], fields["event"], fields["data"])

    def _event_from(self, frame_id: str, event_type: str, data: str) -> Decoded:
        raw = _json_object(data)
        if isinstance(raw, StreamViolation):
            return raw
        if raw.get("type") != event_type:
            return StreamViolation(
                kind=ViolationKind.EVENT_TYPE_MISMATCH,
                detail="the frame's event field is not its data's type",
            )
        event = parse_event(raw)
        if isinstance(event, StreamViolation):
            return event
        if frame_id != str(event.seq):
            return StreamViolation(
                kind=ViolationKind.ID_MISMATCH,
                detail=f"the frame's id is not its data's seq {event.seq}",
            )
        return self._stream.accept(event) or event


def _json_object(data: str) -> dict[str, Any] | StreamViolation:
    # A frame within the size limit can still nest deeper than the parser's
    # recursion limit; that is the sender's broken JSON, not the client's fault.
    try:
        value = json.loads(data, parse_constant=refuse_non_json_constant)
    except (ValueError, RecursionError) as error:
        return StreamViolation(kind=ViolationKind.INVALID_JSON, detail=f"data is not JSON: {error}")
    if not isinstance(value, dict):
        return StreamViolation(kind=ViolationKind.INVALID_JSON, detail="data is not a JSON object")
    return value


def _malformed(detail: str) -> StreamViolation:
    return StreamViolation(kind=ViolationKind.MALFORMED_FRAME, detail=detail)


def _oversized() -> StreamViolation:
    return StreamViolation(
        kind=ViolationKind.OVERSIZED_FRAME,
        detail=f"a frame larger than {MAX_EVENT_BYTES} bytes",
    )
