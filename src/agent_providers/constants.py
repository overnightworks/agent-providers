"""The provider layer's own mechanics: values that are the same everywhere.

A constant lives here when it describes how the agent CLIs and HTTP APIs
behave — how long a probe may take, how much of a CLI's output is read, which
words a login probe looks for, which endpoint a catalog is fetched from. Those
are facts about Claude, Codex and Grok, not about the host that runs them, so
every deployment holds the same value and none of them belongs in
:class:`~agent_providers.config.ProviderRuntimeConfig`.

Everything that *does* differ per deployment — binaries, credential mirrors,
mounted directories, models, keys, process caps — arrives through that
configuration instead, and product vocabulary (a host's defaults, its image and
history rules, its user-facing texts) stays with the host. The rule is simply
the split: a value stays here when it is the same in every deployment, and goes
to the config or the host when it is not.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

CLI_LOGIN_STATUS_CACHE_SECONDS = 30
# Agent CLI login probes return only a few hundred bytes. These bounds keep a
# broken mounted binary from holding a request or its memory indefinitely.
CLI_OUTPUT_READ_LIMIT_BYTES = 64 * 1024
CLI_TERMINATION_GRACE_SECONDS = 1

TURN_TIMEOUT_SECONDS = 600
PROBE_TIMEOUT_SECONDS = 15
CLAUDE_API_MAX_TOKENS = 4_096
GROK_CLI_LINE_CHANNEL_CAPACITY = 64

# The real init event measured 0.34s; these budgets
# keep a wide margin over that without letting a stuck probe block a request
# for anywhere near as long as the old 30s did.
CLAUDE_CLI_TOOL_SURFACE_TIMEOUT_SECONDS = 10
CLAUDE_CLI_NO_TOOL_SURFACE_TIMEOUT_SECONDS = 5
CLAUDE_CLI_COMPLETION_TIMEOUT_SECONDS = 120
# Short on purpose: long enough to shield a struggling probe from being
# re-run by every concurrent caller, short enough that a real repair (binary
# reinstalled, DB reachable again) is picked up on the next request rather
# than staying failed for the lifetime of the success cache.
CLAUDE_CLI_TOOL_SURFACE_FAILURE_CACHE_SECONDS = 10
# The bound, from a reap's first SIGKILL, within which it must count a CLI
# turn's process group empty (the anchored turn's reap and the bounded
# runner's alike) before it frees the turn's slot; also the longest
# wait for the leader's exit to be published after that count. A group not
# confirmed by then is retained, not freed.
CLI_GROUP_CONFIRMATION_SECONDS = 5
# A process that outlives SIGKILL is not a transient hiccup — ten more
# seconds will not make it healthy, and probing again on that schedule only
# spawns another zombie. Cached failures of that specific kind get this much
# longer TTL instead of CLAUDE_CLI_TOOL_SURFACE_FAILURE_CACHE_SECONDS.
CLAUDE_CLI_ZOMBIE_FAILURE_CACHE_SECONDS = 300
# A hard ceiling on all Claude CLI processes: live turns, probes, the leaders of
# retained groups (held until confirm_group_gone frees them), and probe processes
# the bounded runner's background reaper still waits for. A zombie storm
# deliberately degrades to refusing new work (fail-closed), rather than growing
# without bound.
CLAUDE_CLI_MAX_CONCURRENT_PROCESSES = 8

# A missed deadline has one stable reason across a provider's own bound and
# the outer watchdog, so callers need not infer it from incidental prose.
DEADLINE_EXCEEDED_REASON: Final[str] = "deadline_exceeded"


class ProviderName(StrEnum):
    """The providers this library routes a turn to.

    A member equals its name string, so a host's stored string is found by value.
    """

    CLAUDE = "claude"
    GROK = "grok"
    CODEX = "codex"


GROK_CHAT_URL = "https://api.x.ai/v1/chat/completions"
GROK_MODELS_URL = "https://api.x.ai/v1/models"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_API_VERSION = "2023-06-01"

GROK_MODEL_PREFIX = "grok-"
GROK_NON_CHAT_MARKERS: Final[tuple[str, ...]] = (
    "imagine", "image", "video", "voice", "tts", "whisper",
)
OPENAI_CHAT_PREFIXES: Final[tuple[str, ...]] = (
    "gpt-", "o1", "o3", "o4", "codex",
)
OPENAI_NON_CHAT_MARKERS: Final[tuple[str, ...]] = (
    "whisper", "tts", "embedding", "dall-e", "dalle", "transcribe", "realtime",
    "audio", "image", "search", "moderation",
)
CLAUDE_MODEL_PREFIX = "claude-"
CLAUDE_CLI_MODEL_LIST_MARKER = "Available: "

CLAUDE_OAUTH_TOKEN_VARIABLE: Final[str] = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_NO_SESSION_PERSISTENCE_FLAG: Final[str] = "--no-session-persistence"
CLAUDE_RUN_SWITCHES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
    },
)
CLAUDE_CLI_STATUS_ARGS: Final[tuple[str, ...]] = ("auth", "status")
CLAUDE_CLI_LOGGED_IN_FIELD: Final[str] = "loggedIn"
CLAUDE_CLI_AUTH_METHOD_FIELD: Final[str] = "authMethod"
GROK_CLI_STREAMING_OUTPUT_FORMAT: Final[str] = "streaming-json"
GROK_CLI_STATUS_ARGS: Final[tuple[str, ...]] = ("models",)
GROK_CLI_LOGGED_IN_MARKER: Final[str] = "You are logged in with "
GROK_CLI_LOGGED_OUT_MARKER: Final[str] = "You are not authenticated."
GROK_CLI_MODEL_LIST_MARKER: Final[str] = "Available models:"
GROK_CLI_MODEL_BULLETS: Final[tuple[str, ...]] = ("* ", "- ")
CODEX_CLI_STATUS_ARGS: Final[tuple[str, ...]] = ("login", "status")
CODEX_CLI_MODELS_ARGS: Final[tuple[str, ...]] = ("debug", "models")
CODEX_CLI_LOGGED_IN_MARKER: Final[str] = "Logged in using "
CODEX_CLI_LOGGED_OUT_MARKER: Final[str] = "Not logged in"
