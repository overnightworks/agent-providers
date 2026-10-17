"""The release page points a breaking release at its migration section."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
_REPOSITORY = "overnightworks/agent-providers"
_MIGRATION_LINK = (
    "**Breaking release.** Read [Migrating to 0.3.0]"
    f"(https://github.com/{_REPOSITORY}/blob/v0.3.0/README.md#migrating-to-030)"
    " before you bump your pin."
)


def _run_script(step_name: str) -> str:
    step = re.search(
        rf"- name: {re.escape(step_name)}\n(?:.*\n)*?        run: \|\n((?:          .*\n|\n)+)",
        _WORKFLOW.read_text(encoding="utf-8"),
    )
    assert step, f"release.yml has no run script for {step_name}"
    return "\n".join(line[10:] for line in step.group(1).splitlines())


def _notice_for(tag: str, readme: str, tmp_path: Path) -> str:
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    output = tmp_path / "github_output"
    output.touch()
    subprocess.run(
        ["/bin/bash", "-e", "-c", _run_script("Point a breaking release at its migration section")],
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_REF_NAME": tag,
            "GITHUB_REPOSITORY": _REPOSITORY,
            "GITHUB_OUTPUT": str(output),
        },
        check=True,
    )
    return output.read_text(encoding="utf-8").removeprefix("notice=").rstrip("\n")


@pytest.mark.parametrize(
    ("tag", "readme_heading", "notice"),
    [
        ("v0.3.0", "## Migrating to 0.3.0", _MIGRATION_LINK),
        ("v0.3.1", "## Migrating to 0.3.0", ""),
        ("v0.3.0", "## Install", ""),
        ("v0.3.0", "### Migrating to 0.3.0", ""),
    ],
    ids=["tag with its section", "tag of another version", "no section", "section one level down"],
)
def test_the_release_notice_exists_only_for_a_tag_with_its_migration_section(
    tmp_path, tag, readme_heading, notice
):
    assert _notice_for(tag, f"# Title\n\n{readme_heading}\n\ntext\n", tmp_path) == notice


@pytest.mark.parametrize(
    "step_name",
    [
        "Point a breaking release at its migration section",
        "Publish the release with the wheels the check admitted",
    ],
)
def test_a_release_script_never_interpolates_an_expression(step_name):
    assert "${{" not in _run_script(step_name)
