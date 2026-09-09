"""The one owner of child-process creation in this library.

Every child is described by one immutable :class:`ChildProcess`: an absolute
binary, an explicit working directory and the child's *complete* environment.
Nothing is inherited and nothing is merged onto a host baseline. An optional
set of extras on an inherited environment is a denylist seen from the other
side, and a denylist is what this owner exists to remove: the host's real
``HOME`` reaches an agent CLI's credential directory with the parent's rights.

``HOME`` must be set rather than merely absent, because an agent CLI with no
``HOME`` falls back to the passwd home — the very directory a closed
environment is meant to keep out of reach.

A closed environment is process configuration, not access control. It stops
discovery by convention (``HOME``, ``PATH``, ``CODEX_HOME``, ``GROK_HOME``,
``XDG_*``); it does not stop an absolute ``open()``, traversal out of the
working directory, or the network. UID, GID, umask, resource limits,
namespaces and mounts are inherited whatever this module does, and
``start_new_session`` moves only the session and process group. A host that
needs containment adds a sandbox around this spawn.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

HOME_VARIABLE: Final = "HOME"
SEARCH_PATH_VARIABLE: Final = "PATH"

# Names whose value is a property of the machine, not of the operator's
# account: without them an agent CLI cannot resolve its own helpers, speak the
# deployment's locale, or reach the network through its proxy and CA store.
# Naming them is what makes the child environment an allowlist; their values
# come from the host process because only the host knows them.
_INHERITED_MACHINE_VARIABLES: Final[tuple[str, ...]] = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
)

StderrPolicy = Literal["capture", "devnull"]


class ChildProcessSpecificationError(ValueError):
    """A child was described without an absolute binary, a cwd, or a HOME."""


def closed_environment(home: Path, **provider_variables: str) -> dict[str, str]:
    """Build a child's complete environment around one private home.

    The result is exactly ``HOME``, ``PATH``, the machine variables this host
    actually sets, and the provider's own variables. A caller cannot extend an
    inherited base, because there is none.
    """
    environment = {
        name: os.environ[name] for name in _INHERITED_MACHINE_VARIABLES if os.environ.get(name)
    }
    environment[SEARCH_PATH_VARIABLE] = os.environ.get(SEARCH_PATH_VARIABLE) or os.defpath
    environment[HOME_VARIABLE] = str(home)
    environment.update(provider_variables)
    return environment


@dataclass(frozen=True)
class ChildProcess:
    """The complete description of one child: binary, arguments, env, cwd."""

    binary: Path
    arguments: tuple[str, ...]
    environment: Mapping[str, str]
    working_directory: Path

    def __post_init__(self) -> None:
        if not self.binary.is_absolute():
            raise ChildProcessSpecificationError(
                f"A child process needs an absolute binary, not {self.binary}",
            )
        if not self.working_directory.is_absolute():
            raise ChildProcessSpecificationError(
                f"A child process needs an absolute working directory, "
                f"not {self.working_directory}",
            )
        if not self.environment.get(HOME_VARIABLE):
            raise ChildProcessSpecificationError(
                "A child process environment must set HOME: an unset HOME sends the "
                "child to the passwd home",
            )
        object.__setattr__(self, "arguments", tuple(self.arguments))
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    @property
    def command(self) -> tuple[str, ...]:
        return (str(self.binary), *self.arguments)

    def with_arguments(self, arguments: tuple[str, ...]) -> ChildProcess:
        return ChildProcess(
            binary=self.binary,
            arguments=arguments,
            environment=self.environment,
            working_directory=self.working_directory,
        )


def open_pipes(child: ChildProcess, *, stderr: StderrPolicy) -> subprocess.Popen[bytes]:
    """Start one child with pipes on every stream the caller reads."""
    return subprocess.Popen(
        child.command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if stderr == "capture" else subprocess.DEVNULL,
        env=dict(child.environment),
        cwd=str(child.working_directory),
        start_new_session=True,
    )


def run_capturing(
    child: ChildProcess,
    *,
    stdin_text: str,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    """Run one child to completion, feeding stdin and capturing both streams."""
    return subprocess.run(
        child.command,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=dict(child.environment),
        cwd=str(child.working_directory),
        start_new_session=True,
        check=False,
    )


async def open_async_pipes(
    child: ChildProcess,
    *,
    stream_buffer_limit: int | None = None,
) -> asyncio.subprocess.Process:
    """Start one child on the event loop with pipes on every stream."""
    buffer_limit = {} if stream_buffer_limit is None else {"limit": stream_buffer_limit}
    return await asyncio.create_subprocess_exec(
        *child.command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(child.environment),
        cwd=str(child.working_directory),
        start_new_session=True,
        **buffer_limit,
    )
