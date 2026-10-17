"""Each S6 row runs in its own disposable interpreter; a failed cleanup never reaches the suite."""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import coverage
import pytest
from anchored_support import (
    IMPORT_ROOT_PRELUDE,
    TEST_WAIT_SECONDS,
    ChildCleanup,
    absolute_import_roots,
)

from agent_providers.spawn import ChildProcess, closed_environment

CASES_MODULE = Path(__file__).with_name("anchored_start_cases.py")
ROW_INTERPRETER_SECONDS = 8 * TEST_WAIT_SECONDS
"""A failing row chains seven bounded waits (the seam's, the outcome's join, the exit's worker
join, the watcher's readiness and its join, the cleanup's teardown, the session anchor's
finalize) and the interpreter's own start adds one."""
RUN_ONE_ROW = (
    IMPORT_ROOT_PRELUDE
    + """
import json
import pytest
measurement = json.loads(sys.argv[4])
if measurement is not None:
    import coverage
    session = coverage.Coverage(config_file=False, data_suffix=True, **measurement)
    session.start()
exit_code = pytest.main([sys.argv[2], "-p", "no:cacheprovider", "-q", f"--junitxml={sys.argv[3]}"])
if measurement is not None:
    session.stop()
    session.save()
sys.exit(exit_code)
"""
)
ROWS = (
    "test_a_refused_watcher_launch_leaves_no_child",
    "test_a_watcher_launched_before_the_start_raised_never_touches_the_pid",
    "test_a_loss_during_the_rollback_raises_identity_lost_from_the_launch_error[kill-meets-esrch]",
    "test_a_loss_during_the_rollback_raises_identity_lost_from_the_launch_error[reap-meets-echild]",
    "test_a_stop_report_at_the_rollback_reap_is_followed_by_the_exit_report",
)
UNPASSED_OUTCOMES = {"failure", "error", "skipped"}


def outer_measurement() -> str:
    """The outer run's coverage settings as JSON, or `null` when it is not measured: the closed
    environment hides every coverage variable, and the parallel data file next to the outer one
    is what pytest-cov combines into the report."""
    outer = coverage.Coverage.current()
    if outer is None:
        return json.dumps(None)
    return json.dumps(
        {
            "data_file": str(Path(outer.config.data_file).resolve()),
            "source": [str(Path(source).resolve()) for source in outer.config.source or ()],
            "branch": outer.config.branch,
        }
    )


def reported_testcases(report: Path) -> list[tuple[str, bool]]:
    return [
        (case.get("name"), UNPASSED_OUTCOMES.isdisjoint(child.tag for child in case))
        for case in ElementTree.parse(report).iter("testcase")
    ]


@pytest.mark.parametrize("row", ROWS)
def test_a_start_row_passes_in_its_own_interpreter(
    row: str, child_cleanup: ChildCleanup, tmp_path: Path
) -> None:
    output = tmp_path / "interpreter-output.txt"
    report = tmp_path / "report.xml"
    with output.open("wb") as sink:
        interpreter = child_cleanup.start_bare_child(
            ChildProcess(
                binary=Path(sys.executable),
                arguments=(
                    "-I",
                    "-c",
                    RUN_ONE_ROW,
                    absolute_import_roots(),
                    f"{CASES_MODULE}::{row}",
                    str(report),
                    outer_measurement(),
                ),
                environment=closed_environment(tmp_path),
                working_directory=tmp_path,
            ),
            stdin=subprocess.DEVNULL,
            stdout=sink.fileno(),
            stderr=sink.fileno(),
        )
    try:
        exit_code = child_cleanup.wait_exited(interpreter, bound=ROW_INTERPRETER_SECONDS)
    finally:
        print(output.read_text(errors="replace"))

    assert exit_code == 0
    assert reported_testcases(report) == [(row.split("::")[-1], True)]
