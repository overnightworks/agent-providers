"""The port through which an admitted run reaches a provider.

A runner turns one admitted run into the provider core's in-process turn
events (``agent_providers.events``); the service maps those onto the wire.
Each route has at most one runner. The CLI runner (slice A1b) and the API
runner (A2b) are not built yet, so production installs none and a run on a
route without a runner fails as ``provider_unavailable``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agent_client.wire import RunRequest
from agent_providers.events import StreamEvent
from agent_service.identity import Principal
from agent_service.routing import Api, Cli, ServedRoute


@dataclass(frozen=True)
class AdmittedRun:
    run_id: UUID
    app: str
    principal: Principal
    request: RunRequest
    route: ServedRoute


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
