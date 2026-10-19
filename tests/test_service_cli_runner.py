"""The service's CLI runner answers a tool-free run through the real core and ends it correctly.

HTTP rows serve the real app on a loopback socket and run a fake Claude program as a real
child; no installed core configuration exists in any of them. The tool path has its own module
(``test_service_cli_runner_tools``). A tool-free run opens no connection to an app, so these
rows give the runner no TLS context.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import signal
import socket
import ssl
import struct
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import pytest
import uvicorn
from anchored_support import GONE, TEST_WAIT_SECONDS, ChildCleanup
from fastapi import FastAPI
from mcp_app_fake import TestAuthority
from provider_test_support import (
    INIT_LINE,
    SUCCESS_LINE,
    SYSTEM_TEXT,
    TEXT_LINE,
    ScriptedClaudeChild,
    a_child_that_outlives_sigkill,
    a_claude_cli_child,
    a_claude_stdin_line,
    a_fake_claude,
    a_private_root,
    an_init,
    drive_real_children,
    drive_scripted,
    held_claude_slots,
    line,
    own_real_children,
    the_only_tree,
)
from pydantic import ValidationError
from service_support import (
    ALICE,
    CLI_PROFILE,
    PINNED_CLAUDE_VERSION,
    PROCESS_TOKEN,
    SONGMAKER,
    TEST_ISSUER,
    ClientSocket,
    Drive,
    StepClock,
    SteppedLoop,
    TokenMint,
    _service_config_document,
    alice_admitted_run,
    decoded_events,
    driven,
    failure_lines,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    get_run,
    instant,
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    post_run,
    record_signed_tokens,
    respond_through,
    run_body,
    run_headers,
    served,
    service_config,
    the_app_on,
)

import agent_service.app as app_module
import agent_service.config as config_module
from agent_client.sse import RunStreamDecoder
from agent_client.wire import (
    ClientLoss,
    Failed,
    FailureCode,
    RunEvent,
    RunState,
    RunStatus,
)
from agent_providers import lost_groups, spawn
from agent_providers.config import PrivateWorkingRoot, reset_config
from agent_providers.events import AssistantTextEvent
from agent_providers.procfs import GroupCensus
from agent_providers.spawn import KillUntilGoneResult
from agent_service.admission import AdmissionSlot
from agent_service.app import create_app
from agent_service.capability import Capability
from agent_service.cli import main
from agent_service.cli_runner import RUN_ADAPTERS, CliRunner, installed_runners
from agent_service.config import McpConfig, Provider, ServiceConfig
from agent_service.quarantine import Quarantine
from agent_service.run_events import (
    INVALID_EVENT_MESSAGE,
    NO_RUNNER_MESSAGE,
    RUNNER_FAILED_MESSAGE,
    run_frames,
)
from agent_service.run_liveness import LoopClock
from agent_service.run_records import RunRecords
from agent_service.runners import Runners

MINT = TokenMint()
CANARY = "cli-text-" + uuid.uuid4().hex
PLAIN_APP = {"tools": {}, "mcp_origin": None}
VERSION_LINE = f"{PINNED_CLAUDE_VERSION} (Claude Code)"
THREE_MESSAGE_TURN = {
    "system": SYSTEM_TEXT,
    "messages": [
        {"role": "user", "text": "a"},
        {"role": "assistant", "text": "b"},
        {"role": "user", "text": "c"},
    ],
}


@pytest.fixture(autouse=True)
def no_installed_core_runtime() -> None:
    reset_config()


def a_config(
    tmp_path: Path,
    binary: Path,
    *,
    runs: dict[str, object] | None = None,
    songmaker: dict[str, object] | None = None,
) -> ServiceConfig:
    config = service_config(
        runs=runs, songmaker=PLAIN_APP if songmaker is None else songmaker, claude_binary=binary
    )
    assert config.cli is not None
    private_root = PrivateWorkingRoot(a_private_root(tmp_path))
    return config.model_copy(
        update={"cli": config.cli.model_copy(update={"working_root": private_root})}
    )


def trusting(config: ServiceConfig, authority: TestAuthority) -> ServiceConfig:
    return config.model_copy(update={"mcp": McpConfig(ca_file=str(authority.ca_file))})


def tool_free_runners(config: ServiceConfig) -> Runners:
    return installed_runners(config, None, LoopClock())


def private_root_of(config: ServiceConfig) -> Path:
    assert config.cli is not None
    return config.cli.working_root.path


def drive_over_http[T](
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    config: ServiceConfig,
    scenario: Callable[[str], T],
    runners: Runners | None = None,
) -> T:
    """Serve the app with ``runners`` (the installed ones by default) and run ``scenario``.

    The fake program's children are owned by ``child_cleanup`` and their watchers joined.
    """
    app = create_app(
        config,
        runners=runners or tool_free_runners(config),
        fetch_key_set=MINT.fetch_key_set,
    )

    def serve() -> T:
        with served(app) as base_url:
            return scenario(base_url)

    return own_real_children(child_cleanup, monkeypatch, serve)


def headers() -> dict[str, str]:
    return run_headers(MINT.workload(), MINT.user())


def post_a_run(base_url: str) -> list[RunEvent]:
    body = run_body(turn=THREE_MESSAGE_TURN)
    return decoded_events(post_run(base_url, headers(), body).content)


def shown(events: list[RunEvent]) -> list[tuple[str, str | None]]:
    return [
        (event.type, getattr(event, "text", None) or getattr(event, "message", None))
        for event in events[1:]
    ]


def status_of(base_url: str, events: list[RunEvent]) -> RunStatus:
    response = get_run(base_url, headers(), str(events[0].run_id))
    return RunStatus.model_validate_json(response.content)


def all_children_gone(child_cleanup: ChildCleanup) -> list[bool]:
    return [child_cleanup.state(owned) is GONE for owned in child_cleanup.listed()]


def test_a_tool_free_run_streams_the_cli_answer_and_succeeds(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = a_config(tmp_path, a_fake_claude(tmp_path))

    def scenario(base_url: str) -> tuple[list[RunEvent], RunStatus]:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert (events[0].type, shown(events), status.state) == (
        "run_started",
        [("assistant_text", "hi"), ("succeeded", "done")],
        RunState.SUCCEEDED,
    )


def captured_carriers(capture: Path) -> dict[str, str]:
    files = {
        str(file.relative_to(capture)): file.read_text()
        for file in capture.rglob("*")
        if file.is_file() and file.parent != capture
    }
    return {
        "argv": (capture / "argv").read_text(),
        "stdin": (capture / "stdin").read_text(),
        "environment": (capture / "environment").read_text(),
        **files,
    }


def test_the_cli_receives_the_turns_system_history_token_and_bound(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    binary = a_fake_claude(tmp_path, token=PROCESS_TOKEN, max_turns=8, capture=capture)
    config = a_config(tmp_path, binary, runs={"max_tool_calls": 7})

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    carriers = captured_carriers(capture)
    assert (shown(events)[-1], carriers["environment"].count(PROCESS_TOKEN)) == (
        ("succeeded", "done"),
        1,
    )
    assert [
        name for name, text in carriers.items() if name != "environment" and PROCESS_TOKEN in text
    ] == []


def test_each_run_leaves_no_directory_slot_or_place_behind(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = a_config(tmp_path, a_fake_claude(tmp_path), runs={"max_parallel_cli_runs": 1})

    def scenario(base_url: str) -> list[tuple[str | None, list[Path], int, list[bool]]]:
        afterwards = []
        for _ in range(2):
            events = post_a_run(base_url)
            afterwards.append(
                (
                    shown(events)[-1][0],
                    list(private_root_of(config).iterdir()),
                    held_claude_slots(),
                    all_children_gone(child_cleanup),
                )
            )
        return afterwards

    assert drive_over_http(child_cleanup, monkeypatch, config, scenario) == [
        ("succeeded", [], 0, [True]),
        ("succeeded", [], 0, [True, True]),
    ]


def test_a_capability_never_reaches_the_tool_free_cli(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    signed = record_signed_tokens(monkeypatch)
    config = a_config(tmp_path, a_fake_claude(tmp_path, capture=capture), songmaker={"tools": {}})

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

    carriers = captured_carriers(capture)
    assert (bool(signed), shown(events)[-1][0]) == (True, "succeeded")
    assert [name for name, text in carriers.items() if any(token in text for token in signed)] == []


def test_without_a_cli_section_a_tool_app_is_provider_unavailable(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    config = a_config(tmp_path, a_fake_claude(tmp_path, capture=capture), songmaker={})
    without_cli = config.model_copy(update={"cli": None})

    events = drive_over_http(child_cleanup, monkeypatch, without_cli, post_a_run)

    terminal = events[-1]
    assert isinstance(terminal, Failed)
    assert (terminal.code, terminal.message, child_cleanup.listed(), capture.exists()) == (
        FailureCode.PROVIDER_UNAVAILABLE,
        NO_RUNNER_MESSAGE,
        (),
        False,
    )


def an_assistant_line(text: str) -> bytes:
    return line({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


LOGIN_REFUSED_LINE = line(
    {
        "type": "assistant",
        "is_api_error_message": True,
        "error": "authentication_failed",
        "message": {"content": [{"type": "text", "text": f"Not logged in {CANARY}"}]},
    }
)
TURN_LIMIT_LINE = line(
    {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": CANARY}
)
FOREIGN_INIT = an_init(apiKeySource="ANTHROPIC_API_KEY")


@pytest.mark.parametrize(
    ("stream", "then", "class_name", "taken", "verdict"),
    [
        pytest.param(
            (INIT_LINE, LOGIN_REFUSED_LINE), "exit 0", "CliLoginRefusedError", [], None, id="login"
        ),
        pytest.param(
            (INIT_LINE, TEXT_LINE, SUCCESS_LINE),
            f"echo {CANARY} >&2; echo {CANARY}; exit 1",
            "UnavailableError",
            [("assistant_text", "hi")],
            None,
            id="exit",
        ),
        pytest.param(
            (INIT_LINE, TEXT_LINE),
            "exit 0",
            "UnavailableError",
            [("assistant_text", "hi")],
            None,
            id="missing-result",
        ),
        pytest.param(
            (FOREIGN_INIT, TEXT_LINE),
            "exit 0",
            "RunSurfaceRefused",
            [],
            "FOREIGN_CREDENTIAL",
            id="surface",
        ),
        pytest.param(
            (INIT_LINE, TURN_LIMIT_LINE), "exit 0", "UnavailableError", [], None, id="turn-limit"
        ),
    ],
)
def test_a_failing_cli_ends_with_the_services_text_and_its_closed_cause(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stream: tuple[bytes, ...],
    then: str,
    class_name: str,
    taken: list[tuple[str, str]],
    verdict: str | None,
) -> None:
    config = a_config(tmp_path, a_fake_claude(tmp_path, stream=stream, then=then))

    def scenario(base_url: str) -> tuple[list[RunEvent], RunStatus]:
        events = post_a_run(base_url)
        return events, status_of(base_url, events)

    events, status = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert shown(events) == [*taken, ("failed", RUNNER_FAILED_MESSAGE)]
    assert [(event, kind) for event, kind, _ in failure_lines(caplog)] == [
        (f"The runner of run {events[0].run_id} failed", class_name)
    ]
    assert (
        verdict is None or verdict in caplog.text,
        CANARY in caplog.text,
        status.failure_code,
    ) == (
        True,
        False,
        FailureCode.PROVIDER_ERROR,
    )


LONE_SURROGATE_LINE = an_assistant_line("x\ud800 " + CANARY)
SURROGATE_RESULT_LINE = line(
    {"type": "result", "subtype": "success", "is_error": False, "result": "x\ud800 " + CANARY}
)


@pytest.mark.parametrize(
    "stream",
    [
        pytest.param((INIT_LINE, LONE_SURROGATE_LINE, SUCCESS_LINE), id="assistant-text"),
        pytest.param((INIT_LINE, SURROGATE_RESULT_LINE), id="result"),
    ],
)
def test_a_lone_surrogate_from_the_cli_fails_closed_without_its_text(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stream: tuple[bytes, ...],
) -> None:
    config = a_config(tmp_path, a_fake_claude(tmp_path, stream=stream))

    def scenario(base_url: str) -> tuple[bytes, list[RunEvent]]:
        response = post_run(base_url, headers(), run_body(turn=THREE_MESSAGE_TURN))
        return response.content, decoded_events(response.content)

    raw, events = drive_over_http(child_cleanup, monkeypatch, config, scenario)

    assert shown(events) == [("failed", INVALID_EVENT_MESSAGE)]
    assert (CANARY.encode() in raw, CANARY in caplog.text) == (False, False)


def test_a_run_whose_directories_cannot_be_made_fails_after_it_started(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = a_config(tmp_path, a_fake_claude(tmp_path))
    private_root_of(config).chmod(0o500)

    try:
        events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)
    finally:
        private_root_of(config).chmod(0o700)

    assert (events[0].type, shown(events), child_cleanup.listed()) == (
        "run_started",
        [("failed", RUNNER_FAILED_MESSAGE)],
        (),
    )
    assert [kind for _, kind, _ in failure_lines(caplog)] == ["CliEnvironmentUnavailableError"]


class HeldReap:
    """A gate in front of the real group reap: it waits, off the loop, until the test lets go."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.let_go = threading.Event()
        self.reaps = 0

    def wait_until_reached(self) -> None:
        assert self.reached.wait(TEST_WAIT_SECONDS), "the run was never stopped"


