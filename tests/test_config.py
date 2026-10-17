"""The configuration a host installs, cut along the two ways into the library."""

from __future__ import annotations

import logging
import os
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from provider_test_support import override_provider_runtime

from agent_providers.catalog import (
    ProviderRoute,
    ProviderRouteReadinessState,
    probe_provider_route,
)
from agent_providers.config import (
    ProviderRuntimeAlreadyConfiguredError,
    ProviderRuntimeConfig,
    ProviderRuntimeNotConfiguredError,
    TurnRuntimeNotConfiguredError,
    WorkingRootNotPrivateError,
    WorkingRootViolation,
    configure,
    current_config,
    current_turn_config,
    reset_config,
)


def _catalog_only_runtime(tmp_path: Path) -> ProviderRuntimeConfig:
    return ProviderRuntimeConfig(
        claude_cli_binary="/bin/sh",
        grok_cli_binary="/bin/sh",
        codex_cli_binary="/bin/sh",
        cli_binary_search_path=(Path("/usr/bin"),),
        claude_cli_auth_file=tmp_path / "claude.json",
        grok_cli_auth_file=tmp_path / "grok.json",
        codex_cli_auth_file=tmp_path / "codex.json",
        cli_working_directory_root=tmp_path,
    )


def test_a_catalog_host_configures_without_inventing_a_turn_fact(tmp_path: Path) -> None:
    """A host that only asks what a provider offers has no chat model to name."""
    catalog_only = _catalog_only_runtime(tmp_path)
    override_provider_runtime(**catalog_only.model_dump())

    assert current_config().turns is None
    snapshot = probe_provider_route("grok", ProviderRoute.CLI)

    assert snapshot.readiness is ProviderRouteReadinessState.NOT_CONFIGURED


def test_a_turn_under_a_catalog_only_configuration_fails_loudly(tmp_path: Path) -> None:
    override_provider_runtime(**_catalog_only_runtime(tmp_path).model_dump())

    with pytest.raises(TurnRuntimeNotConfiguredError, match="catalog only"):
        current_turn_config()


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


def _below_a_self_looping_symlink(base: Path, _monkeypatch: pytest.MonkeyPatch) -> Path:
    loop = base / "loop"
    loop.symlink_to(loop)
    return loop / "root"


def _with_a_component_longer_than_the_filesystem_allows(
    base: Path, _monkeypatch: pytest.MonkeyPatch,
) -> Path:
    return base / THE_ROOT_NAME.ljust(300, "x")


_REFUSED_ROOTS = (
    pytest.param(WorkingRootViolation.RELATIVE, _relative, id="relative"),
    pytest.param(WorkingRootViolation.MISSING, _absent, id="absent"),
    pytest.param(WorkingRootViolation.MISSING, _a_regular_file, id="regular-file"),
    pytest.param(WorkingRootViolation.MISSING, _below_a_regular_file, id="below-a-regular-file"),
    pytest.param(
        WorkingRootViolation.SYMLINK, _a_symlink_to_a_private_directory, id="symlink",
    ),
    pytest.param(WorkingRootViolation.SYMLINK, _a_dangling_symlink, id="dangling-symlink"),
    pytest.param(WorkingRootViolation.FOREIGN_OWNER, _owned_by_another_user, id="foreign-owner"),
    *(
        pytest.param(WorkingRootViolation.WRONG_MODE, _with_mode(mode), id=f"mode-{mode:o}")
        for mode in (0o755, 0o750, 0o701, 0o500, 0o1700, 0o2700)
    ),
    pytest.param(
        WorkingRootViolation.UNINSPECTABLE, _below_a_self_looping_symlink, id="self-looping-parent",
    ),
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


@pytest.fixture
def refusal(
    request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> _Refusal:
    expected_violation, make_root = request.param
    base = _a_private_directory(tmp_path / THE_ROOT_NAME)
    config = _runtime_with_root(tmp_path, make_root(base, monkeypatch))
    reset_config()
    with caplog.at_level(logging.DEBUG), pytest.raises(WorkingRootNotPrivateError) as raised:
        configure(config)
    return _Refusal(expected_violation, raised.value, list(caplog.records))


_every_refusal = pytest.mark.parametrize(
    "refusal",
    [pytest.param(case.values, id=case.id) for case in _REFUSED_ROOTS],
    indirect=True,
)


def test_the_violations_are_the_closed_set_the_contract_names() -> None:
    assert {member.value for member in WorkingRootViolation} == {
        "relative", "missing", "symlink", "foreign_owner", "wrong_mode", "uninspectable",
    }


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
