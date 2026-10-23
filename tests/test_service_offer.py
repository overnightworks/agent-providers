"""``GET /offer`` over a real socket: the models this person can run in this app.

Decision record 0001 (H5, the 23.10.2026 amendments), item #452. ``GET /offer`` is computed by
the decider ``POST /runs`` admission uses, so its oracle is ``POST /runs`` and never this
file's own logic: for each row of the drift table the offer is compared with what ``/runs``
decides for the same caller, once per model the configuration lists and once for a model it
does not. Test values: app ``songmaker``, the subscription and API groups of ``service_support``,
and one Claude profile listing ``sonnet`` and ``haiku``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from service_support import (
    API_ALIAS,
    API_GROUP,
    LISTED_MODEL,
    PINNED_CLAUDE_VERSION,
    PRESENTATOR_CLIENT,
    PROCESS_TOKEN,
    SONGMAKER_GROUP,
    SUBSCRIPTION_GROUP,
    UNLISTED_MODEL,
    ScriptedRunner,
    TokenMint,
    answer_of,
    assert_rejected,
    decoded_events,
    post_run,
    process_claude_cli,
    run_body,
    run_headers,
    served,
    service_config,
    with_listed_models,
    without_cli,
    without_profiles,
)
from webauth.https_client import KeySetFetchError

from agent_runtime.events import FinalEvent
from agent_runtime_client.wire import (
    Failed,
    FailureCode,
    Offer,
    OfferReason,
    OfferRoute,
    RejectionCode,
    RunRejection,
)
from agent_runtime_service.app import create_app
from agent_runtime_service.config import ServiceConfig
from agent_runtime_service.routing import Api, ServedRoute
from agent_runtime_service.runners import Runner, Runners

MINT = TokenMint()
ANSWER = (FinalEvent(text="done"),)
SONGMAKER_ADMIN_GROUP = "/apps/songmaker/admin"
LISTED_NAMES = (LISTED_MODEL, "haiku")
UNLISTED = f"claude/{UNLISTED_MODEL}"
SUBSCRIBER = [SUBSCRIPTION_GROUP, SONGMAKER_GROUP]
API_CALLER = [API_GROUP, SONGMAKER_GROUP]


def _full_config() -> ServiceConfig:
    return with_listed_models(service_config(), LISTED_NAMES)


def _listed_ids(config: ServiceConfig) -> list[str]:
    return sorted(
        f"{provider}/{name}"
        for provider, names in config.subscription_models().items()
        for name in names
    )


def _headers(groups: list[str]) -> dict[str, str]:
    return run_headers(MINT.workload(), MINT.user(groups=groups))


def _get_offer(base_url: str, groups: list[str]) -> httpx.Response:
    return httpx.get(f"{base_url}/offer", headers=_headers(groups), timeout=10)


def _runners(*, cli: bool, api: bool) -> Runners:
    return Runners(
        cli=ScriptedRunner(ANSWER) if cli else None, api=ScriptedRunner(ANSWER) if api else None
    )


@contextmanager
def _served(config: ServiceConfig, runners: Runners | None = None) -> Iterator[str]:
    app = create_app(config, runners=runners, fetch_key_set=MINT.fetch_key_set)
    with served(app) as base_url:
        yield base_url


def _the_offer(response: httpx.Response) -> Offer:
    assert response.status_code == 200, response.text
    return Offer.model_validate_json(response.content)


def test_a_subscriber_is_offered_the_listed_model_on_the_cli_route_as_ready():
    """Sentence 2, O6, O10: a subscriber's entry is the listed model, on cli, ready"""
    config = with_listed_models(service_config(), [LISTED_MODEL])
    with _served(config, _runners(cli=True, api=False)) as app:
        response = _get_offer(app, SUBSCRIBER)

    (entry,) = _the_offer(response).data
    assert (entry.id, entry.route, entry.ready, entry.reason) == (
        f"claude/{LISTED_MODEL}",
        OfferRoute.CLI,
        True,
        None,
    )


