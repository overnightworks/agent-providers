"""Shared arrangement for the agent service's suites: config, tokens, runners, a socket.

The service is built through ``create_app`` with the one injected seam the
verifier offers, its key-set fetch, so every test drives the real token
checks. Tokens minted here are signed by a throwaway RSA key whose key set the
fetch serves; the Keycloak suite uses the real issuer's tokens instead.
"""

from __future__ import annotations

import asyncio
import functools
import gc
import json
import logging
import re
import selectors
import socket
import threading
import time
import types
import uuid
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Coroutine,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, NoReturn

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from jwt.algorithms import RSAAlgorithm
from pydantic import ValidationError

import agent_service.signing as signing
from agent_client.run_stream import StreamViolation, ViolationKind
from agent_client.sse import RunStreamDecoder
from agent_client.wire import RejectionCode, RunEvent, RunRejection, RunRequest, RunStatus
from agent_providers.events import AssistantTextEvent, StreamEvent
from agent_service.app import create_app
from agent_service.config import RunConfig, ServiceConfig
from agent_service.identity import Principal
from agent_service.routing import Cli
from agent_service.run_events import run_frames
from agent_service.run_records import RunRecords
from agent_service.runners import AdmittedRun, Runners

TEST_ISSUER: Final = "https://keycloak.test/realms/agent-test"
TEST_JWKS_URL: Final = "https://keycloak.test/realms/agent-test/protocol/openid-connect/certs"
SONGMAKER_CLIENT: Final = "songmaker-test"
PRESENTATOR_CLIENT: Final = "presentator-test"
COWRITER: Final = "songmaker.cowriter"
PRESENTER: Final = "presentator.copresenter"
SUBSCRIPTION_GROUP: Final = "/ki-abo"
SONGMAKER_GROUP: Final = "/apps/songmaker"
CLI_PROFILE: Final = "claude-main"
COWRITER_ALIAS: Final = "cowriter"
SONGMAKER_ORIGIN: Final = "https://songmaker.internal"
RUN_ID_PLACEHOLDER: Final = "00000000-0000-4000-8000-000000000000"


def _service_config_document(issuer: str, runs: Mapping[str, object] | None) -> dict[str, Any]:
    return {
        **({} if runs is None else {"runs": runs}),
        "identity": {"issuer": issuer, "jwks_url": TEST_JWKS_URL},
        "profiles": {CLI_PROFILE: {"provider": "claude"}},
        "purposes": {
            COWRITER: {"cli_provider": "claude", "api_alias": COWRITER_ALIAS},
            PRESENTER: {"api_alias": "copresenter"},
        },
        "apps": {
            "songmaker": {
                "workload_client": SONGMAKER_CLIENT,
                "purposes": [COWRITER],
                "tools": {"read_song": "read_only", "edit_song": "side_effecting"},
            },
            "presentator": {"workload_client": PRESENTATOR_CLIENT, "purposes": [PRESENTER]},
        },
    }


def service_config(
    issuer: str = TEST_ISSUER, runs: Mapping[str, object] | None = None
) -> ServiceConfig:
    return ServiceConfig.model_validate(_service_config_document(issuer, runs))


def capable_service_config(
    directory: Path,
    *,
    issuer: str = TEST_ISSUER,
    songmaker: Mapping[str, object] | None = None,
) -> ServiceConfig:
    """``service_config`` with a signing key written under ``directory`` and songmaker's origin."""
    document = _service_config_document(issuer, None)
    document["signing"] = {"key_file": str(written_key_file(directory, generated_rsa_key()))}
    document["apps"]["songmaker"] = {
        **document["apps"]["songmaker"],
        "mcp_origin": SONGMAKER_ORIGIN,
        **(songmaker or {}),
    }
    return ServiceConfig.model_validate(document)


def run_body(**overrides: object) -> dict[str, Any]:
    return {
        "v": 1,
        "purpose": COWRITER,
        "turn": {"system": "You help write songs.", "messages": [{"role": "user", "text": "Hi"}]},
        **overrides,
    }


MOST_BUSY_TURNS_BEFORE_THE_RUN_NEVER_SETTLES: Final = 100_000


