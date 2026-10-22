"""The port through which an admitted run reaches a provider.

A runner turns one admitted run into the provider core's in-process turn
events (``agent_runtime.events``); the service maps those onto the wire.
Each route has at most one runner. Production installs the CLI
runner when ``[cli]`` is set, which serves every app, with or without tools;
the API runner (A2b) is not built yet. A run on a route without a runner fails
as ``provider_unavailable``.

An admitted run of an app with an MCP origin carries its capability
(``agent_runtime_service.capability``), minted at admission; a runner that sends it
keeps it out of everything it yields and logs. ``run`` must not raise before it
returns its generator; an async generator function satisfies this.

A runner must end when its step is cancelled or its generator is closed, and
its close must finish in bounded time: the service ends a silent or overlong
run by cancelling the step and waiting for the close (``run_liveness``), and
never leaves a runner behind.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agent_runtime.events import StreamEvent
from agent_runtime_client.wire import RunRequest
from agent_runtime_service.capability import Capability
from agent_runtime_service.identity import Principal
from agent_runtime_service.routing import Api, Cli, ServedRoute


@dataclass(frozen=True)
class AdmittedRun:
    run_id: UUID
    app: str
    principal: Principal
    request: RunRequest
    route: ServedRoute
    capability: Capability | None


class Runner(Protocol):
    def run(self, admitted: AdmittedRun) -> AsyncGenerator[StreamEvent, None]: ...


@dataclass(frozen=True)
class Runners:
    cli: Runner | None = None
    api: Runner | None = None

    def for_route(self, served: ServedRoute) -> Runner | None:
        match served:
            case Cli():
                return self.cli
            case Api():
                return self.api
