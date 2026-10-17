"""The service process closes the loggers of httpx and httpcore before it serves.

A logger above CRITICAL makes ``isEnabledFor`` fail before any record exists, so every call the
dependencies make, and every call on a logger made later, leaves no record for any handler a host
installs. Destructive logging configuration runs only in fresh interpreters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
import uvicorn
from capability_support import capability_carrying
from mcp_app_fake import (
    CERTIFICATE_HOST,
    FakeResponse,
    ScriptedApp,
    TestAuthority,
    serving,
)
from service_support import (
    UNTOUCHED_LOGGER,
    LoggerState,
    fake_app_authority,  # noqa: F401 - a fixture, requested by name
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    plain_service_config,
)
from test_service_claude_version import PRINTS_THE_PIN, _config_file_naming, _printing

import agent_service.app as app_module
from agent_service.app import create_app
from agent_service.app_tools import (
    UpstreamRefused,
    UpstreamUnreachable,
    open_app_tools,
)
from agent_service.cli import (
    CLOSED_LOGGER_LEVEL,
    EXIT_BAD_CONFIG,
    close_transport_loggers,
    main,
)

CAPABILITY = capability_carrying("canary.capability.sig")
CLIENT_DEADLINE_SECONDS = 30
CHILD_DEADLINE_SECONDS = 60
EVERY_LEVEL = (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL)
TRANSPORT_ROOTS = ("httpx", "httpcore")
CLOSED_NAMES = ("httpx", "httpcore", "httpcore.http11", "httpcore.connection", "httpcore.proxy")
EXISTING_CHILDREN_AT_DEBUG = (
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.proxy",
    "httpcore.an_arbitrary_existing_child",
)
REFUSAL = FakeResponse(status=401, content_type=None)


class RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self.received: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.received.append(record)


@pytest.fixture
def record_creation(monkeypatch: pytest.MonkeyPatch, logging_state_restored: None) -> list[str]:
    created: list[str] = []
    build_record = logging.getLogRecordFactory()

    def noting(*arguments: object, **keywords: object) -> logging.LogRecord:
        created.append(str(arguments[0]))
        return build_record(*arguments, **keywords)

    logging.setLogRecordFactory(noting)
    monkeypatch.setattr(logging, "lastResort", RecordingHandler())
    return created


@contextmanager
def recorded_on(logger: logging.Logger) -> Iterator[RecordingHandler]:
    handler = RecordingHandler()
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


def calls_at_every_level(logger: logging.Logger) -> None:
    logger.info("info")
    logger.debug("debug")
    logger.warning("warning")
    logger.exception("exception")
    for level in EVERY_LEVEL:
        logger.log(level, "log")


def on_the_transport_hierarchy(created: list[str]) -> list[str]:
    return [name for name in created if name.startswith(TRANSPORT_ROOTS)]


@pytest.mark.parametrize("name", [*CLOSED_NAMES, EXISTING_CHILDREN_AT_DEBUG[-1]])
def test_a_closed_logger_creates_no_record_for_any_call_the_dependencies_make(
    record_creation, name
):
    logger = logging.getLogger(name)
    logging.getLogger().setLevel(logging.DEBUG)
    for existing in EXISTING_CHILDREN_AT_DEBUG:
        logging.getLogger(existing).setLevel(logging.DEBUG)

    close_transport_loggers()
    calls_at_every_level(logger)

    assert record_creation == []


def test_a_logger_made_after_the_closing_inherits_it(record_creation):
    close_transport_loggers()

    later = logging.getLogger("httpcore.later.deep")
    calls_at_every_level(later)

    assert not later.isEnabledFor(logging.CRITICAL)
    assert later.getEffectiveLevel() == CLOSED_LOGGER_LEVEL
    assert record_creation == []


def test_a_handler_on_a_closed_logger_receives_no_critical_call(record_creation):
    child = logging.getLogger("httpcore.http11")
    close_transport_loggers()

    with recorded_on(child) as handler:
        child.critical("critical")

    assert handler.received == []


def test_the_last_resort_receives_no_critical_call_on_a_closed_logger(record_creation):
    child = logging.getLogger("httpcore.http11")
    logging.getLogger().handlers[:] = []
    close_transport_loggers()

    child.critical("critical")

    assert logging.lastResort.received == []


def all_logger_states() -> dict[str, LoggerState]:
    manager = logging.Logger.manager
    states = {
        name: LoggerState.of(logger)
        for name, logger in manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    return {"": LoggerState.of(logging.getLogger()), **states}


def is_closed_by_the_closing(name: str) -> bool:
    return name in TRANSPORT_ROOTS or name.startswith("httpcore.")


def test_closing_twice_changes_only_the_levels_of_the_transport_loggers(logging_state_restored):
    before = all_logger_states()
    disable_before = logging.Logger.manager.disable

    close_transport_loggers()
    once = all_logger_states()
    close_transport_loggers()
    twice = all_logger_states()

    assert once == twice
    assert {name: state for name, state in once.items() if not is_closed_by_the_closing(name)} == {
        name: before.get(name, UNTOUCHED_LOGGER)
        for name in once
        if not is_closed_by_the_closing(name)
    }
    closed = {name: state for name, state in once.items() if is_closed_by_the_closing(name)}
    assert {name: replace(state, level=logging.NOTSET) for name, state in closed.items()} == {
        name: replace(before.get(name, UNTOUCHED_LOGGER), level=logging.NOTSET) for name in closed
    }
    assert {state.level for state in closed.values()} == {CLOSED_LOGGER_LEVEL}
    assert set(TRANSPORT_ROOTS) <= closed.keys()
    assert logging.Logger.manager.disable == disable_before


@pytest.mark.parametrize(
    "name",
    [
        "agent_service.run_liveness",
        "agent_providers.catalog",
        "agent_providers.core_probe",
        "uvicorn.error",
    ],
)
def test_the_closing_leaves_the_services_and_cores_loggers_emitting(logging_state_restored, name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    close_transport_loggers()

    with recorded_on(logger) as handler:
        logger.error("still heard")

    assert [record.getMessage() for record in handler.received] == ["still heard"]


class Reached:
    def __init__(self) -> None:
        self.levels: dict[str, dict[str, int]] = {}

    def noting(self, point: str, result: object = None) -> Callable[..., object]:
        def note(*_arguments: object, **_keywords: object) -> object:
            self.levels[point] = {
                name: logging.getLogger(name).getEffectiveLevel() for name in CLOSED_NAMES
            }
            return result

        return note


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch, logging_state_restored: None) -> Reached:
    reached = Reached()
    monkeypatch.setattr(app_module, "create_app", reached.noting("create_app"))
    monkeypatch.setattr(uvicorn, "run", reached.noting("uvicorn.run"))
    return reached


@pytest.mark.parametrize("point", ["create_app", "uvicorn.run"])
def test_the_process_entry_closes_the_transport_loggers_before_it_serves(tmp_path, reached, point):
    logging.getLogger("httpcore.http11")
    program = _printing(tmp_path, PRINTS_THE_PIN)

    assert main(["--config", str(_config_file_naming(tmp_path, program))]) == 0

    assert reached.levels[point] == dict.fromkeys(CLOSED_NAMES, CLOSED_LOGGER_LEVEL)


def refused_configuration(tmp_path: Path) -> list[str]:
    return ["--config", str(tmp_path / "missing.toml")]


def refused_version(tmp_path: Path) -> list[str]:
    program = _printing(tmp_path, "0.0.1 (Claude Code)\n")
    return ["--config", str(_config_file_naming(tmp_path, program))]


@pytest.mark.parametrize("refusal", [refused_configuration, refused_version])
def test_a_refused_start_leaves_the_transport_loggers_untouched(tmp_path, reached, refusal):
    arguments = refusal(tmp_path)

    assert main(arguments) == EXIT_BAD_CONFIG

    assert reached.levels == {}
    assert [logging.getLogger(name).level for name in TRANSPORT_ROOTS] == [logging.NOTSET] * len(
        TRANSPORT_ROOTS
    )


def fresh_interpreter(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            textwrap.dedent(script),
            os.pathsep.join(sys.path),
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=CHILD_DEADLINE_SECONDS,
        check=False,
    )


def test_importing_the_service_modules_opens_no_transport_logger_level():
    child = fresh_interpreter(
        """
        import json, logging, os, sys

        sys.path[:0] = sys.argv[1].split(os.pathsep)
        import agent_service.app_tools, agent_service.cli

        print(json.dumps({
            name: logger.level
            for name, logger in logging.Logger.manager.loggerDict.items()
            if name.startswith(("httpx", "httpcore")) and isinstance(logger, logging.Logger)
        }))
        """
    )

    assert child.returncode == 0, child.stderr
    levels = json.loads(child.stdout)
    assert "httpx" in levels
    assert set(levels.values()) == {logging.NOTSET}


def test_building_the_app_twice_leaves_the_transport_loggers_untouched(logging_state_restored):
    create_app(plain_service_config())
    create_app(plain_service_config())

    assert [logging.getLogger(name).level for name in TRANSPORT_ROOTS] == [logging.NOTSET] * len(
        TRANSPORT_ROOTS
    )


CONFIGURED_AFTER_CLOSING = """
import json, logging, logging.config, os, sys

