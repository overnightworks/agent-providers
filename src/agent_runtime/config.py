"""The runtime configuration a host application injects into the provider layer.

The provider layer runs Claude, Grok and Codex on someone else's machine: its
binaries, credential mirrors, mounted resource directories, API
keys and the MCP server a turn attaches are deployment facts the host
owns, not values this package may guess. Its catalog and turn paths
therefore read them from one frozen value the host installs once per process
via :func:`configure`, instead of reaching into an application settings
module; the subscription run
(:class:`agent_runtime.claude.provider.ClaudeRun`, in the directories of
:func:`agent_runtime.process.run_directories`) and the working root
(:class:`PrivateWorkingRoot`) take them as explicit arguments instead.

The configuration is cut along the two ways into this library. A host that only
asks which models exist and whether a provider is logged in configures
:class:`ProviderRuntimeConfig` alone; a host that also runs turns adds
:class:`TurnRuntimeConfig`. Turn facts are therefore never mandatory fields a
catalog host has to invent an answer for, and a turn path that runs without
them fails loudly through :func:`current_turn_config` rather than against a
guessed value.

Nothing is installed by default. :func:`current_config` raises
:class:`ProviderRuntimeNotConfiguredError` until the host has configured the
process, so a forgotten call fails loudly at the first provider turn rather
than silently running against a guessed path. A process configures once, so
replacing an installed configuration with a *different* one raises
:class:`ProviderRuntimeAlreadyConfiguredError` rather than letting the later
caller quietly win; :func:`reset_config` drops the installation again — the
test-suite counterpart of clearing a settings cache.
"""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from agent_runtime.errors import raise_without_chain

MAX_FILE_PREFIX_LENGTH = 64

FilePrefix = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=MAX_FILE_PREFIX_LENGTH,
        pattern=r"^[A-Za-z0-9._-]+$",
    ),
]
"""A file-name stem: the library appends random characters and creates the
file directly in ``cli_working_directory_root``, so no separator can be in it."""


class McpServerSpec(BaseModel):
    """The one stdio MCP server a host attaches to a turn.

    The provider layer knows no MCP server of its own. It writes this
    declaration into the temporary ``--mcp-config`` file the CLI reads,
    pre-approves the server's tools on the command line, and refuses to run a
    turn unless the CLI announces exactly ``tool_names`` — so the host, not
    this package, decides what a prompt carrying untrusted content can reach.

    ``environment`` is written into that file in the order given, with
    ``user_id_environment_variable`` appended per turn; the file is mode 0600,
    which is why credentials belong here and never on the command line.

    ``config_file_prefix`` is a :data:`FilePrefix`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    name: str = Field(min_length=1)
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    environment: dict[str, SecretStr] = Field(default_factory=dict)
    user_id_environment_variable: str = Field(min_length=1)
    config_file_prefix: FilePrefix
    tool_names: frozenset[str] = Field(min_length=1)


class GrokTurnRuntime(BaseModel):
    """The Grok turn facts, present only on a host that runs Grok turns.

    ``prompt_file_prefix`` is a :data:`FilePrefix`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    prompt_file_prefix: FilePrefix
    prompt_file_placeholder: str = Field(min_length=1)


class CodexTurnRuntime(BaseModel):
    """The Codex turn facts, present only on a host that runs Codex turns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code_mode_host_binary: Path
    resources_directory: Path
    max_concurrent_processes: int = Field(ge=1)
    max_concurrent_image_runs: int = Field(ge=1)


class TurnRuntimeConfig(BaseModel):
    """The deployment facts a turn needs and a catalog never reads.

    Every turn receives a fresh private home beneath ``cli_working_directory_root``.
    A Claude turn's home holds no login file: its setup token, read from
    ``claude.token_file`` at each start, reaches the child only as
    ``CLAUDE_CODE_OAUTH_TOKEN`` in its environment. Grok and Codex turns get a
    private copy of their credential file in theirs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    mcp_server: McpServerSpec | None

    codex: CodexTurnRuntime | None = None
    grok: GrokTurnRuntime | None = None


class ClaudeCliRuntime(BaseModel):
    """The deployment facts of the Claude CLI, present only on a host that runs it.

    ``token_file`` is the provider's login source: one line, the token
    ``claude setup-token`` prints, in a file that grants group and others nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    binary: str
    binary_search_globs: tuple[str, ...] = ()
    token_file: Path


class GrokCliRuntime(BaseModel):
    """The deployment facts of the Grok CLI, present only on a host that runs it.

    ``token_file`` is the provider's login source; Grok defines its format, an
    ``auth.json`` copied whole, mode 0400.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    binary: str
    token_file: Path


