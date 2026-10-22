"""The service starts only from a configuration it accepts, and names the field it refuses."""

from __future__ import annotations

import errno
import json
import math
import os
import stat
import tomllib
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from mcp_app_fake import TestAuthority
from provider_test_support import fake_cli_executable
from pydantic import ValidationError
from service_support import (
    CALLED_PLAINLY_OR_FROM_AN_ACTIVE_HANDLER,
    PINNED_CLAUDE_VERSION,
    PROCESS_TOKEN,
    called_from,
    everything_a_refusal_holds,
    logging_state_snapshot,  # noqa: F401 - a fixture, requested by name
    process_claude_cli,
    process_signing_key_file,
    process_token_file,
    process_working_root,
    reaches_private_key,
    reaches_text,
)

from agent_runtime.secret_file import UnsoundSecretFile, read_token_file
from agent_runtime_service import app as app_module
from agent_runtime_service.cli import EXIT_BAD_CONFIG, main
from agent_runtime_service.config import (
    AppConfig,
    ConfigError,
    ProfileConfig,
    Provider,
    ServiceConfig,
    load_config,
)

SONGMAKER_ORIGIN_LINE = 'mcp_origin = "https://songmaker.internal"\n'
SIGNING_KEY_LINE = f"key_file = {json.dumps(str(process_signing_key_file()))}"
SIGNING_SECTION = f"[signing]\n{SIGNING_KEY_LINE}\n\n"
MCP_CA_FILE = TestAuthority(process_signing_key_file().parent).ca_file
MCP_SECTION = f"[mcp]\nca_file = {json.dumps(str(MCP_CA_FILE))}\n\n"
TOKEN_FILE_LINE = f"token_file = {json.dumps(str(process_token_file()))}"
CLAUDE_BINARY_LINE = f"binary = {json.dumps(str(process_claude_cli()))}"
CLAUDE_VERSION_LINE = f'version = "{PINNED_CLAUDE_VERSION}"'
ROOT_MARKER = "synthetic-private-root-marker"
WORKING_ROOT_LINE = f"working_root = {json.dumps(str(process_working_root().path))}"


MODELS_LINE = 'models = ["sonnet", "opus"]'


def cli_section(*, binary: str, version: str, root: str, models: str = MODELS_LINE) -> str:
    root_lines = f"{root}\n" if root else ""
    program_lines = "".join(f"{line}\n" for line in (binary, version, models) if line)
    program_table = f"[cli.claude]\n{program_lines}\n" if program_lines else ""
    return f"[cli]\n{root_lines}\n{program_table}"


CLI_SECTION = cli_section(
    binary=CLAUDE_BINARY_LINE, version=CLAUDE_VERSION_LINE, root=WORKING_ROOT_LINE
)
TOOLS_LINE = 'tools = { read_song = "read_only" }'

SUBSCRIPTION_GROUPS_LINE = (
    'subscription_groups = ["/old-subscription-members", "/subscription-members"]'
)

VALID_CONFIG = f"""
[identity]
issuer = "https://keycloak.test/realms/agent-test"
jwks_url = "https://keycloak.test/realms/agent-test/protocol/openid-connect/certs"
audience = "agent-test-audience"
{SUBSCRIPTION_GROUPS_LINE}
api_groups = ["/api-members"]
group_claim = "member-groups"

[server]
host = "127.0.0.1"
port = 8099

{CLI_SECTION}{SIGNING_SECTION}{MCP_SECTION}[profiles.claude-main]
provider = "claude"
{TOKEN_FILE_LINE}

[route]
api_alias = "assistant-api"

[apps.songmaker]
workload_client = "songmaker-test"
{SONGMAKER_ORIGIN_LINE}"""


@pytest.fixture(autouse=True)
def _never_really_serve(monkeypatch):
    def fail_instead_of_binding_a_port(*_arguments, **_where):
        raise AssertionError("a refused configuration reached serving")

    monkeypatch.setattr(uvicorn, "run", fail_instead_of_binding_a_port)


def _written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "agent-service.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loading_a_file_does_not_run_the_program_its_cli_section_names(tmp_path):
    probe_log = tmp_path / "probe-starts"
    program = fake_cli_executable(tmp_path, {("--version",): "any"}, calls_log=probe_log)
    binary_line = f"binary = {json.dumps(str(program))}"
    text = VALID_CONFIG.replace(
        CLI_SECTION,
        cli_section(binary=binary_line, version=CLAUDE_VERSION_LINE, root=WORKING_ROOT_LINE),
    )

    load_config(_written(tmp_path, text))

    assert not probe_log.exists()


def test_a_valid_file_loads_with_its_configured_identity_and_the_documented_defaults(tmp_path):
    config = load_config(_written(tmp_path, VALID_CONFIG))

    assert config.identity.audience == "agent-test-audience"
    assert config.identity.subscription_groups == {
        "/old-subscription-members",
        "/subscription-members",
    }
    assert config.identity.api_groups == {"/api-members"}
    assert config.route.api_alias == "assistant-api"
    assert config.identity.group_claim == "member-groups"
    assert config.profiles["claude-main"].provider is Provider.CLAUDE
    assert (config.runs.heartbeat_interval_s, config.runs.idle_timeout_s) == (15, 120.0)
    assert config.runs.max_duration_s == 600.0
    assert config.runs.stall_timeout_s == 30.0
    assert (config.runs.max_tool_calls, config.runs.max_bytes) == (30, 4194304)
    assert config.runs.max_parallel_cli_runs == 5
    assert config.app_of_workload_client("songmaker-test") == "songmaker"


def test_an_installation_without_an_origin_needs_no_signing_section(tmp_path):
    assert load_config(_written(tmp_path, _without_origin_and_signing())).signing is None


def test_a_leftover_tools_table_of_an_app_stops_startup_naming_the_field(tmp_path, capsys):
    text = _with(SONGMAKER_ORIGIN_LINE, f"{TOOLS_LINE}\n{SONGMAKER_ORIGIN_LINE}")

    assert _refused_on_one_line(_written(tmp_path, text), capsys).startswith("apps.songmaker.tools")


SECOND_CLAUDE_PROFILE = f'\n[profiles.claude-spare]\nprovider = "claude"\n{TOKEN_FILE_LINE}\n'
SECOND_APP_WITH_SAME_CLIENT = '\n[apps.presentator]\nworkload_client = "songmaker-test"\n'


def _with(old: str, new: str) -> str:
    assert old in VALID_CONFIG
    return VALID_CONFIG.replace(old, new)


def _without_origin_and_signing() -> str:
    return (
        _with(SIGNING_SECTION, "").replace(SONGMAKER_ORIGIN_LINE, "")
    )


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
    "max_duration_s = 2147484",
    "max_duration_s = 2147483.5",
    "max_duration_s = 2147483.0005",
    "max_tool_calls = 0",
    "max_tool_calls = true",
    "max_tool_calls = 30.0",
    "max_bytes = 0",
    "max_bytes = -1",
    "max_bytes = 4194304.0",
    "max_parallel_cli_runs = 0",
    "max_parallel_cli_runs = -1",
    "max_parallel_cli_runs = true",
    "max_parallel_cli_runs = 5.0",
    "max_tool_call = 30",
)


def _with_runs(setting: str) -> str:
    return VALID_CONFIG + f"\n[runs]\n{setting}\n"


