"""How the service and the core read a secret kept in a file: once, from one descriptor.

The signing key and a profile's login token follow the same rules (decision record
0001, A1a4 ruling): the file is opened once, type, mode and size come from that
descriptor, and no message carries the file's content.
A token file holding JSON, such as a credentials file, is refused: a line opening an object
or array, or parsing as a JSON scalar. It must hold one setup token.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

MAX_SECRET_FILE_BYTES: Final = 16 * 1024
API_KEY_PREFIX: Final = "sk-ant-api"
_OPEN_FLAGS: Final = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
UNSHOWABLE_PATH: Final = "contains a control or non-printing character; a path must not"


class UnsoundSecretFile(ValueError):
    """A secret file the service refuses to use; the message is the service's own words."""


def shown_text(text: str) -> str:
    """Operator-supplied text with control and non-printing characters escaped, one line."""
    return "".join(map(_shown_character, text))


def _shown_character(character: str) -> str:
    return character if character.isprintable() else character.encode("unicode_escape").decode()


def read_secret_file(path: Path, subject: str) -> bytes:
    """The file's bytes; a refusal names the path in an ``UnsoundSecretFile``, never the content."""
    shown = shown_text(str(path))
    if shown != str(path):
        raise UnsoundSecretFile(f"{shown} {UNSHOWABLE_PATH}")
    try:
        read = _read_through_one_descriptor(path, subject)
    except OSError as error:
        raise UnsoundSecretFile(f"{shown} cannot be read: {error.strerror}") from error
    except UnsoundSecretFile as refusal:
        raise UnsoundSecretFile(f"{shown} {refusal}") from None
    if isinstance(read, _ReadRefusal):
        raise UnsoundSecretFile(f"{shown} {read.reason}")
    return read


def read_token_file(path: Path) -> str:
    """The one-line token the file holds; an API key is refused, since only a login token fits."""
    judged = _token_or_refusal(read_secret_file(path, "token file"))
    if isinstance(judged, _TokenRefusal):
        raise UnsoundSecretFile(f"{shown_text(str(path))} {judged.reason}")
    return judged


@dataclass(frozen=True)
class _TokenRefusal:
    """Returned, never raised: no content-bearing frame and no ``UnicodeDecodeError``
    (whose ``.object`` holds the bytes) may sit in a refusal's chain."""

    reason: str


def _token_or_refusal(contents: bytes) -> str | _TokenRefusal:
    """The token, or a returned refusal; raising here would chain a frame holding the bytes."""
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        return _TokenRefusal("is not UTF-8 text")
    token = text.removesuffix("\n")
    if not token:
        return _TokenRefusal("is empty; it must hold the token")
    if "\n" in token:
        return _TokenRefusal("holds more than one line; it must hold one token")
    if not token.isprintable():
        return _TokenRefusal("contains a control or non-printing character; a token must not")
    if _is_json_value(token):
        return _TokenRefusal("holds a JSON document; it must hold one setup token")
    if any(character.isspace() for character in token):
        return _TokenRefusal("holds whitespace; a setup token holds none")
    if token.startswith(API_KEY_PREFIX):
        return _TokenRefusal("holds an API key; it must hold a subscription token from setup-token")
    return token


JSON_WHITESPACE = " \t\r\n"


def _is_json_value(line: str) -> bool:
    """Whether the line opens a JSON object or array, or parses as a JSON scalar.

    An opening bracket, after JSON's own leading whitespace, is refused unparsed: the parser
    recurses on nesting, and its ``JSONDecodeError`` and ``RecursionError`` frames hold the
    text. A scalar parse neither recurses nor leaves anything but the bool."""
    if line.lstrip(JSON_WHITESPACE).startswith(("{", "[")):
        return True
    try:
        json.loads(line)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class _ReadRefusal:
    """Returned, never raised: the reading frame holds file content and must not sit in a chain."""

    reason: str


def _read_through_one_descriptor(path: Path, subject: str) -> bytes | _ReadRefusal:
    descriptor = os.open(path, _OPEN_FLAGS)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise UnsoundSecretFile("is not a regular file")
        if status.st_mode & 0o077:
            raise UnsoundSecretFile(
                f"is open to group or others (mode {stat.S_IMODE(status.st_mode):04o});"
                f" a {subject} must grant them nothing, for example 0600 or 0400"
            )
        if status.st_size > MAX_SECRET_FILE_BYTES:
            raise UnsoundSecretFile(f"is larger than {MAX_SECRET_FILE_BYTES} bytes")
        return _read_within_limit(descriptor)
    finally:
        os.close(descriptor)


def _read_within_limit(descriptor: int) -> bytes | _ReadRefusal:
    chunks: list[bytes] = []
    remaining = MAX_SECRET_FILE_BYTES + 1
    try:
        while remaining and (chunk := os.read(descriptor, remaining)):
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as error:
        return _ReadRefusal(f"cannot be read: {error.strerror}")
    if remaining == 0:
        return _ReadRefusal(f"is larger than {MAX_SECRET_FILE_BYTES} bytes")
    return b"".join(chunks)
