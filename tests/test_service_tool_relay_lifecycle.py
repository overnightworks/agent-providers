import asyncio
import gc
import logging
import signal
from collections.abc import Awaitable, Callable
from http import HTTPStatus

import pytest
from bounds import TEST_WAIT_SECONDS
from relay_support import (
    RUN_ID,
    AbsorbingOwner,
    Delivery,
    HoldingSteppedLoop,
    RawClient,
    RequestHead,
    ScriptedOwner,
    SuppressingOwner,
    blocking_connect,
    chunked_head,
    close_bounded,
    connection_numbers,
    drive_stepped,
    parked_at,
    port_of,
    post,
    record_task_names,
    relay_tasks,
    retained_relay_tasks,
    yield_until,
)
from service_support import StepClock

from agent_client.mcp_wire import JSON_MEDIA_TYPE, HttpAnswer
from agent_service.tool_relay import (
    BODY_RECEIVE_SECONDS,
    HEAD_RECEIVE_SECONDS,
    MAX_RELAY_CONNECTIONS,
    RELAY_CLOSE_SECONDS,
    Relay,
    RelayCleanupIncomplete,
)

SIGNALS = (signal.SIGINT, signal.SIGTERM)
JUST_BEFORE_SECONDS = 0.001
SHARES_OF_A_WINDOW = 4
ANSWER_BEYOND_THE_SOCKET_BUFFERS = HttpAnswer(HTTPStatus.OK, JSON_MEDIA_TYPE, b"x" * 16_777_216)
CLOSE_TASK = f"relay:{RUN_ID}:close"

Scenario = Callable[[Relay, StepClock], Awaitable[None]]
Opening = Callable[[Relay], bytes]


def partial_head(relay: Relay) -> bytes:
    return b"POST /mcp HTTP/1.1\r\nHost:"


def partial_length_body(relay: Relay) -> bytes:
    return RequestHead.standard(relay, 100).encode() + b"abc"


def partial_chunked_body(relay: Relay) -> bytes:
    return chunked_head(relay).encode() + b"64\r\nabc"


def handlers() -> dict[signal.Signals, object]:
    return {sig: signal.getsignal(sig) for sig in SIGNALS}


def relay_log_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("agent_service")]


def unretrieved_task_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "never retrieved" in r.getMessage()]


async def answered(client: RawClient, relay: Relay) -> None:
    await client.send(post(relay, b"{}"))
    assert (await client.answer()).status == HTTPStatus.OK


async def connect_idle(relay: Relay, count: int) -> list[RawClient]:
    clients = [await RawClient.connect(port_of(relay)) for _ in range(count)]
    await yield_until(lambda: len(relay_tasks()) == count)
    return clients


async def start_gated_request(relay: Relay, owner: ScriptedOwner) -> RawClient:
    client = await RawClient.connect(port_of(relay))
    await client.send(post(relay, b"{}"))
    await owner.wait_entered()
    return client


async def exhaust_close_budget(relay: Relay, clock: StepClock) -> RelayCleanupIncomplete:
    closing = asyncio.create_task(relay.close())
    deadline = clock.now + RELAY_CLOSE_SECONDS
    await parked_at(clock, deadline)
    clock.advance_to(deadline - JUST_BEFORE_SECONDS)
    await parked_at(clock, deadline)
    assert not closing.done()
    clock.advance_to(deadline)
    with pytest.raises(RelayCleanupIncomplete) as outcome:
        await closing
    return outcome.value


async def trickle_until_aborted(
    client: RawClient, clock: StepClock, opening: bytes, window: float
) -> None:
    await client.send(opening)
    deadline = clock.now + window
    await parked_at(clock, deadline)
    for _ in range(SHARES_OF_A_WINDOW - 1):
        clock.advance(window / SHARES_OF_A_WINDOW)
        await client.send(b"a")
        await parked_at(clock, deadline)
    clock.advance(window / SHARES_OF_A_WINDOW)
    assert await client.delivery() is Delivery.ABSENT