@dataclass(frozen=True)
class _Oracle:
    """``POST /runs`` on a twin app that has a recording runner on each route."""

    base_url: str
    runners: Runners

    def route_of(self, groups: list[str], model: str) -> ServedRoute | httpx.Response | None:
        """The route ``/runs`` admits ``model`` on, ``None`` if not enabled, else the refusal."""
        cli, api = self.runners.cli, self.runners.api
        before = (len(cli.admitted), len(api.admitted))
        response = post_run(self.base_url, _headers(groups), run_body(model=model))
        if response.status_code == 200:
            admitted = cli.admitted if len(cli.admitted) > before[0] else api.admitted
            return admitted[-1].route
        if RunRejection.model_validate_json(response.content).code is RejectionCode.NOT_ENABLED:
            return None
        return response


def _ready_according_to_runs(base_url: str, groups: list[str], model: str) -> bool:
    response = post_run(base_url, _headers(groups), run_body(model=model))
    terminal = decoded_events(response.content)[-1]
    if isinstance(terminal, Failed):
        assert terminal.code is FailureCode.PROVIDER_UNAVAILABLE
        return False
    return True


CALLERS = {
    "subscription": SUBSCRIBER,
    "api": API_CALLER,
    "both": [SUBSCRIPTION_GROUP, API_GROUP, SONGMAKER_GROUP],
    "member-without-an-ai-group": [SONGMAKER_GROUP],
    "look-alike-app": [SUBSCRIPTION_GROUP, "/apps/songmaker-other"],
}
CONFIGS = {
    "full": _full_config,
    "listed-name-without-a-profile": lambda: without_profiles(_full_config()),
    "no-cli": lambda: without_cli(_full_config()),
}
RUNNER_SETS = {
    "no-runner": (False, False),
    "cli-runner": (True, False),
    "api-runner": (False, True),
    "both-runners": (True, True),
}


@pytest.mark.parametrize("runner_set", RUNNER_SETS)
@pytest.mark.parametrize("config_name", CONFIGS)
@pytest.mark.parametrize("caller", CALLERS)
def test_the_offer_equals_what_runs_decides_for_the_same_caller(caller, config_name, runner_set):
    """Sentence 1, O2, O3, O14: the drift table"""
    groups, config = CALLERS[caller], CONFIGS[config_name]()
    cli, api = RUNNER_SETS[runner_set]
    twin_runners = _runners(cli=True, api=True)
    with (
        _served(config, _runners(cli=cli, api=api)) as row_app,
        _served(config, twin_runners) as twin_app,
    ):
        oracle = _Oracle(twin_app, twin_runners)
        decided = {model: oracle.route_of(groups, model) for model in _listed_ids(config)}
        unlisted_decision = oracle.route_of(groups, UNLISTED)
        refusals = [
            d for d in (*decided.values(), unlisted_decision) if isinstance(d, httpx.Response)
        ]
        offer_response = _get_offer(row_app, groups)
        if refusals:
            assert answer_of(offer_response) == answer_of(refusals[0])
            return
        expected = {
            model: (
                OfferRoute(type(served_route).__name__.lower()),
                _ready_according_to_runs(row_app, groups, model),
            )
            for model, served_route in decided.items()
            if served_route is not None
        }

    offered = {entry.id: (entry.route, entry.ready) for entry in _the_offer(offer_response).data}
    assert offered == expected
    if isinstance(unlisted_decision, Api):
        assert UNLISTED not in offered


@pytest.mark.parametrize(
    ("groups", "cli", "api", "route", "reason"),
    [
        pytest.param(SUBSCRIBER, False, False, OfferRoute.CLI, OfferReason.NOT_BUILT, id="2-cli"),
        pytest.param(API_CALLER, False, False, OfferRoute.API, OfferReason.NOT_BUILT, id="3-api"),
        pytest.param(API_CALLER, False, True, OfferRoute.API, None, id="3a-api-runner"),
    ],
)
def test_an_entry_is_ready_exactly_when_a_runner_serves_its_route(groups, cli, api, route, reason):
    config = with_listed_models(service_config(), [LISTED_MODEL])
    config = without_profiles(config) if route is OfferRoute.API else config
    with _served(config, _runners(cli=cli, api=api)) as app:
        (entry,) = _the_offer(_get_offer(app, groups)).data

    assert (entry.route, entry.ready, entry.reason) == (route, reason is None, reason)


