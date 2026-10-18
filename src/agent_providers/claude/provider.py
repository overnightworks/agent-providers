"""Claude provider — unified interface for CLI and API backends.

The Claude CLI and API backends, the MCP-attached and tool-free command
lines, and the tool-surface gate with its health state.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import glob
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from pydantic import SecretStr

from agent_client.wire import TurnMessage
from agent_providers import process
from agent_providers.anchored_turn import (
    CliProcessPool,
    CliProcessPoolSaturated,
    ReapBounds,
    anchored_turn_child,
)
from agent_providers.config import (
    McpServerSpec,
    current_claude_runtime,
    current_config,
    current_turn_config,
)
from agent_providers.constants import (
    CLAUDE_CLI_COMPLETION_TIMEOUT_SECONDS,
    CLAUDE_CLI_CURRENT_MODEL_MARKER,
    CLAUDE_CLI_MAX_CONCURRENT_PROCESSES,
    CLAUDE_CLI_MODEL_LIST_MARKER,
    CLAUDE_CLI_NO_TOOL_SURFACE_TIMEOUT_SECONDS,
    CLAUDE_CLI_TOOL_SURFACE_FAILURE_CACHE_SECONDS,
    CLAUDE_CLI_TOOL_SURFACE_TIMEOUT_SECONDS,
    CLAUDE_CLI_ZOMBIE_FAILURE_CACHE_SECONDS,
    CLI_GROUP_CONFIRMATION_SECONDS,
    CLI_OUTPUT_READ_LIMIT_BYTES,
    CLI_TERMINATION_GRACE_SECONDS,
    DEADLINE_EXCEEDED_REASON,
    PROBE_TIMEOUT_SECONDS,
)
from agent_providers.errors import (
    ProviderUnavailableError,
    SafeRouteReasonCode,
    normalize_route_failure,
    refuse_empty_model,
)
from agent_providers.events import (
    AssistantTextEvent,
    FinalEvent,
    StreamEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_providers.process import (
    CliLogin,
    claude_cli_login,
    clear_claude_cli_login_cache,
    resolve_cli_binary,
    run_claude_catalog_cli,
)
from agent_providers.spawn import (
    AnchoredChild,
    ChildProcess,
    ChildProcessSpecificationError,
    PipedProcessTimeout,
    closed_environment,
    run_capturing,
)
from agent_providers.transcript import flatten_messages, stdin_prompt

log = logging.getLogger(__name__)

CLAUDE_CLI_PROCESS_POOL: Final = CliProcessPool(
    limit=CLAUDE_CLI_MAX_CONCURRENT_PROCESSES,
    name="Claude CLI",
    reap=ReapBounds(
        sigterm_grace_seconds=CLI_TERMINATION_GRACE_SECONDS,
        confirmation_seconds=CLI_GROUP_CONFIRMATION_SECONDS,
    ),
)

_sync_clients: dict[str, object] = {}
_async_clients: dict[str, object] = {}
_client_lock = threading.Lock()

_NO_BUILTIN_TOOLS: Final = ""
_NO_SETTING_SOURCES: Final = ""
CLAUDE_CLI_MODEL_CATALOG_ERROR: Final = "Claude CLI could not list models."
CLAUDE_CLI_BINARY_NOT_FOUND_DETAIL: Final = "Claude CLI binary not found"
CLAUDE_CLI_UNAVAILABLE_DETAIL: Final = "Claude CLI is unavailable. Check server logs for details."
CLAUDE_CLI_LOGIN_REFUSED_DETAIL: Final = "Claude CLI login was refused."
CLAUDE_CLI_LOGIN_NOT_CONFIGURED_DETAIL: Final = "Claude CLI is not signed in."

# The CLI is a bind-mounted, self-updating binary reading a prompt that carries
# untrusted content (host data, @-mentions, tool results). These flags make its
# reachable tool surface a property of this command line alone: `--tools ""`
# removes the whole built-in set, so a tool shipped by a future version cannot
# be called even though nobody here has heard of it; `--setting-sources ""`
# drops the mounted settings file, whose `permissions.allow` and `defaultMode`
# would otherwise decide what a turn may do; `--strict-mcp-config`
# ignores MCP servers configured anywhere but in our own `--mcp-config`;
# `--disable-slash-commands` closes the one channel `--tools ""` does not
# touch — the CLI still resolves its own slash commands and skills from a
# prompt that begins with `/`, so this flag removes that surface rather than
# relying on our own prompt always starting with trusted system text.
_TOOL_ISOLATION_FLAGS: Final = (
    "--tools",
    _NO_BUILTIN_TOOLS,
    "--setting-sources",
    _NO_SETTING_SOURCES,
    "--strict-mcp-config",
    "--disable-slash-commands",
)

RELAY_SERVER_NAME: Final = "relay"
_OAUTH_TOKEN_VARIABLE: Final = "CLAUDE_CODE_OAUTH_TOKEN"
_RUN_SWITCHES: Final = {
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
}
# The CLI counts one turn per tool-using response and checks its limit after
# that response's calls ran, so one turn beyond the service's call limit stays
# behind it (ADR 0001, A1b-0b2 and A1b-4a).
TURNS_BEYOND_TOOL_CALLS: Final = 1

_NO_TOOLS_EXPECTED: Final[frozenset[str]] = frozenset()

# Never a real user: the probe only lists the MCP server's advertised tools,
# it never invokes one, so no request in its name ever touches a user's data.
_TOOL_SURFACE_PROBE_USER_ID: Final = "tool-surface-probe"

_CLI_AUTHENTICATION_FAILED: Final = "authentication_failed"
_CLI_INIT_EVENT_TYPE: Final = "system"
_CLI_INIT_EVENT_SUBTYPE: Final = "init"
_TOOL_SURFACE_PROBE_PROMPT: Final = "."

_STREAM_BUFFER_LIMIT = 4 * 1024 * 1024
_CLAUDE_TURN_HOME_PREFIX: Final = "agent-cli-claude-turn-"
_CLAUDE_CREDENTIAL_PATH: Final = Path(".claude/.credentials.json")


def clear_client_cache() -> None:
    with _client_lock:
        _sync_clients.clear()
        _async_clients.clear()


def clear_cli_login_status_cache() -> None:
    clear_claude_cli_login_cache()


class UnavailableError(Exception):
    """Raised when no Claude backend is available."""


class CliUnavailableError(UnavailableError):
    """Raised when the Claude CLI cannot run here: its binary is missing, or
    its private home or credential copy cannot be made."""


class CliLoginNotConfiguredError(UnavailableError):
    """Raised when the configured Claude CLI credential is missing or unusable."""


class CliLoginRefusedError(UnavailableError):
    """Raised when the Claude CLI reports that its login was refused.

    The message is fixed: the CLI's own words never reach a caller or a log.
    """


class CliToolSurfaceError(UnavailableError):
    """Raised when the mounted CLI's announced tool surface does not match
    what a given call line expects — extra tools, missing ones, or a
    still-reachable slash command (see ``verify_cli_tool_surface()`` and
    ``verify_no_builtin_cli_tools()``).

    Deliberately an ``UnavailableError``: a CLI whose tool surface we cannot
    vouch for is not a CLI we run untrusted content through, so every
    caller that already handles "no backend" refuses the turn.
    """


class _ZombieProbeError(UnavailableError):
    """Raised when a tool-surface probe's own CLI process outlived
    SIGKILL. Not a plain ``UnavailableError``: it tells
    ``_record_tool_surface_failure`` to use the much longer
    ``CLAUDE_CLI_ZOMBIE_FAILURE_CACHE_SECONDS`` TTL instead of the ordinary
    one — a process stuck past SIGKILL will not become healthy in ten
    seconds, and probing again on that schedule only spawns another zombie.
    """


class _DeadlineExhausted(UnavailableError):
    """The caller-owned deadline elapsed before the one-shot completion started."""


class _ClaudeCliProcessPoolSaturated(UnavailableError):
    """A CLI process was refused before it could create another child process."""


@dataclass
class ClaudeResponse:
    text: str


# ── Public interface ───────────────────────────────────────────────


def _refuse_unusable_model(model: str | None, *, route: Literal["api", "cli"]) -> None:
    """An empty name is never a default; the API has no default to fall back to."""
    refuse_empty_model("claude", route, model)
    if model is None and route == "api":
        raise ProviderUnavailableError(
            "claude",
            route,
            normalize_route_failure(SafeRouteReasonCode.ROUTE_FAILED),
        )


def call_claude(
    prompt: str,
    api_key: str | None = None,
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 1024,
    messages: list[dict[str, str]] | None = None,
    timeout_seconds: float | None = None,
) -> ClaudeResponse:
    _refuse_unusable_model(model, route="api" if api_key else "cli")
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    if api_key:
        log.info("Claude: using API backend (model=%s)", model)
        return _call_api(prompt, api_key, system, model, max_tokens, messages, deadline=deadline)
    log.info("Claude: using CLI backend (model=%s)", model)
    return _call_cli(prompt, system, model, messages, deadline=deadline)


async def acall_claude(
    prompt: str,
    api_key: str | None = None,
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 1024,
    messages: list[dict[str, str]] | None = None,
) -> ClaudeResponse:
    _refuse_unusable_model(model, route="api" if api_key else "cli")
    if api_key:
        log.info("Claude: using async API backend (model=%s)", model)
        return await _acall_api(prompt, api_key, system, model, max_tokens, messages)
    log.info("Claude: using async CLI backend (model=%s)", model)
    return await _acall_cli(prompt, system, model, messages)


async def acall_claude_with_mcp(
    prompt: str,
    *,
    user_id: str,
    system: str | None = None,
    model: str | None = None,
    messages: list[dict[str, str]] | None = None,
    timeout_seconds: int = 600,
) -> ClaudeResponse:
    """Call the Claude CLI with the host's MCP server attached.

    Spawns the CLI which in turn spawns the MCP server subprocess with the
    turn's user in its environment. Claude's built-in tools are removed from
    the session; only that server's tools are reachable — or none at all,
    where the host configured no MCP server. This path exists exclusively for
    MCP-attached turns and requires the CLI backend (the Anthropic SDK
    does not expose MCP servers).
    """
    _refuse_unusable_model(model, route="cli")
    binary = await verify_cli_tool_surface()
    flat_prompt = flatten_messages(prompt, messages)
    stdin_body = stdin_prompt(system, flat_prompt)
    log.info("Claude: MCP+CLI backend (model=%s, user=%s)", model, user_id)

    home = _claude_private_home()
    try:
        with _mcp_turn_cli_arguments(model, user_id, stream=False) as arguments:
            child = _claude_child_process(binary, arguments, home.path)
            async with _anchored_turn_child(child, home_reservation=home.reserve()) as proc:
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(stdin_body.encode()),
                        timeout=timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    raise UnavailableError(
                        f"Claude CLI timed out after {timeout_seconds}s",
                    )
    finally:
        home.close()

    stdout = stdout_bytes.decode()
    if proc.returncode != 0:
        log.warning(
            "Claude MCP CLI failed (rc=%d, stderr_bytes=%d)",
            proc.returncode,
            len(stderr_bytes),
        )
        raise UnavailableError(
            CLAUDE_CLI_UNAVAILABLE_DETAIL,
        )

    text = _parse_cli_output(stdout)
    log.debug("Claude MCP CLI response: %d chars", len(text))
    return ClaudeResponse(text=text.strip())


async def acall_claude_with_mcp_stream(
    prompt: str,
    *,
    user_id: str,
    system: str | None = None,
    model: str | None = None,
    messages: list[dict[str, str]] | None = None,
    timeout_seconds: int = 600,
    correlation_id: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream Claude CLI output as parsed events.

    Spawns the Claude CLI with ``--output-format stream-json`` and yields
    typed ``StreamEvent`` instances as the subprocess emits newline-delimited
    JSON lines. A turn that reaches its end yields a ``FinalEvent`` last; a
    failing CLI raises ``UnavailableError`` instead. Malformed JSON lines are
    logged and skipped rather than raising.

    ``correlation_id`` stamps every yielded event with the caller's own handle
    for this turn; it is excluded from serialization and never reaches a wire
    payload.
    """
    _refuse_unusable_model(model, route="cli")
    binary = await verify_cli_tool_surface()
    flat_prompt = flatten_messages(prompt, messages)
    stdin_body = stdin_prompt(system, flat_prompt)
    log.info("Claude: streaming MCP+CLI (model=%s, user=%s)", model, user_id)

    home = _claude_private_home()
    try:
        with _mcp_turn_cli_arguments(model, user_id, stream=True) as arguments:
            child = _claude_child_process(binary, arguments, home.path)
            async with _anchored_turn_child(
                child,
                home_reservation=home.reserve(),
                stream_buffer_limit=_STREAM_BUFFER_LIMIT,
            ) as proc:
                proc.stdin.write(stdin_body.encode())
                await proc.stdin.drain()
                proc.stdin.close()
                async for event in _consume_stream(proc, timeout_seconds, correlation_id):
                    yield event
    finally:
        home.close()