class _IdleSelector(selectors.DefaultSelector):
    """A selector that tells when the loop has nothing left to run and is about to block."""

    def __init__(self) -> None:
        super().__init__()
        self._idle_watchers: list[asyncio.Future[None]] = []
        self._busy_turns = 0

    def idle(self) -> asyncio.Future[None]:
        watcher = asyncio.get_running_loop().create_future()
        self._idle_watchers.append(watcher)
        return watcher

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        if not self._idle_watchers:
            return super().select(timeout)
        if timeout == 0:  # a ready callback keeps the loop busy
            self._busy_turns += 1
            if self._busy_turns <= MOST_BUSY_TURNS_BEFORE_THE_RUN_NEVER_SETTLES:
                return super().select(timeout)
            self._release_watchers(AssertionError("the run never settled"))
        else:
            self._release_watchers(None)
        return super().select(0)  # the watchers' owners are ready now, so do not block

    def _release_watchers(self, failure: AssertionError | None) -> None:
        watchers, self._idle_watchers = self._idle_watchers, []
        self._busy_turns = 0
        for watcher in watchers:
            if watcher.done():
                continue
            if failure is None:
                watcher.set_result(None)
            else:
                watcher.set_exception(failure)


class SteppedLoop(asyncio.SelectorEventLoop):
    """An event loop that can wait until every task is parked on a future."""

    def __init__(self) -> None:
        selector = _IdleSelector()
        super().__init__(selector)
        self.idle_selector = selector

    async def settled(self) -> None:
        await self.idle_selector.idle()


