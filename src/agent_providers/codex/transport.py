"""The Codex subscription CLI transport for one tool-loop turn."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final

from agent_providers.claude.provider import (
    flatten_messages,
    stdin_prompt,
)
from agent_providers.codex.pool import (
    CodexProcessKind,
    get_codex_process_pool,
)
from agent_providers.codex.protocol import (
    BLOCKED_ITEM_TYPES,
    CODEX_APPROVAL_POLICY_NEVER_CONFIG,
    CODEX_CLI_LINE_CHANNEL_CAPACITY,
    CODEX_EMPTY_MCP_SERVERS_CONFIG,
    CODEX_ITEM_COMPLETED_EVENT,
    INFORMATIONAL_ITEM_TYPES,
    ITEM_EVENT_TYPES,
    CodexCliStreamFailure,
    codex_child_process,
    codex_cli_failure_reason,
    create_codex_turn_home,
    error_item_message,
    event_item,
    event_item_type,
    failed_turn_message,
    parse_codex_line,
    run_reserved_codex_cli,
    top_level_error_message,
)
from agent_providers.constants import TURN_TIMEOUT_SECONDS
from agent_providers.errors import (
    CodexProcessPoolSaturatedError,
    ProviderUnavailableError,
    SafeRouteReasonCode,
    TextToolCallRejectedError,
    normalize_route_failure,
    raise_without_chain,
)
from agent_providers.process import (
    AgentCliUnavailableError,
    CliLineChannel,
    CliRunOutcome,
    PrivateHomeNotCreatedError,
    cli_settle_deadline,
)
from agent_providers.sandbox.paths import CODEX_TOOL_TURN_DIRECTORY_PREFIX
from agent_providers.text_tool_protocol import (
    FinalText as ParsedFinalText,
)
from agent_providers.text_tool_protocol import (
    TextToolCall,
    TextToolProtocolError,
    TextToolRejection,
    TextToolStreamParser,
    render_tool_result,
)
from agent_providers.tool_loop import (
    FinalText,
    InitialTurn,
    TextDelta,
    ToolCall,
    ToolCallBatch,
    ToolResultBatch,
    TransportResponse,
)
from agent_providers.tools import ToolCatalog

_CODEX_TOOL_ISOLATION_CONFIGS: Final = (
    CODEX_APPROVAL_POLICY_NEVER_CONFIG,
    CODEX_EMPTY_MCP_SERVERS_CONFIG,
    "features.shell_tool=false",
    "features.unified_exec=false",
    "features.browser_use=false",
    "features.computer_use=false",
    "features.multi_agent=false",
    "features.image_generation=false",
    "features.plugins=false",
    "features.hooks=false",
    'web_search="disabled"',
    "features.code_mode_host=false",
    "features.code_mode=false",
    "features.code_mode_only=false",
)
# Codex appends version-specific remediation after this stable isolation notice.
_CODE_MODE_HOST_DISABLED_ISOLATION_NOTICE_PREFIX: Final = (
    "Code Mode is unavailable because code-mode host is disabled"
)
log = logging.getLogger(__name__)


@dataclass
class _CodexToolRoundState:
    """Protocol state accumulated while receiving one Codex CLI round."""

    saw_success: bool = False
    error_message: str | None = None
    received_thread_id: str | None = None


class CodexCliToolTransport:
    """One private, resumable Codex CLI session for the shared tool loop.

    Codex receives each round on stdin. Its private home and persisted session
    are siblings of an empty private working directory and removed by
    :meth:`aclose`.
    """

    def __init__(self, *, model: str, catalog: ToolCatalog) -> None:
        self._model = model
        self._catalog = catalog
        try:
            self._turn_home = create_codex_turn_home(CODEX_TOOL_TURN_DIRECTORY_PREFIX)
        except PrivateHomeNotCreatedError:
            raise_without_chain(
                ProviderUnavailableError(
                    "codex",
                    "cli",
                    normalize_route_failure(SafeRouteReasonCode.CLI_BINARY_UNAVAILABLE),
                )
            )
        except AgentCliUnavailableError:
            raise_without_chain(
                ProviderUnavailableError(
                    "codex",
                    "cli",
                    normalize_route_failure(SafeRouteReasonCode.CLI_AUTH_REJECTED),
                )
            )
        self._deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        self._thread_id: str | None = None
        self._round_index = 0
        self._closed = False

    async def stream(
        self,
        message: InitialTurn | ToolResultBatch,
    ) -> AsyncIterator[TransportResponse]:
        """Stream one response, retaining only the server-issued thread ID."""
        if self._closed:
            raise RuntimeError("Codex CLI tool transport is closed")
        _refuse_a_multi_result_batch(message)
        prompt = _tool_transport_prompt(self._catalog, message)
        is_resume = self._thread_id is not None
        arguments = _codex_tool_arguments(
            self._model,
            thread_id=self._thread_id,
        )
        self._round_index += 1
        channel = CliLineChannel(CODEX_CLI_LINE_CHANNEL_CAPACITY)
        try:
            child = codex_child_process(arguments, self._turn_home)
        except AgentCliUnavailableError:
            raise_without_chain(
                ProviderUnavailableError(
                    "codex",
                    "cli",
                    normalize_route_failure(SafeRouteReasonCode.CLI_PROTOCOL_ERROR),
                )
            )
        home_reservation = self._turn_home.home.reserve()
        try:
            reservation = get_codex_process_pool().reserve(CodexProcessKind.TEXT)
        except CodexProcessPoolSaturatedError as exc:
            home_reservation.release()
            raise ProviderUnavailableError(
                "codex",
                "cli",
                normalize_route_failure(SafeRouteReasonCode.CLI_CAPACITY_EXHAUSTED),
            ) from exc
        except BaseException:  # NOSONAR(S5754) the home token is released, re-raises
            home_reservation.release()
            raise
        runner = asyncio.create_task(
            asyncio.to_thread(
                run_reserved_codex_cli,
                reservation,
                home_reservation,
                child,
                stdin_payload=prompt,
                deadline=self._deadline,
                channel=channel,
            )
        )
        parser = TextToolStreamParser(self._catalog)
        state = _CodexToolRoundState()
        started_at = time.monotonic()
        rejection: TextToolRejection | None = None
        stream_failure_reason: SafeRouteReasonCode | None = None
        try:
            while True:
                item = await asyncio.to_thread(
                    channel.receive, settle_by=cli_settle_deadline(self._deadline)
                )
                if isinstance(item, CliRunOutcome):
                    outcome = item
                    break
                event_type, event = parse_codex_line(item)
                text = _consume_codex_tool_event(
                    event_type,
                    event,
                    is_resume=is_resume,
                    expected_thread_id=self._thread_id,
                    parser=parser,
                    state=state,
                    channel=channel,
                )
                if text:
                    yield TextDelta(text)
            await asyncio.shield(runner)
            response, thread_id = _finish_codex_tool_round(
                outcome,
                is_resume=is_resume,
                state=state,
                parser=parser,
                round_index=self._round_index,
                started_at=started_at,
            )
            if thread_id is not None:
                self._thread_id = thread_id
            yield response
        except TextToolProtocolError as refused:
            channel.request_abort()
            await asyncio.shield(runner)
            rejection = refused.rejection
        except CodexCliStreamFailure as exc:
            channel.request_abort()
            await asyncio.shield(runner)
            stream_failure_reason = (
                SafeRouteReasonCode.TOOL_EXECUTION_FAILED
                if exc.code == "codex_cli_tool_call_blocked"
                else SafeRouteReasonCode.CLI_PROTOCOL_ERROR
            )
        finally:
            channel.request_abort()
            await asyncio.shield(runner)
        if rejection is not None:
            raise_without_chain(TextToolCallRejectedError("codex", "cli", rejection))
        if stream_failure_reason is not None:
            raise_without_chain(
                ProviderUnavailableError(
                    "codex",
                    "cli",
                    normalize_route_failure(stream_failure_reason),
                ),
            )

    async def aclose(self) -> None:
        """Remove all private session and prompt material after reaping a round."""
        if self._closed:
            return
        self._closed = True
        await asyncio.shield(asyncio.to_thread(self._turn_home.home.close))


def _codex_tool_arguments(
    model: str,
    *,
    thread_id: str | None = None,
) -> tuple[str, ...]:
    """Build a read-only start or resume command for one tool-loop round.

    Codex CLI 0.147 exposes ``--sandbox`` only on ``exec``.  ``exec resume``
    accepts the equivalent TOML override instead, so the sandbox is pinned on
    every round even though the command spelling differs.
    """
    common = (
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        *(part for config in _CODEX_TOOL_ISOLATION_CONFIGS for part in ("-c", config)),
        "-c",
        'sandbox_mode="read-only"',
        "--model",
        model,
    )
    if thread_id is None:
        return (
            "exec",
            "--sandbox",
            "read-only",
            *common,
            "-",
        )
    return (
        "exec",
        "resume",
        *common,
        thread_id,
        "-",
    )


def _refuse_a_multi_result_batch(message: InitialTurn | ToolResultBatch) -> None:
    if isinstance(message, ToolResultBatch) and len(message.results) != 1:
        raise ProviderUnavailableError(
            "codex",
            "cli",
            normalize_route_failure(SafeRouteReasonCode.TOOL_PROTOCOL_ERROR),
        )


def _tool_transport_prompt(
    catalog: ToolCatalog,
    message: InitialTurn | ToolResultBatch,
) -> bytes:
    """Render one initial prompt or exactly one completed tool result."""
    if isinstance(message, InitialTurn):
        return stdin_prompt(
            message.system,
            flatten_messages("", message.messages),
        ).encode()
    result = message.results[0]
    try:
        value = json.loads(result.content)
    except (json.JSONDecodeError, RecursionError):
        value = result.content
    return render_tool_result(catalog, value).encode()


def _consume_codex_tool_event(
    event_type: str,
    event: dict[str, object],
    *,
    is_resume: bool,
    expected_thread_id: str | None,
    parser: TextToolStreamParser,
    state: _CodexToolRoundState,
    channel: CliLineChannel,
) -> str | None:
    """Apply one Codex event and return any safe assistant-text delta."""
    if state.saw_success:
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    if event_type == "thread.started":
        _record_codex_thread_id(
            event,
            is_resume=is_resume,
            expected_thread_id=expected_thread_id,
            state=state,
        )
        return None
    if event_type == "turn.started":
        return None
    if event_type in ITEM_EVENT_TYPES:
        return _consume_codex_item_event(event_type, event, parser, state, channel)
    if event_type == "turn.completed":
        _completed_turn(event)
        state.saw_success = True
        return None
    if event_type == "error":
        state.error_message = top_level_error_message(event)
        channel.request_abort()
        return None
    if event_type == "turn.failed":
        state.error_message = failed_turn_message(event)
        channel.request_abort()
        return None
    raise CodexCliStreamFailure("codex_cli_stream_protocol_error")


def _record_codex_thread_id(
    event: dict[str, object],
    *,
    is_resume: bool,
    expected_thread_id: str | None,
    state: _CodexToolRoundState,
) -> None:
    thread_id = _thread_started_id(event)
    if state.received_thread_id is not None or (is_resume and thread_id != expected_thread_id):
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    state.received_thread_id = thread_id


def _consume_codex_item_event(
    event_type: str,
    event: dict[str, object],
    parser: TextToolStreamParser,
    state: _CodexToolRoundState,
    channel: CliLineChannel,
) -> str | None:
    item_type = event_item_type(event)
    if item_type in BLOCKED_ITEM_TYPES:
        raise CodexCliStreamFailure("codex_cli_tool_call_blocked")
    if event_type == CODEX_ITEM_COMPLETED_EVENT and item_type == "error":
        completed_error = error_item_message(event)
        if _is_code_mode_host_disabled_isolation_notice(completed_error):
            log.info("Codex CLI ignored its code-mode-host isolation notice")
            return None
        state.error_message = completed_error
        channel.request_abort()
        return None
    if item_type not in INFORMATIONAL_ITEM_TYPES:
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    if event_type == CODEX_ITEM_COMPLETED_EVENT and item_type == "agent_message":
        return parser.feed(_completed_agent_message(event))
    return None


def _finish_codex_tool_round(
    outcome: CliRunOutcome,
    *,
    is_resume: bool,
    state: _CodexToolRoundState,
    parser: TextToolStreamParser,
    round_index: int,
    started_at: float,
) -> tuple[TransportResponse, str | None]:
    """Validate one completed Codex round and produce its terminal response."""
    _raise_for_codex_outcome(outcome, state.saw_success, state.error_message, None)
    if not is_resume and state.received_thread_id is None:
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    parsed = parser.finish()
    if isinstance(parsed, TextToolCall):
        call = ToolCall(str(uuid.uuid4()), parsed.name, parsed.arguments)
        _log_codex_tool_round(round_index, started_at, call.name)
        return ToolCallBatch((call,)), state.received_thread_id
    if isinstance(parsed, ParsedFinalText):
        _log_codex_tool_round(round_index, started_at, None)
        return FinalText(parsed.text), state.received_thread_id
    raise CodexCliStreamFailure("codex_cli_stream_protocol_error")


def _thread_started_id(event: dict[str, object]) -> str:
    thread_id = event.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    try:
        return str(uuid.UUID(thread_id))
    except ValueError as exc:
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error") from exc


def _log_codex_tool_round(
    round_index: int,
    started_at: float,
    tool_name: str | None,
) -> None:
    duration_ms = round((time.monotonic() - started_at) * 1000)
    log.info(
        "Codex CLI tool round provider=codex route=cli round=%s duration_ms=%s tool=%s is_error=%s",
        round_index,
        duration_ms,
        tool_name or "none",
        False,
    )


def _completed_agent_message(event: dict[str, object]) -> str:
    item = event_item(event)
    text = item.get("text")
    if not isinstance(text, str):
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")
    return text


def _is_code_mode_host_disabled_isolation_notice(message: str) -> bool:
    """Recognize the one Codex notice caused by this adapter's isolation."""
    return message.startswith(_CODE_MODE_HOST_DISABLED_ISOLATION_NOTICE_PREFIX)