def _field_named_for(setting: str) -> str:
    field = setting.split(" = ")[0]
    if field == "max_parallel_cli_runs" or setting.startswith("max_duration_s = 21474"):
        return f"runs.{field}"
    return "runs"


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
            _with("[route]", '[purposes.chat]\napi_alias = "assistant-api"\n\n[route]'),
            "purposes",
            id="C1-leftover-purposes-table",
        ),
        pytest.param(
            _with(
                'workload_client = "songmaker-test"\n',
                'workload_client = "songmaker-test"\npurposes = ["chat"]\n',
            ),
            "apps.songmaker.purposes",
            id="C2-leftover-app-purposes",
        ),
        pytest.param(
            _with(SUBSCRIPTION_GROUPS_LINE, 'subscription_group = "/ki-abo"'),
            "identity.subscription_group:",
            id="C3-the-singular-subscription-group",
        ),
        pytest.param(
            _with(SUBSCRIPTION_GROUPS_LINE, "subscription_groups = []"),
            "identity.subscription_groups",
            id="C4-no-subscription-group",
        ),
        pytest.param(
            _with(SUBSCRIPTION_GROUPS_LINE, 'subscription_groups = ["ki-abo"]'),
            "identity.subscription_groups",
            id="C4-a-subscription-group-without-a-slash",
        ),
        pytest.param(
            _with('api_groups = ["/api-members"]', 'api_groups = ["api-members"]'),
            "identity.api_groups",
            id="an-api-group-without-a-slash",
        ),
        pytest.param(
            _with('[route]\napi_alias = "assistant-api"\n', ""),
            "route.api_alias: required because identity.api_groups is set",
            id="C5-api-groups-without-an-alias",
        ),
        pytest.param(
            _with('api_groups = ["/api-members"]\n', ""),
            "identity.api_groups: required because route.api_alias is set",
            id="C5-an-alias-without-api-groups",
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
            _with('issuer = "https://keycloak.test/realms/agent-test"\n', ""),
            "identity.issuer",
            id="missing-issuer",
        ),
        *(
            pytest.param(
                _with(line, ""), f"identity.{field}", id=f"missing-{field.replace('_', '-')}"
            )
            for field, line in (
                ("audience", 'audience = "agent-test-audience"\n'),
                ("subscription_groups", SUBSCRIPTION_GROUPS_LINE + "\n"),
                ("group_claim", 'group_claim = "member-groups"\n'),
            )
        ),
        pytest.param("[identity\n", "not TOML", id="not-toml"),
        pytest.param(_with(SIGNING_KEY_LINE, ""), "signing.key_file", id="signing-without-key"),
        pytest.param(
            _with(SIGNING_KEY_LINE, f'{SIGNING_KEY_LINE}\nkey = "x"'),
            "signing.key: Extra inputs are not permitted",
            id="signing-with-a-key-member",
        ),
        pytest.param(
            _with(SIGNING_KEY_LINE, 'key = "/run/secrets/key.pem"'),
            "signing.key: Extra inputs are not permitted",
            id="signing-with-only-a-key-member",
        ),
        *[
            pytest.param(
                _with_runs(setting),
                _field_named_for(setting),
                id=f"runs-{setting.replace(' ', '')}",
            )
            for setting in REFUSED_RUN_SETTINGS
        ],
    ],
)
def test_a_refused_file_stops_startup_naming_the_field(tmp_path, capsys, text, named_field):
    exit_status = main(["--config", str(_written(tmp_path, text))])

    assert exit_status == EXIT_BAD_CONFIG
    assert named_field in capsys.readouterr().err


@pytest.mark.parametrize(
    "max_duration_s",
    [300.5, 2147483, math.nextafter(2147483.0, -math.inf)],
    ids=["a-fraction", "the-bound", "just-below-the-bound"],
)
def test_run_limits_given_as_integers_or_floats_load(tmp_path, max_duration_s):
    text = _with_runs(
        f"heartbeat_interval_s = 20\nidle_timeout_s = 90\nmax_duration_s = {max_duration_s}"
    )

    runs = load_config(_written(tmp_path, text)).runs

    assert (runs.heartbeat_interval_s, runs.idle_timeout_s, runs.max_duration_s) == (
        20,
        90,
        max_duration_s,
    )


@pytest.mark.parametrize("stall_timeout_s", [16, 120])
def test_a_stall_timeout_between_heartbeat_and_idle_loads(tmp_path, stall_timeout_s):
    text = _with_runs(f"stall_timeout_s = {stall_timeout_s}")

    runs = load_config(_written(tmp_path, text)).runs

    assert runs.stall_timeout_s == stall_timeout_s


def test_run_budgets_given_in_the_file_load(tmp_path):
    text = _with_runs("max_tool_calls = 5\nmax_bytes = 2097152")

    runs = load_config(_written(tmp_path, text)).runs

    assert (runs.max_tool_calls, runs.max_bytes) == (5, 2097152)


def test_an_explicit_parallel_run_limit_loads_and_gives_that_many_places(tmp_path):
    runs = load_config(_written(tmp_path, _with_runs("max_parallel_cli_runs = 2"))).runs

    assert runs.max_parallel_cli_runs == 2


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


PRESENTATOR_ON_SONGMAKERS_ORIGIN = (
    '\n[apps.presentator]\nworkload_client = "presentator-test"\n' + SONGMAKER_ORIGIN_LINE
)


def _with_origin(tmp_path: Path, origin: str) -> Path:
    return _written(tmp_path, _with(SONGMAKER_ORIGIN_LINE, f"mcp_origin = {json.dumps(origin)}\n"))


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
    assert config.capability_scope("songmaker", []).origin == origin


def test_an_app_without_an_origin_and_without_signing_has_no_capability_scope(tmp_path):
    config = load_config(_written(tmp_path, _without_origin_and_signing()))

    assert config.apps["songmaker"].mcp_origin is None
    assert config.capability_scope("songmaker", ["read_song"]) is None


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
    config_file = _written(tmp_path, VALID_CONFIG + PRESENTATOR_ON_SONGMAKERS_ORIGIN)

    assert _refused_on_one_line(config_file, capsys) == (
        "apps.presentator.mcp_origin: https://songmaker.internal is already"
        " the MCP origin of app 'songmaker'"
    )


PROFILES_WITH_NEWLINES = (
    VALID_CONFIG.replace("[profiles.claude-main]", '[profiles."claude\\nmain"]')
    + '\n[profiles."claude\\nspare"]\nprovider = "claude"\n'
)
APPS_WITH_NEWLINES = VALID_CONFIG.replace("[apps.songmaker]", '[apps."song\\nmaker"]')
SECOND_APP_ON_THE_SAME_CLIENT = '\n[apps."pres\\nentator"]\nworkload_client = "songmaker-test"\n'


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        pytest.param(
            PROFILES_WITH_NEWLINES,
            "profiles.claude\\nspare.provider: claude already has profile 'claude\\nmain'",
            id="second-profile-for-a-provider",
        ),
        pytest.param(
            APPS_WITH_NEWLINES + SECOND_APP_ON_THE_SAME_CLIENT,
            "apps.pres\\nentator.workload_client: 'songmaker-test' is already"
            " the workload client of app 'song\\nmaker'",
            id="workload-client-of-two-apps",
        ),
        pytest.param(
            APPS_WITH_NEWLINES.replace(SIGNING_SECTION, ""),
            "apps.song\\nmaker.mcp_origin: needs a [signing] section,"
            " because the app's runs carry a signed capability",
            id="origin-without-signing",
        ),
        pytest.param(
            (
                APPS_WITH_NEWLINES
                + '\n[apps."pres\\nentator"]\nworkload_client = "presentator-test"\n'
                + SONGMAKER_ORIGIN_LINE
            ),
            "apps.pres\\nentator.mcp_origin: https://songmaker.internal is already"
            " the MCP origin of app 'song\\nmaker'",
            id="origin-of-two-apps",
        ),
    ],
)
def test_a_name_with_a_newline_is_shown_escaped_once_on_one_line(tmp_path, capsys, text, refusal):
    assert _refused_on_one_line(_written(tmp_path, text), capsys) == refusal