@pytest.fixture
def held_reap(monkeypatch: pytest.MonkeyPatch) -> Iterator[HeldReap]:
    hold = HeldReap()
    reap = spawn.AnchoredChild.kill_until_gone

    async def held(child: spawn.AnchoredChild, *, within: float) -> spawn.KillUntilGoneResult:
        hold.reaps += 1
        hold.reached.set()
        await asyncio.to_thread(hold.let_go.wait, TEST_WAIT_SECONDS)
        return await reap(child, within=within)

    monkeypatch.setattr(spawn.AnchoredChild, "kill_until_gone", held)
    yield hold
    hold.let_go.set()


POLL_INTERVAL_SECONDS = 0.02


def eventually(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + TEST_WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "the condition never held"
        time.sleep(POLL_INTERVAL_SECONDS)


def leave_after(base_url: str, event_type: str) -> uuid.UUID:
    """Start a run, read its stream up to ``event_type`` and reset the connection.

    A reset reaches the server as a lost connection without a write from it.
    """
    body = json.dumps(run_body(turn=THREE_MESSAGE_TURN)).encode()
    request_headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
        "Connection": "close",
        **headers(),
    }
    head = "".join(f"{name}: {value}\r\n" for name, value in request_headers.items())
    host, port = urlsplit(base_url).netloc.split(":")
    with socket.create_connection((host, int(port)), timeout=TEST_WAIT_SECONDS) as client:
        client.sendall(f"POST /runs HTTP/1.1\r\nHost: {host}\r\n{head}\r\n".encode() + body)
        received = b""
        while f'"type":"{event_type}"'.encode() not in received:
            chunk = client.recv(4096)
            assert chunk, f"the stream ended before {event_type}"
            received += chunk
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    match = re.search(rb'"run_id":"([0-9a-f-]{36})"', received)
    assert match is not None
    return uuid.UUID(match.group(1).decode())


