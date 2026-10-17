"""``agent-service --config PATH``: load the configuration, then serve ``POST /runs``.

A configuration the service refuses, or a Claude program that does not report the pinned
version, ends the process with exit status 2 and one line naming the field; nothing is served.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

import uvicorn

from agent_service.app import create_app
from agent_service.claude_version import ClaudeVersionRefused, require_pinned_version
from agent_service.config import ConfigError, load_config
from agent_service.secret_file import shown_text

EXIT_BAD_CONFIG: Final = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-service", description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="the service's TOML file")
    arguments = parser.parse_args(argv)
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
