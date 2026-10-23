"""The models one person can run in one app, as ``GET /offer`` lists them.

Each model a ``[cli.<provider>]`` table lists is put to ``routing.route`` as ``/runs``
admission puts it, with the same five fixed arguments; the one decider says whether the
person is enabled and on which route. This module adds only readiness, read the way
``run_frames`` is handed its runner: a route is ready when a runner is installed for it.
"""

from __future__ import annotations

from agent_runtime_client.wire import OfferedModel, OfferReason, OfferRoute
from agent_runtime_service.config import ServiceConfig
from agent_runtime_service.routing import Cli, NotEnabled, route
from agent_runtime_service.runners import Runners


def offered_models(
    config: ServiceConfig, runners: Runners, groups: frozenset[str]
) -> tuple[OfferedModel, ...]:
    """The enabled models, sorted by id; a model ``route`` does not enable is absent."""
    listed_models = config.subscription_models()
    offered = []
    for provider, names in listed_models.items():
        for name in names:
            model = f"{provider}/{name}"
            served = route(
                config.identity, config.route, config.profiles, groups, model, listed_models
            )
            if isinstance(served, NotEnabled):
                continue
            ready = runners.for_route(served) is not None
            offered.append(
                OfferedModel(
                    id=model,
                    object="model",
                    created=0,
                    owned_by=provider,
                    route=OfferRoute.CLI if isinstance(served, Cli) else OfferRoute.API,
                    ready=ready,
                    reason=None if ready else OfferReason.NOT_BUILT,
                )
            )
    return tuple(sorted(offered, key=lambda entry: entry.id))
