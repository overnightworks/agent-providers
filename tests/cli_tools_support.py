"""A fake Claude that talks to the run's relay, and a TLS fake app, for the CLI runner's tool path.

``a_relay_calling_claude`` writes an executable Python stand-in: it reads ``$HOME/relay-mcp.json``,
speaks ``initialize``, ``tools/list`` and ``tools/call`` to the relay with the bearer header
the file names, announces the relay as connected, optionally prints the CLI's own
``tool_use``/``tool_result`` lines, records every start fact and every reply it received under
``capture``, and ends with a ``result`` line. A refused
``tools/list`` leaves it with no tools, and ``give_up_when_present`` makes it give each call up
(``notifications/cancelled``, then a closed connection) once that file exists, instead of waiting.
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp_app_fake import (
    CERTIFICATE_HOST,
    McpAppFake,
    RecordedRequest,
    Responder,
    ScriptedApp,
    TestAuthority,
    serving,
    tool_listing,
)

STRAY_TOOL_LINES = (
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "toolu_stray", "name": "mcp__relay__stray", "input": {}}
            ]
        },
    },
    {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "toolu_stray", "content": "stray"}]
        },
    },
)

_CLAUDE_SOURCE = """\
#!{python}
import http.client, json, os, sys, time, urllib.error, urllib.parse, urllib.request

PLAN = json.loads({plan!r})
if sys.argv[1:] == ["--version"]:
    print(PLAN["version_line"])
    sys.exit(0)
stdin_line = sys.stdin.readline()
home = os.environ["HOME"]
capture = PLAN["capture"]
marker = None
if PLAN["marked_by_system_prompt"]:
    marker = open(home + "/system-prompt").read()
    capture = capture + "/" + marker
os.makedirs(capture + "/files", exist_ok=True)
with open(capture + "/stdin", "w") as handle:
    handle.write(stdin_line)
with open(capture + "/argv", "w") as handle:
    handle.write("\\n".join(sys.argv[1:]))
with open(capture + "/environment", "w") as handle:
    handle.write("\\n".join(f"{{k}}={{v}}" for k, v in os.environ.items()))
for name in os.listdir(home):
    with open(home + "/" + name, "rb") as source, open(capture + "/files/" + name, "wb") as copy:
        copy.write(source.read())
relay = json.load(open(home + "/relay-mcp.json"))["mcpServers"]["relay"]


def rpc(method, params=None, request_id=None, version=None):
    body = {{"jsonrpc": "2.0", "method": method}}
    if request_id is not None:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    headers = {{**relay["headers"], "Content-Type": "application/json"}}
    if version:
        headers["MCP-Protocol-Version"] = version
    request = urllib.request.Request(
        relay["url"], data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=PLAN["timeout"]) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except (urllib.error.URLError, ConnectionError, OSError):
        return None
    with open(capture + "/replies", "a") as handle:
        reply = {{"method": method, "status": status, "body": raw.decode()}}
        handle.write(json.dumps(reply) + "\\n")
    return json.loads(raw) if raw else None


def wait_for(path):
    deadline = time.monotonic() + PLAN["timeout"]
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            sys.exit(3)
        time.sleep(0.02)


def call_then_give_up(arguments, request_id, version):
    address = urllib.parse.urlsplit(relay["url"])
    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=PLAN["timeout"])
    body = {{"jsonrpc": "2.0", "method": "tools/call", "id": request_id, "params": arguments}}
    headers = {{
        **relay["headers"], "Content-Type": "application/json", "MCP-Protocol-Version": version
    }}
    connection.request("POST", address.path, body=json.dumps(body), headers=headers)
    wait_for(PLAN["give_up_when_present"])
    rpc("notifications/cancelled", {{"requestId": request_id}}, version=version)
    connection.close()


def emit(payload):
    print(json.dumps(payload), flush=True)


listing = None
version = None
if PLAN["relay_status"] == "connected":
    version = rpc("initialize", {{"protocolVersion": "2025-11-25"}}, 1)["result"]["protocolVersion"]
    rpc("notifications/initialized", version=version)
    listing = (rpc("tools/list", {{}}, 2, version) or {{}}).get("result", {{}}).get("tools")
tool_names = ["mcp__relay__" + tool["name"] for tool in listing or []]
emit({{
    "type": "system", "subtype": "init", "tools": tool_names, "apiKeySource": "none",
    "mcp_servers": [{{"name": "relay", "status": PLAN["relay_status"]}}],
}})
time.sleep(PLAN["linger_before_calls_seconds"])
for number, call in enumerate(PLAN["calls"], start=1):
    for stray in PLAN["stray_lines"]:
        emit(stray)
    marked = {{"marker": marker}} if marker else {{}}
    arguments = {{"name": call["name"], "arguments": {{**call["arguments"], **marked}}}}
    if PLAN["give_up_when_present"]:
        call_then_give_up(arguments, 10 + number, version)
    else:
        rpc("tools/call", arguments, 10 + number, version)