async def _consume_stream(
    proc: AnchoredChild,
    timeout_seconds: int,
    correlation_id: str | None = None,
) -> AsyncIterator[StreamEvent]:
    text_chunks: list[str] = []
    final_text: str | None = None
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr))
    try:
        try:
            async for raw_line in _iter_lines(proc.stdout, timeout_seconds):
                parsed = _safe_json_loads(raw_line)
                if parsed is None:
                    continue
                for event in _parse_stream_event(parsed, text_chunks, correlation_id):
                    if isinstance(event, FinalEvent):
                        final_text = event.text
                        continue
                    yield event
        except asyncio.TimeoutError:
            raise UnavailableError(
                f"Claude CLI timed out after {timeout_seconds}s",
            )
        await proc.exited()
        stderr_size = await stderr_task
        if proc.returncode != 0:
            log.warning(
                "Claude MCP stream failed (rc=%d, stderr_bytes=%d)",
                proc.returncode,
                stderr_size,
            )
            raise UnavailableError(
                CLAUDE_CLI_UNAVAILABLE_DETAIL,
            )

        assembled = final_text if final_text is not None else "".join(text_chunks)
        yield FinalEvent(text=assembled.strip(), correlation_id=correlation_id)
    finally:
        if not stderr_task.done():
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)


async def _iter_lines(
    stdout: asyncio.StreamReader | None,
    timeout_seconds: int,
) -> AsyncIterator[bytes]:
    if stdout is None:
        return
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        line = await asyncio.wait_for(stdout.readline(), timeout=remaining)
        if not line:
            return
        yield line


async def _drain_stream(stream: asyncio.StreamReader | None) -> int:
    """Drain a subprocess pipe without retaining its potentially sensitive body."""
    if stream is None:
        return 0
    total = 0
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return total
        total += len(chunk)


def _safe_json_loads(raw_line: bytes) -> dict | None:
    line = raw_line.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        log.warning("Claude stream: skipping malformed JSON line (%d bytes)", len(line))
        return None
    if not isinstance(parsed, dict):
        log.warning("Claude stream: skipping non-object event")
        return None
    return parsed


def _parse_stream_event(
    payload: dict,
    text_chunks: list[str],
    correlation_id: str | None,
) -> list[StreamEvent]:
    kind = payload.get("type")
    if kind == "assistant":
        return _parse_assistant_events(payload, text_chunks, correlation_id)
    if kind == "user":
        return _parse_user_events(payload, correlation_id)
    if kind == "result":
        return _parse_result_events(payload, correlation_id)
    return []


def _raise_for_error_result(payload: dict) -> None:
    if payload.get("is_error"):
        raise UnavailableError(CLAUDE_CLI_UNAVAILABLE_DETAIL)


def _parse_result_events(payload: dict, correlation_id: str | None) -> list[StreamEvent]:
    _raise_for_error_result(payload)
    text = payload.get("result")
    if isinstance(text, str):
        return [FinalEvent(text=text, correlation_id=correlation_id)]
    return []


def _raise_for_api_error_message(payload: dict) -> None:
    if not payload.get("is_api_error_message"):
        return
    if payload.get("error") == _CLI_AUTHENTICATION_FAILED:
        raise CliLoginRefusedError(CLAUDE_CLI_LOGIN_REFUSED_DETAIL)
    raise UnavailableError(CLAUDE_CLI_UNAVAILABLE_DETAIL)


def _parse_assistant_events(
    payload: dict,
    text_chunks: list[str],
    correlation_id: str | None,
) -> list[StreamEvent]:
    message = payload.get("message") or {}
    _raise_for_api_error_message(payload)
    events: list[StreamEvent] = []
    for block in message.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            text = block.get("text") or ""
            text_chunks.append(text)
            events.append(AssistantTextEvent(text=text, correlation_id=correlation_id))
        elif btype == "tool_use":
            events.append(ToolCallEvent(
                tool_use_id=block.get("id") or "",
                name=block.get("name") or "",
                input=block.get("input") or {},
                correlation_id=correlation_id,
            ))
    return events


