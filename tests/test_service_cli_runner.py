"""The service's CLI runner answers a tool-free run through the real core and ends it correctly.

HTTP rows serve the real app on a loopback socket and run a fake Claude program as a real
child; no installed core configuration exists in any of them.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from anchored_support import GONE, TEST_WAIT_SECONDS, ChildCleanup
from provider_test_support import (
    INIT_LINE,
    SUCCESS_LINE,
    SYSTEM_TEXT,
    TEXT_LINE,
    a_claude_stdin_line,
    a_fake_claude,
    a_private_root,
    an_init,
    drive_real_children,
    held_claude_slots,
    line,
    own_real_children,
)
from service_support import (
    PINNED_CLAUDE_VERSION,
    PROCESS_TOKEN,
    ClientSocket,
    Drive,
    SteppedLoop,
    TokenMint,
    decoded_events,
    driven,
    failure_lines,
    get_run,
    post_run,
    record_signed_tokens,
    respond_through_the_app,
    run_body,
    run_headers,
    served,
    service_config,
)

from agent_client.wire import (
    ClientLoss,
    Failed,
    FailureCode,
    RunEvent,
    RunState,
    RunStatus,
)
from agent_providers import spawn
from agent_providers.config import PrivateWorkingRoot, reset_config
from agent_service.app import create_app
from agent_service.cli_runner import installed_runners
from agent_service.config import ServiceConfig
from agent_service.run_events import INVALID_EVENT_MESSAGE, NO_RUNNER_MESSAGE, RUNNER_FAILED_MESSAGE

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


def private_root_of(config: ServiceConfig) -> Path:
    assert config.cli is not None
    return config.cli.working_root.path


def drive_over_http[T](
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    config: ServiceConfig,
    scenario: Callable[[str], T],
) -> T:
    """Serve the app with the installed runners and run ``scenario(base_url)`` against it.

    The fake program's children are owned by ``child_cleanup`` and their watchers joined.
    """
    app = create_app(config, runners=installed_runners(config), fetch_key_set=MINT.fetch_key_set)

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


def test_an_app_with_tools_ends_no_provider_yet_without_a_child(
    tmp_path: Path, child_cleanup: ChildCleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    config = a_config(tmp_path, a_fake_claude(tmp_path, capture=capture), songmaker={})

    events = drive_over_http(child_cleanup, monkeypatch, config, post_a_run)

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
            "exit 1",
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


def eventually(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + TEST_WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, "the condition never held"
        threading.Event().wait(0.02)


def leave_after(base_url: str, event_type: str) -> uuid.UUID:
    """Start a run, read its stream up to ``event_type`` and close the connection."""
    body = json.dumps(run_body(turn=THREE_MESSAGE_TURN))
    with httpx.stream(
        "POST",
        f"{base_url}/runs",
        headers={"Content-Type": "application/json", **headers()},
        content=body,
        timeout=10,
    ) as response:
        assert response.status_code == 200
        run_id = None
        for text in response.iter_lines():
            if text.startswith("data: "):
                event = json.loads(text.removeprefix("data: "))
                run_id = run_id or uuid.UUID(event["run_id"])
                if event["type"] == event_type:
                    return run_id
    raise AssertionError(f"the stream ended before {event_type}")


def read_status(base_url: str, run_id: uuid.UUID) -> RunStatus:
    response = get_run(base_url, headers(), str(run_id))
    return RunStatus.model_validate_json(response.content)


PARKED_AFTER_ONE_TEXT = (INIT_LINE, TEXT_LINE)
PARKED = "exec sleep 600"


def a_parked_claude(
    directory: Path, *, for_the_client_socket: bool = False, says_again_after: Path | None = None
) -> Path:
    """A fake that streams one text and parks, saying one more once ``says_again_after`` is fed.

    The socket client asks the default one-line turn; the HTTP rows ask the three-message one.
    """
    then = PARKED
    if says_again_after is not None:
        again = an_assistant_line("again").decode()
        then = f"read -r go < {says_again_after}\ncat <<'EOF'\n{again}EOF\n{PARKED}"
    if for_the_client_socket:
        return a_fake_claude(
            directory,
            stream=PARKED_AFTER_ONE_TEXT,
            then=then,
            system_text="You help write songs.",
            stdin_line=a_claude_stdin_line("User: Hi"),
        )
    return a_fake_claude(directory, stream=PARKED_AFTER_ONE_TEXT, then=then)


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
        )
        next_run = leave_after(base_url, "run_started")
        eventually(lambda: read_status(base_url, next_run).state is not RunState.RUNNING)
        return while_held, afterwards, held_reap.reaps

    assert drive_over_http(child_cleanup, monkeypatch, config, scenario) == (
        (RunState.RUNNING, 429, [False]),
        (RunState.CANCELLED, ClientLoss.DISCONNECTED, [True], [], 0),
        2,
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
    config = a_config(tmp_path, binary, runs=runs)

    async def scenario() -> None:
        client = ClientSocket(blocks_the_second_chunk=blocks_the_client)
        async with driven(read=False) as drive:
            response = await respond_through_the_app(
                drive, client, monkeypatch, runners=installed_runners(config), config=config
            )
            await (
                client.blocked.wait() if blocks_the_client else client.received(b"assistant_text")
            )
            if later_activity:
                await drive.run_to(20)
                await asyncio.to_thread(feed.write_text, "go\n")
                await client.received(b"again")
            await drive.run_to_end()
            await settled_and_held(held_reap)
            while_held = (never_stopped_state(drive, client), all_children_gone(child_cleanup))
            held_reap.let_go.set()
            client.released.set()
            await response
            status = drive.status(client.run_id)
            assert status is not None
            assert (while_held, held_reap.reaps) == ((RunState.RUNNING, [False]), 1)
            assert (
                (status.state, status.failure_code, status.client_lost),
                all_children_gone(child_cleanup),
                list(private_root_of(config).iterdir()),
                held_claude_slots(),
            ) == ((state, failure_code, client_lost), [True], [], 0)

    in_stepped_loop(child_cleanup, monkeypatch, scenario())


def test_a_stop_at_a_yielded_event_closes_the_core_at_once(
    tmp_path: Path,
    child_cleanup: ChildCleanup,
    monkeypatch: pytest.MonkeyPatch,
    held_reap: HeldReap,
) -> None:
    binary = a_parked_claude(tmp_path, for_the_client_socket=True)
    config = a_config(tmp_path, binary)

    async def scenario() -> None:
        client = ClientSocket(blocks_the_second_chunk=True)
        async with driven(read=False) as drive:
            response = await respond_through_the_app(
                drive, client, monkeypatch, runners=installed_runners(config), config=config
            )
            await client.blocked.wait()
            await asyncio.get_running_loop().settled()
            client.leave()
            await settled_and_held(held_reap)
            while_held = (never_stopped_state(drive, client), all_children_gone(child_cleanup))
            held_reap.let_go.set()
            client.released.set()
            await response
            status = drive.status(client.run_id)
            assert status is not None
            assert (while_held, held_reap.reaps) == ((RunState.RUNNING, [False]), 1)
            assert (
                (status.state, status.client_lost),
                all_children_gone(child_cleanup),
                list(private_root_of(config).iterdir()),
                held_claude_slots(),
            ) == ((RunState.CANCELLED, ClientLoss.DISCONNECTED), [True], [], 0)

    in_stepped_loop(child_cleanup, monkeypatch, scenario())
