"""The runtime configuration a host application injects into the provider layer.

The provider layer runs Claude, Grok and Codex on someone else's machine: its
binaries, credential mirrors, mounted resource directories, API
keys and the MCP server a turn attaches are deployment facts the host
owns, not values this package may guess. Its catalog and turn paths
therefore read them from one frozen value the host installs once per process
via :func:`configure`, instead of reaching into an application settings
module; the subscription run
(:class:`agent_providers.claude.provider.ClaudeRun`) takes them as explicit
arguments instead.

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
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from agent_providers.errors import raise_without_chain


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
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    command: str = Field(min_length=1)
    args: tuple[str, ...] = ()
    environment: dict[str, SecretStr] = Field(default_factory=dict)
    user_id_environment_variable: str = Field(min_length=1)
    config_file_prefix: str = Field(min_length=1)
    tool_names: frozenset[str] = Field(min_length=1)


class TurnRuntimeConfig(BaseModel):
    """The deployment facts a turn needs and a catalog never reads.

    Claude and Grok turns receive a fresh private credential home beneath
    ``cli_working_directory_root``. Codex does the same for its own turn paths.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    codex_code_mode_host_binary: Path
    codex_resources_directory: Path

    codex_max_concurrent_processes: int = Field(ge=1)
    codex_max_concurrent_image_runs: int = Field(ge=1)

    cli_prompt_file_prefix: str = Field(min_length=1)
    cli_prompt_file_placeholder: str = Field(min_length=1)

    mcp_server: McpServerSpec | None


class ClaudeCliRuntime(BaseModel):
    """The deployment facts of the Claude CLI, present only on a host that runs it.

    ``token_file`` is the provider's login source; Claude defines its format.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    binary: str
    binary_search_globs: tuple[str, ...] = ()
    token_file: Path


class ProviderRuntimeConfig(BaseModel):
    """Every deployment fact the provider layer needs, as one immutable value.

    ``cli_working_directory_root`` is the directory below which every child
    creates its private working directory and every temporary file this layer
    writes, so a deployment states that root once instead of inheriting
    whatever ``TMPDIR`` happens to be. It holds every credential copy, so
    :func:`configure` refuses a root that is not private: an absolute path to an
    existing directory, not a symlink, owned by the process's effective user,
    with mode exactly 0700. The host keeps that owner, mode and path
    resolution, and the process's effective identity, for the process
    lifetime. The check proves privacy only on a filesystem with POSIX
    ownership and modes, and claims no protection against a hostile
    replacement after startup.

    ``cli_binary_search_path`` is the only place a bare CLI name is resolved
    from. A child is always started from an absolute binary; resolving one
    against the parent's inherited ``PATH`` would put that choice back in the
    environment this layer closed.

    ``claude.token_file``, ``grok_cli_auth_file`` and ``codex_cli_auth_file``
    are each provider's login source: the credential files the catalog paths
    copy, mode 0400, into a private child home. The provider defines the
    format of its own file. Those children receive only that home, the provider's own home
    variable, ``PATH`` and the machine's locale, proxy and CA settings — never
    the operator's credential directory, whose renewal writes would otherwise
    succeed with the parent's rights.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    anthropic_api_key: SecretStr | None = None
    xai_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    claude: ClaudeCliRuntime | None = None

    grok_cli_binary: str
    codex_cli_binary: str
    cli_binary_search_path: tuple[Path, ...]

    grok_cli_auth_file: Path
    codex_cli_auth_file: Path

    cli_working_directory_root: Path

    turns: TurnRuntimeConfig | None = None


class ProviderRuntimeNotConfiguredError(RuntimeError):
    """A provider ran before its host installed a runtime configuration."""


class ProviderRuntimeAlreadyConfiguredError(RuntimeError):
    """A second, differing configuration was installed over a live one."""


class ProviderNotConfiguredError(RuntimeError):
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


class TurnRuntimeNotConfiguredError(RuntimeError):
    """A turn path ran under a configuration that covers only the catalog."""


class WorkingRootViolation(StrEnum):
    RELATIVE = "relative"
    MISSING = "missing"
    SYMLINK = "symlink"
    FOREIGN_OWNER = "foreign_owner"
    WRONG_MODE = "wrong_mode"
    UNINSPECTABLE = "uninspectable"


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


_NOT_CONFIGURED_DETAIL = (
    "The agent-provider runtime is unconfigured. Call "
    "agent_providers.config.configure() during application startup."
)

_TURNS_NOT_CONFIGURED_DETAIL = (
    "This process is configured for the provider catalog only. Install a "
    "TurnRuntimeConfig as ProviderRuntimeConfig.turns before running a turn."
)

_ALREADY_CONFIGURED_DETAIL = (
    "The agent-provider runtime is already configured with a different value. "
    "A process configures once; call agent_providers.config.reset_config() "
    "first to install another one."
)

_PRIVATE_DIRECTORY_MODE = 0o700

_configured: ProviderRuntimeConfig | None = None
_configuration_lock = threading.Lock()


def _inspect_working_root(root: Path) -> None:
    if not root.is_absolute():
        raise_without_chain(WorkingRootNotPrivateError(WorkingRootViolation.RELATIVE))
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
    :class:`WorkingRootNotPrivateError` unless it is an absolute path to an
    existing directory, not a symlink, owned by the effective user, with mode
    exactly 0700. A refusal installs nothing and leaves an earlier installation
    in place. The host keeps the root's owner, mode and path resolution, and the
    process's effective identity, for the process lifetime; the check proves
    privacy only on a filesystem with POSIX ownership and modes and guards
    against no hostile replacement after startup.
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