class StepClock:
    """A monotonic clock that moves only when a test advances it.

    It is also the run's ``RunClock``: ``wait_until`` parks the waiter until
    an advance reaches its deadline, and ``parked`` says which deadline the
    run waits for next, so a test steps from one deadline to the next without
    sleeping. It needs the ``SteppedLoop``.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.advance_to(self.now + seconds)

    def advance_to(self, instant: float) -> None:
        self.now = instant
        for deadline, waiter in sorted(self._waiters, key=lambda parked: parked[0]):
            if deadline <= instant and not waiter.done():
                waiter.set_result(None)

    async def wait_until(self, deadline: float) -> None:
        if deadline <= self.now:
            return
        parked = (deadline, asyncio.get_running_loop().create_future())
        self._waiters.append(parked)
        try:
            await parked[1]
        finally:
            self._waiters.remove(parked)

    async def parked(self) -> float | None:
        """The deadline the run waits for next, once every other task waits on a future."""
        loop = asyncio.get_running_loop()
        assert isinstance(loop, SteppedLoop), "run the scenario in a SteppedLoop"
        await loop.settled()
        return min((deadline for deadline, _ in self._waiters), default=None)


WALL_CLOCK_START: Final = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)


def instant(seconds_after_start: float) -> datetime:
    return WALL_CLOCK_START + timedelta(seconds=seconds_after_start)


class WallClock:
    """An aware-UTC wall clock that moves only when a test advances or steps it."""

    def __init__(self) -> None:
        self.current = WALL_CLOCK_START

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)

    def set_to(self, moment: datetime) -> None:
        self.current = moment


SHORT_TOOL_NAME_LENGTH = 35
LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT = 55


def tool_names_with_the_last_of_length(last_tool_name_length: int) -> list[str]:
    """128 tool names, sorted; the last is the one whose length tunes a capability to the limit."""
    names = [f"{index:03d}".ljust(SHORT_TOOL_NAME_LENGTH, "x") for index in range(127)]
    return [*names, "127".ljust(last_tool_name_length, "x")]


def _reaches(root: object, found: Callable[[object], bool]) -> bool:
    """Whether any object reachable from ``root`` satisfies ``found``.

    The walk follows ``gc.get_referents`` and skips types, modules and
    functions, whose reach is the program's and not the object's.
    """
    seen: set[int] = set()
    pending = [root]
    while pending:
        candidate = pending.pop()
        if id(candidate) in seen or isinstance(
            candidate, type | types.ModuleType | types.FunctionType | types.BuiltinFunctionType
        ):
            continue
        seen.add(id(candidate))
        if found(candidate):
            return True
        pending.extend(gc.get_referents(candidate))
    return False


def reaches_text(root: object, needles: Sequence[str]) -> bool:
    """Whether any text reachable from ``root`` contains a needle."""
    return _reaches(
        root,
        lambda candidate: (
            isinstance(candidate, str) and any(needle in candidate for needle in needles)
        ),
    )


def reaches_private_key(root: object) -> bool:
    """Whether any private-key object, which can reproduce its PEM, is reachable from ``root``."""
    return _reaches(root, lambda candidate: hasattr(candidate, "private_bytes"))


def record_signed_tokens(monkeypatch) -> list[str]:
    """Every token the signing key signs from now on, in order."""
    signed: list[str] = []
    sign = signing.SigningKey.sign

    def recording_sign(key, claims, token_type):
        signed.append(sign(key, claims, token_type))
        return signed[-1]

    monkeypatch.setattr(signing.SigningKey, "sign", recording_sign)
    return signed


class UnrelatedFailure(Exception):
    """Raised by a test only to leave a handler active around the call under test."""


CALLED_PLAINLY_OR_FROM_AN_ACTIVE_HANDLER = [
    pytest.param(False, id="plainly"),
    pytest.param(True, id="from-an-active-handler"),
]


def called_from(call: Callable[[], object], inside_active_handler: bool) -> object:
    if not inside_active_handler:
        return call()
    try:
        raise UnrelatedFailure
    except UnrelatedFailure:
        return call()


def everything_a_refusal_holds(refusal: BaseException) -> list[object]:
    """The refusal, its cause and context chain, their frames' locals and pydantic's contexts.

    The first frame of the refusal's own traceback is the caller that caught it, so it is left out.
    """
    held: list[object] = []
    pending: list[BaseException] = [refusal]
    seen: set[int] = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        held.append(error)
        pending.extend(link for link in (error.__cause__, error.__context__) if link)
        if isinstance(error, ValidationError):
            for problem in error.errors():
                held.append(problem)
                pending.extend(
                    value
                    for value in problem.get("ctx", {}).values()
                    if isinstance(value, BaseException)
                )
        traceback = error.__traceback__
        if error is refusal and traceback:
            traceback = traceback.tb_next
        while traceback:
            held.append(list(traceback.tb_frame.f_locals.values()))
            traceback = traceback.tb_next
    return held


class TokenMint:
    """Signs RS256 tokens with a throwaway key and serves that key's set, counting each fetch."""

    def __init__(self, key_id: str = "test-signing-key") -> None:
        self.key_id = key_id
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key_set_fetches = 0

    def key_set(self) -> dict[str, Any]:
        public = json.loads(RSAAlgorithm.to_jwk(self._key.public_key()))
        return {"keys": [{**public, "kid": self.key_id, "use": "sig", "alg": "RS256"}]}

    def fetch_key_set(self, _url: str) -> bytes:
        self.key_set_fetches += 1
        return json.dumps(self.key_set()).encode()

    def token(self, claims: Mapping[str, object]) -> str:
        return jwt.encode(dict(claims), self._key, algorithm="RS256", headers={"kid": self.key_id})

    def workload(self, client: str = SONGMAKER_CLIENT, **overrides: object) -> str:
        now = int(time.time())
        claims = {
            "iss": TEST_ISSUER,
            "aud": "agent-service",
            "sub": f"service-account-user-of-{client}",
            "azp": client,
            "client_id": client,
            "realm_access": {"roles": ["agent-workload", "default-roles-agent-test"]},
            "iat": now,
            "exp": now + 300,
        }
        return self.token(_merged(claims, overrides))

    def user(self, groups: object = (SUBSCRIPTION_GROUP,), **overrides: object) -> str:
        now = int(time.time())
        claims = {
            "iss": TEST_ISSUER,
            "aud": ["agent-service", "account"],
            "sub": "user-alice",
            "azp": SONGMAKER_CLIENT,
            "groups": groups,
            "realm_access": {"roles": ["default-roles-agent-test"]},
            "iat": now,
            "exp": now + 300,
        }
        return self.token(_merged(claims, overrides))


