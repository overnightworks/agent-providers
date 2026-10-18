"""The one seam an async Claude test gets its child through, and the guard that keeps it.

Async Claude behaviour tests take their child from ``a_claude_cli_child`` and
install it with ``install_claude_cli_children``, the only place that names
``provider.open_async_pipes``. The guard walks the modules listed in
``GUARDED_MODULES`` and fails on any patch of ``asyncio.create_subprocess_exec``
or of ``open_async_pipes`` there.

Two modules are deliberate exceptions and are not listed: ``test_spawn_owner.py``
(the spawn-policy tests, which name ``create_subprocess_exec`` as a string and
run the real door) and the anchored-door coexistence test in
``test_anchored_door.py``, which awaits a stock ``create_subprocess_exec``
child beside an anchored zombie.
"""

from __future__ import annotations

import ast
import asyncio
import textwrap
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from anchored_support import TEST_WAIT_SECONDS
from provider_test_support import (
    EventGate,
    ScenarioDeadline,
    a_claude_cli_child,
    install_claude_cli_children,
    scenario_holds,
)

from agent_providers.claude import provider
from agent_providers.spawn import ChildProcess, closed_environment

GUARDED_MODULES = (
    "test_claude_provider.py",
    "test_claude_adapter.py",
    "test_claude_provider_streaming.py",
)
PATCHING_CALLS = frozenset({"setattr", "patch", "object"})
GUARDED_DOOR_NAMES = frozenset({"create_subprocess_exec", "open_async_pipes"})


def patched_door_lines(source: str) -> list[int]:
    """Line numbers of every call that patches an async spawn door by name."""
    tree = ast.parse(source)
    patching_calls = PATCHING_CALLS | _patch_aliases(tree)
    return [
        call.lineno
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and _callee_name(call) in patching_calls
        and any(
            _names_a_door(argument)
            for argument in [*call.args, *(keyword.value for keyword in call.keywords)]
        )
    ]


def _patch_aliases(tree: ast.AST) -> frozenset[str]:
    return frozenset(
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "unittest.mock"
        for alias in node.names
        if alias.name == "patch" and alias.asname
    )


def _callee_name(call: ast.Call) -> str | None:
    callee = call.func
    if isinstance(callee, ast.Attribute):
        return callee.attr
    return callee.id if isinstance(callee, ast.Name) else None


def _names_a_door(argument: ast.expr) -> bool:
    return (
        isinstance(argument, ast.Constant)
        and isinstance(argument.value, str)
        and argument.value.rsplit(".", 1)[-1] in GUARDED_DOOR_NAMES
    )


@pytest.mark.parametrize(
    "patch_spelling",
    [
        'monkeypatch.setattr("asyncio.create_subprocess_exec", f)',
        'patch("asyncio.create_subprocess_exec", f)',
        'patch.object(asyncio, "create_subprocess_exec", f)',
        'monkeypatch.setattr(asyncio, "create_subprocess_exec", f)',
        'monkeypatch.setattr(provider.asyncio, "create_subprocess_exec", f)',
        'monkeypatch.setattr(\n    provider.asyncio,\n    "create_subprocess_exec",\n    f,\n)',
        'monkeypatch.setattr(provider, "open_async_pipes", f)',
        'patch("agent_providers.claude.provider.open_async_pipes", f)',
        'from unittest.mock import patch as replace\nreplace("asyncio.create_subprocess_exec", f)',
        'patch(target="asyncio.create_subprocess_exec", new=f)',
        'monkeypatch.setattr(target=provider, name="open_async_pipes", value=f)',
    ],
)
def test_the_guard_flags_every_spelling_of_a_door_patch(patch_spelling: str) -> None:
    assert patched_door_lines(
        "def test():\n" + textwrap.indent(patch_spelling, "    ") + "\n",
    ) != []


@pytest.mark.parametrize(
    "harmless_source",
    [
        '"""asyncio.create_subprocess_exec() still runs subprocess.Popen()."""',
        'name = "create_subprocess_exec"',
        'monkeypatch.setattr(spawn.subprocess, "Popen", f)',
        'patch("subprocess.run", return_value=completed)',
    ],
)
def test_the_guard_ignores_text_and_patches_of_other_doors(harmless_source: str) -> None:
    assert patched_door_lines(harmless_source + "\n") == []


@pytest.mark.parametrize("module", GUARDED_MODULES)
def test_a_guarded_module_patches_no_async_spawn_door(module: str) -> None:
    source = (Path(__file__).parent / module).read_text()

    assert patched_door_lines(source) == []


def test_the_builder_answers_communicate_and_exposes_the_child_surface() -> None:
    child = a_claude_cli_child(
        pid=77, returncode=3, communicate=(b"out", b"err"), stream_lines=[b"line\n"],
        stderr=b"warning",
    )

    async def drive() -> tuple[tuple[bytes, bytes], bytes, bytes, int]:
        return (
            await child.communicate(b"prompt"),
            await child.stdout.readline(),
            await child.stderr.read(),
            await child.wait(),
        )

    assert asyncio.run(drive()) == ((b"out", b"err"), b"line\n", b"warning", 0)
    assert (child.pid, child.returncode) == (77, 3)
    child.communicate.assert_awaited_once_with(b"prompt")