class CodexCliRuntime(BaseModel):
    """The deployment facts of the Codex CLI, present only on a host that runs it.

    ``token_file`` is the provider's login source; Codex defines its format, an
    ``auth.json`` whose copy is blanked: mode 0600 for a turn, 0400 for the catalog.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    binary: str
    token_file: Path


class ProviderRuntimeConfig(BaseModel):
    """Every deployment fact the provider layer needs, as one immutable value.

    ``cli_working_directory_root`` is the directory below which the catalog and
    turn paths create every child's private working directory and every
    temporary file those paths write, so a deployment states that root once instead of inheriting
    whatever ``TMPDIR`` happens to be. It holds every credential copy, so
    :func:`configure` refuses a root that is not private: an absolute path
    written without a ``..`` component to an existing directory, not a symlink,
    owned by the process's effective user, with mode exactly 0700. The host
    keeps that owner, mode and path resolution, and the process's effective
    identity, for the process lifetime. The check proves privacy only on a filesystem with POSIX
    ownership and modes, and claims no protection against a hostile
    replacement after startup.

    ``cli_binary_search_path`` is the only place a bare CLI name is resolved
    from. A child is always started from an absolute binary; resolving one
    against the parent's inherited ``PATH`` would put that choice back in the
    environment this layer closed.

    ``claude.token_file``, ``grok.token_file`` and ``codex.token_file``
    are each provider's login source. ``claude.token_file`` holds the setup token,
    which a Claude child gets only in its environment; its catalog copies no file.
    ``grok.token_file`` and ``codex.token_file`` are the credential files the Grok
    and Codex catalog paths copy, mode 0400, into a private child home; each
    provider defines the format of its own file. Those children receive only that
    home, the provider's own home variable, ``PATH`` and the machine's locale, proxy
    and CA settings — never the operator's credential directory, whose renewal
    writes would otherwise succeed with the parent's rights.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    anthropic_api_key: SecretStr | None = None
    xai_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    claude: ClaudeCliRuntime | None = None

    grok: GrokCliRuntime | None = None

    codex: CodexCliRuntime | None = None

    cli_binary_search_path: tuple[Path, ...]

    cli_working_directory_root: Path

    turns: TurnRuntimeConfig | None = None


class HostWiringError(RuntimeError):
    """The host did not install what a call needs; no call can fix it."""


class ProviderRuntimeNotConfiguredError(HostWiringError):
    """A provider ran before its host installed a runtime configuration."""


class ProviderRuntimeAlreadyConfiguredError(RuntimeError):
    """A second, differing configuration was installed over a live one."""


class ProviderNotConfiguredError(HostWiringError):
    """A provider ran under a configuration that holds no block for it."""

    def __init__(self, provider: str, block: str) -> None:
        super().__init__(
            f"The {provider} provider is not configured: the runtime "
            f"configuration has no {block!r} block."
        )
        self.provider = provider
        self.block = block

    def __reduce__(self) -> tuple[type[ProviderNotConfiguredError], tuple[str, str]]:
        return (type(self), (self.provider, self.block))


class TurnRuntimeNotConfiguredError(HostWiringError):
    """A turn path ran under a configuration that covers only the catalog."""


class WorkingRootViolation(StrEnum):
    RELATIVE = "relative"
    MISSING = "missing"
    SYMLINK = "symlink"
    FOREIGN_OWNER = "foreign_owner"
    WRONG_MODE = "wrong_mode"
    UNINSPECTABLE = "uninspectable"
    PARENT_COMPONENT = "parent_component"


class WorkingRootNotPrivateError(RuntimeError):
    """``cli_working_directory_root`` is not a private directory.

    It names only the closed violation, never the path or the failure of the
    inspection that found it: the root is the parent of every credential copy.
    """

    def __init__(self, violation: WorkingRootViolation) -> None:
        super().__init__(f"cli_working_directory_root is not private: {violation.value}.")
        self.violation = violation

    def __reduce__(self) -> tuple[type[WorkingRootNotPrivateError], tuple[WorkingRootViolation]]:
        return (type(self), (self.violation,))


@dataclass(frozen=True)
class PrivateWorkingRoot:
    """A working root that passed the privacy rule when it was constructed.

    The host keeps the root's owner, mode and path resolution, and the
    process's effective identity, for the process lifetime. The check proves
    privacy only on a filesystem with POSIX ownership and modes, and claims no
    protection against a hostile replacement after construction.
    """

    path: Path

    def __post_init__(self) -> None:
        _inspect_working_root(self.path)


_NOT_CONFIGURED_DETAIL = (
    "The agent-provider runtime is unconfigured. Call "
    "agent_runtime.config.configure() during application startup."
)

_TURNS_NOT_CONFIGURED_DETAIL = (
    "This process is configured for the provider catalog only. Install a "
    "TurnRuntimeConfig as ProviderRuntimeConfig.turns before running a turn."
)

_ALREADY_CONFIGURED_DETAIL = (
    "The agent-provider runtime is already configured with a different value. "
    "A process configures once; call agent_runtime.config.reset_config() "
    "first to install another one."
)

