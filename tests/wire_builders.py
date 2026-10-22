"""Builders for the agent service's wire events, shared by the wire and SSE suites."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from agent_runtime_client.tools import ToolEffect
from agent_runtime_client.wire import (
    PROTOCOL_VERSION,
    AssistantText,
    Cancelled,
    Failed,
    FailureCode,
    Heartbeat,
    RunStarted,
    Succeeded,
    ToolCall,
    ToolResult,
)

RUN_ID = UUID("8c2f6c1e-3b5a-4d8e-9f10-2a7b4c6d8e01")
OTHER_RUN_ID = UUID("0d5e7f90-1a2b-4c3d-8e4f-5a6b7c8d9e02")


def wire(type_: str, seq: int, **payload: object) -> dict[str, object]:
    """One event as the JSON object it travels as."""
    return {"v": PROTOCOL_VERSION, "run_id": str(RUN_ID), "seq": seq, "type": type_, **payload}


def started(run_id: UUID = RUN_ID) -> RunStarted:
    return RunStarted(v=PROTOCOL_VERSION, run_id=run_id, seq=0, heartbeat_interval_s=15)


def text(seq: int, words: str) -> AssistantText:
    return AssistantText(v=PROTOCOL_VERSION, run_id=RUN_ID, seq=seq, text=words)


def call(
    seq: int,
    tool_use_id: str,
    effect: ToolEffect = ToolEffect.SIDE_EFFECTING,
    *,
    name: str = "tool",
    tool_input: dict[str, Any] | None = None,
) -> ToolCall:
    return ToolCall(
        v=PROTOCOL_VERSION,
        run_id=RUN_ID,
        seq=seq,
        tool_use_id=tool_use_id,
        name=name,
        input={} if tool_input is None else tool_input,
        effect=effect,
    )


def result(seq: int, tool_use_id: str, content: str = "ok") -> ToolResult:
    return ToolResult(
        v=PROTOCOL_VERSION,
        run_id=RUN_ID,
        seq=seq,
        tool_use_id=tool_use_id,
        content=content,
        is_error=False,
    )


def heartbeat(seq: int, run_id: UUID = RUN_ID) -> Heartbeat:
    return Heartbeat(v=PROTOCOL_VERSION, run_id=run_id, seq=seq)


def succeeded(seq: int, words: str = "Done.") -> Succeeded:
    return Succeeded(v=PROTOCOL_VERSION, run_id=RUN_ID, seq=seq, text=words)


def cancelled(seq: int) -> Cancelled:
    return Cancelled(v=PROTOCOL_VERSION, run_id=RUN_ID, seq=seq)


def interrupted(seq: int) -> Failed:
    return Failed(
        v=PROTOCOL_VERSION,
        run_id=RUN_ID,
        seq=seq,
        code=FailureCode.INTERRUPTED,
        message="The run was cut short.",
    )