def _completed_turn(event: dict[str, object]) -> None:
    if not isinstance(event.get("usage"), dict):
        raise CodexCliStreamFailure("codex_cli_stream_protocol_error")


def _raise_for_codex_outcome(
    outcome: CliRunOutcome,
    saw_success: bool,
    error_message: str | None,
    completed_error_item_message: str | None,
) -> None:
    if saw_success:
        if not outcome.complete or outcome.returncode != 0:
            _raise_codex_cli_failure(outcome, None)
        return
    if completed_error_item_message is not None:
        _raise_codex_cli_failure(outcome, completed_error_item_message)
    if error_message is not None:
        _raise_codex_cli_failure(outcome, error_message)
    if not outcome.complete or outcome.returncode != 0:
        _raise_codex_cli_failure(outcome, None)
    raise ProviderUnavailableError(
        "codex",
        "cli",
        normalize_route_failure(SafeRouteReasonCode.CLI_PROTOCOL_ERROR),
    )


def _raise_codex_cli_failure(outcome: CliRunOutcome, error_message: str | None) -> None:
    log.warning(
        "Codex CLI failed (rc=%s, stderr_bytes=%d)",
        outcome.returncode,
        len(outcome.stderr.encode()),
    )
    raise ProviderUnavailableError(
        "codex",
        "cli",
        normalize_route_failure(codex_cli_failure_reason(error_message, outcome.stderr)),
    )


