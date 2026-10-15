"""``POST /runs`` over a real socket: who may start a run, with which body, on which route.

Tokens are minted by a throwaway key the verifier's injected key-set fetch
serves, so the service's real checks run; the Keycloak suite proves the same
entrance with real Keycloak tokens. S1: both tokens are required. S2: a user
token cannot pose as the workload. S3: the request names only purpose and
turn. S4: H5 routing decides, and a refused caller starts no run.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator

import httpx
import pytest
from service_support import (
    COWRITER,
    PRESENTATOR_CLIENT,
    PRESENTER,
    SONGMAKER_CLIENT,
    SONGMAKER_GROUP,
    ScriptedRunner,
    TokenMint,
    assert_rejected,
    decoded_events,
    post_run,
    run_body,
    run_headers,
    served,
    service_config,
)
from webauth.jwt_verification import KeySetFetchError

from agent_client.wire import Failed, FailureCode, RejectionCode
from agent_providers.events import AssistantTextEvent, FinalEvent
from agent_service.app import create_app
from agent_service.routing import Api, Cli
from agent_service.run_request import MAX_REQUEST_BYTES
from agent_service.runners import Runners

CLI_SCRIPT = (AssistantTextEvent(text="via the CLI"), FinalEvent(text="CLI answer"))
API_SCRIPT = (AssistantTextEvent(text="via the API"), FinalEvent(text="API answer"))

MINT = TokenMint()
OTHER_MINT = TokenMint()


@pytest.fixture(scope="module")
def runners() -> Runners:
    return Runners(cli=ScriptedRunner(CLI_SCRIPT), api=ScriptedRunner(API_SCRIPT))


@pytest.fixture(scope="module")
def service(runners) -> Iterator[str]:
    app = create_app(service_config(), runners=runners, fetch_key_set=MINT.fetch_key_set)
    with served(app) as base_url:
        yield base_url


def _admitted_events(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return [event.type for event in decoded_events(response.content)]


def test_a_subscription_member_with_both_tokens_runs_on_the_cli_route(service, runners):
    response = post_run(service, run_headers(MINT.workload(), MINT.user()))

    events = decoded_events(response.content)
    assert _admitted_events(response) == ["run_started", "assistant_text", "succeeded"]
    assert events[-1].text == "CLI answer"
    assert runners.cli.admitted[-1].route == Cli("claude-main")
    assert runners.cli.admitted[-1].principal.subject == "user-alice"


def test_an_app_group_member_runs_on_the_api_route(service, runners):
    response = post_run(
        service, run_headers(MINT.workload(), MINT.user(groups=[SONGMAKER_GROUP], sub="user-bob"))
    )

    assert decoded_events(response.content)[-1].text == "API answer"
    assert runners.api.admitted[-1].route == Api("cowriter")


def test_a_member_of_neither_group_is_not_enabled_and_starts_no_run(service, runners):
    runs_before = len(runners.cli.admitted) + len(runners.api.admitted)

    response = post_run(service, run_headers(MINT.workload(), MINT.user(groups=[])))

    assert_rejected(response, RejectionCode.NOT_ENABLED, 403)
    assert len(runners.cli.admitted) + len(runners.api.admitted) == runs_before


def test_a_route_without_a_runner_answers_provider_unavailable():
    app = create_app(service_config(), fetch_key_set=MINT.fetch_key_set)
    with served(app) as base_url:
        response = post_run(base_url, run_headers(MINT.workload(), MINT.user()))

    terminal = decoded_events(response.content)[-1]
    assert isinstance(terminal, Failed)
    assert terminal.code is FailureCode.PROVIDER_UNAVAILABLE


@pytest.mark.parametrize(
    ("workload", "user", "code", "status"),
    [
        pytest.param(True, True, RejectionCode.UNAVAILABLE, 503, id="both-tokens"),
        pytest.param(False, True, RejectionCode.UNAUTHORIZED, 401, id="no-workload-token"),
        pytest.param(True, False, RejectionCode.UNAVAILABLE, 503, id="no-user-token"),
    ],
)
def test_an_unfetchable_key_set_answers_unavailable_after_the_workload_token_is_checked(
    workload, user, code, status
):
    def unreachable_issuer(_url: str) -> bytes:
        raise KeySetFetchError("the issuer cannot be reached")

    app = create_app(service_config(), fetch_key_set=unreachable_issuer)
    with served(app) as base_url:
        response = post_run(
            base_url,
            run_headers(MINT.workload() if workload else None, MINT.user() if user else None),
        )

    assert_rejected(response, code, status)


TokenFor = Callable[[], str | None]


def _no_token() -> None:
    return None


def _expired_workload() -> str:
    now = int(time.time())
    return MINT.workload(iat=now - 400, exp=now - 100)


def _long_lived_workload() -> str:
    now = int(time.time())
    return MINT.workload(iat=now, exp=now + 301)


def _user_with_the_workload_subject() -> str:
    return MINT.user(sub=f"service-account-user-of-{SONGMAKER_CLIENT}")


UNAUTHORIZED = RejectionCode.UNAUTHORIZED
FORBIDDEN = RejectionCode.FORBIDDEN

TOKEN_REJECTIONS = [
    pytest.param(_no_token, _no_token, UNAUTHORIZED, id="no-tokens"),
    pytest.param(_no_token, MINT.user, UNAUTHORIZED, id="no-workload-token"),
    pytest.param(MINT.workload, _no_token, UNAUTHORIZED, id="no-user-token"),
    pytest.param(lambda: "not.a.jwt", MINT.user, UNAUTHORIZED, id="garbage-workload-token"),
    pytest.param(OTHER_MINT.workload, MINT.user, UNAUTHORIZED, id="workload-of-an-unknown-key"),
    pytest.param(_expired_workload, MINT.user, UNAUTHORIZED, id="expired-workload-token"),
    pytest.param(
        MINT.workload, lambda: MINT.user(aud="account"), UNAUTHORIZED, id="user-wrong-audience"
    ),
    pytest.param(
        MINT.workload,
        lambda: MINT.user(iss="https://elsewhere.test/realms/x"),
        UNAUTHORIZED,
        id="user-token-wrong-issuer",
    ),
    pytest.param(
        MINT.workload, lambda: MINT.user(groups="/ki-abo"), UNAUTHORIZED, id="groups-not-a-list"
    ),
    pytest.param(
        MINT.workload,
        lambda: MINT.user(groups=["/ki-abo", 7]),
        UNAUTHORIZED,
        id="groups-with-a-non-string",
    ),
    pytest.param(
        MINT.workload, lambda: MINT.user(sub=None), UNAUTHORIZED, id="user-token-without-subject"
    ),
    pytest.param(
        lambda: MINT.workload(realm_access={"roles": ["default-roles-agent-test"]}),
        MINT.user,
        FORBIDDEN,
        id="workload-without-the-workload-role",
    ),
    pytest.param(
        lambda: MINT.workload(client_id=PRESENTATOR_CLIENT),
        MINT.user,
        FORBIDDEN,
        id="workload-client-id-differs-from-azp",
    ),
    pytest.param(
        lambda: MINT.workload(client_id=None), MINT.user, FORBIDDEN, id="workload-without-client-id"
    ),
    pytest.param(
        lambda: MINT.workload(client="unknown-app"),
        lambda: MINT.user(azp="unknown-app"),
        FORBIDDEN,
        id="workload-of-an-unregistered-client",
    ),
    pytest.param(_long_lived_workload, MINT.user, FORBIDDEN, id="workload-living-over-300-s"),
    pytest.param(
        lambda: MINT.workload(sub=""), MINT.user, UNAUTHORIZED, id="workload-with-empty-subject"
    ),
    pytest.param(
        lambda: MINT.workload(sub=None), MINT.user, FORBIDDEN, id="workload-without-subject"
    ),
    pytest.param(
        MINT.workload,
        lambda: MINT.user(azp=PRESENTATOR_CLIENT),
        FORBIDDEN,
        id="user-logged-in-through-another-app",
    ),
    pytest.param(
        MINT.workload,
        _user_with_the_workload_subject,
        FORBIDDEN,
        id="user-subject-equals-workload-subject",
    ),
    pytest.param(
        lambda: MINT.workload(realm_access={"roles": []}),
        lambda: "not.a.jwt",
        UNAUTHORIZED,
        id="an-unusable-token-answers-before-a-forbidden-one",
    ),
]


@pytest.mark.parametrize(("workload_token", "user_token", "code"), TOKEN_REJECTIONS)
def test_tokens_that_do_not_bind_one_app_to_one_user_are_refused(
    service, workload_token: TokenFor, user_token: TokenFor, code
):
    workload, user = workload_token(), user_token()

    response = post_run(service, run_headers(workload, user))

    assert_rejected(response, code, 401 if code is UNAUTHORIZED else 403)
    for token in (workload, user):
        if token is not None:
            assert token not in response.text
            assert all(token not in value for value in response.headers.values())


def test_every_unusable_credential_answers_the_same_body(service):
    bodies = {
        post_run(service, run_headers(None, MINT.user())).content,
        post_run(service, run_headers(_expired_workload(), MINT.user())).content,
        post_run(service, run_headers(MINT.workload(), MINT.user(aud="account"))).content,
    }

    assert len(bodies) == 1


def test_a_user_token_sent_as_the_workload_token_is_forbidden(service):
    user = MINT.user()

    assert_rejected(post_run(service, run_headers(user, user)), FORBIDDEN, 403)


def test_a_user_token_outside_the_bearer_scheme_is_unauthorized(service):
    headers = {"Agent-Workload-Token": MINT.workload(), "Authorization": f"Basic {MINT.user()}"}

    assert_rejected(post_run(service, headers), UNAUTHORIZED, 401)


@pytest.mark.parametrize(
    "field",
    ["provider", "model", "route", "user_id", "mcp_url", "mcp_token", "tools"],
)
def test_a_body_naming_anything_beyond_purpose_and_turn_is_invalid(service, field):
    response = post_run(
        service, run_headers(MINT.workload(), MINT.user()), run_body(**{field: "x"})
    )

    assert_rejected(response, RejectionCode.INVALID_REQUEST, 422)


def _too_large() -> bytes:
    return json.dumps(run_body(turn={"system": "x" * MAX_REQUEST_BYTES, "messages": []})).encode()


BODY_REJECTIONS = [
    pytest.param(b'{"v": 1, "purpose": NaN}', RejectionCode.INVALID_REQUEST, 422, id="nan"),
    pytest.param(
        json.dumps(run_body())[:-1].encode() + b', "purpose": "songmaker.cowriter"}',
        RejectionCode.INVALID_REQUEST,
        422,
        id="a-member-named-twice",
    ),
    pytest.param(b"[1]", RejectionCode.INVALID_REQUEST, 422, id="not-an-object"),
    pytest.param(b'{"v": 1,', RejectionCode.INVALID_REQUEST, 422, id="not-json"),
    pytest.param(_too_large(), RejectionCode.INVALID_REQUEST, 422, id="over-256-kib"),
    pytest.param(
        json.dumps(run_body(turn={"system": "x", "messages": []})).encode(),
        RejectionCode.INVALID_REQUEST,
        422,
        id="wrong-shape",
    ),
    pytest.param(
        json.dumps(run_body(v=2)).encode(), RejectionCode.UNSUPPORTED_VERSION, 400, id="v-2"
    ),
    pytest.param(
        json.dumps(run_body(v="1")).encode(),
        RejectionCode.UNSUPPORTED_VERSION,
        400,
        id="v-as-a-string",
    ),
    pytest.param(
        json.dumps(run_body(v=2, provider="claude")).encode(),
        RejectionCode.UNSUPPORTED_VERSION,
        400,
        id="version-is-read-before-the-shape",
    ),
    pytest.param(
        json.dumps(run_body(purpose=PRESENTER)).encode(),
        FORBIDDEN,
        403,
        id="another-apps-purpose",
    ),
    pytest.param(
        json.dumps(run_body(purpose="songmaker.unknown")).encode(),
        FORBIDDEN,
        403,
        id="unknown-purpose",
    ),
]


@pytest.mark.parametrize(("content", "code", "status"), BODY_REJECTIONS)
def test_a_body_that_is_not_an_allowed_run_request_is_refused(service, content, code, status):
    response = post_run(service, run_headers(MINT.workload(), MINT.user()), content=content)

    assert_rejected(response, code, status)


def test_an_oversized_body_without_a_declared_length_is_refused_while_read(service):
    def chunks() -> Iterator[bytes]:
        yield b'{"v": 1, "purpose": "'
        for _ in range(MAX_REQUEST_BYTES // 1024 + 1):
            yield b"x" * 1024
        yield b'"}'

    response = httpx.post(
        f"{service}/runs",
        headers=run_headers(MINT.workload(), MINT.user()),
        content=chunks(),
        timeout=10,
    )

    assert "content-length" not in response.request.headers
    assert_rejected(response, RejectionCode.INVALID_REQUEST, 422)


def test_tokens_are_checked_before_the_body(service):
    response = post_run(service, run_headers(None, MINT.user()), content=b"not json")

    assert_rejected(response, UNAUTHORIZED, 401)


def test_no_log_line_carries_token_text(service, caplog):
    workload, user = MINT.workload(), MINT.user()
    with caplog.at_level(logging.DEBUG):
        post_run(service, run_headers(workload, user))
        post_run(service, run_headers(workload, MINT.user(azp=PRESENTATOR_CLIENT)))
        post_run(service, run_headers(workload, user), content=b"not json")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert workload not in logged
    assert user not in logged


def test_the_admitted_purpose_is_the_one_the_runner_receives(service, runners):
    post_run(service, run_headers(MINT.workload(), MINT.user()))

    assert runners.cli.admitted[-1].request.purpose == COWRITER
    assert runners.cli.admitted[-1].app == "songmaker"
