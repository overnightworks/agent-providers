"""Replace a login token file without a restart.

The agent service reads a profile's ``token_file`` at the start of every run, so a
renewed token serves the next run. This script writes the new token (read from
standard input, never from an argument) into a file beside the old one at mode 0600,
checks it by the rules the service applies (``agent_runtime.secret_file``), and
moves it over the old file in one atomic rename. A refused token leaves the old file
untouched and the new file removed.

Usage: ``uv run python scripts/replace_token_file.py PATH < new-token``
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from agent_runtime.secret_file import UnsoundSecretFile, read_token_file

NEW_FILE_MODE = 0o600


def replace_token_file(path: Path, token: str) -> None:
    """Make ``path`` hold ``token``, or raise ``UnsoundSecretFile`` and change nothing."""
    beside = path.with_name(f".{path.name}.new")
    descriptor = os.open(beside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, NEW_FILE_MODE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as new_file:
            new_file.write(token.strip() + "\n")
        read_token_file(beside)
        os.replace(beside, path)
    except BaseException:
        beside.unlink(missing_ok=True)
        raise


def main(arguments: Sequence[str], standard_input: TextIO) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path", type=Path, help="the profile's token_file (absolute path)")
    path = parser.parse_args(arguments).path
    try:
        replace_token_file(path, standard_input.read())
    except UnsoundSecretFile as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin))
