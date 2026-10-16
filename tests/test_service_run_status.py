"""``GET /runs/{id}`` over a real socket: only the app and user that started a run read it.

Decision record 0001, A1a2 ruling, items 4 to 6. The store is seeded before the
server starts, so the endpoint's answers are those of the real token checks and
the real store.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest
from service_support import (
    TEST_ISSUER,
    WallClock,
    answer_of,
    assert_rejected,
    get_run,
    instant,
    post_run,
    run_headers,
    served,
    service_config,
)
from test_service_entrance import MINT, TOKEN_REJECTIONS
from webauth.jwt_verification import KeySetFetchError
from wire_builders import started, succeeded

from agent_client.wire import RejectionCode, RunState, RunStatus
from agent_service.app import create_app
from agent_service.identity import Principal
from agent_service.run_records import RunRecorder, RunRecords

ALICE = Principal(TEST_ISSUER, "user-alice")
BOB = Principal(TEST_ISSUER, "user-bob")
ALICE_ELSEWHERE = Principal("https://elsewhere.test/realms/x", "user-alice")
NOT_FOUND_BODY = b'{"v":1,"code":"not_found","message":"No such run."}'


@dataclass(frozen=True)
class Seeded:
    records: RunRecords
    running: UUID
    owned: UUID
    expired: UUID
    evicted: UUID
    other_app: UUID
    other_subject: UUID
    other_issuer: UUID


def _seed() -> Seeded:
    clock = WallClock()
    records = RunRecords(clock, max_ended=3)

    def begin(app: str, principal: Principal) -> tuple[UUID, RunRecorder]:
        run_id = uuid4()
        return run_id, records.begin(started(run_id), app, principal)

    def ended_at(seconds: float, app: str = "songmaker", principal: Principal = ALICE) -> UUID:
        run_id, recorder = begin(app, principal)
        recorder.accepted(succeeded(1))
        clock.set_to(instant(seconds))
        recorder.over()
        return run_id

    running, _ = begin("songmaker", ALICE)
    expired = ended_at(10)
    evicted = ended_at(3700)
    owned = ended_at(3701)
    ended_at(3702)
    ended_at(3703)
    other_app, _ = begin("presentator", ALICE)
    other_subject, _ = begin("songmaker", BOB)
    other_issuer, _ = begin("songmaker", ALICE_ELSEWHERE)
    return Seeded(records, running, owned, expired, evicted, other_app, other_subject, other_issuer)


@pytest.fixture(scope="module")
def seeded() -> Seeded:
    return _seed()


@pytest.fixture(scope="module")
def service(seeded: Seeded) -> Iterator[str]:
    app = create_app(service_config(), records=seeded.records, fetch_key_set=MINT.fetch_key_set)
    with served(app) as base_url:
        yield base_url


def _alice() -> dict[str, str]:
    return run_headers(MINT.workload(), MINT.user())


def test_the_owner_reads_a_running_run(service, seeded: Seeded):
    response = get_run(service, _alice(), str(seeded.running))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    status = RunStatus.model_validate_json(response.content)
    assert (status.run_id, status.state) == (seeded.running, RunState.RUNNING)
    assert status.ended_at is None


def test_the_owner_reads_an_ended_run_as_a_body_of_exactly_eight_keys(service, seeded: Seeded):
    response = get_run(service, _alice(), str(seeded.owned))

    assert response.status_code == 200
    assert set(json.loads(response.content)) == {
        "v",
        "run_id",
        "state",
        "failure_code",
        "events",
        "started_at",
        "ended_at",
        "client_lost",
    }
    status = RunStatus.model_validate_json(response.content)
    assert (status.state, status.events, status.client_lost) == (RunState.SUCCEEDED, 2, None)


def test_the_owner_reads_the_same_run_in_upper_case(service, seeded: Seeded):
    lower = get_run(service, _alice(), str(seeded.owned))
    upper = get_run(service, _alice(), str(seeded.owned).upper())

    assert upper.status_code == 200
    assert upper.content == lower.content


def test_an_id_never_begun_answers_the_one_not_found(service):
    response = get_run(service, _alice(), str(uuid4()))

    assert_rejected(response, RejectionCode.NOT_FOUND, 404)
    assert response.content == NOT_FOUND_BODY
    assert response.headers["content-type"] == "application/json"


def _denied_run_ids() -> list:
    return [
        pytest.param(lambda seeded: seeded.expired, id="an expired run"),
        pytest.param(lambda seeded: seeded.evicted, id="an evicted run"),
        pytest.param(lambda seeded: seeded.other_app, id="another app's run, same user"),
        pytest.param(lambda seeded: seeded.other_subject, id="another user's run"),
        pytest.param(lambda seeded: seeded.other_issuer, id="the same subject, another issuer"),
    ]


def _unreadable_segments() -> list:
    return [
        pytest.param("not-a-uuid", id="not a uuid"),
        pytest.param("{8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01}", id="braced"),
        pytest.param("urn:uuid:8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01", id="urn"),
        pytest.param("8c2f6c1e3b5a4d8e9f102a7b4c6d8e01", id="32 bare hex digits"),
        pytest.param("+2345678123442348234123456789abc", id="a sign and 31 digits"),
        pytest.param("8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e0", id="35 characters"),
    ]


@pytest.mark.parametrize("denied_id", _denied_run_ids())
def test_a_run_the_caller_may_not_read_answers_exactly_as_an_id_never_begun(
    service, seeded: Seeded, denied_id
):
    never_begun = get_run(service, _alice(), str(uuid4()))

    denied = get_run(service, _alice(), str(denied_id(seeded)))

    assert answer_of(denied) == answer_of(never_begun)


@pytest.mark.parametrize("segment", _unreadable_segments())
def test_a_segment_that_is_not_the_text_form_of_a_run_id_answers_as_an_id_never_begun(
    service, segment: str
):
    never_begun = get_run(service, _alice(), str(uuid4()))

    unreadable = get_run(service, _alice(), segment)

    assert answer_of(unreadable) == answer_of(never_begun)


@pytest.mark.parametrize(("workload_token", "user_token", "code"), TOKEN_REJECTIONS)
def test_bad_tokens_are_refused_alike_for_a_known_an_unknown_and_a_malformed_id(
    service, seeded: Seeded, workload_token, user_token, code
):
    headers = run_headers(workload_token(), user_token())

    answers = [
        answer_of(get_run(service, headers, segment))
        for segment in (str(seeded.owned), str(uuid4()), "not-a-uuid")
    ]

    assert answers[0] == answers[1] == answers[2]
    assert answers[0] == answer_of(post_run(service, headers))
    assert_rejected(get_run(service, headers, str(seeded.owned)), code, answers[0][0])


def test_an_unfetchable_key_set_answers_unavailable_for_every_id(seeded: Seeded):
    def unreachable_issuer(_url: str) -> bytes:
        raise KeySetFetchError("the issuer cannot be reached")

    app = create_app(service_config(), records=seeded.records, fetch_key_set=unreachable_issuer)
    with served(app) as base_url:
        responses = [
            get_run(base_url, _alice(), segment)
            for segment in (str(seeded.owned), str(uuid4()), "not-a-uuid")
        ]

    for response in responses:
        assert_rejected(response, RejectionCode.UNAVAILABLE, 503)


def test_no_log_line_carries_token_text(service, seeded: Seeded, caplog):
    workload, user = MINT.workload(), MINT.user()
    with caplog.at_level(logging.DEBUG):
        for segment in (str(seeded.owned), str(uuid4()), "not-a-uuid"):
            get_run(service, run_headers(workload, user), segment)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert not [r for r in caplog.records if r.name.startswith("agent_service")]
    assert workload not in logged
    assert user not in logged


def test_the_status_never_names_the_app_or_the_principal(service, seeded: Seeded):
    body = get_run(service, _alice(), str(seeded.owned)).text

    assert "songmaker" not in body
    assert "user-alice" not in body


def test_a_post_to_a_run_path_answers_the_same_for_every_id(service, seeded: Seeded):
    answers = [
        httpx.post(f"{service}/runs/{run_id}", headers=_alice(), timeout=10)
        for run_id in (seeded.owned, uuid4())
    ]

    assert answers[0].status_code == 405
    assert answer_of(answers[0]) == answer_of(answers[1])


def test_a_get_without_an_id_is_not_a_status_read(service):
    response = httpx.get(f"{service}/runs/", headers=_alice(), timeout=10, follow_redirects=True)

    assert response.status_code == 405


def test_a_second_app_over_the_same_configuration_remembers_nothing(seeded: Seeded):
    restarted = create_app(service_config(), fetch_key_set=MINT.fetch_key_set)

    with served(restarted) as base_url:
        response = get_run(base_url, _alice(), str(seeded.owned))

    assert_rejected(response, RejectionCode.NOT_FOUND, 404)