def _parse_user_events(payload: dict, correlation_id: str | None) -> list[StreamEvent]:
    message = payload.get("message") or {}
    events: list[StreamEvent] = []
    for block in message.get("content") or []:
        if block.get("type") == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                texts = [c.get("text", "") for c in content if isinstance(c, dict)]
                content_str = "".join(texts)
            elif isinstance(content, str):
                content_str = content
            else:
                content_str = ""
            events.append(ToolResultEvent(
                tool_use_id=block.get("tool_use_id") or "",
                content=content_str,
                is_error=bool(block.get("is_error", False)),
                correlation_id=correlation_id,
            ))
    return events


def is_available(api_key: str | None = None) -> bool:
    if api_key:
        return True
    return _find_claude_binary() is not None


def cli_login_status() -> CliLogin:
    """Return the mounted Claude CLI's cached login probe."""
    return claude_cli_login(_find_claude_binary())


@dataclass(frozen=True)
class ClaudeCliModelCatalog:
    """What the CLI's `/model` text states.

    ``aliases`` are the names the `--model` flag accepts. ``current_model_label``
    is the CLI's own name for the model a turn with no model runs: something to
    show, never a model to pass and never one of the aliases.
    """

    aliases: tuple[str, ...]
    current_model_label: str | None


def cli_model_catalog() -> ClaudeCliModelCatalog:
    """Read the CLI's model aliases and its stated default from one `/model` start.

    The CLI's own text is the only source for this — there is no
    machine-readable catalog endpoint for a CLI-only login. Raises
    UnavailableError, never returns an empty alias tuple, if that text doesn't
    contain the expected `Available: a, b, c.` fragment.

    The CLI's closing "or a full model ID" is not a name, and the flag also
    takes full ids that the aliases do not hold.
    """
    binary = _require_claude_binary()
    with _faults_in_the_claude_family():
        run = run_claude_catalog_cli(binary, ("-p", "/model"))
    if run is None:
        raise UnavailableError("Claude CLI /model failed to run")
    if not run.complete:
        raise UnavailableError(
            f"Claude CLI /model timed out after {PROBE_TIMEOUT_SECONDS}s",
        )
    if run.returncode != 0:
        log.warning(
            "Claude CLI model catalog failed (rc=%d, stderr_chars=%d)",
            run.returncode,
            len(run.stderr),
        )
        raise UnavailableError(
            CLAUDE_CLI_MODEL_CATALOG_ERROR,
        )
    return ClaudeCliModelCatalog(
        _model_aliases_in(run.stdout), _current_model_label_in(run.stdout),
    )


def _model_aliases_in(stdout: str) -> tuple[str, ...]:
    for line in stdout.splitlines():
        if CLAUDE_CLI_MODEL_LIST_MARKER not in line:
            continue
        after_marker = line.split(CLAUDE_CLI_MODEL_LIST_MARKER, 1)[1]
        aliases = tuple(
            token.strip()
            for token in after_marker.rstrip(".").split(", ")
            if token.strip() and " " not in token.strip()
        )
        if aliases:
            return aliases
    raise UnavailableError(
        "Claude CLI /model output did not contain a parseable model list",
    )


def _current_model_label_in(stdout: str) -> str | None:
    marked_lines = [
        line for line in stdout.splitlines()
        if line.startswith(CLAUDE_CLI_CURRENT_MODEL_MARKER)
    ]
    if len(marked_lines) != 1:
        return None
    _, opening, after_opening = marked_lines[0].partition("`")
    label, closing, _ = after_opening.partition("`")
    if not (opening and closing and label and label.isprintable()):
        return None
    return label


# ── Shared helpers ─────────────────────────────────────────────────


def _build_api_kwargs(
    prompt: str,
    system: str | None,
    model: str,
    max_tokens: int,
    messages: list[dict[str, str]] | None,
) -> dict:
    if messages is not None:
        api_messages = messages
    else:
        api_messages = [{"role": "user", "content": prompt}]

    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": api_messages,
    }
    if system:
        kwargs["system"] = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    return kwargs


@dataclass(frozen=True)
class ClaudeRun:
    """One subscription run of the pinned CLI: its child and its one stdin line.

    It describes the run and starts nothing; the spawn owner starts the child.
    """

    binary: Path
    token: SecretStr
    home: Path
    working_directory: Path
    system_prompt_file: Path
    max_tool_calls: int
    history: tuple[TurnMessage, ...] = field(repr=False)
    relay_config: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.token, SecretStr):
            raise ChildProcessSpecificationError("A Claude run needs its token as a SecretStr")
        paths = {
            "binary": self.binary,
            "home": self.home,
            "working_directory": self.working_directory,
            "system_prompt_file": self.system_prompt_file,
            "relay_config": self.relay_config,
        }
        for name, path in paths.items():
            if path is not None and not path.is_absolute():
                raise ChildProcessSpecificationError(f"A Claude run needs an absolute {name}")
        if self.working_directory == self.home:
            raise ChildProcessSpecificationError(
                "A Claude run needs a working_directory apart from its home",
            )
        if self.max_tool_calls < 1:
            raise ChildProcessSpecificationError("A Claude run needs max_tool_calls of at least 1")
        if not self.history:
            raise ChildProcessSpecificationError("A Claude run needs a non-empty history")

    def child(self) -> ChildProcess:
        return ChildProcess(
            binary=self.binary,
            arguments=self._arguments(),
            environment=closed_environment(
                self.home,
                **{_OAUTH_TOKEN_VARIABLE: self.token.get_secret_value()},
                **_RUN_SWITCHES,
            ),
            working_directory=self.working_directory,
        )

    def stdin_message(self) -> bytes:
        transcript = flatten_messages(
            "",
            [{"role": message.role.value, "content": message.text} for message in self.history],
        )
        line = {"type": "user", "message": {"role": "user", "content": transcript}}
        return (json.dumps(line) + "\n").encode()

    def _arguments(self) -> tuple[str, ...]:
        arguments = (
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            *_TOOL_ISOLATION_FLAGS,
            "--no-session-persistence",
            "--max-turns",
            str(self.max_tool_calls + TURNS_BEYOND_TOOL_CALLS),
            "--system-prompt-file",
            str(self.system_prompt_file),
        )
        if self.relay_config is None:
            return arguments
        return (
            *arguments,
            "--mcp-config",
            str(self.relay_config),
            "--allowedTools",
            f"{_mcp_tool_prefix(RELAY_SERVER_NAME)}*",
        )


@contextmanager
def _faults_in_the_claude_family() -> Iterator[None]:
    try:
        yield
    except process.CliLoginUnusableError as exc:
        raise CliLoginNotConfiguredError(CLAUDE_CLI_LOGIN_NOT_CONFIGURED_DETAIL) from exc
    except process.CliEnvironmentUnavailableError as exc:
        raise CliUnavailableError(CLAUDE_CLI_UNAVAILABLE_DETAIL) from exc


def _claude_private_home() -> process.PrivateCredentialHome:
    with _faults_in_the_claude_family():
        return process.PrivateCredentialHome(
            current_claude_runtime().token_file,
            _CLAUDE_CREDENTIAL_PATH,
            prefix=_CLAUDE_TURN_HOME_PREFIX,
            missing_credential_is_error=True,
        )


def _claude_child_process(
    binary: Path,
    arguments: list[str],
    home: Path,
) -> ChildProcess:
    """Describe one Claude child around its disposable credential home."""
    return ChildProcess(
        binary=binary,
        arguments=tuple(arguments),
        environment=closed_environment(home),
        working_directory=home,
    )


def _model_arguments(model: str | None) -> list[str]:
    return [] if model is None else ["--model", model]


def _cli_arguments(
    model: str | None,
    *,
    stream: bool = False,
) -> list[str]:
    """Arguments for a single-turn completion that needs no tools at all."""
    cmd = [
        "-p",
        *_model_arguments(model),
        "--output-format",
        "stream-json" if stream else "json",
        *_TOOL_ISOLATION_FLAGS,
    ]
    if stream:
        cmd.append("--verbose")
    return cmd


def _mcp_tool_prefix(server_name: str) -> str:
    """The CLI namespaces an MCP tool as ``mcp__<server>__<tool>``; the server
    registers the bare tool names."""
    return f"mcp__{server_name}__"


def _mcp_allowed_tools(spec: McpServerSpec) -> str:
    return f"{_mcp_tool_prefix(spec.name)}*"


def _expected_mcp_tool_names(spec: McpServerSpec) -> frozenset[str]:
    prefix = _mcp_tool_prefix(spec.name)
    return frozenset(f"{prefix}{name}" for name in spec.tool_names)