def _merged(claims: dict[str, object], overrides: Mapping[str, object]) -> dict[str, object]:
    """Overrides replace claims; an override of ``None`` removes the claim."""
    merged = {**claims, **overrides}
    return {name: value for name, value in merged.items() if value is not None}


@functools.cache
def generated_rsa_key(bits: int = 3072, exponent: int = 65537) -> rsa.RSAPrivateKey:
    """One throwaway key per shape for the whole session; no private key is ever committed."""
    return rsa.generate_private_key(public_exponent=exponent, key_size=bits)


def pem_of(private_key: Any) -> bytes:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def written_key_file(directory: Path, key: Any, mode: int = 0o600) -> Path:
    path = directory / "signing-key.pem"
    path.write_bytes(pem_of(key))
    path.chmod(mode)
    return path


def run_headers(workload_token: str | None, user_token: str | None) -> dict[str, str]:
    headers = {}
    if workload_token is not None:
        headers["Agent-Workload-Token"] = workload_token
    if user_token is not None:
        headers["Authorization"] = f"Bearer {user_token}"
    return headers


class ScriptedRunner:
    """A runner that yields one fixed list of core events and records what it was asked."""

    def __init__(self, events: Sequence[StreamEvent | Exception]) -> None:
        self._events = events
        self.admitted: list[AdmittedRun] = []

    async def run(self, admitted: AdmittedRun) -> AsyncGenerator[StreamEvent, None]:
        self.admitted.append(admitted)
        for event in self._events:
            if isinstance(event, Exception):
                raise event
            yield event


@contextmanager
def served(app: FastAPI) -> Iterator[str]:
    """Serve ``app`` with uvicorn on a real loopback socket; yield its base URL.

    The socket is bound and listening before the server thread starts, so a
    request made at once waits in the backlog instead of racing the startup.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "the test server did not stop"


def post_run(
    base_url: str,
    headers: Mapping[str, str],
    body: object = None,
    *,
    content: bytes | None = None,
) -> httpx.Response:
    payload = content if content is not None else json.dumps(run_body() if body is None else body)
    return httpx.post(
        f"{base_url}/runs",
        headers={"Content-Type": "application/json", **headers},
        content=payload,
        timeout=10,
    )


def get_run(base_url: str, headers: Mapping[str, str], segment: str) -> httpx.Response:
    return httpx.get(f"{base_url}/runs/{segment}", headers=dict(headers), timeout=10)


def answer_of(response: httpx.Response) -> tuple[int, bytes, dict[str, str]]:
    """What an answer says, for identity comparisons: its status, body and headers but ``date``."""
    headers = {name: value for name, value in response.headers.items() if name != "date"}
    return response.status_code, response.content, headers


def assert_rejected(response: httpx.Response, code: RejectionCode, status: int) -> None:
    assert response.status_code == status, response.text
    assert RunRejection.model_validate_json(response.content).code is code
    assert response.headers["cache-control"] == "no-store"
    if code is RejectionCode.UNAUTHORIZED:
        assert response.headers["www-authenticate"] == "Bearer"
    if code is RejectionCode.UNAVAILABLE:
        assert response.headers["retry-after"] == "30"
    if code is RejectionCode.BUSY:
        assert response.headers["retry-after"] == "10"


def decoded_events(raw: bytes) -> list[RunEvent]:
    """Every event of a whole stream, asserting the client's decoder finds no violation."""
    decoder = RunStreamDecoder()
    decoded = decoder.feed(raw) + decoder.close()
    events = [item for item in decoded if not isinstance(item, StreamViolation)]
    assert len(events) == len(decoded), f"the stream broke the contract: {decoded[len(events) :]}"
    return events


def with_placeholder_run_id(raw: bytes) -> bytes:
    """The stream's bytes with its random run id replaced, so frames compare as frozen text."""
    run_id = json.loads(raw.split(b"data: ", 1)[1].split(b"\n", 1)[0])["run_id"]
    uuid.UUID(run_id)
    return raw.replace(run_id.encode(), RUN_ID_PLACEHOLDER.encode())


