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
import tracemalloc
from collections.abc import Callable, Iterator

import anyio
import httpx
import pytest
from fastapi import FastAPI
from service_support import (
    ADMITTED_PURPOSES,
    API_ALIAS,
    API_GROUP,
    COWRITER,
    LISTED_MODEL,
    MALFORMED_PURPOSES,
    OLD_SUBSCRIPTION_GROUP,
    PRESENTATOR_CLIENT,
    SONGMAKER_CLIENT,
    SONGMAKER_GROUP,
    SONGMAKER_TOOLS,
    SUBSCRIPTION_GROUP,
    UNLISTED_MODEL,
    ScriptedRunner,
    StepClock,
    TokenMint,
    asgi_post_status,
    assert_rejected,
    decoded_events,
    post_run,
    run_body,
    run_headers,
    served,
    service_config,
    service_config_file,
    with_listed_models,
    without_cli,
    without_profiles,
)
from webauth.https_client import KeySetFetchError
from webauth.jwt_verification import KEY_SET_REFRESH_COOLDOWN_SECONDS

from agent_runtime.events import AssistantTextEvent, FinalEvent
from agent_runtime_client.wire import Failed, FailureCode, RejectionCode, RunRejection
from agent_runtime_service.app import create_app, rejection
from agent_runtime_service.config import load_config
from agent_runtime_service.routing import Api, Cli
from agent_runtime_service.run_request import MAX_REQUEST_BYTES
from agent_runtime_service.runners import Runners

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


def test_a_runner_that_offers_tools_serves_an_app_with_tools(service):
    response = post_run(
        service, run_headers(MINT.workload(), MINT.user()), run_body(tools=SONGMAKER_TOOLS)
    )

    assert decoded_events(response.content)[-1].text == "CLI answer"


@pytest.mark.parametrize("purpose", ADMITTED_PURPOSES)
def test_a_purpose_in_the_grammar_is_admitted_from_any_app(service, purpose):
    response = post_run(
        service, run_headers(MINT.workload(), MINT.user()), run_body(purpose=purpose)
    )

    assert _admitted_events(response) == ["run_started", "assistant_text", "succeeded"]


def test_a_run_request_body_with_a_utf8_byte_order_mark_is_admitted(service):
    body = b"\xef\xbb\xbf" + json.dumps(run_body()).encode()

    response = post_run(service, run_headers(MINT.workload(), MINT.user()), content=body)

    assert _admitted_events(response) == ["run_started", "assistant_text", "succeeded"]


def test_an_api_group_member_of_the_app_runs_on_the_api_route(service, runners):
    response = post_run(
        service,
        run_headers(
            MINT.workload(), MINT.user(groups=[SONGMAKER_GROUP, API_GROUP], sub="user-bob")
        ),
    )

    assert decoded_events(response.content)[-1].text == "API answer"
    assert runners.api.admitted[-1].route == Api(API_ALIAS)


def _loaded_service_run(tmp_path, subscription_groups, user_groups):
    runners = Runners(cli=ScriptedRunner(CLI_SCRIPT), api=ScriptedRunner(API_SCRIPT))
    config = load_config(service_config_file(tmp_path, subscription_groups=subscription_groups))
    app = create_app(config, runners=runners, fetch_key_set=MINT.fetch_key_set)
    with served(app) as base_url:
        response = post_run(
            base_url,
            run_headers(MINT.workload(), MINT.user(groups=[*user_groups, SONGMAKER_GROUP])),
        )
    return response, runners


@pytest.mark.parametrize("group", [SUBSCRIPTION_GROUP, OLD_SUBSCRIPTION_GROUP])
def test_a_member_of_either_listed_subscription_group_alone_runs_on_the_cli_route(tmp_path, group):
    response, runners = _loaded_service_run(
        tmp_path, [SUBSCRIPTION_GROUP, OLD_SUBSCRIPTION_GROUP], [group]
    )

    assert _admitted_events(response) == ["run_started", "assistant_text", "succeeded"]
    assert runners.cli.admitted[-1].route == Cli("claude-main")
    assert runners.api.admitted == []