def test_the_builder_child_and_its_stdin_refuse_attributes_a_process_lacks() -> None:
    child = a_claude_cli_child()

    with pytest.raises(AttributeError):
        child.not_a_process_attribute  # noqa: B018
    with pytest.raises(AttributeError):
        child.stdin.not_a_pipe_attribute  # noqa: B018


def test_the_builder_raises_the_communicate_error_it_was_given() -> None:
    child = a_claude_cli_child(communicate=asyncio.TimeoutError())

    communicating = child.communicate(b"prompt")
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(communicating)


def test_the_builder_runs_without_stdin_and_while_still_running() -> None:
    child = a_claude_cli_child(returncode=None, has_stdin=False)

    assert child.stdin is None
    assert child.returncode is None


def test_the_builder_reader_refuses_a_line_beyond_the_limit_it_was_built_with() -> None:
    child = a_claude_cli_child(stream_lines=[b"x" * 11 + b"\n"], stdout_limit=10)

    reading = child.stdout.readline()
    with pytest.raises(ValueError, match="exceed the limit"):
        asyncio.run(reading)


@pytest.mark.parametrize("gated_call", ["communicate", "wait"])
def test_a_gated_child_call_completes_only_after_its_release(gated_call: str) -> None:
    deadline = ScenarioDeadline()
    gate = EventGate(deadline)
    child = a_claude_cli_child(**{f"{gated_call}_gate": gate})
    call = getattr(child, gated_call)

    async def drive() -> None:
        calls: list[asyncio.Future[Any]] = []
        async with scenario_holds([gate], calls, set()), asyncio.timeout(deadline.remaining()):
            pending = asyncio.ensure_future(call(b"") if gated_call == "communicate" else call())
            calls.append(pending)
            await gate.started.wait()
            assert not pending.done()
            gate.release.set()
            await pending

    asyncio.run(drive())


class _TaskFault(Exception):
    """A registered task's own failure."""


@dataclass
class _HeldScenario:
    """One scenario of ``scenario_holds``: a real gate and the tasks held on it."""

    gate: EventGate
    tasks: list[asyncio.Future[Any]] = field(default_factory=list)
    asserted_faults: set[asyncio.Future[Any]] = field(default_factory=set)
    body_failure: AssertionError = field(
        default_factory=lambda: AssertionError("the scenario body failed"),
    )

    def register_task_ending_with(self, outcome: BaseException | None) -> asyncio.Task[None]:
        task = asyncio.create_task(self._end_after_the_hold(outcome))
        self.tasks.append(task)
        return task

    async def _end_after_the_hold(self, outcome: BaseException | None) -> None:
        await self.gate.pass_through()
        if outcome is not None:
            raise outcome


@dataclass(frozen=True)
class _HoldsCase:
    body: Callable[[_HeldScenario], Awaitable[None]]
    expected_failure: Callable[[_HeldScenario], type[BaseException] | None]
    reported: Callable[[_HeldScenario, BaseException], bool]


async def _body_fails_while_tasks_pend_and_a_gate_missed(held: _HeldScenario) -> None:
    held.register_task_ending_with(_TaskFault("first"))
    held.register_task_ending_with(None)
    held.gate.missed.set()
    raise held.body_failure


async def _body_completes_while_two_tasks_fault(held: _HeldScenario) -> None:
    held.register_task_ending_with(_TaskFault("first"))
    held.register_task_ending_with(_TaskFault("second"))
    held.register_task_ending_with(None)


async def _body_cancels_a_task_and_asserts_an_expected_fault(held: _HeldScenario) -> None:
    held.register_task_ending_with(None).cancel()
    expected_fault = held.register_task_ending_with(_TaskFault("expected"))
    held.gate.release.set()
    with pytest.raises(_TaskFault):
        await expected_fault
    held.asserted_faults.add(expected_fault)


async def _body_completes_with_a_missed_gate(held: _HeldScenario) -> None:
    held.gate.missed.set()


async def _body_fails_before_asserting_an_expected_fault(held: _HeldScenario) -> None:
    held.register_task_ending_with(_TaskFault("expected"))
    raise held.body_failure


async def _body_completes_without_asserting_an_expected_fault(held: _HeldScenario) -> None:
    held.register_task_ending_with(_TaskFault("expected"))


def _the_body_failure_itself(held: _HeldScenario, raised: BaseException) -> bool:
    return raised is held.body_failure


def _every_task_fault_in_one_group(held: _HeldScenario, raised: BaseException) -> bool:
    return isinstance(raised, ExceptionGroup) and sorted(
        str(fault) for fault in raised.exceptions
    ) == ["first", "second"]


