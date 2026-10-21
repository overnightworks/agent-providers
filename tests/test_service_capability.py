"""A run's capability: its claims and expiry, its signature, the 8,192-character guard, and
its admission: each admitted run of an app with an MCP origin hands its runner one capability,
an oversized one is refused, and no frame, answer or log line carries it."""

from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import httpx
import pytest
from capability_support import capability_carrying, constructed_token
from jwt.algorithms import RSAAlgorithm
from provider_test_support import formatted_log_text
from service_support import (
    API_GROUP,
    COWRITER,
    LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT,
    LONGEST_PURPOSE,
    MALFORMED_PURPOSES,
    PRESENTATOR_CLIENT,
    PRESENTER,
    SONGMAKER_GROUP,
    SONGMAKER_ORIGIN,
    TEST_ISSUER,
    ScriptedRunner,
    TokenMint,
    WallClock,
    answer_of,
    assert_rejected,
    capable_service_config,
    decoded_events,
    everything_a_refusal_holds,
    failure_lines,
    get_run,
    pem_of,
    plain_service_config,
    post_run,
    reaches_text,
    record_signed_tokens,
    run_body,
    run_headers,
    served,
    tool_names_with_the_last_of_length,
    with_placeholder_run_id,
)
from webauth.https_client import KeySetFetchError
from webauth.jwt_verification import (
    MAX_TOKEN_CHARACTERS,
    JwtVerifier,
    Rejected,
    RejectionReason,
    VerifiedClaims,
)

from agent_client.capability import (
    CAPABILITY_ISSUER,
    CAPABILITY_TOKEN_TYPE,
    MAX_CAPABILITY_CHARACTERS,
    CapabilityClaims,
)
from agent_client.wire import MAX_PURPOSE_LENGTH, Failed, FailureCode, RejectionCode, RunStarted
from agent_providers.events import AssistantTextEvent, FinalEvent
from agent_service.app import create_app
from agent_service.capability import (
    PREFLIGHT_ISSUED_AT,
    PREFLIGHT_PURPOSE,
    PREFLIGHT_SUBJECT,
    CapabilityScope,
    CapabilityTooLarge,
    capability_claims,
    preflight_claims,
    signed_capability,
)
from agent_service.config import ServiceConfig
from agent_service.runners import AdmittedRun, Runners
from agent_service.signing import SigningKey

RUN_ID = uuid.UUID("8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01")
NOW_IN_2026 = 1_792_152_000.0


def _scope(tmp_path: Path, **songmaker: object) -> CapabilityScope:
    scope = capable_service_config(tmp_path, songmaker=songmaker).capability_scope("songmaker")
    assert scope is not None
    return scope


def _claims_of(scope: CapabilityScope, **overrides: object) -> CapabilityClaims:
    values = {
        "run_id": RUN_ID,
        "app": "songmaker",
        "issuer": TEST_ISSUER,
        "subject": "user-alice",
        "purpose": COWRITER,
        "now": NOW_IN_2026,
        "max_duration_s": 600.0,
        **overrides,
    }
    return capability_claims(scope, **values)


@pytest.mark.parametrize(
    ("now", "max_duration_s", "issued_at", "expires_at"),
    [
        pytest.param(1_792_152_000.7, 600.0, 1_792_152_000, 1_792_152_600, id="whole-maximum"),
        pytest.param(1_792_152_000.9, 300.5, 1_792_152_000, 1_792_152_301, id="fractional-maximum"),
    ],
)
def test_a_capability_is_issued_at_the_floored_now_and_expires_at_the_floored_end(
    tmp_path, now, max_duration_s, issued_at, expires_at
):
    claims = _claims_of(_scope(tmp_path), now=now, max_duration_s=max_duration_s)

    assert (claims.iat, claims.exp) == (issued_at, expires_at)