@pytest.mark.parametrize(
    ("table_name", "shown_name"),
    [
        pytest.param('"song/maker"', "song/maker", id="A10-a-slash-makes-two-segments"),
        pytest.param('" songmaker"', " songmaker", id="A10-whitespace-around-the-name"),
        pytest.param('"song\\u0007maker"', "song\\x07maker", id="A10-a-control-character"),
    ],
)
def test_an_app_name_that_is_not_one_group_segment_stops_startup_naming_the_app(
    tmp_path, capsys, table_name, shown_name
):
    text = VALID_CONFIG.replace("[apps.songmaker]", f"[apps.{table_name}]")

    refusal = _refused_on_one_line(_written(tmp_path, text), capsys)

    assert refusal.startswith(f"apps.{shown_name}: ")


ORIGIN_WITHOUT_SIGNING = _with(SIGNING_SECTION, "")
ORIGIN_WITHOUT_SIGNING_REFUSAL = (
    "apps.songmaker.mcp_origin: needs a [signing] section,"
    " because the app's runs carry a signed capability"
)


def test_an_origin_without_a_signing_section_stops_startup_naming_the_field(tmp_path, capsys):
    config_file = _written(tmp_path, ORIGIN_WITHOUT_SIGNING)

    assert _refused_on_one_line(config_file, capsys) == ORIGIN_WITHOUT_SIGNING_REFUSAL


NO_ORIGIN = VALID_CONFIG.replace(SONGMAKER_ORIGIN_LINE, "")
ORIGIN_WITHOUT_CA_FILE_REFUSAL = (
    "mcp.ca_file: required because apps.songmaker declares an mcp_origin"
)


@pytest.mark.parametrize("mcp_section", ["", "[mcp]\n\n"], ids=["no-table", "empty-table"])
def test_an_app_origin_without_mcp_ca_file_is_refused_at_startup(tmp_path, capsys, mcp_section):
    config_file = _written(tmp_path, VALID_CONFIG.replace(MCP_SECTION, mcp_section))

    assert _refused_on_one_line(config_file, capsys) == ORIGIN_WITHOUT_CA_FILE_REFUSAL


@pytest.mark.parametrize(
    "mcp_section", ["", "[mcp]\n\n", MCP_SECTION], ids=["no-table", "empty-table", "table"]
)
def test_no_origin_needs_no_mcp_ca_file(tmp_path, mcp_section):
    text = NO_ORIGIN.replace(MCP_SECTION, mcp_section)

    assert load_config(_written(tmp_path, text)).apps["songmaker"].mcp_origin is None


def test_a_missing_mcp_ca_file_does_not_stop_startup_while_no_app_declares_an_origin(
    tmp_path, served_apps
):
    absent = tmp_path / "absent-ca.pem"
    text = NO_ORIGIN.replace(MCP_SECTION, f"[mcp]\nca_file = {json.dumps(str(absent))}\n\n")

    assert main(["--config", str(_written(tmp_path, text))]) == 0
    assert len(served_apps) == 1


def test_a_relative_mcp_ca_file_is_refused_by_the_configuration(tmp_path, capsys):
    text = VALID_CONFIG.replace(MCP_SECTION, '[mcp]\nca_file = "ca.pem"\n\n')

    assert _refused_on_one_line(_written(tmp_path, text), capsys).startswith("mcp.ca_file: ")


def test_a_missing_mcp_ca_file_stops_startup_naming_the_key(tmp_path, capsys):
    absent = tmp_path / "absent-ca.pem"
    text = VALID_CONFIG.replace(MCP_SECTION, f"[mcp]\nca_file = {json.dumps(str(absent))}\n\n")

    assert main(["--config", str(_written(tmp_path, text))]) == EXIT_BAD_CONFIG
    assert "mcp.ca_file: cannot be loaded" in capsys.readouterr().err


CANARY = "canary-token-value-7f3a91"
API_KEY_SHAPE = "sk-ant-" + "api03-" + CANARY
WHITESPACE_REASON = "holds whitespace; a setup token holds none"
JSON_DOCUMENT_REASON = "holds a JSON document; it must hold one setup token"
TOKEN_FILE_FIELD = "profiles.claude-main.token_file"


def _token_file_with(tmp_path: Path, contents: bytes, mode: int = 0o600) -> Path:
    path = tmp_path / "claude.token"
    path.write_bytes(contents)
    path.chmod(mode)
    return path


def _token_config(token_file: str) -> str:
    return _without_origin_and_signing().replace(
        TOKEN_FILE_LINE, f"token_file = {json.dumps(token_file)}"
    )


def _holding(contents: bytes, mode: int = 0o600) -> Callable[[Path, pytest.MonkeyPatch], str]:
    return lambda tmp_path, _monkeypatch: str(_token_file_with(tmp_path, contents, mode))


def _outgrowing_a_reported_size_of_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    token_file = _token_file_with(tmp_path, CANARY.encode() + b"x" * (64 * 1024))
    real_fstat = os.fstat

    def reporting_zero(descriptor):
        reported = list(real_fstat(descriptor))
        reported[stat.ST_SIZE] = 0
        return os.stat_result(reported)

    monkeypatch.setattr(os, "fstat", reporting_zero)
    return str(token_file)


def _failing_after_a_partial_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    token_file = _token_file_with(tmp_path, CANARY.encode())
    real_read = os.read
    reads = []

    def read_then_fail(descriptor, length):
        reads.append(descriptor)
        if len(reads) > 1:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real_read(descriptor, len(CANARY))

    monkeypatch.setattr(os, "read", read_then_fail)
    return str(token_file)


def _open_to_others(mode: int) -> str:
    return (
        f"is open to group or others (mode {mode:04o});"
        " a token file must grant them nothing, for example 0600 or 0400"
    )


