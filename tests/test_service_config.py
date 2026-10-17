"""The service starts only from a configuration it accepts, and names the field it refuses."""

from __future__ import annotations

import errno
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
import uvicorn
from service_support import (
    CALLED_PLAINLY_OR_FROM_AN_ACTIVE_HANDLER,
    LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT,
    called_from,
    everything_a_refusal_holds,
    process_signing_key_file,
    process_token_file,
    reaches_text,
    record_signed_tokens,
    tool_names_with_the_last_of_length,
)

from agent_client.tools import ToolEffect
from agent_service.cli import EXIT_BAD_CONFIG, main
from agent_service.config import ConfigError, Provider, load_config

SONGMAKER_ORIGIN_LINE = 'mcp_origin = "https://songmaker.internal"\n'
SIGNING_KEY_LINE = f"key_file = {json.dumps(str(process_signing_key_file()))}"
SIGNING_SECTION = f"[signing]\n{SIGNING_KEY_LINE}\n\n"
TOKEN_FILE_LINE = f"token_file = {json.dumps(str(process_token_file()))}"
TOOLS_LINE = 'tools = { read_song = "read_only" }'

VALID_CONFIG = f"""
[identity]
issuer = "https://keycloak.test/realms/agent-test"
jwks_url = "https://keycloak.test/realms/agent-test/protocol/openid-connect/certs"

[server]
host = "127.0.0.1"
port = 8099

{SIGNING_SECTION}[profiles.claude-main]
provider = "claude"
{TOKEN_FILE_LINE}

[purposes."songmaker.cowriter"]
cli_provider = "claude"
api_alias = "cowriter"

[apps.songmaker]
workload_client = "songmaker-test"
purposes = ["songmaker.cowriter"]
{TOOLS_LINE}
{SONGMAKER_ORIGIN_LINE}"""


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
    assert config.runs.max_parallel_cli_runs == 5
    assert config.app_of_workload_client("songmaker-test") == "songmaker"


def test_an_installation_without_tools_needs_no_signing_section(tmp_path):
    assert load_config(_written(tmp_path, _without_tools_origin_and_signing())).signing is None


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


def _without_tools_origin_and_signing() -> str:
    return (
        _with(SIGNING_SECTION, "").replace(TOOLS_LINE + "\n", "").replace(SONGMAKER_ORIGIN_LINE, "")
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
                "runs.max_parallel_cli_runs"
                if setting.startswith("max_parallel_cli_runs")
                else "runs",
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
    '\n[apps.presentator]\nworkload_client = "presentator-test"\npurposes = []\n'
    + SONGMAKER_ORIGIN_LINE
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
    assert config.capability_scope("songmaker").origin == origin


def test_an_app_without_an_origin_and_without_signing_has_no_capability_scope(tmp_path):
    config = load_config(_written(tmp_path, _without_tools_origin_and_signing()))

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
    config_file = _written(tmp_path, VALID_CONFIG + PRESENTATOR_ON_SONGMAKERS_ORIGIN)

    assert _refused_on_one_line(config_file, capsys) == (
        "apps.presentator.mcp_origin: https://songmaker.internal is already"
        " the MCP origin of app 'songmaker'"
    )


def _tools_line_of_the_tuned_app(last_tool_name_length: int) -> str:
    names = tool_names_with_the_last_of_length(last_tool_name_length)
    return "tools = { " + ", ".join(f'"{name}" = "read_only"' for name in names) + " }"


def _tuned_app_config(last_tool_name_length: int = LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT):
    tools_line = _tools_line_of_the_tuned_app(last_tool_name_length)
    return _with(TOOLS_LINE, tools_line)


PROFILES_WITH_NEWLINES = (
    VALID_CONFIG.replace("[profiles.claude-main]", '[profiles."claude\\nmain"]')
    + '\n[profiles."claude\\nspare"]\nprovider = "claude"\n'
)
APPS_WITH_NEWLINES = VALID_CONFIG.replace("[apps.songmaker]", '[apps."song\\nmaker"]')
SECOND_APP_ON_THE_SAME_CLIENT = (
    '\n[apps."pres\\nentator"]\nworkload_client = "songmaker-test"\npurposes = []\n'
)


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        pytest.param(
            PROFILES_WITH_NEWLINES,
            "profiles.claude\\nspare.provider: claude already has profile 'claude\\nmain'",
            id="second-profile-for-a-provider",
        ),
        pytest.param(
            APPS_WITH_NEWLINES.replace(
                'purposes = ["songmaker.cowriter"]', 'purposes = ["no\\npurpose"]'
            ),
            "apps.song\\nmaker.purposes: unknown purpose 'no\\npurpose'",
            id="unknown-purpose",
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
                "purposes = []\n" + SONGMAKER_ORIGIN_LINE
            ),
            "apps.pres\\nentator.mcp_origin: https://songmaker.internal is already"
            " the MCP origin of app 'song\\nmaker'",
            id="origin-of-two-apps",
        ),
        pytest.param(
            _tuned_app_config(LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT + 1).replace(
                "[apps.songmaker]", '[apps."song\\nmaker"]'
            ),
            "apps.song\\nmaker.tools: the capability for purpose 'songmaker.cowriter'"
            " would be 8196 characters; the limit is 8192",
            id="capability-too-large",
        ),
    ],
)
def test_a_name_with_a_newline_is_shown_escaped_once_on_one_line(tmp_path, capsys, text, refusal):
    assert _refused_on_one_line(_written(tmp_path, text), capsys) == refusal