_PRIVATE_DIRECTORY_MODE = 0o700

_configured: ProviderRuntimeConfig | None = None
_configuration_lock = threading.Lock()


def _inspect_working_root(root: Path) -> None:
    if not root.is_absolute():
        raise_without_chain(WorkingRootNotPrivateError(WorkingRootViolation.RELATIVE))
    if ".." in root.parts:
        raise_without_chain(WorkingRootNotPrivateError(WorkingRootViolation.PARENT_COMPONENT))
    try:
        status = os.lstat(root)
    except (FileNotFoundError, NotADirectoryError):
        raise_without_chain(WorkingRootNotPrivateError(WorkingRootViolation.MISSING))
    except (OSError, ValueError):
        raise_without_chain(WorkingRootNotPrivateError(WorkingRootViolation.UNINSPECTABLE))
    if stat.S_ISLNK(status.st_mode):
        raise_without_chain(WorkingRootNotPrivateError(WorkingRootViolation.SYMLINK))
    if not stat.S_ISDIR(status.st_mode):
        raise_without_chain(WorkingRootNotPrivateError(WorkingRootViolation.MISSING))
    if status.st_uid != os.geteuid():
        raise_without_chain(WorkingRootNotPrivateError(WorkingRootViolation.FOREIGN_OWNER))
    if stat.S_IMODE(status.st_mode) != _PRIVATE_DIRECTORY_MODE:
        raise_without_chain(WorkingRootNotPrivateError(WorkingRootViolation.WRONG_MODE))


def configure(config: ProviderRuntimeConfig) -> None:
    """Install the host's configuration as this process's provider runtime.

    Installing the same value again is a no-op, so a host may call this from
    every startup path it owns without ordering them. Installing a differing
    one is refused: two owners disagreeing about one deployment is a defect,
    and letting the later caller win would silently discard the earlier value.

    Every call first inspects ``cli_working_directory_root`` and raises
    :class:`WorkingRootNotPrivateError` unless it is an absolute path written
    without a ``..`` component to an existing directory, not a symlink, owned
    by the effective user, with mode exactly 0700. A refusal installs nothing
    and leaves an earlier installation in place. The host keeps the root's
    owner, mode and path resolution, and the process's effective identity, for
    the process lifetime; the check proves privacy only on a filesystem with
    POSIX ownership and modes and guards against no hostile replacement after
    startup.
    """
    global _configured
    _inspect_working_root(config.cli_working_directory_root)
    with _configuration_lock:
        if _configured is not None and _configured != config:
            raise ProviderRuntimeAlreadyConfiguredError(_ALREADY_CONFIGURED_DETAIL)
        _configured = config


def current_config() -> ProviderRuntimeConfig:
    """Return the installed configuration, or refuse to run without one."""
    with _configuration_lock:
        config = _configured
    if config is None:
        raise ProviderRuntimeNotConfiguredError(_NOT_CONFIGURED_DETAIL)
    return config


def current_turn_config() -> TurnRuntimeConfig:
    """Return the installed turn configuration, or refuse to run a turn.

    A catalog-only host configures no turn facts, so a turn path reaching this
    deployment is a wiring defect. It says so here instead of running against
    an invented model name or an invented resource directory.
    """
    turns = current_config().turns
    if turns is None:
        raise TurnRuntimeNotConfiguredError(_TURNS_NOT_CONFIGURED_DETAIL)
    return turns


def reset_config() -> None:
    """Drop the installed configuration so the next caller must configure again."""
    global _configured
    with _configuration_lock:
        _configured = None


def current_claude_runtime() -> ClaudeCliRuntime:
    """Return the installed Claude block, or refuse naming the missing provider."""
    claude = current_config().claude
    if claude is None:
        raise ProviderNotConfiguredError("claude", "claude")
    return claude


def current_grok_runtime() -> GrokCliRuntime:
    """Return the installed Grok block, or refuse naming the missing provider."""
    grok = current_config().grok
    if grok is None:
        raise ProviderNotConfiguredError("grok", "grok")
    return grok


def current_grok_turn_runtime() -> GrokTurnRuntime:
    """Return the installed Grok turn block, or refuse naming the missing provider."""
    grok = current_turn_config().grok
    if grok is None:
        raise ProviderNotConfiguredError("grok", "turns.grok")
    return grok


def current_codex_runtime() -> CodexCliRuntime:
    """Return the installed Codex block, or refuse naming the missing provider."""
    codex = current_config().codex
    if codex is None:
        raise ProviderNotConfiguredError("codex", "codex")
    return codex


def current_codex_turn_runtime() -> CodexTurnRuntime:
    """Return the installed Codex turn block, or refuse naming the missing provider."""
    codex = current_turn_config().codex
    if codex is None:
        raise ProviderNotConfiguredError("codex", "turns.codex")
    return codex