def read_status(base_url: str, run_id: uuid.UUID) -> RunStatus:
    response = get_run(base_url, headers(), str(run_id))
    return RunStatus.model_validate_json(response.content)


PARKED_AFTER_ONE_TEXT = (INIT_LINE, TEXT_LINE)
PARKED_AFTER_THREE_TEXTS = (
    INIT_LINE,
    TEXT_LINE,
    an_assistant_line("two"),
    an_assistant_line("three"),
)
PARKED = "exec sleep 600"


def a_parked_claude(
    directory: Path,
    *,
    for_the_client_socket: bool = False,
    says_again_after: Path | None = None,
    stream: tuple[bytes, ...] = PARKED_AFTER_ONE_TEXT,
) -> Path:
    """A fake that streams its texts and parks, saying one more once ``says_again_after`` is fed.

    The socket client asks the default one-line turn; the HTTP rows ask the three-message one.
    """
    then = PARKED
    if says_again_after is not None:
        again = an_assistant_line("again").decode()
        then = f"read -r go < {says_again_after}\ncat <<'EOF'\n{again}EOF\n{PARKED}"
    if for_the_client_socket:
        return a_fake_claude(
            directory,
            stream=stream,
            then=then,
            system_text="You help write songs.",
            stdin_line=a_claude_stdin_line("User: Hi"),
        )
    return a_fake_claude(directory, stream=stream, then=then)


