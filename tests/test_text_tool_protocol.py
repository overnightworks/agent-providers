"""The subscription-CLI text tool wire contract is deliberately strict."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass, fields

import pytest
from provider_test_support import (
    A_CALLER_CANARY,
    CALLER_CONTEXTS,
    DEEPLY_NESTED_JSON,
    SAMPLE_TOOL_CATALOG,
    SAMPLE_TOOL_MARKUP,
    CallerCanaryError,
)
from pydantic import ValidationError

from agent_client.tools import ToolDeclaration
from agent_providers.text_tool_protocol import (
    FinalText,
    TextToolCall,
    TextToolProtocolError,
    TextToolRejection,
    TextToolRejectionRule,
    TextToolStreamParser,
    parse_text_tool_response,
    render_tool_catalog,
    render_tool_result,
)
from agent_providers.tools import ToolCatalog, ToolProtocolMarkup


def _call(payload: str) -> str:
    return f"<notes_tool_call>\n{payload}\n</notes_tool_call>"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            _call('{"name":"get_note","arguments":{"note_id":"note-1"}}'),
            TextToolCall("get_note", {"note_id": "note-1"}),
        ),
        (
            " \t\r\n<notes_tool_call>\r\n"
            '{"name":"list_notes","arguments":{}}\r\n'
            "</notes_tool_call>\n ",
            TextToolCall("list_notes", {}),
        ),
    ],
)
def test_parses_a_leading_call_with_allowed_newlines_and_outer_whitespace(response, expected):
    assert parse_text_tool_response(SAMPLE_TOOL_CATALOG, response) == expected


def test_parses_a_call_whose_closing_tag_follows_the_json_on_the_same_line():
    response = (
        "<notes_tool_call>\n"
        '{"name":"get_note","arguments":{"note_id":"note-1"}}</notes_tool_call>'
    )

    assert parse_text_tool_response(SAMPLE_TOOL_CATALOG, response) == TextToolCall(
        "get_note", {"note_id": "note-1"},
    )


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        (
            [
                " \n<notes_to",
                "ol_call>\n{\"name\":\"get_note\",",
                "\"arguments\":{\"note_id\":\"note-1\"}}\n</notes_tool_call>",
            ],
            TextToolCall("get_note", {"note_id": "note-1"}),
        ),
        (
            [
                "<notes_tool_call>\r",
                "\n{\"name\":\"list_notes\",\"arguments\":{}}\r\n",
                "</notes_tool_call>",
            ],
            TextToolCall("list_notes", {}),
        ),
    ],
)
def test_stream_parser_buffers_a_call_split_across_text_events(chunks, expected):
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)

    emitted = [parser.feed(chunk) for chunk in chunks]

    assert emitted == [""] * len(chunks)
    assert parser.finish() == expected


def test_stream_parser_executes_a_call_whose_closing_tag_follows_the_json_on_the_same_line():
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)

    parser.feed(
        "<notes_tool_call>\n"
        '{"name":"get_note","arguments":{"note_id":"note-1"}}</notes_tool_call>'
    )

    assert parser.finish() == TextToolCall("get_note", {"note_id": "note-1"})


def test_stream_parser_executes_a_line_delimited_call_after_streamed_prose():
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)

    assert parser.feed("I'll look that up.\n<notes_to") == "I'll look that up.\n"
    assert parser.feed("ol_call>\n{\"name\":\"get_note\",") == ""
    assert parser.feed("\"arguments\":{\"note_id\":\"note-1\"}}\n</notes_tool_call>") == ""

    assert parser.finish() == TextToolCall("get_note", {"note_id": "note-1"})


def test_stream_parser_leaves_a_fenced_line_delimited_call_as_assistant_text():
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)
    chunks = [
        "```json\n<notes_tool",
        '_call>\n{"name":"get_note","arguments":{"note_id":"note-1"}}\n',
        "</notes_tool_call>\n```",
    ]

    assert [parser.feed(chunk) for chunk in chunks] == chunks
    assert parser.finish() == FinalText("")


def test_stream_parser_executes_an_unfenced_call_after_a_fenced_example():
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)
    example = "```json\n" + _call('{"name":"list_notes","arguments":{}}') + "\n```\n"

    assert parser.feed(example + "<notes_tool") == example
    assert parser.feed(
        '_call>\n{"name":"get_note","arguments":{"note_id":"note-1"}}\n'
        "</notes_tool_call>"
    ) == ""
    assert parser.finish() == TextToolCall("get_note", {"note_id": "note-1"})


@pytest.mark.parametrize(
    "response",
    [
        "An answer with <notes_tool_call> in running text.",
        "```json\n" + _call('{"name":"get_note","arguments":{"note_id":"note-1"}}') + "\n```",
    ],
)
def test_nonleading_or_fenced_call_syntax_is_ordinary_final_text(response):
    assert parse_text_tool_response(SAMPLE_TOOL_CATALOG, response) == FinalText(response)


def test_stream_parser_executes_a_text_call_followed_by_a_trailing_remark():
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)
    text = (
        "Paragraph one, line one\n"
        "Paragraph one, line two\n"
        "\n"
        "Paragraph two, line one\n"
        "Paragraph two, line two"
    )
    payload = json.dumps(
        {"name": "update_note_text", "arguments": {"note_id": "note-1", "text": text}},
    )

    chunks = [
        "<notes_tool_call>\n",
        payload[:15],
        payload[15:40],
        payload[40:],
        "\n</notes_tool_call>",
        "\nDone — tell me if you want another paragraph.",
    ]
    for chunk in chunks:
        parser.feed(chunk)

    assert parser.finish() == TextToolCall(
        "update_note_text", {"note_id": "note-1", "text": text},
    )


def test_stream_parser_rejects_a_second_opening_block_after_a_complete_call():
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)
    chunks = [
        "<notes_tool_call>\n",
        '{"name":"list_notes","arguments":{}}',
        "\n</notes_tool_call>",
        "\n<notes_tool_call>\n",
        '{"name":"list_notes","arguments":{}}\n</notes_tool_call>',
    ]
    for chunk in chunks:
        parser.feed(chunk)

    with pytest.raises(TextToolProtocolError):
        parser.finish()


def test_stream_parser_rejects_a_second_opening_block_after_a_trailing_remark():
    """The smuggle case: a complete call, a closing remark, then a second call.

    At most one call is ever returned and nothing in the remainder is
    executed; here the second block is also detected and rejected, since it
    still starts its own line outside a markdown fence -- the same scan the
    live parser itself uses to recognize a call.
    """
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)
    chunks = [
        "<notes_tool_call>\n",
        '{"name":"list_notes","arguments":{}}',
        "\n</notes_tool_call>",
        "\nDone, thanks! One more thing --\n<notes_tool_call>\n",
        '{"name":"list_notes","arguments":{}}\n</notes_tool_call>',
    ]
    for chunk in chunks:
        parser.feed(chunk)

    with pytest.raises(TextToolProtocolError):
        parser.finish()


def test_stream_parser_takes_the_first_complete_call_when_a_later_close_tag_also_appears():
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)
    chunks = [
        "<notes_tool_call>\n",
        '{"name":"list_notes","arguments":{}}',
        "</notes_tool_call>",
        " By the way, </notes_tool_call> is just a tag name.",
    ]
    for chunk in chunks:
        parser.feed(chunk)

    assert parser.finish() == TextToolCall("list_notes", {})


def test_stream_parser_parses_a_text_call_that_mentions_the_close_tag_text():
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)
    text = "use </notes_tool_call> to call a tool"
    payload = json.dumps(
        {"name": "update_note_text", "arguments": {"note_id": "note-1", "text": text}},
    )

    parser.feed("<notes_tool_call>\n" + payload + "\n</notes_tool_call>")

    assert parser.finish() == TextToolCall(
        "update_note_text", {"note_id": "note-1", "text": text},
    )


def test_stream_parser_parses_a_text_mention_of_the_close_tag_with_a_same_line_real_close():
    """The production combination: an in-string close tag AND a same-line real close.

    Neither the in-string mention nor the newline-free close is enough on
    its own to reproduce the original bug's shape; a model that both quotes
    the tag in text and closes the call on the same line as the final
    brace needs the JSON decoder to walk past the in-string one structurally
    and still recognize the real one right after the value ends.
    """
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)
    text = "use </notes_tool_call> to call a tool"
    payload = json.dumps(
        {"name": "update_note_text", "arguments": {"note_id": "note-1", "text": text}},
    )

    parser.feed("<notes_tool_call>\n" + payload + "</notes_tool_call>")

    assert parser.finish() == TextToolCall(
        "update_note_text", {"note_id": "note-1", "text": text},
    )


def test_stream_parser_forwards_ordinary_text_and_keeps_only_whitespace_until_finish():
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)

    assert parser.feed("hello ") == "hello "
    assert parser.feed("world") == "world"
    assert parser.finish() == FinalText("")

    whitespace_parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)

    assert whitespace_parser.feed(" \t\n") == ""
    assert whitespace_parser.finish() == FinalText(" \t\n")


A_CATALOG_WITH_A_NUMBER = ToolCatalog(
    tools=(
        ToolDeclaration(
            name="set_font_size",
            description="Set the font size.",
            parameters={
                "type": "object",
                "properties": {"size": {"type": "number"}},
                "required": ["size"],
                "additionalProperties": False,
            },
        ),
    ),
    markup=SAMPLE_TOOL_MARKUP,
)


def test_a_finite_number_argument_is_accepted():
    response = _call('{"name":"set_font_size","arguments":{"size":96.5}}')

    assert parse_text_tool_response(A_CATALOG_WITH_A_NUMBER, response) == TextToolCall(
        "set_font_size", {"size": 96.5}
    )


def test_tool_results_are_json_data_in_the_result_tags():
    assert render_tool_result(SAMPLE_TOOL_CATALOG, {"note_id": "note-1", "updated": True}) == (
        '<notes_tool_result>\n{"note_id":"note-1","updated":true}\n'
        "</notes_tool_result>"
    )


def test_protocol_rejection_never_logs_model_or_protocol_text(caplog):
    secret_protocol = _call(
        '{"name":"get_note","arguments":{"note_id":"note-secret","text":"private text"}}'
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(TextToolProtocolError) as raised:
            parse_text_tool_response(SAMPLE_TOOL_CATALOG, secret_protocol)

    assert raised.value.__cause__ is None
    assert secret_protocol not in caplog.text
    assert "note-secret" not in caplog.text
    assert "private text" not in caplog.text


A_BRANDED_CATALOG = ToolCatalog(
    tools=(
        ToolDeclaration(
            name="open_slide",
            description="Open one slide of the deck.",
            parameters={
                "type": "object",
                "properties": {"slide_id": {"type": "string"}},
                "required": ["slide_id"],
                "additionalProperties": False,
            },
        ),
    ),
    markup=ToolProtocolMarkup(tag_namespace="atelier", product_name="Atelier"),
)


@pytest.mark.parametrize(
    ("model", "fields", "missing"),
    [
        (ToolCatalog, {"tools": SAMPLE_TOOL_CATALOG.tools}, {"markup"}),
        (ToolProtocolMarkup, {}, {"tag_namespace", "product_name"}),
        (ToolProtocolMarkup, {"product_name": "Atelier"}, {"tag_namespace"}),
        (ToolProtocolMarkup, {"tag_namespace": "atelier"}, {"product_name"}),
    ],
)
def test_a_catalog_or_markup_without_its_brand_is_refused_by_field_name(model, fields, missing):
    with pytest.raises(ValidationError) as refusal:
        model(**fields)

    refused = {error["loc"][0] for error in refusal.value.errors()}
    assert refused == missing
    assert {error["type"] for error in refusal.value.errors()} == {"missing"}


@pytest.mark.parametrize(
    "fields",
    [
        {"tag_namespace": "", "product_name": "Atelier"},
        {"tag_namespace": "atelier", "product_name": ""},
    ],
)
def test_an_empty_brand_is_refused(fields):
    with pytest.raises(ValidationError):
        ToolProtocolMarkup(**fields)


def test_a_host_brands_the_prompt_and_the_markers_it_is_parsed_with():
    call = (
        "<atelier_tool_call>\n"
        '{"name":"open_slide","arguments":{"slide_id":"slide-1"}}\n'
        "</atelier_tool_call>"
    )

    prompt = render_tool_catalog(A_BRANDED_CATALOG)

    assert prompt.startswith("To call a Atelier tool,")
    assert "<atelier_tool_call>" in prompt
    assert "notes" not in prompt
    assert parse_text_tool_response(A_BRANDED_CATALOG, call) == TextToolCall(
        "open_slide", {"slide_id": "slide-1"},
    )
    assert render_tool_result(A_BRANDED_CATALOG, {"ok": True}) == (
        '<atelier_tool_result>\n{"ok":true}\n</atelier_tool_result>'
    )


def test_a_branded_catalog_reads_another_hosts_markers_as_ordinary_text():
    another_hosts_call = _call('{"name":"open_slide","arguments":{"slide_id":"slide-1"}}')

    assert parse_text_tool_response(A_BRANDED_CATALOG, another_hosts_call) == FinalText(
        another_hosts_call,
    )


def test_a_catalog_refuses_a_tool_it_never_declared():
    undeclared = (
        "<atelier_tool_call>\n"
        '{"name":"get_note","arguments":{"note_id":"note-1"}}\n'
        "</atelier_tool_call>"
    )

    with pytest.raises(TextToolProtocolError):
        parse_text_tool_response(A_BRANDED_CATALOG, undeclared)


OPEN = "<notes_tool_call>"
CLOSE = "</notes_tool_call>"
A_LIST_CALL = '{"name":"list_notes","arguments":{}}'
A_NUMBER_CALL = '{{"name":"set_font_size","arguments":{{"size":{}}}}}'
THE_VALUE_START = len(OPEN) + len("\n")


def _parse_one_shot(catalog: ToolCatalog, text: str):
    return parse_text_tool_response(catalog, text)


def _parse_streamed(catalog: ToolCatalog, text: str):
    parser = TextToolStreamParser(catalog)
    parser.feed(text)
    return parser.finish()


PARSERS: dict[str, Callable] = {"one-shot": _parse_one_shot, "stream": _parse_streamed}


@dataclass(frozen=True)
class _Refusal:
    text: str
    rule: TextToolRejectionRule
    offset: int
    parsers: tuple[str, ...] = ("one-shot", "stream")
    catalog: ToolCatalog = SAMPLE_TOOL_CATALOG


def _at(text: str, marker: str) -> int:
    return text.index(marker)


def _cases(*tables: dict[str, _Refusal]) -> list:
    return [
        pytest.param(PARSERS[name], row, id=f"{name}-{row_id}")
        for table in tables
        for row_id, row in table.items()
        for name in row.parsers
    ]


def _refusal_of_text(
    parse: Callable, text: str, catalog: ToolCatalog = SAMPLE_TOOL_CATALOG,
) -> TextToolProtocolError:
    with pytest.raises(TextToolProtocolError) as raised:
        parse(catalog, text)
    return raised.value


def _refusal_of(parse: Callable, row: _Refusal) -> TextToolProtocolError:
    return _refusal_of_text(parse, row.text, row.catalog)


def _deeply_nested_catalog(depth: int) -> tuple[ToolCatalog, str]:
    schema: dict = {"type": "object", "properties": {}}
    for _ in range(depth):
        schema = {"type": "object", "properties": {"n": schema}}
    catalog = ToolCatalog(
        tools=(ToolDeclaration(name="nest", description="Nest.", parameters=schema),),
        markup=SAMPLE_TOOL_MARKUP,
    )
    arguments = '{"n":' * depth + "{}" + "}" * depth
    return catalog, _call(f'{{"name":"nest","arguments":{arguments}}}')


_DEEP_CATALOG, _DEEP_CALL = _deeply_nested_catalog(600)

CALL_NOT_JSON_ROWS = {
    "opening-line-alone-lf": _Refusal(OPEN + "\n", TextToolRejectionRule.CALL_NOT_JSON, len(OPEN)),
    "opening-line-alone-crlf": _Refusal(
        OPEN + "\r\n", TextToolRejectionRule.CALL_NOT_JSON, len(OPEN),
    ),
    "opening-line-and-blank-lines": _Refusal(
        OPEN + "\n\n \n", TextToolRejectionRule.CALL_NOT_JSON, len(OPEN),
    ),
    "json-broken-before-the-close": _Refusal(
        f'{OPEN}\n{{"name":"list_notes",\n{CLOSE}',
        TextToolRejectionRule.CALL_NOT_JSON,
        _at(f'{OPEN}\n{{"name":"list_notes",\n{CLOSE}', CLOSE),
    ),
    "json-cut-off-at-the-end": _Refusal(
        f'{OPEN}\n{{"name":"list_notes","arguments":{{}}',
        TextToolRejectionRule.CALL_NOT_JSON,
        len(f'{OPEN}\n{{"name":"list_notes","arguments":{{}}'),
    ),
    "prose-before-the-json": _Refusal(
        f"{OPEN}\nHere is the call: {A_LIST_CALL}\n{CLOSE}",
        TextToolRejectionRule.CALL_NOT_JSON,
        THE_VALUE_START,
    ),
    "a-bare-word": _Refusal(
        _call("not json"), TextToolRejectionRule.CALL_NOT_JSON, THE_VALUE_START,
    ),
    **{
        f"the-constant-{constant}": _Refusal(
            _call(A_NUMBER_CALL.format(constant)),
            TextToolRejectionRule.CALL_NOT_JSON,
            THE_VALUE_START,
            catalog=A_CATALOG_WITH_A_NUMBER,
        )
        for constant in ("NaN", "Infinity", "-Infinity")
    },
    "decoder-recursion": _Refusal(
        _call(DEEPLY_NESTED_JSON), TextToolRejectionRule.CALL_NOT_JSON, THE_VALUE_START,
    ),
}
CLOSE_TAG_ROWS = {
    "prose-after-the-value": _Refusal(
        f"{OPEN}\n{A_LIST_CALL} trailing words\n{CLOSE}",
        TextToolRejectionRule.CLOSE_TAG_NOT_AFTER_CALL,
        _at(f"{OPEN}\n{A_LIST_CALL} trailing words\n{CLOSE}", "trailing"),
    ),
    "no-close-tag-at-all": _Refusal(
        f"{OPEN}\n{A_LIST_CALL}",
        TextToolRejectionRule.CLOSE_TAG_NOT_AFTER_CALL,
        len(f"{OPEN}\n{A_LIST_CALL}"),
    ),
    "json-whitespace-then-a-sentence": _Refusal(
        f"{OPEN}\n{A_LIST_CALL} \t\n  Hello there\n{CLOSE}",
        TextToolRejectionRule.CLOSE_TAG_NOT_AFTER_CALL,
        _at(f"{OPEN}\n{A_LIST_CALL} \t\n  Hello there\n{CLOSE}", "Hello"),
    ),
}
_TWO_CALLS = f"{A_LIST_CALL}{CLOSE}\n{OPEN}\n{A_LIST_CALL}\n{CLOSE}"
TEXT_AFTER_CALL_ROWS = {
    "a-closing-remark": _Refusal(
        f"{OPEN}\n{A_LIST_CALL}{CLOSE}\nDone!",
        TextToolRejectionRule.TEXT_AFTER_CALL,
        _at(f"{OPEN}\n{A_LIST_CALL}{CLOSE}\nDone!", "\nDone!"),
        parsers=("one-shot",),
    ),
    "a-second-call": _Refusal(
        f"{OPEN}\n{_TWO_CALLS}",
        TextToolRejectionRule.TEXT_AFTER_CALL,
        _at(f"{OPEN}\n{_TWO_CALLS}", f"{CLOSE}\n") + len(CLOSE),
        parsers=("one-shot",),
    ),
}
SECOND_CALL_ROWS = {
    "behind-an-lf-opening-line": _Refusal(
        f"{OPEN}\n{_TWO_CALLS}",
        TextToolRejectionRule.SECOND_CALL,
        f"{OPEN}\n{_TWO_CALLS}".rindex(OPEN),
        parsers=("stream",),
    ),
    "behind-a-crlf-opening-line": _Refusal(
        f"{OPEN}\r\n{_TWO_CALLS}",
        TextToolRejectionRule.SECOND_CALL,
        f"{OPEN}\r\n{_TWO_CALLS}".rindex(OPEN),
        parsers=("stream",),
    ),
    "after-a-trailing-remark": _Refusal(
        f"{OPEN}\n{A_LIST_CALL}{CLOSE}\nOkay.\n{OPEN}\n{A_LIST_CALL}\n{CLOSE}",
        TextToolRejectionRule.SECOND_CALL,
        f"{OPEN}\n{A_LIST_CALL}{CLOSE}\nOkay.\n{OPEN}\n{A_LIST_CALL}\n{CLOSE}".rindex(OPEN),
        parsers=("stream",),
    ),
}
NOT_A_CALL_OBJECT_ROWS = {
    row_id: _Refusal(_call(payload), TextToolRejectionRule.NOT_A_CALL_OBJECT, THE_VALUE_START)
    for row_id, payload in {
        "a-list": "[1]",
        "no-arguments": '{"name":"list_notes"}',
        "an-extra-member": '{"name":"list_notes","arguments":{},"extra":true}',
        "a-numeric-name": '{"name":3,"arguments":{}}',
        "arguments-a-list": '{"name":"list_notes","arguments":[]}',
        "arguments-a-string": '{"name":"list_notes","arguments":"no"}',
        "arguments-null": '{"name":"list_notes","arguments":null}',
    }.items()
}
UNKNOWN_TOOL_ROWS = {
    "an-undeclared-tool": _Refusal(
        _call('{"name":"delete_everything","arguments":{}}'),
        TextToolRejectionRule.UNKNOWN_TOOL,
        THE_VALUE_START,
    ),
}
ARGUMENTS_OFF_SCHEMA_ROWS = {
    **{
        row_id: _Refusal(
            _call(payload), TextToolRejectionRule.ARGUMENTS_OFF_SCHEMA, THE_VALUE_START,
        )
        for row_id, payload in {
            "wrong-argument-type": '{"name":"get_note","arguments":{"note_id":3}}',
            "missing-required-argument": '{"name":"get_note","arguments":{}}',
            "undeclared-property": (
                '{"name":"get_note","arguments":{"note_id":"note-1","extra":true}}'
            ),
        }.items()
    },
    "validation-recursion": _Refusal(
        _DEEP_CALL,
        TextToolRejectionRule.ARGUMENTS_OFF_SCHEMA,
        THE_VALUE_START,
        catalog=_DEEP_CATALOG,
    ),
}
PRECEDENCE_ROWS = {
    "prose-before-the-close-beats-an-unknown-tool": _Refusal(
        f'{OPEN}\n{{"name":"delete_everything","arguments":{{}}}} prose\n{CLOSE}',
        TextToolRejectionRule.CLOSE_TAG_NOT_AFTER_CALL,
        _at(f'{OPEN}\n{{"name":"delete_everything","arguments":{{}}}} prose\n{CLOSE}', "prose"),
    ),
    "a-second-call-beats-a-non-object": _Refusal(
        f"{OPEN}\n[1]{CLOSE}\n{OPEN}\n{A_LIST_CALL}\n{CLOSE}",
        TextToolRejectionRule.SECOND_CALL,
        f"{OPEN}\n[1]{CLOSE}\n{OPEN}\n{A_LIST_CALL}\n{CLOSE}".rindex(OPEN),
        parsers=("stream",),
    ),
    "text-after-the-close-beats-an-unknown-tool": _Refusal(
        f'{OPEN}\n{{"name":"delete_everything","arguments":{{}}}}{CLOSE}\nDone',
        TextToolRejectionRule.TEXT_AFTER_CALL,
        _at(f'{OPEN}\n{{"name":"delete_everything","arguments":{{}}}}{CLOSE}\nDone', "\nDone"),
        parsers=("one-shot",),
    ),
    "a-non-object-beats-an-unknown-tool": _Refusal(
        _call('{"name":"delete_everything","arguments":[]}'),
        TextToolRejectionRule.NOT_A_CALL_OBJECT,
        THE_VALUE_START,
    ),
    "an-unknown-tool-beats-off-schema-arguments": _Refusal(
        _call('{"name":"delete_everything","arguments":{"x":1}}'),
        TextToolRejectionRule.UNKNOWN_TOOL,
        THE_VALUE_START,
    ),
}
RULE_TABLES = (
    CALL_NOT_JSON_ROWS,
    CLOSE_TAG_ROWS,
    TEXT_AFTER_CALL_ROWS,
    SECOND_CALL_ROWS,
    NOT_A_CALL_OBJECT_ROWS,
    UNKNOWN_TOOL_ROWS,
    ARGUMENTS_OFF_SCHEMA_ROWS,
)


@pytest.mark.parametrize(("parse", "row"), _cases(*RULE_TABLES))
def test_a_refused_call_names_the_rule_it_broke_and_where(parse, row):
    rejection = _refusal_of(parse, row).rejection

    assert (rejection.rule, rejection.offset) == (row.rule, row.offset)
    assert rejection.length == len(row.text.strip())
    assert 0 <= rejection.offset <= rejection.length


@pytest.mark.parametrize(("parse", "row"), _cases(PRECEDENCE_ROWS))
def test_a_call_breaking_several_rules_is_named_under_the_first_in_order(parse, row):
    rejection = _refusal_of(parse, row).rejection

    assert (rejection.rule, rejection.offset) == (row.rule, row.offset)


def test_the_rules_a_refusal_can_name_are_exactly_these_seven():
    assert {rule.value for rule in TextToolRejectionRule} == {
        "call_not_json",
        "close_tag_not_after_call",
        "text_after_call",
        "second_call",
        "not_a_call_object",
        "unknown_tool",
        "arguments_off_schema",
    }


@pytest.mark.parametrize("parse", PARSERS.values(), ids=PARSERS)
@pytest.mark.parametrize(
    ("newline", "whitespace_before_the_value", "filler_length"),
    [
        pytest.param("\n", "", 10, id="lf-opening-line"),
        pytest.param("\r\n", "", 10, id="crlf-opening-line"),
        pytest.param("\n", "", 30_000, id="thirty-thousand-characters-before-the-break"),
        pytest.param("\n", " \t\n", 10, id="whitespace-before-the-value"),
        pytest.param("\r\n", " \t\n", 30_000, id="crlf-whitespace-and-thirty-thousand"),
    ],
)
def test_an_offset_counts_from_the_block_start_past_the_opening_line_and_whitespace(
    parse, newline, whitespace_before_the_value, filler_length,
):
    value = f'["{"x" * filler_length}",]'
    decoder = json.JSONDecoder()
    with pytest.raises(json.JSONDecodeError) as decoder_refusal:
        decoder.raw_decode(value)
    row = _Refusal(
        OPEN + newline + whitespace_before_the_value + value,
        TextToolRejectionRule.CALL_NOT_JSON,
        len(OPEN + newline + whitespace_before_the_value) + decoder_refusal.value.pos,
    )

    rejection = _refusal_of(parse, row).rejection

    assert rejection.offset == row.offset


def test_a_nonjson_constant_is_refused_at_the_value_start_past_skipped_whitespace():
    row = _Refusal(
        OPEN + "\n \t\n" + "NaN", TextToolRejectionRule.CALL_NOT_JSON, len(OPEN + "\n \t\n"),
    )

    assert _refusal_of(_parse_streamed, row).rejection.offset == row.offset


def _broken_block(before: str, after: str) -> str:
    return f'{OPEN}\n["{before}",@{after}'


def _window_of(text: str) -> tuple[TextToolRejection, str]:
    return _refusal_of_text(_parse_streamed, text).rejection, text.strip()


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_broken_block("", "tail"), id="broken-near-the-start-of-a-short-block"),
        pytest.param(_broken_block("a" * 300, "b" * 300), id="broken-in-the-middle-of-a-block"),
        pytest.param(f"{OPEN}\n{A_LIST_CALL}", id="broken-at-the-block-length"),
        pytest.param(_broken_block("a" * 511, "b" * 511), id="window-edges-touch-the-block-ends"),
    ],
)
def test_the_excerpt_is_the_block_within_512_characters_of_where_it_broke(text):
    rejection, block = _window_of(text)

    assert rejection.excerpt_start == max(0, rejection.offset - 512)
    assert rejection.excerpt.get_secret_value() == block[
        rejection.excerpt_start : min(rejection.length, rejection.offset + 512)
    ]
    assert [field.name for field in fields(rejection)] == [
        "rule", "offset", "length", "excerpt_start", "excerpt",
    ]


def test_a_one_mebibyte_block_gives_an_excerpt_of_exactly_1024_characters():
    half_a_mebibyte = 512 * 1024

    rejection, _ = _window_of(_broken_block("a" * half_a_mebibyte, "b" * half_a_mebibyte))

    assert len(rejection.excerpt.get_secret_value()) == 1024
    assert rejection.offset - rejection.excerpt_start == 512


def test_a_thirty_thousand_character_block_broken_at_29950_gives_characters_29438_to_30000():
    prefix = f'{OPEN}\n["'
    before = "a" * (29_950 - len(prefix) - len('",'))

    rejection, block = _window_of(_broken_block(before, "b" * 49))

    assert (rejection.offset, rejection.length) == (29_950, 30_000)
    assert rejection.excerpt_start == 29_438
    assert rejection.excerpt.get_secret_value() == block[29_438:30_000]
    assert len(rejection.excerpt.get_secret_value()) == 562


@pytest.mark.parametrize(
    "unit",
    [
        pytest.param("\u00e4", id="umlaut"),
        pytest.param("\U0001f600", id="astral-emoji"),
        pytest.param("e\u0301", id="combining-accent"),
        pytest.param("\ud83d\ude00", id="explicit-surrogate-pair"),
    ],
)
@pytest.mark.parametrize("edge", ["window-start", "window-end"])
def test_the_excerpt_is_the_exact_code_point_slice_even_through_a_character(unit, edge):
    text = _broken_block("a" * 1000, "b" * 1000)
    offset = text.index("@")
    window_start, window_end = offset - 512, offset + 512
    unit_position = window_start - (len(unit) - 1) if edge == "window-start" else window_end - 1
    text = text[:unit_position] + unit + text[unit_position + len(unit):]

    rejection, block = _window_of(text)
    excerpt = rejection.excerpt.get_secret_value()

    assert excerpt == block[window_start:window_end]
    assert excerpt[0] == (unit[-1] if edge == "window-start" else "a")
    assert excerpt[-1] == (unit[0] if edge == "window-end" else "b")


def test_the_excerpt_keeps_a_decomposed_character_inside_the_window_without_normalization():
    decomposed_e_acute = "e\u0301"
    text = _broken_block("a" * 1000, "b" * 1000)
    offset = text.index("@")
    text = text[: offset - 100] + decomposed_e_acute + text[offset - 100 + 2 :]

    rejection, block = _window_of(text)
    excerpt = rejection.excerpt.get_secret_value()

    assert excerpt == block[offset - 512 : offset + 512]
    assert decomposed_e_acute in excerpt


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        pytest.param(
            [f"{OPEN}\n{A_LIST_CALL}{CLOSE}\nDone -- want another?"],
            TextToolCall("list_notes", {}),
            id="a-trailing-remark-after-the-close",
        ),
        pytest.param(
            [f"{OPEN}\n{A_LIST_CALL}{CLOSE}"],
            TextToolCall("list_notes", {}),
            id="the-close-on-the-same-line-as-the-json",
        ),
        pytest.param(
            [f'{OPEN}\n{{"name":"update_note_text","arguments":'
             f'{{"note_id":"s","text":"mentions {CLOSE} inside"}}}}\n{CLOSE}'],
            TextToolCall(
                "update_note_text", {"note_id": "s", "text": f"mentions {CLOSE} inside"},
            ),
            id="a-close-tag-inside-a-string",
        ),
    ],
)
def test_the_stream_parser_still_accepts_what_it_accepted_before_rules_were_named(
    chunks, expected,
):
    parser = TextToolStreamParser(SAMPLE_TOOL_CATALOG)
    for chunk in chunks:
        parser.feed(chunk)

    assert parser.finish() == expected


@CALLER_CONTEXTS
@pytest.mark.parametrize(("parse", "row"), _cases(*RULE_TABLES, PRECEDENCE_ROWS))
def test_a_refusal_has_an_empty_exception_chain_at_the_parser_catch(
    parse, row, while_the_caller_handles_an_exception,
):
    def refusal_caught_at_the_parser() -> TextToolProtocolError:
        return _refusal_of(parse, row)

    if while_the_caller_handles_an_exception:
        try:
            raise CallerCanaryError(A_CALLER_CANARY)
        except CallerCanaryError:
            error = refusal_caught_at_the_parser()
    else:
        error = refusal_caught_at_the_parser()

    assert error.__context__ is None
    assert error.__cause__ is None
    assert not hasattr(error, "__notes__")
    assert (error.args, str(error)) == ((), "")
    assert A_CALLER_CANARY not in "".join(traceback.format_exception(error))