@pytest.mark.parametrize(
    ("kept", "dropped"),
    [(SUBSCRIPTION_GROUP, OLD_SUBSCRIPTION_GROUP), (OLD_SUBSCRIPTION_GROUP, SUBSCRIPTION_GROUP)],
)
def test_a_subscription_group_removed_from_the_configuration_is_not_enabled(
    tmp_path, kept, dropped
):
    response, runners = _loaded_service_run(tmp_path, [kept], [dropped])

    assert_rejected(response, RejectionCode.NOT_ENABLED, 403)
    assert runners.cli.admitted == []
    assert runners.api.admitted == []


def test_a_member_of_neither_group_is_not_enabled_and_starts_no_run(service, runners):
    runs_before = len(runners.cli.admitted) + len(runners.api.admitted)

    response = post_run(service, run_headers(MINT.workload(), MINT.user(groups=[SONGMAKER_GROUP])))

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
        pytest.param(MINT.workload, MINT.user, RejectionCode.UNAVAILABLE, 503, id="both-tokens"),
        pytest.param(
            lambda: None, MINT.user, RejectionCode.UNAUTHORIZED, 401, id="no-workload-token"
        ),
        pytest.param(
            MINT.workload, lambda: None, RejectionCode.UNAVAILABLE, 503, id="no-user-token"
        ),
        pytest.param(
            lambda: "not.a.jwt",
            MINT.user,
            RejectionCode.UNAUTHORIZED,
            401,
            id="malformed-workload-token",
        ),
        pytest.param(
            lambda: MINT.workload(exp=int(time.time()) - 100, iat=int(time.time()) - 400),
            MINT.user,
            RejectionCode.UNAVAILABLE,
            503,
            id="expired-workload-token",
        ),
        pytest.param(
            lambda: MINT.workload(aud="account"),
            MINT.user,
            RejectionCode.UNAVAILABLE,
            503,
            id="workload-token-of-another-audience",
        ),
    ],
)
def test_an_unfetchable_key_set_answers_unavailable_after_the_workload_token_is_checked(
    workload, user, code, status
):
    def unreachable_issuer(_url: str) -> bytes:
        raise KeySetFetchError("the issuer cannot be reached")

    app = create_app(service_config(), fetch_key_set=unreachable_issuer)
    with served(app) as base_url:
        response = post_run(base_url, run_headers(workload(), user()))

    assert_rejected(response, code, status)


@pytest.mark.parametrize(
    ("issuer_serves_the_kid", "code", "status"),
    [
        pytest.param(False, RejectionCode.UNAVAILABLE, 503, id="refresh-fails"),
        pytest.param(True, RejectionCode.UNAUTHORIZED, 401, id="refresh-succeeds-without-the-kid"),
    ],
)
def test_a_token_with_an_unknown_kid_answers_by_whether_the_key_set_can_be_refreshed(
    issuer_serves_the_kid, code, status
):
    clock = StepClock()
    forged = TokenMint(key_id="never-published")
    fetches = 0

    def issuer_that_fails_after_the_first_fetch(url: str) -> bytes:
        nonlocal fetches
        fetches += 1
        if fetches > 1 and not issuer_serves_the_kid:
            raise KeySetFetchError("the issuer cannot be reached")
        return MINT.fetch_key_set(url)

    app = create_app(
        service_config(),
        fetch_key_set=issuer_that_fails_after_the_first_fetch,
        monotonic_clock=clock,
    )
    with served(app) as base_url:
        warm_up = post_run(base_url, run_headers(MINT.workload(), MINT.user()))
        assert warm_up.status_code != 503, warm_up.text
        clock.advance(KEY_SET_REFRESH_COOLDOWN_SECONDS)
        response = post_run(base_url, run_headers(forged.workload(), MINT.user()))

    assert_rejected(response, code, status)