def _build_mcp_config(spec: McpServerSpec, user_id: str) -> str:
    """Serialize the temporary --mcp-config payload for the host's stdio server.

    The JSON is written to a mode-0600 file so the credentials the host put in
    ``spec.environment`` never appear in ``/proc/<pid>/cmdline`` or process
    listings. The turn's user is the one value this layer adds.
    """
    environment = {name: value.get_secret_value() for name, value in spec.environment.items()}
    environment[spec.user_id_environment_variable] = user_id
    config = {
        "mcpServers": {
            spec.name: {
                "command": spec.command,
                "args": list(spec.args),
                "env": environment,
            },
        },
    }
    return json.dumps(config)


def _write_mcp_config(spec: McpServerSpec, user_id: str) -> str:
    handle, path = tempfile.mkstemp(
        prefix=spec.config_file_prefix,
        suffix=".json",
        dir=current_config().cli_working_directory_root,
    )
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write(_build_mcp_config(spec, user_id))
    os.chmod(path, 0o600)
    return path


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return


def _mcp_cli_arguments(
    model: str | None,
    config_path: str,
    spec: McpServerSpec,
    *,
    stream: bool = False,
) -> list[str]:
    """Command for an MCP-attached turn: the host's MCP tools and nothing else.

    ``--allowedTools`` pre-approves that server's tools so the session never
    needs a permission answer nobody is there to give. Everything else is
    either absent (``--tools ""``) or falls through to the CLI's default
    permission mode, which in ``--print`` mode can only refuse.
    """
    output_format = "stream-json" if stream else "json"
    cmd = [
        "-p",
        *_model_arguments(model),
        "--output-format",
        output_format,
        *_TOOL_ISOLATION_FLAGS,
        "--allowedTools",
        _mcp_allowed_tools(spec),
        "--mcp-config",
        config_path,
    ]
    if stream:
        cmd.append("--verbose")
    return cmd


@contextmanager
def _mcp_turn_cli_arguments(
    model: str | None,
    user_id: str,
    *,
    stream: bool,
) -> Iterator[list[str]]:
    """The MCP-attached turn's command line, for as long as its MCP config file lives.

    A deployment that configures no MCP server (``mcp_server=None``) runs the
    tool-free command line instead — no ``--mcp-config``, no ``--allowedTools``,
    and ``verify_cli_tool_surface()`` verified it against the tool-free
    expectation. Nothing is written to disk on that route.
    """
    spec = current_turn_config().mcp_server
    if spec is None:
        yield _cli_arguments(model, stream=stream)
        return
    config_path = _write_mcp_config(spec, user_id)
    try:
        yield _mcp_cli_arguments(model, config_path, spec, stream=stream)
    finally:
        _unlink_quiet(config_path)


# ── Tool-surface verification ──────────────────────────────────────
#
# Two gates share the machinery below, one per invocation shape:
#
# - ``verify_cli_tool_surface()`` guards the MCP-attached turn
#   (``acall_claude_with_mcp*``): the CLI must announce exactly the tools of
#   the host's ``McpServerSpec``, nothing more and nothing less. A host that
#   configured no MCP server routes that turn — and this gate — to the
#   tool-free shape below instead.
# - ``verify_no_builtin_cli_tools()`` / ``averify_no_builtin_cli_tools()``
#   guard every tool-free turn (``_call_cli`` / ``_acall_cli`` — every
#   tool-free caller funnels through these):
#   the CLI must announce no tools at all.
#
# Single-flight is shaped so that no lock is held across the probe: a lock
# held across external I/O can always block unboundedly inside the held
# section, so the probe runs with no lock at all:
#
# - The dict lock (``_tool_surface_lock``) only ever guards a dict
#   lookup/insert — never a probe, never an await. The first cold caller
#   for a key publishes a future/event under that lock and releases it
#   immediately, then probes with *no lock held at all*. Every other
#   caller finds the published future and awaits it, each with its own
#   timeout — see ``_verify_tool_surface_async``/``_sync``.
# - The probe itself runs under one overall deadline covering every step
#   (process start, stdin write, stdout read) — not a separate timeout per
#   step that can add up to an unknown total. Only the final cleanup (the
#   bounded runner's reap: SIGTERM grace, then the group's count and exit
#   wait after SIGKILL) has its own, separate, small bound —
#   cleanup must always be attempted regardless of how much of the answer
#   budget is left. See ``_probe_cli_surface_async``/``_sync``.
# - A group the bounded runner's reap did not confirm gone after SIGKILL is
#   kept by ``process.run_cli_bounded``'s background reap. One reservation per
#   process caps that work so a run of bad
#   probes cannot grow an unbounded pool of waiting
#   tasks/threads — and its failure is cached for
#   ``CLAUDE_CLI_ZOMBIE_FAILURE_CACHE_SECONDS`` (much longer than an
#   ordinary probe failure): ten more seconds will not make a process that
#   ignored SIGKILL healthy, and probing again on that schedule only
#   spawns another zombie.
#
# Deliberately two probe kinds, not one reused kind: the MCP-attached probe
# spawns the host's MCP server subprocess, which may need dependencies a
# tool-free container lacks — registering and listing its tools touches no
# database, only a tool *call* does, so that is not the reason for the
# split. Where those dependencies are absent this probe would always fail —
# verified live against the real CLI that a missing MCP connection reports
# zero tools, not the expected set. The no-MCP probe never attaches
# ``--mcp-config`` at all, so it needs none of them and is the one that
# runs anywhere.
#
# The no-MCP check does not need the MCP-attached one's stronger guarantee:
# the command line ``_build_cli_cmd`` actually runs has no ``--mcp-config``
# and no ``--allowedTools`` at all, a strict subset of what the MCP-attached
# probe puts on its own command line. So "no tool announced" is the correct,
# and cheapest, thing to verify for that shape — there is nothing beyond the
# built-ins for a wider check to find.


@dataclass(frozen=True)
class BinaryBuild:
    """Identity of the CLI build behind the ``claude`` path, mount and all."""

    path: str
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class _AttachedMcpServer:
    """The MCP server a probe attaches, and the temporary config file that
    tells the CLI how to reach it. ``None`` in its place means the probe
    attaches nothing — the tool-free shape."""

    spec: McpServerSpec
    config_path: str


@dataclass(frozen=True)
class _AnnouncedSurface:
    """What the CLI's own ``system`` init event says it can reach.

    ``mcp_connected`` is ``None`` when no ``--mcp-config`` was attached (the
    no-builtin-tools probe never attaches one, so the question does not
    apply); ``True``/``False`` otherwise, read from the init event's own
    ``mcp_servers`` status for our server rather than assumed from the tool
    list — a failed MCP connection reports the same empty ``tools`` a clean,
    intentionally tool-free CLI would, and the two must not be confused.
    """

    tools: tuple[str, ...]
    slash_commands: tuple[str, ...]
    mcp_connected: bool | None


@dataclass(frozen=True)
class _ToolSurfaceMismatch:
    """Everything the announced surface offers beyond, or fails to offer
    from, what was expected."""

    unexpected_tools: tuple[str, ...]
    missing_tools: tuple[str, ...]
    slash_commands: tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.unexpected_tools or self.missing_tools or self.slash_commands)

    def describe(self) -> str:
        parts = []
        if self.unexpected_tools:
            parts.append(f"unexpected tools: {', '.join(self.unexpected_tools)}")
        if self.missing_tools:
            parts.append(f"missing tools: {', '.join(self.missing_tools)}")
        if self.slash_commands:
            parts.append(f"slash commands: {', '.join(self.slash_commands)}")
        return "; ".join(parts)


@dataclass(frozen=True)
class _ToolSurfaceFailure:
    """A probe failure, remembered separately from a genuine verdict — see
    the module-level comment above for why the two must never share a
    cache. It keeps the failure's class and safe message, never the exception
    or its traceback. A zombie class gets the much longer
    ``CLAUDE_CLI_ZOMBIE_FAILURE_CACHE_SECONDS``."""

    recorded_at: float
    error_class: type[UnavailableError]
    message: str

    def ttl_seconds(self) -> float:
        return (
            CLAUDE_CLI_ZOMBIE_FAILURE_CACHE_SECONDS
            if issubclass(self.error_class, _ZombieProbeError)
            else CLAUDE_CLI_TOOL_SURFACE_FAILURE_CACHE_SECONDS
        )


_ToolSurfaceKey = tuple[BinaryBuild, frozenset[str]]

