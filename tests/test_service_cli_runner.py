"""The service's CLI runner answers a tool-free run through the real core and ends it correctly.

HTTP rows serve the real app on a loopback socket and run a fake Claude program as a real
child; no installed core configuration exists in any of them.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from anchored_leaders import spawn_warnings
from anchored_kernel import join_watcher, recorded_watcher_starts
from anchored_support import GONE, ChildCleanup
from provider_test_support import (
    INIT_LINE,
    SUCCESS_LINE,
    SYSTEM_TEXT,
    TEXT_LINE,
    a_fake_claude,
    a_private_root,
    an_init,
    held_claude_slots,
    line,
)
from service_support import (
    CLI_PROFILE,
    PINNED_CLAUDE_VERSION,
    PROCESS_TOKEN,
    TokenMint,
    decoded_events,
    failure_lines,
    get_run,
    post_run,
    record_signed_tokens,
    run_body,
    run_headers,
    served,
    service_config,
)

from agent_client.wire import (
    AssistantText,
    Failed,
    FailureCode,
    RunEvent,
    RunState,
    RunStatus,
    Succeeded,
)
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
    return config.model_copy(update={"cli": config.cli.model_copy(update={"working_root": private_root})})


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

    children_before = len(child_cleanup.listed())
    with recorded_watcher_starts(monkeypatch) as watchers:
        try:
            return child_cleanup.capture(serve).value  # type: ignore[return-value]
        finally:
            for owned in child_cleanup.listed()[children_before:]:
                join_watcher(watchers.thread(owned.pid))


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
    assert [name for name, text in carriers.items() if name != "environment" and PROCESS_TOKEN in text] == []


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
    assert [
        name for name, text in carriers.items() if any(token in text for token in signed)
    ] == []


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
        pytest.param((INIT_LINE, LOGIN_REFUSED_LINE), "exit 0", "CliLoginRefusedError", [], None, id="login"),
        pytest.param(
            (INIT_LINE, TEXT_LINE, SUCCESS_LINE), "exit 1", "UnavailableError", [("assistant_text", "hi")], None, id="exit"
        ),
        pytest.param(
            (INIT_LINE, TEXT_LINE), "exit 0", "UnavailableError", [("assistant_text", "hi")], None, id="missing-result"
        ),
        pytest.param((FOREIGN_INIT, TEXT_LINE), "exit 0", "RunSurfaceRefused", [], "FOREIGN_CREDENTIAL", id="surface"),
        pytest.param((INIT_LINE, TURN_LIMIT_LINE), "exit 0", "UnavailableError", [], None, id="turn-limit"),
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
    assert (verdict is None or verdict in caplog.text, CANARY in caplog.text, status.failure_code) == (
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
