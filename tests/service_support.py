"""Shared arrangement for the agent service's suites: config, tokens, runners, a socket.

The service is built through ``create_app`` with the one injected seam the
verifier offers, its key-set fetch, so every test drives the real token
checks. Tokens minted here are signed by a throwaway RSA key whose key set the
fetch serves; the Keycloak suite uses the real issuer's tokens instead.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Final

import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from jwt.algorithms import RSAAlgorithm

from agent_client.run_stream import StreamViolation
from agent_client.sse import RunStreamDecoder
from agent_client.wire import RejectionCode, RunEvent, RunRejection
from agent_providers.events import StreamEvent
from agent_service.config import ServiceConfig
from agent_service.runners import AdmittedRun

TEST_ISSUER: Final = "https://keycloak.test/realms/agent-test"
TEST_JWKS_URL: Final = "https://keycloak.test/realms/agent-test/protocol/openid-connect/certs"
SONGMAKER_CLIENT: Final = "songmaker-test"
PRESENTATOR_CLIENT: Final = "presentator-test"
COWRITER: Final = "songmaker.cowriter"
PRESENTER: Final = "presentator.copresenter"
SUBSCRIPTION_GROUP: Final = "/ki-abo"
SONGMAKER_GROUP: Final = "/apps/songmaker"
CLI_PROFILE: Final = "claude-main"
COWRITER_ALIAS: Final = "cowriter"
RUN_ID_PLACEHOLDER: Final = "00000000-0000-4000-8000-000000000000"


def service_config(issuer: str = TEST_ISSUER) -> ServiceConfig:
    return ServiceConfig.model_validate(
        {
            "identity": {"issuer": issuer, "jwks_url": TEST_JWKS_URL},
            "profiles": {CLI_PROFILE: {"provider": "claude"}},
            "purposes": {
                COWRITER: {"cli_provider": "claude", "api_alias": COWRITER_ALIAS},
                PRESENTER: {"api_alias": "copresenter"},
            },
            "apps": {
                "songmaker": {
                    "workload_client": SONGMAKER_CLIENT,
                    "purposes": [COWRITER],
                    "tools": {"read_song": "read_only", "edit_song": "side_effecting"},
                },
                "presentator": {"workload_client": PRESENTATOR_CLIENT, "purposes": [PRESENTER]},
            },
        }
    )


def run_body(**overrides: object) -> dict[str, Any]:
    return {
        "v": 1,
        "purpose": COWRITER,
        "turn": {"system": "You help write songs.", "messages": [{"role": "user", "text": "Hi"}]},
        **overrides,
    }


class TokenMint:
    """Signs RS256 tokens with a throwaway key and serves that key's set, counting each fetch."""

    KEY_ID: Final = "test-signing-key"

    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key_set_fetches = 0

    def key_set(self) -> dict[str, Any]:
        public = json.loads(RSAAlgorithm.to_jwk(self._key.public_key()))
        return {"keys": [{**public, "kid": self.KEY_ID, "use": "sig", "alg": "RS256"}]}

    def fetch_key_set(self, _url: str) -> bytes:
        self.key_set_fetches += 1
        return json.dumps(self.key_set()).encode()

    def token(self, claims: Mapping[str, object]) -> str:
        return jwt.encode(dict(claims), self._key, algorithm="RS256", headers={"kid": self.KEY_ID})

    def workload(self, client: str = SONGMAKER_CLIENT, **overrides: object) -> str:
        now = int(time.time())
        claims = {
            "iss": TEST_ISSUER,
            "aud": "agent-service",
            "sub": f"service-account-user-of-{client}",
            "azp": client,
            "client_id": client,
            "realm_access": {"roles": ["agent-workload", "default-roles-agent-test"]},
            "iat": now,
            "exp": now + 300,
        }
        return self.token(_merged(claims, overrides))

    def user(self, groups: object = (SUBSCRIPTION_GROUP,), **overrides: object) -> str:
        now = int(time.time())
        claims = {
            "iss": TEST_ISSUER,
            "aud": ["agent-service", "account"],
            "sub": "user-alice",
            "azp": SONGMAKER_CLIENT,
            "groups": groups,
            "realm_access": {"roles": ["default-roles-agent-test"]},
            "iat": now,
            "exp": now + 300,
        }
        return self.token(_merged(claims, overrides))


def _merged(claims: dict[str, object], overrides: Mapping[str, object]) -> dict[str, object]:
    """Overrides replace claims; an override of ``None`` removes the claim."""
    merged = {**claims, **overrides}
    return {name: value for name, value in merged.items() if value is not None}


def run_headers(workload_token: str | None, user_token: str | None) -> dict[str, str]:
    headers = {}
    if workload_token is not None:
        headers["Agent-Workload-Token"] = workload_token
    if user_token is not None:
        headers["Authorization"] = f"Bearer {user_token}"
    return headers


class ScriptedRunner:
    """A runner that yields one fixed list of core events and records what it was asked."""

    def __init__(self, events: Sequence[StreamEvent | Exception]) -> None:
        self._events = events
        self.admitted: list[AdmittedRun] = []

    async def run(self, admitted: AdmittedRun) -> AsyncGenerator[StreamEvent, None]:
        self.admitted.append(admitted)
        for event in self._events:
            if isinstance(event, Exception):
                raise event
            yield event


@contextmanager
def served(app: FastAPI) -> Iterator[str]:
    """Serve ``app`` with uvicorn on a real loopback socket; yield its base URL.

    The socket is bound and listening before the server thread starts, so a
    request made at once waits in the backlog instead of racing the startup.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "the test server did not stop"


def post_run(
    base_url: str,
    headers: Mapping[str, str],
    body: object = None,
    *,
    content: bytes | None = None,
) -> httpx.Response:
    payload = content if content is not None else json.dumps(run_body() if body is None else body)
    return httpx.post(
        f"{base_url}/runs",
        headers={"Content-Type": "application/json", **headers},
        content=payload,
        timeout=10,
    )


def assert_rejected(response: httpx.Response, code: RejectionCode, status: int) -> None:
    assert response.status_code == status, response.text
    assert RunRejection.model_validate_json(response.content).code is code
    assert response.headers["cache-control"] == "no-store"
    if code is RejectionCode.UNAUTHORIZED:
        assert response.headers["www-authenticate"] == "Bearer"
    if code is RejectionCode.UNAVAILABLE:
        assert response.headers["retry-after"] == "30"


def decoded_events(raw: bytes) -> list[RunEvent]:
    """Every event of a whole stream, asserting the client's decoder finds no violation."""
    decoder = RunStreamDecoder()
    decoded = decoder.feed(raw) + decoder.close()
    events = [item for item in decoded if not isinstance(item, StreamViolation)]
    assert len(events) == len(decoded), f"the stream broke the contract: {decoded[len(events) :]}"
    return events


def with_placeholder_run_id(raw: bytes) -> bytes:
    """The stream's bytes with its random run id replaced, so frames compare as frozen text."""
    run_id = json.loads(raw.split(b"data: ", 1)[1].split(b"\n", 1)[0])["run_id"]
    uuid.UUID(run_id)
    return raw.replace(run_id.encode(), RUN_ID_PLACEHOLDER.encode())
