"""The tool contract an app declares and every agent layer reads.

This module is the one owner of ``ToolDeclaration``; the provider core builds
its ``ToolCatalog`` on it and re-exports the identical class.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolDeclaration(BaseModel):
    """One tool as a model sees it: its name, its purpose, its arguments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parameters: dict[str, Any]