REFUSED_TOKEN_FILES = [
    pytest.param(_holding(CANARY.encode(), 0o640), _open_to_others(0o640), id="readable-by-group"),
    pytest.param(
        _holding(CANARY.encode(), 0o644), _open_to_others(0o644), id="readable-by-everyone"
    ),
    pytest.param(
        lambda tmp_path, _monkeypatch: str(tmp_path), "is not a regular file", id="directory"
    ),
    pytest.param(
        lambda tmp_path, _monkeypatch: str(tmp_path / "absent.token"),
        "cannot be read: No such file or directory",
        id="missing",
    ),
    pytest.param(_holding(b""), "is empty; it must hold the token", id="empty"),
    pytest.param(_holding(b"\n"), "is empty; it must hold the token", id="only-a-newline"),
    pytest.param(
        _holding(f"{CANARY}\n{CANARY}\n".encode()),
        "holds more than one line; it must hold one token",
        id="two-lines",
    ),
    pytest.param(
        _holding(b"x" * (16 * 1024) + CANARY.encode()),
        "is larger than 16384 bytes",
        id="oversized",
    ),
    pytest.param(
        _holding(f"{CANARY}\x1b[0m".encode()),
        "contains a control or non-printing character; a token must not",
        id="escape-character",
    ),
    pytest.param(
        _holding(f"{CANARY}\r\n".encode()),
        "contains a control or non-printing character; a token must not",
        id="carriage-return",
    ),
    pytest.param(
        _outgrowing_a_reported_size_of_zero,
        "is larger than 16384 bytes",
        id="outgrows-a-reported-size-of-zero",
    ),
    pytest.param(
        _failing_after_a_partial_read,
        "cannot be read: Input/output error",
        id="read-fails-after-a-partial-chunk",
    ),
    pytest.param(_holding(b"\xff" + CANARY.encode()), "is not UTF-8 text", id="not-text"),
    pytest.param(
        _holding(b" " + API_KEY_SHAPE.encode()),
        WHITESPACE_REASON,
        id="api-key-shape-with-a-leading-space",
    ),
    pytest.param(
        _holding(f"{CANARY[:4]} {CANARY[4:]}".encode()), WHITESPACE_REASON, id="inner-space"
    ),
    pytest.param(_holding(f"{CANARY}  ".encode()), WHITESPACE_REASON, id="trailing-spaces"),
    pytest.param(
        _holding(
            f'{{"claudeAiOauth":{{"accessToken":"{CANARY}","refreshToken":"{CANARY}"}}}}'.encode()
        ),
        JSON_DOCUMENT_REASON,
        id="compact-credentials-json",
    ),
    pytest.param(_holding(b"{}"), JSON_DOCUMENT_REASON, id="empty-json-object"),
    pytest.param(
        _holding(f'{{"claudeAiOauth": {{"accessToken": "{CANARY}"}}}}'.encode()),
        JSON_DOCUMENT_REASON,
        id="spaced-one-line-json",
    ),
    pytest.param(_holding(f'["{CANARY}"]'.encode()), JSON_DOCUMENT_REASON, id="json-array"),
    pytest.param(_holding(f'"{CANARY}"'.encode()), JSON_DOCUMENT_REASON, id="json-string"),
    pytest.param(
        _holding(b"[" * 15000 + CANARY.encode()),
        JSON_DOCUMENT_REASON,
        id="json-nested-past-the-decoder-depth",
    ),
    pytest.param(
        _holding(b" " + b"[" * 15000 + CANARY.encode()),
        JSON_DOCUMENT_REASON,
        id="json-nested-past-the-decoder-depth-after-a-space",
    ),
    pytest.param(
        _holding(b" " + b"{" * 15000 + CANARY.encode()),
        JSON_DOCUMENT_REASON,
        id="json-object-nested-past-the-decoder-depth-after-a-space",
    ),
    pytest.param(_holding(b"12345"), JSON_DOCUMENT_REASON, id="json-number"),
    pytest.param(
        _holding(f'{{"a":"{CANARY}"'.encode()), JSON_DOCUMENT_REASON, id="truncated-json-object"
    ),
    pytest.param(
        _holding(f'{{\n  "claudeAiOauth": {{"accessToken": "{CANARY}"}}\n}}\n'.encode()),
        "holds more than one line; it must hold one token",
        id="multi-line-json-object",
    ),
    pytest.param(
        _holding(API_KEY_SHAPE.encode()),
        "holds an API key; it must hold a subscription token from setup-token",
        id="api-key-shape",
    ),
    pytest.param(
        _holding(f"{API_KEY_SHAPE}\n".encode()),
        "holds an API key; it must hold a subscription token from setup-token",
        id="api-key-shape-with-a-newline",
    ),
]


@pytest.mark.parametrize(("arrange", "reason"), REFUSED_TOKEN_FILES)
def test_an_unsound_token_file_stops_startup_with_one_line_naming_field_and_path_only(
    tmp_path, capsys, monkeypatch, arrange, reason
):
    token_file = arrange(tmp_path, monkeypatch)
    config_file = _written(tmp_path, _token_config(token_file))

    refusal = _refused_on_one_line(config_file, capsys)

    assert refusal == f"{TOKEN_FILE_FIELD}: {token_file} {reason}"
    assert CANARY not in refusal


@pytest.mark.parametrize("inside_active_handler", CALLED_PLAINLY_OR_FROM_AN_ACTIVE_HANDLER)
@pytest.mark.parametrize(("arrange", "reason"), REFUSED_TOKEN_FILES)
def test_an_unsound_token_file_leaks_no_content_into_the_refusal_or_what_it_holds(
    tmp_path, monkeypatch, arrange, reason, inside_active_handler
):
    config_file = _written(tmp_path, _token_config(arrange(tmp_path, monkeypatch)))

    with pytest.raises(ConfigError) as refusal:
        called_from(lambda: load_config(config_file), inside_active_handler)

    held = everything_a_refusal_holds(refusal.value)
    assert not reaches_text(held, [CANARY])
    assert CANARY not in repr(held)


@pytest.mark.parametrize("token_file", ["secrets/claude.token", "~/.secrets/claude.token"])
def test_a_relative_token_path_is_refused_naming_the_field(tmp_path, capsys, token_file):
    config_file = _written(tmp_path, _token_config(token_file))

    assert _refused_on_one_line(config_file, capsys) == (
        f"{TOKEN_FILE_FIELD}: {token_file} must be an absolute path to the token file;"
        " ~ is not expanded"
    )


@pytest.mark.parametrize("trailing", ["", "\n"])
def test_a_sound_token_file_loads_with_or_without_a_trailing_newline_keeping_its_path(
    tmp_path, trailing
):
    token_file = _token_file_with(tmp_path, (CANARY + trailing).encode(), 0o400)

    config = load_config(_written(tmp_path, _token_config(str(token_file))))

    assert config.profiles["claude-main"].token_file == token_file


@pytest.mark.parametrize("flag", ["O_NONBLOCK", "O_CLOEXEC"])
def test_a_token_file_is_refused_without_content_where_os_lacks_an_open_flag(
    tmp_path, monkeypatch, flag
):
    token_file = _token_file_with(tmp_path, CANARY.encode())
    monkeypatch.delattr(os, flag)

    with pytest.raises(UnsoundSecretFile, match=flag) as refusal:
        read_token_file(token_file)

    assert CANARY not in f"{refusal.value}{refusal.value.__cause__}"


def test_the_checked_token_never_shows_in_the_repr_text_or_dump_of_the_configuration(tmp_path):
    token_file = _token_file_with(tmp_path, CANARY.encode())

    config = load_config(_written(tmp_path, _token_config(str(token_file))))

    assert not reaches_text([repr(config), str(config), repr(config.profiles)], [CANARY])
    assert CANARY not in config.model_dump_json()


def test_a_path_swapped_after_the_open_is_judged_by_the_opened_token_file(tmp_path, monkeypatch):
    token_file = _token_file_with(tmp_path, CANARY.encode())
    real_open = os.open

    def open_then_swap(path, flags, *arguments):
        descriptor = real_open(path, flags, *arguments)
        impostor = tmp_path / "impostor.token"
        impostor.write_bytes(b'{"impostor": true}')
        impostor.chmod(0o600)
        impostor.rename(token_file)
        return descriptor

    monkeypatch.setattr(os, "open", open_then_swap)

    config = load_config(_written(tmp_path, _token_config(str(token_file))))

    assert config.profiles["claude-main"].token_file == token_file


PROFILE_TABLE = '[profiles.claude-main]\nprovider = "claude"\n' + TOKEN_FILE_LINE + "\n\n"
NO_PROFILE = _with(PROFILE_TABLE, "")
WITHOUT_TOKEN_FILE = _with(TOKEN_FILE_LINE + "\n", "")
WITHOUT_ORIGIN = _with(SONGMAKER_ORIGIN_LINE, "")
@pytest.fixture
def served_apps(monkeypatch, logging_state_restored):
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **where: served.append((app, where)))
    return served


