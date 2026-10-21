"""The strict YAML subset an agent file's front matter is written in.

Every line is ``key: value`` where the value is a plain, single-quoted or
double-quoted string, a flow list (``[a, b]``) or the lead of a block list
(``key:`` then indented ``- item`` lines). Anything else YAML allows (anchors,
aliases, tags, multi-line scalars, nested maps, comments, tabs, duplicate keys)
is refused, so no loader reads the file differently from this one and every
value is text. The subset is valid YAML, so Claude Code reads the same files.
"""

from __future__ import annotations

import re
from typing import Final

_KEY: Final = re.compile(r"([A-Za-z][A-Za-z0-9_-]*):(?: +(.*))?")
_LIST_ITEM: Final = re.compile(r" +- +(.+)")
_FLOW_ITEM: Final = re.compile(
    r""" *("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^,\[\]{}"']+?) *(,|\Z)"""
)
_DOUBLE_QUOTED: Final = re.compile(r'"((?:[^"\\]|\\.)*)"')
_SINGLE_QUOTED: Final = re.compile(r"'((?:[^']|'')*)'")
_ESCAPES: Final = {"\\": "\\", '"': '"', "n": "\n"}
_FORBIDDEN_PLAIN_START: Final = "&*!|>%@`?-#[]{},'\""

FrontMatterValue = str | list[str]


class FrontMatterError(ValueError):
    """The front matter leaves the subset; the message names the key or line."""


def parse_front_matter(text: str) -> dict[str, FrontMatterValue]:
    """The keys of the front matter with text or lists of text as values."""
    if "\t" in text:
        raise FrontMatterError("tabs are not allowed")
    entries: dict[str, FrontMatterValue] = {}
    block_keys: set[str] = set()
    open_key: str | None = None
    last_key: str | None = None
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip()
        if not line:
            continue
        if line[0] == " ":
            _append_block_item(entries, open_key, last_key, line, number)
            continue
        key, value = _split_key(line, number)
        if key in entries:
            raise FrontMatterError(f"{key}: appears twice")
        entries[key] = _value(key, value)
        last_key = key
        open_key = key if not value else None
        if open_key is not None:
            block_keys.add(open_key)
    for key in block_keys:
        if not entries[key]:
            raise FrontMatterError(f"{key}: has no value")
    return entries


def _split_key(line: str, number: int) -> tuple[str, str]:
    matched = _KEY.fullmatch(line)
    if matched is None:
        raise FrontMatterError(f"line {number}: expected 'key: value'")
    return matched[1], matched[2] or ""


def _append_block_item(
    entries: dict[str, FrontMatterValue],
    open_key: str | None,
    last_key: str | None,
    line: str,
    number: int,
) -> None:
    matched = _LIST_ITEM.fullmatch(line)
    items = entries.get(open_key) if open_key is not None else None
    if matched is None or not isinstance(items, list):
        owner = last_key if last_key is not None else f"line {number}"
        raise FrontMatterError(
            f"{owner}: nested maps, multi-line scalars and stray indentation are not supported"
        )
    try:
        items.append(_scalar(matched[1]))
    except FrontMatterError as error:
        raise FrontMatterError(f"{open_key}: {error}") from error


def _value(key: str, value: str) -> FrontMatterValue:
    try:
        if not value:
            return []
        if value[0] == "[":
            return _flow_list(value)
        return _scalar(value)
    except FrontMatterError as error:
        raise FrontMatterError(f"{key}: {error}") from error


def _flow_list(value: str) -> list[str]:
    if value[-1] != "]":
        raise FrontMatterError("a flow list must end with ]")
    inner = value[1:-1].strip()
    items: list[str] = []
    position = 0
    while inner:
        matched = _FLOW_ITEM.match(inner, position)
        if matched is None:
            raise FrontMatterError("a flow list holds only strings separated by commas")
        items.append(_scalar(matched[1]))
        position = matched.end()
        if not matched[2]:
            break
    return items


def _scalar(text: str) -> str:
    if text[0] == '"':
        return _double_quoted(text)
    if text[0] == "'":
        quoted = _SINGLE_QUOTED.fullmatch(text)
        if quoted is None:
            raise FrontMatterError("a single-quoted string is not closed")
        return quoted[1].replace("''", "'")
    if text[0] in _FORBIDDEN_PLAIN_START:
        raise FrontMatterError(
            f"a plain string cannot start with {text[0]!r}: anchors, aliases, tags, "
            "block scalars and nested values are not supported; quote it"
        )
    if ": " in text or " #" in text or text.endswith(":"):
        raise FrontMatterError("a plain string cannot hold ': ' or ' #'; quote it")
    return text


def _double_quoted(text: str) -> str:
    quoted = _DOUBLE_QUOTED.fullmatch(text)
    if quoted is None:
        raise FrontMatterError("a double-quoted string is not closed")

    def unescape(escape: re.Match[str]) -> str:
        replacement = _ESCAPES.get(escape[1])
        if replacement is None:
            raise FrontMatterError(f"escape \\{escape[1]} is not supported")
        return replacement

    return re.sub(r"\\(.)", unescape, quoted[1])
