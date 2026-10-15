"""The service starts only from a configuration it accepts, and names the field it refuses."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_client.tools import ToolEffect
from agent_service.cli import EXIT_BAD_CONFIG, main
from agent_service.config import Provider, load_config

VALID_CONFIG = """
[identity]
issuer = "https://keycloak.test/realms/agent-test"
jwks_url = "https://keycloak.test/realms/agent-test/protocol/openid-connect/certs"

[server]
host = "127.0.0.1"
port = 8099

[profiles.claude-main]
provider = "claude"

[purposes."songmaker.cowriter"]
cli_provider = "claude"
api_alias = "cowriter"

[apps.songmaker]
workload_client = "songmaker-test"
purposes = ["songmaker.cowriter"]
tools = { read_song = "read_only" }
"""


def _written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "agent-service.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_valid_file_loads_with_the_documented_defaults(tmp_path):
    config = load_config(_written(tmp_path, VALID_CONFIG))

    assert config.identity.audience == "agent-service"
    assert config.identity.subscription_group == "/ki-abo"
    assert config.identity.group_claim == "groups"
    assert config.profiles["claude-main"].provider is Provider.CLAUDE
    assert config.app_of_workload_client("songmaker-test") == "songmaker"


@pytest.mark.parametrize(
    ("tool", "effect"),
    [("read_song", ToolEffect.READ_ONLY), ("unlisted_tool", ToolEffect.SIDE_EFFECTING)],
)
def test_an_unlisted_tool_counts_as_side_effecting(tmp_path, tool, effect):
    config = load_config(_written(tmp_path, VALID_CONFIG))

    assert config.apps["songmaker"].tool_effect(tool) is effect


SECOND_CLAUDE_PROFILE = '\n[profiles.claude-spare]\nprovider = "claude"\n'
SECOND_APP_WITH_SAME_CLIENT = (
    '\n[apps.presentator]\nworkload_client = "songmaker-test"\npurposes = []\n'
)


def _with(old: str, new: str) -> str:
    assert old in VALID_CONFIG
    return VALID_CONFIG.replace(old, new)


def _with_key_set_url(url: str) -> str:
    return _with(
        'jwks_url = "https://keycloak.test/realms/agent-test/protocol/openid-connect/certs"',
        f'jwks_url = "{url}"',
    )


@pytest.mark.parametrize(
    ("text", "named_field"),
    [
        pytest.param(
            _with("port = 8099", "port = 8099\nworkers = 4"), "server.workers", id="unknown-field"
        ),
        pytest.param(
            _with('provider = "claude"', 'provider = "gemini"'),
            "profiles.claude-main.provider",
            id="unknown-provider",
        ),
        pytest.param(
            VALID_CONFIG + SECOND_CLAUDE_PROFILE,
            "profiles.claude-spare.provider",
            id="second-profile-for-a-provider",
        ),
        pytest.param(
            _with('purposes = ["songmaker.cowriter"]', 'purposes = ["songmaker.judge"]'),
            "apps.songmaker.purposes",
            id="app-purpose-not-configured",
        ),
        pytest.param(
            VALID_CONFIG + SECOND_APP_WITH_SAME_CLIENT,
            "apps.presentator.workload_client",
            id="workload-client-of-two-apps",
        ),
        pytest.param(
            _with('jwks_url = "https://', 'jwks_url = "http://'),
            "identity.jwks_url",
            id="plain-http-key-set",
        ),
        pytest.param(
            _with_key_set_url("https://"), "identity.jwks_url", id="key-set-url-without-a-host"
        ),
        pytest.param(
            _with_key_set_url("https://reader:secret@keycloak.test/certs"),
            "identity.jwks_url",
            id="key-set-url-with-credentials",
        ),
        pytest.param(
            _with_key_set_url("https://keycloak.test:99999/certs"),
            "identity.jwks_url",
            id="key-set-url-with-an-invalid-port",
        ),
        pytest.param(
            _with('read_song = "read_only"', 'read_song = "harmless"'),
            "apps.songmaker.tools.read_song",
            id="unknown-tool-effect",
        ),
        pytest.param(
            _with('issuer = "https://keycloak.test/realms/agent-test"\n', ""),
            "identity.issuer",
            id="missing-issuer",
        ),
        pytest.param("[identity\n", "not TOML", id="not-toml"),
    ],
)
def test_a_refused_file_stops_startup_naming_the_field(tmp_path, capsys, text, named_field):
    exit_status = main(["--config", str(_written(tmp_path, text))])

    assert exit_status == EXIT_BAD_CONFIG
    assert named_field in capsys.readouterr().err


def test_a_missing_file_stops_startup(tmp_path, capsys):
    exit_status = main(["--config", str(tmp_path / "absent.toml")])

    assert exit_status == EXIT_BAD_CONFIG
    assert "cannot be read" in capsys.readouterr().err


def test_a_file_that_is_not_utf8_stops_startup(tmp_path, capsys):
    path = tmp_path / "agent-service.toml"
    path.write_bytes(VALID_CONFIG.replace("songmaker-test", "songmaker-t\xe9st").encode("latin-1"))

    exit_status = main(["--config", str(path)])

    assert exit_status == EXIT_BAD_CONFIG
    assert "not UTF-8" in capsys.readouterr().err
