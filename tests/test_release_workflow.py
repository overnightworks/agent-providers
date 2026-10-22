"""The release page points a breaking release at its migration section and lists the wheels."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
_REPOSITORY = "overnightworks/agent-providers"
_MIGRATION_LINK = (
    "**Breaking release.** Read [Migrating to 0.3.0]"
    f"(https://github.com/{_REPOSITORY}/blob/v0.3.0/docs/MIGRATING.md#migrating-to-030)"
    " before you bump your pin."
)


def _run_script(step_name: str) -> str:
    step = re.search(
        rf"- name: {re.escape(step_name)}\n(?:.*\n)*?        run: \|\n((?:          .*\n|\n)+)",
        _WORKFLOW.read_text(encoding="utf-8"),
    )
    assert step, f"release.yml has no run script for {step_name}"
    return "\n".join(line[10:] for line in step.group(1).splitlines())


def _notice_for(tag: str, migrating: str, tmp_path: Path) -> str:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "MIGRATING.md").write_text(migrating, encoding="utf-8")
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
    ("tag", "heading", "notice"),
    [
        ("v0.3.0", "## Migrating to 0.3.0", _MIGRATION_LINK),
        ("v0.3.1", "## Migrating to 0.3.0", ""),
        ("v0.3.0", "## Install", ""),
        ("v0.3.0", "### Migrating to 0.3.0", ""),
    ],
    ids=["tag with its section", "tag of another version", "no section", "section one level down"],
)
def test_the_release_notice_exists_only_for_a_tag_with_its_migration_section(
    tmp_path, tag, heading, notice
):
    assert _notice_for(tag, f"# Title\n\n{heading}\n\ntext\n", tmp_path) == notice


def test_the_release_page_lists_each_wheel_with_its_url_and_sha256(tmp_path):
    wheels = {
        "overnightworks_agent_client-0.3.0-py3-none-any.whl": b"client",
        "overnightworks_agent_providers-0.3.0-py3-none-any.whl": b"core",
        "overnightworks_agent_service-0.3.0-py3-none-any.whl": b"service",
    }
    (tmp_path / "dist").mkdir()
    for name, content in wheels.items():
        (tmp_path / "dist" / name).write_bytes(content)
    output = tmp_path / "github_output"
    output.touch()
    subprocess.run(
        ["/bin/bash", "-e", "-c", _run_script("List the wheels with their hashes")],
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_REF_NAME": "v0.3.0",
            "GITHUB_REPOSITORY": _REPOSITORY,
            "GITHUB_OUTPUT": str(output),
        },
        check=True,
    )
    install = output.read_text(encoding="utf-8")
    for name, content in wheels.items():
        distribution = name.split("-")[0].replace("_", "-")
        url = f"https://github.com/{_REPOSITORY}/releases/download/v0.3.0/{name}"
        digest = hashlib.sha256(content).hexdigest()
        assert f"{distribution} @ {url}#sha256={digest}\n" in install


@pytest.mark.parametrize(
    "step_name",
    [
        "Point a breaking release at its migration section",
        "List the wheels with their hashes",
        "Publish the release with the wheels the check admitted",
    ],
)
def test_a_release_script_never_interpolates_an_expression(step_name):
    assert "${{" not in _run_script(step_name)
