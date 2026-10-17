"""``agent-service --config PATH``: harden the process, load the configuration, then serve.

A platform other than Linux, a root process, a process that cannot be made non-dumpable (so the
Claude program and anything it starts could read the service's memory), a configuration the
service refuses, or a Claude program that does not report the pinned version ends the process
with exit status 2 and one line naming the reason; nothing is served.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from agent_service.process_hardening import StartupHardeningRefused, harden_process

EXIT_BAD_CONFIG: Final = 2


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

    from agent_service.app import create_app
    from agent_service.claude_version import ClaudeVersionRefused, require_pinned_version
    from agent_service.config import ConfigError, load_config
    from agent_service.secret_file import shown_text

    try:
        config = load_config(arguments.config)
    except ConfigError as error:
        print(f"agent-service: refusing to start: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG
    try:
        if config.cli is not None:
            require_pinned_version(config.cli.claude_binary, config.cli.claude_version)
    except ClaudeVersionRefused as error:
        print(
            f"agent-service: refusing to start: {shown_text(str(arguments.config))}: {error}",
            file=sys.stderr,
        )
        return EXIT_BAD_CONFIG
    uvicorn.run(create_app(config), host=config.server.host, port=config.server.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