def test_the_connection_over_the_cap_is_aborted_and_the_open_ones_still_answer() -> None:  # L1
    owner = ScriptedOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        clients = await connect_idle(relay, MAX_RELAY_CONNECTIONS)
        extra = await RawClient.connect(port_of(relay))
        assert await extra.delivery() is Delivery.ABSENT
        for client in clients:
            await answered(client, relay)
        assert owner.bodies == [b"{}"] * MAX_RELAY_CONNECTIONS

    drive_stepped(scenario, owner)


def test_a_slot_freed_by_a_leaving_client_serves_the_next_connection() -> None:  # L2
    async def scenario(relay: Relay, clock: StepClock) -> None:
        clients = await connect_idle(relay, MAX_RELAY_CONNECTIONS)
        clients[0].disconnect()
        await yield_until(lambda: len(relay_tasks()) == MAX_RELAY_CONNECTIONS - 1)
        await answered(await RawClient.connect(port_of(relay)), relay)

    drive_stepped(scenario)


async def idle_after_an_answer(client: RawClient, relay: Relay) -> None:
    await answered(client, relay)


async def idle_without_a_byte(client: RawClient, relay: Relay) -> None:
    return None


@pytest.mark.parametrize(
    "idle",
    [pytest.param(idle_after_an_answer, id="L3"), pytest.param(idle_without_a_byte, id="L4")],
)
def test_an_idle_connection_has_no_deadline_and_is_answered_later(
    idle: Callable[[RawClient, Relay], Awaitable[None]],
) -> None:
    async def scenario(relay: Relay, clock: StepClock) -> None:
        client = await RawClient.connect(port_of(relay))
        await idle(client, relay)
        await parked_at(clock, None)
        clock.advance(HEAD_RECEIVE_SECONDS + BODY_RECEIVE_SECONDS + 1)
        await parked_at(clock, None)
        await answered(client, relay)

    drive_stepped(scenario)


async def fresh_connection(relay: Relay, clock: StepClock) -> RawClient:
    return await RawClient.connect(port_of(relay))


async def kept_alive_connection(relay: Relay, clock: StepClock) -> RawClient:
    client = await RawClient.connect(port_of(relay))
    await answered(client, relay)
    clock.advance(HEAD_RECEIVE_SECONDS / 2)
    return client


@pytest.mark.parametrize(
    ("connection", "opening", "window"),
    [
        pytest.param(fresh_connection, partial_head, HEAD_RECEIVE_SECONDS, id="L5"),
        pytest.param(kept_alive_connection, partial_head, HEAD_RECEIVE_SECONDS, id="L6"),
        pytest.param(fresh_connection, partial_length_body, BODY_RECEIVE_SECONDS, id="L9"),
    ],
)
def test_a_stalled_request_is_aborted_at_its_deadline_without_the_owner_and_frees_its_slot(
    connection: Callable[[Relay, StepClock], Awaitable[RawClient]], opening: Opening, window: float
) -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        fillers = await connect_idle(relay, MAX_RELAY_CONNECTIONS - 1)
        client = await connection(relay, clock)
        calls_before = len(owner.requests)
        await client.send(opening(relay))
        deadline = clock.now + window
        await parked_at(clock, deadline)
        clock.advance_to(deadline - JUST_BEFORE_SECONDS)
        await parked_at(clock, deadline)
        assert not client.has_ended()
        clock.advance_to(deadline)
        assert await client.delivery() is Delivery.ABSENT
        assert len(owner.requests) == calls_before
        await yield_until(lambda: len(relay_tasks()) == MAX_RELAY_CONNECTIONS - 1)
        await answered(await RawClient.connect(port_of(relay)), relay)
        assert not any(filler.has_ended() for filler in fillers)

    drive_stepped(scenario, owner)


async def next_head_in_the_requests_segment(
    client: RawClient, relay: Relay, owner: ScriptedOwner
) -> None:
    await client.send(post(relay, b"{}") + partial_head(relay))
    await owner.wait_entered()


