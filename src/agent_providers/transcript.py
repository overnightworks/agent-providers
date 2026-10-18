"""The prompt a CLI turn reads from stdin."""

from __future__ import annotations


def flatten_messages(prompt: str, messages: list[dict[str, str]] | None) -> str:
    """Render a chat history as the single prompt a CLI turn reads from stdin."""
    if messages is None:
        return prompt
    parts = []
    for msg in messages:
        prefix = "User" if msg["role"] == "user" else "Assistant"
        parts.append(f"{prefix}: {msg['content']}")
    return "\n\n".join(parts)


def stdin_prompt(system: str | None, prompt: str) -> str:
    """Prepend the system instruction to the prompt a CLI turn reads from stdin."""
    if system:
        return f"{system}\n\n{prompt}"
    return prompt