def test_a_client_that_leaves_ends_the_cli_before_the_place_is_free(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    held_reap: HeldReap,
) -> None:
    binary = a_parked_claude(tmp_path)
    config = a_config(tmp_path, binary, runs={"max_parallel_cli_runs": 1})

    def scenario(base_url: str) -> tuple[Any, ...]:
        run_id = leave_after(base_url, "assistant_text")
        held_reap.wait_until_reached()
        while_held = (
            read_status(base_url, run_id).state,
            post_run(base_url, headers(), run_body(turn=THREE_MESSAGE_TURN)).status_code,
            all_children_gone(child_cleanup),
        )
        held_reap.let_go.set()
        eventually(lambda: read_status(base_url, run_id).state is not RunState.RUNNING)
        status = read_status(base_url, run_id)
        afterwards = (
            status.state,
            status.client_lost,
            all_children_gone(child_cleanup),
            list(private_root_of(config).iterdir()),
            held_claude_slots(),
            held_reap.reaps,
        )
        next_run = leave_after(base_url, "assistant_text")
        eventually(lambda: read_status(base_url, next_run).state is not RunState.RUNNING)
        return while_held, afterwards, all_children_gone(child_cleanup)

    assert drive_over_http(child_cleanup, monkeypatch, config, scenario) == (
        (RunState.RUNNING, 429, [False]),
        (RunState.CANCELLED, ClientLoss.DISCONNECTED, [True], [], 0, 1),
        [True, True],
    )


