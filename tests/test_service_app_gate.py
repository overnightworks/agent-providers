"""The app gate and the role check, at admission for every route (decision record 0001).

A person is a member of an app when one of their groups is ``/apps/<app>`` or a path below it,
segment-aware. Everything below is driven through the real entrance over a socket; "nothing
signed" and "no run" are read back from the signing key and from both runners, never
inferred from the 403. Test values: apps ``songmaker`` (with an origin) and ``presentator``,
the subscription and API groups of ``service_support``, and one Claude profile listing
``sonnet``. A person's role is ``admin`` in ``/apps/<app>/admin`` or below, else ``user``;
a body whose ``roles`` lacks it answers ``forbidden`` after shape and before everything else.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
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

from agent_runtime.events import AssistantTextEvent, FinalEvent
from agent_runtime_client.agents import load_agent
from agent_runtime_client.wire import RejectionCode, TurnMessage, TurnRole
from agent_runtime_service.admission import AdmissionSlot
from agent_runtime_service.app import create_app
from agent_runtime_service.config import ServiceConfig
from agent_runtime_service.identity import Principal
from agent_runtime_service.routing import Api
from agent_runtime_service.run_records import RunRecords
from agent_runtime_service.runners import Runners

MINT = TokenMint()
ANSWER = (AssistantTextEvent(text="answer"), FinalEvent(text="done"))
FORBIDDEN = RejectionCode.FORBIDDEN
NOT_ENABLED = RejectionCode.NOT_ENABLED
LISTED = f"claude/{LISTED_MODEL}"
UNLISTED = f"claude/{UNLISTED_MODEL}"
SONGMAKER_ADMIN_GROUP = "/apps/songmaker/admin"
PRESENTATOR_ADMIN_GROUP = "/apps/presentator/admin"
ALICE = Principal(TEST_ISSUER, "user-alice")
ABSENT = object()
ADMIN_ONLY = ["admin"]
EVERYONE = ["admin", "user"]


@dataclass(frozen=True)
class Gate:
    base_url: str
    runners: Runners
    signed: list[str]
    records: RunRecords
    slot_takes: list[object]
    config: ServiceConfig

    def post(self, groups: list[str], **body: object) -> httpx.Response:
        document = {
            name: value for name, value in run_body(**body).items() if value is not ABSENT
        }
        return self.post_document(groups, document)

    def post_document(self, groups: list[str], document: dict[str, object]) -> httpx.Response:
        headers = run_headers(MINT.workload(), MINT.user(groups=groups))
        return post_run(self.base_url, headers, document)

    def refuse(
        self,
        groups: list[str],
        code: RejectionCode = FORBIDDEN,
        status: int = 403,
        **body: object,
    ) -> None:
        """Post as ``groups``; the refusal signs nothing, starts no run and takes no place."""
        with no_run_started(self.runners):
            response = self.post(groups, **body)
        assert_rejected(response, code, status)
        assert self.signed == []
        assert self.slot_takes == []

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


SUBSCRIBER_ROLE_USER = [SUBSCRIPTION_GROUP, SONGMAKER_GROUP]
SUBSCRIBER_ROLE_ADMIN = [SUBSCRIPTION_GROUP, SONGMAKER_ADMIN_GROUP]


@pytest.mark.parametrize(
    "groups",
    [
        pytest.param(SUBSCRIBER_ROLE_USER, id="R1-the-app-group"),
        pytest.param([SUBSCRIPTION_GROUP, "/apps/songmaker/team"], id="R1-a-plain-child"),
    ],
)
def test_a_user_on_an_admin_only_agent_is_forbidden_and_nothing_starts(gate, groups):
    gate.refuse(groups, roles=ADMIN_ONLY)


@pytest.mark.parametrize(
    ("groups", "roles"),
    [
        pytest.param(SUBSCRIBER_ROLE_ADMIN, ADMIN_ONLY, id="R2-admin-child-admin-only"),
        pytest.param(
            [SUBSCRIPTION_GROUP, f"{SONGMAKER_ADMIN_GROUP}/leads"],
            ADMIN_ONLY,
            id="R2-admin-descendant-admin-only",
        ),
        pytest.param(SUBSCRIBER_ROLE_ADMIN, EVERYONE, id="R2-admin-child-everyone"),
        pytest.param(
            [SUBSCRIPTION_GROUP, f"{SONGMAKER_ADMIN_GROUP}/leads"],
            EVERYONE,
            id="R2-admin-descendant-everyone",
        ),
        pytest.param(SUBSCRIBER_ROLE_USER, EVERYONE, id="R2-user-everyone"),
    ],
)
def test_a_person_whose_role_the_agent_lists_runs_on_the_subscription(gate, groups, roles):
    _admitted(gate.post(groups, roles=roles))

    assert len(gate.runners.cli.admitted) == 1


@pytest.mark.parametrize(
    "roles",
    [
        pytest.param(ABSENT, id="R3-absent"),
        pytest.param(None, id="R3-null"),
        pytest.param([], id="R3-empty"),
        pytest.param(["user"], id="R3-user-alone"),
        pytest.param(["user", "admin"], id="R3-reversed"),
        pytest.param(["admin", "admin"], id="R3-repeated"),
        pytest.param("admin", id="R3-a-string"),
        pytest.param(["guest"], id="R3-unknown-role"),
    ],
)
def test_a_member_whose_roles_are_missing_or_malformed_is_told_the_body_is_invalid(gate, roles):
    gate.refuse(
        SUBSCRIBER_ROLE_ADMIN, RejectionCode.INVALID_REQUEST, 422, roles=roles
    )


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"role": "admin"}, id="R4a-a-claimed-role"),
        pytest.param({"appRoles": EVERYONE}, id="R4b-the-files-spelling"),
    ],
)
def test_a_body_naming_a_role_beside_roles_is_invalid(gate, extra):
    gate.refuse(
        SUBSCRIBER_ROLE_USER,
        RejectionCode.INVALID_REQUEST,
        422,
        **{"roles": ADMIN_ONLY if "role" in extra else EVERYONE, **extra},
    )


def test_an_admins_admin_only_body_replayed_with_a_users_token_is_forbidden(gate):
    body = run_body(roles=ADMIN_ONLY)
    _admitted(gate.post_document(SUBSCRIBER_ROLE_ADMIN, body))

    with no_run_started(gate.runners):
        replayed = gate.post_document(SUBSCRIBER_ROLE_USER, body)

    assert_rejected(replayed, FORBIDDEN, 403)


def test_the_role_is_refused_before_the_route_is_known(gate):
    gate.refuse(SUBSCRIBER_ROLE_USER, roles=ADMIN_ONLY, model=UNLISTED)

    gate.refuse(SUBSCRIBER_ROLE_USER, NOT_ENABLED, roles=EVERYONE, model=UNLISTED)


def test_a_user_on_an_admin_only_agent_is_forbidden_before_the_api_route(gate):
    gate.refuse([API_GROUP, SONGMAKER_GROUP], roles=ADMIN_ONLY, model=UNLISTED)


@pytest.mark.parametrize(
    "app_group",
    [
        pytest.param("/apps/songmaker/administrators", id="R6-a-longer-name"),
        pytest.param("/apps/songmaker/admin-x", id="R6-a-hyphenated-name"),
    ],
)
def test_a_group_that_only_starts_like_admin_does_not_make_an_admin(gate, app_group):
    gate.refuse([SUBSCRIPTION_GROUP, app_group], roles=ADMIN_ONLY)


def test_a_non_member_posting_no_roles_is_forbidden_not_told_the_body_is_invalid(gate):
    gate.refuse([SUBSCRIPTION_GROUP], roles=ABSENT)


AGENT_FILE = "---\nname: cowriter\ndescription: d\nappRoles: {roles}\n---\nYou help write songs.\n"


def _agent_request(tmp_path: Path, roles: str):
    source = tmp_path / "cowriter.md"
    source.write_text(AGENT_FILE.format(roles=roles), encoding="utf-8")
    agent = load_agent(source, [])
    return agent.request([TurnMessage(role=TurnRole.USER, text="Hi")]).model_dump(mode="json")


@pytest.mark.parametrize(
    ("roles", "groups", "admitted"),
    [
        pytest.param("[admin]", SUBSCRIBER_ROLE_USER, False, id="C1-admin-file-user"),
        pytest.param("[admin]", SUBSCRIBER_ROLE_ADMIN, True, id="C1-admin-file-admin"),
        pytest.param("[admin, user]", SUBSCRIBER_ROLE_USER, True, id="C1-everyone-file-user"),
    ],
)
def test_the_roles_of_an_agent_file_are_what_the_service_enforces(
    gate, tmp_path, roles, groups, admitted
):
    document = _agent_request(tmp_path, roles)

    if admitted:
        _admitted(gate.post_document(groups, document))
    else:
        with no_run_started(gate.runners):
            assert_rejected(gate.post_document(groups, document), FORBIDDEN, 403)