def _with_profile_of(provider: str, name: str = '"codex-main"', token: bool = False) -> str:
    extra = TOKEN_FILE_LINE + "\n" if token else ""
    return NO_PROFILE.replace(
        "[route]", f'[profiles.{name}]\nprovider = "{provider}"\n{extra}\n[route]', 1
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            _without_origin_and_signing().replace(PROFILE_TABLE, ""), id="no-profile"
        ),
        pytest.param(VALID_CONFIG, id="one-claude-profile-with-its-token"),
    ],
)
def test_a_document_with_no_profile_or_one_sound_claude_profile_reaches_serving(
    tmp_path, served_apps, monkeypatch, text
):
    app_configs = []
    create_app = app_module.create_app
    monkeypatch.setattr(
        app_module,
        "create_app",
        lambda config, **keywords: app_configs.append(config) or create_app(config, **keywords),
    )

    assert main(["--config", str(config_file := _written(tmp_path, text))]) == 0

    assert [config.profiles for config in app_configs] == [load_config(config_file).profiles]

    assert [where for _app, where in served_apps] == [{"host": "127.0.0.1", "port": 8099}]


def test_an_installation_without_a_profile_has_none_and_still_scopes_its_apps(tmp_path):
    config = load_config(_written(tmp_path, NO_PROFILE))

    assert config.profiles == {}
    assert config.capability_scope("songmaker", []) is not None


PROFILE_RULE_REFUSALS = [
    pytest.param(
        WITHOUT_TOKEN_FILE,
        "profiles.claude-main.token_file: required for a claude profile",
        id="claude-without-a-token-file",
    ),
    pytest.param(
        WITHOUT_TOKEN_FILE.replace(CLI_SECTION, ""),
        "profiles.claude-main.token_file: required for a claude profile",
        id="claude-without-a-token-file-nor-cli-section",
    ),
    pytest.param(
        _with_profile_of("codex"),
        "profiles.codex-main.provider: codex has no subscription route yet;"
        " only claude can have a profile",
        id="codex",
    ),
    pytest.param(
        _with_profile_of("grok", '"grok-main"'),
        "profiles.grok-main.provider: grok has no subscription route yet;"
        " only claude can have a profile",
        id="grok",
    ),
    pytest.param(
        _with_profile_of("codex", token=True),
        "profiles.codex-main.provider: codex has no subscription route yet;"
        " only claude can have a profile",
        id="codex-with-a-token-file",
    ),
    pytest.param(
        VALID_CONFIG + SECOND_CLAUDE_PROFILE,
        "profiles.claude-spare.provider: claude already has profile 'claude-main'",
        id="two-claude-profiles",
    ),
    pytest.param(
        VALID_CONFIG + '\n[profiles.codex-main]\nprovider = "codex"\n',
        "profiles.codex-main.provider: codex has no subscription route yet;"
        " only claude can have a profile",
        id="claude-and-codex",
    ),
]


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        *PROFILE_RULE_REFUSALS,
        pytest.param(
            _with('provider = "claude"', 'provider = "gemini"'),
            "profiles.claude-main.provider: Input should be",
            id="unknown-provider",
        ),
        pytest.param(
            _with(TOKEN_FILE_LINE, 'token_file = ""'),
            "profiles.claude-main.token_file:  must be an absolute path to the token file;"
            " ~ is not expanded",
            id="empty-token-file-path",
        ),
    ],
)
def test_a_profile_the_service_cannot_serve_stops_startup_naming_the_field(
    tmp_path, capsys, text, refusal
):
    assert _refused_on_one_line(_written(tmp_path, text), capsys).startswith(refusal)


@pytest.mark.parametrize("inside_active_handler", CALLED_PLAINLY_OR_FROM_AN_ACTIVE_HANDLER)
@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        *PROFILE_RULE_REFUSALS,
        pytest.param(
            ORIGIN_WITHOUT_SIGNING, ORIGIN_WITHOUT_SIGNING_REFUSAL, id="origin-without-signing"
        ),
    ],
)
def test_a_refused_relationship_holds_no_token_or_key_in_what_the_refusal_holds(
    tmp_path, text, refusal, inside_active_handler
):
    with pytest.raises(ConfigError) as refused:
        called_from(lambda: load_config(_written(tmp_path, text)), inside_active_handler)

    held = everything_a_refusal_holds(refused.value)
    assert refusal in str(refused.value)
    assert not reaches_text(held, [PROCESS_TOKEN])
    assert not reaches_private_key(held)


@pytest.mark.parametrize(
    ("control", "shown_name"),
    [
        pytest.param("\\r", "a\\rb", id="carriage-return"),
        pytest.param("\\u001b", "a\\x1bb", id="escape"),
        pytest.param("\\n", "a\\nb", id="newline"),
    ],
)
@pytest.mark.parametrize(
    ("arrange", "field"),
    [
        pytest.param(
            lambda name: _with_profile_of("codex", name),
            "provider: codex has no subscription route yet",
            id="provider",
        ),
        pytest.param(
            lambda name: WITHOUT_TOKEN_FILE.replace("claude-main", name),
            "token_file: required for a claude profile",
            id="token-file",
        ),
        pytest.param(
            lambda name: ORIGIN_WITHOUT_SIGNING.replace("[apps.songmaker]", f"[apps.{name}]"),
            "mcp_origin: needs a [signing] section",
            id="mcp-origin",
        ),
    ],
)
def test_a_name_with_a_control_character_is_refused_on_one_line_with_no_raw_control_character(
    tmp_path, capsys, arrange, field, control, shown_name
):
    name = f'"a{control}b"'
    refusal = _refused_on_one_line(_written(tmp_path, arrange(name)), capsys)

    assert field in refusal
    assert shown_name in refusal
    assert refusal.isprintable()


@pytest.mark.parametrize(
    ("text", "fields"),
    [
        pytest.param(
            WITHOUT_TOKEN_FILE.replace(SONGMAKER_ORIGIN_LINE, ""),
            ("profiles.claude-main.token_file", "apps.songmaker.mcp_origin"),
            id="missing-token-and-missing-origin",
        ),
        pytest.param(
            _with_profile_of("codex").replace(SONGMAKER_ORIGIN_LINE, ""),
            ("profiles.codex-main.provider", "apps.songmaker.mcp_origin"),
            id="codex-and-missing-origin",
        ),
        pytest.param(
            _with("port = 8099", "port = 8099\nworkers = 4").replace(SONGMAKER_ORIGIN_LINE, ""),
            ("server.workers", "apps.songmaker.mcp_origin"),
            id="field-error-and-missing-origin",
        ),
    ],
)
def test_a_document_with_several_defects_is_refused_on_one_line_naming_one_of_them(
    tmp_path, capsys, text, fields
):
    refusal = _refused_on_one_line(_written(tmp_path, text), capsys)

    assert any(field in refusal for field in fields)


MALFORMED_PIN_REFUSAL = (
    "cli.claude.version: must be three dot-separated integers without leading zeros,"
    " written as a string, such as 2.1.302"
)
NOT_A_PROGRAM_PATH = (
    "cli.claude.binary: {shown}must be an absolute path to the program; ~ is not expanded"
)


