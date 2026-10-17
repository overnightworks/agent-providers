"""H7 admission: one subscription run at a time per service, the slot a lease holds.

A busy CLI route answers ``busy`` with ``Retry-After`` before anything is signed; refusals
and API runs take nothing; the lease is released when the run is over or the response ends,
and a late release by an old response never frees a newer run's slot.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

import httpx
import pytest
from service_support import (
    PRESENTER,
    SONGMAKER,
    SONGMAKER_GROUP,
    ClientSocket,
    ScriptedRunner,
    TokenMint,
    alice_admitted_run,
    assert_rejected,
    capable_service_config,
    decoded_events,
    post_run,
    record_signed_tokens,
    run_body,
    run_headers,
    served,
    service_config,
    shared_mint,
)
from starlette.requests import ClientDisconnect
from test_service_capability import QUOTATION_MARKS_256, TUNED_AT_THE_LIMIT
from webauth.jwt_verification import KeySetFetchError

from agent_client.wire import RejectionCode, RunStarted
from agent_providers.events import AssistantTextEvent, FinalEvent
from agent_service.admission import AdmissionSlot
from agent_service.app import create_app
from agent_service.config import RunConfig
from agent_service.run_events import run_frames
from agent_service.run_liveness import LoopClock
from agent_service.run_records import RunRecords
from agent_service.runners import Runner, Runners

WAIT_LIMIT_S: Final = 10
MINT = TokenMint()
FINAL = (FinalEvent(text="done"),)
BOB_ON_THE_API_ROUTE = {"groups": [SONGMAKER_GROUP], "sub": "user-bob"}


class HeldRunner:
    """A CLI runner whose run stays open until a test lets it go, for a bounded time."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.proceed = threading.Event()

    async def run(self, admitted):
        self.entered.set()
        await asyncio.to_thread(self.proceed.wait, WAIT_LIMIT_S)
        yield FinalEvent(text="done")


@dataclass
class _Service:
    base_url: str
    held: HeldRunner
    api: ScriptedRunner
    signed: list[str]
    pool: ThreadPoolExecutor

    def run(self, user: str | None = None, *, workload: str | None = None, **body: object):
        headers = run_headers(workload or MINT.workload(), user or MINT.user())
        return post_run(self.base_url, headers, run_body(**body))

    def begin_held_cli_run(self) -> Future[httpx.Response]:
        self.held.entered.clear()
        run = self.pool.submit(self.run)
        assert self.held.entered.wait(WAIT_LIMIT_S), "the CLI run was not admitted"
        return run

    def let_go(self, run: Future[httpx.Response]) -> httpx.Response:
        self.held.proceed.set()
        return run.result(WAIT_LIMIT_S)


@pytest.fixture
def service(tmp_path, monkeypatch) -> Iterator[_Service]:
    held = HeldRunner()
    api = ScriptedRunner(FINAL)
    signed = record_signed_tokens(monkeypatch)
    config = capable_service_config(tmp_path, songmaker=TUNED_AT_THE_LIMIT)
    app = create_app(config, runners=Runners(cli=held, api=api), fetch_key_set=MINT.fetch_key_set)
    with served(app) as base_url, ThreadPoolExecutor() as pool:
        try:
            yield _Service(base_url, held, api, signed, pool)
        finally:
            held.proceed.set()


def _assert_streamed_to_a_terminal(response: httpx.Response) -> None:
    assert response.status_code == 200
    assert isinstance(decoded_events(response.content)[0], RunStarted)


def test_a_free_cli_route_is_admitted_and_holds_the_lease(service):
    first = service.begin_held_cli_run()

    assert_rejected(service.run(), RejectionCode.BUSY, 429)

    _assert_streamed_to_a_terminal(service.let_go(first))


def test_a_busy_cli_route_answers_busy_before_a_run_id_or_a_signature(service, monkeypatch):
    run_ids: list[uuid.UUID] = []
    mint_run_id = uuid.uuid4

    def counting_run_id() -> uuid.UUID:
        run_ids.append(mint_run_id())
        return run_ids[-1]

    monkeypatch.setattr("agent_service.app.uuid.uuid4", counting_run_id)
    first = service.begin_held_cli_run()
    run_ids_before, signed_before = len(run_ids), len(service.signed)

    busy = service.run()

    assert_rejected(busy, RejectionCode.BUSY, 429)
    assert (len(run_ids), len(service.signed)) == (run_ids_before, signed_before)
    service.let_go(first)