def test_an_origin_without_a_signing_section_stops_startup_naming_the_field(tmp_path, capsys):
    config_file = _written(tmp_path, _with(SIGNING_SECTION, ""))

    assert _refused_on_one_line(config_file, capsys) == (
        "apps.songmaker.mcp_origin: needs a [signing] section,"
        " because the app's runs carry a signed capability"
    )


def test_an_app_whose_capability_is_exactly_8192_characters_at_the_preflight_loads(tmp_path):
    load_config(_written(tmp_path, _tuned_app_config()))


@pytest.mark.parametrize(
    ("text", "purpose"),
    [
        pytest.param(
            _tuned_app_config(LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT + 1),
            "songmaker.cowriter",
            id="one-more-character-in-a-tool-name",
        ),
        pytest.param(
            (
                _tuned_app_config()
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
            _tuned_app_config().replace("agent-test", "agent-testx"),
            "songmaker.cowriter",
            id="an-issuer-one-character-longer",
        ),
    ],
)
def test_an_app_whose_capability_would_be_8193_characters_stops_startup_naming_its_tools(
    tmp_path, capsys, text, purpose
):
    config_file = _written(tmp_path, text)

    assert _refused_on_one_line(config_file, capsys) == (
        f"apps.songmaker.tools: the capability for purpose {purpose!r}"
        " would be 8193 characters; the limit is 8192"
    )


def test_an_oversized_capability_refusal_holds_no_token_anywhere(tmp_path, monkeypatch):
    signed = record_signed_tokens(monkeypatch)
    config_file = _written(tmp_path, _tuned_app_config(LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT + 1))

    with pytest.raises(ConfigError) as refusal:
        load_config(config_file)

    assert signed
    assert not reaches_text(everything_a_refusal_holds(refusal.value), signed)


CANARY = "canary-token-value-7f3a91"
API_KEY_SHAPE = "sk-ant-" + "api03-" + CANARY
WHITESPACE_REASON = "holds whitespace; a setup token holds none"
TOKEN_FILE_FIELD = "profiles.claude-main.token_file"


def _token_file_with(tmp_path: Path, contents: bytes, mode: int = 0o600) -> Path:
    path = tmp_path / "claude.token"
    path.write_bytes(contents)
    path.chmod(mode)
    return path


def _token_config(token_file: str) -> str:
    return _without_tools_origin_and_signing().replace(
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
def test_a_sound_token_file_is_read_with_its_trailing_newline_dropped(tmp_path, trailing):
    token_file = _token_file_with(tmp_path, (CANARY + trailing).encode(), 0o400)

    config = load_config(_written(tmp_path, _token_config(str(token_file))))

    assert config.profiles["claude-main"].token.get_secret_value() == CANARY


def test_a_loaded_token_never_shows_in_the_repr_or_text_of_the_configuration(tmp_path):
    token_file = _token_file_with(tmp_path, CANARY.encode())

    config = load_config(_written(tmp_path, _token_config(str(token_file))))

    assert not reaches_text([repr(config), str(config), repr(config.profiles)], [CANARY])
    assert CANARY not in config.model_dump_json()


def test_a_path_swapped_after_the_open_still_loads_the_opened_token_file(tmp_path, monkeypatch):
    token_file = _token_file_with(tmp_path, CANARY.encode())
    real_open = os.open

    def open_then_swap(path, flags, *arguments):
        descriptor = real_open(path, flags, *arguments)
        impostor = tmp_path / "impostor.token"
        impostor.write_bytes(b"impostor")
        impostor.chmod(0o600)
        impostor.rename(token_file)
        return descriptor

    monkeypatch.setattr(os, "open", open_then_swap)

    config = load_config(_written(tmp_path, _token_config(str(token_file))))

    assert config.profiles["claude-main"].token.get_secret_value() == CANARY


def test_the_token_file_is_read_once_so_replacing_it_afterwards_changes_nothing(tmp_path):
    token_file = _token_file_with(tmp_path, CANARY.encode())
    config = load_config(_written(tmp_path, _token_config(str(token_file))))

    token_file.write_bytes(b"replacement")

    assert config.profiles["claude-main"].token.get_secret_value() == CANARY


PROFILE_TABLE = '[profiles.claude-main]\nprovider = "claude"\n' + TOKEN_FILE_LINE + "\n\n"
NO_PROFILE = _with(PROFILE_TABLE, "")
WITHOUT_TOKEN_FILE = _with(TOKEN_FILE_LINE + "\n", "")
WITHOUT_ORIGIN = _with(SONGMAKER_ORIGIN_LINE, "")
SECOND_APP_LISTING_TOOLS_WITHOUT_ORIGIN = (
    VALID_CONFIG
    + '\n[apps.presentator]\nworkload_client = "presentator-test"\npurposes = []\n'
    + 'tools = { read_song = "read_only" }\n'
)


@pytest.fixture
def served_apps(monkeypatch):
    served = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **where: served.append((app, where)))
    return served


def _with_profile_of(provider: str, name: str = '"codex-main"', token: bool = False) -> str:
    extra = TOKEN_FILE_LINE + "\n" if token else ""
    return NO_PROFILE.replace(
        "[purposes", f'[profiles.{name}]\nprovider = "{provider}"\n{extra}\n[purposes', 1
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            _without_tools_origin_and_signing().replace(PROFILE_TABLE, ""), id="no-profile"
        ),
        pytest.param(VALID_CONFIG, id="one-claude-profile-with-its-token"),
    ],
)
def test_a_document_with_no_profile_or_one_sound_claude_profile_reaches_serving(
    tmp_path, served_apps, text
):
    assert main(["--config", str(_written(tmp_path, text))]) == 0

    assert [where for _app, where in served_apps] == [{"host": "127.0.0.1", "port": 8099}]


