"""The descendant handshake the real-leader rows share, and the script that starts it."""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

CONSUMES_STDIN = "while IFS= read -r line; do :; done\n"
WAITS_WITHOUT_READING_STDIN = "exec sleep 60\n"

DESCENDANT_PROGRAM = """\
import os
import signal
import sys

identity, ack, ready, escapes = sys.argv[1:5]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
null = os.open(os.devnull, os.O_RDWR)
for stream in (0, 1, 2):
    os.dup2(null, stream)
os.closerange(3, 1 << 16)
with open("/proc/self/stat") as stat:
    start_time = stat.read().rsplit(")", 1)[1].split()[19]
os.write(os.open(identity, os.O_WRONLY), f"{os.getpid()} {start_time}\\n".encode())
if not os.read(os.open(ack, os.O_RDONLY), 1):
    sys.exit(1)
if escapes == "1":
    os.setsid()
os.write(os.open(ready, os.O_WRONLY), b"ready\\n")
while True:
    signal.pause()
"""


@dataclass(frozen=True)
class Handshake:
    program: Path
    identity: Path
    ack: Path
    ready: Path


def open_handshake(directory: Path) -> Handshake:
    paths = Handshake(*(directory / name for name in ("descendant.py", "identity", "ack", "ready")))
    paths.program.write_text(DESCENDANT_PROGRAM)
    for fifo in (paths.identity, paths.ack, paths.ready):
        os.mkfifo(fifo)
    return paths


def lingering_descendant(
    answer: str, *, exit_code: int, blocks: bool, escapes: bool, handshake: Handshake,
) -> str:
    arguments = " ".join(
        shlex.quote(str(argument))
        for argument in (
            sys.executable,
            "-I",
            handshake.program,
            handshake.identity,
            handshake.ack,
            handshake.ready,
            int(escapes),
        )
    )
    starts_the_descendant = f"{arguments} </dev/null >/dev/null 2>&1 &\n"
    waits_for_its_handshake = f"read -r _ < {shlex.quote(str(handshake.ready))}\n"
    answers = CONSUMES_STDIN + answer + f"exit {exit_code}\n"
    return starts_the_descendant + waits_for_its_handshake + (
        WAITS_WITHOUT_READING_STDIN if blocks else answers
    )


def process_identity(pid: int) -> tuple[str, str] | None:
    """The state and start time of ``pid``, or ``None`` when the kernel lists no such process."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            fields = stat.read().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return fields[0], fields[19]