@pytest.mark.parametrize(
    ("user", "body", "code"),
    [
        pytest.param(None, {"purpose": PRESENTER}, RejectionCode.FORBIDDEN, id="purpose-refused"),
        pytest.param(MINT.user(groups=()), {}, RejectionCode.NOT_ENABLED, id="route-not-enabled"),
    ],
)
def test_a_refusal_before_the_slot_takes_nothing(service, user, body, code):
    assert_rejected(service.run(user, **body), code, 403)

    _assert_streamed_to_a_terminal(service.let_go(service.begin_held_cli_run()))


def test_an_api_run_while_the_cli_is_busy_is_admitted_and_takes_and_frees_nothing(service):
    first = service.begin_held_cli_run()

    api_run = service.run(MINT.user(**BOB_ON_THE_API_ROUTE))

    _assert_streamed_to_a_terminal(api_run)
    assert len(service.api.admitted) == 1
    assert_rejected(service.run(), RejectionCode.BUSY, 429)
    service.let_go(first)


def test_a_capability_too_large_after_the_take_releases_the_lease(service):
    assert_rejected(
        service.run(MINT.user(sub=QUOTATION_MARKS_256)), RejectionCode.CAPABILITY_TOO_LARGE, 422
    )

    _assert_streamed_to_a_terminal(service.let_go(service.begin_held_cli_run()))


@pytest.mark.parametrize("failing_step", ["AdmittedRun", "run_frames", "_RunResponse"])
def test_a_failure_before_the_response_exists_releases_the_lease(
    service, monkeypatch, failing_step
):
    def broken(*args, **kwargs):
        raise RuntimeError("the hand-over broke")

    with monkeypatch.context() as broken_hand_over:
        broken_hand_over.setattr(f"agent_service.app.{failing_step}", broken)
        assert service.run().status_code == 500

    _assert_streamed_to_a_terminal(service.let_go(service.begin_held_cli_run()))


def test_two_concurrent_cli_requests_give_one_busy_answer_with_the_header_on_the_wire(service):
    requests = [service.pool.submit(service.run) for _ in range(2)]
    refused, _ = wait(requests, WAIT_LIMIT_S, return_when=FIRST_COMPLETED)

    (busy,) = (request.result() for request in refused)
    assert_rejected(busy, RejectionCode.BUSY, 429)
    (admitted,) = set(requests) - refused
    _assert_streamed_to_a_terminal(service.let_go(admitted))


def test_unavailable_keeps_its_own_retry_after():
    def unreachable_issuer(_url: str) -> bytes:
        raise KeySetFetchError("the issuer cannot be reached")

    app = create_app(
        service_config(), runners=Runners(cli=HeldRunner()), fetch_key_set=unreachable_issuer
    )
    with served(app) as base_url:
        unavailable = post_run(base_url, run_headers(MINT.workload(), MINT.user()))

    assert_rejected(unavailable, RejectionCode.UNAVAILABLE, 503)
    assert unavailable.headers["retry-after"] == "30"