def in_stepped_loop(
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    scenario: Coroutine[Any, Any, None],
) -> None:
    drive_real_children(child_cleanup, monkeypatch, scenario, loop_factory=SteppedLoop)


async def settled_and_held(held_reap: HeldReap) -> None:
    await asyncio.to_thread(held_reap.wait_until_reached)


def never_stopped_state(drive: Drive, client: ClientSocket) -> RunState:
    status = drive.status(client.run_id)
    assert status is not None
    return status.state


class StoppedRun(NamedTuple):
    status: RunStatus
    afterwards: tuple[list[bool], list[Path], int, int, int, list[bool]]


async def post_a_second_run(app: FastAPI) -> ClientSocket:
    second = ClientSocket()
    await respond_through(app, second)
    return second


def answer_status(client: ClientSocket) -> int:
    return client.messages[0]["status"]


HELD_WITH_THE_PLACE_TAKEN = (RunState.RUNNING, [False], 429)


async def stopped_run_held_then_released(
    app: FastAPI,
    drive: Drive,
    client: ClientSocket,
    response: asyncio.Task[None],
    held_reap: HeldReap,
    child_cleanup: ChildCleanup,
    config: ServiceConfig,
) -> StoppedRun:
    """Observe a stopped run while its reap is held and once it is released.

    The place is taken while held when a second run is refused ``busy``, and free
    after the release when the next run is admitted.
    """
    await settled_and_held(held_reap)
    refused = await post_a_second_run(app)
    while_held = (
        never_stopped_state(drive, client),
        all_children_gone(child_cleanup),
        answer_status(refused),
    )
    assert while_held == HELD_WITH_THE_PLACE_TAKEN
    held_reap.let_go.set()
    client.released.set()
    await response
    status = drive.status(client.run_id)
    assert status is not None
    after_the_stop = (
        all_children_gone(child_cleanup),
        list(private_root_of(config).iterdir()),
        held_claude_slots(),
        held_reap.reaps,
    )
    admitted = ClientSocket()
    admitted_response = await respond_through(app, admitted)
    await admitted.received(b"assistant_text")
    admitted.leave()
    await admitted_response
    afterwards = (*after_the_stop, answer_status(admitted), all_children_gone(child_cleanup))
    return StoppedRun(status, afterwards)