class GatedRunner:
    """A runner that yields what a test releases, and records how it was stopped.

    ``log`` holds "closing" and "closed" as the runner's close goes through,
    so a test can order them against what the client took. ``hold_close``
    keeps the close open until ``release_close``.
    """

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[StreamEvent | Exception | None] = asyncio.Queue()
        self._close_gate: asyncio.Event | None = None
        self.close_error: Exception | None = None
        self.steps = 0
        self.cancellations = 0
        self.closes = 0
        self.log: list[str] = []

    def release(self, event: StreamEvent | Exception) -> None:
        self._inbox.put_nowait(event)

    def exhaust(self) -> None:
        self._inbox.put_nowait(None)

    def hold_close(self) -> None:
        self._close_gate = asyncio.Event()

    def release_close(self) -> None:
        if self._close_gate is not None:
            self._close_gate.set()

    async def run(self, admitted: AdmittedRun) -> AsyncGenerator[StreamEvent, None]:
        try:
            while True:
                self.steps += 1
                try:
                    event = await self._inbox.get()
                except asyncio.CancelledError:
                    self.cancellations += 1
                    raise
                if event is None:
                    return
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            await self._closed()

    async def _closed(self) -> None:
        self.closes += 1
        self.log.append("closing")
        if self._close_gate is not None:
            await self._close_gate.wait()
        if self.close_error is not None:
            raise self.close_error
        self.log.append("closed")


STRAY_CANCELLATION_CANARY: Final = "CANARY_OF_A_STRAY_CANCELLATION"


async def stray_cancellation(*, after_its_stop: bool) -> NoReturn:
    """Raise a ``CancelledError`` while no cancellation request is on the task.

    With ``after_its_stop`` it first waits until the task is cancelled and
    then withdraws that request, as a step that swallows its stop would.
    """
    if after_its_stop:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            current = asyncio.current_task()
            assert current is not None
            current.uncancel()
    raise asyncio.CancelledError(STRAY_CANCELLATION_CANARY)


class StrayCancelledRunner(GatedRunner):
    """A runner whose first step ends in a ``stray_cancellation``, then closes as a gated one."""

    def __init__(self, *, after_its_stop: bool) -> None:
        super().__init__()
        self._after_its_stop = after_its_stop

    async def run(self, admitted: AdmittedRun) -> AsyncGenerator[StreamEvent, None]:
        try:
            self.steps += 1
            await stray_cancellation(after_its_stop=self._after_its_stop)
            yield QUIET
        finally:
            await self._closed()


SONGMAKER = service_config().apps["songmaker"]
ALICE = Principal(TEST_ISSUER, "user-alice")


def alice_admitted_run(run_id: uuid.UUID) -> AdmittedRun:
    return AdmittedRun(
        run_id=run_id,
        app="songmaker",
        principal=ALICE,
        request=RunRequest.model_validate(run_body()),
        route=Cli("claude-main"),
        capability=None,
    )


QUIET = AssistantTextEvent(text="")


FAILURE_LINE = re.compile(r"(?P<event>.+?): (?P<kind>\w+) at (?P<positions>.+)")
POSITION = re.compile(r"[\w.]+\.py:\d+:[\w<>]+")


def failure_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str, str]]:
    """Each ERROR line as its event, its class and the function that raised.

    Every line must be the event, the class and well-formed stack positions
    alone, with nothing attached to the record.
    """
    lines = []
    for record in caplog.records:
        if record.levelno < logging.ERROR:
            continue
        assert record.exc_info is None
        line = FAILURE_LINE.fullmatch(record.getMessage())
        assert line is not None, record.getMessage()
        positions = line["positions"].split(", ")
        assert all(POSITION.fullmatch(position) for position in positions), positions
        lines.append((line["event"], line["kind"], positions[-1].rsplit(":", 1)[1]))
    return lines