# Extra margin a follower waits beyond the leader's own worst case before
# giving up on its own — generous, since this only ever fires if the
# leader's own bound (probe deadline + the bounded runner's settle margin,
# ``process.cli_settle_margin_seconds()``) somehow failed to hold; it is a
# defense-in-depth backstop, not the expected path.
_FOLLOWER_WAIT_MARGIN_SECONDS: Final = 2

# _tool_surface_lock guards only the dicts below — a plain lookup/insert,
# never held across a probe or an await. Single-flight is a *published
# future*, not a held mutex: the leader claims the in-flight slot under
# this lock, releases it immediately, and probes with nothing held at all.
_tool_surface_lock = threading.Lock()
_tool_surface_verdicts: dict[_ToolSurfaceKey, _ToolSurfaceMismatch] = {}
_tool_surface_failures: dict[_ToolSurfaceKey, _ToolSurfaceFailure] = {}
_tool_surface_inflight_async: dict[_ToolSurfaceKey, asyncio.Future[_ToolSurfaceMismatch]] = {}
_tool_surface_inflight_sync: dict[
    _ToolSurfaceKey, concurrent.futures.Future[_ToolSurfaceMismatch]
] = {}


# The independent async tasks that actually run a probe — tracked the same
# way, for the same two reasons: keep a reference so they cannot be
# garbage-collected while still running, and give shutdown something to
# cancel.
_tool_surface_probe_tasks: set[asyncio.Task] = set()
# The tool-surface gate's most recent verdict, for a host's health check — not
# a boot-time snapshot: every call to
# ``verify_cli_tool_surface()`` updates this, cache hit or fresh probe
# alike, so a later successful MCP-attached turn clears an earlier boot-time
# "unverified", and a later drifted build (after a self-update) replaces
# an earlier "ok" — a health check always reads the gate's own current answer,
# never a value frozen at startup.
_tool_surface_health_lock = threading.Lock()
_tool_surface_health_state: Literal["ok", "drift", "unverified"] = "unverified"


def clear_cli_tool_surface_cache() -> None:
    global _tool_surface_health_state
    with _tool_surface_lock:
        _tool_surface_verdicts.clear()
        _tool_surface_failures.clear()
        _tool_surface_inflight_async.clear()
        _tool_surface_inflight_sync.clear()
    with _tool_surface_health_lock:
        _tool_surface_health_state = "unverified"


def claude_cli_tool_surface_health() -> Literal["ok", "drift", "unverified"]:
    """The tool-surface gate's most recent verdict — what a host's health
    check reports. See the module-level
    comment above ``_tool_surface_health_state`` for why this is a live
    value, not a boot-time snapshot."""
    with _tool_surface_health_lock:
        return _tool_surface_health_state


def _record_tool_surface_health(state: Literal["ok", "drift"]) -> None:
    global _tool_surface_health_state
    with _tool_surface_health_lock:
        _tool_surface_health_state = state


def _discard_tool_surface_probe_task(task: asyncio.Task) -> None:
    with _tool_surface_lock:
        _tool_surface_probe_tasks.discard(task)


async def shutdown_tool_surface_background_tasks() -> None:
    """Cancel every outstanding probe-runner task — call from the app's own
    shutdown (``server.py``'s lifespan), the same way it already cancels its
    other background loops.

    A retained group stays as it is: it receives no signal and keeps its pool
    slot and home until ``confirm_group_gone`` frees them. A leader still
    unreaped when the process exits is re-parented to and reaped by init (under
    a container, its init), while the exit sweep removes the home.
    """
    with _tool_surface_lock:
        tasks = list(_tool_surface_probe_tasks)
        _tool_surface_probe_tasks.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def verify_cli_tool_surface() -> Path:
    """Raise unless the mounted CLI reaches nothing but the host's MCP
    tools; return the resolved binary path to run the real turn with.

    Probes with the same ``--mcp-config`` a real MCP-attached turn attaches, so
    "clean" means the CLI is actually still connecting that MCP server and
    reporting exactly its tools — not merely reporting no built-ins with
    nothing attached to compare against. A connection that fails to
    establish, with nothing *else* wrong, is a probe *failure*
    (short-lived, retried on the next call), never a "the CLI offers zero
    of the expected tools" verdict (permanent, per build) — the two look
    identical in the raw ``tools`` list alone, so ``mcp_connected`` is what
    tells them apart. But an unexpected tool or a slash command is a
    permanent mismatch regardless of ``mcp_connected`` — a CLI reporting
    ``tools=["Bash"]`` while its MCP connection also happens to be down is
    not "unverifiable", it is dangerous, and caching that for only ten
    seconds would let it look clean again far too soon. See
    ``_evaluate_tool_surface`` for where that split actually happens.

    A host that configures no MCP server (``mcp_server=None``) runs its
    turns on the tool-free command line, so this gate verifies that shape
    instead: no tool at all, the same expectation every tool-free turn is
    held to.

    Also records the outcome as the live health state — on every call, cache
    hit or fresh probe alike, so that state always reflects this gate's most
    recent answer rather than a value frozen at boot.
    """
    spec = current_turn_config().mcp_server
    try:
        if spec is None:
            result = await averify_no_builtin_cli_tools()
        else:
            result = await _verify_mcp_tool_surface(spec)
    except CliToolSurfaceError:
        _record_tool_surface_health("drift")
        raise
    except UnavailableError:
        _record_tool_surface_health("unverified")
        raise
    _record_tool_surface_health("ok")
    return result


async def _verify_mcp_tool_surface(spec: McpServerSpec) -> Path:
    async def probe(deadline: float) -> _AnnouncedSurface:
        config_path = _write_mcp_config(spec, _TOOL_SURFACE_PROBE_USER_ID)
        try:
            return await _probe_cli_surface_async(
                Path(build.path),
                mcp=_AttachedMcpServer(spec, config_path),
                deadline=deadline,
            )
        finally:
            _unlink_quiet(config_path)

    build, key = _tool_surface_key(_expected_mcp_tool_names(spec))
    return await _verify_tool_surface_async(
        build,
        key,
        probe,
        timeout_seconds=CLAUDE_CLI_TOOL_SURFACE_TIMEOUT_SECONDS,
    )


async def averify_no_builtin_cli_tools() -> Path:
    """Async twin of ``verify_no_builtin_cli_tools`` for ``_acall_cli``."""
    build, key = _tool_surface_key(_NO_TOOLS_EXPECTED)

    async def probe(deadline: float) -> _AnnouncedSurface:
        return await _probe_cli_surface_async(
            Path(build.path),
            mcp=None,
            deadline=deadline,
        )

    return await _verify_tool_surface_async(
        build,
        key,
        probe,
        timeout_seconds=CLAUDE_CLI_NO_TOOL_SURFACE_TIMEOUT_SECONDS,
    )


def verify_no_builtin_cli_tools() -> Path:
    """Raise unless the mounted CLI reaches no tool at all under
    ``_TOOL_ISOLATION_FLAGS``; return the resolved binary path to run the
    real turn with. Sync twin for ``_call_cli``, which has no event loop to
    await one in — a host calls it from a plain synchronous child,
    not an async context.
    """
    build, key = _tool_surface_key(_NO_TOOLS_EXPECTED)

    def probe(probe_deadline: float) -> _AnnouncedSurface:
        return _probe_cli_surface_sync(
            Path(build.path),
            mcp=None,
            deadline=probe_deadline,
        )

    return _verify_tool_surface_sync(
        build,
        key,
        probe,
    )


# ── single-flight: async ────────────────────────────────────────────


async def _verify_tool_surface_async(
    build: BinaryBuild,
    key: _ToolSurfaceKey,
    probe: Callable[[float], Awaitable[_AnnouncedSurface]],
    *,
    timeout_seconds: float,
) -> Path:
    """Single-flight through a published future, never a held lock.

    The dict lock is taken twice, briefly: once to check the cache, once
    to claim the in-flight slot (or find someone else already holds it).
    It is never held while probing.

    The caller that claims the slot does not probe inline in its own
    coroutine: an inline leader whose own task later
    gets cancelled (an aborted request, say) used to remove the in-flight
    entry and resolve the future right there, even though the real probe
    (running in a worker thread) kept going in the
    background. A third caller landing in that window found no cached
    verdict and no in-flight entry, and started a *second* probe while
    the first was still running. So the probe now runs as an independent
    task (``_run_probe_and_resolve_async``) that nothing directly awaits;
    every caller, including the one that triggered it, just waits on the
    shared future like everyone else. Cancelling any one caller's own
    wait cannot touch that task or the entry it will eventually resolve.
    """
    cached = _cached_tool_surface_verdict(key)
    if cached is not None:
        return _finish_tool_surface_check(build, cached)

    future, is_leader = _claim_or_join_inflight_async(key)
    if is_leader:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        task = asyncio.create_task(_run_probe_and_resolve_async(key, probe, deadline, future))
        with _tool_surface_lock:
            _tool_surface_probe_tasks.add(task)
        task.add_done_callback(_discard_tool_surface_probe_task)

    mismatch = await _await_follower_result_async(build, future, timeout_seconds)
    return _finish_tool_surface_check(build, mismatch)


