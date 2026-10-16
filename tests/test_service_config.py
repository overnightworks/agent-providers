"""The service starts only from a configuration it accepts, and names the field it refuses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from service_support import (
    LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT,
    everything_a_refusal_holds,
    generated_rsa_key,
    reaches_text,
    record_signed_tokens,
    tool_names_with_the_last_of_length,
    written_key_file,
)

from agent_client.tools import ToolEffect
from agent_service.cli import EXIT_BAD_CONFIG, main
from agent_service.config import ConfigError, Provider, load_config

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
    assert (config.runs.heartbeat_interval_s, config.runs.idle_timeout_s) == (15, 120.0)
    assert config.runs.max_duration_s == 600.0
    assert config.runs.stall_timeout_s == 30.0
    assert (config.runs.max_tool_calls, config.runs.max_bytes) == (30, 4194304)
    assert config.app_of_workload_client("songmaker-test") == "songmaker"


def test_a_file_without_a_signing_section_has_no_signing_key(tmp_path):
    assert load_config(_written(tmp_path, VALID_CONFIG)).signing is None


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


REFUSED_RUN_SETTINGS = (
    "heartbeat_interval_s = true",
    "heartbeat_interval_s = 15.5",
    "heartbeat_interval_s = 15.0",
    "heartbeat_interval_s = 0",
    "heartbeat_interval_s = 301",
    "idle_timeout_s = 10",
    "idle_timeout_s = 20",
    "stall_timeout_s = 15",
    "stall_timeout_s = 10",
    "stall_timeout_s = 121",
    "stall_timeout_s = 0",
    "stall_timeout_s = -1",
    "stall_timeout_s = true",
    "stall_timeout_s = inf",
    "stall_timeout_s = nan",
    "idle_timeout_s = 0",
    "idle_timeout_s = true",
    "max_duration_s = inf",
    "max_duration_s = nan",
    "max_duration_s = 0",
    "max_duration_s = -1",
    "max_duration_s = 100",
    "max_tool_calls = 0",
    "max_tool_calls = true",
    "max_tool_calls = 30.0",
    "max_bytes = 0",
    "max_bytes = -1",
    "max_bytes = 4194304.0",
    "max_tool_call = 30",
)


def _with_runs(setting: str) -> str:
    return VALID_CONFIG + f"\n[runs]\n{setting}\n"


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
        pytest.param(VALID_CONFIG + "\n[signing]\n", "signing.key_file", id="signing-without-key"),
        pytest.param(
            VALID_CONFIG + '\n[signing]\nkey_file = "/run/secrets/key.pem"\nkey = "x"\n',
            "signing.key: Extra inputs are not permitted",
            id="signing-with-a-key-member",
        ),
        pytest.param(
            VALID_CONFIG + '\n[signing]\nkey = "/run/secrets/key.pem"\n',
            "signing.key: Extra inputs are not permitted",
            id="signing-with-only-a-key-member",
        ),
        *[
            pytest.param(_with_runs(setting), "runs", id=f"runs-{setting.replace(' ', '')}")
            for setting in REFUSED_RUN_SETTINGS
        ],
    ],
)
def test_a_refused_file_stops_startup_naming_the_field(tmp_path, capsys, text, named_field):
    exit_status = main(["--config", str(_written(tmp_path, text))])

    assert exit_status == EXIT_BAD_CONFIG
    assert named_field in capsys.readouterr().err


def test_run_limits_given_as_integers_or_floats_load(tmp_path):
    text = _with_runs("heartbeat_interval_s = 20\nidle_timeout_s = 90\nmax_duration_s = 300.5")

    runs = load_config(_written(tmp_path, text)).runs

    assert (runs.heartbeat_interval_s, runs.idle_timeout_s, runs.max_duration_s) == (20, 90, 300.5)


@pytest.mark.parametrize("stall_timeout_s", [16, 120])
def test_a_stall_timeout_between_heartbeat_and_idle_loads(tmp_path, stall_timeout_s):
    text = _with_runs(f"stall_timeout_s = {stall_timeout_s}")

    runs = load_config(_written(tmp_path, text)).runs

    assert runs.stall_timeout_s == stall_timeout_s


def test_run_budgets_given_in_the_file_load(tmp_path):
    text = _with_runs("max_tool_calls = 5\nmax_bytes = 2097152")

    runs = load_config(_written(tmp_path, text)).runs

    assert (runs.max_tool_calls, runs.max_bytes) == (5, 2097152)


def _refused_on_one_line(config_file: Path, capsys) -> str:
    assert main(["--config", str(config_file)]) == EXIT_BAD_CONFIG
    refusal = capsys.readouterr().err
    assert refusal.count("\n") == 1
    return refusal.removeprefix(f"agent-service: refusing to start: {config_file}: ").rstrip("\n")


MEMBER_NAMED_WITH_A_NEWLINE = '"extra\\nfield" = 1'


@pytest.mark.parametrize(
    ("config_text", "refusal"),
    [
        pytest.param(
            _with("port = 8099", f"port = 8099\n{MEMBER_NAMED_WITH_A_NEWLINE}"),
            "server.extra\\nfield: Extra inputs are not permitted",
            id="server",
        ),
        pytest.param(
            _with("[server]", f"{MEMBER_NAMED_WITH_A_NEWLINE}\n\n[server]"),
            "identity.extra\\nfield: Extra inputs are not permitted",
            id="identity",
        ),
    ],
)
def test_an_unexpected_member_with_a_newline_in_its_name_is_refused_on_one_line(
    tmp_path, capsys, config_text, refusal
):
    assert _refused_on_one_line(_written(tmp_path, config_text), capsys) == refusal


def _config_path_with_a_newline(tmp_path: Path) -> Path:
    return tmp_path / "agent\nservice.toml"


def _file_written_as(contents: bytes):
    def write(path: Path) -> None:
        path.write_bytes(contents)

    return write


@pytest.mark.parametrize(
    ("write", "refusal"),
    [
        pytest.param(lambda path: None, "cannot be read: No such file or directory", id="missing"),
        pytest.param(
            _file_written_as(
                VALID_CONFIG.replace("songmaker-test", "songmaker-t\xe9st").encode("latin-1")
            ),
            "not UTF-8 TOML",
            id="not-utf8",
        ),
        pytest.param(_file_written_as(b"[identity\n"), "not TOML: ", id="not-toml"),
        pytest.param(
            _file_written_as(
                _with('issuer = "https://keycloak.test/realms/agent-test"\n', "").encode()
            ),
            "identity.issuer: Field required",
            id="invalid",
        ),
    ],
)
def test_a_config_path_with_a_newline_is_shown_escaped_on_one_line_in_every_refusal(
    tmp_path, capsys, write, refusal
):
    config_file = _config_path_with_a_newline(tmp_path)
    write(config_file)

    assert main(["--config", str(config_file)]) == EXIT_BAD_CONFIG
    shown = capsys.readouterr().err
    assert shown.count("\n") == 1
    assert shown.startswith(
        f"agent-service: refusing to start: {str(config_file).replace(chr(10), chr(92) + 'n')}:"
        f" {refusal}"
    )


SONGMAKER_ORIGIN_LINE = 'mcp_origin = "https://songmaker.internal"\n'
PRESENTATOR_ON_SONGMAKERS_ORIGIN = (
    '\n[apps.presentator]\nworkload_client = "presentator-test"\npurposes = []\n'
    + SONGMAKER_ORIGIN_LINE
)


def _signing_section(tmp_path: Path) -> str:
    key_file = written_key_file(tmp_path, generated_rsa_key())
    return f"\n[signing]\nkey_file = {json.dumps(str(key_file))}\n"


def _with_origin(tmp_path: Path, origin: str) -> Path:
    origin_line = f"mcp_origin = {json.dumps(origin)}\n"
    return _written(tmp_path, VALID_CONFIG + origin_line + _signing_section(tmp_path))


@pytest.mark.parametrize(
    "origin",
    [
        "https://songmaker.internal",
        "https://songmaker.internal:8443",
        "https://xn--bcher-kva.internal:8443",
        "https://123.internal",
        "https://api.0xfoo",
    ],
)
def test_an_app_with_a_canonical_https_origin_and_a_signing_key_loads(tmp_path, origin):
    config = load_config(_with_origin(tmp_path, origin))

    assert config.apps["songmaker"].mcp_origin == origin
    assert config.capability_scope("songmaker").origin == origin


def test_an_app_without_an_origin_and_without_signing_has_no_capability_scope(tmp_path):
    config = load_config(_written(tmp_path, VALID_CONFIG))

    assert config.apps["songmaker"].mcp_origin is None
    assert config.capability_scope("songmaker") is None


@pytest.mark.parametrize(
    ("origin", "refusal"),
    [
        pytest.param(
            "http://songmaker.internal",
            "must be an https origin: https://<host> or https://<host>:<port>",
            id="http-scheme",
        ),
        pytest.param(
            "HTTPS://Songmaker.internal:443",
            "must be written in its canonical form https://songmaker.internal",
            id="canonical-form-hint",
        ),
        pytest.param(
            "https://songmaker.internal/mcp",
            "must be https://<host> or https://<host>:<port> and nothing else:"
            " no user information, path, query, fragment, space, control or non-ASCII character",
            id="path",
        ),
        pytest.param(
            "https://0x7f000001", "must name a DNS host, not an IP address or a number", id="number"
        ),
    ],
)
def test_an_origin_the_format_refuses_stops_startup_naming_the_field(
    tmp_path, capsys, origin, refusal
):
    assert _refused_on_one_line(_with_origin(tmp_path, origin), capsys) == (
        f"apps.songmaker.mcp_origin: {refusal}"
    )


def test_two_apps_on_one_origin_stop_startup_naming_the_second(tmp_path, capsys):
    text = VALID_CONFIG + SONGMAKER_ORIGIN_LINE + _signing_section(tmp_path)
    config_file = _written(tmp_path, text + PRESENTATOR_ON_SONGMAKERS_ORIGIN)

    assert _refused_on_one_line(config_file, capsys) == (
        "apps.presentator.mcp_origin: https://songmaker.internal is already"
        " the MCP origin of app 'songmaker'"
    )


PROFILES_WITH_NEWLINES = (
    VALID_CONFIG.replace("[profiles.claude-main]", '[profiles."claude\\nmain"]')
    + '\n[profiles."claude\\nspare"]\nprovider = "claude"\n'
)
APPS_WITH_NEWLINES = VALID_CONFIG.replace("[apps.songmaker]", '[apps."song\\nmaker"]')
SECOND_APP_ON_THE_SAME_CLIENT = (
    '\n[apps."pres\\nentator"]\nworkload_client = "songmaker-test"\npurposes = []\n'
)


@pytest.mark.parametrize(
    ("text_of", "refusal"),
    [
        pytest.param(
            lambda tmp_path: PROFILES_WITH_NEWLINES,
            "profiles.claude\\nspare.provider: claude already has profile 'claude\\nmain'",
            id="second-profile-for-a-provider",
        ),
        pytest.param(
            lambda tmp_path: APPS_WITH_NEWLINES.replace(
                'purposes = ["songmaker.cowriter"]', 'purposes = ["no\\npurpose"]'
            ),
            "apps.song\\nmaker.purposes: unknown purpose 'no\\npurpose'",
            id="unknown-purpose",
        ),
        pytest.param(
            lambda tmp_path: APPS_WITH_NEWLINES + SECOND_APP_ON_THE_SAME_CLIENT,
            "apps.pres\\nentator.workload_client: 'songmaker-test' is already"
            " the workload client of app 'song\\nmaker'",
            id="workload-client-of-two-apps",
        ),
        pytest.param(
            lambda tmp_path: APPS_WITH_NEWLINES.replace(
                'workload_client = "songmaker-test"',
                'workload_client = "songmaker-test"\n' + SONGMAKER_ORIGIN_LINE,
            ),
            "apps.song\\nmaker.mcp_origin: needs a [signing] section,"
            " because the app's runs carry a signed capability",
            id="origin-without-signing",
        ),
        pytest.param(
            lambda tmp_path: (
                VALID_CONFIG.replace("[apps.songmaker]", '[apps."song\\nmaker"]')
                + SONGMAKER_ORIGIN_LINE
                + _signing_section(tmp_path)
                + '\n[apps."pres\\nentator"]\nworkload_client = "presentator-test"\n'
                "purposes = []\n" + SONGMAKER_ORIGIN_LINE
            ),
            "apps.pres\\nentator.mcp_origin: https://songmaker.internal is already"
            " the MCP origin of app 'song\\nmaker'",
            id="origin-of-two-apps",
        ),
        pytest.param(
            lambda tmp_path: _tuned_app_config(
                tmp_path, LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT + 1
            ).replace("[apps.songmaker]", '[apps."song\\nmaker"]'),
            "apps.song\\nmaker.tools: the capability for purpose 'songmaker.cowriter'"
            " would be 8196 characters; the limit is 8192",
            id="capability-too-large",
        ),
    ],
)
def test_a_name_with_a_newline_is_shown_escaped_once_on_one_line(
    tmp_path, capsys, text_of, refusal
):
    assert _refused_on_one_line(_written(tmp_path, text_of(tmp_path)), capsys) == refusal


def test_an_origin_without_a_signing_section_stops_startup_naming_the_field(tmp_path, capsys):
    config_file = _written(tmp_path, VALID_CONFIG + SONGMAKER_ORIGIN_LINE)

    assert _refused_on_one_line(config_file, capsys) == (
        "apps.songmaker.mcp_origin: needs a [signing] section,"
        " because the app's runs carry a signed capability"
    )


def _tools_line_of_the_tuned_app(last_tool_name_length: int) -> str:
    names = tool_names_with_the_last_of_length(last_tool_name_length)
    return "tools = { " + ", ".join(f'"{name}" = "read_only"' for name in names) + " }"


def _tuned_app_config(
    tmp_path: Path, last_tool_name_length: int = LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT
):
    tools_line = _tools_line_of_the_tuned_app(last_tool_name_length)
    text = _with('tools = { read_song = "read_only" }', tools_line)
    return text + SONGMAKER_ORIGIN_LINE + _signing_section(tmp_path)


def test_an_app_whose_capability_is_exactly_8192_characters_at_the_preflight_loads(tmp_path):
    load_config(_written(tmp_path, _tuned_app_config(tmp_path)))


@pytest.mark.parametrize(
    ("text_of", "purpose"),
    [
        pytest.param(
            lambda tmp_path: _tuned_app_config(tmp_path, LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT + 1),
            "songmaker.cowriter",
            id="one-more-character-in-a-tool-name",
        ),
        pytest.param(
            lambda tmp_path: (
                _tuned_app_config(tmp_path)
                .replace(
                    'purposes = ["songmaker.cowriter"]',
                    'purposes = ["songmaker.cowriter", "songmaker.cowriterx"]',
                )
                .replace(
                    "[apps.songmaker]",
                    '[purposes."songmaker.cowriterx"]\napi_alias = "cowriter"\n\n[apps.songmaker]',
                )
            ),
            "songmaker.cowriterx",
            id="a-second-purpose-one-character-longer",
        ),
        pytest.param(
            lambda tmp_path: _tuned_app_config(tmp_path).replace("agent-test", "agent-testx"),
            "songmaker.cowriter",
            id="an-issuer-one-character-longer",
        ),
    ],
)
def test_an_app_whose_capability_would_be_8193_characters_stops_startup_naming_its_tools(
    tmp_path, capsys, text_of, purpose
):
    config_file = _written(tmp_path, text_of(tmp_path))

    assert _refused_on_one_line(config_file, capsys) == (
        f"apps.songmaker.tools: the capability for purpose {purpose!r}"
        " would be 8193 characters; the limit is 8192"
    )


def test_an_oversized_capability_refusal_holds_no_token_anywhere(tmp_path, monkeypatch):
    signed = record_signed_tokens(monkeypatch)
    config_file = _written(
        tmp_path, _tuned_app_config(tmp_path, LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT + 1)
    )

    with pytest.raises(ConfigError) as refusal:
        load_config(config_file)

    assert signed
    assert not reaches_text(everything_a_refusal_holds(refusal.value), signed)