@pytest.mark.parametrize(
    ("extra_signing_keys", "key_set_fetchable", "code", "status"),
    [
        pytest.param(0, True, None, 200, id="sole-rs256-key"),
        pytest.param(1, True, RejectionCode.UNAUTHORIZED, 401, id="two-rs256-keys"),
        pytest.param(0, False, RejectionCode.UNAVAILABLE, 503, id="no-fetchable-key-set"),
    ],
)
def test_a_token_without_a_kid_is_admitted_only_against_a_sole_rs256_key(
    extra_signing_keys, key_set_fetchable, code, status
):
    mint = TokenMint(kidless=True, extra_signing_keys=extra_signing_keys)

    def fetch_key_set(url: str) -> bytes:
        if not key_set_fetchable:
            raise KeySetFetchError("the issuer cannot be reached")
        return mint.fetch_key_set(url)

    app = create_app(
        service_config(),
        runners=Runners(cli=ScriptedRunner(CLI_SCRIPT), api=ScriptedRunner(API_SCRIPT)),
        fetch_key_set=fetch_key_set,
    )
    with served(app) as base_url:
        response = post_run(base_url, run_headers(mint.workload(), mint.user()))

    if code is None:
        assert _admitted_events(response) == ["run_started", "assistant_text", "succeeded"]
    else:
        assert_rejected(response, code, status)


def test_one_key_set_fetch_serves_every_request_of_the_process():
    mint = TokenMint()
    app = create_app(service_config(), fetch_key_set=mint.fetch_key_set)
    with served(app) as base_url:
        for _ in range(3):
            post_run(base_url, run_headers(mint.workload(), mint.user()))

    assert mint.key_set_fetches == 1


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
        MINT.workload,
        lambda: MINT.user(groups="/subscription-members"),
        UNAUTHORIZED,
        id="groups-not-a-list",
    ),
    pytest.param(
        MINT.workload,
        lambda: MINT.user(groups=["/subscription-members", 7]),
        UNAUTHORIZED,
        id="groups-with-a-non-string",
    ),
    pytest.param(
        MINT.workload, lambda: MINT.user(sub=None), UNAUTHORIZED, id="user-token-without-subject"
    ),
    pytest.param(
        MINT.workload,
        lambda: MINT.user(sub="user-\ud800"),
        UNAUTHORIZED,
        id="user-subject-with-a-lone-surrogate",
    ),
    pytest.param(
        MINT.workload,
        lambda: MINT.user(azp=PRESENTATOR_CLIENT, sub="user-\ud800"),
        UNAUTHORIZED,
        id="lone-surrogate-answers-before-the-azp-mismatch",
    ),
    pytest.param(
        lambda: MINT.workload(client="unknown-app"),
        lambda: MINT.user(azp="unknown-app", sub="user-\ud800"),
        UNAUTHORIZED,
        id="lone-surrogate-answers-before-an-unregistered-client",
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
        lambda: MINT.workload(client_id=""),
        MINT.user,
        FORBIDDEN,
        id="workload-with-empty-client-id",
    ),
    pytest.param(lambda: MINT.workload(azp=None), MINT.user, FORBIDDEN, id="workload-without-azp"),
    pytest.param(lambda: MINT.workload(azp=""), MINT.user, FORBIDDEN, id="workload-with-empty-azp"),
    pytest.param(MINT.workload, lambda: MINT.user(azp=None), FORBIDDEN, id="user-without-azp"),
    pytest.param(MINT.workload, lambda: MINT.user(azp=""), FORBIDDEN, id="user-with-empty-azp"),
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
    ["provider", "route", "user_id", "mcp_url", "mcp_token"],
)
def test_a_body_naming_a_field_reserved_to_the_service_is_invalid(service, field):
    response = post_run(
        service, run_headers(MINT.workload(), MINT.user()), run_body(**{field: "x"})
    )

    assert_rejected(response, RejectionCode.INVALID_REQUEST, 422)


LISTED = f"claude/{LISTED_MODEL}"
UNLISTED = f"claude/{UNLISTED_MODEL}"
SUBSCRIBER = [SUBSCRIPTION_GROUP, SONGMAKER_GROUP]
API_MEMBER = [API_GROUP, SONGMAKER_GROUP]


