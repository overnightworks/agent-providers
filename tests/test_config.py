"""The configuration a host installs, cut along the two ways into the library."""

from __future__ import annotations

import copy
import logging
import os
import pickle
import stat
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest
from provider_test_support import (
    CALLER_CONTEXTS,
    override_grok_runtime,
    override_provider_runtime,
    override_turn_runtime,
    shell_child,
)
from pydantic import ValidationError

from agent_providers.catalog import (
    ProviderRoute,
    ProviderRouteReadinessState,
    probe_provider_route,
)
from agent_providers.claude import provider as claude_provider
from agent_providers.config import (
    GrokTurnRuntime,
    McpServerSpec,
    PrivateWorkingRoot,
    ProviderNotConfiguredError,
    ProviderRuntimeAlreadyConfiguredError,
    ProviderRuntimeConfig,
    ProviderRuntimeNotConfiguredError,
    TurnRuntimeConfig,
    TurnRuntimeNotConfiguredError,
    WorkingRootNotPrivateError,
    WorkingRootViolation,
    configure,
    current_claude_runtime,
    current_codex_runtime,
    current_codex_turn_runtime,
    current_config,
    current_grok_runtime,
    current_grok_turn_runtime,
    current_turn_config,
    reset_config,
)
from agent_providers.process import run_cli_bounded


def _catalog_only_runtime(tmp_path: Path) -> ProviderRuntimeConfig:
    return ProviderRuntimeConfig(
        cli_binary_search_path=(Path("/usr/bin"),),
        cli_working_directory_root=tmp_path,
    )


def test_a_catalog_host_configures_without_inventing_a_turn_fact(tmp_path: Path) -> None:
    """A host that only asks what a provider offers has no chat model to name."""
    catalog_only = _catalog_only_runtime(tmp_path)
    override_provider_runtime(**catalog_only.model_dump())
    override_grok_runtime(binary="/bin/sh", token_file=tmp_path / "grok.json")

    assert current_config().turns is None
    snapshot = probe_provider_route("grok", ProviderRoute.CLI)

    assert snapshot.readiness is ProviderRouteReadinessState.NOT_CONFIGURED


def test_a_turn_under_a_catalog_only_configuration_fails_loudly(tmp_path: Path) -> None:
    override_provider_runtime(**_catalog_only_runtime(tmp_path).model_dump())

    with pytest.raises(TurnRuntimeNotConfiguredError, match="catalog only"):
        current_turn_config()


def test_turn_config_refuses_the_removed_chat_model() -> None:
    sample_fields = current_turn_config().model_dump()

    with pytest.raises(ValidationError) as refused:
        TurnRuntimeConfig(**sample_fields, claude_chat_model="claude-opus-4-6")

    assert [error["type"] for error in refused.value.errors()] == ["extra_forbidden"]
    assert refused.value.errors()[0]["loc"] == ("claude_chat_model",)


THE_ROOT_NAME = "canary-root-5d1e"


def _runtime_with_root(tmp_path: Path, root: Path) -> ProviderRuntimeConfig:
    return _catalog_only_runtime(tmp_path).model_copy(update={"cli_working_directory_root": root})


def _a_private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


def _install_over_nothing(config: ProviderRuntimeConfig) -> None:
    reset_config()
    configure(config)


