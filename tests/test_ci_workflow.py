"""The CI jobs are time-bounded so a hung run fails instead of occupying a runner."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPOSITORY = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPOSITORY / ".github" / "workflows" / "ci.yml"
_DECLARED_PYTHON_MINORS = ["3.12", "3.13", "3.14", "3.15"]


def _job_body(job: str) -> str:
    job_body = re.search(
        rf"^  {job}:\n(.*?)(?=^  \S|\Z)", _WORKFLOW.read_text(encoding="utf-8"), re.M | re.S
    )
    assert job_body, f"CI has no job {job}"
    return job_body.group(1)


def _timeout_minutes(job: str) -> int:
    timeout = re.search(r"^    timeout-minutes: (\d+)$", _job_body(job), re.M)
    assert timeout, f"CI job {job} has no timeout-minutes"
    return int(timeout.group(1))


@pytest.mark.parametrize(
    ("job", "minutes"),
    [("checks", 20), ("keycloak-token-shapes", 30), ("dependency-floors", 20)],
)
def test_a_ci_job_is_bounded_in_time(job, minutes):
    assert _timeout_minutes(job) == minutes


def test_checks_runs_on_every_declared_python_minor_under_its_own_name():
    checks = _job_body("checks")
    matrix = re.search(r'^        python-version: \[(.*)\]$', checks, re.M)
    assert matrix
    assert re.findall(r'"([\d.]+)"', matrix.group(1)) == _DECLARED_PYTHON_MINORS
    assert "    name: checks\n" in checks
    assert "fail-fast: false" in checks
    assert "python-version: ${{ matrix.python-version }}" in checks
    assert "matrix." not in re.sub(r"^\s+(python-version|if):.*$", "", checks, flags=re.M)


def test_only_the_oldest_leg_uploads_coverage():
    upload = re.search(r"Upload the coverage report.*?\n\n", _job_body("checks"), re.S)
    assert upload
    assert "if: matrix.python-version == '3.12'" in upload.group(0)


def test_dependency_floors_resolves_the_lowest_direct_dependencies():
    floors = _job_body("dependency-floors")
    assert "UV_RESOLUTION: lowest-direct" in floors
    assert 'python-version: "3.12"' in floors
    assert "--locked" not in floors


def test_the_quality_verdict_requires_the_dependency_floors():
    quality = _job_body("quality")
    needs = re.search(r"^    needs: \[(.*)\]$", quality, re.M)
    assert needs
    assert "dependency-floors" in needs.group(1)
    config = (_REPOSITORY / "quality" / "config.toml").read_text(encoding="utf-8")
    assert '"dependency-floors"' in config.split("[secrets]")[0]
