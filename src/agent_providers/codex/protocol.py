"""The Codex CLI's shared plumbing: its isolation, its login, its events.

Both Codex routes — the tool transport and the image
turn — drive the same binary: they install the same redacted login into a
private home, spawn through the same admission-bound runner, and read the
same JSON event grammar. That common ground lives here so neither route
owns the other's internals.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agent_providers.codex.pool import CodexProcessReservation, get_codex_process_pool
from agent_providers.codex_login import blanked_copy
from agent_providers.config import current_config
from agent_providers.errors import SafeRouteReasonCode, raise_without_chain
from agent_providers.process import (
    CODEX_CLI_BINARY_MISSING_DETAIL,
    CliEnvironmentCause,
    CliEnvironmentUnavailableError,
    CliLineChannel,
    PrivateCredentialHome,
    PrivateHomeReservation,
    require_cli_binary,
    run_cli_bounded,
    run_publishing_failures,
)
from agent_providers.sandbox.paths import CODEX_HOME_DIRECTORY_NAME
from agent_providers.spawn import ChildProcess, closed_environment

CODEX_CLI_LINE_CHANNEL_CAPACITY: Final = 64
CODEX_TURN_LOGIN_MODE: Final = 0o600
CODEX_CLI_TURN_OUTPUT_READ_LIMIT_BYTES: Final = 4 * 1024 * 1024

_AUTH_FAILURE_MARKERS: Final = ("401", "unauthorized", "unauthenticated")

CODEX_APPROVAL_POLICY_NEVER_CONFIG: Final = 'approval_policy="never"'
CODEX_EMPTY_MCP_SERVERS_CONFIG: Final = "mcp_servers={}"
CODEX_ITEM_COMPLETED_EVENT: Final = "item.completed"
BLOCKED_ITEM_TYPES: Final = frozenset(
    {
        "collab_agent_tool_call",
        "command_execution",
        "file_change",
        "image_generation",
        "mcp_tool_call",
        "web_search",
    }
)
INFORMATIONAL_ITEM_TYPES: Final = frozenset(
    {
        "agent_message",
        "reasoning",
        "todo_list",
    }
)
ITEM_EVENT_TYPES: Final = frozenset(
    {
        "item.started",
        "item.updated",
        CODEX_ITEM_COMPLETED_EVENT,
    }
)
INFORMATIONAL_EVENT_TYPES: Final = frozenset({"thread.started", "turn.started"})


class CodexCliStreamFailure(Exception):
    """The streamed protocol named a terminal adapter failure."""

    def __init__(self, code: str) -> None:
        self.code = code


@dataclass(frozen=True)
class CodexTurnHome:
    """One turn's private home and the two directories the Codex child runs in."""

    home: PrivateCredentialHome
    work_directory: Path
    codex_home: Path


def codex_model_arguments(model: str | None) -> tuple[str, ...]:
    """The ``--model`` pair for a chosen model; none for the CLI's own default."""
    return () if model is None else ("--model", model)


def codex_child_process(arguments: tuple[str, ...], turn_home: CodexTurnHome) -> ChildProcess:
    """Describe one Codex child around this turn's own private directories.

    ``HOME`` is the turn home, not the account that started this process, so
    the CLI discovers only the redacted login this turn installed under
    ``CODEX_HOME``.
    """
    binary = require_cli_binary(current_config().codex_cli_binary, CODEX_CLI_BINARY_MISSING_DETAIL)
    return ChildProcess(
        binary=binary,
        arguments=arguments,
        environment=closed_environment(
            turn_home.home.path.resolve(), CODEX_HOME=str(turn_home.codex_home.resolve())
        ),
        working_directory=turn_home.work_directory.resolve(),
    )


def release_turn_reservations(
    home_reservation: PrivateHomeReservation,
    release_slot: Callable[[], None],
) -> None:
    """Release a turn's home token and its pool slot, the slot even when the token raises."""
    try:
        home_reservation.release()
    finally:
        release_slot()


