"""The agent file an app ships, and the definition read from it.

An agent file is Claude Code's subagent format (YAML front matter between two
``---`` lines, then the prompt as Markdown) with two extra keys, ``model`` and
``appRoles`` (decision record 0001, H9). ``offered_to`` is a display filter for
the app's own UI; the boundary is the service's role check.
"""

from __future__ import annotations

import re
from importlib.resources.abc import Traversable
from typing import Annotated, Any, Final

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

from agent_client.tools import ToolDeclaration
from agent_client.wire import AgentRole, AgentRoles, Purpose, RunRequest, Turn, TurnMessage

_AGENT_FILE_SUFFIX: Final = ".md"
_FRONT_MATTER_FENCE: Final = "---"
_MODEL_PATTERN: Final = re.compile(r"[a-z0-9-]+/[a-z0-9.-]+")
_AGENT_ROLES: Final = TypeAdapter(AgentRole)
_PURPOSE: Final = TypeAdapter(Purpose)


class AgentFileError(ValueError):
    """An agent file is refused; the message names the file and the field."""


def _refuse_non_model(text: str) -> str:
    if _MODEL_PATTERN.fullmatch(text) is None:
        raise ValueError("must be <provider>/<model> with no version, such as claude/sonnet")
    return text


def _refuse_non_single_line(text: str) -> str:
    if not text.strip() or "\n" in text or "\r" in text:
        raise ValueError("must be a non-empty single line")
    return text


class _FrontMatter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: Purpose
    description: Annotated[StrictStr, AfterValidator(_refuse_non_single_line)]
    tools: list[StrictStr] = Field(default_factory=list)
    model: Annotated[StrictStr, AfterValidator(_refuse_non_model)] | None = None
    roles: AgentRoles = Field(alias="appRoles")


class AgentDefinition(BaseModel):
    """One agent as its file states it, with the app's tool declarations it selected."""

    model_config = ConfigDict(frozen=True)

    name: Purpose
    description: str
    model: str | None
    roles: AgentRoles
    tools: tuple[ToolDeclaration, ...]
    prompt: str

    def offered_to(self, role: AgentRole) -> bool:
        """Whether a person with this app role is shown the agent (not a security boundary)."""
        try:
            checked_role = _AGENT_ROLES.validate_python(role, strict=True)
        except ValidationError as error:
            raise ValueError(f"role must be 'admin' or 'user', not {role!r}") from error
        return checked_role in self.roles

    def request(self, messages: list[TurnMessage]) -> RunRequest:
        """The body that starts a run of this agent over these messages."""
        return RunRequest(
            v=1, purpose=self.name, turn=Turn(system=self.prompt, messages=messages)
        )


def load_agent(source: Traversable, declarations: list[ToolDeclaration]) -> AgentDefinition:
    """Read and check one agent file against the app's tool declarations.

    Raises ``AgentFileError`` naming the file and the field of the first problem.
    """
    file_name = source.name
    stem = _checked_stem(file_name)
    front_matter_text, prompt = _split_file(file_name, _read_text(source, file_name))
    front_matter = _parse_front_matter(file_name, front_matter_text)
    if front_matter.name != stem:
        raise AgentFileError(
            f"{file_name}: name {front_matter.name!r} must equal the file name {stem!r}"
        )
    if not prompt:
        raise AgentFileError(f"{file_name}: prompt is empty")
    return AgentDefinition(
        name=front_matter.name,
        description=front_matter.description,
        model=front_matter.model,
        roles=front_matter.roles,
        tools=_selected_tools(file_name, front_matter.tools, declarations),
        prompt=prompt,
    )


def _checked_stem(file_name: str) -> str:
    stem = file_name.removesuffix(_AGENT_FILE_SUFFIX)
    if stem == file_name:
        raise AgentFileError(f"{file_name}: an agent file must end in {_AGENT_FILE_SUFFIX}")
    try:
        _PURPOSE.validate_python(stem)
    except ValidationError as error:
        raise AgentFileError(
            f"{file_name}: file name stem {stem!r}: {_first_message(error)}"
        ) from error
    return stem


def _read_text(source: Traversable, file_name: str) -> str:
    try:
        return source.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise AgentFileError(f"{file_name}: the file is not UTF-8") from error


def _split_file(file_name: str, text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != _FRONT_MATTER_FENCE:
        raise AgentFileError(
            f"{file_name}: front matter must open with a {_FRONT_MATTER_FENCE} line"
        )
    try:
        closing = lines.index(_FRONT_MATTER_FENCE, 1)
    except ValueError as error:
        raise AgentFileError(
            f"{file_name}: front matter must close with a {_FRONT_MATTER_FENCE} line"
        ) from error
    return "\n".join(lines[1:closing]), "\n".join(lines[closing + 1 :]).strip()


def _parse_front_matter(file_name: str, text: str) -> _FrontMatter:
    try:
        parsed: Any = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise AgentFileError(f"{file_name}: front matter is not valid YAML: {error}") from error
    if not isinstance(parsed, dict):
        raise AgentFileError(f"{file_name}: front matter must be a mapping of keys")
    try:
        return _FrontMatter.model_validate(parsed)
    except ValidationError as error:
        raise AgentFileError(f"{file_name}: {_first_message(error)}") from error


def _first_message(error: ValidationError) -> str:
    first = error.errors(include_input=False, include_url=False)[0]
    field = ".".join(str(part) for part in first["loc"])
    return f"{field}: {first['msg']}" if field else first["msg"]


def _selected_tools(
    file_name: str, tool_names: list[str], declarations: list[ToolDeclaration]
) -> tuple[ToolDeclaration, ...]:
    by_name: dict[str, ToolDeclaration] = {}
    for declaration in declarations:
        if declaration.name in by_name:
            raise AgentFileError(f"{file_name}: tools: {declaration.name!r} is declared twice")
        by_name[declaration.name] = declaration
    for position, tool_name in enumerate(tool_names):
        if tool_name in tool_names[:position]:
            raise AgentFileError(f"{file_name}: tools: {tool_name!r} is listed twice")
        if tool_name not in by_name:
            raise AgentFileError(f"{file_name}: tools: {tool_name!r} is not a declared tool")
    return tuple(by_name[tool_name] for tool_name in tool_names)