def in_loop(scenario: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    @functools.wraps(scenario)
    def run(*args: Any, **kwargs: Any) -> None:
        with asyncio.Runner(loop_factory=SteppedLoop) as runner:
            runner.run(scenario(*args, **kwargs))

    return run


class BrokenClock(StepClock):
    """A clock whose wait fails, which makes the run's supervision fail.

    Each wait raises a new failure from ``breaks_with``.
    """

    def __init__(
        self, breaks_with: Callable[[], BaseException] = lambda: RuntimeError("the clock broke")
    ) -> None:
        super().__init__()
        self._breaks_with = breaks_with

    async def wait_until(self, deadline: float) -> None:
        raise self._breaks_with()


class ClientSocket:
    """The client end of one ASGI response: the request it posts, what it takes, when it leaves.

    ``send`` blocks on the second body chunk until ``released`` is set, as
    uvicorn's drain does, or raises ``second_chunk_raises`` there;
    ``blocks_the_first_chunk`` blocks the first one forever. With
    ``suspends_the_start`` the start message never completes. ``receive`` answers
    the request first and ``http.disconnect`` once the client ``leave``s.
    """

    def __init__(
        self,
        *,
        spec_version: str = "2.3",
        blocks_the_first_chunk: bool = False,
        blocks_the_second_chunk: bool = False,
        second_chunk_raises: Exception | None = None,
        suspends_the_start: bool = False,
        leaves_at_once: bool = False,
    ) -> None:
        self.scope = _run_scope(spec_version)
        self.messages: list[dict[str, Any]] = []
        self.released = asyncio.Event()
        self.answering = asyncio.Event()
        self._blocks_the_first_chunk = blocks_the_first_chunk
        self._blocks_the_second_chunk = blocks_the_second_chunk
        self._second_chunk_raises = second_chunk_raises
        self._suspends_the_start = suspends_the_start
        self._left = asyncio.Event()
        if leaves_at_once:
            self.leave()
        self._request: list[dict[str, Any]] = [
            {"type": "http.request", "body": json.dumps(run_body()).encode(), "more_body": False}
        ]

    def leave(self) -> None:
        self._left.set()

    async def receive(self) -> dict[str, Any]:
        if self._request:
            return self._request.pop()
        await self._left.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict[str, Any]) -> None:
        self.answering.set()
        if message["type"] == "http.response.start" and self._suspends_the_start:
            self._suspends_the_start = False
            await asyncio.Event().wait()
        if message["type"] == "http.response.body" and self._bodies() == 0:
            if self._blocks_the_first_chunk:
                await asyncio.Event().wait()
        if message["type"] == "http.response.body" and self._bodies() == 1:
            if self._second_chunk_raises is not None:
                raise self._second_chunk_raises
            if self._blocks_the_second_chunk:
                await self.released.wait()
        self.messages.append(message)

    def _bodies(self) -> int:
        return sum(m["type"] == "http.response.body" for m in self.messages)

    @property
    def body(self) -> bytes:
        return b"".join(m["body"] for m in self.messages if m["type"] == "http.response.body")

    @property
    def run_id(self) -> uuid.UUID:
        return uuid.UUID(json.loads(self.body.split(b"data: ", 1)[1].split(b"\n", 1)[0])["run_id"])


@functools.cache
def shared_mint() -> TokenMint:
    return TokenMint()


def _run_scope(spec_version: str) -> dict[str, Any]:
    mint = shared_mint()
    headers = run_headers(mint.workload(), mint.user())
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/runs",
        "raw_path": b"/runs",
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()]
        + [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }


async def respond_through_the_app(
    drive: Drive,
    client: ClientSocket,
    monkeypatch: pytest.MonkeyPatch,
    runners: Runners | None = None,
) -> asyncio.Task[None]:
    """Post a run to the real app on the drive's runner, clock and store; its response task.

    ``runners`` replace the drive's runner, as ``Runners()`` serves a run without one.

    The token checks leave the loop for a worker thread, so this returns only
    once the app has begun to answer: from then on the run waits on the clock alone.
    """
    monkeypatch.setattr("agent_service.app.LoopClock", lambda: drive.clock)
    app = create_app(
        service_config(runs=drive.runs.model_dump()),
        runners=runners or Runners(cli=drive.runner),
        records=drive.records,
        fetch_key_set=shared_mint().fetch_key_set,
    )
    response = asyncio.create_task(app(client.scope, client.receive, client.send))
    answering = asyncio.create_task(client.answering.wait())
    await asyncio.wait({response, answering}, return_when=asyncio.FIRST_COMPLETED)
    answering.cancel()
    await asyncio.gather(answering, return_exceptions=True)
    return response