async def _run_probe_and_resolve_async(
    key: _ToolSurfaceKey,
    probe: Callable[[float], Awaitable[_AnnouncedSurface]],
    deadline: float,
    future: asyncio.Future[_ToolSurfaceMismatch],
) -> None:
    """The actual probe, run as work independent of whoever triggered it.

    Nothing directly awaits this task, so a caller's own cancellation
    never reaches it — it always runs to completion (or its own
    deadline/reap-bounded failure) and is what resolves ``future``,
    regardless of whether the original triggering caller is still around
    to see the answer.
    """
    build = key[0]
    try:
        surface = await probe(deadline)
        mismatch = _evaluate_tool_surface(key, surface)
    except UnavailableError as exc:
        if not isinstance(exc, _ClaudeCliProcessPoolSaturated):
            _record_tool_surface_failure(key, exc)
        _resolve_inflight_async(key, future, exception=exc)
        return
    except asyncio.CancelledError as exc:
        _resolve_inflight_async(key, future, exception=_follower_safe_exception(build, exc))
        raise
    except Exception as exc:
        _resolve_inflight_async(key, future, exception=_follower_safe_exception(build, exc))
        return
    _resolve_inflight_async(key, future, result=mismatch)


async def _await_follower_result_async(
    build: BinaryBuild,
    future: asyncio.Future[_ToolSurfaceMismatch],
    probe_timeout_seconds: float,
) -> _ToolSurfaceMismatch:
    follower_budget = _follower_wait_budget_seconds(probe_timeout_seconds)
    try:
        # shield: this follower's own wait_for must not cancel the shared
        # future out from under the leader (or every other follower) just
        # because *this* caller gave up waiting on it.
        return await asyncio.wait_for(asyncio.shield(future), timeout=follower_budget)
    except asyncio.TimeoutError:
        raise UnavailableError(
            f"Claude CLI at {build.path} did not answer within {follower_budget}s "
            "waiting on another caller's in-flight probe",
        )


def _claim_or_join_inflight_async(
    key: _ToolSurfaceKey,
) -> tuple[asyncio.Future[_ToolSurfaceMismatch], bool]:
    with _tool_surface_lock:
        future = _tool_surface_inflight_async.get(key)
        if future is not None:
            return future, False
        future = asyncio.get_running_loop().create_future()
        _tool_surface_inflight_async[key] = future
        return future, True


def _resolve_inflight_async(
    key: _ToolSurfaceKey,
    future: asyncio.Future[_ToolSurfaceMismatch],
    *,
    result: _ToolSurfaceMismatch | None = None,
    exception: BaseException | None = None,
) -> None:
    with _tool_surface_lock:
        if _tool_surface_inflight_async.get(key) is future:
            del _tool_surface_inflight_async[key]
    if future.done():
        return
    if exception is not None:
        future.set_exception(exception)
        # This runs inside the independent probe task, which
        # never reads the future back itself — every caller, the one that
        # triggered the probe included, awaits it the same way through
        # _await_follower_result_async. But any of them may already have
        # given up on their own wait_for by the time we get here, leaving
        # nobody left to retrieve it the normal way; without this call,
        # asyncio logs "exception was never retrieved" once the future is
        # garbage-collected.
        future.exception()
    else:
        future.set_result(result)


# ── single-flight: sync ─────────────────────────────────────────────


def _verify_tool_surface_sync(
    build: BinaryBuild,
    key: _ToolSurfaceKey,
    probe: Callable[[float], _AnnouncedSurface],
) -> Path:
    """Sync twin of ``_verify_tool_surface_async`` — a
    ``concurrent.futures.Future`` instead of an ``asyncio.Future``, since
    this runs where ``_call_cli`` has no event loop to publish one on."""
    cached = _cached_tool_surface_verdict(key)
    if cached is not None:
        return _finish_tool_surface_check(build, cached)

    future, is_leader = _claim_or_join_inflight_sync(key)
    if not is_leader:
        follower_budget = _follower_wait_budget_seconds(
            CLAUDE_CLI_NO_TOOL_SURFACE_TIMEOUT_SECONDS,
        )
        try:
            mismatch = future.result(timeout=follower_budget)
        except concurrent.futures.TimeoutError:
            raise UnavailableError(
                f"Claude CLI at {build.path} did not answer within {follower_budget}s "
                "waiting on another caller's in-flight probe",
            )
        return _finish_tool_surface_check(build, mismatch)

    probe_deadline = time.monotonic() + CLAUDE_CLI_NO_TOOL_SURFACE_TIMEOUT_SECONDS
    try:
        surface = probe(probe_deadline)
        mismatch = _evaluate_tool_surface(key, surface)
    except UnavailableError as exc:
        if not isinstance(exc, _ClaudeCliProcessPoolSaturated):
            _record_tool_surface_failure(key, exc)
        _resolve_inflight_sync(key, future, exception=exc)
        raise exc
    except BaseException as exc:
        _resolve_inflight_sync(key, future, exception=_follower_safe_exception(build, exc))
        raise
    _resolve_inflight_sync(key, future, result=mismatch)
    return _finish_tool_surface_check(build, mismatch)


def _claim_or_join_inflight_sync(
    key: _ToolSurfaceKey,
) -> tuple[concurrent.futures.Future[_ToolSurfaceMismatch], bool]:
    with _tool_surface_lock:
        future = _tool_surface_inflight_sync.get(key)
        if future is not None:
            return future, False
        future = concurrent.futures.Future()
        _tool_surface_inflight_sync[key] = future
        return future, True


def _resolve_inflight_sync(
    key: _ToolSurfaceKey,
    future: concurrent.futures.Future[_ToolSurfaceMismatch],
    *,
    result: _ToolSurfaceMismatch | None = None,
    exception: BaseException | None = None,
) -> None:
    with _tool_surface_lock:
        if _tool_surface_inflight_sync.get(key) is future:
            del _tool_surface_inflight_sync[key]
    if future.done():
        return
    if exception is not None:
        future.set_exception(exception)
    else:
        future.set_result(result)


def _follower_safe_exception(build: BinaryBuild, exc: BaseException) -> BaseException:
    """What a *follower* sees when the leader's own probe ends in
    something other than ``UnavailableError`` — a cancelled leader task,
    an unexpected bug, anything.

    The leader's own ``raise`` still carries the real exception, unfiltered
    — that is what shows up in its own logs/stack trace. But a follower is
    an unrelated caller that never asked to be cancelled or to inherit a
    stranger's bug: handing it the leader's literal ``CancelledError``
    would make its own task look cancelled too, confusing whatever is
    awaiting *it* (a request handler, a test). A follower always gets a
    normal, catchable ``UnavailableError`` instead.
    """
    if isinstance(exc, UnavailableError):
        return exc
    return UnavailableError(
        f"Claude CLI probe for {build.path} was aborted before it could answer: {exc}",
    )


def _follower_wait_budget_seconds(probe_timeout_seconds: float) -> float:
    return probe_timeout_seconds + _cleanup_margin_seconds()


def _cleanup_margin_seconds() -> float:
    return process.cli_settle_margin_seconds() + _FOLLOWER_WAIT_MARGIN_SECONDS


# ── cache lookups shared by both domains ────────────────────────────


def _tool_surface_key(expected_tools: frozenset[str]) -> tuple[BinaryBuild, _ToolSurfaceKey]:
    binary = _require_claude_binary()
    build = _binary_build(binary)
    return build, (build, expected_tools)


def _cached_tool_surface_verdict(key: _ToolSurfaceKey) -> _ToolSurfaceMismatch | None:
    """The remembered verdict for this exact (binary build, expectation)
    pair, or ``None`` when it still needs a fresh probe.

    Raises the cached message directly when a probe already failed for this
    pair within its failure's own TTL (ordinary or, for a zombie, the much
    longer one) — short on purpose for the ordinary case, so a struggling
    CLI or MCP connection does not stay refused for as long as a genuine
    verdict would (the unbounded cache below): once that window passes, the
    next call re-probes rather than trusting a stale failure forever.
    """
    with _tool_surface_lock:
        verdict = _tool_surface_verdicts.get(key)
        if verdict is not None:
            return verdict
        failure = _tool_surface_failures.get(key)
    if failure is None:
        return None
    if time.monotonic() - failure.recorded_at >= failure.ttl_seconds():
        return None
    raise failure.error_class(failure.message)


