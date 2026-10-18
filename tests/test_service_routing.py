"""H5 routing over its full cross-product (decision record 0001).

Every combination of the five inputs is one row: whether the purpose names a
CLI provider, whether a profile for that provider exists, whether the caller is
in ``/subscription-members``, whether the purpose has an API alias, and whether the caller is
in the app's group.
"""

from __future__ import annotations

import pytest

from agent_service.config import ProfileConfig, Provider, PurposeConfig
from agent_service.routing import Api, Cli, NotEnabled, route

CLAUDE_PROFILE = {"claude-main": ProfileConfig(provider=Provider.CLAUDE)}
ONLY_ANOTHER_PROVIDERS_PROFILE = {"grok-main": ProfileConfig(provider=Provider.GROK)}
CLI = Cli("claude-main")
API = Api("cowriter")
OFF = NotEnabled()

# cli set, profile exists, in subscription group, api alias set, in /apps/songmaker -> route
CROSS_PRODUCT = [
    (True, True, True, True, True, CLI),
    (True, True, True, True, False, CLI),
    (True, True, True, False, True, CLI),
    (True, True, True, False, False, CLI),
    (True, True, False, True, True, API),
    (True, True, False, True, False, OFF),
    (True, True, False, False, True, OFF),
    (True, True, False, False, False, OFF),
    (True, False, True, True, True, API),
    (True, False, True, True, False, OFF),
    (True, False, True, False, True, OFF),
    (True, False, True, False, False, OFF),
    (True, False, False, True, True, API),
    (True, False, False, True, False, OFF),
    (True, False, False, False, True, OFF),
    (True, False, False, False, False, OFF),
    (False, True, True, True, True, API),
    (False, True, True, True, False, OFF),
    (False, True, True, False, True, OFF),
    (False, True, True, False, False, OFF),
    (False, True, False, True, True, API),
    (False, True, False, True, False, OFF),
    (False, True, False, False, True, OFF),
    (False, True, False, False, False, OFF),
    (False, False, True, True, True, API),
    (False, False, True, True, False, OFF),
    (False, False, True, False, True, OFF),
    (False, False, True, False, False, OFF),
    (False, False, False, True, True, API),
    (False, False, False, True, False, OFF),
    (False, False, False, False, True, OFF),
    (False, False, False, False, False, OFF),
]


def _row_id(row: tuple) -> str:
    names = ("cli", "profile", "subscription-members", "alias", "app-group")
    present = [name if flag else f"no-{name}" for name, flag in zip(names, row[:5], strict=True)]
    return "-".join(present)


@pytest.mark.parametrize(
    ("cli_provider", "profile_exists", "in_subscription", "alias", "in_app_group", "expected"),
    CROSS_PRODUCT,
    ids=[_row_id(row) for row in CROSS_PRODUCT],
)
def test_route_follows_the_h5_table(
    cli_provider, profile_exists, in_subscription, alias, in_app_group, expected
):
    purpose = PurposeConfig(
        cli_provider=Provider.CLAUDE if cli_provider else None,
        api_alias="cowriter" if alias else None,
    )
    groups = frozenset(
        group
        for group, member in (
            ("/subscription-members", in_subscription),
            ("/apps/songmaker", in_app_group),
        )
        if member
    )

    served = route(
        purpose,
        CLAUDE_PROFILE if profile_exists else ONLY_ANOTHER_PROVIDERS_PROFILE,
        groups,
        "songmaker",
        subscription_group="/subscription-members",
    )

    assert served == expected


def test_the_cross_product_is_complete():
    assert len({row[:5] for row in CROSS_PRODUCT}) == 2**5


def test_another_apps_group_does_not_open_the_api_route():
    served = route(
        PurposeConfig(api_alias="cowriter"),
        {},
        frozenset({"/apps/presentator"}),
        "songmaker",
        subscription_group="/subscription-members",
    )

    assert served == NotEnabled()