class Drive:
    """One run read by a client task that a test can stall, on a clock the test moves."""

    def __init__(
        self,
        runner: GatedRunner,
        runs: RunConfig,
        clock: StepClock,
        *,
        read: bool = True,
        records: RunRecords | None = None,
    ) -> None:
        self.runner = runner
        self.runs = runs
        self.clock = clock
        self.records = records or RunRecords(lambda: instant(clock.now))
        self.run_id = uuid.uuid4()
        self.frames = run_frames(
            alice_admitted_run(self.run_id), runner, SONGMAKER, runs, clock, self.records
        )
        self.events: list[tuple[float, RunEvent]] = []
        self.frame_sizes: list[int] = []
        self._decoder = RunStreamDecoder()
        self._may_read = asyncio.Event()
        self._may_read.set()
        self.reader = asyncio.create_task(self._read()) if read else None

    async def _read(self) -> None:
        while True:
            await self._may_read.wait()
            try:
                frame = await anext(self.frames)
            except StopAsyncIteration:
                return
            self.took(frame)

    def took(self, frame: bytes) -> None:
        self.frame_sizes.append(len(frame))
        for item in self._decoder.feed(frame):
            assert not isinstance(item, StreamViolation), item
            self.events.append((self.clock.now, item))
            self.runner.log.append(f"taken:{item.type}")

    async def drain(self) -> None:
        """Take every frame the stream still holds, until it ends."""
        async for frame in self.frames:
            self.took(frame)

    def end_of_stream(self) -> list[ViolationKind]:
        """What the client's decoder reports about the stream's end: nothing for a clean one."""
        return [violation.kind for violation in self._decoder.close()]

    def status(self, run_id: uuid.UUID | None = None) -> RunStatus | None:
        """Alice's read of a run of the songmaker app: this drive's, unless another is named."""
        return self.records.read(run_id or self.run_id, "songmaker", ALICE)

    def stall(self) -> None:
        self._may_read.clear()

    def resume(self) -> None:
        self._may_read.set()

    @property
    def types(self) -> list[str]:
        return [event.type for _, event in self.events]

    @property
    def seqs(self) -> list[int]:
        return [event.seq for _, event in self.events]

    @property
    def terminal(self) -> tuple[float, RunEvent]:
        return self.events[-1]

    async def run_to(self, instant: float) -> None:
        while (deadline := await self.clock.parked()) is not None and deadline <= instant:
            self.clock.advance_to(deadline)
        self.clock.advance_to(instant)
        await self.clock.parked()

    async def run_to_end(self) -> list[float]:
        """Step from deadline to deadline until the run is over; the deadlines it waited for."""
        deadlines = []
        while (deadline := await self.clock.parked()) is not None:
            deadlines.append(deadline)
            self.clock.advance_to(deadline)
        return deadlines

    async def speak_quietly_every(self, seconds: int, until: int) -> None:
        for instant in range(seconds, until + 1, seconds):
            await self.run_to(instant)
            self.runner.release(QUIET)

    async def finished(self) -> None:
        assert self.reader is not None
        await self.reader
        self.assert_nothing_left()

    def assert_nothing_left(self) -> None:
        assert asyncio.all_tasks() == {asyncio.current_task()}
        assert self.runner.closes == 1


@asynccontextmanager
async def driven(
    runner: GatedRunner | None = None,
    runs: RunConfig | None = None,
    clock: StepClock | None = None,
    *,
    read: bool = True,
    records: RunRecords | None = None,
) -> AsyncIterator[Drive]:
    drive = Drive(
        runner or GatedRunner(),
        runs or RunConfig(),
        clock or StepClock(),
        read=read,
        records=records,
    )
    try:
        yield drive
    finally:
        drive.runner.release_close()
        if drive.reader is not None:
            drive.reader.cancel()
        await asyncio.gather(*([drive.reader] if drive.reader else []), return_exceptions=True)
        await drive.frames.aclose()