def the_place_is_free_after_one_reap(afterwards: tuple[Any, ...]) -> bool:
    return afterwards == ([True], [], 0, 1, 200, [True, True])


@pytest.mark.parametrize(
    ("runs", "blocks_the_client", "later_activity", "state", "failure_code", "client_lost"),
    [
        pytest.param({}, False, False, RunState.FAILED, FailureCode.IDLE_TIMEOUT, None, id="idle"),
        pytest.param(
            {"idle_timeout_s": 590.0},
            False,
            True,
            RunState.FAILED,
            FailureCode.LIMIT_EXCEEDED,
            None,
            id="maximum",
        ),
        pytest.param({}, True, False, RunState.CANCELLED, None, ClientLoss.DROPPED, id="stall"),
    ],
)
def test_every_stop_reaps_the_cli_once_before_the_record_ends(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    held_reap: HeldReap,
    runs: dict[str, object],
    blocks_the_client: bool,
    later_activity: bool,
    state: RunState,
    failure_code: FailureCode | None,
    client_lost: ClientLoss | None,
) -> None:
    feed = tmp_path / "feed"
    os.mkfifo(feed)
    binary = a_parked_claude(
        tmp_path, for_the_client_socket=True, says_again_after=feed if later_activity else None
    )
    config = a_config(tmp_path, binary, runs={**runs, "max_parallel_cli_runs": 1})

    async def scenario() -> None:
        client = ClientSocket(blocks_the_second_chunk=blocks_the_client)
        async with driven(read=False) as drive:
            app = the_app_on(drive, monkeypatch, tool_free_runners(config), config)
            response = await respond_through(app, client)
            await (
                client.blocked.wait() if blocks_the_client else client.received(b"assistant_text")
            )
            if later_activity:
                await drive.run_to(20)
                await asyncio.to_thread(feed.write_text, "go\n")
                await client.received(b"again")
            await drive.run_to_end()
            stopped = await stopped_run_held_then_released(
                app, drive, client, response, held_reap, child_cleanup, config
            )
            assert (
                stopped.status.state,
                stopped.status.failure_code,
                stopped.status.client_lost,
            ) == (state, failure_code, client_lost)
            assert the_place_is_free_after_one_reap(stopped.afterwards)

    in_stepped_loop(child_cleanup, monkeypatch, scenario())