@pytest.mark.parametrize(
    ("songmaker", "tools"),
    [
        pytest.param({}, ("edit_song", "read_song"), id="the-keys-of-the-table-sorted"),
        pytest.param({"tools": {}}, (), id="an-empty-table-grants-nothing"),
    ],
)
def test_a_capability_says_who_where_what_and_which_tools(tmp_path, songmaker, tools):
    claims = _claims_of(_scope(tmp_path, **songmaker))

    assert claims == CapabilityClaims(
        iss=CAPABILITY_ISSUER,
        aud=SONGMAKER_ORIGIN,
        sub=str(RUN_ID),
        iat=1_792_152_000,
        exp=1_792_152_600,
        app="songmaker",
        purpose=COWRITER,
        principal={"iss": TEST_ISSUER, "sub": "user-alice"},
        tools=tools,
    )


def _verifier_of(key: SigningKey, wall_clock_reading: float, audience: str) -> JwtVerifier:
    key_set = {
        "keys": [
            {
                **RSAAlgorithm.to_jwk(key.public_key, as_dict=True),
                "kid": key.kid,
                "use": "sig",
                "alg": "RS256",
            }
        ]
    }
    return JwtVerifier(
        issuer=CAPABILITY_ISSUER,
        audiences=[audience],
        jwks_url="https://keys.test/jwks",
        leeway_seconds=0,
        fetch_key_set=lambda _url: json.dumps(key_set).encode(),
        wall_clock=lambda: wall_clock_reading,
        monotonic_clock=lambda: 0.0,
    )


def _header_of(token: str) -> dict[str, object]:
    header = token.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))


def test_a_signed_capability_has_exactly_the_capability_header(tmp_path):
    scope = _scope(tmp_path)

    capability = signed_capability(scope, _claims_of(scope))

    assert _header_of(capability.token) == {
        "alg": "RS256",
        "kid": scope.key.kid,
        "typ": CAPABILITY_TOKEN_TYPE,
    }


@pytest.mark.parametrize(
    "subject",
    [
        "user-alice",
        "user\x00alice",
        "user-\U0001f3b5",
        "u" * 600,
        '"' * 255,
        " User-Alice ",
        "Stra\u00dfe",
        "e\u0301",
        "caf\u00e9",
        "\ufb01",
    ],
    ids=[
        "ascii",
        "nul",
        "astral",
        "600-characters",
        "255-quotation-marks",
        "padded-and-capitalised",
        "sharp-s",
        "decomposed-e",
        "precomposed-e",
        "fi-ligature",
    ],
)
def test_a_verifier_accepts_a_capability_until_it_expires_with_the_subject_unchanged(
    tmp_path, subject
):
    scope = _scope(tmp_path)
    claims = _claims_of(scope, subject=subject)
    token = signed_capability(scope, claims).token

    verdict = _verifier_of(scope.key, claims.exp - 0.1, SONGMAKER_ORIGIN).verify(token)

    assert isinstance(verdict, VerifiedClaims)
    assert CapabilityClaims.from_claims(verdict.claims) == claims
    assert verdict.claims["principal"]["sub"] == subject


def test_a_verifier_refuses_a_capability_at_its_expiry_and_for_another_origin(tmp_path):
    scope = _scope(tmp_path)
    claims = _claims_of(scope)
    token = signed_capability(scope, claims).token

    assert _verifier_of(scope.key, claims.exp, SONGMAKER_ORIGIN).verify(token) == Rejected(
        RejectionReason.EXPIRED
    )
    assert _verifier_of(scope.key, claims.exp - 0.1, "https://other.internal").verify(
        token
    ) == Rejected(RejectionReason.INVALID_AUDIENCE)


def test_the_preflight_claims_reserve_the_subject_and_the_widest_timestamps(tmp_path):
    scope = _scope(tmp_path)

    claims = preflight_claims(scope, app="songmaker", issuer=TEST_ISSUER, max_duration_s=600.5)

    assert PREFLIGHT_SUBJECT == '"' * 255
    assert (claims.principal.sub, claims.iat, claims.exp) == (
        PREFLIGHT_SUBJECT,
        9_999_999_999,
        10_000_000_599,
    )
    assert len(claims.sub) == 36
    assert len(PREFLIGHT_PURPOSE) == MAX_PURPOSE_LENGTH
    assert (claims.aud, claims.app, claims.purpose, claims.tools) == (
        SONGMAKER_ORIGIN,
        "songmaker",
        PREFLIGHT_PURPOSE,
        ("edit_song", "read_song"),
    )


