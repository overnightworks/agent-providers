"""The private directory names a host's mount policy has to permit.

Every confined Codex run, the image turn and the tool turn, works inside
Bubblewrap with exactly one writable place: a ``codex-home`` below a directory
named by one of these prefixes. Each directory is created directly below the
host's private ``cli_working_directory_root``, so a host profile permits
``<root>/<prefix>*/codex-home/`` for its resolved root and nothing else. Each
name is a contract between this package and the policy file the host loads into
its kernel. The host owns its own profile, an AppArmor or seccomp profile it
derives from these constants, and this package ships the constants, not the
profile. Stating a name once here is what makes a rename a red test in the
host's suite instead of a silently refused mount at the next turn.
"""

from __future__ import annotations

from typing import Final

CODEX_IMAGE_TURN_DIRECTORY_PREFIX: Final = "agent-cli-codex-image-turn-"
CODEX_TOOL_TURN_DIRECTORY_PREFIX: Final = "agent-cli-codex-tool-turn-"
CODEX_HOME_DIRECTORY_NAME: Final = "codex-home"
