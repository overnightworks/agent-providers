"""The service's first startup step: refuse root, then make the process non-dumpable.

A non-dumpable process owns its ``/proc/<pid>/environ`` and ``/proc/<pid>/mem`` as root, so a
same-UID child of the Claude program cannot read the signing key or the token the service holds.
This runs before the configuration is read, so no secret is in memory when it closes.
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Any, Final

PR_SET_DUMPABLE: Final = 4
NOT_DUMPABLE: Final = 0
REQUIRED_PLATFORM: Final = "linux"


class StartupHardeningRefused(Exception):
    """The process may not serve; the text is the one-line reason."""


def running_platform() -> str:
    return sys.platform


def effective_user_id() -> int:
    return os.geteuid()


def load_libc() -> Any:
    return ctypes.CDLL(None, use_errno=True)


def set_not_dumpable() -> None:
    prctl = load_libc().prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(PR_SET_DUMPABLE, NOT_DUMPABLE, 0, 0, 0) == -1:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def harden_process() -> None:
    if running_platform() != REQUIRED_PLATFORM:
        raise StartupHardeningRefused(
            f"needs Linux (/proc and prctl); this platform is {running_platform()}"
        )
    if effective_user_id() == 0:
        raise StartupHardeningRefused(
            "running as root (effective uid 0); run the service as an unprivileged user"
        )
    try:
        set_not_dumpable()
    except OSError as error:
        raise StartupHardeningRefused(
            f"cannot make the process non-dumpable: {error.strerror}"
        ) from error