@pytest.mark.parametrize(
    ("binary_line", "refusal"),
    [
        pytest.param(
            'binary = "claude"',
            NOT_A_PROGRAM_PATH.format(shown="claude "),
            id="bare-name",
        ),
        pytest.param(
            'binary = "./claude"',
            NOT_A_PROGRAM_PATH.format(shown="./claude "),
            id="dot-relative",
        ),
        pytest.param(
            'binary = "~/claude"',
            NOT_A_PROGRAM_PATH.format(shown="~/claude "),
            id="tilde",
        ),
        pytest.param("binary = 7", NOT_A_PROGRAM_PATH.format(shown=""), id="number"),
        pytest.param("", "cli.claude.binary: Field required", id="missing"),
    ],
)
def test_a_program_binary_that_is_not_an_absolute_path_is_refused_by_field(
    tmp_path, capsys, binary_line, refusal
):
    text = _with(
        CLI_SECTION,
        cli_section(binary=binary_line, version=CLAUDE_VERSION_LINE, root=WORKING_ROOT_LINE),
    )

    assert _refused_on_one_line(_written(tmp_path, text), capsys) == refusal


@pytest.mark.parametrize(
    ("version_line", "refusal"),
    [
        pytest.param("", "cli.claude.version: Field required", id="missing"),
        pytest.param('version = "2.1"', MALFORMED_PIN_REFUSAL, id="two-integers"),
        pytest.param('version = "2.1.302.1"', MALFORMED_PIN_REFUSAL, id="four-integers"),
        pytest.param('version = "v2.1.302"', MALFORMED_PIN_REFUSAL, id="v-prefix"),
        pytest.param('version = "2.01.302"', MALFORMED_PIN_REFUSAL, id="leading-zero"),
        pytest.param('version = "2.1.x"', MALFORMED_PIN_REFUSAL, id="not-a-number"),
        pytest.param('version = ""', MALFORMED_PIN_REFUSAL, id="empty"),
        pytest.param("version = 2", MALFORMED_PIN_REFUSAL, id="integer"),
    ],
)
def test_a_pinned_version_that_is_not_three_integers_is_refused_by_field(
    tmp_path, capsys, version_line, refusal
):
    text = _with(
        CLI_SECTION,
        cli_section(binary=CLAUDE_BINARY_LINE, version=version_line, root=WORKING_ROOT_LINE),
    )

    assert _refused_on_one_line(_written(tmp_path, text), capsys) == refusal


def _with_models(models_line: str) -> str:
    return _with(
        CLI_SECTION,
        cli_section(
            binary=CLAUDE_BINARY_LINE,
            version=CLAUDE_VERSION_LINE,
            root=WORKING_ROOT_LINE,
            models=models_line,
        ),
    )


@pytest.mark.parametrize(
    "models_line",
    [
        pytest.param("", id="C1-missing"),
        pytest.param('models = "sonnet"', id="C2-a-string"),
        pytest.param('models = ["sonnet@1"]', id="C3-an-entry-outside-the-grammar"),
    ],
)
def test_a_claude_program_without_a_models_array_in_the_grammar_is_refused_by_field(
    tmp_path, capsys, models_line
):
    refusal = _refused_on_one_line(_written(tmp_path, _with_models(models_line)), capsys)

    assert refusal.startswith("cli.claude.models")


@pytest.mark.parametrize(
    ("models_line", "listed"),
    [
        pytest.param('models = ["sonnet", "opus"]', {"sonnet", "opus"}, id="C4-two-names"),
        pytest.param("models = []", set(), id="C5-none"),
    ],
)
def test_a_claude_program_lists_the_models_it_serves(tmp_path, models_line, listed):
    config = load_config(_written(tmp_path, _with_models(models_line)))

    assert config.subscription_models() == {Provider.CLAUDE: listed}


def _cli_naming_the_working_root(root_line: str, program: Path) -> str:
    binary_line = f"binary = {json.dumps(str(program))}"
    return _with(
        CLI_SECTION,
        cli_section(binary=binary_line, version=CLAUDE_VERSION_LINE, root=root_line),
    )


def _root_line(root: str) -> str:
    return f"working_root = {json.dumps(root)}"


def _private_directory(path: Path, mode: int = 0o700) -> Path:
    path.mkdir()
    path.chmod(mode)
    return path


def _a_missing_root(tmp_path: Path) -> str:
    return str(tmp_path / "absent-root")


def _a_regular_file_as_root(tmp_path: Path) -> str:
    path = tmp_path / "root-file"
    path.write_text("")
    return str(path)


def _a_link_to_a_private_directory(tmp_path: Path) -> str:
    link = tmp_path / "root-link"
    link.symlink_to(_private_directory(tmp_path / "real-root"))
    return str(link)


def _a_root_open_to_the_group(tmp_path: Path) -> str:
    return str(_private_directory(tmp_path / "open-root", 0o750))


def _a_private_root_written_with_a_parent_component(tmp_path: Path) -> str:
    _private_directory(tmp_path / "x")
    return str(_private_directory(tmp_path / "root").parent / "x" / ".." / "root")


NOT_PRIVATE_ROOTS = (
    pytest.param(lambda _tmp: "run/x", "relative", id="relative"),
    pytest.param(lambda _tmp: "~/x", "relative", id="tilde"),
    pytest.param(lambda _tmp: "", "relative", id="empty"),
    pytest.param(_a_missing_root, "missing", id="absent"),
    pytest.param(_a_regular_file_as_root, "missing", id="regular-file"),
    pytest.param(_a_link_to_a_private_directory, "symlink", id="symlink"),
    pytest.param(_a_root_open_to_the_group, "wrong_mode", id="mode-0750"),
    pytest.param(
        _a_private_root_written_with_a_parent_component,
        "parent_component",
        id="parent-component",
    ),
)