async def next_head_after_the_request_entered_the_owner(
    client: RawClient, relay: Relay, owner: ScriptedOwner
) -> None:
    await client.send(post(relay, b"{}"))
    await owner.wait_entered()
    await client.send(partial_head(relay))


@pytest.mark.parametrize(
    "next_head",
    [
        pytest.param(next_head_in_the_requests_segment, id="L7"),
        pytest.param(next_head_after_the_request_entered_the_owner, id="L7-stream-buffered"),
    ],
)
def test_a_partial_next_head_behind_a_gated_request_is_timed_from_the_answer(
    next_head: Callable[[RawClient, Relay, ScriptedOwner], Awaitable[None]],
) -> None:
    owner = ScriptedOwner(gated=True)

    async def scenario(relay: Relay, clock: StepClock) -> None:
        client = await RawClient.connect(port_of(relay))
        await next_head(client, relay, owner)
        await parked_at(clock, None)
        clock.advance(HEAD_RECEIVE_SECONDS * 2)
        await parked_at(clock, None)
        assert not client.has_ended()
        owner.release()
        assert (await client.answer()).status == HTTPStatus.OK
        await parked_at(clock, clock.now + HEAD_RECEIVE_SECONDS)
        clock.advance(HEAD_RECEIVE_SECONDS)
        assert await client.delivery() is Delivery.ABSENT

    drive_stepped(scenario, owner)


@pytest.mark.parametrize(
    ("opening", "window"),
    [
        pytest.param(partial_head, HEAD_RECEIVE_SECONDS, id="L8"),
        pytest.param(partial_length_body, BODY_RECEIVE_SECONDS, id="L10"),
        pytest.param(partial_chunked_body, BODY_RECEIVE_SECONDS, id="L11"),
    ],
)
def test_a_trickled_request_is_aborted_at_the_absolute_deadline_without_the_owner(
    opening: Opening, window: float
) -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        client = await RawClient.connect(port_of(relay))
        await trickle_until_aborted(client, clock, opening(relay), window)
        assert owner.requests == []

    drive_stepped(scenario, owner)


@pytest.mark.parametrize(
    "opening",
    [
        pytest.param(partial_head, id="head"),
        pytest.param(partial_length_body, id="length-body"),
        pytest.param(partial_chunked_body, id="chunked-body"),
    ],
)
def test_a_client_that_stops_sending_mid_request_is_dropped_unanswered(opening: Opening) -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(opening(relay))
        client.finish_sending()
        assert await client.delivery() is Delivery.ABSENT
        assert owner.requests == []

    drive_stepped(scenario, owner)


def head_split_before_its_last_bytes(relay: Relay) -> tuple[bytes, bytes]:
    request = post(relay, b"{}")
    return request[:10], request[10:]


def body_split_before_its_last_bytes(relay: Relay) -> tuple[bytes, bytes]:
    return partial_length_body(relay), b"x" * 97


def malformed_head_split(relay: Relay) -> tuple[bytes, bytes]:
    request = post(relay, b"{}")
    return request[:10], b"\x00" + request[10:]


def invalid_chunk_framing_split(relay: Relay) -> tuple[bytes, bytes]:
    return chunked_head(relay).encode(), b"zz\r\nabc\r\n"


async def yield_turns(turns: int) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    "turns_before_the_clock_moves", [1, 2], ids=["timer-first", "reader-first"]
)
@pytest.mark.parametrize(
    ("split", "window"),
    [
        pytest.param(head_split_before_its_last_bytes, HEAD_RECEIVE_SECONDS, id="head"),
        pytest.param(body_split_before_its_last_bytes, BODY_RECEIVE_SECONDS, id="body"),
        pytest.param(malformed_head_split, HEAD_RECEIVE_SECONDS, id="malformed-head"),
        pytest.param(invalid_chunk_framing_split, BODY_RECEIVE_SECONDS, id="invalid-chunk"),
    ],
)
def test_a_request_completed_in_the_turn_its_deadline_passes_never_reaches_the_owner(
    split: Callable[[Relay], tuple[bytes, bytes]], window: float, turns_before_the_clock_moves: int
) -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        client = await RawClient.connect(port_of(relay))
        first, last = split(relay)
        await client.send(first)
        deadline = clock.now + window
        await parked_at(clock, deadline)
        await client.send(last)
        await yield_turns(turns_before_the_clock_moves)
        clock.advance_to(deadline)
        assert await client.delivery() is Delivery.ABSENT
        assert owner.requests == []

    drive_stepped(scenario, owner)


