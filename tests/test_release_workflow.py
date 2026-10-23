"""The release page points a breaking release at its migration section and lists the wheels."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
_REPOSITORY = "overnightworks/agent-runtime"
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


def _release_steps() -> list[dict[str, object]]:
    jobs = YAML(typ="safe").load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    return [step for job in jobs.values() for step in job["steps"]]


def _publish_environment() -> dict[str, str]:
    for step in _release_steps():
        if step.get("name") == "Publish the release with the wheels the check admitted":
            return step["env"]
    raise AssertionError("release.yml has no publish step")


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


def _wheel_list_output(tmp_path: Path) -> tuple[dict[str, bytes], str]:
    wheels = {
        "overnightworks_agent_runtime_client-0.3.0-py3-none-any.whl": b"client",
        "overnightworks_agent_runtime-0.3.0-py3-none-any.whl": b"core",
        "overnightworks_agent_runtime_service-0.3.0-py3-none-any.whl": b"service",
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
    return wheels, output.read_text(encoding="utf-8")


def test_the_release_page_lists_each_wheel_with_its_url_and_sha256(tmp_path):
    wheels, install = _wheel_list_output(tmp_path)
    for name, content in wheels.items():
        distribution = name.split("-")[0].replace("_", "-")
        url = f"https://github.com/{_REPOSITORY}/releases/download/v0.3.0/{name}"
        digest = hashlib.sha256(content).hexdigest()
        assert f"{distribution} @ {url}#sha256={digest}\n" in install


_INSTALL_LINES = [
    f"overnightworks-agent-runtime{suffix} @ "
    f"https://example.test/overnightworks-agent-runtime{suffix}.whl#sha256={digest}"
    for suffix, digest in (("-client", "aa"), ("", "bb"), ("-service", "cc"))
]


def _publish(notice: str, tmp_path: Path) -> list[str]:
    """Run the publish step with a `gh` that records its arguments, NUL-terminated."""
    (tmp_path / "dist").mkdir()
    wheels = ["a.whl", "b.whl", "c.whl"]
    for wheel in wheels:
        (tmp_path / "dist" / wheel).touch()
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    recorded = tmp_path / "recorded"
    fake_gh = bin_directory / "gh"
    fake_gh.write_text(f'#!/bin/sh\nprintf \'%s\\0\' "$@" > "{recorded}"\n', encoding="utf-8")
    fake_gh.chmod(0o755)
    subprocess.run(
        [
            "/bin/bash",
            "-e",
            "-c",
            _run_script("Publish the release with the wheels the check admitted"),
        ],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_directory}:/usr/bin:/bin",
            "GITHUB_REF_NAME": "v0.3.0",
            "NOTICE": notice,
            "INSTALL": "\n".join(_INSTALL_LINES),
        },
        check=True,
    )
    return recorded.read_text(encoding="utf-8").split("\0")[:-1]


def _notes_of(arguments: list[str]) -> str:
    """The notes are the one argument after `--notes`, and the last one."""
    assert arguments.index("--notes") == len(arguments) - 2
    return arguments[-1]


def test_the_published_notes_open_with_the_migration_link_then_the_three_requirements(tmp_path):
    notes = _notes_of(_publish(_MIGRATION_LINK, tmp_path))

    assert notes == "\n".join([_MIGRATION_LINK, "", *_INSTALL_LINES])


def test_the_published_notes_without_a_migration_section_are_the_three_requirements_only(tmp_path):
    notes = _notes_of(_publish("", tmp_path))

    assert notes == "\n".join(_INSTALL_LINES)


def test_the_publish_step_takes_the_notice_and_the_install_lines_from_the_steps_that_wrote_them():
    environment = _publish_environment()

    assert environment["NOTICE"] == "${{ steps.notice.outputs.notice }}"
    assert environment["INSTALL"] == "${{ steps.install.outputs.install }}"


def test_the_release_job_has_the_steps_the_publish_step_reads_from():
    step_ids = {step.get("id") for step in _release_steps()}

    assert {"notice", "install"} <= step_ids


def test_the_wheel_list_is_written_as_a_multiline_output_named_install(tmp_path):
    _, install = _wheel_list_output(tmp_path)
    lines = install.splitlines()

    opening, delimiter = lines[0].split("<<")
    assert opening == "install"
    assert lines[-1] == delimiter


def test_the_release_is_created_with_generated_notes_and_the_three_wheels(tmp_path):
    arguments = _publish("", tmp_path)

    assert arguments[:3] == ["release", "create", "v0.3.0"]
    assert arguments[3:6] == ["dist/a.whl", "dist/b.whl", "dist/c.whl"]
    assert "--generate-notes" in arguments


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
