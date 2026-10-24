"""Arrangement shared by the tests of a run started with a job ticket (#457)."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import httpx
from service_support import (
    ScriptedRunner,
    TokenMint,
    WallClock,
    decoded_events,
    process_signing_key_file,
    served,
)
from webauth.jwt_verification import KeySetFetch

from agent_runtime.events import AssistantTextEvent, FinalEvent
from agent_runtime_client.wire import PROTOCOL_VERSION
from agent_runtime_service.app import JOB_TICKET_HEADER, WORKLOAD_TOKEN_HEADER, create_app
from agent_runtime_service.config import ServiceConfig
from agent_runtime_service.job_ticket import JOB_TICKET_TOKEN_TYPE
from agent_runtime_service.runners import AdmittedRun, Runner, Runners
from agent_runtime_service.signing import SigningKey, load_signing_key

CLI_SCRIPT: Final = (AssistantTextEvent(text="via the CLI"), FinalEvent(text="CLI answer"))
API_SCRIPT: Final = (AssistantTextEvent(text="via the API"), FinalEvent(text="API answer"))


@dataclass(frozen=True)
class RunningService:
    base_url: str
    cli: Runner
    api: ScriptedRunner
    clock: WallClock

    def admitted(self) -> list[AdmittedRun]:
        """The runs the recording CLI runner was asked to start."""
        assert isinstance(self.cli, ScriptedRunner)
        return self.cli.admitted


@contextmanager
def running_service(
    config: ServiceConfig,
    fetch_key_set: KeySetFetch,
    *,
    cli: Runner | None = None,
    clock: WallClock | None = None,
) -> Iterator[RunningService]:
    """The service over a real socket; its runners record, or the given ``cli`` runs."""
    cli = cli or ScriptedRunner(CLI_SCRIPT)
    api = ScriptedRunner(API_SCRIPT)
    clock = clock or WallClock()
    app = create_app(
        config, runners=Runners(cli=cli, api=api), fetch_key_set=fetch_key_set, wall_clock=clock
    )
    with served(app) as base_url:
        yield RunningService(base_url, cli, api, clock)


def with_background(config: ServiceConfig, background: bool) -> ServiceConfig:
    (name, profile), *_ = config.profiles.items()
    return config.model_copy(
        update={"profiles": {name: profile.model_copy(update={"background": background})}}
    )


def ticket_headers(workload_token: str | None, ticket: str | None) -> dict[str, str]:
    headers = {}
    if workload_token is not None:
        headers[WORKLOAD_TOKEN_HEADER] = workload_token
    if ticket is not None:
        headers[JOB_TICKET_HEADER] = ticket
    return headers


def minted_ticket(
    base_url: str,
    mint: TokenMint,
    groups: tuple[str, ...],
    *,
    purpose: str,
    client: str,
    subject: str = "user-alice",
) -> str:
    """A ticket from the real ``POST /job-tickets`` for the person ``mint.user`` names."""
    response = httpx.post(
        f"{base_url}/job-tickets",
        headers={
            "Content-Type": "application/json",
            WORKLOAD_TOKEN_HEADER: mint.workload(client),
            "Authorization": f"Bearer {mint.user(groups, sub=subject, azp=client)}",
        },
        content=json.dumps({"v": PROTOCOL_VERSION, "purpose": purpose, "expires_in_s": 3600}),
        timeout=10,
    )
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def process_key() -> SigningKey:
    return load_signing_key(process_signing_key_file())


def signed_ticket(claims: Mapping[str, object], token_type: str = JOB_TICKET_TOKEN_TYPE) -> str:
    """A ticket signed with the service's own key from the given claims, as written."""
    return process_key().sign(claims, token_type)


def run_id_of(stream: bytes) -> UUID:
    return decoded_events(stream)[0].run_id