@pytest.mark.parametrize("turns_of_timer_cleanup", range(8))
def test_the_body_deadline_counts_from_the_parsed_head_whatever_the_timer_cleanup_takes(
    turns_of_timer_cleanup: int,
) -> None:
    owner = ScriptedOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        client = await RawClient.connect(port_of(relay))
        head = partial_length_body(relay)[: -len(b"abc")]
        await client.send(head[:10])
        await parked_at(clock, HEAD_RECEIVE_SECONDS)
        await client.send(head[10:])
        await yield_turns(turns_of_timer_cleanup)
        clock.advance_to(BODY_RECEIVE_SECONDS + 1)
        await yield_turns(turns_of_timer_cleanup)
        clock.advance_to(2 * BODY_RECEIVE_SECONDS)
        await client.send(b"x" * 100)
        assert await client.delivery() is Delivery.ABSENT
        assert owner.requests == []

    drive_stepped(scenario, owner)


def test_the_owners_time_counts_against_neither_deadline() -> None:  # L12
    owner = ScriptedOwner(gated=True)

    async def scenario(relay: Relay, clock: StepClock) -> None:
        client = await start_gated_request(relay, owner)
        clock.advance(HEAD_RECEIVE_SECONDS + BODY_RECEIVE_SECONDS + 1)
        await parked_at(clock, None)
        owner.release()
        assert (await client.answer()).status == HTTPStatus.OK

    drive_stepped(scenario, owner)


def test_closing_an_idle_relay_returns_and_refuses_the_old_port() -> None:  # L13
    async def scenario(relay: Relay, clock: StepClock) -> None:
        (idle,) = await connect_idle(relay, 1)
        await close_bounded(relay)
        assert await idle.at_eof()
        old_port = port_of(relay)
        with pytest.raises(ConnectionRefusedError):
            await RawClient.connect(old_port)

    drive_stepped(scenario)


def test_closing_with_a_cooperating_owner_in_flight_needs_no_clock_time() -> None:  # L14
    owner = ScriptedOwner(gated=True)

    async def scenario(relay: Relay, clock: StepClock) -> None:
        client = await start_gated_request(relay, owner)
        await close_bounded(relay)
        assert clock.now == 0.0
        assert owner.cancelled
        assert await client.delivery() is Delivery.ABSENT
        assert relay_tasks() == []

    drive_stepped(scenario, owner)


def test_a_close_the_owner_outlasts_raises_the_incomplete_outcome_and_keeps_the_task(
    caplog: pytest.LogCaptureFixture,
) -> None:  # L15
    owner = SuppressingOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        await start_gated_request(relay, owner)
        outcome = await exhaust_close_budget(relay, clock)
        assert "1" in str(outcome)
        for text in (str(outcome), repr(outcome)):
            assert relay.access.secret not in text
            assert RUN_ID not in text
        old_port = port_of(relay)
        with pytest.raises(ConnectionRefusedError):
            await RawClient.connect(old_port)
        assert len(connection_numbers(relay_tasks())) == 1
        assert relay_tasks() == [f"relay:{RUN_ID}:conn-1", f"relay:{RUN_ID}:conn-1:answer"]

    drive_stepped(scenario, owner)
    assert relay_log_records(caplog) == []


