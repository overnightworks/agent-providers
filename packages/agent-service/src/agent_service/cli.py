"""``agent-service --config PATH``: harden the process, load the configuration, then serve.

A platform other than Linux, a root process, a process that cannot be made non-dumpable (so the
Claude program and anything it starts could read the service's memory), a configuration the
service refuses, or a pinned program that does not report its version ends the process
with exit status 2 and one line naming the reason; nothing is served. Otherwise it serves the
app with the CLI runner installed when a ``[cli]`` section is set. The runner trusts exactly the
CA bundle ``[mcp] ca_file`` names for the apps' MCP origins, loaded only when an app declares
one; a bundle that cannot be loaded ends the process the same way.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Final

from agent_service.process_hardening import StartupHardeningRefused, harden_process

EXIT_BAD_CONFIG: Final = 2
TRANSPORT_LOGGER_ROOTS: Final = ("httpx", "httpcore")
CLOSED_LOGGER_LEVEL: Final = logging.CRITICAL + 1


def close_transport_loggers() -> None:
    """Set the transport loggers above every call the dependencies make.

    A level fails ``isEnabledFor`` before any record exists, and a logger made later inherits
    it; a filter would act only after the record was built, and never on a child's.
    """
    children = [name for name in logging.Logger.manager.loggerDict if name.startswith("httpcore.")]
    for name in [*TRANSPORT_LOGGER_ROOTS, *children]:
        logging.getLogger(name).setLevel(CLOSED_LOGGER_LEVEL)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-service", description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="the service's TOML file")
    arguments = parser.parse_args(argv)
    try:
        harden_process()
    except StartupHardeningRefused as error:
        print(f"agent-service: refusing to start: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG
    # Imported only now: the application modules need Unix-only os constants at import, so
    # importing them first would crash a non-Linux start before the refusal line.
    import uvicorn

    from agent_providers.secret_file import shown_text
    from agent_service.app import create_app
    from agent_service.cli_runner import installed_runners, mcp_tls_context
    from agent_service.cli_version import ProgramVersionRefused, require_pinned_versions
    from agent_service.config import ConfigError, load_config
    from agent_service.run_liveness import LoopClock

    try:
        config = load_config(arguments.config)
    except ConfigError as error:
        print(f"agent-service: refusing to start: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG
    try:
        if config.cli is not None:
            require_pinned_versions(config.cli)
    except ProgramVersionRefused as error:
        print(
            f"agent-service: refusing to start: {shown_text(str(arguments.config))}: {error}",
            file=sys.stderr,
        )
        return EXIT_BAD_CONFIG
    try:
        trust_bundle = config.mcp_trust_bundle()
        tls_context = None if trust_bundle is None else mcp_tls_context(trust_bundle)
    except OSError as error:
        print(
            "agent-service: refusing to start: mcp.ca_file: cannot be loaded:"
            f" {shown_text(str(error))}",
            file=sys.stderr,
        )
        return EXIT_BAD_CONFIG
    runners = installed_runners(config, tls_context, LoopClock())
    close_transport_loggers()
    uvicorn.run(
        create_app(config, runners=runners),
        host=config.server.host,
        port=config.server.port,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
