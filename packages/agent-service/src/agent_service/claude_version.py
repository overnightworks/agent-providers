"""The Claude version the installation pins, and the startup probe that holds the program to it."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Final

from pydantic import BeforeValidator

from agent_providers.config import PrivateWorkingRoot
from agent_providers.process import (
    CliEnvironmentUnavailableError,
    CliRun,
    run_catalog_cli,
    run_directories,
)
from agent_providers.secret_file import shown_text
from agent_providers.spawn import ChildProcess, closed_environment

VERSION_PATTERN: Final = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", re.ASCII)
VERSION_OUTPUT_SUFFIX: Final = " (Claude Code)"
MALFORMED_PIN: Final = (
    "must be three dot-separated integers without leading zeros, written as a string,"
    " such as 2.1.302"
)


class ClaudeVersionRefused(ValueError):
    """The program does not report the pinned version; the message follows the config path."""


def _pin_in_grammar(value: object) -> str:
    if not isinstance(value, str) or VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError(MALFORMED_PIN)
    return value


PinnedClaudeVersion = Annotated[str, BeforeValidator(_pin_in_grammar)]


def parse_version_output(stdout: str) -> str | None:
    """The version ``claude --version`` printed, or ``None`` when the output is anything else."""
    text = stdout.removesuffix("\n")
    if not text.endswith(VERSION_OUTPUT_SUFFIX):
        return None
    version = text.removesuffix(VERSION_OUTPUT_SUFFIX)
    return version if VERSION_PATTERN.fullmatch(version) else None


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


def _refusal(binary: Path, pinned: str, run: CliRun | None) -> str | None:
    shown_binary = shown_text(str(binary))
    reason = _unusable_run(run)
    reported = parse_version_output(run.stdout) if run is not None and reason is None else None
    if reported is None:
        why = reason or _what_was_printed(run.stdout)
        return (
            f"cli.claude_version: {shown_binary} did not print a version ({why});"
            f" the pinned version is {pinned}"
        )
    if reported != pinned:
        return f"cli.claude_version: {shown_binary} reports {reported}, not the pinned {pinned}"
    return None


def require_pinned_version(binary: Path, pinned: str, working_root: PrivateWorkingRoot) -> None:
    """Run ``binary --version`` in a run tree below ``working_root``; refuse unless ``pinned``.

    The tree goes only once the probe's spawn failed or its group is confirmed gone; a tree
    that cannot be removed is logged by the core and never changes the outcome.
    """
    refusal = _refusal(binary, pinned, _probe(binary, working_root))
    if refusal is not None:
        raise ClaudeVersionRefused(refusal)