def test_a_token_with_the_preflight_claims_is_refused_as_issued_in_the_future_today(tmp_path):
    scope = _scope(tmp_path)
    claims = preflight_claims(scope, app="songmaker", issuer=TEST_ISSUER, max_duration_s=600.0)
    token = signed_capability(scope, claims).token

    verdict = _verifier_of(scope.key, NOW_IN_2026, SONGMAKER_ORIGIN).verify(token)

    assert verdict == Rejected(RejectionReason.ISSUED_IN_FUTURE)
    assert PREFLIGHT_ISSUED_AT > NOW_IN_2026


def test_the_capability_limit_is_the_limit_webauths_verifier_enforces():
    assert MAX_CAPABILITY_CHARACTERS == MAX_TOKEN_CHARACTERS


def _scope_with_the_last_tool_named_to(
    scope: CapabilityScope, last_tool_length: int
) -> CapabilityScope:
    tools = tool_names_with_the_last_of_length(last_tool_length)
    return CapabilityScope(scope.origin, tuple(sorted(tools)), scope.key)


def _preflight_token_of(scope: CapabilityScope) -> str:
    claims = preflight_claims(scope, app="songmaker", issuer=TEST_ISSUER, max_duration_s=600.0)
    return signed_capability(scope, claims).token


def test_a_capability_of_exactly_8192_characters_is_signed(tmp_path):
    scope = _scope_with_the_last_tool_named_to(_scope(tmp_path), LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT)

    assert len(_preflight_token_of(scope)) == MAX_CAPABILITY_CHARACTERS


def test_a_capability_of_8193_characters_is_refused_with_its_length(tmp_path):
    scope = _scope_with_the_last_tool_named_to(
        _scope(tmp_path), LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT + 1
    )

    with pytest.raises(CapabilityTooLarge) as refusal:
        _preflight_token_of(scope)

    assert refusal.value.length == MAX_CAPABILITY_CHARACTERS + 1


def test_a_refused_oversized_capability_holds_no_token_anywhere(tmp_path, monkeypatch):
    signed = record_signed_tokens(monkeypatch)
    scope = _scope_with_the_last_tool_named_to(
        _scope(tmp_path), LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT + 1
    )

    with pytest.raises(CapabilityTooLarge) as refusal:
        _preflight_token_of(scope)

    assert signed
    assert not reaches_text(everything_a_refusal_holds(refusal.value), signed)


def test_no_representation_of_a_capability_or_its_scope_shows_the_token_or_the_key(tmp_path):
    scope = _scope(tmp_path)
    capability = signed_capability(scope, _claims_of(scope))
    shown = [repr(capability), str(capability), repr(scope), str(scope)]
    secret_lines = pem_of(scope.key.private_key).decode().splitlines()

    assert not any(capability.token in text for text in shown)
    assert not any(line in text for text in shown for line in secret_lines)
    assert not any("PRIVATE KEY" in text for text in shown)