def _relative(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    return Path(THE_ROOT_NAME) / "relative"


def _absent(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    return base / "absent"


def _a_regular_file(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    root = base / "file"
    root.write_text("")
    return root


def _below_a_regular_file(base: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _a_regular_file(base, monkeypatch) / "root"


def _a_symlink_to_a_private_directory(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    root = base / "link"
    root.symlink_to(_a_private_directory(base / "real"))
    return root


def _a_dangling_symlink(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    root = base / "dangling"
    root.symlink_to(base / "nowhere")
    return root


def _owned_by_another_user(base: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(base).st_uid + 1)
    return _a_private_directory(base / "owned-elsewhere")


def _with_mode(mode: int) -> Callable[[Path, pytest.MonkeyPatch], Path]:
    def root_with_mode(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
        root = _a_private_directory(base / "moded")
        root.chmod(mode)
        return root

    return root_with_mode


def _owned_by_another_user_and_world_readable(
    base: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    root = _owned_by_another_user(base, monkeypatch)
    root.chmod(0o755)
    return root


def _containing_a_nul_byte(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    return base / "nul\0byte"


def _below_a_self_looping_symlink(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    loop = base / "loop"
    loop.symlink_to(loop)
    return loop / "root"


def _with_a_component_longer_than_the_filesystem_allows(
    base: Path, _monkeypatch: pytest.MonkeyPatch,
) -> Path:
    return base / THE_ROOT_NAME.ljust(300, "x")


def _after_a_symlinked_component(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    deep = _a_private_directory(_a_private_directory(base / "real") / "deep")
    _a_private_directory(base / "real" / "root")
    (base / "link").symlink_to(deep)
    return base / "link" / ".." / "root"


def _through_a_real_directory(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    _a_private_directory(base / "sub")
    _a_private_directory(base / "root")
    return base / "sub" / ".." / "root"


def _before_a_missing_target(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    _a_private_directory(base / "sub")
    return base / "sub" / ".." / "absent"


def _ending_in_a_parent(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    return _a_private_directory(base / "root") / ".."


def _relative_with_a_parent(_base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    return Path("..") / THE_ROOT_NAME


_REFUSED_ROOTS = (
    pytest.param(WorkingRootViolation.RELATIVE, _relative, id="relative"),
    pytest.param(
        WorkingRootViolation.RELATIVE, _relative_with_a_parent, id="relative-with-a-parent",
    ),
    pytest.param(
        WorkingRootViolation.PARENT_COMPONENT,
        _after_a_symlinked_component,
        id="parent-after-a-symlinked-component",
    ),
    pytest.param(
        WorkingRootViolation.PARENT_COMPONENT,
        _through_a_real_directory,
        id="parent-through-a-real-directory",
    ),
    pytest.param(
        WorkingRootViolation.PARENT_COMPONENT,
        _before_a_missing_target,
        id="parent-before-a-missing-target",
    ),
    pytest.param(WorkingRootViolation.PARENT_COMPONENT, _ending_in_a_parent, id="trailing-parent"),
    pytest.param(WorkingRootViolation.MISSING, _absent, id="absent"),
    pytest.param(WorkingRootViolation.MISSING, _a_regular_file, id="regular-file"),
    pytest.param(WorkingRootViolation.MISSING, _below_a_regular_file, id="below-a-regular-file"),
    pytest.param(
        WorkingRootViolation.SYMLINK, _a_symlink_to_a_private_directory, id="symlink",
    ),
    pytest.param(WorkingRootViolation.SYMLINK, _a_dangling_symlink, id="dangling-symlink"),
    pytest.param(WorkingRootViolation.FOREIGN_OWNER, _owned_by_another_user, id="foreign-owner"),
    pytest.param(
        WorkingRootViolation.FOREIGN_OWNER,
        _owned_by_another_user_and_world_readable,
        id="foreign-owner-with-wrong-mode",
    ),
    *(
        pytest.param(WorkingRootViolation.WRONG_MODE, _with_mode(mode), id=f"mode-{mode:o}")
        for mode in (0o755, 0o750, 0o701, 0o500, 0o1700, 0o2700)
    ),
    pytest.param(
        WorkingRootViolation.UNINSPECTABLE, _below_a_self_looping_symlink, id="self-looping-parent",
    ),
    pytest.param(WorkingRootViolation.UNINSPECTABLE, _containing_a_nul_byte, id="nul-byte"),
    pytest.param(
        WorkingRootViolation.UNINSPECTABLE,
        _with_a_component_longer_than_the_filesystem_allows,
        id="over-long-component",
    ),
)


@dataclass(frozen=True)
class _Refusal:
    expected_violation: WorkingRootViolation
    error: WorkingRootNotPrivateError
    log_records: list[logging.LogRecord]


def _check_the_root_of(config: ProviderRuntimeConfig) -> None:
    PrivateWorkingRoot(config.cli_working_directory_root)


_REFUSING_ENTRIES = (
    pytest.param(configure, id="configure"),
    pytest.param(_check_the_root_of, id="private-working-root"),
)


def _entered_while_handling_an_exception_naming_the_root(
    entry: Callable[[ProviderRuntimeConfig], None], config: ProviderRuntimeConfig,
) -> None:
    try:
        raise PermissionError(13, "denied", str(Path("/") / THE_ROOT_NAME))
    except PermissionError:
        entry(config)


@pytest.fixture(params=_REFUSING_ENTRIES)
def refusing_entry(request) -> Callable[[ProviderRuntimeConfig], None]:
    return request.param


@pytest.fixture
def refusal(
    request,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    refusing_entry: Callable[[ProviderRuntimeConfig], None],
    while_the_caller_handles_an_exception: bool,
) -> _Refusal:
    expected_violation, make_root = request.param
    base = _a_private_directory(tmp_path / THE_ROOT_NAME)
    config = _runtime_with_root(tmp_path, make_root(base, monkeypatch))
    reset_config()
    install = (
        partial(_entered_while_handling_an_exception_naming_the_root, refusing_entry)
        if while_the_caller_handles_an_exception
        else refusing_entry
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(WorkingRootNotPrivateError) as raised:
        install(config)
    return _Refusal(expected_violation, raised.value, list(caplog.records))


_every_refused_root = pytest.mark.parametrize(
    "refusal",
    [pytest.param(case.values, id=case.id) for case in _REFUSED_ROOTS],
    indirect=True,
)


def _every_refusal(test: Callable[..., None]) -> Callable[..., None]:
    return _every_refused_root(CALLER_CONTEXTS(test))


def test_the_violations_are_the_closed_set_the_contract_names() -> None:
    assert {member.value for member in WorkingRootViolation} == {
        "relative", "missing", "symlink", "foreign_owner", "wrong_mode", "uninspectable",
        "parent_component",
    }


@pytest.mark.parametrize("name", ["..root", "a..b", "root."])
def test_a_root_whose_names_only_contain_dots_is_accepted(tmp_path: Path, name: str) -> None:
    root = _a_private_directory(tmp_path / name)
    config = _runtime_with_root(tmp_path, root)

    _install_over_nothing(config)

    assert (current_config(), PrivateWorkingRoot(root).path) == (config, root)


def test_a_private_root_is_installed(tmp_path: Path) -> None:
    config = _runtime_with_root(tmp_path, _a_private_directory(tmp_path / THE_ROOT_NAME))

    _install_over_nothing(config)

    assert current_config() == config


def test_a_private_root_below_a_symlinked_parent_is_installed(tmp_path: Path) -> None:
    real = _a_private_directory(tmp_path / "real")
    (tmp_path / "link").symlink_to(real)
    _a_private_directory(real / "root")
    config = _runtime_with_root(tmp_path, tmp_path / "link" / "root")

    _install_over_nothing(config)

    assert current_config() == config


@pytest.mark.parametrize(
    "below_a_symlinked_parent", [False, True], ids=["plain", "symlinked-parent"],
)
def test_a_private_root_constructs_with_no_runtime_installed(
    tmp_path: Path, below_a_symlinked_parent: bool,
) -> None:
    real = _a_private_directory(tmp_path / "real")
    root = _a_private_directory(real / "root")
    if below_a_symlinked_parent:
        (tmp_path / "link").symlink_to(real)
        root = tmp_path / "link" / "root"
    reset_config()
    with pytest.raises(ProviderRuntimeNotConfiguredError):
        current_config()

    assert PrivateWorkingRoot(root).path == root


def test_an_identical_reinstall_of_a_private_root_is_a_no_op(tmp_path: Path) -> None:
    config = _runtime_with_root(tmp_path, _a_private_directory(tmp_path / THE_ROOT_NAME))
    _install_over_nothing(config)

    configure(config)

    assert current_config() == config


def test_an_identical_reinstall_inspects_the_root_again(tmp_path: Path) -> None:
    root = _a_private_directory(tmp_path / THE_ROOT_NAME)
    config = _runtime_with_root(tmp_path, root)
    _install_over_nothing(config)
    root.chmod(0o755)

    with pytest.raises(WorkingRootNotPrivateError) as raised:
        configure(config)

    assert raised.value.violation is WorkingRootViolation.WRONG_MODE
    assert current_config() == config


@_every_refusal
def test_a_refusal_names_only_the_closed_violation(refusal: _Refusal) -> None:
    error = refusal.error

    assert error.violation is refusal.expected_violation
    assert str(error) == f"cli_working_directory_root is not private: {error.violation.value}."
    assert error.args == (str(error),)


@_every_refusal
def test_a_refusal_leaks_neither_the_path_nor_a_chain(refusal: _Refusal) -> None:
    error = refusal.error
    rendered = (
        str(error), repr(error), repr(error.args), "".join(traceback.format_exception(error)),
    )

    assert not any(THE_ROOT_NAME in text for text in rendered)
    assert error.__cause__ is None
    assert error.__context__ is None


@_every_refusal
def test_a_refused_fresh_configure_installs_nothing(refusal: _Refusal) -> None:
    with pytest.raises(ProviderRuntimeNotConfiguredError):
        current_config()


@_every_refusal
def test_a_refusal_writes_no_log_record(refusal: _Refusal) -> None:
    assert refusal.log_records == []


def test_a_refused_root_wins_over_a_differing_installation(tmp_path: Path) -> None:
    installed = _runtime_with_root(tmp_path, _a_private_directory(tmp_path / THE_ROOT_NAME))
    _install_over_nothing(installed)
    refused = _runtime_with_root(tmp_path, tmp_path / "absent")

    with pytest.raises(WorkingRootNotPrivateError) as raised:
        configure(refused)

    assert not isinstance(raised.value, ProviderRuntimeAlreadyConfiguredError)
    assert current_config() == installed


@pytest.mark.parametrize(
    "duplicate",
    [lambda error: pickle.loads(pickle.dumps(error)), copy.copy, copy.deepcopy],
    ids=["pickle", "copy", "deepcopy"],
)
def test_a_refusal_survives_being_copied_with_its_violation(
    duplicate: Callable[[WorkingRootNotPrivateError], WorkingRootNotPrivateError],
) -> None:
    original = WorkingRootNotPrivateError(WorkingRootViolation.WRONG_MODE)

    duplicated = duplicate(original)

    assert duplicated.violation is original.violation
    assert duplicated.args == original.args


def test_a_claude_call_without_a_claude_block_names_the_provider(tmp_path: Path) -> None:
    override_provider_runtime(**_catalog_only_runtime(tmp_path).model_dump())

    with pytest.raises(ProviderNotConfiguredError) as refused:
        current_claude_runtime()

    assert (refused.value.provider, refused.value.block) == ("claude", "claude")
    assert "claude" in str(refused.value)


@pytest.mark.parametrize(
    "removed_flat_field",
    [
        pytest.param("claude_cli_binary", id="binary"),
        pytest.param("claude_cli_binary_search_globs", id="binary-search-globs"),
        pytest.param("claude_cli_auth_file", id="auth-file"),
    ],
)
def test_a_host_with_a_removed_flat_claude_field_is_refused_naming_the_field(
    removed_flat_field: str, tmp_path: Path,
) -> None:
    facts = {**_catalog_only_runtime(tmp_path).model_dump(), removed_flat_field: "/bin/sh"}

    with pytest.raises(ValidationError, match=removed_flat_field):
        ProviderRuntimeConfig(**facts)


def test_a_grok_call_without_a_grok_block_names_the_provider(tmp_path: Path) -> None:
    override_provider_runtime(**_catalog_only_runtime(tmp_path).model_dump())

    with pytest.raises(ProviderNotConfiguredError) as refused:
        current_grok_runtime()

    assert (refused.value.provider, refused.value.block) == ("grok", "grok")
    assert "grok" in str(refused.value)


def test_a_grok_turn_without_a_grok_turn_block_names_the_provider() -> None:
    override_turn_runtime(grok=None)

    with pytest.raises(ProviderNotConfiguredError) as refused:
        current_grok_turn_runtime()

    assert (refused.value.provider, refused.value.block) == ("grok", "turns.grok")


@pytest.mark.parametrize(
    "removed_flat_field",
    [
        pytest.param("grok_cli_binary", id="binary"),
        pytest.param("grok_cli_auth_file", id="auth-file"),
    ],
)
def test_a_host_with_a_removed_flat_grok_field_is_refused_naming_the_field(
    removed_flat_field: str, tmp_path: Path,
) -> None:
    facts = {**_catalog_only_runtime(tmp_path).model_dump(), removed_flat_field: "/bin/sh"}

    with pytest.raises(ValidationError, match=removed_flat_field):
        ProviderRuntimeConfig(**facts)


@pytest.mark.parametrize(
    "removed_flat_field",
    [
        pytest.param("cli_prompt_file_prefix", id="prompt-file-prefix"),
        pytest.param("cli_prompt_file_placeholder", id="prompt-file-placeholder"),
    ],
)
def test_a_host_with_a_removed_flat_prompt_file_field_is_refused_naming_the_field(
    removed_flat_field: str,
) -> None:
    facts = {**current_turn_config().model_dump(), removed_flat_field: "notes-"}

    with pytest.raises(ValidationError, match=removed_flat_field):
        TurnRuntimeConfig(**facts)


def test_a_codex_call_without_a_codex_block_names_the_provider(tmp_path: Path) -> None:
    override_provider_runtime(**_catalog_only_runtime(tmp_path).model_dump())

    with pytest.raises(ProviderNotConfiguredError) as refused:
        current_codex_runtime()

    assert (refused.value.provider, refused.value.block) == ("codex", "codex")
    assert "codex" in str(refused.value)


def test_a_codex_turn_without_a_codex_turn_block_names_the_provider() -> None:
    override_turn_runtime(codex=None)

    with pytest.raises(ProviderNotConfiguredError) as refused:
        current_codex_turn_runtime()

    assert (refused.value.provider, refused.value.block) == ("codex", "turns.codex")


@pytest.mark.parametrize(
    "removed_flat_field",
    [
        pytest.param("codex_cli_binary", id="binary"),
        pytest.param("codex_cli_auth_file", id="auth-file"),
    ],
)
def test_a_host_with_a_removed_flat_codex_field_is_refused_naming_the_field(
    removed_flat_field: str, tmp_path: Path,
) -> None:
    facts = {**_catalog_only_runtime(tmp_path).model_dump(), removed_flat_field: "/bin/sh"}

    with pytest.raises(ValidationError, match=removed_flat_field):
        ProviderRuntimeConfig(**facts)


@pytest.mark.parametrize(
    "removed_flat_field",
    [
        pytest.param("codex_code_mode_host_binary", id="code-mode-host-binary"),
        pytest.param("codex_resources_directory", id="resources-directory"),
        pytest.param("codex_max_concurrent_processes", id="max-concurrent-processes"),
        pytest.param("codex_max_concurrent_image_runs", id="max-concurrent-image-runs"),
    ],
)
def test_a_host_with_a_removed_flat_codex_turn_field_is_refused_naming_the_field(
    removed_flat_field: str,
) -> None:
    facts = {**current_turn_config().model_dump(), removed_flat_field: "/bin/sh"}

    with pytest.raises(ValidationError, match=removed_flat_field):
        TurnRuntimeConfig(**facts)


_PREFIX_MODELS = {"mcp_server": McpServerSpec, "grok": GrokTurnRuntime}
_PREFIX_FIELDS = [
    pytest.param("mcp_server", "config_file_prefix", id="mcp-config"),
    pytest.param("grok", "prompt_file_prefix", id="grok-prompt"),
]


def _turns_facts_with(block: str, field: str, value: object) -> dict:
    turns = current_turn_config().model_dump()
    turns[block][field] = value
    return turns


def _host_facts_with(block: str, field: str, value: object, root: Path) -> dict:
    return {
        **current_config().model_dump(),
        "cli_working_directory_root": root,
        "turns": _turns_facts_with(block, field, value),
    }


def _the_prefix_block_built_with(block: str, field: str, value: object) -> object:
    return _PREFIX_MODELS[block](**_turns_facts_with(block, field, value)[block])


def _refusal_of(build: Callable[[], object]) -> ValidationError:
    with pytest.raises(ValidationError) as raised:
        build()
    return raised.value


_PREFIX_ENTRY_LEVELS = [
    pytest.param(
        lambda block, field, value, root: _the_prefix_block_built_with(block, field, value),
        id="block",
    ),
    pytest.param(
        lambda block, field, value, root: TurnRuntimeConfig(
            **_turns_facts_with(block, field, value),
        ),
        id="turn-config",
    ),
    pytest.param(
        lambda block, field, value, root: configure(
            ProviderRuntimeConfig.model_validate(_host_facts_with(block, field, value, root)),
        ),
        id="host-config",
    ),
]

_PREFIXES_THAT_COULD_LEAVE_THE_ROOT = ["/tmp/x-", "../esc-", "../", "sub/", "a/b-", "/", "a\x00b"]
_PREFIXES_OUTSIDE_THE_CLOSED_NAME_SET = ["a b-", "x*", "ok\n", "pr\u00e4fix-"]
_PLAIN_PREFIXES = ["notes-mcp-", ".hid-", "a_b.c-1", "NOTES-CLI-", ".", ".."]
_BOUNDARY_PREFIXES = [
    pytest.param("..", id="two-dots"),
    pytest.param(".", id="one-dot"),
    pytest.param("A" * 64, id="64-uppercase"),
    pytest.param("NOTES-", id="uppercase-stem"),
]


@pytest.mark.parametrize(("block", "field"), _PREFIX_FIELDS)
@pytest.mark.parametrize("prefix", _PREFIXES_THAT_COULD_LEAVE_THE_ROOT)
def test_a_file_prefix_that_could_leave_the_root_is_refused_by_both_hosts_fields(
    block: str, field: str, prefix: str,
) -> None:
    refused = _refusal_of(lambda: _the_prefix_block_built_with(block, field, prefix))

    assert refused.errors(include_input=False)[0]["loc"] == (field,)


@pytest.mark.parametrize(("block", "field"), _PREFIX_FIELDS)
@pytest.mark.parametrize("prefix", _PREFIXES_OUTSIDE_THE_CLOSED_NAME_SET)
def test_a_file_prefix_outside_the_closed_name_set_is_refused(
    block: str, field: str, prefix: str,
) -> None:
    refused = _refusal_of(lambda: _the_prefix_block_built_with(block, field, prefix))

    assert refused.errors(include_input=False)[0]["loc"] == (field,)


@pytest.mark.parametrize(("block", "field"), _PREFIX_FIELDS)
def test_a_file_prefix_is_accepted_at_64_characters(block: str, field: str) -> None:
    accepted = _the_prefix_block_built_with(block, field, "a" * 64)

    assert getattr(accepted, field) == "a" * 64


@pytest.mark.parametrize(("block", "field"), _PREFIX_FIELDS)
@pytest.mark.parametrize(
    "prefix",
    [pytest.param("a" * 65, id="65-characters"), pytest.param("", id="empty")],
)
def test_a_file_prefix_beyond_the_length_bounds_is_refused(
    block: str, field: str, prefix: str,
) -> None:
    refused = _refusal_of(lambda: _the_prefix_block_built_with(block, field, prefix))

    assert refused.errors(include_input=False)[0]["loc"] == (field,)


@pytest.mark.parametrize(("block", "field"), _PREFIX_FIELDS)
@pytest.mark.parametrize("prefix", _PLAIN_PREFIXES)
def test_a_plain_file_prefix_is_accepted(block: str, field: str, prefix: str) -> None:
    accepted = _the_prefix_block_built_with(block, field, prefix)

    assert getattr(accepted, field) == prefix


@pytest.mark.parametrize("entry", _PREFIX_ENTRY_LEVELS)
@pytest.mark.parametrize(("block", "field"), _PREFIX_FIELDS)
def test_a_refused_file_prefix_error_names_the_field_and_never_holds_the_value(
    block: str, field: str, entry: Callable[..., object], tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "canary/prefix-7f3a"

    with caplog.at_level(logging.DEBUG):
        refused = _refusal_of(lambda: entry(block, field, canary, tmp_path))

    assert field in str(refused)
    assert "canary" not in str(refused) + repr(refused) + caplog.text


@pytest.mark.parametrize(("block", "field"), _PREFIX_FIELDS)
def test_a_refused_file_prefix_in_a_host_config_installs_nothing_and_keeps_the_earlier_install(
    block: str, field: str, tmp_path: Path,
) -> None:
    earlier = current_config()
    facts = _host_facts_with(block, field, "../esc-", tmp_path)

    refused = _refusal_of(lambda: configure(ProviderRuntimeConfig.model_validate(facts)))

    assert refused.errors(include_input=False)[0]["loc"] == ("turns", block, field)
    assert current_config() is earlier


@pytest.mark.parametrize("prefix", _BOUNDARY_PREFIXES)
def test_an_accepted_mcp_config_prefix_writes_a_mode_0600_file_directly_in_the_root(
    prefix: str, tmp_path: Path,
) -> None:
    override_provider_runtime(cli_working_directory_root=tmp_path)
    spec = _the_prefix_block_built_with("mcp_server", "config_file_prefix", prefix)

    path = Path(claude_provider._write_mcp_config(spec, "u-1"))

    assert path.parent == tmp_path
    assert path.name.startswith(prefix)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("prefix", _BOUNDARY_PREFIXES)
def test_an_accepted_grok_prompt_prefix_writes_a_mode_0600_file_directly_in_the_root(
    prefix: str, tmp_path: Path,
) -> None:
    override_provider_runtime(cli_working_directory_root=tmp_path)
    grok = _the_prefix_block_built_with("grok", "prompt_file_prefix", prefix)

    outcome = run_cli_bounded(
        shell_child("-c", 'printf "%s %s" "$1" "$(stat -c %a "$1")"', "unused", "placeholder"),
        stdin_payload=None,
        read="all",
        deadline=time.monotonic() + 5,
        prompt_file_bytes=b"private prompt",
        prompt_file_arg_index=3,
        prompt_file_prefix=grok.prompt_file_prefix,
    )
    path, mode = outcome.stdout.rsplit(" ", 1)

    assert Path(path).parent == tmp_path
    assert Path(path).name.startswith(prefix)
    assert mode == "600"
