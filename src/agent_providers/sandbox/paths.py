"""The private directory names a host's mount policy has to permit.

Every confined Codex run — the image turn, the tool turn, and the proof that
the deployment's sandbox still holds — works inside Bubblewrap with exactly one
writable place: a ``codex-home`` below a directory named by one of these
prefixes. These prefixes are therefore the directories a host's sandbox profile
must permit, and nothing else: each name is a contract between this package and
the policy file the host loads into its kernel. The host owns its own profile —
an AppArmor or seccomp profile it derives from these constants — and this
package ships the constants, not the profile. Stating a name once here is what
makes a rename a red test in the host's suite instead of a silently refused
mount at the next turn.

A project embedding this package brings its own profile built from these
constants; when a second host exists, naming the ``/tmp`` namespace the
directories live in becomes its own injected deployment fact, the way
``cli_working_directory_root`` and ``cli_prompt_file_prefix`` already are.
"""

from __future__ import annotations

from typing import Final

CODEX_IMAGE_TURN_DIRECTORY_PREFIX: Final = "songmaker-cover-codex-"
CODEX_TOOL_TURN_DIRECTORY_PREFIX: Final = "songmaker-codex-tool-"
CODEX_SANDBOX_PROOF_DIRECTORY: Final = "songmaker-codex-sandbox-probe"
CODEX_HOME_DIRECTORY_NAME: Final = "codex-home"