class _FailingRecorderOver:
    """Makes the record's end fail, as a recorder failure in ``run_over`` does."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def __call__(self) -> None:
        def over(recorder, fallback=None):
            raise RuntimeError("the recorder broke")

        self._monkeypatch.setattr("agent_service.run_records.RunRecorder.over", over)


def _consumed(runner: Runner | None, lease, *, recorder_fails: bool = False) -> None:
    tolerated = (RuntimeError,) if recorder_fails else ()

    async def collected() -> None:
        frames = run_frames(
            alice_admitted_run(uuid.uuid4()),
            runner,
            SONGMAKER,
            RunConfig(),
            LoopClock(),
            RunRecords(),
            lease,
        )
        with suppress(*tolerated):
            [frame async for frame in frames]

    asyncio.run(collected())


@pytest.mark.parametrize("recorder_fails", [False, True], ids=["ends-normally", "recorder-fails"])
@pytest.mark.parametrize(
    "runner", [ScriptedRunner(FINAL), None], ids=["with-a-runner", "no-runner"]
)
def test_a_run_that_is_over_releases_its_lease_even_when_its_record_fails(
    monkeypatch, runner, recorder_fails
):
    if recorder_fails:
        _FailingRecorderOver(monkeypatch)()
    slot = AdmissionSlot()
    lease = slot.try_take()

    _consumed(runner, lease, recorder_fails=recorder_fails)

    assert slot.try_take() is not None


def test_a_late_release_by_an_old_run_leaves_the_lease_of_a_newer_run_held():
    slot = AdmissionSlot()
    old_lease = slot.try_take()
    _consumed(ScriptedRunner(FINAL), old_lease)
    newer_lease = slot.try_take()
    assert newer_lease is not None

    old_lease.release()

    assert slot.try_take() is None
    newer_lease.release()
    assert slot.try_take() is not None


def test_a_response_closed_before_its_first_step_releases_the_lease():
    async def scenario() -> int:
        app = create_app(
            service_config(),
            runners=Runners(cli=ScriptedRunner(FINAL)),
            fetch_key_set=shared_mint().fetch_key_set,
        )
        abandoned = ClientSocket(suspends_the_start=True, leaves_at_once=True)
        await app(abandoned.scope, abandoned.receive, abandoned.send)
        next_run = ClientSocket()
        await app(next_run.scope, next_run.receive, next_run.send)
        return next_run.messages[0]["status"]

    assert asyncio.run(scenario()) == 200


STALLING_RUNS = {
    "heartbeat_interval_s": 1,
    "stall_timeout_s": 1.5,
    "idle_timeout_s": 3.0,
    "max_duration_s": 8.0,
}


class GatedRunner:
    """A CLI runner whose first run never ends and whose close waits on a gate.

    Later runs hold until ``later_runs_proceed`` is set.
    """

    def __init__(self) -> None:
        self.runs = 0
        self.entered = asyncio.Event()
        self.closing = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_gate = asyncio.Event()
        self.later_runs_proceed = asyncio.Event()

    async def run(self, admitted):
        self.runs += 1
        first = self.runs == 1
        try:
            self.entered.set()
            yield AssistantTextEvent(text="streaming")
            await (asyncio.Event() if first else self.later_runs_proceed).wait()
            yield FinalEvent(text="done")
        finally:
            if first:
                self.closing.set()
                await self.close_gate.wait()
                self.closed.set()


async def _bounded(awaitable):
    return await asyncio.wait_for(awaitable, WAIT_LIMIT_S)


class _Asking:
    """One more CLI request over the ASGI app, answered or still streaming."""

    def __init__(self, app, spec_version: str = "2.3") -> None:
        self.client = ClientSocket(spec_version=spec_version)
        self.response = asyncio.create_task(
            app(self.client.scope, self.client.receive, self.client.send)
        )

    async def status(self) -> int:
        await _bounded(self.client.answering.wait())
        return self.client.messages[0]["status"]

    async def finished(self, runner: GatedRunner) -> None:
        runner.later_runs_proceed.set()
        self.client.leave()
        await _bounded(self.response)


async def _asked_until_admitted(app, attempts: int = 100) -> _Asking:
    for _ in range(attempts):
        asking = _Asking(app)
        if await asking.status() == 200:
            return asking
    raise AssertionError("the slot was never freed")


def _gated_app(runs: dict[str, object] | None = None):
    runner = GatedRunner()
    app = create_app(
        service_config(runs=runs),
        runners=Runners(cli=runner),
        fetch_key_set=shared_mint().fetch_key_set,
    )
    return runner, app


@pytest.mark.parametrize(
    "gone",
    [
        pytest.param({"spec_version": "2.3"}, id="spec-2.3-client-leaves"),
        pytest.param(
            {"spec_version": "2.4", "second_chunk_raises": OSError("peer gone")},
            id="spec-2.4-send-fails",
        ),
    ],
)
def test_the_slot_stays_taken_until_the_stopped_run_has_been_closed(gone):
    async def scenario() -> tuple[int, int]:
        runner, app = _gated_app()
        departing = ClientSocket(**gone)
        old_response = asyncio.create_task(app(departing.scope, departing.receive, departing.send))
        await _bounded(runner.entered.wait())
        if gone["spec_version"] == "2.3":
            await _bounded(departing.answering.wait())
            departing.leave()
        await _bounded(runner.closing.wait())

        during_the_close = await _Asking(app).status()
        runner.close_gate.set()
        with suppress(ClientDisconnect):
            await _bounded(old_response)
        after_the_close = _Asking(app)
        after_status = await after_the_close.status()
        await after_the_close.finished(runner)
        return during_the_close, after_status

    assert asyncio.run(scenario()) == (429, 200)


def test_the_next_run_is_admitted_while_the_old_connection_hangs_and_the_old_response_ends_late():
    async def scenario() -> tuple[bool, int, int]:
        runner, app = _gated_app(STALLING_RUNS)
        runner.close_gate.set()
        hanging = ClientSocket(blocks_the_second_chunk=True)
        old_response = asyncio.create_task(app(hanging.scope, hanging.receive, hanging.send))
        await _bounded(runner.closed.wait())

        successor = await _asked_until_admitted(app)
        old_still_hanging = not old_response.done()
        hanging.released.set()
        hanging.leave()
        await _bounded(old_response)
        while_successor_runs = await _Asking(app).status()
        await successor.finished(runner)
        after_successor = _Asking(app)
        await after_successor.finished(runner)
        return old_still_hanging, while_successor_runs, await after_successor.status()

    assert asyncio.run(scenario()) == (True, 429, 200)