def test_a_stop_at_a_yielded_event_closes_the_core_at_once(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    held_reap: HeldReap,
) -> None:
    binary = a_parked_claude(tmp_path, for_the_client_socket=True, stream=PARKED_AFTER_THREE_TEXTS)
    config = a_config(tmp_path, binary, runs={"max_parallel_cli_runs": 1})

    async def scenario() -> None:
        client = ClientSocket(blocks_the_second_chunk=True)
        async with driven(read=False) as drive:
            app = the_app_on(drive, monkeypatch, tool_free_runners(config), config)
            response = await respond_through(app, client)
            await client.blocked.wait()
            await asyncio.get_running_loop().settled()
            client.leave()
            stopped = await stopped_run_held_then_released(
                app, drive, client, response, held_reap, child_cleanup, config
            )
            assert (stopped.status.state, stopped.status.client_lost) == (
                RunState.CANCELLED,
                ClientLoss.DISCONNECTED,
            )
            assert the_place_is_free_after_one_reap(stopped.afterwards)

    in_stepped_loop(child_cleanup, monkeypatch, scenario())


def test_a_closed_runner_has_reaped_the_cli_before_its_close_returns(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = a_config(tmp_path, a_parked_claude(tmp_path, for_the_client_socket=True))

    async def scenario() -> tuple[AssistantTextEvent, int, list[Path], list[bool]]:
        runner = CliRunner(config, tls_context=None, clock=StepClock())
        events = runner.run(alice_admitted_run(uuid.uuid4()))
        first = await anext(events)
        await events.aclose()
        return (
            first,
            held_claude_slots(),
            list(private_root_of(config).iterdir()),
            all_children_gone(child_cleanup),
        )

    assert drive_real_children(child_cleanup, monkeypatch, scenario()) == (
        AssistantTextEvent(text="hi"),
        0,
        [],
        [True],
    )


@dataclass
class ScriptedRun:
    """A run of the real runner over a scripted child, read frame by frame from the stream."""

    frames: AsyncGenerator[bytes, None]
    slot: AdmissionSlot
    records: RunRecords
    clock: StepClock
    run_id: uuid.UUID
    root: Path

    def place_is_held(self) -> bool:
        probe = self.slot.try_take()
        if probe is not None:
            probe.release()
        return probe is None

    def status(self) -> RunStatus:
        status = self.records.read(self.run_id, "songmaker", ALICE)
        assert status is not None
        return status

    def held_state(self) -> tuple[bool, int, bool]:
        return self.place_is_held(), held_claude_slots(), the_only_tree(self.root).is_dir()

    async def frees_its_place_once_the_group_is_confirmed(self, child: ScriptedClaudeChild) -> None:
        retry = next(
            task for task in asyncio.all_tasks() if task.get_name().startswith("quarantine:")
        )
        self.clock.advance_to(await self.clock.parked() or 0)
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            while (deadline := await self.clock.parked()) is None:
                await asyncio.sleep(0.01)
        assert self.held_state() == (True, 1, True)
        child.publish_exit(-signal.SIGKILL)
        self.clock.advance_to(deadline)
        await asyncio.wait_for(retry, TEST_WAIT_SECONDS)
        assert (self.place_is_held(), held_claude_slots(), list(self.root.iterdir())) == (
            False,
            0,
            [],
        )


def a_scripted_run(
    tmp_path: Path,
    *,
    config: ServiceConfig | None = None,
    tls_context: ssl.SSLContext | None = None,
    capability: Capability | None = None,
) -> ScriptedRun:
    config = config or a_config(tmp_path, a_fake_claude(tmp_path))
    clock = StepClock()
    slot = AdmissionSlot(1)
    records = RunRecords(lambda: instant(clock.now))
    run_id = uuid.uuid4()
    frames = run_frames(
        dataclasses.replace(alice_admitted_run(run_id), capability=capability),
        CliRunner(config, tls_context=tls_context, clock=clock),
        SONGMAKER,
        config.runs,
        clock,
        records,
        slot.try_take(),
        quarantine=Quarantine(clock, lost_groups.confirm_group_gone),
    )
    return ScriptedRun(frames, slot, records, clock, run_id, private_root_of(config))


def test_a_group_that_outlives_the_stop_keeps_the_runs_place_until_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = a_child_that_outlives_sigkill(
        censuses=(GroupCensus.LIVE, GroupCensus.EMPTY),
        stderr_stalls=True,
        stream_lines=[INIT_LINE, TEXT_LINE],
    )
    run = a_scripted_run(tmp_path)

    async def scenario() -> None:
        await anext(run.frames)
        await anext(run.frames)
        async with asyncio.timeout(TEST_WAIT_SECONDS / 4):
            await run.frames.aclose()
        status = run.status()
        assert (status.state, status.failure_code) == (RunState.FAILED, FailureCode.PROVIDER_ERROR)
        assert run.held_state() == (True, 1, True)
        await run.frees_its_place_once_the_group_is_confirmed(child)

    drive_scripted(monkeypatch, child, scenario(), loop_factory=SteppedLoop)


def test_an_unconfirmed_group_after_a_clean_end_never_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = a_claude_cli_child(
        stream_lines=[INIT_LINE, TEXT_LINE, SUCCESS_LINE],
        returncode=0,
        gone=(KillUntilGoneResult(GroupCensus.LIVE, ended_in_time=True),),
        censuses=(GroupCensus.LIVE, GroupCensus.EMPTY),
        stderr_stalls=True,
    )
    run = a_scripted_run(tmp_path)

    async def scenario() -> None:
        decoder = RunStreamDecoder()
        async with asyncio.timeout(TEST_WAIT_SECONDS / 4):
            async for frame in run.frames:
                decoder.feed(frame)
        status = run.status()
        assert (status.state, status.failure_code) == (RunState.FAILED, FailureCode.PROVIDER_ERROR)
        assert run.held_state() == (True, 1, True)
        await run.frees_its_place_once_the_group_is_confirmed(child)

    drive_scripted(monkeypatch, child, scenario(), loop_factory=SteppedLoop)


def through_the_service_entry[Result](
    tmp_path: Path,
    config: ServiceConfig,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    scenario: Callable[[str], Result],
) -> Result:
    """Start ``cli.main`` on ``config``, serve the app it builds and play ``scenario`` on it."""
    served_apps: list[FastAPI] = []
    create_app_for_the_test = app_module.create_app

    def create_app_with_the_test_key_set(built: ServiceConfig, **keywords: Any):
        return create_app_for_the_test(built, fetch_key_set=MINT.fetch_key_set, **keywords)

    monkeypatch.setattr(uvicorn, "run", lambda built, **_where: served_apps.append(built))
    monkeypatch.setattr(config_module, "load_config", lambda _path: config)
    monkeypatch.setattr(app_module, "create_app", create_app_with_the_test_key_set)

    def start_and_play() -> Result:
        assert main(["--config", str(tmp_path / "agent-service.toml")]) == 0
        with served(served_apps[0]) as base_url:
            return scenario(base_url)

    return own_real_children(child_cleanup, monkeypatch, start_and_play)


def test_the_service_entry_installs_the_cli_runner(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    logging_state_restored: None,
    authority: TestAuthority,
) -> None:
    binary = a_fake_claude(tmp_path, version_line=VERSION_LINE)
    config = trusting(a_config(tmp_path, binary, songmaker=PLAIN_APP), authority)

    events = through_the_service_entry(tmp_path, config, child_cleanup, monkeypatch, post_a_run)

    assert shown(events)[-1] == ("succeeded", "done")


@pytest.mark.parametrize("provider", list(Provider))
def test_a_profile_loads_only_for_a_provider_with_a_run_adapter(provider: Provider) -> None:
    document = _service_config_document(TEST_ISSUER, None, None)
    document["profiles"][CLI_PROFILE]["provider"] = provider.value

    try:
        ServiceConfig.model_validate(document)
    except ValidationError as refusal:
        refusal_names_the_provider = provider.value in str(refusal)
        loads = False
    else:
        refusal_names_the_provider = True
        loads = True

    assert (loads, refusal_names_the_provider) == (provider in RUN_ADAPTERS, True)
