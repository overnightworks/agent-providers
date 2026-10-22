"""The event family a provider turn streams to its in-process caller.

Every transport — Claude's CLI/MCP stream, the OpenAI-compatible HTTP
adapters, the tool loop — yields these types. They are the core's own turn
events, not the agent service's wire: the streamed run contract has one owner,
``agent_runtime_client.wire``, onto which the agent service maps a turn's events
(slice A1a). Hosts that call the core directly still consume these types, so
their field names and defaults stay a public contract.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StreamEvent(BaseModel):
    """Base class for every event a provider turn streams.

    ``correlation_id`` is the host's own handle for the turn that produced
    the event — a job id, a request id — carried so the host can log and
    group events without threading a second value beside them. It is
    excluded from serialization: the serialised event is the same with it as
    without it.
    """

    type: str
    correlation_id: str | None = Field(default=None, exclude=True)


class AssistantTextEvent(StreamEvent):
    type: Literal["assistant_text"] = "assistant_text"
    text: str


class ToolCallEvent(StreamEvent):
    type: Literal["tool_call"] = "tool_call"
    tool_use_id: str
    name: str
    input: dict = Field(default_factory=dict)


class ToolResultEvent(StreamEvent):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


class FinalEvent(StreamEvent):
    type: Literal["final"] = "final"
    text: str