def test_a_member_without_an_ai_group_gets_an_empty_list_not_not_enabled():
    """O4"""
    with _served(_full_config(), _runners(cli=True, api=True)) as app:
        response = _get_offer(app, [SONGMAKER_GROUP])

    assert _the_offer(response).data == []


def test_the_offer_has_the_shape_of_a_models_list():
    """Sentence 5, O10"""
    with _served(_full_config(), _runners(cli=True, api=False)) as app:
        body = json.loads(_get_offer(app, SUBSCRIBER).content)

    assert body == {
        "v": 2,
        "object": "list",
        "data": [
            {
                "id": f"claude/{name}",
                "object": "model",
                "created": 0,
                "owned_by": "claude",
                "route": "cli",
                "ready": True,
                "reason": None,
            }
            for name in sorted(LISTED_NAMES)
        ],
    }


def test_a_standard_models_reader_finds_the_ids():
    """Sentence 5"""
    with _served(_full_config(), _runners(cli=True, api=False)) as app:
        response = _get_offer(app, SUBSCRIBER)

    assert [model["id"] for model in json.loads(response.content)["data"]] == _listed_ids(
        _full_config()
    )
    assert _the_offer(response).v == 2


def test_the_offer_is_never_cached():
    """O5"""
    with _served(_full_config(), _runners(cli=True, api=False)) as app:
        assert _get_offer(app, SUBSCRIBER).headers["cache-control"] == "no-store"
        refused = _get_offer(app, [SUBSCRIPTION_GROUP])

    assert_rejected(refused, RejectionCode.FORBIDDEN, 403)


class _ChangingRunners(Runners):
    def __init__(self, installed: list[Runner | None]) -> None:
        super().__init__()
        self._installed = installed

    def for_route(self, served_route: ServedRoute) -> Runner | None:
        return self._installed[0]


def test_the_offer_is_computed_on_every_ask():
    """O5"""
    installed: list[Runner | None] = [ScriptedRunner(ANSWER)]
    with _served(_full_config(), _ChangingRunners(installed)) as app:
        first = _the_offer(_get_offer(app, SUBSCRIBER))
        installed[0] = None
        second = _the_offer(_get_offer(app, SUBSCRIBER))

    assert {entry.ready for entry in first.data} == {True}
    assert {entry.reason for entry in second.data} == {OfferReason.NOT_BUILT}


