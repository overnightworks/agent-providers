"""Grok subscription CLI transport for one co-writer tool-loop turn."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agent_providers.claude.provider import (
    flatten_messages,
    stdin_prompt,
)
from agent_providers.config import current_config, current_turn_config
from agent_providers.constants import (
    COWRITER_CLI_TIMEOUT_SECONDS,
    COWRITER_GROK_CLI_LINE_CHANNEL_CAPACITY,
    GROK_CLI_STREAMING_OUTPUT_FORMAT,
)
from agent_providers.errors import (
    ProviderUnavailableError,
    SafeRouteReasonCode,
    ToolProtocolRejectionCode,
    normalize_route_failure,
)
from agent_providers.process import (
    AgentCliUnavailableError,
    CliCredentialNotConfiguredError,
    CliLineChannel,
    CliRunOutcome,
    PrivateCredentialHome,
    resolve_cli_binary,
    run_cli_bounded,
)
from agent_providers.spawn import ChildProcess, closed_environment
from agent_providers.text_tool_protocol import (
    FinalText as ParsedFinalText,
)
from agent_providers.text_tool_protocol import (
    TextToolCall,
    TextToolProtocolError,
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

_AUTH_FAILURE_MARKERS: Final = ("401", "oidc", "unauthenticated")
# "usage" is dropped with the rest: this transport reports no token deltas
# yet. Surfacing them as a `UsageEvent` is named follow-up work on #825
# (planner gap 7, atelier-2 port requirement 7) and starts here.
_IGNORED_EVENT_TYPES: Final = frozenset({"thought", "usage", "available_commands", "plan"})
_PROMPT_FILE_ARGUMENT_INDEX: Final = 1
# Grok owns this larger bound because a 600-second streamed turn can include
# substantial thought and usage NDJSON before its final answer.
GROK_CLI_TURN_OUTPUT_READ_LIMIT_BYTES: Final = 4 * 1024 * 1024
GROK_CLI_TURN_DIRECTORY_PREFIX: Final = "songmaker-grok-cli-"

log = logging.getLogger(__name__)


class _GrokCliStreamFailure(Exception):
    """The streamed protocol named a terminal adapter failure."""

    def __init__(self, code: ToolProtocolRejectionCode) -> None:
        self.code = code


@dataclass
class _GrokToolRoundState:
    """Protocol state accumulated while receiving one Grok CLI round."""

    saw_end: bool = False
    error_message: str | None = None
    received_session_id: str | None = None


class GrokCliToolTransport:
    """One private, resumable Grok CLI session for the shared tool loop.

    A unique credential home contains both Grok's throwaway profile and its
    session files. Every round in a transport shares that home; another turn
    receives a different one.
    """

    def __init__(self, *, model: str, catalog: ToolCatalog) -> None:
        self._model = model
        self._catalog = catalog
        try:
            self._home = PrivateCredentialHome(
                current_config().grok_cli_auth_file,
                Path(".grok/auth.json"),
                prefix=GROK_CLI_TURN_DIRECTORY_PREFIX,
                missing_credential_is_error=True,
            )
        except CliCredentialNotConfiguredError as exc:
            raise ProviderUnavailableError(
                "grok",
                "cli",
                normalize_route_failure(SafeRouteReasonCode.CLI_LOGIN_NOT_CONFIGURED),
            ) from exc
        except AgentCliUnavailableError as exc:
            raise ProviderUnavailableError(
                "grok",
                "cli",
                normalize_route_failure(SafeRouteReasonCode.CLI_PROTOCOL_ERROR),
            ) from exc
        self._working_directory = self._home.path.resolve()
        self._deadline = time.monotonic() + COWRITER_CLI_TIMEOUT_SECONDS
        self._session_id: str | None = None
        self._round_index = 0
        self._closed = False

    def _child_for(self, arguments: tuple[str, ...]) -> ChildProcess:
        binary = resolve_cli_binary(current_config().grok_cli_binary)
        if binary is None:
            raise ProviderUnavailableError(
                "grok",
                "cli",
                normalize_route_failure(SafeRouteReasonCode.CLI_PROTOCOL_ERROR),
            )
        return ChildProcess(
            binary=binary,
            arguments=arguments,
            environment=_grok_turn_environment(self._working_directory),
            working_directory=self._working_directory,
        )

    async def stream(
        self,
        message: InitialTurn | ToolResultBatch,
    ) -> AsyncIterator[TransportResponse]:
        """Stream one response and retain its server-issued session ID."""
        if self._closed:
            raise RuntimeError("Grok CLI tool transport is closed")
        try:
            prompt = _tool_transport_prompt(self._catalog, message)
        except TextToolProtocolError:
            _log_tool_protocol_rejection(ToolProtocolRejectionCode.TOOL_RESULT_BATCH_INVALID)
            raise ProviderUnavailableError(
                "grok",
                "cli",
                normalize_route_failure(SafeRouteReasonCode.TOOL_PROTOCOL_ERROR),
            ) from None
        is_resume = self._session_id is not None
        if is_resume:
            arguments = _grok_cli_tool_arguments(self._model, self._session_id)
        else:
            arguments = _grok_cli_tool_arguments(self._model)
        self._round_index += 1
        channel = CliLineChannel(COWRITER_GROK_CLI_LINE_CHANNEL_CAPACITY)
        try:
            child = self._child_for(arguments)
            home_reservation = self._home.reserve()
        except BaseException:
            self._home.close()
            raise
        runner = asyncio.create_task(asyncio.to_thread(
            run_cli_bounded,
            child,
            stdin_payload=None,
            read="all",
            deadline=self._deadline,
            output_read_limit_bytes=GROK_CLI_TURN_OUTPUT_READ_LIMIT_BYTES,
            stdout_line_channel=channel,
            prompt_file_bytes=prompt,
            prompt_file_arg_index=_PROMPT_FILE_ARGUMENT_INDEX,
            on_spawn_failed=home_reservation.on_spawn_failed,
            on_reaped=home_reservation.on_reaped,
        ))
        parser = TextToolStreamParser(self._catalog)
        state = _GrokToolRoundState()
        started_at = time.monotonic()
        try:
            while True:
                item = await asyncio.to_thread(channel.receive)
                if isinstance(item, CliRunOutcome):
                    outcome = item
                    break
                event_type, event_data = _parse_grok_line(item)
                text = _consume_grok_tool_event(event_type, event_data, parser, state, channel)
                if text:
                    yield TextDelta(text)
            await asyncio.shield(runner)
            response, session_id = _finish_grok_tool_round(
                outcome,
                is_resume=is_resume,
                expected_session_id=self._session_id,
                state=state,
                parser=parser,
                round_index=self._round_index,
                started_at=started_at,
            )
            self._session_id = session_id
            yield response
        except TextToolProtocolError:
            channel.request_abort()
            await asyncio.shield(runner)
            _log_tool_protocol_rejection(ToolProtocolRejectionCode.TEXT_TOOL_RESPONSE_INVALID)
            raise ProviderUnavailableError(
                "grok",
                "cli",
                normalize_route_failure(SafeRouteReasonCode.TOOL_PROTOCOL_ERROR),
            ) from None
        except _GrokCliStreamFailure as exc:
            channel.request_abort()
            await asyncio.shield(runner)
            _log_tool_protocol_rejection(exc.code)
            reason = (
                SafeRouteReasonCode.TOOL_PROTOCOL_ERROR
                if exc.code is ToolProtocolRejectionCode.NATIVE_TOOL_BLOCKED
                else SafeRouteReasonCode.CLI_PROTOCOL_ERROR
            )
            raise ProviderUnavailableError(
                "grok",
                "cli",
                normalize_route_failure(reason),
            ) from None
        finally:
            channel.request_abort()
            await asyncio.shield(runner)

    async def aclose(self) -> None:
        """Request cleanup after every current or background reaper finishes."""
        if self._closed:
            return
        self._closed = True
        self._home.close()


def _tool_transport_prompt(
    catalog: ToolCatalog,
    message: InitialTurn | ToolResultBatch,
) -> bytes:
    if isinstance(message, InitialTurn):
        return stdin_prompt(
            message.system,
            flatten_messages("", message.messages),
        ).encode()
    if len(message.results) != 1:
        raise TextToolProtocolError()
    result = message.results[0]
    try:
        value = json.loads(result.content)
    except json.JSONDecodeError:
        value = result.content
    return render_tool_result(catalog, value).encode()


def _consume_grok_tool_event(
    event_type: str,
    event: dict[str, object],
    parser: TextToolStreamParser,
    state: _GrokToolRoundState,
    channel: CliLineChannel,
) -> str | None:
    """Apply one Grok event and return any safe assistant-text delta."""
    if state.saw_end:
        raise _GrokCliStreamFailure(ToolProtocolRejectionCode.CLI_STREAM_RESPONSE_INVALID)
    if event_type in {"tool_call", "tool_call_update"}:
        raise _GrokCliStreamFailure(ToolProtocolRejectionCode.NATIVE_TOOL_BLOCKED)
    if event_type == "text":
        return parser.feed(_text_event_data(event))
    if event_type == "end":
        _end_event_data(event)
        state.received_session_id = _stream_session_id(event)
        state.saw_end = True
        return None
    if event_type == "error":
        state.error_message = _error_event_data(event)
        channel.request_abort()
        return None
    if event_type in _IGNORED_EVENT_TYPES:
        return None
    raise _GrokCliStreamFailure(ToolProtocolRejectionCode.CLI_STREAM_RESPONSE_INVALID)


def _finish_grok_tool_round(
    outcome: CliRunOutcome,
    *,
    is_resume: bool,
    expected_session_id: str | None,
    state: _GrokToolRoundState,
    parser: TextToolStreamParser,
    round_index: int,
    started_at: float,
) -> tuple[TransportResponse, str]:
    """Validate one completed Grok round and produce its terminal response."""
    _raise_for_grok_outcome(outcome, state.saw_end, state.error_message)
    session_id = state.received_session_id
    if session_id is None or (is_resume and session_id != expected_session_id):
        raise _GrokCliStreamFailure(ToolProtocolRejectionCode.CLI_STREAM_RESPONSE_INVALID)
    parsed = parser.finish()
    if isinstance(parsed, TextToolCall):
        call = ToolCall(str(uuid.uuid4()), parsed.name, parsed.arguments)
        _log_tool_round(round_index, session_id, started_at, call.name)
        return ToolCallBatch((call,)), session_id
    if isinstance(parsed, ParsedFinalText):
        _log_tool_round(round_index, session_id, started_at, None)
        return FinalText(parsed.text), session_id
    raise _GrokCliStreamFailure(ToolProtocolRejectionCode.CLI_STREAM_RESPONSE_INVALID)


def _grok_cli_tool_arguments(
    model: str,
    session_id: str | None = None,
) -> tuple[str, ...]:
    command = [
        "--prompt-file",
        current_turn_config().cli_prompt_file_placeholder,
        "--output-format",
        GROK_CLI_STREAMING_OUTPUT_FORMAT,
        "--deny",
        "*",
        "--max-turns",
        "1",
        "--no-subagents",
        "--disable-web-search",
        "--model",
        model,
    ]
    if session_id is not None:
        command.extend(("--resume", session_id))
    return tuple(command)


def _stream_session_id(event: dict[str, object]) -> str:
    session_id = event.get("sessionId")
    if not isinstance(session_id, str):
        raise _GrokCliStreamFailure(ToolProtocolRejectionCode.CLI_STREAM_RESPONSE_INVALID)
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise _GrokCliStreamFailure(
            ToolProtocolRejectionCode.CLI_STREAM_RESPONSE_INVALID,
        ) from exc
    return session_id


def _grok_turn_environment(home: Path) -> dict[str, str]:
    return closed_environment(home)


def _log_tool_round(
    round_index: int,
    session_id: str,
    started_at: float,
    tool_name: str | None,
) -> None:
    duration_ms = round((time.monotonic() - started_at) * 1000)
    log.info(
        "Co-writer Grok CLI provider=grok route=cli round=%s session=%s "
        "duration_ms=%s tool=%s is_error=%s",
        round_index,
        session_id,
        duration_ms,
        tool_name or "none",
        False,
    )


def _log_tool_protocol_rejection(code: ToolProtocolRejectionCode) -> None:
    log.warning(
        "Co-writer tool protocol rejection provider=grok route=cli code=%s",
        code,
    )


def _parse_grok_line(line: bytes) -> tuple[str, dict[str, object]]:
    try:
        parsed = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    if not isinstance(parsed, dict):
        raise _GrokCliStreamFailure(ToolProtocolRejectionCode.CLI_STREAM_RESPONSE_INVALID)
    event_type = parsed.get("type")
    if not isinstance(event_type, str):
        raise _GrokCliStreamFailure(ToolProtocolRejectionCode.CLI_STREAM_RESPONSE_INVALID)
    return event_type, parsed


def _text_event_data(event: dict[str, object]) -> str:
    data = event.get("data")
    if not isinstance(data, str):
        raise _GrokCliStreamFailure(ToolProtocolRejectionCode.CLI_STREAM_RESPONSE_INVALID)
    return data


def _end_event_data(event: dict[str, object]) -> None:
    if not isinstance(event.get("stopReason"), str):
        raise _GrokCliStreamFailure(ToolProtocolRejectionCode.CLI_STREAM_RESPONSE_INVALID)


def _error_event_data(event: dict[str, object]) -> str:
    message = event.get("message")
    if not isinstance(message, str):
        raise _GrokCliStreamFailure(ToolProtocolRejectionCode.CLI_STREAM_RESPONSE_INVALID)
    return message


def _raise_for_grok_outcome(
    outcome: CliRunOutcome,
    saw_end: bool,
    error_message: str | None,
) -> None:
    if error_message is not None or not outcome.complete or outcome.returncode != 0 or not saw_end:
        if _contains_auth_failure(error_message) or _contains_auth_failure(outcome.stderr):
            raise ProviderUnavailableError(
                "grok",
                "cli",
                normalize_route_failure(SafeRouteReasonCode.CLI_AUTH_REJECTED),
            )
        raise ProviderUnavailableError(
            "grok",
            "cli",
            normalize_route_failure(SafeRouteReasonCode.CLI_PROTOCOL_ERROR),
        )


def _contains_auth_failure(value: str | None) -> bool:
    return value is not None and any(marker in value.lower() for marker in _AUTH_FAILURE_MARKERS)