def _routed_run(config, groups, *, afterwards=None, **body):
    """Post one run; ``afterwards(base_url, headers)`` may probe the same app
    before it is discarded."""
    runners = Runners(cli=ScriptedRunner(CLI_SCRIPT), api=ScriptedRunner(API_SCRIPT))
    app = create_app(config, runners=runners, fetch_key_set=MINT.fetch_key_set)
    with served(app) as base_url:
        headers = run_headers(MINT.workload(), MINT.user(groups=groups))
        response = post_run(base_url, headers, run_body(**body))
        if afterwards is not None:
            afterwards(base_url, headers)
    return response, runners


def test_h1_a_listed_model_reaches_the_cli_route_by_name():
    response, runners = _routed_run(service_config(), SUBSCRIBER, model=LISTED)

    assert _admitted_events(response)
    assert runners.cli.admitted[-1].route == Cli("claude-main", LISTED_MODEL)


def test_h2_an_unlisted_model_reaches_an_api_only_caller_by_its_full_name():
    response, runners = _routed_run(service_config(), API_MEMBER, model=UNLISTED)

    assert _admitted_events(response)
    assert runners.api.admitted[-1].route == Api(API_ALIAS, UNLISTED)


def test_h3_a_null_model_runs_the_default_even_when_the_subscription_lists_nothing():
    config = with_listed_models(service_config(), [])

    response, runners = _routed_run(config, SUBSCRIBER, model=None)

    assert _admitted_events(response)
    assert runners.cli.admitted[-1].route == Cli("claude-main", None)


@pytest.mark.parametrize(
    ("configure", "groups", "api_route"),
    [
        pytest.param(without_cli, API_MEMBER, True, id="H4-no-cli-api"),
        pytest.param(without_cli, SUBSCRIBER, False, id="H5-no-cli-subscriber"),
        pytest.param(without_profiles, API_MEMBER, True, id="H6-no-profiles-api"),
        pytest.param(without_profiles, SUBSCRIBER, False, id="H7-no-profiles-subscriber"),
        pytest.param(
            lambda config: with_listed_models(config, []),
            SUBSCRIBER,
            False,
            id="H8-nothing-listed-subscriber",
        ),
    ],
)
def test_a_named_model_without_subscription_facts_goes_to_the_api_or_is_refused(
    configure, groups, api_route
):
    response, runners = _routed_run(configure(service_config()), groups, model=LISTED)

    if api_route:
        assert _admitted_events(response)
        assert runners.api.admitted[-1].route == Api(API_ALIAS, LISTED)
    else:
        assert_rejected(response, RejectionCode.NOT_ENABLED, 403)
        assert runners.cli.admitted == runners.api.admitted == []


def test_v6_a_malformed_model_is_invalid_before_tools_are_forbidden(service):
    response = post_run(
        service,
        run_headers(
            MINT.workload(client=PRESENTATOR_CLIENT),
            MINT.user(azp=PRESENTATOR_CLIENT, groups=[SUBSCRIPTION_GROUP, "/apps/presentator"]),
        ),
        run_body(model="sonnet", tools=SONGMAKER_TOOLS),
    )

    assert_rejected(response, RejectionCode.INVALID_REQUEST, 422)


def test_v7_a_malformed_model_with_an_unsupported_version_answers_the_version(service):
    response = post_run(
        service, run_headers(MINT.workload(), MINT.user()), run_body(v=1, model="sonnet")
    )

    assert_rejected(response, RejectionCode.UNSUPPORTED_VERSION, 400)


def _too_large() -> bytes:
    return json.dumps(run_body(turn={"system": "x" * MAX_REQUEST_BYTES, "messages": []})).encode()


