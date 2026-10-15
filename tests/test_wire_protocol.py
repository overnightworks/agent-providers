"""The agent service's streamed run contract, owned by ``agent_client``.

These tests pin decision record 0001, H6 and its A0b sub-ruling: the closed
wire union, the run-stream rules, and the tool effect class that decides how
a cut-short run ends.
"""

from __future__ import annotations

import json

import pytest

from agent_client.tools import ToolDeclaration, ToolEffect
from agent_providers.tools import ToolCatalog, anthropic_tool_schemas, openai_tool_schemas


def _declaration(name: str, **fields: object) -> ToolDeclaration:
    return ToolDeclaration(
        name=name,
        description=f"{name} does one thing",
        parameters={"type": "object", "properties": {}},
        **fields,
    )


def test_a_tool_declared_without_an_effect_counts_as_side_effecting() -> None:
    assert _declaration("write_lyrics").effect is ToolEffect.SIDE_EFFECTING


@pytest.mark.parametrize("project", [anthropic_tool_schemas, openai_tool_schemas])
def test_the_effect_class_never_reaches_a_model(project) -> None:
    catalog = ToolCatalog(
        tools=(
            _declaration("read_song", effect=ToolEffect.READ_ONLY),
            _declaration("write_lyrics", effect=ToolEffect.SIDE_EFFECTING),
        )
    )

    assert "effect" not in json.dumps(project(catalog))