def test_the_offer_never_opens_the_login_file(tmp_path: Path):
    """O6"""
    token_file = tmp_path / "claude.token"
    token_file.write_text(PROCESS_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    config = with_listed_models(service_config(token_file=token_file), [LISTED_MODEL])
    token_file.unlink()
    with _served(config, _runners(cli=True, api=False)) as app:
        response = _get_offer(app, SUBSCRIBER)

    (entry,) = _the_offer(response).data
    assert entry.ready


def _no_workload_token() -> dict[str, str]:
    return run_headers(None, MINT.user(groups=SUBSCRIBER))


def _no_user_token() -> dict[str, str]:
    return run_headers(MINT.workload(), None)


def _malformed_user_token() -> dict[str, str]:
    return run_headers(MINT.workload(), "not.a.jwt")


def _user_of_another_app() -> dict[str, str]:
    return run_headers(MINT.workload(), MINT.user(azp=PRESENTATOR_CLIENT))


def _user_outside_the_app() -> dict[str, str]:
    return run_headers(MINT.workload(), MINT.user(groups=[SUBSCRIPTION_GROUP]))


@pytest.mark.parametrize(
    ("headers", "code", "status"),
    [
        pytest.param(_no_workload_token, RejectionCode.UNAUTHORIZED, 401, id="15-no-workload"),
        pytest.param(_no_user_token, RejectionCode.UNAUTHORIZED, 401, id="16-no-user-token"),
        pytest.param(_malformed_user_token, RejectionCode.UNAUTHORIZED, 401, id="16-malformed"),
        pytest.param(_user_of_another_app, RejectionCode.FORBIDDEN, 403, id="17-another-app"),
        pytest.param(_user_outside_the_app, RejectionCode.FORBIDDEN, 403, id="17-non-member"),
    ],
)
def test_the_offer_refuses_tokens_as_runs_does(headers, code, status):
    with _served(_full_config(), _runners(cli=True, api=False)) as app:
        offer_response = httpx.get(f"{app}/offer", headers=headers(), timeout=10)
        runs_response = post_run(app, headers())

    assert_rejected(offer_response, code, status)
    assert answer_of(offer_response) == answer_of(runs_response)


def test_an_unfetchable_key_set_answers_unavailable_as_runs_does():
    """O7"""
    def unreachable_issuer(_url: str) -> bytes:
        raise KeySetFetchError("the issuer cannot be reached")

    app = create_app(_full_config(), fetch_key_set=unreachable_issuer)
    with served(app) as base_url:
        offer_response = httpx.get(f"{base_url}/offer", headers=_headers(SUBSCRIBER), timeout=10)
        runs_response = post_run(base_url, _headers(SUBSCRIBER))

    assert_rejected(offer_response, RejectionCode.UNAVAILABLE, 503)
    assert answer_of(offer_response) == answer_of(runs_response)


def test_a_plain_member_and_an_admin_are_both_answered():
    """O8"""
    with _served(_full_config(), _runners(cli=True, api=False)) as app:
        member = _the_offer(_get_offer(app, SUBSCRIBER))
        admin = _the_offer(_get_offer(app, [SUBSCRIPTION_GROUP, SONGMAKER_ADMIN_GROUP]))

    assert member == admin
    assert len(member.data) == len(LISTED_NAMES)


def test_the_offer_is_for_the_asking_person():
    """O9"""
    with _served(_full_config(), _runners(cli=True, api=False)) as app:
        subscriber = _the_offer(_get_offer(app, SUBSCRIBER))
        api_caller = _the_offer(_get_offer(app, API_CALLER))

    assert {entry.route for entry in subscriber.data} == {OfferRoute.CLI}
    assert {entry.route for entry in api_caller.data} == {OfferRoute.API}


def test_the_offer_answers_get_only():
    """Sentence 1 (cell 23)"""
    with _served(_full_config(), _runners(cli=True, api=False)) as app:
        headers = _headers(SUBSCRIBER)
        posted = httpx.post(f"{app}/offer", headers=headers, timeout=10)
        below = httpx.get(f"{app}/offer/anything", headers=headers, timeout=10)
        head = httpx.head(f"{app}/offer", headers=headers, timeout=10)

    assert (posted.status_code, below.status_code, head.status_code) == (405, 404, 405)


@pytest.mark.parametrize(
    ("app_group", "status"),
    [
        pytest.param(SONGMAKER_GROUP, 200, id="24-the-app"),
        pytest.param("/apps/songmaker/team", 200, id="24-a-child"),
        pytest.param("/apps/songmaker-other", 403, id="24-a-look-alike"),
    ],
)
def test_the_gate_holds_for_the_offer(app_group, status):
    with _served(_full_config(), _runners(cli=True, api=False)) as app:
        response = _get_offer(app, [SUBSCRIPTION_GROUP, app_group])

    assert response.status_code == status


def test_no_answer_shows_a_token_a_path_or_the_api_alias():
    """Sentence 2 (no leak)"""
    needles = [PROCESS_TOKEN, API_ALIAS, str(process_claude_cli()), PINNED_CLAUDE_VERSION]
    answers: list[httpx.Response] = []
    with _served(_full_config(), _runners(cli=False, api=False)) as app:
        for groups in (SUBSCRIBER, API_CALLER, [SUBSCRIPTION_GROUP]):
            headers = _headers(groups)
            needles += [headers["Agent-Workload-Token"], headers["Authorization"].split()[1]]
            answers.append(httpx.get(f"{app}/offer", headers=headers, timeout=10))

    assert [needle for response in answers for needle in needles if needle in response.text] == []
