"""The app gate: membership of the app, checked at admission for every route (decision record 0001).

A person is a member of an app when one of their groups is ``/apps/<app>`` or a path below it,
segment-aware. Everything below is driven through the real entrance over a socket; "nothing
signed" and "no run" are read back from the signing key and from both runners, never
inferred from the 403. Test values: apps ``songmaker`` (with an origin) and ``presentator``,
the subscription and API groups of ``service_support``, and one Claude profile listing
``sonnet``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest
from service_support import (
    API_ALIAS,
    API_GROUP,
    LISTED_MODEL,
    MALFORMED_PURPOSES,
    SONGMAKER_GROUP,
    SUBSCRIPTION_GROUP,
    TEST_ISSUER,
    UNLISTED_MODEL,
    ScriptedRunner,
    TokenMint,
    WallClock,
    answer_of,
    asgi_post_status,
    assert_rejected,
    capable_service_config,
    get_run,
    no_run_started,
    post_run,
    record_signed_tokens,
    run_body,
    run_headers,
    served,
)
from wire_builders import started

from agent_client.wire import RejectionCode
from agent_providers.events import AssistantTextEvent, FinalEvent
from agent_service.admission import AdmissionSlot
from agent_service.app import create_app
from agent_service.config import ServiceConfig
from agent_service.identity import Principal
from agent_service.routing import Api
from agent_service.run_records import RunRecords
from agent_service.runners import Runners

MINT = TokenMint()
ANSWER = (AssistantTextEvent(text="answer"), FinalEvent(text="done"))
FORBIDDEN = RejectionCode.FORBIDDEN
NOT_ENABLED = RejectionCode.NOT_ENABLED
LISTED = f"claude/{LISTED_MODEL}"
UNLISTED = f"claude/{UNLISTED_MODEL}"
SONGMAKER_ADMIN_GROUP = "/apps/songmaker/admin"
PRESENTATOR_ADMIN_GROUP = "/apps/presentator/admin"
ALICE = Principal(TEST_ISSUER, "user-alice")


@dataclass(frozen=True)
class Gate:
    base_url: str
    runners: Runners
    signed: list[str]
    records: RunRecords
    slot_takes: list[object]
    config: ServiceConfig

    def post(self, groups: list[str], **body: object) -> httpx.Response:
        headers = run_headers(MINT.workload(), MINT.user(groups=groups))
        return post_run(self.base_url, headers, run_body(**body))

    def refuse(self, groups: list[str], code: RejectionCode = FORBIDDEN, **body: object) -> None:
        """Post as ``groups``; the refusal signs nothing and starts no run."""
        with no_run_started(self.runners):
            response = self.post(groups, **body)
        assert_rejected(response, code, 403)
        assert self.signed == []

    def read(self, run_id: UUID, groups: list[str]) -> httpx.Response:
        return get_run(
            self.base_url, run_headers(MINT.workload(), MINT.user(groups=groups)), str(run_id)
        )

    def seed_a_run_of_alice(self) -> UUID:
        run_id = uuid4()
        self.records.begin(started(run_id), "songmaker", ALICE)
        return run_id


@pytest.fixture
def gate(tmp_path, monkeypatch) -> Iterator[Gate]:
    signed = record_signed_tokens(monkeypatch)
    slot_takes: list[object] = []
    take = AdmissionSlot.try_take

    def recording_take(slot):
        slot_takes.append(slot)
        return take(slot)

    monkeypatch.setattr(AdmissionSlot, "try_take", recording_take)
    runners = Runners(cli=ScriptedRunner(ANSWER), api=ScriptedRunner(ANSWER))
    records = RunRecords(WallClock())
    config = capable_service_config(tmp_path)
    app = create_app(
        config,
        runners=runners,
        records=records,
        fetch_key_set=MINT.fetch_key_set,
    )
    with served(app) as base_url:
        yield Gate(base_url, runners, signed, records, slot_takes, config)


def _admitted(response: httpx.Response) -> None:
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "groups",
    [
        pytest.param([SONGMAKER_GROUP], id="A1-the-app-group"),
        pytest.param([SONGMAKER_ADMIN_GROUP], id="A2-a-child-of-the-app"),
        pytest.param([f"{SONGMAKER_ADMIN_GROUP}/leads"], id="A2-a-deeper-descendant"),
    ],
)
def test_a_member_of_the_app_or_a_path_below_it_runs_on_the_subscription(gate, groups):
    _admitted(gate.post([SUBSCRIPTION_GROUP, *groups]))

    assert len(gate.runners.cli.admitted) == 1


@pytest.mark.parametrize(
    "groups",
    [
        pytest.param(["/apps/songmaker-other"], id="A3-a-look-alike-name"),
        pytest.param([PRESENTATOR_ADMIN_GROUP], id="A4-the-other-apps-group"),
        pytest.param([], id="A5-subscription-without-the-app"),
    ],
)
def test_a_subscriber_outside_the_app_is_forbidden_and_nothing_starts(gate, groups):
    gate.refuse([SUBSCRIPTION_GROUP, *groups])


@pytest.mark.parametrize(
    "groups",
    [
        pytest.param([API_GROUP], id="A5-api-without-the-app"),
        pytest.param([SUBSCRIPTION_GROUP, API_GROUP], id="A5-both-without-the-app"),
    ],
)
def test_an_entitled_caller_outside_the_app_is_forbidden_before_routing(gate, groups):
    gate.refuse(groups)


@pytest.mark.parametrize("model", [None, LISTED])
@pytest.mark.parametrize("app_group", [SONGMAKER_GROUP, SONGMAKER_ADMIN_GROUP])
def test_a_member_without_an_ai_group_passes_the_gate_and_is_not_enabled(gate, app_group, model):
    gate.refuse([app_group], NOT_ENABLED, model=model)


@pytest.mark.parametrize(
    ("groups", "model"),
    [
        pytest.param([API_GROUP, "/apps/songmaker/team"], None, id="A7-descendant-no-model"),
        pytest.param([API_GROUP, "/apps/songmaker/team"], UNLISTED, id="A7-descendant-unlisted"),
        pytest.param([API_GROUP, SONGMAKER_GROUP], None, id="A7-the-exact-parent"),
    ],
)
def test_an_api_caller_in_the_app_reaches_the_api_route(gate, groups, model):
    _admitted(gate.post(groups, model=model))

    assert gate.runners.api.admitted[-1].route == Api(API_ALIAS, model)
    assert gate.runners.cli.admitted == []


def test_a_member_who_left_the_app_is_forbidden_their_old_run(gate):
    run_id = gate.seed_a_run_of_alice()

    assert_rejected(gate.read(run_id, [SUBSCRIPTION_GROUP]), FORBIDDEN, 403)


def test_a_non_member_is_forbidden_alike_for_a_known_and_a_never_begun_id(gate):
    known = gate.read(gate.seed_a_run_of_alice(), [SUBSCRIPTION_GROUP])
    unknown = gate.read(uuid4(), [SUBSCRIPTION_GROUP])

    assert_rejected(unknown, FORBIDDEN, 403)
    assert answer_of(known) == answer_of(unknown)


def test_a_member_moved_from_the_admin_group_to_the_plain_group_still_reads_their_run(gate):
    run_id = gate.seed_a_run_of_alice()
    assert gate.read(run_id, [SONGMAKER_ADMIN_GROUP]).status_code == 200

    assert gate.read(run_id, [SONGMAKER_GROUP]).status_code == 200


def test_a_non_member_is_refused_before_the_request_body_is_read(gate):
    async def receive() -> dict[str, object]:
        raise AssertionError("the request body was read")

    app = create_app(
        gate.config,
        runners=gate.runners,
        fetch_key_set=MINT.fetch_key_set,
    )
    headers = run_headers(MINT.workload(), MINT.user(groups=[SUBSCRIPTION_GROUP]))

    assert asyncio.run(asgi_post_status(app, headers, receive)) == 403


@pytest.mark.parametrize("model", [LISTED, UNLISTED])
def test_a_dual_entitled_non_member_reaches_no_route_and_takes_no_place(gate, model):
    gate.refuse([SUBSCRIPTION_GROUP, API_GROUP], model=model)

    assert gate.slot_takes == []


NOT_A_REQUEST = [
    pytest.param({"v": 1}, RejectionCode.UNSUPPORTED_VERSION, 400, id="G2-unsupported-version"),
    pytest.param(
        {"purpose": MALFORMED_PURPOSES[2]},
        RejectionCode.INVALID_REQUEST,
        422,
        id="G2-malformed-purpose",
    ),
    pytest.param({"turn": {}}, RejectionCode.INVALID_REQUEST, 422, id="G2-bad-turn"),
]


@pytest.mark.parametrize(("body", "code", "status"), NOT_A_REQUEST)
def test_a_member_learns_what_is_wrong_with_the_body(gate, body, code, status):
    assert_rejected(gate.post([SUBSCRIPTION_GROUP, SONGMAKER_GROUP], **body), code, status)


@pytest.mark.parametrize(("body", "_code", "_status"), NOT_A_REQUEST)
def test_a_non_member_learns_nothing_about_the_body(gate, body, _code, _status):
    gate.refuse([SUBSCRIPTION_GROUP], **body)