def test_an_installation_without_a_profile_has_none(tmp_path):
    config = load_config(_written(tmp_path, NO_PROFILE))

    assert config.profiles == {}


def test_an_api_only_installation_with_tools_an_origin_and_signing_loads(tmp_path):
    config = load_config(_written(tmp_path, NO_PROFILE.replace('cli_provider = "claude"\n', "")))

    assert config.profiles == {}
    assert config.capability_scope("songmaker") is not None


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        pytest.param(
            WITHOUT_TOKEN_FILE,
            "profiles.claude-main.token_file: required for a claude profile",
            id="claude-without-a-token-file",
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
        pytest.param(
            _with('provider = "claude"', 'provider = "gemini"'),
            "profiles.claude-main.provider: Input should be",
            id="unknown-provider",
        ),
        pytest.param(
            _with(TOKEN_FILE_LINE, 'token_file = ""'),
            "profiles.claude-main.token_file:",
            id="empty-token-file-path",
        ),
    ],
)
def test_a_profile_the_service_cannot_serve_stops_startup_naming_the_field(
    tmp_path, capsys, text, refusal
):
    assert _refused_on_one_line(_written(tmp_path, text), capsys).startswith(refusal)


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        pytest.param(
            WITHOUT_ORIGIN,
            "apps.songmaker.mcp_origin: required because the app lists tools",
            id="with-a-profile",
        ),
        pytest.param(
            _with(PROFILE_TABLE, "").replace(SONGMAKER_ORIGIN_LINE, ""),
            "apps.songmaker.mcp_origin: required because the app lists tools",
            id="without-a-profile",
        ),
        pytest.param(
            _with('cli_provider = "claude"\n', "").replace(SONGMAKER_ORIGIN_LINE, ""),
            "apps.songmaker.mcp_origin: required because the app lists tools",
            id="purposes-with-only-an-api-alias",
        ),
        pytest.param(
            SECOND_APP_LISTING_TOOLS_WITHOUT_ORIGIN,
            "apps.presentator.mcp_origin: required because the app lists tools",
            id="only-the-second-app",
        ),
    ],
)
def test_an_app_that_lists_tools_without_an_origin_stops_startup_naming_the_field(
    tmp_path, capsys, text, refusal
):
    assert _refused_on_one_line(_written(tmp_path, text), capsys) == refusal


