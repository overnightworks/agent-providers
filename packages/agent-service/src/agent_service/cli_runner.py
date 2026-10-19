"""The CLI route's runner: the profile a route names, run through the core with its tools.

``CliRunner.run`` builds the provider run of the route's profile. When the run's capability
grants tools it first opens the run's tool set (``RunTools``), hands the adapter only the
relay's address and secret, and merges the relay's tool events with the core's text and ending
(``RelayedEvents``); the CLI's own tool lines are dropped. Its one exit path closes the tool
set once, with the core's reap, so a login refusal, a failed exit, a barrier refusal or a
lost containment reaches the service's run supervision as it was raised. It reads no
installed core configuration: each fact arrives as an argument.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from agent_providers.claude.provider import ClaudeRun
from agent_providers.cli_run import CliRunAdapter, RelayAccess, astream_cli_run
from agent_providers.config import PrivateWorkingRoot
from agent_providers.events import StreamEvent
from agent_service.config import Provider, ServiceConfig
from agent_service.relayed_events import RelayedEvents
from agent_service.run_liveness import RunClock
from agent_service.run_tools import RunTools
from agent_service.runners import AdmittedRun, Runners


@dataclass(frozen=True)
class ProfileLaunch:
    provider: Provider
    binary: Path
    token: SecretStr
    max_tool_calls: int


AdapterFactory = Callable[[ProfileLaunch, AdmittedRun, RelayAccess | None], CliRunAdapter]


def claude_run(
    launch: ProfileLaunch, admitted: AdmittedRun, relay: RelayAccess | None
) -> CliRunAdapter:
    turn = admitted.request.turn
    return ClaudeRun(
        binary=launch.binary,
        token=launch.token,
        system=turn.system,
        max_tool_calls=launch.max_tool_calls,
        history=tuple(turn.messages),
        relay=relay,
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


def mcp_tls_context(ca_file: Path) -> ssl.SSLContext:
    """The context that trusts exactly the bundle ``ca_file`` names.

    Not ``ssl.create_default_context()``: that one honours ``SSL_CERT_FILE`` and
    ``SSLKEYLOGFILE``, which would let the environment widen the trust or log the app's keys.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(ca_file))
    return context


class CliRunner:
    def __init__(
        self, config: ServiceConfig, *, tls_context: ssl.SSLContext | None, clock: RunClock
    ) -> None:
        assert config.cli is not None
        self._working_root: PrivateWorkingRoot = config.cli.working_root
        self._launches = _launches_of(config)
        self._tls_context = tls_context
        self._clock = clock

    async def run(self, admitted: AdmittedRun) -> AsyncGenerator[StreamEvent, None]:
        tools: RunTools | None = None
        events: RelayedEvents | None = None
        try:
            tools = await self._opened_tools(admitted)
            launch = self._launches[admitted.route.profile]
            adapter = RUN_ADAPTERS[launch.provider](
                launch, admitted, None if tools is None else tools.access
            )
            events = RelayedEvents(
                astream_cli_run(self._working_root, adapter),
                None if tools is None else tools.calls,
            )
            async for event in events:
                yield event
        finally:
            await self._finish(tools, events)

    async def _opened_tools(self, admitted: AdmittedRun) -> RunTools | None:
        capability = admitted.capability
        if capability is None or not capability.claims.tools:
            return None
        assert self._tls_context is not None
        return await RunTools.open(
            str(admitted.run_id), capability.claims.aud, capability, self._tls_context, self._clock
        )

    @staticmethod
    async def _finish(tools: RunTools | None, events: RelayedEvents | None) -> None:
        reap = events.close if events is not None else _nothing_to_reap
        if tools is None:
            await reap()
        else:
            await tools.close(reap)


async def _nothing_to_reap() -> None:
    return None


def installed_runners(
    config: ServiceConfig, tls_context: ssl.SSLContext | None, clock: RunClock
) -> Runners:
    if config.cli is None:
        return Runners()
    runner = CliRunner(config, tls_context=tls_context, clock=clock)
    return Runners(cli=runner, cli_offers_tools=False)
