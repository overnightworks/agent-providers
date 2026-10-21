"""The agent file: Claude Code's subagent format plus ``model`` and ``appRoles`` (#383)."""

from __future__ import annotations

import traceback
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


def with_field_replaced(front_matter: str) -> str:
    base = "name: notes-assistant\ndescription: d\nappRoles: [admin]\n"
    replaced_key = front_matter.lstrip().split(":")[0]
    kept = "".join(
        line + "\n" for line in base.splitlines() if line.split(":")[0] != replaced_key
    )
    return kept + front_matter


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


def front_matter_file(tmp_path: Path, front_matter: str, body: str = PROMPT) -> Path:
    return write(tmp_path, f"---\n{front_matter}\n---\n{body}\n")


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


@pytest.mark.parametrize(
    ("body", "prompt"),
    [
        ("\n\nFirst.\n\nSecond.\n\n", "First.\n\nSecond."),
        ("\n    code\n    next\n", "    code\n    next"),
        ("Line with hard break  \nnext  \n", "Line with hard break  \nnext  "),
        ("\n  \n  indented\n \n", "  indented"),
    ],
    ids=["blank-lines", "indented-code", "trailing-spaces", "blank-whitespace-lines"],
)
def test_prompt_drops_only_surrounding_blank_lines(
    tmp_path: Path, body: str, prompt: str
) -> None:
    assert load(tmp_path, agent_text(body=body)).prompt == prompt


@pytest.mark.parametrize(
    "roles", ["[user]", "[]", "[user, admin]", "[admin, admin]", "admin", "[admin, no]", None]
)
def test_app_roles_other_than_the_two_lists_are_refused(tmp_path: Path, roles: str | None) -> None:
    path = write(tmp_path, agent_text(roles=roles))

    with pytest.raises(AgentFileError, match="appRoles"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize("key", ["color: red", "permissionMode: plan"])
def test_unknown_key_is_refused_and_named(tmp_path: Path, key: str) -> None:
    path = write(tmp_path, agent_text(extra=key))

    with pytest.raises(AgentFileError, match=key.split(":")[0]):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize("name", ["other-name", "123", "'123'", '"notes-assistant2"'])
def test_name_differing_from_the_stem_is_refused(tmp_path: Path, name: str) -> None:
    path = write(tmp_path, agent_text(name=name))

    with pytest.raises(AgentFileError, match="name"):
        load_agent(path, DECLARATIONS)


def test_missing_name_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, agent_text().replace("name: notes-assistant\n", "", 1))

    with pytest.raises(AgentFileError, match="name"):
        load_agent(path, DECLARATIONS)


def test_undeclared_tool_is_refused_and_named(tmp_path: Path) -> None:
    path = write(tmp_path, agent_text(tools="[delete_note]"))

    with pytest.raises(AgentFileError, match="delete_note"):
        load_agent(path, DECLARATIONS)


def test_duplicate_tool_in_the_file_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, agent_text(tools="[read_note, read_note]"))

    with pytest.raises(AgentFileError, match="read_note"):
        load_agent(path, DECLARATIONS)


def test_two_declarations_with_one_name_are_refused(tmp_path: Path) -> None:
    twin = READ_NOTE.model_copy(update={"effect": ToolEffect.SIDE_EFFECTING})
    path = write(tmp_path, agent_text())

    with pytest.raises(AgentFileError, match="read_note"):
        load_agent(path, [READ_NOTE, twin])


@pytest.mark.parametrize("body", ["", "   \n\n  "])
def test_empty_prompt_is_refused(tmp_path: Path, body: str) -> None:
    path = write(tmp_path, agent_text(body=body))

    with pytest.raises(AgentFileError, match="prompt"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize(
    "stem", ["Notes", "notes.assistant", "notes_assistant", "a" + "b" * 64, "1notes", "-notes"]
)
def test_stem_that_is_not_a_purpose_is_refused(tmp_path: Path, stem: str) -> None:
    path = write(tmp_path, agent_text(name=stem), stem=stem)

    with pytest.raises(AgentFileError, match=stem):
        load_agent(path, DECLARATIONS)


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
    ids=["no-opening", "no-closing", "broken-list", "not-a-mapping", "empty-front-matter"],
)
def test_malformed_front_matter_is_refused(tmp_path: Path, text: str) -> None:
    path = write(tmp_path, text)

    with pytest.raises(AgentFileError, match="notes-assistant.md"):
        load_agent(path, DECLARATIONS)


