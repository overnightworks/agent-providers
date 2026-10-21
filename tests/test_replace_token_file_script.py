import importlib.util
import io
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replace_token_file.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("replace_token_file", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def token_file(tmp_path: Path, content: str) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    path = directory / "claude-token"
    path.write_text(content)
    path.chmod(0o600)
    return path


def test_a_sound_token_replaces_the_old_file_at_mode_0600(script, tmp_path):
    path = token_file(tmp_path, "old-token\n")

    status = script.main([str(path)], io.StringIO("new-token\n"))

    assert status == 0
    assert path.read_text() == "new-token\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [entry.name for entry in path.parent.iterdir()] == ["claude-token"]


def test_a_json_credentials_file_is_refused_and_the_old_token_stays(script, tmp_path, capsys):
    path = token_file(tmp_path, "old-token\n")

    status = script.main([str(path)], io.StringIO('{"claudeAiOauth": {}}'))

    assert status == 2
    assert path.read_text() == "old-token\n"
    assert [entry.name for entry in path.parent.iterdir()] == ["claude-token"]
    assert "claudeAiOauth" not in capsys.readouterr().err


def test_an_api_key_is_refused_and_the_old_token_stays(script, tmp_path):
    path = token_file(tmp_path, "old-token\n")

    status = script.main([str(path)], io.StringIO("sk-ant-api03-abc\n"))

    assert status == 2
    assert path.read_text() == "old-token\n"
