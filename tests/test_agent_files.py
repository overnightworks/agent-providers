"""The agent file: Claude Code's subagent format plus ``model`` and ``appRoles`` (#383)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_client.agents import AgentDefinition, AgentFileError, load_agent
from agent_client.tools import ToolDeclaration, ToolEffect
from agent_client.wire import RunRequest, TurnMessage, TurnRole

READ_NOTE = ToolDeclaration(
    name="read_note", description="Read a note", parameters={}, effect=ToolEffect.READ_ONLY
)
EDIT_NOTE = ToolDeclaration(name="edit_note", description="Edit a note", parameters={})
DECLARATIONS = [READ_NOTE, EDIT_NOTE]

PROMPT = "You help with notes."


def agent_text(
    *,
    name: str = "notes-assistant",
    description: str | None = "Helps with notes",
    tools: str | None = "[read_note, edit_note]",
    model: str | None = "claude/sonnet",
    roles: str | None = "[admin, user]",
    extra: str = "",
    body: str = PROMPT,
) -> str:
    lines = [f"name: {name}"]
    if description is not None:
        lines.append(f"description: {description}")
    if tools is not None:
        lines.append(f"tools: {tools}")
    if model is not None:
        lines.append(f"model: {model}")
    if roles is not None:
        lines.append(f"appRoles: {roles}")
    if extra:
        lines.append(extra)
    return "---\n" + "\n".join(lines) + "\n---\n" + body + "\n"


def write(tmp_path: Path, text: str | bytes, stem: str = "notes-assistant") -> Path:
    path = tmp_path / f"{stem}.md"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path: Path, text: str | bytes, stem: str = "notes-assistant", declarations=None):
    path = write(tmp_path, text, stem)
    return load_agent(path, DECLARATIONS if declarations is None else declarations)


def test_full_file_loads_with_tools_in_file_order(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text(tools="[edit_note, read_note]"))

    assert agent.name == "notes-assistant"
    assert agent.description == "Helps with notes"
    assert agent.model == "claude/sonnet"
    assert agent.roles == ["admin", "user"]
    assert agent.tools == (EDIT_NOTE, READ_NOTE)
    assert agent.tools[1].effect is ToolEffect.READ_ONLY
    assert agent.prompt == PROMPT


def test_minimal_file_has_no_tools_and_no_model(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text(tools=None, model=None, roles="[admin]"))

    assert agent.tools == ()
    assert agent.model is None


def test_prompt_is_the_body_without_surrounding_blank_lines(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text(body="\n\nFirst.\n\nSecond.\n\n"))

    assert agent.prompt == "First.\n\nSecond."


@pytest.mark.parametrize(
    "roles", ["[user]", "[]", "[user, admin]", "[admin, admin]", "admin", None]
)
def test_app_roles_other_than_the_two_lists_are_refused(tmp_path: Path, roles: str | None) -> None:
    with pytest.raises(AgentFileError, match="appRoles"):
        load(tmp_path, agent_text(roles=roles))


@pytest.mark.parametrize("key", ["color: red", "permissionMode: plan"])
def test_unknown_key_is_refused_and_named(tmp_path: Path, key: str) -> None:
    with pytest.raises(AgentFileError, match=key.split(":")[0]):
        load(tmp_path, agent_text(extra=key))


def test_name_differing_from_the_stem_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AgentFileError, match="name"):
        load(tmp_path, agent_text(name="other-name"))


def test_missing_name_is_refused(tmp_path: Path) -> None:
    text = agent_text().replace("name: notes-assistant\n", "", 1)
    with pytest.raises(AgentFileError, match="name"):
        load(tmp_path, text)


def test_undeclared_tool_is_refused_and_named(tmp_path: Path) -> None:
    with pytest.raises(AgentFileError, match="delete_note"):
        load(tmp_path, agent_text(tools="[delete_note]"))


def test_duplicate_tool_in_the_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AgentFileError, match="read_note"):
        load(tmp_path, agent_text(tools="[read_note, read_note]"))


def test_two_declarations_with_one_name_are_refused(tmp_path: Path) -> None:
    twin = READ_NOTE.model_copy(update={"effect": ToolEffect.SIDE_EFFECTING})
    with pytest.raises(AgentFileError, match="read_note"):
        load(tmp_path, agent_text(), declarations=[READ_NOTE, twin])


@pytest.mark.parametrize("body", ["", "   \n\n  "])
def test_empty_prompt_is_refused(tmp_path: Path, body: str) -> None:
    with pytest.raises(AgentFileError, match="prompt"):
        load(tmp_path, agent_text(body=body))


@pytest.mark.parametrize(
    "stem", ["Notes", "notes.assistant", "notes_assistant", "a" + "b" * 64, "1notes", "-notes"]
)
def test_stem_that_is_not_a_purpose_is_refused(tmp_path: Path, stem: str) -> None:
    with pytest.raises(AgentFileError, match=stem):
        load(tmp_path, agent_text(name=stem), stem=stem)


def test_file_without_md_suffix_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "notes-assistant.txt"
    path.write_text(agent_text(), encoding="utf-8")

    with pytest.raises(AgentFileError, match="notes-assistant.txt"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize(
    "text",
    [
        "name: notes-assistant\nappRoles: [admin]\n---\nbody\n",
        "---\nname: notes-assistant\nappRoles: [admin]\nbody\n",
        "---\nname: [unclosed\n---\nbody\n",
        "---\n- a\n- b\n---\nbody\n",
        "---\n---\nbody\n",
    ],
    ids=["no-opening", "no-closing", "broken-yaml", "not-a-mapping", "empty-front-matter"],
)
def test_malformed_front_matter_is_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(AgentFileError, match="notes-assistant.md"):
        load(tmp_path, text)


def test_bytes_that_are_not_utf8_are_refused(tmp_path: Path) -> None:
    with pytest.raises(AgentFileError, match="UTF-8"):
        load(tmp_path, b"---\nname: \xff\xfe\n---\nbody\n")


@pytest.mark.parametrize("model", ["claude/sonnet", "xai/grok-4.6", "openai/gpt-6-astra"])
def test_model_in_the_grammar_is_accepted(tmp_path: Path, model: str) -> None:
    assert load(tmp_path, agent_text(model=model)).model == model


@pytest.mark.parametrize(
    "model", ["claude/sonnet@2", "'claude/sonnet:1'", "Claude/Sonnet", "sonnet", "''"]
)
def test_model_outside_the_grammar_is_refused(tmp_path: Path, model: str) -> None:
    with pytest.raises(AgentFileError, match="model"):
        load(tmp_path, agent_text(model=model))


def test_yaml_boolean_in_app_roles_is_not_read_as_text(tmp_path: Path) -> None:
    with pytest.raises(AgentFileError, match="appRoles"):
        load(tmp_path, agent_text(roles="[admin, no]"))


def test_numeric_name_is_not_read_as_text(tmp_path: Path) -> None:
    with pytest.raises(AgentFileError, match="name"):
        load(tmp_path, agent_text(name="123"), stem="123")


@pytest.mark.parametrize("description", [None, "''", '"two\\nlines"', "|\n  two\n  lines"])
def test_missing_empty_or_multiline_description_is_refused(
    tmp_path: Path, description: str | None
) -> None:
    with pytest.raises(AgentFileError, match="description"):
        load(tmp_path, agent_text(description=description))


@pytest.mark.parametrize(
    ("roles", "admin", "user"),
    [("[admin]", True, False), ("[admin, user]", True, True)],
)
def test_offered_to_follows_the_app_roles(
    tmp_path: Path, roles: str, admin: bool, user: bool
) -> None:
    agent = load(tmp_path, agent_text(roles=roles))

    assert (agent.offered_to("admin"), agent.offered_to("user")) == (admin, user)


@pytest.mark.parametrize("role", ["guest", ""])
def test_offered_to_refuses_a_role_outside_the_two(tmp_path: Path, role: str) -> None:
    with pytest.raises(ValueError, match="role"):
        load(tmp_path, agent_text()).offered_to(role)  # type: ignore[arg-type]


def test_request_is_the_v1_body_with_purpose_and_prompt(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text())

    request = agent.request([TurnMessage(role=TurnRole.USER, text="hi")])

    assert request.purpose == "notes-assistant"
    assert request.turn.system == PROMPT
    assert [message.text for message in request.turn.messages] == ["hi"]
    assert RunRequest.model_validate(request.model_dump(mode="json")) == request
    assert set(request.model_dump()) == {"v", "purpose", "turn"}


def test_request_without_messages_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load(tmp_path, agent_text()).request([])


def test_loaded_definition_is_frozen(tmp_path: Path) -> None:
    agent: AgentDefinition = load(tmp_path, agent_text())

    with pytest.raises(ValidationError):
        agent.name = "other"  # type: ignore[misc]
