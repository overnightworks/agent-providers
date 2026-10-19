"""The CLI route's runner: the profile a route names, run tool-free through the core.

``CliRunner.run`` builds the provider run of the route's profile and yields every event of
``astream_cli_run`` unchanged. It catches nothing, so a login refusal, a failed exit or a
lost containment reaches the service's run supervision as the core raised it. It reads no
installed core configuration: each fact arrives as an argument.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from agent_providers.claude.provider import ClaudeRun
from agent_providers.cli_run import CliRunAdapter, astream_cli_run
from agent_providers.config import PrivateWorkingRoot
from agent_providers.events import StreamEvent
from agent_service.config import Provider, ServiceConfig
from agent_service.runners import AdmittedRun, Runners


@dataclass(frozen=True)
class ProfileLaunch:
    provider: Provider
    binary: Path
    token: SecretStr
    max_tool_calls: int


AdapterFactory = Callable[[ProfileLaunch, AdmittedRun], CliRunAdapter]


def claude_run(launch: ProfileLaunch, admitted: AdmittedRun) -> CliRunAdapter:
    turn = admitted.request.turn
    return ClaudeRun(
        binary=launch.binary,
        token=launch.token,
        system=turn.system,
        max_tool_calls=launch.max_tool_calls,
        history=tuple(turn.messages),
        relay=None,
    )


RUN_ADAPTERS: Final[Mapping[Provider, AdapterFactory]] = {Provider.CLAUDE: claude_run}


def _launches_of(config: ServiceConfig) -> Mapping[str, ProfileLaunch]:
    assert config.cli is not None
    programs = config.cli.programs()
    launches = {}
    for name, profile in config.profiles.items():
        assert profile.token is not None
        launches[name] = ProfileLaunch(
            provider=profile.provider,
            binary=programs[profile.provider].binary,
            token=profile.token,
            max_tool_calls=config.runs.max_tool_calls,
        )
    return launches


class CliRunner:
    def __init__(self, config: ServiceConfig) -> None:
        assert config.cli is not None
        self._working_root: PrivateWorkingRoot = config.cli.working_root
        self._launches = _launches_of(config)

    async def run(self, admitted: AdmittedRun) -> AsyncGenerator[StreamEvent, None]:
        launch = self._launches[admitted.route.profile]
        adapter = RUN_ADAPTERS[launch.provider](launch, admitted)
        async with aclosing(astream_cli_run(self._working_root, adapter)) as events:
            async for event in events:
                yield event


def installed_runners(config: ServiceConfig) -> Runners:
    if config.cli is None:
        return Runners()
    return Runners(cli=CliRunner(config), cli_offers_tools=False)