def _record_tool_surface_failure(key: _ToolSurfaceKey, error: UnavailableError) -> None:
    with _tool_surface_lock:
        _tool_surface_failures[key] = _ToolSurfaceFailure(
            time.monotonic(), type(error), str(error),
        )


def _evaluate_tool_surface(
    key: _ToolSurfaceKey,
    surface: _AnnouncedSurface,
) -> _ToolSurfaceMismatch:
    """Remembered per (binary build, expectation), not per process: the CLI
    is a bind-mounted install that updates itself under a running container,
    and an update is exactly the event this check exists for.

    Raises ``UnavailableError`` instead of returning, for a probe *failure*
    the caller must cache only briefly (see ``_verify_tool_surface_async``/
    ``_sync``), in exactly one case: the MCP connection never established
    and that is the *only* thing wrong — nothing unexpected was announced,
    no missing tool can be blamed on anything but the connection itself.
    An unexpected tool or a still-reachable slash command is always a
    permanent mismatch, MCP connected or not: a CLI that offers ``Bash``
    while its MCP connection also happens to be down is not "we could not
    verify it", it is a confirmed problem with this exact build, and must
    stay refused until the build changes, not for a mere ten seconds.
    """
    advertised = frozenset(surface.tools)
    expected_tools = key[1]
    unexpected_tools = tuple(sorted(advertised - expected_tools))
    missing_tools = tuple(sorted(expected_tools - advertised))
    slash_commands = surface.slash_commands

    if surface.mcp_connected is False and not unexpected_tools and not slash_commands:
        raise UnavailableError(
            "Claude CLI could not connect the host's MCP server — "
            "cannot verify its tool surface",
        )

    mismatch = _ToolSurfaceMismatch(
        unexpected_tools=unexpected_tools,
        missing_tools=missing_tools,
        slash_commands=slash_commands,
    )
    with _tool_surface_lock:
        _tool_surface_verdicts[key] = mismatch
        _tool_surface_failures.pop(key, None)
    return mismatch


def _finish_tool_surface_check(build: BinaryBuild, mismatch: _ToolSurfaceMismatch) -> Path:
    if mismatch:
        raise CliToolSurfaceError(
            f"Claude CLI at {build.path} does not match its expected tool "
            f"surface — {mismatch.describe()}",
        )
    return Path(build.path)


def _binary_build(binary: Path) -> BinaryBuild:
    resolved = binary.resolve()
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise UnavailableError(f"Claude CLI at {resolved} cannot be read: {exc}") from exc
    return BinaryBuild(str(resolved), stat.st_mtime_ns, stat.st_size)


def _tool_surface_probe_arguments(*, mcp: _AttachedMcpServer | None) -> list[str]:
    cmd = [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        *_TOOL_ISOLATION_FLAGS,
    ]
    if mcp is not None:
        cmd += [
            "--allowedTools",
            _mcp_allowed_tools(mcp.spec),
            "--mcp-config",
            mcp.config_path,
        ]
    return cmd


# ── the probe itself: one overall deadline, async ───────────────────


async def _probe_cli_surface_async(
    binary: Path,
    *,
    mcp: _AttachedMcpServer | None,
    deadline: float,
) -> _AnnouncedSurface:
    """What a session built like ``cmd`` announces it can reach.

    Delegates to the sync gate on a worker thread, rather than spawning
    through ``asyncio.create_subprocess_exec`` directly. That keeps a stuck
    spawn away from the event loop; the sync gate delegates process handling
    to ``process.run_cli_bounded``.

    Reading the ``system`` init event and then killing the session bounds
    but does not eliminate the API call's cost: the full probe prompt is
    already on the wire by the time we read that line, so a request already
    in flight is not excluded — the CLI's own ``--max-budget-usd`` was
    checked live and only aborts a session *after* a call completes, not
    before one starts, so it does not close that gap either.

    The outer wait also covers executor queueing, while the runner keeps
    responsibility for eventually reaping a late spawn.
    """
    remaining = max(deadline - asyncio.get_running_loop().time(), 0)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _probe_cli_surface_sync,
                binary,
                mcp=mcp,
                deadline=deadline,
            ),
            timeout=remaining + _cleanup_margin_seconds(),
        )
    except asyncio.TimeoutError:
        raise UnavailableError("Claude CLI probe cleanup did not finish within its budget")


# ── the probe itself: one overall deadline, sync ────────────────────


def _probe_cli_surface_sync(
    binary: Path,
    *,
    mcp: _AttachedMcpServer | None,
    deadline: float,
) -> _AnnouncedSurface:
    """Run one tool-surface probe through the shared bounded CLI runner."""
    if deadline <= time.monotonic():
        raise UnavailableError("Claude CLI probe preflight budget was already exhausted")
    home = _claude_private_home()
    try:
        child = _claude_child_process(binary, _tool_surface_probe_arguments(mcp=mcp), home.path)
        stdin_payload = _TOOL_SURFACE_PROBE_PROMPT.encode()
        home_reservation = home.reserve()
        reservation = CLAUDE_CLI_PROCESS_POOL.reserve()
        if reservation is None:
            home_reservation.release()
            raise _ClaudeCliProcessPoolSaturated(CLAUDE_CLI_PROCESS_POOL.limit_message())

        def on_reaped(_process_id: int, _became_zombie: bool) -> None:
            CLAUDE_CLI_PROCESS_POOL.release(reservation)
            home_reservation.release()

        def on_spawn_failed() -> None:
            on_reaped(0, False)

        outcome = process.run_cli_bounded(
            child,
            stdin_payload=stdin_payload,
            read="first_line",
            deadline=deadline,
            stderr="devnull",
            output_read_limit_bytes=CLI_OUTPUT_READ_LIMIT_BYTES,
            on_spawn_failed=on_spawn_failed,
            on_reaped=on_reaped,
        )
    finally:
        home.close()
    if outcome.reason is process.CliRunReason.SPAWN_FAILED:
        raise UnavailableError("Claude CLI probe failed to run")
    if outcome.reason is process.CliRunReason.DEADLINE_BEFORE_SPAWN:
        raise UnavailableError("Claude CLI probe did not start within its budget")
    if outcome.reason is process.CliRunReason.CLEANUP_OVERRAN:
        raise UnavailableError("Claude CLI probe cleanup did not finish within its budget")
    if outcome.became_zombie:
        raise _ZombieProbeError(
            "Claude CLI probe process group was not confirmed gone after SIGKILL"
        )
    if outcome.reason is process.CliRunReason.IO_ERROR:
        raise UnavailableError("Claude CLI probe failed to run")
    if outcome.reason in {
        process.CliRunReason.DEADLINE_WHILE_WRITING,
        process.CliRunReason.DEADLINE_WHILE_READING,
    }:
        raise UnavailableError("Claude CLI probe did not answer within its budget")
    if outcome.reason is process.CliRunReason.OUTPUT_LIMIT_REACHED:
        raise UnavailableError("Claude CLI probe output exceeded its read limit")
    if outcome.reason is not process.CliRunReason.COMPLETE:
        raise RuntimeError(f"Unexpected Claude CLI probe outcome: {outcome.reason}")
    return _parse_announced_surface(outcome.stdout.encode(), mcp=mcp)


@asynccontextmanager
async def _anchored_turn_child(
    child: ChildProcess,
    *,
    home_reservation: process.PrivateHomeReservation,
    stream_buffer_limit: int | None = None,
) -> AsyncIterator[AnchoredChild]:
    """Claude's door to the neutral anchored turn: only its entry failures are translated.

    Whatever the turn body or the reap raises after the child is open propagates
    unchanged, so a ``FileNotFoundError`` from there is never a missing binary.
    """
    turn = AsyncExitStack()
    saturation_message = None
    try:
        anchored = await turn.enter_async_context(
            anchored_turn_child(
                CLAUDE_CLI_PROCESS_POOL,
                child,
                home_reservation=home_reservation,
                stream_buffer_limit=stream_buffer_limit,
            )
        )
    except CliProcessPoolSaturated as saturated:
        saturation_message = str(saturated)
    except FileNotFoundError as missing_binary:
        raise CliUnavailableError(CLAUDE_CLI_BINARY_NOT_FOUND_DETAIL) from missing_binary
    if saturation_message is not None:
        raise _ClaudeCliProcessPoolSaturated(saturation_message)
    async with turn:
        yield anchored