BODY_REJECTIONS = [
    pytest.param(b'{"v": 2, "purpose": NaN}', RejectionCode.INVALID_REQUEST, 422, id="nan"),
    pytest.param(
        json.dumps(run_body())[:-1].encode() + b', "purpose": "cowriter"}',
        RejectionCode.INVALID_REQUEST,
        422,
        id="a-member-named-twice",
    ),
    pytest.param(b"[1]", RejectionCode.INVALID_REQUEST, 422, id="not-an-object"),
    pytest.param(b'{"v": 2,', RejectionCode.INVALID_REQUEST, 422, id="not-json"),
    pytest.param(_too_large(), RejectionCode.INVALID_REQUEST, 422, id="over-256-kib"),
    *(
        pytest.param(
            json.dumps(run_body()).encode(encoding),
            RejectionCode.INVALID_REQUEST,
            422,
            id=encoding,
        )
        for encoding in ("utf-16", "utf-16-le", "utf-32", "utf-32-be")
    ),
    pytest.param(
        json.dumps(run_body(turn={"system": "x", "messages": []})).encode(),
        RejectionCode.INVALID_REQUEST,
        422,
        id="wrong-shape",
    ),
    pytest.param(
        json.dumps(run_body(v=1)).encode(), RejectionCode.UNSUPPORTED_VERSION, 400, id="v-1"
    ),
    pytest.param(
        json.dumps(run_body(v=3)).encode(), RejectionCode.UNSUPPORTED_VERSION, 400, id="v-3"
    ),
    pytest.param(
        json.dumps({name: value for name, value in run_body().items() if name != "tools"}).encode(),
        RejectionCode.INVALID_REQUEST,
        422,
        id="tools-omitted",
    ),
    *(
        pytest.param(
            json.dumps(run_body(tools=tools)).encode(),
            RejectionCode.INVALID_REQUEST,
            422,
            id=f"tools-{label}",
        )
        for label, tools in (
            ("with-an-empty-name", {"": "read_only"}),
            ("with-an-unknown-effect", {"read_song": "write"}),
            ("as-a-list", ["read_song"]),
        )
    ),
    pytest.param(
        json.dumps(run_body(v="1")).encode(),
        RejectionCode.UNSUPPORTED_VERSION,
        400,
        id="v-as-a-string",
    ),
    pytest.param(
        json.dumps(run_body(v=1, provider="claude")).encode(),
        RejectionCode.UNSUPPORTED_VERSION,
        400,
        id="version-is-read-before-the-shape",
    ),
    pytest.param(
        json.dumps({name: value for name, value in run_body().items() if name != "v"}).encode(),
        RejectionCode.UNSUPPORTED_VERSION,
        400,
        id="no-version",
    ),
    pytest.param(
        b'{"v": 2, "purpose": NaN}',
        RejectionCode.INVALID_REQUEST,
        422,
        id="strict-json-is-checked-before-the-version",
    ),
    pytest.param(
        b'{"v": 2, "v": 2}',
        RejectionCode.INVALID_REQUEST,
        422,
        id="a-member-named-twice-is-checked-before-the-version",
    ),
    *(
        pytest.param(
            json.dumps(run_body(purpose=purpose)).encode(),
            RejectionCode.INVALID_REQUEST,
            422,
            id=f"purpose-outside-the-grammar-{purpose!r}",
        )
        for purpose in MALFORMED_PURPOSES
    ),
]


@pytest.mark.parametrize(("content", "code", "status"), BODY_REJECTIONS)
def test_a_body_that_is_not_an_allowed_run_request_is_refused(service, content, code, status):
    response = post_run(service, run_headers(MINT.workload(), MINT.user()), content=content)

    assert_rejected(response, code, status)


def test_an_oversized_body_without_a_declared_length_is_refused_while_read(service):
    def chunks() -> Iterator[bytes]:
        yield b'{"v": 2, "purpose": "'
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


async def _asgi_status(app: FastAPI, headers: dict[str, str], body: bytes) -> int:
    pending = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict[str, object]:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    return await asgi_post_status(app, headers, receive)