def _core_config_seeing(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    from agent_runtime import config as core_config

    seen = types.SimpleNamespace(**{"lstat": os.lstat, "geteuid": os.geteuid, **overrides})
    monkeypatch.setattr(core_config, "os", seen)


def _the_probe_never_started(probe_log: Path) -> bool:
    return not probe_log.exists()


def test_a_cli_table_loads_its_working_root_and_each_program_table(tmp_path):
    config = load_config(_written(tmp_path, VALID_CONFIG))

    assert config.cli.working_root == process_working_root()
    assert list(config.cli.programs()) == [Provider.CLAUDE]
    assert config.cli.claude.binary == process_claude_cli()
    assert config.cli.claude.version == PINNED_CLAUDE_VERSION


def _config_naming_a_marked_root(tmp_path: Path, make_the_root: bool) -> Path:
    marked_root = tmp_path / ROOT_MARKER
    if make_the_root:
        _private_directory(marked_root)
    program = fake_cli_executable(tmp_path, {("--version",): "any"})
    return _written(tmp_path, _cli_naming_the_working_root(_root_line(str(marked_root)), program))


def test_a_loaded_config_shows_no_working_root_in_its_repr_or_str(tmp_path):
    config = load_config(_config_naming_a_marked_root(tmp_path, make_the_root=True))

    shown = [repr(config), str(config), repr(config.cli), str(config.cli)]

    assert [ROOT_MARKER in text for text in shown] == [False] * len(shown)


def test_a_refused_working_root_leaves_no_validation_error_behind_to_leak_it(tmp_path):
    config_file = _config_naming_a_marked_root(tmp_path, make_the_root=False)

    with pytest.raises(ConfigError) as refusal:
        load_config(config_file)

    assert (refusal.value.__cause__, refusal.value.__context__) == (None, None)
    assert ROOT_MARKER not in str(refusal.value)


def test_a_cli_section_without_a_working_root_is_refused_before_any_probe(tmp_path, capsys):
    probe_log = tmp_path / "probe-starts"
    program = fake_cli_executable(tmp_path, {("--version",): "any"}, calls_log=probe_log)
    text = _cli_naming_the_working_root("", program)

    refusal = _refused_on_one_line(_written(tmp_path, text), capsys)

    assert refusal == "cli.working_root: Field required"
    assert _the_probe_never_started(probe_log)


@pytest.mark.parametrize(("arrange", "violation"), NOT_PRIVATE_ROOTS)
def test_a_working_root_that_is_not_private_stops_the_start_naming_only_its_violation(
    tmp_path, capsys, arrange, violation
):
    probe_log = tmp_path / "probe-starts"
    program = fake_cli_executable(tmp_path, {("--version",): "any"}, calls_log=probe_log)
    root = arrange(tmp_path)
    text = _cli_naming_the_working_root(_root_line(root), program)

    refusal = _refused_on_one_line(_written(tmp_path, text), capsys)

    assert refusal == f"cli.working_root: not private: {violation}"
    assert _the_probe_never_started(probe_log)


def test_a_working_root_owned_by_another_user_stops_the_start_naming_only_its_violation(
    tmp_path, capsys, monkeypatch
):
    root = _private_directory(tmp_path / "foreign-root")
    program = fake_cli_executable(tmp_path, {("--version",): "any"})
    text = _cli_naming_the_working_root(_root_line(str(root)), program)
    _core_config_seeing(monkeypatch, geteuid=lambda: os.stat(root).st_uid + 1)

    refusal = _refused_on_one_line(_written(tmp_path, text), capsys)

    assert refusal == "cli.working_root: not private: foreign_owner"


def test_a_working_root_that_cannot_be_inspected_stops_the_start_without_the_failure_text(
    tmp_path, capsys, monkeypatch
):
    root = _private_directory(tmp_path / "unreadable-root")
    program = fake_cli_executable(tmp_path, {("--version",): "any"})
    text = _cli_naming_the_working_root(_root_line(str(root)), program)

    def refuse_to_inspect(_path):
        raise PermissionError(errno.EACCES, "inspection-failure-text")

    _core_config_seeing(monkeypatch, lstat=refuse_to_inspect)

    refusal = _refused_on_one_line(_written(tmp_path, text), capsys)

    assert refusal == "cli.working_root: not private: uninspectable"


def test_a_working_root_that_is_not_a_string_is_refused_without_echoing_it(tmp_path, capsys):
    program = fake_cli_executable(tmp_path, {("--version",): "any"})
    text = _cli_naming_the_working_root("working_root = 7", program)

    refusal = _refused_on_one_line(_written(tmp_path, text), capsys)

    assert (
        refusal
        == "cli.working_root: must be an absolute path to the working root; ~ is not expanded"
    )


def test_a_bad_working_root_and_a_bad_token_file_are_both_named_on_one_line(tmp_path, capsys):
    root = _a_missing_root(tmp_path)
    unreadable = tmp_path / "unreadable.token"
    program = fake_cli_executable(tmp_path, {("--version",): "any"})
    text = _cli_naming_the_working_root(_root_line(root), program).replace(
        TOKEN_FILE_LINE, f"token_file = {json.dumps(str(unreadable))}"
    )

    refusal = _refused_on_one_line(_written(tmp_path, text), capsys)

    fields = [part.split(":", 1)[0] for part in refusal.split("; ")]
    assert sorted(fields) == ["cli.working_root", TOKEN_FILE_FIELD]
    assert root not in refusal.replace(str(unreadable), "")


PROGRAM_TABLE_REQUIRED = "cli.claude: required for a claude profile"
ROOT_ONLY_CLI = cli_section(binary="", version="", root=WORKING_ROOT_LINE, models="")
FORMER_FLAT_KEYS = f"claude_{CLAUDE_BINARY_LINE}\nclaude_{CLAUDE_VERSION_LINE}\n"
FORMER_FLAT_KEYS_REFUSED = (
    "cli.claude_binary: Extra inputs are not permitted;"
    " cli.claude_version: Extra inputs are not permitted"
)
UNCHANGED_V030_CLI = f"[cli]\n{FORMER_FLAT_KEYS}\n"
WITH_ROOT_AND_FLAT_KEYS = f"[cli]\n{WORKING_ROOT_LINE}\n{FORMER_FLAT_KEYS}\n"


def _unreadable_token_file(tmp_path: Path, text: str) -> str:
    unreadable = tmp_path / "missing.token"
    return text.replace(TOKEN_FILE_LINE, f"token_file = {json.dumps(str(unreadable))}")


@pytest.mark.parametrize(
    "cli_text",
    [
        pytest.param("", id="no-cli-table"),
        pytest.param(ROOT_ONLY_CLI, id="working-root-only"),
        pytest.param(UNCHANGED_V030_CLI, id="former-flat-keys"),
    ],
)
def test_a_claude_profile_without_its_program_table_is_refused_before_its_token_file_is_opened(
    tmp_path, capsys, cli_text
):
    text = _unreadable_token_file(tmp_path, _with(CLI_SECTION, cli_text))

    assert _refused_on_one_line(_written(tmp_path, text), capsys) == PROGRAM_TABLE_REQUIRED


@pytest.mark.parametrize(
    ("cli_text", "refusal"),
    [
        pytest.param(WITH_ROOT_AND_FLAT_KEYS, FORMER_FLAT_KEYS_REFUSED, id="beside-the-root"),
        pytest.param(
            UNCHANGED_V030_CLI,
            f"cli.working_root: Field required; {FORMER_FLAT_KEYS_REFUSED}",
            id="unchanged-v030",
        ),
        pytest.param(
            f"[cli]\n{WORKING_ROOT_LINE}\n{FORMER_FLAT_KEYS}\n[cli.claude]\n"
            f"{CLAUDE_BINARY_LINE}\n{CLAUDE_VERSION_LINE}\n{MODELS_LINE}\n\n",
            FORMER_FLAT_KEYS_REFUSED,
            id="beside-a-program-table",
        ),
    ],
)
def test_the_former_flat_program_keys_stop_the_start_naming_each_key(
    tmp_path, capsys, cli_text, refusal
):
    probe_log = tmp_path / "probe-starts"
    program = fake_cli_executable(tmp_path, {("--version",): "any"}, calls_log=probe_log)
    logged_binary_line = f"binary = {json.dumps(str(program))}"
    text = NO_PROFILE.replace(CLI_SECTION, cli_text.replace(CLAUDE_BINARY_LINE, logged_binary_line))

    assert _refused_on_one_line(_written(tmp_path, text), capsys) == refusal
    assert _the_probe_never_started(probe_log)


@pytest.mark.parametrize("provider", ["codex", "grok"])
def test_a_program_table_for_a_provider_without_a_version_reader_stops_the_start(
    tmp_path, capsys, provider
):
    probe_log = tmp_path / "probe-starts"
    program = fake_cli_executable(tmp_path, {("--version",): "any"}, calls_log=probe_log)
    table = f'[cli.{provider}]\nbinary = {json.dumps(str(program))}\nversion = "1.0.60"\n\n'
    text = NO_PROFILE.replace(CLI_SECTION, ROOT_ONLY_CLI + table)

    refusal = _refused_on_one_line(_written(tmp_path, text), capsys)

    assert refusal == f"cli.{provider}: Extra inputs are not permitted"
    assert _the_probe_never_started(probe_log)


def test_the_working_root_belongs_to_the_cli_table_not_to_a_program(tmp_path, capsys):
    program_with_root = (
        cli_section(binary=CLAUDE_BINARY_LINE, version=CLAUDE_VERSION_LINE, root="").rstrip("\n")
        + f"\n{WORKING_ROOT_LINE}\n\n"
    )
    text = NO_PROFILE.replace(CLI_SECTION, program_with_root)

    assert _refused_on_one_line(_written(tmp_path, text), capsys) == (
        "cli.working_root: Field required; cli.claude.working_root: Extra inputs are not permitted"
    )


def test_a_second_signing_table_is_refused_as_a_duplicate_table(tmp_path, capsys):
    text = VALID_CONFIG + f"\n{SIGNING_SECTION}"

    assert _refused_on_one_line(_written(tmp_path, text), capsys).startswith("not TOML")


def _songmaker(**members: Any) -> AppConfig:
    return AppConfig(workload_client="songmaker-test", **members)


def _edited(edit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    document = tomllib.loads(VALID_CONFIG)
    edit(document)
    return document


def _profile_edit(**members: Any) -> Callable[[dict[str, Any]], None]:
    return lambda document: document["profiles"]["claude-main"].update(members)


def _app_edit(*, signing: bool = True, **members: Any) -> Callable[[dict[str, Any]], None]:
    def edit(document: dict[str, Any]) -> None:
        document["apps"]["songmaker"].update(members)
        if not signing:
            del document["signing"]

    return edit


def _profiles_edit(profiles: dict[Any, Any]) -> Callable[[dict[str, Any]], None]:
    return lambda document: document.update(profiles=profiles)


NO_ROUTE_REFUSAL = "has no subscription route yet; only claude can have a profile"
UNIQUE_CLAUDE_PROFILE_REFUSAL = "claude already has profile"

REFUSED_REPRESENTATIONS = [
    pytest.param(
        _profile_edit(token_file=None),
        "profiles.claude-main.token_file: required for a claude profile",
        id="token-file-none",
    ),
    pytest.param(
        _profiles_edit({"claude-main": ProfileConfig(provider=Provider.CLAUDE)}),
        "profiles.claude-main.token_file: required for a claude profile",
        id="tokenless-claude-instance",
    ),
    pytest.param(
        _profiles_edit({"codex-main": ProfileConfig(provider=Provider.CODEX)}),
        NO_ROUTE_REFUSAL,
        id="codex-instance",
    ),
    pytest.param(
        _profiles_edit(
            {"first": ProfileConfig(provider="claude"), "second": ProfileConfig(provider="claude")}
        ),
        UNIQUE_CLAUDE_PROFILE_REFUSAL,
        id="two-claude-instances",
    ),
    pytest.param(
        _profile_edit(provider=b"codex"), "profiles.claude-main.provider", id="bytes-provider"
    ),
    pytest.param(
        _profiles_edit({b"p": {"provider": "codex"}}), "profiles", id="bytes-profile-name"
    ),
    pytest.param(
        lambda document: document.update(
            signing=None,
            apps={"songmaker": _songmaker(mcp_origin="https://songmaker.internal")},
        ),
        ORIGIN_WITHOUT_SIGNING_REFUSAL,
        id="origin-instance-with-signing-none",
    ),
    pytest.param(
        lambda document: document.update(signing=None),
        ORIGIN_WITHOUT_SIGNING_REFUSAL,
        id="origin-with-signing-none",
    ),
]


@pytest.mark.parametrize("inside_active_handler", CALLED_PLAINLY_OR_FROM_AN_ACTIVE_HANDLER)
@pytest.mark.parametrize(("edit", "refusal"), REFUSED_REPRESENTATIONS)
def test_every_representation_pydantic_accepts_of_a_refused_relationship_is_refused(
    edit, refusal, inside_active_handler
):
    document = _edited(edit)

    with pytest.raises(ValidationError) as refused:
        called_from(lambda: ServiceConfig.model_validate(document), inside_active_handler)

    held = everything_a_refusal_holds(refused.value)
    assert refusal in str(refused.value)
    assert not reaches_text(held, [PROCESS_TOKEN])
    assert not reaches_private_key(held)


def test_an_app_may_name_no_origin_explicitly_and_needs_no_signing():
    document = _edited(
        lambda document: (
            document.pop("signing"),
            document["apps"]["songmaker"].update(mcp_origin=None),
        )
    )

    config = ServiceConfig.model_validate(document)

    assert config.apps["songmaker"].mcp_origin is None


def _with_models_table(*lines: str) -> str:
    return VALID_CONFIG + "\n[apps.songmaker.models]\n" + "".join(f"{line}\n" for line in lines)


@pytest.fixture
def no_secret_is_read(monkeypatch):
    def fail_instead_of_reading(*_arguments, **_where):
        raise AssertionError("a secret file was read before the models table was refused")

    monkeypatch.setattr("agent_runtime_service.config.load_signing_key", fail_instead_of_reading)
    monkeypatch.setattr("agent_runtime_service.config.read_token_file", fail_instead_of_reading)


@pytest.mark.parametrize(
    ("agent", "shown"),
    [
        pytest.param('"Cowriter"', "Cowriter", id="C1-capital"),
        pytest.param('"1a"', "1a", id="C2-starts-with-a-digit"),
        pytest.param('""', "", id="C3-empty"),
        pytest.param(f'"a{"b" * 64}"', f"a{'b' * 64}", id="C4-65-characters"),
        pytest.param('"a/b"', "a/b", id="C5-slash"),
    ],
)
def test_c1_to_c5_an_agent_name_outside_the_grammar_stops_startup_naming_it(
    tmp_path, capsys, no_secret_is_read, agent, shown
):
    config_file = _written(tmp_path, _with_models_table(f'{agent} = "claude/sonnet"'))

    refusal = _refused_on_one_line(config_file, capsys)

    assert refusal.startswith(f"apps.songmaker.models.{shown}: agent name")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param('"sonnet"', id="C6-no-provider"),
        pytest.param('"claude/sonnet@2"', id="C7-versioned"),
        pytest.param('""', id="C8-empty"),
        pytest.param("5", id="C9-not-a-string"),
    ],
)
def test_c6_to_c9_a_model_outside_the_grammar_stops_startup_naming_the_agent(
    tmp_path, capsys, no_secret_is_read, value
):
    config_file = _written(tmp_path, _with_models_table(f"cowriter = {value}"))

    refusal = _refused_on_one_line(config_file, capsys)

    assert refusal.startswith("apps.songmaker.models.cowriter: model")


