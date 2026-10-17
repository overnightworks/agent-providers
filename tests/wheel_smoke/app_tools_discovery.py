"""Runs from the three installed wheels alone: the service's upstream client discovers a tool.

Started as ``python -I <this file>``, which leaves this directory off
``sys.path``; the fake app lives in ``tests/``, so that directory is added.
"""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
from importlib.metadata import requires
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1])
)  # the fake app is test code, not a wheel

from capability_support import capability_carrying  # noqa: E402
from mcp_app_fake import CERTIFICATE_HOST, ScriptedApp, TestAuthority, serving  # noqa: E402

from agent_service.app_tools import open_app_tools  # noqa: E402

DISCOVERY_DEADLINE_SECONDS = 30
EXPECTED_HTTPX_RANGE = {">=0.27", "<1"}


def declared_httpx_ranges() -> list[set[str]]:
    unconditional_httpx = (
        re.fullmatch(r"httpx\s*([^;\s]+)", text.strip())
        for text in requires("overnightworks-agent-service") or []
    )
    return [
        {specifier.strip() for specifier in match.group(1).split(",")}
        for match in unconditional_httpx
        if match
    ]


async def discover(origin: str, authority: TestAuthority) -> list[str]:
    async with asyncio.timeout(DISCOVERY_DEADLINE_SECONDS):
        async with open_app_tools(
            origin, capability_carrying("smoke-capability"), authority.client_context()
        ) as session:
            return [tool.name for tool in await session.list_tools()]


def main() -> None:
    assert declared_httpx_ranges() == [EXPECTED_HTTPX_RANGE], declared_httpx_ranges()
    with tempfile.TemporaryDirectory() as directory:
        authority = TestAuthority(Path(directory))
        with serving(ScriptedApp(), authority.credentials_for(CERTIFICATE_HOST)) as fake:
            discovered = asyncio.run(discover(fake.origin, authority))
    assert discovered == ["echo"], discovered
    print("discovered", discovered)


main()
