"""The CI jobs are time-bounded so a hung run fails instead of occupying a runner."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _timeout_minutes(job: str) -> int:
    job_body = re.search(
        rf"^  {job}:\n(.*?)(?=^  \S|\Z)", _WORKFLOW.read_text(encoding="utf-8"), re.M | re.S
    )
    assert job_body, f"CI has no job {job}"
    timeout = re.search(r"^    timeout-minutes: (\d+)$", job_body.group(1), re.M)
    assert timeout, f"CI job {job} has no timeout-minutes"
    return int(timeout.group(1))


@pytest.mark.parametrize(("job", "minutes"), [("checks", 20), ("keycloak-token-shapes", 30)])
def test_a_ci_job_is_bounded_in_time(job, minutes):
    assert _timeout_minutes(job) == minutes