def test_a_single_chunk_over_the_limit_is_refused_without_being_buffered():
    """Driven in-process: uvicorn hands the body over in socket-read pieces, never as one chunk."""
    chunk_size = 16 * MAX_REQUEST_BYTES
    chunk = b"x" * chunk_size
    app = create_app(service_config(), fetch_key_set=MINT.fetch_key_set)
    headers = run_headers(MINT.workload(), MINT.user())
    tracemalloc.start()
    try:
        status = anyio.run(_asgi_status, app, headers, chunk)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert status == 422
    assert peak < chunk_size // 2


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


@pytest.mark.parametrize("code", list(RejectionCode))
def test_every_rejection_code_answers_with_a_status_and_a_message(code):
    response = rejection(code)

    body = RunRejection.model_validate_json(response.body)
    assert (body.code, bool(body.message)) == (code, True)
    assert 400 <= response.status_code < 600


def test_a_capability_too_large_rejection_is_422_with_its_own_message_and_no_retry():
    response = rejection(RejectionCode.CAPABILITY_TOO_LARGE)

    body = RunRejection.model_validate_json(response.body)
    assert response.status_code == 422
    assert body.message == "This run's capability would exceed 8192 characters."
    assert response.headers["cache-control"] == "no-store"
    assert "www-authenticate" not in response.headers
    assert "retry-after" not in response.headers


OPUS = "claude/opus"
BOTH_RIGHTS = [SUBSCRIPTION_GROUP, API_GROUP, SONGMAKER_GROUP]
CLI_ON_SONNET = Cli("claude-main", LISTED_MODEL)


@pytest.mark.parametrize(
    ("groups", "request_model", "route"),
    [
        pytest.param(SUBSCRIBER, None, Cli("claude-main", None), id="O12-no-model"),
        pytest.param(BOTH_RIGHTS, OPUS, Api(API_ALIAS, OPUS), id="S3-both-rights-unlisted"),
    ],
)
def test_s3_a_run_is_routed_on_exactly_the_model_it_requests(groups, request_model, route):
    response, runners = _routed_run(service_config(), groups, model=request_model)

    assert _admitted_events(response)
    admitted = runners.cli.admitted + runners.api.admitted
    assert [run.route for run in admitted] == [route]


def test_o13_two_runs_of_one_purpose_each_run_the_model_they_ask_for_without_a_restart():
    admitted_second_run = []

    def second_run(base_url, headers):
        admitted_second_run.append(post_run(base_url, headers, run_body(model=OPUS)))

    first, runners = _routed_run(service_config(), BOTH_RIGHTS, afterwards=second_run, model=LISTED)

    assert _admitted_events(first)
    assert _admitted_events(admitted_second_run[0])
    admitted = runners.cli.admitted + runners.api.admitted
    assert [run.route for run in admitted] == [CLI_ON_SONNET, Api(API_ALIAS, OPUS)]


def _run_on_the_file(tmp_path, subscription_groups):
    return service_config_file(tmp_path, subscription_groups=subscription_groups)


def _post_both_rights(app, runners):
    with served(app) as base_url:
        response = post_run(
            base_url,
            run_headers(MINT.workload(), MINT.user(groups=BOTH_RIGHTS)),
            run_body(model=LISTED),
        )
    assert _admitted_events(response)
    return (runners.cli.admitted + runners.api.admitted)[-1].route


def _app_from(config_file):
    runners = Runners(cli=ScriptedRunner(CLI_SCRIPT), api=ScriptedRunner(API_SCRIPT))
    app = create_app(load_config(config_file), runners=runners, fetch_key_set=MINT.fetch_key_set)
    return app, runners


def test_x_the_file_is_read_at_startup_and_a_restart_reads_it_again(tmp_path):
    config_file = _run_on_the_file(tmp_path, [SUBSCRIPTION_GROUP])
    running, running_runners = _app_from(config_file)
    assert _post_both_rights(running, running_runners) == CLI_ON_SONNET  # X1

    edited = _run_on_the_file(tmp_path, ["/not-a-subscriber-group"])
    assert _post_both_rights(running, running_runners) == CLI_ON_SONNET  # X2
    assert _post_both_rights(*_app_from(edited)) == Api(API_ALIAS, LISTED)  # X3
