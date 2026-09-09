"""The runtime configuration a host application injects into the provider layer.

The provider layer runs Claude, Grok and Codex on someone else's machine: its
binaries, credential mirrors, mounted resource directories, chat model, API
keys and the MCP server a co-writer turn attaches are deployment facts the host
owns, not values this package may guess. It therefore reads them from one
frozen value the host installs once per process via :func:`configure`, instead
of reaching into an application settings module.

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

Self-contained by design: pydantic only, no application import, so the package
can be released on its own (issue #825).
"""

from __future__ import annotations

import threading
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class McpServerSpec(BaseModel):
    """The one stdio MCP server a host attaches to a co-writer turn.

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

    claude_chat_model: str
    codex_code_mode_host_binary: Path
    codex_resources_directory: Path

    codex_max_concurrent_processes: int = Field(ge=1)
    codex_max_concurrent_image_runs: int = Field(ge=1)

    cli_prompt_file_prefix: str = Field(min_length=1)
    cli_prompt_file_placeholder: str = Field(min_length=1)

    mcp_server: McpServerSpec | None


class ProviderRuntimeConfig(BaseModel):
    """Every deployment fact the provider layer needs, as one immutable value.

    ``cli_working_directory_root`` is the directory below which every child
    creates its private working directory and every temporary file this layer
    writes, so a deployment that confines the agent CLIs — songmaker sandboxes
    them under ``/tmp`` — states that root once instead of inheriting whatever
    ``TMPDIR`` happens to be.

    ``cli_binary_search_path`` is the only place a bare CLI name is resolved
    from. A child is always started from an absolute binary; resolving one
    against the parent's inherited ``PATH`` would put that choice back in the
    environment this layer closed.

    ``claude_cli_auth_file``, ``grok_cli_auth_file`` and ``codex_cli_auth_file``
    are the credential files the catalog paths copy, mode 0400, into a private
    child home. Those children receive only that home, the provider's own home
    variable, ``PATH`` and the machine's locale, proxy and CA settings — never
    the operator's credential directory, whose renewal writes would otherwise
    succeed with the parent's rights.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    anthropic_api_key: SecretStr | None = None
    xai_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    claude_cli_binary: str
    grok_cli_binary: str
    codex_cli_binary: str
    claude_cli_binary_search_globs: tuple[str, ...] = ()
    cli_binary_search_path: tuple[Path, ...]

    claude_cli_auth_file: Path
    grok_cli_auth_file: Path
    codex_cli_auth_file: Path

    cli_working_directory_root: Path

    turns: TurnRuntimeConfig | None = None


class ProviderRuntimeNotConfiguredError(RuntimeError):
    """A provider ran before its host installed a runtime configuration."""


class ProviderRuntimeAlreadyConfiguredError(RuntimeError):
    """A second, differing configuration was installed over a live one."""


class TurnRuntimeNotConfiguredError(RuntimeError):
    """A turn path ran under a configuration that covers only the catalog."""


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

_configured: ProviderRuntimeConfig | None = None
_configuration_lock = threading.Lock()


def configure(config: ProviderRuntimeConfig) -> None:
    """Install the host's configuration as this process's provider runtime.

    Installing the same value again is a no-op, so a host may call this from
    every startup path it owns without ordering them. Installing a differing
    one is refused: two owners disagreeing about one deployment is a defect,
    and letting the later caller win would silently discard the earlier value.
    """
    global _configured
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