def test_bytes_that_are_not_utf8_are_refused(tmp_path: Path) -> None:
    path = write(tmp_path, b"---\nname: \xff\xfe\n---\nbody\n")

    with pytest.raises(AgentFileError, match="UTF-8"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize("model", ["claude/sonnet", "xai/grok-4.6", "openai/gpt-6-astra"])
def test_model_in_the_grammar_is_accepted(tmp_path: Path, model: str) -> None:
    assert load(tmp_path, agent_text(model=model)).model == model


@pytest.mark.parametrize(
    "model", ["claude/sonnet@2", "'claude/sonnet:1'", "Claude/Sonnet", "sonnet", "''", "null", ""]
)
def test_model_outside_the_grammar_or_without_a_value_is_refused(
    tmp_path: Path, model: str
) -> None:
    path = write(tmp_path, agent_text(model=model))

    with pytest.raises(AgentFileError, match="model"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize("description", [None, "''", '"two\\nlines"', "|", "|\n  two\n  lines"])
def test_missing_empty_or_multiline_description_is_refused(
    tmp_path: Path, description: str | None
) -> None:
    path = write(tmp_path, agent_text(description=description))

    with pytest.raises(AgentFileError, match="description"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize(
    ("front_matter", "key"),
    [
        ("name: notes-assistant\nname: notes-assistant", "name"),
        ("appRoles: [admin]\nappRoles: [admin, user]", "appRoles"),
        ("tools: []\ntools: [edit_note]", "tools"),
        ("model: claude/sonnet\nmodel: claude/opus", "model"),
        ("tools:\n  - read_note\ntools: [edit_note]", "tools"),
    ],
)
def test_duplicate_key_is_refused_and_named(
    tmp_path: Path, front_matter: str, key: str
) -> None:
    path = front_matter_file(tmp_path, with_field_replaced(front_matter))

    with pytest.raises(AgentFileError, match=f"{key}: appears twice"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize(
    ("front_matter", "key"),
    [
        ("tools: *shared", "front matter"),
        ("description: |\n  two\n  lines", "description"),
        ("description: >\n  folded\n\n  paragraphs", "description"),
        ("tools:\n  nested: value", "tools"),
        ("color:\n  red: 1", "color"),
        ("description: Helps: with notes", "front matter"),
        ("description: 'unclosed", "front matter"),
        ('description: "bad \\q escape"', "front matter"),
        ("tools: [[read_note]]", "tools"),
        ("tools: {a: b}", "tools"),
        ("tools: [read_note", "front matter"),
        ("tools:", "tools"),
        ("\tdescription: tabbed", "front matter"),
        ("description: 123", "description"),
        ("description: true", "description"),
        ("description: null", "description"),
        ("tools: [read_note, 1]", "tools"),
        ("tools:\n  - read_note\n    - edit_note", "tools"),
    ],
    ids=lambda value: value.replace("\n", "|") if "\n" in value else value,
)
def test_front_matter_that_is_not_plain_text_is_refused_and_named(
    tmp_path: Path, front_matter: str, key: str
) -> None:
    path = front_matter_file(tmp_path, with_field_replaced(front_matter))

    with pytest.raises(AgentFileError, match=rf"notes-assistant\.md: .*{key}") as refusal:
        load_agent(path, DECLARATIONS)

    assert "appears twice" not in str(refusal.value)


@pytest.mark.parametrize(
    "roles", ["[admin, 1]", "[admin, true]", "[admin, null]", "\n  - admin\n    - user"]
)
def test_app_roles_that_are_not_text_or_are_misindented_are_refused(
    tmp_path: Path, roles: str
) -> None:
    path = write(tmp_path, agent_text(roles=roles))

    with pytest.raises(AgentFileError, match=r"notes-assistant\.md: appRoles"):
        load_agent(path, DECLARATIONS)


def test_name_that_is_a_number_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, agent_text(name="123"), stem="notes-assistant")

    with pytest.raises(AgentFileError, match=r"notes-assistant\.md: name"):
        load_agent(path, DECLARATIONS)


def test_nul_inside_quotes_is_refused_without_echoing_it(tmp_path: Path) -> None:
    path = write(tmp_path, agent_text(description='"secret\x00text"'))

    with pytest.raises(AgentFileError, match=r"notes-assistant\.md") as refusal:
        load_agent(path, DECLARATIONS)

    assert "secret" not in str(refusal.value)


@pytest.mark.parametrize("character", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e"])
def test_control_character_inside_quotes_is_refused_not_normalized(
    tmp_path: Path, character: str
) -> None:
    path = write(tmp_path, agent_text(description=f'"before{character}after"'))

    with pytest.raises(AgentFileError, match=r"notes-assistant\.md"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize(
    "fields",
    [
        {"extra": "description: sentinelvalue"},
        {"extra": "description: 'sentinelvalue"},
        {"model": "sentinelvalue"},
        {"description": "[sentinelvalue]"},
    ],
    ids=["duplicate-key", "syntax-error", "invalid-model", "non-string-description"],
)
def test_formatted_traceback_does_not_show_file_values(
    tmp_path: Path, fields: dict[str, str]
) -> None:
    path = write(tmp_path, agent_text(**fields))

    with pytest.raises(AgentFileError) as refusal:
        load_agent(path, DECLARATIONS)

    assert "sentinelvalue" not in "".join(traceback.format_exception(refusal.value))
    assert refusal.value.__cause__ is None
    assert refusal.value.__suppress_context__


def test_duplicate_key_message_does_not_echo_the_value(tmp_path: Path) -> None:
    path = write(tmp_path, agent_text(extra="description: topsecret"))

    with pytest.raises(AgentFileError, match="description: appears twice") as refusal:
        load_agent(path, DECLARATIONS)

    assert "topsecret" not in str(refusal.value)


def test_yaml_anchors_and_tags_are_read_as_yaml_reads_them(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text(tools="&shared [read_note]", description="!!str Helps"))

    assert (agent.tools, agent.description) == ((READ_NOTE,), "Helps")


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Helps with notes", "Helps with notes"),
        ("'Helps: with notes'", "Helps: with notes"),
        ('"Helps: with # notes"', "Helps: with # notes"),
        ("'it''s'", "it's"),
        ('"say \\"hi\\" \\\\"', 'say "hi" \\'),
        ("a:b", "a:b"),
    ],
)
def test_scalars_are_plain_or_quoted_text(
    tmp_path: Path, description: str, expected: str
) -> None:
    assert load(tmp_path, agent_text(description=description)).description == expected


@pytest.mark.parametrize(
    ("tools", "selected"),
    [
        ("[]", ()),
        ("[read_note]", (READ_NOTE,)),
        ("['edit_note', \"read_note\"]", (EDIT_NOTE, READ_NOTE)),
        ("[ read_note ,edit_note ]", (READ_NOTE, EDIT_NOTE)),
        ("\n  - read_note\n  - 'edit_note'", (READ_NOTE, EDIT_NOTE)),
    ],
    ids=["empty-flow", "one", "quoted-items", "loose-spacing", "block-list"],
)
def test_lists_are_flow_or_block(
    tmp_path: Path, tools: str, selected: tuple[ToolDeclaration, ...]
) -> None:
    assert load(tmp_path, agent_text(tools=tools)).tools == selected


def test_crlf_line_endings_load_like_lf(tmp_path: Path) -> None:
    text = agent_text(body="First.\n\nSecond.").replace("\n", "\r\n")

    agent = load(tmp_path, text.encode("utf-8"))

    assert (agent.name, agent.roles, agent.prompt) == (
        "notes-assistant",
        ["admin", "user"],
        "First.\n\nSecond.",
    )


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
    agent = load(tmp_path, agent_text())

    with pytest.raises(ValueError, match="role"):
        agent.offered_to(role)  # type: ignore[arg-type]


def test_request_is_the_v2_body_with_purpose_prompt_and_tool_effects(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text())

    request = agent.request([TurnMessage(role=TurnRole.USER, text="hi")])

    assert request.purpose == "notes-assistant"
    assert request.turn.system == PROMPT
    assert [message.text for message in request.turn.messages] == ["hi"]
    assert RunRequest.model_validate(request.model_dump(mode="json")) == request
    assert request.v == 2
    assert request.tools == {
        "read_note": ToolEffect.READ_ONLY,
        "edit_note": ToolEffect.SIDE_EFFECTING,
    }
    assert set(request.model_dump()) == {"v", "purpose", "model", "turn", "tools"}


@pytest.mark.parametrize(
    ("model_line", "model"),
    [
        pytest.param("claude/sonnet", "claude/sonnet", id="A1-the-files-model"),
        pytest.param(None, None, id="A2-no-model"),
    ],
)
def test_request_carries_the_files_model_or_none(
    tmp_path: Path, model_line: str | None, model: str | None
) -> None:
    agent = load(tmp_path, agent_text(model=model_line))

    assert agent.request([TurnMessage(role=TurnRole.USER, text="hi")]).model == model


def test_request_of_an_agent_without_tools_names_none(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text(tools="[]"))

    assert agent.request([TurnMessage(role=TurnRole.USER, text="hi")]).tools == {}


def test_request_without_messages_is_refused(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text())

    with pytest.raises(ValidationError):
        agent.request([])


def test_loaded_definition_is_frozen(tmp_path: Path) -> None:
    agent: AgentDefinition = load(tmp_path, agent_text())

    with pytest.raises(ValidationError):
        agent.name = "other"  # type: ignore[misc]
