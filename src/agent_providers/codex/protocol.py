"""The Codex CLI's shared plumbing: its isolation, its login, its events.

Both Codex routes — the tool transport and the image
turn — drive the same binary: they install the same redacted login into a
private home, spawn through the same admission-bound runner, and read the
same JSON event grammar. That common ground lives here so neither route
owns the other's internals.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from agent_providers.codex.pool import CodexProcessReservation, get_codex_process_pool
from agent_providers.codex_login import blanked_copy
from agent_providers.config import current_config
from agent_providers.errors import SafeRouteReasonCode, raise_without_chain
from agent_providers.process import (
    AgentCliUnavailableError,
    CliRunOutcome,
    CliRunReason,
    PrivateCredentialHome,
    PrivateHomeNotCreatedError,
    resolve_cli_binary,
    run_cli_bounded,
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


def codex_child_process(
    arguments: tuple[str, ...],
    *,
    turn_root: Path,
    work_directory: Path,
    codex_home: Path,
) -> ChildProcess:
    """Describe one Codex child around this turn's own private directories.

    ``HOME`` is the turn root, not the account that started this process, so
    the CLI discovers only the redacted login this turn installed under
    ``CODEX_HOME``.
    """
    binary = resolve_cli_binary(current_config().codex_cli_binary)
    if binary is None:
        raise AgentCliUnavailableError("the configured Codex CLI binary is not on the search path")
    return ChildProcess(
        binary=binary,
        arguments=arguments,
        environment=closed_environment(turn_root, CODEX_HOME=str(codex_home)),
        working_directory=work_directory,
    )


def run_reserved_codex_cli(
    reservation: CodexProcessReservation,
    child: ChildProcess,
    **kwargs,
) -> CliRunOutcome:
    """Run one already-admitted CLI process through the bounded runner."""
    process_pool = get_codex_process_pool()

    def on_spawned(process_id: int) -> None:
        process_pool.bind(reservation, process_id)

    def on_spawn_failed() -> None:
        process_pool.abandon_unspawned(reservation)

    def on_reaped(process_id: int, _became_zombie: bool) -> None:
        process_pool.reap(reservation, process_id)

    try:
        outcome = run_cli_bounded(
            child,
            on_spawned=on_spawned,
            on_spawn_failed=on_spawn_failed,
            on_reaped=on_reaped,
            **kwargs,
        )
    except BaseException:
        process_pool.abandon_unspawned(reservation)
        raise
    if outcome.reason is CliRunReason.SPAWN_FAILED:
        process_pool.abandon_unspawned(reservation)
    return outcome


def create_codex_turn_home(prefix: str) -> PrivateCredentialHome:
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
        (home.path / "work").mkdir(mode=0o700)
    except OSError:
        home.close()
        raise_without_chain(PrivateHomeNotCreatedError("could not create a private CLI home"))
    except BaseException:  # NOSONAR(S5754) closes the home, re-raises
        home.close()
        raise
    return home


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