for stray in PLAN["stray_lines"]:
    emit(stray)
time.sleep(PLAN["linger_seconds"])
if PLAN["exit_code"]:
    sys.exit(PLAN["exit_code"])
emit({{"type": "result", "subtype": "success", "is_error": False, "result": "done"}})
"""


@dataclass(frozen=True)
class ClaudeCall:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


def a_relay_calling_claude(
    directory: Path,
    *,
    version: str,
    calls: Sequence[ClaudeCall] = (),
    relay_status: str = "connected",
    stray_tool_lines: bool = False,
    exit_code: int = 0,
    linger_seconds: float = 0.0,
    linger_before_calls_seconds: float = 0.0,
    give_up_when_present: Path | None = None,
    timeout_seconds: float = 20.0,
    marked_by_system_prompt: bool = False,
) -> Path:
    """The pinned program's stand-in for a relay run; it captures into ``directory/capture``.

    With ``marked_by_system_prompt`` one executable serves parallel runs: the run's system text
    is its marker, naming its capture directory ``capture/<marker>`` and travelling in every
    call's arguments as ``marker``.
    """
    plan = {
        "version_line": f"{version} (Claude Code)",
        "capture": str(directory / "capture"),
        "calls": [{"name": call.name, "arguments": dict(call.arguments)} for call in calls],
        "relay_status": relay_status,
        "stray_lines": list(STRAY_TOOL_LINES) if stray_tool_lines else [],
        "exit_code": exit_code,
        "linger_seconds": linger_seconds,
        "linger_before_calls_seconds": linger_before_calls_seconds,
        "give_up_when_present": None if give_up_when_present is None else str(give_up_when_present),
        "timeout": timeout_seconds,
        "marked_by_system_prompt": marked_by_system_prompt,
    }
    binary = directory / "claude"
    binary.write_text(_CLAUDE_SOURCE.format(python=sys.executable, plan=json.dumps(plan)))
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


@dataclass(frozen=True)
class ChildCapture:
    """What the fake Claude recorded about its start and every reply the relay gave it."""

    directory: Path

    def text_of(self, name: str) -> str:
        return (self.directory / name).read_text() if (self.directory / name).exists() else ""

    @property
    def started(self) -> bool:
        return (self.directory / "argv").exists()

    @property
    def everything_it_was_given(self) -> str:
        files = self.directory / "files"
        file_texts = (
            [path.read_text() for path in sorted(files.iterdir())] if files.exists() else []
        )
        return "\n".join(
            [self.text_of("argv"), self.text_of("environment"), self.stdin, *file_texts]
        )

    @property
    def relay(self) -> dict[str, Any]:
        return json.loads(self.run_files["relay-mcp.json"])["mcpServers"]["relay"]

    @property
    def relay_secret(self) -> str:
        return self.relay["headers"]["Authorization"].removeprefix("Bearer ")

    @property
    def relay_port(self) -> int:
        return urlsplit(self.relay["url"]).port or 0

    @property
    def stdin(self) -> str:
        return self.text_of("stdin")

    @property
    def run_files(self) -> dict[str, str]:
        files = self.directory / "files"
        return {path.name: path.read_text() for path in files.iterdir()} if files.exists() else {}

    @property
    def replies(self) -> list[dict[str, Any]]:
        lines = self.text_of("replies").splitlines()
        return [reply for reply in map(json.loads, lines) if reply["method"] == "tools/call"]


@dataclass(frozen=True)
class ToolApp:
    """The TLS fake app of a tool run: its origin, trust and what it was asked."""

    fake: McpAppFake

    @property
    def origin(self) -> str:
        return self.fake.origin

    @property
    def calls(self) -> list[RecordedRequest]:
        return [request for request in self.fake.requests if request.rpc_method == "tools/call"]

    @property
    def capability(self) -> str:
        bearer = self.fake.requests[0].headers["authorization"]
        return bearer.removeprefix("Bearer ")


@contextmanager
def a_tool_app(
    serving_authority: TestAuthority,
    tools: Sequence[str] = ("save_note", "read_note"),
    *,
    overrides: Mapping[str, Responder] | None = None,
) -> Iterator[ToolApp]:
    app = ScriptedApp(pages=[tool_listing(list(tools))], overrides=overrides)
    with serving(app, serving_authority.credentials_for(CERTIFICATE_HOST)) as fake:
        yield ToolApp(fake)