def _loaded_recording_the_signed_tokens(
    tmp_path, monkeypatch, caplog
) -> tuple[ServiceConfig, list[str]]:
    signed = record_signed_tokens(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        config = capable_service_config(tmp_path)
    return config, signed


def test_loading_a_configuration_keeps_no_measurement_token_anywhere(tmp_path, monkeypatch, caplog):
    config, measurement_tokens = _loaded_recording_the_signed_tokens(tmp_path, monkeypatch, caplog)

    formatted = [logging.Formatter().format(record) for record in caplog.records]
    assert measurement_tokens
    assert not reaches_text(config, measurement_tokens)
    for token in measurement_tokens:
        assert not any(token in text for text in formatted)
        assert token not in repr(config)


MINT = TokenMint()
SCRIPT = (AssistantTextEvent(text="hello"), FinalEvent(text="done"))
TUNED_AT_THE_LIMIT = {
    "tools": dict.fromkeys(
        tool_names_with_the_last_of_length(LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT), "read_only"
    )
}
QUOTATION_MARKS_255 = '"' * 255
QUOTATION_MARKS_256 = '"' * 256
YEAR_2286 = datetime.fromtimestamp(10_000_000_000, UTC)


@dataclass
class _Service:
    base_url: str
    config: ServiceConfig
    runners: Runners
    clock: WallClock
    signed: list[str]

    @property
    def scope(self) -> CapabilityScope:
        scope = self.config.capability_scope("songmaker")
        assert scope is not None
        return scope

    def verify(self, token: str, *, at: float, audience: str = SONGMAKER_ORIGIN):
        return _verifier_of(self.scope.key, at, audience).verify(token)

    def run(self, user: str | None = None, *, workload: str | None = None, **body: object):
        headers = run_headers(workload or MINT.workload(), user or MINT.user())
        return post_run(self.base_url, headers, run_body(**body))

    @property
    def cli(self) -> ScriptedRunner:
        return self.runners.cli

    @property
    def api(self) -> ScriptedRunner:
        return self.runners.api

    def capability_of_last_cli_run(self):
        capability = self.cli.admitted[-1].capability
        assert capability is not None
        return capability


ServingFactory = Callable[..., Iterator[_Service]]


@pytest.fixture
def serving(tmp_path, monkeypatch, caplog) -> ServingFactory:
    """Serve the real app with ``capable_service_config``; uvicorn's records reach ``caplog``.

    uvicorn does not propagate its records, and it resets its logger's handlers
    when the server is configured, so the handler joins after the server is up.
    Configuring a second server strips the handler of the first, so a test that reads
    logs serves one app at a time and never nests ``serving``.
    """
    caplog.set_level(logging.DEBUG)

    @contextmanager
    def serve(
        *,
        songmaker: dict[str, object] | None = None,
        runners: Runners | None = None,
        fetch_key_set=MINT.fetch_key_set,
        capable: bool = True,
    ) -> Iterator[_Service]:
        config = (
            capable_service_config(tmp_path, songmaker=songmaker)
            if capable
            else plain_service_config()
        )
        signed = record_signed_tokens(monkeypatch)
        clock = WallClock()
        installed = runners or Runners(cli=ScriptedRunner(SCRIPT), api=ScriptedRunner(SCRIPT))
        app = create_app(config, runners=installed, fetch_key_set=fetch_key_set, wall_clock=clock)
        uvicorn_logger = logging.getLogger("uvicorn")
        with served(app) as base_url:
            uvicorn_logger.addHandler(caplog.handler)
            try:
                yield _Service(base_url, config, installed, clock, signed)
            finally:
                uvicorn_logger.removeHandler(caplog.handler)

    return serve


def _segment_of(token: str) -> str:
    return token.rsplit(".", 1)[1]


def _run_id_of(response: httpx.Response) -> uuid.UUID:
    started = decoded_events(response.content)[0]
    assert isinstance(started, RunStarted)
    return started.run_id


def _assert_no_token_in_any_log(caplog, tokens: list[str]) -> None:
    assert tokens
    logged = [formatted_log_text(caplog.records), caplog.text]
    for token in tokens:
        assert not any(token in text or _segment_of(token) in text for text in logged)
    uvicorn_errors = [
        record
        for record in caplog.records
        if record.name == "uvicorn.error" and record.levelno >= logging.ERROR
    ]
    assert uvicorn_errors == []


def _claims_for(admitted: AdmittedRun, subject: str, **overrides: object) -> CapabilityClaims:
    values = {
        "iss": CAPABILITY_ISSUER,
        "aud": SONGMAKER_ORIGIN,
        "sub": str(admitted.run_id),
        "iat": 1_792_152_000,
        "exp": 1_792_152_600,
        "app": "songmaker",
        "purpose": COWRITER,
        "principal": {"iss": TEST_ISSUER, "sub": subject},
        "tools": ("edit_song", "read_song"),
        **overrides,
    }
    return CapabilityClaims.model_validate(values)


BOB_ON_THE_API_ROUTE = {"groups": [SONGMAKER_GROUP, API_GROUP], "sub": "user-bob"}


@pytest.mark.parametrize(
    ("route", "user_claims", "subject"),
    [
        pytest.param("cli", {}, "user-alice", id="cli-route-alice"),
        pytest.param("api", BOB_ON_THE_API_ROUTE, "user-bob", id="api-route-bob"),
    ],
)
def test_the_runner_receives_a_capability_that_verifies_with_the_callers_principal(
    serving, route, user_claims, subject
):
    with serving() as service:
        service.run(MINT.user(**user_claims))

        admitted = getattr(service, route).admitted[-1]
        assert admitted.capability is not None
        verdict = service.verify(admitted.capability.token, at=1_792_152_000.0)
        assert isinstance(verdict, VerifiedClaims)
        claims = _claims_for(admitted, subject)
        assert CapabilityClaims.from_claims(verdict.claims) == claims
        assert admitted.capability.claims == claims
        assert admitted.principal.subject == subject


def test_a_purpose_of_another_apps_agents_changes_only_the_label_of_the_capability(serving):
    with serving() as service:
        service.run()
        service.run(purpose=PRESENTER)

        own, relabelled = (admitted.capability.claims for admitted in service.cli.admitted)
        assert relabelled.purpose == PRESENTER
        assert (relabelled.aud, relabelled.app, relabelled.principal, relabelled.tools) == (
            own.aud,
            own.app,
            own.principal,
            own.tools,
        )


def test_two_runs_get_two_capabilities_each_bound_to_its_own_run_id_and_signed_by_one_key(serving):
    with serving() as service:
        run_ids = [_run_id_of(service.run()) for _ in range(2)]

        capabilities = [admitted.capability for admitted in service.cli.admitted]
        assert [capability.claims.sub for capability in capabilities] == [str(i) for i in run_ids]
        assert run_ids[0] != run_ids[1]
        assert len({capability.token for capability in capabilities}) == 2
        assert {_header_of(capability.token)["kid"] for capability in capabilities} == {
            service.scope.key.kid
        }


def test_the_capability_and_the_run_record_read_the_one_wall_clock(serving):
    with serving() as service:
        service.clock.advance(0.7)

        run_id = _run_id_of(service.run())

        claims = service.capability_of_last_cli_run().claims
        assert (claims.iat, claims.exp) == (1_792_152_000, 1_792_152_600)
        status = get_run(service.base_url, run_headers(MINT.workload(), MINT.user()), str(run_id))
        assert status.json()["started_at"].startswith("2026-10-16T12:00:00.7")


def test_a_capability_outlives_its_run_until_its_expiry_and_no_longer(serving):
    with serving() as service:
        events = decoded_events(service.run().content)
        token = service.capability_of_last_cli_run().token
        expires_at = service.capability_of_last_cli_run().claims.exp

        assert events[-1].type == "succeeded"
        assert isinstance(service.verify(token, at=expires_at - 0.1), VerifiedClaims)
        assert service.verify(token, at=expires_at) == Rejected(RejectionReason.EXPIRED)


def test_an_app_without_an_origin_gets_no_capability_and_signs_nothing(serving):
    with serving() as service, serving(capable=False) as plain:
        run = {"purpose": PRESENTER}
        headers = {
            "workload": MINT.workload(client=PRESENTATOR_CLIENT),
            "user": MINT.user(azp=PRESENTATOR_CLIENT, groups=["/apps/presentator", API_GROUP]),
        }
        with_origins = service.run(**headers, **run)
        without_origins = plain.run(**headers, **run)

        assert service.api.admitted[-1].capability is None
        assert service.signed == []
        assert with_placeholder_run_id(with_origins.content) == with_placeholder_run_id(
            without_origins.content
        )


def test_the_production_path_without_runners_mints_one_capability_and_streams_as_without_origins(
    serving,
):
    with serving(runners=Runners()) as service, serving(capable=False, runners=Runners()) as plain:
        with_origin = service.run()
        without_origin = plain.run()

        events = decoded_events(with_origin.content)
        assert [event.type for event in events] == ["run_started", "failed"]
        assert isinstance(events[-1], Failed)
        assert events[-1].code is FailureCode.PROVIDER_UNAVAILABLE
        assert len(service.signed) == 1
        assert with_placeholder_run_id(with_origin.content) == with_placeholder_run_id(
            without_origin.content
        )


def _unreachable_issuer(_url: str) -> bytes:
    raise KeySetFetchError("the issuer cannot be reached")


@pytest.mark.parametrize(
    ("fetch_key_set", "user_claims", "body"),
    [
        pytest.param(MINT.fetch_key_set, {"sub": None}, {}, id="unauthorized"),
        pytest.param(_unreachable_issuer, {}, {}, id="unavailable"),
        pytest.param(MINT.fetch_key_set, {"groups": []}, {}, id="not-enabled"),
        pytest.param(MINT.fetch_key_set, {}, {"v": 2}, id="unsupported-version"),
        pytest.param(MINT.fetch_key_set, {}, {"turn": {"system": "x", "messages": []}}, id="shape"),
    ],
)
def test_a_refusal_before_the_capability_reaches_no_runner_signs_nothing_and_answers_alike(
    serving, fetch_key_set, user_claims, body
):
    with serving(fetch_key_set=fetch_key_set) as service:
        with serving(capable=False, fetch_key_set=fetch_key_set) as plain:
            user = MINT.user(**user_claims)
            with_origin = service.run(user, **body)
            without_origin = plain.run(user, **body)

            assert answer_of(with_origin) == answer_of(without_origin)
            assert with_origin.status_code in {400, 401, 403, 422, 503}
            assert service.cli.admitted == service.api.admitted == []
            assert service.signed == []


def _on_the_tuned_app(serving):
    return serving(songmaker=TUNED_AT_THE_LIMIT)


def test_a_capability_over_8192_characters_is_refused_before_any_runner_stream_or_record(
    serving, monkeypatch, caplog
):
    pinned = uuid.uuid4()
    monkeypatch.setattr("agent_service.app.uuid.uuid4", lambda: pinned)
    user = MINT.user(sub=QUOTATION_MARKS_256)
    with _on_the_tuned_app(serving) as service:
        refusal = service.run(user, purpose=LONGEST_PURPOSE)

        assert_rejected(refusal, RejectionCode.CAPABILITY_TOO_LARGE, 422)
        assert refusal.headers["content-type"] == "application/json"
        assert service.cli.admitted == service.api.admitted == []
        (token,) = service.signed
        answered = [refusal.text, *refusal.headers.values()]
        assert not any(token in text or _segment_of(token) in text for text in answered)
        status = get_run(service.base_url, run_headers(MINT.workload(), user), str(pinned))
        assert_rejected(status, RejectionCode.NOT_FOUND, 404)
    (warning,) = [record for record in caplog.records if record.name == "agent_service.app"]
    assert warning.levelno == logging.WARNING
    assert warning.exc_info is None
    assert warning.args == ("songmaker", len(token), MAX_CAPABILITY_CHARACTERS)
    assert len(token) == MAX_CAPABILITY_CHARACTERS + 1
    logged = formatted_log_text(caplog.records)
    assert token not in logged
    assert QUOTATION_MARKS_256 not in logged


def test_a_capability_over_8192_characters_is_refused_at_a_wall_clock_past_2286(serving):
    with _on_the_tuned_app(serving) as service:
        service.clock.set_to(YEAR_2286)

        refusal = service.run(MINT.user(sub=QUOTATION_MARKS_255), purpose=LONGEST_PURPOSE)

        assert_rejected(refusal, RejectionCode.CAPABILITY_TOO_LARGE, 422)
        assert service.cli.admitted == []


def test_the_widest_admitted_capability_is_the_preflights_reserve_with_a_short_subject(serving):
    with _on_the_tuned_app(serving) as service:
        response = service.run(purpose=LONGEST_PURPOSE)

        assert response.status_code == 200
        assert len(service.capability_of_last_cli_run().token) == 7_524


@pytest.mark.parametrize(
    "subject",
    ["u" * 600, "user\x00alice", "user-\U0001f3b5"],
    ids=["600-characters", "nul", "astral"],
)
def test_a_verified_subject_of_any_valid_unicode_is_admitted_and_carried_exactly(serving, subject):
    with serving() as service:
        response = service.run(MINT.user(sub=subject))

        assert response.status_code == 200
        assert service.cli.admitted[-1].principal.subject == subject
        assert service.capability_of_last_cli_run().claims.principal.sub == subject


@pytest.mark.parametrize("client", ["songmaker", "presentator"])
@pytest.mark.parametrize("method", ["POST", "GET"])
def test_a_subject_with_a_lone_surrogate_is_unauthorized_for_every_app_and_never_a_500(
    serving, client, method
):
    workload_client = PRESENTATOR_CLIENT if client == "presentator" else "songmaker-test"
    headers = run_headers(
        MINT.workload(client=workload_client),
        MINT.user(azp=workload_client, sub="user-\ud800"),
    )
    with serving() as service:
        response = (
            post_run(service.base_url, headers)
            if method == "POST"
            else get_run(service.base_url, headers, str(uuid.uuid4()))
        )

        assert_rejected(response, RejectionCode.UNAUTHORIZED, 401)
        assert service.signed == []


def test_another_origin_does_not_verify_the_capability(serving):
    with serving() as service:
        service.run()

        verdict = service.verify(
            service.capability_of_last_cli_run().token,
            at=1_792_152_000.0,
            audience="https://presentator.internal",
        )

        assert verdict == Rejected(RejectionReason.INVALID_AUDIENCE)


def test_neither_repr_nor_str_of_an_admitted_run_shows_the_token(serving):
    with serving() as service:
        service.run()

        admitted = service.cli.admitted[-1]
        token = service.capability_of_last_cli_run().token
        assert admitted.capability is not None
        assert not any(token in text for text in (repr(admitted), str(admitted)))


def test_no_frame_header_or_status_body_or_log_of_an_admitted_run_carries_the_token(
    serving, caplog
):
    with serving() as service:
        user = MINT.user()
        response = service.run(user)
        run_id = _run_id_of(response)
        status = get_run(service.base_url, run_headers(MINT.workload(), user), str(run_id))

        (token,) = service.signed
        assert service.capability_of_last_cli_run().token == token
        for answer in (response, status):
            held = [answer.text, *answer.headers.values(), *answer.headers.keys()]
            assert not any(token in text or _segment_of(token) in text for text in held)
        _assert_no_token_in_any_log(caplog, service.signed)


class _ChainKind(Enum):
    IMPLICIT = "an implicit chain"
    EXPLICIT = "an explicit chain"


def _raise_token_bearing_error(admitted: AdmittedRun, chain: _ChainKind) -> None:
    """Raise a ``RuntimeError`` whose message and note hold the token, chained as ``chain`` says.

    Implicit: raised inside the ``except`` of a genuinely raised ``KeyError(token)``, without
    ``from``, so ``__context__`` holds the token and ``__suppress_context__`` is False.
    Explicit: ``from`` a genuinely raised and caught ``ValueError(segment)``, so ``__cause__``
    holds the signature segment, carries a traceback and ``__suppress_context__`` is True.
    """
    token = admitted.capability.token
    error = RuntimeError(token)
    error.add_note(token)
    if chain is _ChainKind.IMPLICIT:
        try:
            raise KeyError(token)
        except KeyError:
            raise error
    try:
        raise ValueError(_segment_of(token))
    except ValueError as cause:
        raise error from cause


class _RunnerThatRaisesTheTokenIt:
    """A runner whose step raises, or whose close raises, an error that carries its capability."""

    def __init__(
        self, chain: _ChainKind, *, inside_a_group: bool = False, in_its_close: bool = False
    ) -> None:
        self._chain = chain
        self._inside_a_group = inside_a_group
        self._in_its_close = in_its_close

    def _fail(self, admitted: AdmittedRun) -> None:
        try:
            _raise_token_bearing_error(admitted, self._chain)
        except RuntimeError as error:
            if not self._inside_a_group:
                raise
            raise ExceptionGroup("the runner failed", [error]) from None

    async def run(self, admitted: AdmittedRun):
        if self._in_its_close:
            try:
                yield FinalEvent(text="done")
                yield AssistantTextEvent(text="never taken")
            finally:
                self._fail(admitted)
        self._fail(admitted)
        yield


_FAILING_RUNNER_CASES = [
    ("step-raises-the-token", {}, "The runner of run {} failed", "RuntimeError", "failed"),
    (
        "step-raises-the-token-in-a-group",
        {"inside_a_group": True},
        "The runner of run {} failed",
        "ExceptionGroup",
        "failed",
    ),
    (
        "close-raises-the-token",
        {"in_its_close": True},
        "Closing the runner of run {} failed",
        "RuntimeError",
        "succeeded",
    ),
]


@pytest.mark.parametrize(
    ("runner", "event", "kind", "terminal"),
    [
        pytest.param(
            _RunnerThatRaisesTheTokenIt(chain, **options),
            event,
            kind,
            terminal,
            id=f"{name}-through-{chain.value.replace(' ', '-')}",
        )
        for name, options, event, kind, terminal in _FAILING_RUNNER_CASES
        for chain in _ChainKind
    ],
)
def test_a_failing_runner_is_logged_by_its_class_and_the_token_reaches_no_log(
    serving, caplog, runner, event, kind, terminal
):
    with serving(runners=Runners(cli=runner)) as service:
        response = service.run()

        events = decoded_events(response.content)
        run_id = _run_id_of(response)
        assert events[-1].type == terminal
        assert [(line, name) for line, name, _raiser in failure_lines(caplog)] == [
            (event.format(run_id), kind)
        ]
        _assert_no_token_in_any_log(caplog, service.signed)


def test_nothing_logged_over_an_admitted_run_the_refusals_and_the_guard_carries_a_token(
    serving, caplog
):
    with _on_the_tuned_app(serving) as service:
        service.run()
        service.run(MINT.user(sub=QUOTATION_MARKS_256))
        service.run(MINT.user(sub=None))
        service.run(purpose=MALFORMED_PURPOSES[1])
        service.run(MINT.user(groups=[]))
        service.run(v=2)
        service.run(turn={"system": "x", "messages": []})
        signed = list(service.signed)
    with serving(fetch_key_set=_unreachable_issuer) as unreachable:
        assert unreachable.run().status_code == 503

    assert len(signed) == 2
    _assert_no_token_in_any_log(caplog, signed)


def _the_segment_in_json(token: str, _other: str) -> bytes:
    return json.dumps({"text": f"echo {_segment_of(token)} end"}).encode()


_SIGNATURE_WITH_DASHES_AND_UNDERSCORES = bytes(range(256)) + bytes(range(128))
_ANOTHER_SIGNATURE = bytes(reversed(_SIGNATURE_WITH_DASHES_AND_UNDERSCORES))


@pytest.mark.parametrize(
    ("body_of", "appears"),
    [
        pytest.param(lambda token, _other: token.encode(), True, id="K1a-the-whole-token"),
        pytest.param(_the_segment_in_json, True, id="K1b-the-signature-segment-inside-other-bytes"),
        pytest.param(
            lambda _token, other: _segment_of(other).encode(),
            False,
            id="K1c-another-tokens-signature-segment",
        ),
        pytest.param(
            lambda token, _other: token.rsplit(".", 1)[0].encode(),
            False,
            id="K1d-the-header-and-payload-alone",
        ),
    ],
)
def test_the_capability_appears_in_a_body_only_through_its_signature_segment(
    body_of: Callable[[str, str], bytes], appears: bool
) -> None:
    token = constructed_token(_SIGNATURE_WITH_DASHES_AND_UNDERSCORES)
    other = constructed_token(_ANOTHER_SIGNATURE)
    assert {"-", "_"} <= set(_segment_of(token))

    assert capability_carrying(token).appears_in(body_of(token, other)) is appears


def test_a_really_signed_capability_is_found_by_its_signature_segment_in_json_output(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    token = signed_capability(scope, _claims_of(scope)).token

    assert capability_carrying(token).appears_in(_the_segment_in_json(token, token))