def test_the_next_close_after_the_owner_lets_go_completes(
    caplog: pytest.LogCaptureFixture,
) -> None:  # L16
    owner = SuppressingOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        await start_gated_request(relay, owner)
        await exhaust_close_budget(relay, clock)
        await exhaust_close_budget(relay, clock)
        assert clock.now == 2 * RELAY_CLOSE_SECONDS
        owner.release()
        await close_bounded(relay)
        assert relay_tasks() == []

    drive_stepped(scenario, owner)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_task_ending_mid_budget_does_not_renew_the_close_deadline() -> None:
    owner = SuppressingOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        await start_gated_request(relay, owner)
        loop, held = await hold_one_accepted_connection(relay)
        closing = asyncio.create_task(relay.close())
        deadline = clock.now + RELAY_CLOSE_SECONDS
        await parked_at(clock, deadline)
        clock.advance(RELAY_CLOSE_SECONDS / 2)
        owner.release()
        await yield_until(lambda: connection_numbers(relay_tasks()) == [])
        await parked_at(clock, deadline)
        assert not closing.done()
        clock.advance_to(deadline)
        with pytest.raises(RelayCleanupIncomplete):
            await closing
        loop.release_acceptance()
        await close_bounded(relay)
        assert await held.delivery() is Delivery.ABSENT

    drive_stepped(scenario, owner, loop_factory=HoldingSteppedLoop)


def test_an_owner_absorbing_one_cancellation_is_cancelled_again_by_the_next_close() -> None:  # L17
    owner = AbsorbingOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        await start_gated_request(relay, owner)
        await exhaust_close_budget(relay, clock)
        await close_bounded(relay)
        assert relay_tasks() == []

    drive_stepped(scenario, owner)


def test_two_concurrent_closes_share_one_attempt_and_both_return() -> None:  # L18
    owner = ScriptedOwner(gated=True)

    async def scenario(relay: Relay, clock: StepClock) -> None:
        names = record_task_names()
        await start_gated_request(relay, owner)
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            results = await asyncio.gather(relay.close(), relay.close())
        assert results == [None, None]
        assert names.count(CLOSE_TASK) == 1

    drive_stepped(scenario, owner)


def test_two_concurrent_closes_share_one_incomplete_outcome() -> None:  # L19
    owner = SuppressingOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        names = record_task_names()
        await start_gated_request(relay, owner)
        first = asyncio.create_task(relay.close())
        second = asyncio.create_task(relay.close())
        deadline = clock.now + RELAY_CLOSE_SECONDS
        await parked_at(clock, deadline)
        clock.advance_to(deadline)
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, RelayCleanupIncomplete) for result in results)
        assert names.count(CLOSE_TASK) == 1

    drive_stepped(scenario, owner)


def test_cancelling_one_close_caller_leaves_the_attempt_to_the_other() -> None:  # L20
    owner = SuppressingOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        await start_gated_request(relay, owner)
        cancelled = asyncio.create_task(relay.close())
        waiting = asyncio.create_task(relay.close())
        await parked_at(clock, clock.now + RELAY_CLOSE_SECONDS)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        owner.release()
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            assert await waiting is None
        assert relay_tasks() == []

    drive_stepped(scenario, owner)


def test_an_attempt_whose_every_caller_was_cancelled_still_ends_and_leaves_no_unretrieved_error(
    caplog: pytest.LogCaptureFixture,
) -> None:  # L21
    owner = SuppressingOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        await start_gated_request(relay, owner)
        caller = asyncio.create_task(relay.close())
        deadline = clock.now + RELAY_CLOSE_SECONDS
        await parked_at(clock, deadline)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        clock.advance_to(deadline)
        await yield_until(lambda: CLOSE_TASK not in relay_tasks())
        owner.release()
        await close_bounded(relay)
        gc.collect()
        assert unretrieved_task_errors(caplog) == []

    drive_stepped(scenario, owner)
    gc.collect()
    assert unretrieved_task_errors(caplog) == []


def test_closing_after_a_complete_close_returns_at_once_with_no_new_attempt() -> None:  # L22
    async def scenario(relay: Relay, clock: StepClock) -> None:
        names = record_task_names()
        await close_bounded(relay)
        await close_bounded(relay)
        assert names.count(CLOSE_TASK) == 1

    drive_stepped(scenario)


