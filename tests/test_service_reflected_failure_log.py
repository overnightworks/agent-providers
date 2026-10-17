"""The service's log of a failure whose upstream reflected the capability holds none of it.

Each scenario lets an upstream echo the ``Capability.token`` the client sent
into one place the upstream controls, then writes the closed error through
``log_failure`` and through ``logger.exception`` to a handler formatted by
uvicorn's own ``DefaultFormatter``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import ssl
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field

import httpx
import pytest
from capability_support import capability_carrying
from mcp_app_fake import (
    CERTIFICATE_HOST,
    FakeResponse,
    McpAppFake,
    RecordedRequest,
    ScriptedApp,
    TestAuthority,
    serving,
)
from service_support import failure_lines
from uvicorn.logging import DefaultFormatter

from agent_service.app_tools import UpstreamError, open_app_tools
from agent_service.run_liveness import log_failure

CAPABILITY = capability_carrying("canary.capability.sig")
TOKEN = CAPABILITY.token
EVENT = "the app refused the run"
UNREACHED_ORIGIN = "https://localhost:1"
CLIENT_DEADLINE_SECONDS = 30


@pytest.fixture(scope="session")
def authority(tmp_path_factory: pytest.TempPathFactory) -> TestAuthority:
    return TestAuthority(tmp_path_factory.mktemp("reflected-failure-ca"))


@dataclass
class FormattedLog:
    logger: logging.Logger
    stream: io.StringIO

    def written_by(self, write: Callable[[], None]) -> str:
        before = self.stream.tell()
        write()
        return self.stream.getvalue()[before:]


@pytest.fixture
def private_log(caplog: pytest.LogCaptureFixture) -> Iterator[FormattedLog]:
    logger = logging.getLogger("reflected-failure-log-test")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    stream = io.StringIO()
    formatted = logging.StreamHandler(stream)
    formatted.setFormatter(DefaultFormatter("%(levelprefix)s %(message)s", use_colors=False))
    logger.addHandler(formatted)
    logger.addHandler(caplog.handler)
    yield FormattedLog(logger, stream)
    logger.removeHandler(formatted)
    logger.removeHandler(caplog.handler)


def closed_error_of(origin: str, context: ssl.SSLContext) -> UpstreamError:
    async def handshake_and_list() -> None:
        async with asyncio.timeout(CLIENT_DEADLINE_SECONDS):
            async with open_app_tools(origin, CAPABILITY, context) as session:
                await session.list_tools()

    with pytest.raises(UpstreamError) as raised:
        asyncio.run(handshake_and_list())
    return raised.value


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, answer: Callable[[httpx.Request], httpx.Response]) -> None:
        self._answer = answer
        self.authorizations: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.authorizations.append(request.headers["Authorization"])
        return self._answer(request)


@dataclass
class Upstream:
    authority: TestAuthority
    context: ssl.SSLContext
    monkeypatch: pytest.MonkeyPatch
    stack: ExitStack
    sent_authorizations: list[str] = field(default_factory=list)

    def over_tls(self, responder: Callable[[RecordedRequest], FakeResponse]) -> UpstreamError:
        credentials = self.authority.credentials_for(CERTIFICATE_HOST)
        fake: McpAppFake = self.stack.enter_context(serving(responder, credentials))
        error = closed_error_of(fake.origin, self.context)
        self.sent_authorizations += [request.headers["authorization"] for request in fake.requests]
        return error

    def in_memory(self, answer: Callable[[httpx.Request], httpx.Response]) -> UpstreamError:
        transport = RecordingTransport(answer)
        self.monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: transport)
        error = closed_error_of(UNREACHED_ORIGIN, self.context)
        self.sent_authorizations += transport.authorizations
        return error


@pytest.fixture
def upstream(authority: TestAuthority, monkeypatch: pytest.MonkeyPatch) -> Iterator[Upstream]:
    with ExitStack() as stack:
        yield Upstream(authority, authority.client_context(), monkeypatch, stack)


def answering_initialize(response: FakeResponse) -> ScriptedApp:
    return ScriptedApp(overrides={"initialize": lambda _request: response})


def raising_read_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadError(TOKEN, request=request)


REFLECTIONS: dict[str, Callable[[Upstream], UpstreamError]] = {
    "body": lambda upstream: upstream.over_tls(
        answering_initialize(FakeResponse(body=f"not json {TOKEN}".encode()))
    ),
    "content-encoding": lambda upstream: upstream.over_tls(
        answering_initialize(FakeResponse(headers={"Content-Encoding": TOKEN}))
    ),
    "reason-phrase": lambda upstream: upstream.in_memory(
        lambda _request: httpx.Response(401, extensions={"reason_phrase": TOKEN.encode()})
    ),
    "session-id": lambda upstream: upstream.over_tls(
        ScriptedApp(
            session_id=TOKEN,
            overrides={"tools/list": lambda _request: FakeResponse(status=401)},
        )
    ),
    "transport-exception": lambda upstream: upstream.in_memory(raising_read_error),
}


def logged_through_both_routes(log: FormattedLog, error: UpstreamError) -> dict[str, str]:
    def through_exception() -> None:
        try:
            raise error
        except UpstreamError:
            log.logger.exception(EVENT)

    return {
        "log_failure": log.written_by(lambda: log_failure(log.logger, EVENT, error)),
        "logger.exception": log.written_by(through_exception),
    }


@pytest.mark.parametrize("reflection", REFLECTIONS)
def test_the_formatted_log_of_a_reflected_failure_names_the_class_and_holds_no_capability(
    reflection, upstream, private_log
):
    error = REFLECTIONS[reflection](upstream)

    assert set(upstream.sent_authorizations) == {f"Bearer {TOKEN}"}
    for route, written in logged_through_both_routes(private_log, error).items():
        assert type(error).__name__ in written, route
        assert TOKEN not in written, route


def test_log_failure_of_a_closed_error_writes_one_error_line_of_event_class_and_positions(
    upstream, private_log, caplog
):
    error = REFLECTIONS["body"](upstream)

    log_failure(private_log.logger, EVENT, error)

    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    ((event, kind, _raiser),) = failure_lines(caplog)
    assert (event, kind) == (EVENT, type(error).__name__)