def _the_unasserted_fault(held: _HeldScenario, raised: BaseException) -> bool:
    return isinstance(raised, _TaskFault) and str(raised) == "expected"


def _the_recorded_miss(held: _HeldScenario, raised: BaseException) -> bool:
    return "outlived its deadline" in str(raised)


HOLDS_CASES = {
    "body_fails_with_pending_tasks": _HoldsCase(
        _body_fails_while_tasks_pend_and_a_gate_missed,
        lambda _held: AssertionError,
        _the_body_failure_itself,
    ),
    "body_completes_with_two_task_faults": _HoldsCase(
        _body_completes_while_two_tasks_fault, lambda _held: ExceptionGroup,
        _every_task_fault_in_one_group,
    ),
    "cancelled_and_asserted_tasks_are_not_faults": _HoldsCase(
        _body_cancels_a_task_and_asserts_an_expected_fault, lambda _held: None,
        lambda _held, _raised: True,
    ),
    "body_completes_with_a_missed_gate": _HoldsCase(
        _body_completes_with_a_missed_gate, lambda _held: pytest.fail.Exception,
        _the_recorded_miss,
    ),
    "body_fails_before_asserting_the_expected_fault": _HoldsCase(
        _body_fails_before_asserting_an_expected_fault, lambda _held: AssertionError,
        _the_body_failure_itself,
    ),
    "body_completes_without_asserting_the_expected_fault": _HoldsCase(
        _body_completes_without_asserting_an_expected_fault, lambda _held: _TaskFault,
        _the_unasserted_fault,
    ),
}


@pytest.mark.parametrize("case", HOLDS_CASES.values(), ids=HOLDS_CASES)
def test_scenario_holds_drains_every_task_without_masking_the_body(case: _HoldsCase) -> None:
    held = _HeldScenario(EventGate(ScenarioDeadline()))
    expected_failure = case.expected_failure(held)
    expectation: AbstractContextManager[Any] = (
        nullcontext() if expected_failure is None else pytest.raises(expected_failure)
    )

    async def drive() -> None:
        async with asyncio.timeout(2 * TEST_WAIT_SECONDS):
            async with scenario_holds([held.gate], held.tasks, held.asserted_faults):
                await case.body(held)

    with expectation as raised:
        asyncio.run(drive())

    assert all(task.done() for task in held.tasks)
    assert held.gate.release.is_set()
    assert expected_failure is None or case.reported(held, raised.value)


def test_a_stalled_child_stdout_stays_pending_after_its_lines() -> None:
    child = a_claude_cli_child(stream_lines=[b"line\n"], stdout_stalls=True)

    async def drive() -> None:
        assert await child.stdout.readline() == b"line\n"
        stalled = asyncio.ensure_future(child.stdout.readline())
        await asyncio.wait({stalled}, timeout=0)
        assert not stalled.done()
        stalled.cancel()

    asyncio.run(drive())


def _a_child_to_start() -> ChildProcess:
    home = Path("/")
    return ChildProcess(
        binary=Path("/usr/bin/claude"),
        arguments=("-p",),
        environment=closed_environment(home),
        working_directory=home,
    )


def test_the_installer_hands_out_queued_children_in_order_and_records_each_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = a_claude_cli_child(pid=1), a_claude_cli_child(pid=2)
    spawns = install_claude_cli_children(monkeypatch, first, second)
    to_start = _a_child_to_start()

    async def start_twice() -> list[object]:
        return [
            await provider.open_async_pipes(to_start, stream_buffer_limit=4096),
            await provider.open_async_pipes(to_start),
        ]

    assert asyncio.run(start_twice()) == [first, second]
    assert spawns.children == [to_start, to_start]
    assert spawns.stream_buffer_limits == [4096, None]


def test_the_installer_builds_a_child_from_the_factory_once_the_queue_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = a_claude_cli_child(pid=1)
    spawns = install_claude_cli_children(
        monkeypatch,
        queued,
        factory=lambda _child, limit: a_claude_cli_child(pid=200, stdout_limit=limit),
    )
    to_start = _a_child_to_start()

    async def start_twice() -> list[int]:
        return [
            (await provider.open_async_pipes(to_start, stream_buffer_limit=64)).pid
            for _ in range(2)
        ]

    assert asyncio.run(start_twice()) == [1, 200]
    assert spawns.stream_buffer_limits == [64, 64]


def test_the_installer_fails_a_spawn_with_nothing_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns = install_claude_cli_children(monkeypatch)

    spawning = provider.open_async_pipes(_a_child_to_start())
    with pytest.raises(pytest.fail.Exception, match="none queued"):
        asyncio.run(spawning)

    assert len(spawns.children) == 1


def test_the_installer_shows_an_empty_record_when_nothing_was_spawned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns = install_claude_cli_children(monkeypatch)

    assert spawns.children == []
    assert spawns.stream_buffer_limits == []
