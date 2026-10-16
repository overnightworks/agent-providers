"""The HTTP surface: ``POST /runs``, the one entrance through which an app starts a run,
and ``GET /runs/{id}``, through which the app and user that started it read where it stands.

Both routes check the two tokens first, with the same answers. A ``POST`` is then
checked in a fixed order, and the first failure answers:

1. the workload token, then the user token (``agent_service.identity``);
2. the body's protocol version, then its shape (``agent_service.run_request``);
3. whether the app may ask for the purpose;
4. the route (``agent_service.routing``).

An admitted run answers ``200 text/event-stream`` with its frames
(``agent_service.run_events``). Every refusal is a typed ``RunRejection``
that is never cached. No answer echoes a token. A ``GET`` answers the run's
``RunStatus`` to its owner, and one ``not_found`` to everyone else: for an id
nobody began, an expired or evicted run, another app's or user's run, and a
path segment that is not a run id's text form.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from http import HTTPStatus
from typing import Final
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from webauth.jwt_verification import KeySetFetch, MonotonicClock, fetch_key_set_over_https

from agent_client.capability import MAX_CAPABILITY_CHARACTERS
from agent_client.wire import PROTOCOL_VERSION, RejectionCode, RunRejection, RunRequest
from agent_service.config import ServiceConfig
from agent_service.identity import Caller, CallerCheck, build_verifier
from agent_service.routing import NotEnabled, route
from agent_service.run_events import run_frames
from agent_service.run_liveness import LoopClock
from agent_service.run_records import RunRecords
from agent_service.run_request import MAX_REQUEST_BYTES, parse_run_request
from agent_service.runners import AdmittedRun, Runners

WORKLOAD_TOKEN_HEADER: Final = "Agent-Workload-Token"
RETRY_UNAVAILABLE_AFTER_SECONDS: Final = 30
_BEARER_SCHEME: Final = "bearer"

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
    RejectionCode.NOT_ENABLED: "AI is not enabled for this user and purpose.",
    RejectionCode.BUSY: "The service is busy; retry later.",
    RejectionCode.UNSUPPORTED_VERSION: f"This service speaks protocol version {PROTOCOL_VERSION}.",
    RejectionCode.UNAUTHORIZED: "Both a user token and a workload token are required and must"
    " be valid.",
    RejectionCode.FORBIDDEN: "The tokens do not permit this request.",
    RejectionCode.INVALID_REQUEST: "The body is not a valid run request.",
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
) -> FastAPI:
    callers = CallerCheck(build_verifier(config.identity, fetch_key_set, monotonic_clock), config)
    installed_runners = runners or Runners()
    installed_records = records or RunRecords()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/runs")
    async def start_run(request: Request) -> Response:
        caller = await _caller_of(callers, request)
        if isinstance(caller, RejectionCode):
            return rejection(caller)
        body = await _limited_body(request)
        run_request = RejectionCode.INVALID_REQUEST if body is None else parse_run_request(body)
        if isinstance(run_request, RejectionCode):
            return rejection(run_request)
        return _admitted(config, installed_runners, caller, run_request)

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
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    return app


def _admitted(
    config: ServiceConfig, runners: Runners, caller: Caller, run_request: RunRequest
) -> Response:
    app_config = config.apps[caller.app]
    if run_request.purpose not in app_config.purposes:
        return rejection(RejectionCode.FORBIDDEN)
    served = route(
        config.purposes[run_request.purpose],
        config.profiles,
        caller.groups,
        caller.app,
        subscription_group=config.identity.subscription_group,
    )
    if isinstance(served, NotEnabled):
        return rejection(RejectionCode.NOT_ENABLED)
    admitted = AdmittedRun(
        run_id=uuid.uuid4(),
        app=caller.app,
        principal=caller.principal,
        request=run_request,
        route=served,
    )
    return StreamingResponse(
        run_frames(admitted, runners.for_route(served), app_config, config.runs, LoopClock()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store"},
    )


def rejection(code: RejectionCode) -> Response:
    headers = {"Cache-Control": "no-store"}
    if code is RejectionCode.UNAUTHORIZED:
        headers["WWW-Authenticate"] = "Bearer"
    if code is RejectionCode.UNAVAILABLE:
        headers["Retry-After"] = str(RETRY_UNAVAILABLE_AFTER_SECONDS)
    body = RunRejection(v=PROTOCOL_VERSION, code=code, message=_MESSAGE[code])
    return Response(
        content=body.model_dump_json(),
        status_code=_STATUS[code],
        media_type="application/json",
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