sys.path[:0] = sys.argv[1].split(os.pathsep)
import uvicorn
from agent_service.cli import close_transport_loggers

STEPS = {
    "uvicorn-config": lambda: uvicorn.Config(lambda scope, receive, send: None, log_level="debug"),
    "basic-config": lambda: logging.basicConfig(force=True, level=logging.DEBUG),
    "dict-config": lambda: logging.config.dictConfig(
        {"version": 1, "disable_existing_loggers": False, "loggers": {"httpx": {"level": "DEBUG"}}}
    ),
}
NAMES = ("httpx", "httpcore", "httpcore.http11")
for name in NAMES:
    logging.getLogger(name)
close_transport_loggers()
for step in sys.argv[2].split("+"):
    STEPS[step]()

created = []
build_record = logging.getLogRecordFactory()
logging.setLogRecordFactory(lambda *a, **k: created.append(a[0]) or build_record(*a, **k))
for name in NAMES:
    for level in (10, 20, 30, 40, 50):
        logging.getLogger(name).log(level, "call")
print(json.dumps({
    "levels": {name: logging.getLogger(name).level for name in NAMES},
    "disabled": {name: logging.getLogger(name).disabled for name in NAMES},
    "created": sorted(set(created)),
    "uvicorn_error_level": logging.getLogger("uvicorn.error").level,
}))
sys.exit(1 if created else 0)
"""


@pytest.mark.parametrize(
    ("steps", "uvicorn_error_level"),
    [
        ("uvicorn-config", logging.DEBUG),
        ("basic-config", logging.NOTSET),
        ("uvicorn-config+basic-config", logging.DEBUG),
    ],
)
def test_uvicorns_configuration_and_a_forced_basic_config_leave_the_loggers_closed(
    steps, uvicorn_error_level
):
    child = fresh_interpreter(CONFIGURED_AFTER_CLOSING, steps)

    assert child.returncode == 0, child.stderr
    report = json.loads(child.stdout)
    assert report["uvicorn_error_level"] == uvicorn_error_level
    assert set(report["levels"].values()) == {CLOSED_LOGGER_LEVEL}
    assert not any(report["disabled"].values())


def test_an_explicit_dict_config_naming_httpx_reopens_only_that_logger():
    child = fresh_interpreter(CONFIGURED_AFTER_CLOSING, "dict-config")

    assert child.returncode == 1, child.stderr
    report = json.loads(child.stdout)
    assert report["created"] == ["httpx"]


@pytest.fixture
def at_debug_with_observer(record_creation: list[str]) -> list[str]:
    logging.getLogger().setLevel(logging.DEBUG)
    return record_creation


def completed_run(origin: str, context: ssl.SSLContext) -> None:
    async def handshake_list_and_call() -> None:
        async with asyncio.timeout(CLIENT_DEADLINE_SECONDS):
            async with open_app_tools(origin, CAPABILITY, context) as session:
                await session.list_tools()
                await session.call_tool("echo", {"word": "hi"})

    asyncio.run(handshake_list_and_call())


@pytest.mark.parametrize("closing", [True, False], ids=["closed", "control-without-closing"])
def test_a_completed_run_creates_records_on_the_transport_hierarchy_only_without_the_closing(
    authority: TestAuthority, at_debug_with_observer, closing
):
    if closing:
        close_transport_loggers()

    with serving(ScriptedApp(), authority.credentials_for(CERTIFICATE_HOST)) as fake:
        completed_run(fake.origin, authority.client_context())

    names = on_the_transport_hierarchy(at_debug_with_observer)
    if closing:
        assert names == []
    else:
        assert "httpx" in names
        assert any(name.startswith("httpcore.") for name in names)


def refusing_the_handshake(_authority: TestAuthority) -> tuple[ScriptedApp, ssl.SSLContext]:
    app = ScriptedApp(overrides={"initialize": lambda _request: REFUSAL})
    return app, _authority.client_context()


def not_trusting_the_app(_authority: TestAuthority) -> tuple[ScriptedApp, ssl.SSLContext]:
    return ScriptedApp(), ssl.create_default_context()


@pytest.mark.parametrize(
    ("arrangement", "closed_error"),
    [(refusing_the_handshake, UpstreamRefused), (not_trusting_the_app, UpstreamUnreachable)],
    ids=["refused", "unreachable"],
)
def test_a_refused_or_failed_run_creates_no_record_on_the_transport_hierarchy(
    authority: TestAuthority, at_debug_with_observer, arrangement, closed_error
):
    close_transport_loggers()
    app, context = arrangement(authority)

    with serving(app, authority.credentials_for(CERTIFICATE_HOST)) as fake:
        with pytest.raises(closed_error):
            completed_run(fake.origin, context)

    assert on_the_transport_hierarchy(at_debug_with_observer) == []
