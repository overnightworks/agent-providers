"""The HTTP surface: ``POST /runs``, the one entrance through which an app starts a run,
``GET /runs/{id}``, through which the app and user that started it read where it stands,
and ``POST /job-tickets``, through which an app gets a ticket for a later background run.

All routes check the two tokens first, with the same answers. A ``POST`` is then
checked in a fixed order, and the first failure answers:

1. the workload token, then the user token, then membership of the app
   (``/apps/<app>`` or a path below it), before the body is read or anything else
   is decided (``agent_runtime_service.identity``);
2. the body's framing, then its protocol version, then its shape
   (``agent_runtime_service.run_request``);
3. the person's role in the app (``admin`` in ``/apps/<app>/admin`` or below, else
   ``user``) must be one of the body's ``roles``, else ``forbidden``;
4. tools named by an app without an MCP origin answer ``forbidden``, before the route;
5. the route (``agent_runtime_service.routing``), which reads the resolved model (the app's
   per-agent override, else the request's) and the installation's listed models;
6. for the CLI route, the service's admission limit (``agent_runtime_service.admission``): a
   subscription run beyond it answers ``busy`` with ``Retry-After`` before anything is
   signed;
7. for an app with an MCP origin, whether the run's capability, which signs exactly the
   request's tool names, fits the 8,192 characters every verifier accepts
   (``capability_too_large``).

Steps 3 to 7 are ``POST /runs``'s. ``POST /job-tickets`` answers ``not_enabled`` right after
step 2 when the installation has no ``[signing]`` key, and otherwise mints.

An admitted run answers ``200 text/event-stream`` with its frames
(``agent_runtime_service.run_events``), and its response closes those frames whenever it
ends, however it ends, and hands its runner one capability when its app has an MCP
origin. Every refusal is a typed ``RunRejection``
that is never cached. No answer echoes a token. A ``GET`` answers the run's
``RunStatus`` to its owner, and one ``not_found`` to everyone else: for an id
nobody began, an expired or evicted run, another app's or user's run, and a
path segment that is not a run id's text form.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing
from datetime import datetime
from http import HTTPStatus
from typing import Final
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send
from webauth.https_client import fetch_key_set_over_https
from webauth.jwt_verification import KeySetFetch, MonotonicClock

from agent_runtime import lost_groups
from agent_runtime_client.capability import MAX_CAPABILITY_CHARACTERS
from agent_runtime_client.wire import (
    PROTOCOL_VERSION,
    JobTicketRequest,
    RejectionCode,
    RunRejection,
    RunRequest,
)
from agent_runtime_service.admission import AdmissionSlot, Lease
from agent_runtime_service.capability import (
    Capability,
    CapabilityScope,
    CapabilityTooLarge,
    capability_claims,
    signed_capability,
)
from agent_runtime_service.config import ServiceConfig
from agent_runtime_service.identity import Caller, CallerCheck, build_verifier
from agent_runtime_service.job_ticket import (
    job_ticket_claims,
    parse_job_ticket_request,
    signed_job_ticket,
)
from agent_runtime_service.quarantine import GroupConfirmation, Quarantine
from agent_runtime_service.routing import Cli, NotEnabled, ServedRoute, route
from agent_runtime_service.run_events import run_frames
from agent_runtime_service.run_liveness import LoopClock
from agent_runtime_service.run_records import RunRecords, utc_now
from agent_runtime_service.run_request import MAX_REQUEST_BYTES, parse_run_request
from agent_runtime_service.runners import AdmittedRun, Runners

WORKLOAD_TOKEN_HEADER: Final = "Agent-Workload-Token"
RETRY_UNAVAILABLE_AFTER_SECONDS: Final = 30
RETRY_BUSY_AFTER_SECONDS: Final = 10
_BEARER_SCHEME: Final = "bearer"
_JSON_MEDIA_TYPE: Final = "application/json"

_log = logging.getLogger(__name__)

_STATUS: Final[Mapping[RejectionCode, HTTPStatus]] = {
    RejectionCode.NOT_ENABLED: HTTPStatus.FORBIDDEN,
    RejectionCode.BUSY: HTTPStatus.TOO_MANY_REQUESTS,
    RejectionCode.UNSUPPORTED_VERSION: HTTPStatus.BAD_REQUEST,
    RejectionCode.UNAUTHORIZED: HTTPStatus.UNAUTHORIZED,
    RejectionCode.FORBIDDEN: HTTPStatus.FORBIDDEN,
    RejectionCode.INVALID_REQUEST: HTTPStatus.UNPROCESSABLE_ENTITY,
    RejectionCode.UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    RejectionCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    RejectionCode.CAPABILITY_TOO_LARGE: HTTPStatus.UNPROCESSABLE_ENTITY,
}

_MESSAGE: Final[Mapping[RejectionCode, str]] = {
    RejectionCode.NOT_ENABLED: "AI is not enabled for this request.",
    RejectionCode.BUSY: "The service is busy; retry later.",
    RejectionCode.UNSUPPORTED_VERSION: f"This service speaks protocol version {PROTOCOL_VERSION}.",
    RejectionCode.UNAUTHORIZED: "Both a user token and a workload token are required and must"
    " be valid.",
    RejectionCode.FORBIDDEN: "The tokens do not permit this request.",
    RejectionCode.INVALID_REQUEST: "The body is not a valid request for the route.",
    RejectionCode.UNAVAILABLE: "The service cannot check tokens right now; retry later.",
    RejectionCode.NOT_FOUND: "No such run.",
    RejectionCode.CAPABILITY_TOO_LARGE: "This run's capability would exceed"
    f" {MAX_CAPABILITY_CHARACTERS} characters.",
}


def create_app(
    config: ServiceConfig,
    *,
    runners: Runners | None = None,
    records: RunRecords | None = None,
    fetch_key_set: KeySetFetch = fetch_key_set_over_https,
    monotonic_clock: MonotonicClock = time.monotonic,
    wall_clock: Callable[[], datetime] = utc_now,
    confirm_group_gone: GroupConfirmation = lost_groups.confirm_group_gone,
) -> FastAPI:
    """The service's ASGI app.

    ``wall_clock`` (aware UTC) stamps each capability's and job ticket's issue and expiry and
    the default run-record store. An injected ``records`` keeps its own clock, so the two can
    differ; ``wall_clock`` never reaches the token verifier, which reads webauth's own.
    ``confirm_group_gone`` is how the one quarantine asks whether a lost process group is gone.
    """
    callers = CallerCheck(build_verifier(config.identity, fetch_key_set, monotonic_clock), config)
    installed_runners = runners or Runners()
    installed_records = records or RunRecords(wall_clock)
    subscription_slot = AdmissionSlot(config.runs.max_parallel_cli_runs)
    quarantine = Quarantine(LoopClock(), confirm_group_gone)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.post("/job-tickets")(_job_ticket_route(callers, config, wall_clock))

    @app.post("/runs")
    async def start_run(request: Request) -> Response:
        caller = await _caller_of(callers, request)
        if isinstance(caller, RejectionCode):
            return rejection(caller)
        body = await _limited_body(request)
        run_request = RejectionCode.INVALID_REQUEST if body is None else parse_run_request(body)
        if isinstance(run_request, RejectionCode):
            return rejection(run_request)
        return _admitted(
            config,
            installed_runners,
            installed_records,
            subscription_slot,
            quarantine,
            wall_clock,
            caller,
            run_request,
        )

    @app.get("/runs/{run_id}")
    async def run_status(request: Request, run_id: str) -> Response:
        caller = await _caller_of(callers, request)
        if isinstance(caller, RejectionCode):
            return rejection(caller)
        parsed_id = _run_id_from(run_id)
        status = (
            None
            if parsed_id is None
            else installed_records.read(parsed_id, caller.app, caller.principal)
        )
        if status is None:
            return rejection(RejectionCode.NOT_FOUND)
        return Response(
            content=status.model_dump_json(),
            media_type=_JSON_MEDIA_TYPE,
            headers={"Cache-Control": "no-store"},
        )

    return app


class _RunResponse(StreamingResponse):
    """A run's response, which closes the run's frames itself whenever it ends.

    Starlette never closes the generator it streams, and a generator left
    paused behind a blocked send is only finalized by the garbage collector.

    It also releases the run's lease on exit, after the frames are closed. That
    frees the place only for a run that never began, because the lease is
    released once the run is over, however long the connection hangs on, and
    a lease the quarantine took over is not this one.
    """

    def __init__(self, frames: AsyncGenerator[bytes, None], lease: Lease | None) -> None:
        super().__init__(
            frames, media_type="text/event-stream", headers={"Cache-Control": "no-store"}
        )
        self._lease = lease

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            async with aclosing(self.body_iterator):
                await super().__call__(scope, receive, send)
        finally:
            _release(self._lease)


def _admitted(
    config: ServiceConfig,
    runners: Runners,
    records: RunRecords,
    subscription_slot: AdmissionSlot,
    quarantine: Quarantine,
    wall_clock: Callable[[], datetime],
    caller: Caller,
    run_request: RunRequest,
) -> Response:
    if caller.role not in run_request.roles:
        return rejection(RejectionCode.FORBIDDEN)
    scope = config.capability_scope(caller.app, run_request.tools)
    if run_request.tools and scope is None:
        return rejection(RejectionCode.FORBIDDEN)
    served = route(
        config.identity,
        config.route,
        config.profiles,
        caller.groups,
        config.model_for(caller.app, run_request.purpose, run_request.model),
        config.subscription_models(),
    )
    if isinstance(served, NotEnabled):
        return rejection(RejectionCode.NOT_ENABLED)
    lease = None
    if isinstance(served, Cli):
        lease = subscription_slot.try_take()
        if lease is None:
            return rejection(RejectionCode.BUSY)
    try:
        response = _admitted_response(
            config,
            runners,
            records,
            lease,
            quarantine,
            wall_clock,
            caller,
            run_request,
            served,
            scope,
        )
    except BaseException:
        _release(lease)
        raise
    if isinstance(response, RejectionCode):
        _release(lease)
        return rejection(response)
    return response


def _release(lease: Lease | None) -> None:
    if lease is not None:
        lease.release()


def _admitted_response(
    config: ServiceConfig,
    runners: Runners,
    records: RunRecords,
    lease: Lease | None,
    quarantine: Quarantine,
    wall_clock: Callable[[], datetime],
    caller: Caller,
    run_request: RunRequest,
    served: ServedRoute,
    scope: CapabilityScope | None,
) -> _RunResponse | RejectionCode:
    run_id = uuid.uuid4()
    try:
        capability = _capability_of(config, scope, caller, run_request, run_id, wall_clock)
    except CapabilityTooLarge as too_large:
        _log.warning(
            "The capability of a run of app %s is %d characters; the limit is %d.",
            caller.app,
            too_large.length,
            MAX_CAPABILITY_CHARACTERS,
        )
        return RejectionCode.CAPABILITY_TOO_LARGE
    admitted = AdmittedRun(
        run_id=run_id,
        app=caller.app,
        principal=caller.principal,
        request=run_request,
        route=served,
        capability=capability,
    )
    return _RunResponse(
        run_frames(
            admitted,
            runners.for_route(served),
            config.runs,
            LoopClock(),
            records,
            lease,
            quarantine=quarantine,
        ),
        lease,
    )


def _capability_of(
    config: ServiceConfig,
    scope: CapabilityScope | None,
    caller: Caller,
    run_request: RunRequest,
    run_id: UUID,
    wall_clock: Callable[[], datetime],
) -> Capability | None:
    if scope is None:
        return None
    claims = capability_claims(
        scope,
        run_id=run_id,
        app=caller.app,
        issuer=caller.principal.issuer,
        subject=caller.principal.subject,
        purpose=run_request.purpose,
        now=wall_clock().timestamp(),
        max_duration_s=config.runs.max_duration_s,
    )
    return signed_capability(scope, claims)


def _job_ticket_route(
    callers: CallerCheck, config: ServiceConfig, wall_clock: Callable[[], datetime]
) -> Callable[[Request], Awaitable[Response]]:
    async def mint_job_ticket(request: Request) -> Response:
        caller = await _caller_of(callers, request)
        if isinstance(caller, RejectionCode):
            return rejection(caller)
        body = await _limited_body(request)
        ticket_request = (
            RejectionCode.INVALID_REQUEST if body is None else parse_job_ticket_request(body)
        )
        if isinstance(ticket_request, RejectionCode):
            return rejection(ticket_request)
        return _minted(config, caller, ticket_request, wall_clock)

    return mint_job_ticket


def _minted(
    config: ServiceConfig,
    caller: Caller,
    ticket_request: JobTicketRequest,
    wall_clock: Callable[[], datetime],
) -> Response:
    if config.signing is None:
        return rejection(RejectionCode.NOT_ENABLED)
    claims = job_ticket_claims(config, caller, ticket_request, wall_clock().timestamp())
    return Response(
        content=signed_job_ticket(config.signing.key, claims).model_dump_json(),
        media_type=_JSON_MEDIA_TYPE,
        headers={"Cache-Control": "no-store"},
    )


def rejection(code: RejectionCode) -> Response:
    headers = {"Cache-Control": "no-store"}
    if code is RejectionCode.UNAUTHORIZED:
        headers["WWW-Authenticate"] = "Bearer"
    if code is RejectionCode.UNAVAILABLE:
        headers["Retry-After"] = str(RETRY_UNAVAILABLE_AFTER_SECONDS)
    if code is RejectionCode.BUSY:
        headers["Retry-After"] = str(RETRY_BUSY_AFTER_SECONDS)
    body = RunRejection(v=PROTOCOL_VERSION, code=code, message=_MESSAGE[code])
    return Response(
        content=body.model_dump_json(),
        status_code=_STATUS[code],
        media_type=_JSON_MEDIA_TYPE,
        headers=headers,
    )


async def _caller_of(callers: CallerCheck, request: Request) -> Caller | RejectionCode:
    return await callers.caller(
        request.headers.get(WORKLOAD_TOKEN_HEADER),
        _bearer_token(request.headers.get("Authorization")),
    )


def _run_id_from(segment: str) -> UUID | None:
    """The run id a path segment names: only the 8-4-4-4-12 text form, in any letter case."""
    try:
        run_id = UUID(segment)
    except ValueError:
        return None
    return run_id if str(run_id) == segment.lower() else None


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != _BEARER_SCHEME or not token or " " in token:
        return None
    return token


async def _limited_body(request: Request) -> bytes | None:
    """The body, or ``None`` once it is known to exceed ``MAX_REQUEST_BYTES``.

    A declared length over the limit is refused unread, and a body without
    one is refused at the first chunk that would pass the limit, before that
    chunk is buffered.
    """
    declared = request.headers.get("Content-Length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_REQUEST_BYTES):
        return None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            return None
        body += chunk
    return bytes(body)