@pytest.mark.parametrize(
    "app_without_tools_rule",
    [
        pytest.param(WITHOUT_ORIGIN.replace(TOOLS_LINE, "tools = {}"), id="empty-tools-table"),
        pytest.param(WITHOUT_ORIGIN.replace(TOOLS_LINE + "\n", ""), id="no-tools-key"),
    ],
)
def test_an_app_that_lists_no_tools_needs_no_origin(tmp_path, app_without_tools_rule):
    config = load_config(_written(tmp_path, app_without_tools_rule))

    assert config.apps["songmaker"].mcp_origin is None


CONTROL_CHARACTERS = {"carriage-return": "\\r", "escape": "\\u001b", "newline": "\\n"}


@pytest.mark.parametrize("control", CONTROL_CHARACTERS.values(), ids=CONTROL_CHARACTERS)
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
            lambda name: WITHOUT_ORIGIN.replace("[apps.songmaker]", f"[apps.{name}]"),
            "mcp_origin: required because the app lists tools",
            id="mcp-origin",
        ),
    ],
)
def test_a_name_with_a_control_character_is_refused_on_one_line_with_no_raw_control_character(
    tmp_path, capsys, arrange, field, control
):
    name = f'"a{control}b"'
    refusal = _refused_on_one_line(_written(tmp_path, arrange(name)), capsys)

    assert field in refusal
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


def test_a_cli_section_is_refused_until_it_has_a_consumer(tmp_path, capsys):
    refusal = _refused_on_one_line(_written(tmp_path, VALID_CONFIG + "\n[cli]\n"), capsys)

    assert refusal == "cli: Extra inputs are not permitted"


def test_a_second_signing_table_is_refused_as_a_duplicate_table(tmp_path, capsys):
    text = VALID_CONFIG + f"\n{SIGNING_SECTION}"

    assert _refused_on_one_line(_written(tmp_path, text), capsys).startswith("not TOML")
