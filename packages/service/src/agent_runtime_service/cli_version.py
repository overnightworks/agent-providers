"""The pinned programs' version readers, one per adapter, and the startup probe that uses them."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from agent_runtime.config import PrivateWorkingRoot
from agent_runtime.process import (
    CliEnvironmentUnavailableError,
    CliRun,
    run_catalog_cli,
    run_directories,
)
from agent_runtime.secret_file import shown_text
from agent_runtime.spawn import ChildProcess, closed_environment
from agent_runtime_service.config import VERSION_PATTERN, CliConfig, ProgramPin, Provider

CLAUDE_VERSION_SUFFIX: Final = " (Claude Code)"

VersionReader = Callable[[str], str | None]


class ProgramVersionRefused(ValueError):
    """A program does not report its pinned version; the message follows the config path."""


def claude_version_output(stdout: str) -> str | None:
    """The version ``claude --version`` printed, or ``None`` when the output is anything else."""
    text = stdout.removesuffix("\n")
    if not text.endswith(CLAUDE_VERSION_SUFFIX):
        return None
    version = text.removesuffix(CLAUDE_VERSION_SUFFIX)
    return version if VERSION_PATTERN.fullmatch(version) else None


VERSION_READERS: Final[Mapping[Provider, VersionReader]] = {
    Provider.CLAUDE: claude_version_output,
}


def _unusable_run(run: CliRun | None) -> str | None:
    """Why the run cannot have printed a version, or ``None`` when its output is worth parsing."""
    if run is None:
        return "probe could not start or its start deadline expired"
    if not run.complete:
        return "probe did not complete"
    if run.returncode != 0:
        return f"exit status {run.returncode}"
    return None


def _what_was_printed(stdout: str) -> str:
    if not stdout:
        return "printed nothing"
    return f"printed {len(stdout)} characters"


def _probe(binary: Path, working_root: PrivateWorkingRoot) -> CliRun | None:
    """The probe's run, or ``None`` when its tree could not be made or its start was refused."""
    try:
        with run_directories(working_root, {}) as directories:
            child = ChildProcess(
                binary=binary,
                arguments=("--version",),
                environment=closed_environment(directories.home),
                working_directory=directories.working_directory,
            )
            return run_catalog_cli(child, home=directories)
    except CliEnvironmentUnavailableError:
        return None


def _refusal(
    provider: Provider, pin: ProgramPin, run: CliRun | None, read_version: VersionReader
) -> str | None:
    shown_binary = shown_text(str(pin.binary))
    reason = _unusable_run(run)
    reported = read_version(run.stdout) if run is not None and reason is None else None
    if reported is None:
        why = reason or _what_was_printed(run.stdout)
        return (
            f"cli.{provider}.version: {shown_binary} did not print a version ({why});"
            f" the pinned version is {pin.version}"
        )
    if reported != pin.version:
        return (
            f"cli.{provider}.version: {shown_binary} reports {reported},"
            f" not the pinned {pin.version}"
        )
    return None


def require_pinned_versions(cli: CliConfig) -> None:
    """Run each pinned ``binary --version`` in a run tree below the root; refuse the first miss.

    A tree goes only once its probe's spawn failed or its group is confirmed gone; a tree
    that cannot be removed is logged by the core and never changes the outcome.
    """
    for provider, pin in cli.programs().items():
        read_version = VERSION_READERS[provider]
        refusal = _refusal(provider, pin, _probe(pin.binary, cli.working_root), read_version)
        if refusal is not None:
            raise ProgramVersionRefused(refusal)