@pytest.mark.parametrize(
    ("opening", "window"),
    [
        pytest.param(partial_head, HEAD_RECEIVE_SECONDS, id="L23"),
        pytest.param(partial_length_body, BODY_RECEIVE_SECONDS, id="L24"),
    ],
)
def test_closing_during_a_receive_deadline_wait_ends_the_connection_unanswered(
    opening: Opening, window: float
) -> None:
    async def scenario(relay: Relay, clock: StepClock) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(opening(relay))
        await parked_at(clock, clock.now + window)
        await close_bounded(relay)
        assert relay_tasks() == []
        assert await client.delivery() is Delivery.ABSENT

    drive_stepped(scenario)


def test_a_connection_completed_in_the_kernel_before_the_close_is_never_served() -> None:  # L25
    async def scenario(relay: Relay, clock: StepClock) -> None:
        connected = blocking_connect(port_of(relay))
        await close_bounded(relay)
        client = await RawClient.adopt(connected)
        assert await client.delivery() is Delivery.ABSENT
        assert relay_tasks() == []

    drive_stepped(scenario)


async def hold_one_accepted_connection(relay: Relay) -> tuple[HoldingSteppedLoop, RawClient]:
    loop = asyncio.get_running_loop()
    assert isinstance(loop, HoldingSteppedLoop)
    loop.hold_acceptance(port_of(relay))
    client = await RawClient.connect(port_of(relay))
    await yield_until(lambda: loop.held_acceptances == 1)
    return loop, client


def test_a_connection_whose_acceptance_runs_after_the_close_began_is_aborted() -> None:  # L26
    async def scenario(relay: Relay, clock: StepClock) -> None:
        loop, client = await hold_one_accepted_connection(relay)
        closing = asyncio.create_task(relay.close())
        await parked_at(clock, clock.now + RELAY_CLOSE_SECONDS)
        loop.release_acceptance()
        async with asyncio.timeout(TEST_WAIT_SECONDS):
            assert await closing is None
        assert await client.delivery() is Delivery.ABSENT
        assert relay_tasks() == []

    drive_stepped(scenario, loop_factory=HoldingSteppedLoop)


def test_a_task_cancelled_before_its_first_step_is_deregistered(
    caplog: pytest.LogCaptureFixture,
) -> None:  # L27
    async def scenario(relay: Relay, clock: StepClock) -> None:
        loop, client = await hold_one_accepted_connection(relay)
        loop.release_acceptance()
        await close_bounded(relay)
        assert await client.delivery() is Delivery.ABSENT
        assert relay_tasks() == []
        assert connection_numbers(retained_relay_tasks()) == []
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    drive_stepped(scenario, loop_factory=HoldingSteppedLoop)


def test_closing_while_an_answer_is_written_never_counts_a_cut_answer_as_delivered(
    caplog: pytest.LogCaptureFixture,
) -> None:  # L28
    owner = ScriptedOwner(answers=[ANSWER_BEYOND_THE_SOCKET_BUFFERS])

    async def scenario(relay: Relay, clock: StepClock) -> None:
        client = await RawClient.connect(port_of(relay))
        await client.send(post(relay, b"{}"))
        await owner.wait_entered()
        await parked_at(clock, None)
        await close_bounded(relay)
        assert await client.delivery() in {Delivery.INTERRUPTED, Delivery.ABSENT}
        assert relay_tasks() == []

    drive_stepped(scenario, owner)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_signal_handlers_stay_unchanged_across_an_incomplete_close_and_its_retry() -> None:  # L29
    owner = SuppressingOwner()

    async def scenario(relay: Relay, clock: StepClock) -> None:
        before = handlers()
        await start_gated_request(relay, owner)
        await exhaust_close_budget(relay, clock)
        assert handlers() == before
        owner.release()
        await close_bounded(relay)
        assert handlers() == before

    drive_stepped(scenario, owner)