def _parse_announced_surface(
    raw_line: bytes,
    *,
    mcp: _AttachedMcpServer | None,
) -> _AnnouncedSurface:
    payload = _safe_json_loads(raw_line)
    if (
        payload is None
        or payload.get("type") != _CLI_INIT_EVENT_TYPE
        or payload.get("subtype") != _CLI_INIT_EVENT_SUBTYPE
    ):
        raise UnavailableError(
            "Claude CLI did not open with the expected session init event",
        )
    tools = payload.get("tools")
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise UnavailableError("Claude CLI announced an unreadable tool list")
    commands = payload.get("slash_commands")
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        raise UnavailableError("Claude CLI announced an unreadable slash-command list")
    return _AnnouncedSurface(
        tools=tuple(tools),
        slash_commands=tuple(commands),
        mcp_connected=None if mcp is None else _mcp_connected(payload, mcp.spec.name),
    )


def _mcp_connected(payload: dict, server_name: str) -> bool:
    """Whether the init event's own ``mcp_servers`` list reports the attached
    server connected — read instead of assumed, because a failed connection
    reports the same empty ``tools`` a clean tool-free CLI would."""
    servers = payload.get("mcp_servers")
    if not isinstance(servers, list):
        return False
    return any(
        isinstance(server, dict)
        and server.get("name") == server_name
        and server.get("status") == "connected"
        for server in servers
    )


def _parse_cli_output(stdout: str) -> str:
    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    _raise_for_error_result(outer)
    return outer.get("result", stdout)


def _require_claude_binary() -> Path:
    binary = _find_claude_binary()
    if not binary:
        raise CliUnavailableError(
            "Claude CLI not found. Install Claude Code or provide an API key."
        )
    return binary


def _require_anthropic():
    try:
        import anthropic

        return anthropic
    except ImportError:
        raise UnavailableError("anthropic package not installed. Run: pip install anthropic")


# ── API backends ───────────────────────────────────────────────────


def _call_api(
    prompt: str,
    api_key: str,
    system: str | None,
    model: str,
    max_tokens: int,
    messages: list[dict[str, str]] | None = None,
    *,
    deadline: float | None = None,
) -> ClaudeResponse:
    anthropic = _require_anthropic()
    with _client_lock:
        if api_key not in _sync_clients:
            _sync_clients[api_key] = anthropic.Anthropic(api_key=api_key)
        client = _sync_clients[api_key]

    kwargs = _build_api_kwargs(prompt, system, model, max_tokens, messages)
    request_client = (
        client.with_options(
            timeout=_remaining_deadline_seconds(deadline),
            max_retries=0,
        )
        if deadline is not None
        else client
    )
    try:
        response = request_client.messages.create(**kwargs)
    except anthropic.APITimeoutError as exc:
        if deadline is not None:
            raise UnavailableError(DEADLINE_EXCEEDED_REASON) from exc
        raise
    text = response.content[0].text if response.content else ""
    log.debug("Claude API response: %d chars", len(text))
    return ClaudeResponse(text=text)


def _remaining_deadline_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DeadlineExhausted(DEADLINE_EXCEEDED_REASON)
    return remaining


async def _acall_api(
    prompt: str,
    api_key: str,
    system: str | None,
    model: str,
    max_tokens: int,
    messages: list[dict[str, str]] | None = None,
) -> ClaudeResponse:
    anthropic = _require_anthropic()
    with _client_lock:
        if api_key not in _async_clients:
            _async_clients[api_key] = anthropic.AsyncAnthropic(api_key=api_key)
        client = _async_clients[api_key]

    kwargs = _build_api_kwargs(prompt, system, model, max_tokens, messages)
    response = await client.messages.create(**kwargs)
    text = response.content[0].text if response.content else ""
    log.debug("Claude async API response: %d chars", len(text))
    return ClaudeResponse(text=text)


# ── CLI backends ───────────────────────────────────────────────────


def _call_cli(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    messages: list[dict[str, str]] | None = None,
    *,
    deadline: float | None = None,
) -> ClaudeResponse:
    """The tool-free CLI backend behind both ``call_claude()`` and the
    one-shot completion (``claude.adapter.call_claude_once``).

    Every caller of this function carries content we did not write
    (host data) into the CLI, so the verified-tool-surface gate lives here
    rather than in each caller: a future caller of ``call_claude()``
    inherits it automatically instead of having to remember it.
    """
    binary = verify_no_builtin_cli_tools()
    flat_prompt = flatten_messages(prompt, messages)
    stdin_body = stdin_prompt(system, flat_prompt)
    home = _claude_private_home()
    try:
        child = _claude_child_process(binary, _cli_arguments(model), home.path)
        home_reservation = home.reserve()
    except BaseException:
        home.close()
        raise

    reservation = CLAUDE_CLI_PROCESS_POOL.reserve()
    if reservation is None:
        home_reservation.release()
        home.close()
        raise _ClaudeCliProcessPoolSaturated(CLAUDE_CLI_PROCESS_POOL.limit_message())
    try:
        try:
            proc = run_capturing(
                child,
                stdin_text=stdin_body,
                timeout_seconds=(
                    _remaining_deadline_seconds(deadline)
                    if deadline is not None
                    else CLAUDE_CLI_COMPLETION_TIMEOUT_SECONDS
                ),
            )
        except PipedProcessTimeout as exc:
            if deadline is not None:
                raise UnavailableError(DEADLINE_EXCEEDED_REASON) from exc
            raise UnavailableError(
                f"Claude CLI timed out after {CLAUDE_CLI_COMPLETION_TIMEOUT_SECONDS}s",
            ) from exc

        if proc.returncode != 0:
            log.warning(
                "Claude CLI failed (rc=%d, stderr_chars=%d)",
                proc.returncode,
                len(proc.stderr),
            )
            raise UnavailableError(CLAUDE_CLI_UNAVAILABLE_DETAIL)

        text = _parse_cli_output(proc.stdout)
        log.debug("Claude CLI response: %d chars", len(text))
        return ClaudeResponse(text=text.strip())
    finally:
        # subprocess.run() returns only after its child has exited, including
        # after its own timeout cleanup, so this opaque handle is safe to free.
        CLAUDE_CLI_PROCESS_POOL.release(reservation)
        home_reservation.release()
        home.close()


async def _acall_cli(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    messages: list[dict[str, str]] | None = None,
) -> ClaudeResponse:
    """Async twin of ``_call_cli`` — the tool-free CLI backend behind
    ``acall_claude()``, for a host's async callers.

    Gated the same way and for the same reason as ``_call_cli``: the gate
    sits in the call path both share, not in each caller.
    """
    binary = await averify_no_builtin_cli_tools()
    flat_prompt = flatten_messages(prompt, messages)
    stdin_body = stdin_prompt(system, flat_prompt)
    home = _claude_private_home()
    try:
        child = _claude_child_process(binary, _cli_arguments(model), home.path)
        async with _anchored_turn_child(child, home_reservation=home.reserve()) as proc:
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(stdin_body.encode()),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                raise UnavailableError("Claude CLI timed out after 120s")
    finally:
        home.close()

    stdout = stdout_bytes.decode()
    if proc.returncode != 0:
        log.warning(
            "Claude CLI failed (rc=%d, stderr_bytes=%d)",
            proc.returncode,
            len(stderr_bytes),
        )
        raise UnavailableError(CLAUDE_CLI_UNAVAILABLE_DETAIL)

    text = _parse_cli_output(stdout)
    log.debug("Claude async CLI response: %d chars", len(text))
    return ClaudeResponse(text=text.strip())


# ── Binary discovery ───────────────────────────────────────────────


def _find_claude_binary() -> Path | None:
    claude = current_claude_runtime()
    found = resolve_cli_binary(claude.binary)
    if found is not None:
        log.debug("Found claude binary on the configured search path: %s", found)
        return found

    # The mirror image of resolve_cli_binary: a glob may match a relative or
    # unexecutable path, and a child is only ever started from an absolute
    # binary this process can run.
    for pattern in claude.binary_search_globs:
        for candidate in sorted(glob.glob(pattern), reverse=True):
            path = Path(candidate).resolve()
            if path.is_file() and os.access(path, os.X_OK):
                return path

    return None


def parse_json_response(text: str) -> dict:
    """Extract and parse JSON from a Claude response that may have markdown wrapping."""
    json_str = text.strip()
    if "```" in json_str:
        json_str = json_str.split("```")[1]
        if json_str.startswith("json"):
            json_str = json_str[4:]
        json_str = json_str.strip()

    return json.loads(json_str)