def run_reserved_codex_cli(
    reservation: CodexProcessReservation,
    home_reservation: PrivateHomeReservation,
    child: ChildProcess,
    *,
    stdin_payload: bytes,
    deadline: float,
    channel: CliLineChannel,
) -> None:
    """Run one already-admitted CLI process through the bounded runner.

    The runner releases both reservations through its callbacks; a failure of
    this step ends the channel instead of raising.
    """
    process_pool = get_codex_process_pool()

    def on_spawned(process_id: int) -> None:
        process_pool.bind(reservation, process_id)

    def on_spawn_failed() -> None:
        release_turn_reservations(
            home_reservation, lambda: process_pool.abandon_unspawned(reservation)
        )

    def on_reaped(process_id: int, _became_zombie: bool) -> None:
        release_turn_reservations(
            home_reservation, lambda: process_pool.reap(reservation, process_id)
        )

    run_publishing_failures(
        channel,
        lambda: run_cli_bounded(
            child,
            on_spawned=on_spawned,
            on_spawn_failed=on_spawn_failed,
            on_reaped=on_reaped,
            stdin_payload=stdin_payload,
            read="all",
            deadline=deadline,
            output_read_limit_bytes=CODEX_CLI_TURN_OUTPUT_READ_LIMIT_BYTES,
            stdout_line_channel=channel,
        ),
    )


def create_codex_turn_home(prefix: str) -> CodexTurnHome:
    """Make one private Codex turn home holding ``work`` and the blanked login copy."""
    home = PrivateCredentialHome(
        current_config().codex_cli_auth_file,
        Path(CODEX_HOME_DIRECTORY_NAME) / "auth.json",
        prefix=prefix,
        missing_credential_is_error=True,
        credential_copy=blanked_copy,
        mode=CODEX_TURN_LOGIN_MODE,
    )
    try:
        work_directory = home.path / "work"
        work_directory.mkdir(mode=0o700)
        return CodexTurnHome(
            home=home,
            work_directory=work_directory,
            codex_home=home.path / CODEX_HOME_DIRECTORY_NAME,
        )
    except OSError:
        home.close()
        raise_without_chain(
            CliEnvironmentUnavailableError(
                "could not create a private CLI home", cause=CliEnvironmentCause.HOME_NOT_CREATED,
            )
        )
    except BaseException:  # NOSONAR(S5754) closes the home, re-raises
        home.close()
        raise


def parse_codex_line(line: bytes) -> tuple[str, dict[str, object]]:
    try:
        event = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        event = None
    if not isinstance(event, dict):
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    event_type = event.get("type")
    if not isinstance(event_type, str):
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    return event_type, event


def error_item_message(event: dict[str, object]) -> str:
    message = event_item(event).get("message")
    if not isinstance(message, str):
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    return message


def event_item_type(event: dict[str, object]) -> str:
    item_type = event_item(event).get("type")
    if not isinstance(item_type, str):
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    return item_type


def event_item(event: dict[str, object]) -> dict[str, object]:
    item = event.get("item")
    if not isinstance(item, dict):
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    return item


def top_level_error_message(event: dict[str, object]) -> str:
    message = event.get("message")
    if not isinstance(message, str):
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    return message


def failed_turn_message(event: dict[str, object]) -> str:
    error = event.get("error")
    if not isinstance(error, dict):
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    message = error.get("message")
    if not isinstance(message, str):
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    return message


def codex_cli_failure_reason(*messages: str | None) -> SafeRouteReasonCode:
    """Classify a Codex CLI failure without retaining its payload."""
    if any(_contains_auth_failure(message) for message in messages):
        return SafeRouteReasonCode.CLI_AUTH_REJECTED
    return SafeRouteReasonCode.CLI_PROTOCOL_ERROR


def _contains_auth_failure(value: str | None) -> bool:
    return value is not None and any(marker in value.lower() for marker in _AUTH_FAILURE_MARKERS)