def test_c10_a_models_entry_that_is_not_a_table_stops_startup_naming_the_table(
    tmp_path, capsys, no_secret_is_read
):
    config_file = _written(
        tmp_path,
        VALID_CONFIG.replace(
            SONGMAKER_ORIGIN_LINE, SONGMAKER_ORIGIN_LINE + 'models = "claude/sonnet"\n'
        ),
    )

    refusal = _refused_on_one_line(config_file, capsys)

    assert refusal.startswith("apps.songmaker.models: must be a table")


def test_c11_an_override_loads_and_replaces_the_requested_model_of_its_agent(tmp_path):
    config = load_config(_written(tmp_path, _with_models_table('cowriter = "claude/sonnet"')))

    assert config.model_for("songmaker", "cowriter", "claude/opus") == "claude/sonnet"


def test_c12_without_a_models_table_the_requested_model_stays(tmp_path):
    config = load_config(_written(tmp_path, VALID_CONFIG))

    assert config.model_for("songmaker", "cowriter", "claude/opus") == "claude/opus"
    assert config.model_for("songmaker", "cowriter", None) is None


@pytest.mark.parametrize(
    "model",
    [
        pytest.param("claude/opus", id="C13-unlisted"),
        pytest.param("xai/grok-4.6", id="C14-another-provider"),
    ],
)
def test_c13_c14_a_model_the_subscription_does_not_list_is_a_valid_override(tmp_path, model):
    text = _with_models_table(f'cowriter = "{model}"').replace(MODELS_LINE, 'models = ["sonnet"]')

    config = load_config(_written(tmp_path, text))

    assert config.model_for("songmaker", "cowriter", None) == model
