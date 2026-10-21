"""The service end to end with tokens a real Keycloak 26.8.1 issued.

The ``keycloak-token-shapes`` CI job serves the test realm of
``tests/keycloak/realm.json``; this suite asks it for real client-credentials
workload tokens and real user tokens, serves the service with uvicorn on a
loopback socket, streams ``POST /runs`` with httpx, and checks every stream
against frozen raw SSE frames and through ``RunStreamDecoder``. The negative
cases Keycloak cannot issue are proven with minted tokens in
``test_service_entrance.py``.

Runs only where ``KEYCLOAK_URL`` names that Keycloak.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from service_support import (
    API_ALIAS,
    PRESENTATOR_CLIENT,
    SONGMAKER_CLIENT,
    ScriptedRunner,
    StepClock,
    TokenMint,
    answer_of,
    assert_rejected,
    decoded_events,
    get_run,
    post_run,
    run_body,
    run_headers,
    served,
    service_config,
    with_placeholder_run_id,
)
from test_keycloak_token_shapes import (
    CLIENT_SECRETS,
    KEYCLOAK_URL,
    USER_LOGIN_CLIENT,
    USER_PASSWORDS,
    _access_token,
    _fetch_key_set_from_test_keycloak,
    _realm_url,
    verify,  # noqa: F401 - a fixture, requested by name
)
from webauth.https_client import KeySetFetchError
from webauth.jwt_verification import KEY_SET_REFRESH_COOLDOWN_SECONDS
from wire_builders import started

from agent_client.wire import RejectionCode, RunState, RunStatus
from agent_providers.events import AssistantTextEvent, FinalEvent
from agent_service.app import create_app
from agent_service.identity import Principal
from agent_service.routing import Api, Cli
from agent_service.run_records import RunRecords
from agent_service.runners import Runners

pytestmark = [
    pytest.mark.keycloak,
    pytest.mark.skipif(KEYCLOAK_URL is None, reason="KEYCLOAK_URL names no test Keycloak"),
]

CLI_SCRIPT = (AssistantTextEvent(text="Hello from the CLI."), FinalEvent(text="CLI done."))
API_SCRIPT = (AssistantTextEvent(text="Hello from the API."), FinalEvent(text="API done."))

RUN = "00000000-0000-4000-8000-000000000000"


def _frozen_frames(route: str) -> bytes:
    return (
        "id: 0\nevent: run_started\n"
        f'data: {{"v":2,"run_id":"{RUN}","seq":0,"type":"run_started",'
        '"heartbeat_interval_s":15}\n\n'
        "id: 1\nevent: assistant_text\n"
        f'data: {{"v":2,"run_id":"{RUN}","seq":1,"type":"assistant_text",'
        f'"text":"Hello from the {route}."}}\n\n'
        "id: 2\nevent: succeeded\n"
        f'data: {{"v":2,"run_id":"{RUN}","seq":2,"type":"succeeded","text":"{route} done."}}\n\n'
    ).encode()


def _workload_token(client: str) -> str:
    return _access_token(
        {
            "grant_type": "client_credentials",
            "client_id": client,
            "client_secret": CLIENT_SECRETS[client],
        }
    )


def _user_token(user: str, login_client: str = USER_LOGIN_CLIENT) -> str:
    return _access_token(
        {
            "grant_type": "password",
            "client_id": login_client,
            "client_secret": CLIENT_SECRETS[login_client],
            "username": user,
            "password": USER_PASSWORDS[user],
        }
    )


@pytest.fixture(scope="module")
def runners() -> Runners:
    return Runners(cli=ScriptedRunner(CLI_SCRIPT), api=ScriptedRunner(API_SCRIPT))


@pytest.fixture(scope="module")
def service(runners) -> Iterator[str]:
    app = create_app(
        service_config(issuer=_realm_url()),
        runners=runners,
        fetch_key_set=_fetch_key_set_from_test_keycloak,
    )
    with served(app) as base_url:
        yield base_url


@pytest.mark.parametrize(
    ("user", "route", "expected_route"),
    [("alice", "CLI", Cli("claude-main")), ("bob", "API", Api(API_ALIAS))],
)
def test_a_real_user_and_workload_token_stream_the_scripted_run(
    service, runners, user, route, expected_route
):
    response = post_run(service, run_headers(_songmaker_workload(), _user_token(user)))

    assert response.status_code == 200, response.text
    assert with_placeholder_run_id(response.content) == _frozen_frames(route)
    assert [event.type for event in decoded_events(response.content)] == [
        "run_started",
        "assistant_text",
        "succeeded",
    ]
    runner = runners.cli if expected_route == Cli("claude-main") else runners.api
    assert runner.admitted[-1].route == expected_route


def test_a_real_run_is_recorded_for_its_user_alone(service):
    streamed = post_run(service, run_headers(_songmaker_workload(), _user_token("alice")))
    run_id = str(decoded_events(streamed.content)[0].run_id)

    alice = get_run(service, run_headers(_songmaker_workload(), _user_token("alice")), run_id)
    bob = get_run(service, run_headers(_songmaker_workload(), _user_token("bob")), run_id)

    status = RunStatus.model_validate_json(alice.content)
    assert (status.state, status.events, status.client_lost) == (RunState.SUCCEEDED, 3, None)
    assert_rejected(bob, RejectionCode.NOT_FOUND, 404)


def test_a_user_in_no_group_is_not_enabled(service):
    response = post_run(service, run_headers(_songmaker_workload(), _user_token("carol")))

    assert_rejected(response, RejectionCode.NOT_ENABLED, 403)


def test_a_real_user_token_sent_as_the_workload_token_is_forbidden(service):
    alice = _user_token("alice")

    assert_rejected(post_run(service, run_headers(alice, alice)), RejectionCode.FORBIDDEN, 403)


def test_another_apps_workload_cannot_act_for_a_user_of_this_app(service):
    response = post_run(
        service, run_headers(_workload_token(PRESENTATOR_CLIENT), _user_token("alice"))
    )

    assert_rejected(response, RejectionCode.FORBIDDEN, 403)


def test_only_the_user_who_started_a_run_reads_it_with_real_tokens(verify):  # noqa: F811 - the imported fixture
    alice_claims = verify(_user_token("alice"))
    run_id = uuid4()
    records = RunRecords()
    records.begin(
        started(run_id),
        "songmaker",
        Principal(str(alice_claims["iss"]), str(alice_claims["sub"])),
    )
    app = create_app(
        service_config(issuer=_realm_url()),
        records=records,
        fetch_key_set=_fetch_key_set_from_test_keycloak,
    )
    with served(app) as base_url:
        songmaker = _songmaker_workload()
        alice = get_run(base_url, run_headers(songmaker, _user_token("alice")), str(run_id))
        bob = get_run(base_url, run_headers(songmaker, _user_token("bob")), str(run_id))
        never_begun = get_run(base_url, run_headers(songmaker, _user_token("alice")), str(uuid4()))
        through_presentator = get_run(
            base_url,
            run_headers(
                _workload_token(PRESENTATOR_CLIENT), _user_token("alice", PRESENTATOR_CLIENT)
            ),
            str(run_id),
        )

    assert RunStatus.model_validate_json(alice.content).run_id == run_id
    assert_rejected(never_begun, RejectionCode.NOT_FOUND, 404)
    assert answer_of(bob) == answer_of(never_begun)
    assert answer_of(through_presentator) == answer_of(never_begun)


def test_a_token_of_an_unpublished_key_is_unavailable_once_the_key_set_origin_stops():
    clock = StepClock()
    origin_stopped = False

    def key_set_origin(url: str) -> bytes:
        if origin_stopped:
            raise KeySetFetchError("the key-set origin has stopped")
        return _fetch_key_set_from_test_keycloak(url)

    app = create_app(
        service_config(issuer=_realm_url()),
        fetch_key_set=key_set_origin,
        monotonic_clock=clock,
    )
    unpublished_key = TokenMint(key_id="never-published")
    with served(app) as base_url:
        warm_up = post_run(base_url, run_headers(_songmaker_workload(), _user_token("alice")))
        assert warm_up.status_code != 503, warm_up.text
        origin_stopped = True
        clock.advance(KEY_SET_REFRESH_COOLDOWN_SECONDS)
        response = post_run(
            base_url,
            run_headers(unpublished_key.workload(iss=_realm_url()), _user_token("alice")),
        )

    assert_rejected(response, RejectionCode.UNAVAILABLE, 503)


def _tampered(token: str) -> str:
    header, payload, signature = token.split(".")
    return ".".join((header, payload, ("A" if signature[0] != "A" else "B") + signature[1:]))


def _songmaker_workload() -> str:
    return _workload_token(SONGMAKER_CLIENT)


@pytest.mark.parametrize(
    ("workload_token", "body", "code", "status"),
    [
        pytest.param(lambda: None, run_body(), RejectionCode.UNAUTHORIZED, 401, id="no-workload"),
        pytest.param(
            lambda: _tampered(_songmaker_workload()),
            run_body(),
            RejectionCode.UNAUTHORIZED,
            401,
            id="bad-signature",
        ),
        pytest.param(
            _songmaker_workload,
            run_body(v=1),
            RejectionCode.UNSUPPORTED_VERSION,
            400,
            id="unsupported-version",
        ),
        pytest.param(
            _songmaker_workload,
            run_body(model="opus"),
            RejectionCode.INVALID_REQUEST,
            422,
            id="names-a-model",
        ),
        pytest.param(
            _songmaker_workload,
            run_body(purpose="notes.assistant"),
            RejectionCode.INVALID_REQUEST,
            422,
            id="purpose-outside-the-grammar",
        ),
    ],
)
def test_the_rejection_table_holds_for_real_tokens(service, workload_token, body, code, status):
    response = post_run(service, run_headers(workload_token(), _user_token("alice")), body)

    assert_rejected(response, code, status)
