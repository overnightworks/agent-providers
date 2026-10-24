# Migrating

Read by a host bumping its pin; each release that breaks a host has one section, newest first.

## Migrating to 0.8.0

The service no longer replaces a requested model: `[apps.<app>.models]` is removed. An app
that kept a model per agent in the service configuration stores the choice itself and sends
`model` on every run.

| Before | Now |
| --- | --- |
| `[apps.<app>.models]`, a table of `<agent> = "<provider>/<model>"`, replaced the model of a run by its `purpose` | Removed. A configuration that still holds the table stops startup with exit 2, and the refusal names `apps.<app>.models`. |
| A run without `model` could take the table's model | A run without `model` takes the default of the subscription profile. |
| `[profiles.<name>]` held a provider and a token file | `background` is required, `true` or `false`. A configuration without it, or with a non-boolean, stops startup with exit 2 and the refusal names `profiles.<name>.background`; `true` also needs `[signing]`. Write `false` to keep today's behaviour. |

An app's change of choice takes effect on its next run, with no restart of the service. The wire
stays version 2: `RunRequest` keeps its shape.

`GET /offer` is new: it lists the models the asking person can run in this app, with the same
two live tokens as `POST /runs`, and `agent_runtime_client.wire.parse_offer` reads its body. Each entry
carries `background`. It needs no action ([wire.md](wire.md), "The offer").

Ticket runs are new: `POST /runs` and `GET /runs/{id}` also accept the app's workload token with a
job ticket in the `Agent-Job-Ticket` header instead of a user token, and refuse a request that sends
both. The wire stays version 2, and `POST /job-tickets` and `GET /offer` still take a user token only
([wire.md](wire.md), "Job tickets").

A Grok child no longer holds a byte copy of the login at `grok.token_file`. The Grok turn and the
Grok catalog probe each hand their child a re-serialized copy in which every member named
`refresh_token`, `refreshToken` or `refresh`, at any depth, is the empty string, so a child never
renews and never rotates the stored login; every other member keeps its value. A source that is
not UTF-8 JSON, or whose top level is not a JSON object, has no copy: a Grok turn then refuses
with `ProviderUnavailableError` for `grok`/`cli` and the reason `CLI_LOGIN_NOT_CONFIGURED`, and
the catalog probe reports Grok logged out and logs a warning. Neither shows the file's content.
A host whose `auth.json` is a JSON object needs no action; one that stores the login in another
shape fixes the file.

## Migrating to 0.7.0

A host migrates at its pin bump to 0.7.0, by renaming. Nothing behaves differently: the wire
(`PROTOCOL_VERSION` 2), the configuration file and every identity below are the same bytes as
in 0.6.0, so a 0.6.0 app and a 0.7.0 service (or the reverse) still exchange the same
messages. The wheel URLs are now under
`https://github.com/overnightworks/agent-runtime/releases/download/v0.7.0/`.

| Before | Now |
|---|---|
| distribution `overnightworks-agent-providers` | `overnightworks-agent-runtime` |
| distribution `overnightworks-agent-client` | `overnightworks-agent-runtime-client` |
| distribution `overnightworks-agent-service` | `overnightworks-agent-runtime-service` |
| import `agent_providers` | `agent_runtime` |
| import `agent_client` | `agent_runtime_client` |
| import `agent_service` | `agent_runtime_service` |
| wheel files `overnightworks_agent_providers-…`, `…_agent_client-…`, `…_agent_service-…` | `overnightworks_agent_runtime-…`, `…_agent_runtime_client-…`, `…_agent_runtime_service-…` |
| command `agent-service --config PATH` | `agent-runtime-service --config PATH`; a refusal starts with `agent-runtime-service: refusing to start:` |
| extras such as `overnightworks-agent-client[mcp-server]` | the same extras on the new names |

Logger names follow the imports, because they are `__name__`: `agent_client.mcp_server` is now
`agent_runtime_client.mcp_server`, and a log filter or handler keyed on an old name moves with
it.

What stays, because peers and tokens carry it: the capability issuer `agent-service`, the MCP
`serverInfo` and `clientInfo` name `agent-service`, the Keycloak client and audience
`agent-service`, and every service configuration key and table, so a service configuration
loads unchanged.

Per consumer:

- edge: the lock writer, the image tests, the probe anchor and the `ENTRYPOINT` name the new
  distributions, imports and command.
- presentator: the pin, the imports and the import-linter contracts.
- songmaker: at its next bump, the distribution, the import and the wheel URL.

## Migrating to 0.6.0

A host migrates at its pin bump to 0.6.0. Prepare the realm first: give every user the
`/apps/<app>` membership (and `/apps/<app>/admin` for admins) the app gate below requires.
Then switch the service and every app to 0.6.0 together, because a 0.5.0 app and a 0.6.0
service refuse each other (wire v1 and v2).

`agent-client` gains the agent file loader `agent_client.agents` and, with it, the base
dependency `ruamel.yaml>=0.19,<0.20`: a host that pins the client's dependencies itself adds
`ruamel.yaml>=0.19,<0.20`. One addition needs no action: the optional extra
`agent-client[mcp-server]` (PyJWT with cryptography) brings `agent_client.mcp_server`, which
verifies a run capability and accepts only an RSA key of `capability.SIGNING_KEY_BITS`
(3072) bits; no base module imports it.

The per-app model override `[apps.<app>.models]` below exists in 0.6.0 and 0.7.0; [0.8.0](#migrating-to-080) removes it again, so a host upgrading to 0.8.0 sets none up.

| Before | Now |
|---|---|
| `[identity] subscription_group`, one string | `subscription_groups`, a non-empty array of full group paths, each beginning with `/` as in the token's group claim (`"members"` is refused; write `"/members"` if that is the token's group); write `["/ki-abo", "/ai-subscription"]` while tokens still carry the old name, and drop the old entry once they expire; a leftover `subscription_group` stops startup naming it |
| `[purposes.<purpose>]` tables (`cli_provider`, `api_alias`) and `[apps.<app>] purposes` | removed; a leftover `purposes` or `apps.<app>.purposes` stops startup naming the field. Keycloak decides the route through the caller's groups: a subscription group selects the CLI route, and an API group selects the paid route (both for members of the app, see the app-gate row below) |
| (absent) | `[identity] api_groups` (full group paths beginning with `/`, like `subscription_groups`) and `[route] api_alias`, optional and set together; without them nobody reaches the paid route |
| `RunRequest.purpose`, any non-empty text the app's `purposes` list names | `agent_client.wire.Purpose`: `[a-z][a-z0-9-]{0,63}`, matched against the whole string; anything else answers 422 `invalid_request`. Any valid purpose from any registered app is admitted; it labels the run and its capability, and keys the app's per-agent model override (`[apps.<app>.models]`, below); it chooses no route itself |
| wire version 1: `PROTOCOL_VERSION = 1`, `RunRequest` of `v`, `purpose` and `turn` | wire version 2: `PROTOCOL_VERSION = 2` for the request, every stream event, `RunRejection` and `RunStatus`; a body with `v: 1` answers 400 `unsupported_version`, and a v2 `RunStream` reading a v1 frame reports `unsupported_version`. Send and read `v: 2` |
| (absent) | `RunRequest.tools`, required and possibly `{}`: a map from each tool name to its `ToolEffect`. The service signs exactly those names, sorted, into the run's capability and stamps the effects onto the run's tool calls; a call outside the map is side-effecting. An app without an `mcp_origin` that names any tool answers 403 `forbidden`. `AgentDefinition.request` builds it from the agent's tools |
| (absent) | `RunRequest.model`, optional: `<provider>/<model>` (`agent_client.wire.ModelName`, such as `claude/sonnet`, no version); absent or `null` means no model, anything malformed answers 422 `invalid_request`. `AgentDefinition.request` carries the agent file's `model` |
| `[cli.claude]` of `binary` and `version` | also the required `models`, an array of the model names this installation serves on the subscription (possibly empty); a missing key, a string or an entry outside `[a-z0-9.-]+` stops startup naming `cli.claude.models` |
| a request's route decided by the caller's groups and the app's purpose table | the route reads the run's resolved model: the agent's override from `[apps.<app>.models]` (below) when one exists, else the request's `model`. A resolved model takes the CLI route when the subscription lists exactly that model and the caller has subscription rights, otherwise the paid API route for a caller with API rights, otherwise 403 `not_enabled`; a run with no resolved model routes by the groups alone, as the `subscription_groups` and `api_groups` rows above describe. Apart from the app's own override nothing substitutes the model, and an override grants no right |
| (absent) | `[apps.<app>.models]`, optional: `<agent> = "<provider>/<model>"`, the agent being the `purpose` of its runs (`agent_client.wire.Purpose`) and the value an `agent_client.wire.ModelName`. A name or value outside those grammars stops startup naming `apps.<app>.models.<agent>`, and a `models` that is not a table stops it naming `apps.<app>.models`, in both cases before any secret file is read. A restart applies a change; nothing checks that the app has an agent of that name, and an override for one it never sends matches nothing |
| `[apps.<app>] tools` table (the tools a capability permits, with their effects) | removed; a leftover table stops startup naming `apps.<app>.tools`. The app names its tools in each request; `AppConfig.tools`, `granted_tools` and `tool_effect`, and the startup size check with its `preflight_claims`, are gone, and the 8,192-character check of the actual token (422 `capability_too_large`) is the only size check |
| `agent_client.mcp_wire.object_without_duplicate_members` | removed, no alias remains; `agent_client.mcp_wire.read_strict_json(text)` reads JSON that repeats no member and rejects the literal tokens `NaN`, `Infinity` and `-Infinity` (a number that overflows, such as `1e999`, reads as `inf`), and raises only `ValueError` |
| A signed tool the app's `tools/list` does not offer is dropped from the run | the run ends as `failed: provider_error` before any model execution; an app's `tools/list` must cover every name its requests are signed for. An offered tool that is not signed stays out of the run, as before |
| `ClaudeRun(binary, token, system, max_tool_calls, history, *, relay, tool_timeout_seconds)` | the same with the required keyword `model: str \| None`: `None` runs the CLI's own default model and passes no option, a name runs as the one argument `--model=<name>`; a host that builds a `ClaudeRun` without `model=` gets a `TypeError` |
| `overnightworks-webauth[jwt]` v0.15.0 (agent-service) | v0.20.1 without `[jwt]`, for a host that pins the service's dependencies itself. A token the issuer signed is read as Python's JSON reader reads it: a repeated member keeps its last value, `jku`, `jwk`, `x5u` and `x5c` header members are ignored, deep nesting and non-finite numbers are accepted, `b64: true` (also `crit: ["b64"]`) is accepted where it was refused, and a padded signature segment is accepted. The encoded header segment may now be at most 512 characters, independently of the 8,192-character token limit: a correctly signed token with a longer header, accepted before, answers `401 unauthorized`, so an issuer must shorten its headers. The key is read before the signature and the signature before the payload, so a payload that is no JSON object answers 503 instead of 401 while no key set can be fetched. The key set is read the same way, and an unreachable issuer host with several addresses is waited for per address, outside the 10 s deadline |
| any member of a subscription group reaches every registered app's runs | every route and `GET /runs/{id}` also require membership of the app: one of the user token's groups is `/apps/<app>` or a path below it (segment-aware), else 403 `forbidden` before the body is read. A user token therefore needs the app group, and the realm needs `/apps/<app>/admin` for the apps' admins. An `[apps.<name>]` name that is not exactly one group-path segment (such as `team/notes`; `notes/team` is a descendant group, not an app name) stops startup naming `apps.<name>` |
| the API route needs the caller's exact group `/apps/<app>` | the app gate replaces that guard, so a descendant such as `/apps/notes/team` reaches the API |
| (absent) | `RunRequest.roles`, required: exactly `["admin"]` or `["admin", "user"]`, the roles the agent is offered to; a body without it, or with anything else, answers 422 `invalid_request`. The service answers 403 `forbidden` unless the person's role in the app (`admin` in `/apps/<app>/admin` or below, else `user`) is in it. `AgentDefinition.request` carries the file's `appRoles`, so a host using it changes nothing; a host building a body by hand adds `roles` |

## Migrating to 0.5.0

A host migrates at its pin bump to 0.5.0. The only row is the module move
below; nothing else a host imports or configures changed shape.

Two changes need no action. Every core Claude child (turns, the tool-surface
probe, `auth status` and `-p /model`) now starts with the run switches
(`constants.CLAUDE_RUN_SWITCHES`), and every core `-p` run passes
`--no-session-persistence`; [The child environment is
closed](host.md#the-child-environment-is-closed) says what that switches off. The
`image` extra now accepts `pillow>=12.3,<14`, so a host that pins Pillow 13
resolves against it.

| Before | Now |
|---|---|
| `agent_service.mcp_wire` | `agent_client.mcp_wire`, unchanged; no alias remains |

## Migrating to 0.4.0

A host migrates at its pin bump to 0.4.0; the configuration model refuses
unknown fields, so a host that still sets a removed name fails at startup and
the error names the field. The rows cover what a host imports or relies on from
the core (`agent_providers`) and the client (`agent_client`), and the agent
service's TOML configuration and observable run behaviour; the service's Python
modules (`agent_service.*`) are the program's own code, not a library contract,
and get no row. Two kinds of row name a service class or module:
`agent_service.secret_file` keeps a row because it moved into the core, and the
rows that name a service class describe its TOML or its observable run
behaviour.

| Before | Now |
|---|---|
| `claude_cli_binary` | `claude.binary` |
| `claude_cli_binary_search_globs` | `claude.binary_search_globs` |
| `claude_cli_auth_file` | `claude.token_file` |
| every core Claude turn copied `claude.token_file` into its home as `.claude/.credentials.json` | `claude.token_file` holds one line, the token `claude setup-token` prints: a regular file of at most 16 KiB that grants group and others nothing (for example mode 0600 or 0400), UTF-8, with no whitespace or control character in the token and no API key (`sk-ant-api…`). A line that opens `{` or `[`, or that parses as any other JSON value (a string, a number, `true`, `false` or `null`), is refused, a credentials file included. A turn gets the token only as `CLAUDE_CODE_OAUTH_TOKEN` and fails with `CLI_LOGIN_NOT_CONFIGURED` for a refused file. Write the token into the file before the pin bump |
| the Claude catalog copied `claude.token_file` into its home as `.claude/.credentials.json` for `auth status` and `-p /model`, and a linked, non-regular, unreadable or over-256-KiB file, or a copy that could not be written, stopped the listing with `CLI_LOGIN_NOT_CONFIGURED` | `auth status` gets the token only as `CLAUDE_CODE_OAUTH_TOKEN`; a refused file starts no child, is logged with its path and reason, and reads as not signed in. `-p /model` gets no login and reads no login file, so `catalog.list_provider_models` lists the aliases whatever the file holds |
| `constants.CLAUDE_CLI_CURRENT_MODEL_MARKER` | removed, with no replacement |
| `constants.CLAUDE_CLI_SIGTERM_GRACE_SECONDS` (1), the SIGTERM grace of an async Claude turn's reap | removed; the Claude pool's `ReapBounds.sigterm_grace_seconds` (`claude.provider.CLAUDE_CLI_PROCESS_POOL.reap`) reads the existing `constants.CLI_TERMINATION_GRACE_SECONDS`, the same 1 s |
| `constants.CLAUDE_CLI_ZOMBIE_REAP_TIMEOUT_SECONDS` (5), how long an async Claude turn waited for its killed leader's exit before a background reaper took it | removed, with no renamed twin: `constants.CLI_GROUP_CONFIRMATION_SECONDS` (5) bounds, from the first SIGKILL, the count that must find the group empty, and then the wait for the leader's published exit; the Claude pool reads it as `ReapBounds.confirmation_seconds` |
| `claude.provider.ClaudeCliModelCatalog.current_model_label` and `catalog.ProviderRouteSnapshot.default_model_label` held the Claude CLI's stated default | both fields stay and are always `None`; show no Claude default |
| `grok_cli_binary` | `grok.binary` |
| `grok_cli_auth_file` | `grok.token_file`, still a full `auth.json` copy |
| `turns.cli_prompt_file_prefix` | `turns.grok.prompt_file_prefix` |
| `turns.cli_prompt_file_placeholder` | `turns.grok.prompt_file_placeholder` |
| `codex_cli_binary` | `codex.binary` |
| `codex_cli_auth_file` | `codex.token_file`, still an `auth.json` whose per-turn copy is blanked |
| `turns.codex_code_mode_host_binary` | `turns.codex.code_mode_host_binary` |
| `turns.codex_resources_directory` | `turns.codex.resources_directory` |
| `turns.codex_max_concurrent_processes` | `turns.codex.max_concurrent_processes` |
| `turns.codex_max_concurrent_image_runs` | `turns.codex.max_concurrent_image_runs` |
| `dispatch.stream_turn` and `dispatch.call_provider_once` raised `ProviderUnavailableError` with `ROUTE_FAILED` when `turns` or the configuration itself was missing; every provider's fields were required, so no provider could be missing | both raise `ProviderNotConfiguredError(provider, block)` for a provider or turn block left out, and `TurnRuntimeNotConfiguredError` or `ProviderRuntimeNotConfiguredError` unchanged, all `config.HostWiringError`s; a host that caught `ProviderUnavailableError` for a wiring fault catches `HostWiringError` too |
| `dispatch.image_capability` for the Codex CLI route always answered with the Codex fields set | it raises `ProviderNotConfiguredError("codex", block)` when `codex` or `turns.codex` is missing |
| `catalog.probe_provider_route`, `catalog.list_provider_models` and `catalog.provider_configuration` never met a missing provider, since every provider's fields were required | a provider whose block (`claude`, `grok` or `codex`) is `None` raises `ProviderNotConfiguredError(provider, provider)`, a `config.HostWiringError`, before any child starts: from `catalog.probe_provider_route` and `catalog.list_provider_models` on its CLI route, from `catalog.provider_configuration` without an API key, and from the login probes (`process.grok_cli_status`, `process.codex_cli_login`, `process.codex_cli_login_is_usable`, `process.codex_cli_model_catalog`). It is never a not-ready snapshot or `ProviderNotLoggedIn`. With an API key set, `catalog.provider_configuration` answers `ProviderReady` with `API_KEY` before it probes the CLI (`ProviderCapabilityMissing` for Claude without the `[api]` extra), so it needs no block and starts no login probe. Ask for readiness only of a provider you configured, or catch `HostWiringError` |
| `grok.transport.GrokCliToolTransport` and `codex.transport.CodexCliToolTransport` were constructed without `turns` | constructing one raises `TurnRuntimeNotConfiguredError` without `turns`, `ProviderNotConfiguredError("grok", "turns.grok")` or `ProviderNotConfiguredError("codex", "turns.codex")` without its turn block, and `ProviderNotConfiguredError(provider, provider)` without its provider block, before any home is made. A test that replaces the transport is unaffected |
| `process.run_claude_catalog_cli` copied the configured credential file (`claude_cli_auth_file` at 0.3.0, `claude.token_file` now) into the child's home | it runs in an empty home. A caller that needs authentication passes `login=process.claude_token_environment()`, which puts the setup token only in the environment. `/model` needs none |
| `process.run_cli_bounded` read the Grok prompt-file prefix itself | the caller passes `prompt_file_prefix` together with `prompt_file_bytes` and `prompt_file_arg_index`; the Grok transport passes `turns.grok.prompt_file_prefix` |
| `configure()` and `PrivateWorkingRoot(path)` accepted a root written with a `..` component | both raise `WorkingRootNotPrivateError` with `WorkingRootViolation.PARENT_COMPONENT` (`"parent_component"`); write the root as its resolved path |
| `McpServerSpec(config_file_prefix=...)` and `GrokTurnRuntime(prompt_file_prefix=...)` accepted any non-empty string, an absolute path, one with `/`, a space or a non-ASCII letter included | both raise pydantic's `ValidationError` naming the field, without the value in its rendered text, unless the prefix is 1 to 64 characters from `A-Z a-z 0-9 . _ -` (a bare `.` or `..` stays accepted). Use a plain stem such as `notes-mcp-`; the file is created in `cli_working_directory_root` whatever the old value pointed to. `TurnRuntimeConfig` and `ProviderRuntimeConfig` errors also no longer display the rejected input payload of any field (its location stays). No known host uses a refused value. |
| `process.PrivateCredentialHomeReservation`, with `release()`, `on_reaped(pid, became_zombie)` and `on_spawn_failed()` | `process.PrivateHomeReservation`, the reservation of the base `PrivateHome`, with `release()` only; no alias remains. Where you passed `on_reaped=reservation.on_reaped, on_spawn_failed=reservation.on_spawn_failed` to `process.run_cli_bounded`, pass `**process.released_when_the_runner_ends(reservation)`, which supplies both |
| `agent_service.secret_file` (`read_secret_file`, `read_token_file`, `shown_text`, `UnsoundSecretFile`) | `agent_providers.secret_file`, the same names; the service module is gone, so a host that imported it changes the import |
| `agent_service.config.ProfileConfig.token`, a `SecretStr` read from `token_file` when the configuration loaded | `ProfileConfig.token_file`, the checked `Path` (or `None`); the service reads the file at the start of every run, so a swapped file serves the next run without a restart and a file gone bad fails that run with `provider_error` |
| `spawn.open_pipes(child, stderr=)` returned a `subprocess.Popen` (`spawn.PipedProcess`) | `spawn.open_blocking_anchored_pipes(child, stderr=)` returns a `BlockingAnchoredChild`, with no `wait`, `poll` or `communicate`: wait with `wait_for_exit(timeout)`, signal the group with `signal_group(sig)` or `kill_until_gone(within=)`, and reap only with `release()` once the exit is published; never reap its pid yourself (no known host calls it) |
| `spawn.open_async_pipes(child, stream_buffer_limit=)` returned an asyncio `Process` (`spawn.AsyncPipedProcess`) | `spawn.open_anchored_pipes(child, stream_buffer_limit=)` returns an `AnchoredChild`, with no `wait`: await `exited()` for the code once the exit is published, signal the group with `signal_group(sig)` or `kill_until_gone(within=)`, and end with an explicit `release()` once the exit is published; never reap its pid yourself (no known host calls it) |
| `repr(spawn.ChildProcess(...))` showed its `environment` | the `repr` leaves `environment` out, since it can hold a token; read `child.environment` where you need it |
| `process.cli_settle_deadline(deadline)` was the deadline plus 2.1 s | plus 11.1 s, `process.cli_settle_margin_seconds()`; `CLI_PROBE_CALLER_TIMEOUT_SECONDS` grows by 9 s |
| `on_reaped(pid, True)` also followed a background reap whose group check failed | `on_reaped` follows only a confirmed count; an unconfirmed group keeps its reservation until the process exits |
| an async Claude turn on the CLI route whose leader had not exited 5 s after SIGKILL logged one ERROR, handed it to a background reaper and raised nothing for it | the turn raises `errors.ContainmentLost(group)`, a plain `Exception`, whenever its reap cannot show the group gone (see [Lost process groups](host.md#what-the-host-supplies)). `claude.adapter.stream_claude_turn` passes it unchanged; `dispatch.stream_turn` raises `ProviderUnavailableError` with `ROUTE_FAILED` whose `__cause__` is the `ContainmentLost`. Either way the pool slot and the home stay held, with nothing freeing them in the background, until `lost_groups.confirm_group_gone(group)` returns `True` or the process exits; call it until it does |
| `codex.protocol.run_reserved_codex_cli(...)` looked the process pool up itself | `codex.protocol.run_reserved_codex_cli(process_pool, reservation, home_reservation, child, *, stdin_payload, deadline, channel)` takes the `CodexProcessPool` that issued the reservation as its first argument; pass that pool, so one turn reserves and reaps on one reference (no known host calls it) |
| `claude.provider.acall_claude_with_mcp_stream` answered from its assistant text when the CLI ended with exit 0 and no result line | it raises `UnavailableError`, and `claude.adapter.stream_claude_turn` raises `ProviderUnavailableError` with `CLI_PROTOCOL_ERROR`. Handle the failure like any other CLI failure of the turn; no `FinalEvent` is synthesized, and previously streamed events may already have reached the caller |
| `claude.provider.acall_claude_with_mcp_stream` raised a bare `ValueError` for a CLI output line over its 4 MiB limit, a stderr read error escaped it, and the end of the turn waited for stderr to close | an over-long line raises `UnavailableError`, with no part of the line in the message, a stderr read error no longer fails the turn, and the end of the turn no longer waits for stderr to close. Catch `UnavailableError` (or let `stream_claude_turn` translate it) and drop any `ValueError` handler written for this |
| `claude.provider.acall_claude_with_mcp_stream` parsed each CLI line with `json.loads` on its bytes: a UTF-16 or UTF-32 line was accepted, and a line that was not UTF-8, nested too deep or held an over-long integer, or an event of the wrong shape, escaped the stream as `UnicodeDecodeError`, `RecursionError`, `ValueError`, `AttributeError`, `TypeError` or pydantic's `ValidationError` | each line is decoded as UTF-8 only. A line that is not UTF-8 (a UTF-16 or UTF-32 line included), nests too deep or holds an over-long integer is skipped with a WARNING naming the error class and the length, never the text, as an invalid JSON line already was. An event of the wrong shape raises `UnavailableError`, which `claude.adapter.stream_claude_turn` maps to `ProviderUnavailableError` with `CLI_PROTOCOL_ERROR`. Catch `UnavailableError` where you caught those exceptions |

`[runs] max_duration_s` is refused above 2,147,483 seconds (about 24.8 days), a conservative whole-second limit under the CLI's tool timer; the startup error names `runs.max_duration_s`.

The agent service's configuration (exit 2 naming the field until migrated). A `[cli]` section now installs the CLI runner, which starts real Claude processes; at 0.3.0 CLI runs answered `provider_unavailable`.

| Before | Now |
|---|---|
| `[identity] audience`, `subscription_group` and `group_claim` defaulted to `"agent-service"`, `"/ki-abo"` and `"groups"` | all three required, with no default; to keep the old behaviour, write `audience = "agent-service"`, `subscription_group = "/ki-abo"` and `group_claim = "groups"` |
| `[cli] claude_binary` | `[cli.claude] binary` |
| `[cli] claude_version` | `[cli.claude] version` |
| (absent) | `[cli] working_root`, required, a private directory (mode 0700, owned by the service user, not a link, written without `..`) |
| (absent) | `[mcp] ca_file`, required once any app declares `mcp_origin`: an absolute path to the PEM bundle of the CA that signs the apps' MCP origins; the service trusts no other CA for them |

### Dependencies

| Before | Now |
|---|---|
| `uvicorn>=0.30,<1` (agent-service) | `uvicorn>=0.32.1,<1`; uvicorn 0.30.0 to 0.32.0 never tell a streaming app that its client left, so a run would not stop when the client does; a host on an older uvicorn upgrades it with the pin |
| `anyio>=4,<5` (core and agent-service) | `anyio>=4.12,<5`; a host on an older anyio upgrades it with the pin |
| `overnightworks-webauth[jwt]` v0.13.0 (agent-service) | v0.15.0; a workload token without a `kid`, refused with 401 before, is now checked against the key set's only RS256 key and admitted when it verifies, and still gets 401 when the set holds more than one RS256 key |

## Migrating to 0.3.0

A host migrates at its pin bump to 0.3.0; both configuration models refuse unknown fields.

### Configuration

| Before (`ProviderRuntimeConfig`) | Now |
|---|---|
| `claude_chat_model` | removed: a turn passes its model, or `None` for the Claude CLI's own default; a host that still sets `turns.claude_chat_model` fails at startup |
| `codex_code_mode_host_binary` | `turns.codex_code_mode_host_binary` |
| `codex_resources_directory` | `turns.codex_resources_directory` |
| `codex_max_concurrent_processes` | `turns.codex_max_concurrent_processes` |
| `codex_max_concurrent_image_runs` | `turns.codex_max_concurrent_image_runs` |
| `cli_prompt_file_prefix` | `turns.cli_prompt_file_prefix` |
| `cli_prompt_file_placeholder` | `turns.cli_prompt_file_placeholder` |
| `mcp_server` | `turns.mcp_server` |
| `secret_env_keys` | removed: a child gets a closed environment, so there is nothing to scrub |
| `grok_cli_session_root` | removed: a turn's Grok home is a private directory below `cli_working_directory_root` |
| (absent) | `cli_binary_search_path`: new and required |
| (absent) | `claude_cli_auth_file`: new and required, the file copied read-only into each Claude turn's private home |

### Dependencies

| Before | Now |
|---|---|
| `pydantic>=2,<3` (core and client) | `pydantic>=2.7,<3`; a host on an older pydantic upgrades it with the pin |

### Names and values

| Before | Now | Host action |
|---|---|---|
| `dispatch.stream_cowriter_turn` | `dispatch.stream_turn` | Rename the import. |
| `dispatch.CoverImageCapability` | `dispatch.ImageCapability` | Rename. |
| `dispatch.cover_image_capability` | `dispatch.image_capability` | Rename. |
| `codex.image.generate_codex_cover_image` | `codex.image.generate_codex_image` | Rename the import and every patch of an imported binding. |
| `codex.image.codex_cover_image_capability_is_available` (also patched as `dispatch.codex_cover_image_capability_is_available`) | `codex.image.codex_image_turn_is_available` | Rename the import and the patch targets. |
| `constants.COWRITER_CLI_TIMEOUT_SECONDS` | `constants.TURN_TIMEOUT_SECONDS` | Rename. |
| `constants.COWRITER_MODELS_TIMEOUT_SECONDS` | `constants.PROBE_TIMEOUT_SECONDS` | Rename; patch `agent_providers.process.PROBE_TIMEOUT_SECONDS`. |
| `constants.COWRITER_CLAUDE_API_MAX_TOKENS` | `constants.CLAUDE_API_MAX_TOKENS` | Rename. |
| `constants.COWRITER_GROK_CLI_LINE_CHANNEL_CAPACITY` | `constants.GROK_CLI_LINE_CHANNEL_CAPACITY` | Rename. |
| `constants.COWRITER_GROK_CHAT_URL` | `constants.GROK_CHAT_URL` | Rename. |
| `constants.COWRITER_GROK_MODELS_URL` | `constants.GROK_MODELS_URL` | Rename. |
| `constants.COWRITER_OPENAI_CHAT_URL` | `constants.OPENAI_CHAT_URL` | Rename. |
| `constants.COWRITER_OPENAI_MODELS_URL` | `constants.OPENAI_MODELS_URL` | Rename. |
| `constants.COWRITER_ANTHROPIC_MODELS_URL` | `constants.ANTHROPIC_MODELS_URL` | Rename. |
| `constants.COWRITER_GROK_MODEL_PREFIX` | `constants.GROK_MODEL_PREFIX` | Rename. |
| `constants.COWRITER_GROK_NON_CHAT_MARKERS` | `constants.GROK_NON_CHAT_MARKERS` | Rename. |
| `constants.COWRITER_OPENAI_CHAT_PREFIXES` | `constants.OPENAI_CHAT_PREFIXES` | Rename. |
| `constants.COWRITER_OPENAI_NON_CHAT_MARKERS` | `constants.OPENAI_NON_CHAT_MARKERS` | Rename. |
| `constants.COWRITER_CLAUDE_MODEL_PREFIX` | `constants.CLAUDE_MODEL_PREFIX` | Rename. |
| `constants.COWRITER_CLAUDE_CLI_MODEL_LIST_MARKER` | `constants.CLAUDE_CLI_MODEL_LIST_MARKER` | Rename. |
| `constants.JUDGE_FAILURE_TIMEOUT` = `"judge_timeout"` | `constants.DEADLINE_EXCEEDED_REASON` = `"deadline_exceeded"` | Update `jobs/_runtime.py` and `scoring/lyrical_coherence.py` together to use `DEADLINE_EXCEEDED_REASON`; new timeout jobs record `deadline_exceeded`, while existing stored job errors remain unchanged. No historical data rewrite is part of this release. |
| `process.codex_cli_access_token_is_present` (also patched as `dispatch.codex_cli_access_token_is_present`) | `process.codex_cli_login_is_usable` | Rename the import and the patch target. It now also returns `False` for a login without an id token, account id, auth mode or refresh time, and raises for a wrong-typed field, so `dispatch.image_capability` names `CLI_LOGIN_NOT_CONFIGURED` for an incomplete login. A symbolic link, a FIFO, a directory, a device or a file over 256 KiB as the auth file is refused. |
| `codex.protocol.copy_codex_login_mirror` | removed | Delete any call. Each Codex turn installs its own blanked login copy. Both Codex turns now refuse a symbolic link, a file that is not a regular file (a FIFO, a directory, a device) or a file over 256 KiB as `codex_cli_auth_file`. No known host imports it. |
| `codex.protocol.CodexLoginMirrorError` | removed | Catch what the turns already raised: `codex.image.CodexImageLoginError`, and `ProviderUnavailableError` with `CLI_AUTH_REJECTED`. |
| A Claude CLI turn whose login the CLI refused (`authentication_failed`), which reached `ProviderUnavailableError` as `CLI_PROTOCOL_ERROR` or as answer text | `ProviderUnavailableError` with `CLI_AUTH_REJECTED`, caused by `claude.provider.CliLoginRefusedError` (an `UnavailableError`) | Handle the code as for the other CLI logins. A stream message now yields one event per text, tool-use and tool-result block. An `is_api_error_message` assistant message of any kind now raises before its text is yielded, even at exit 0. A `result` with `is_error` raises `UnavailableError` with a fixed message on both paths, the stream (no `FinalEvent`) and the non-stream turn (`call_claude`, `acall_claude` and the MCP call), which used to return the error text as the answer. The no-result fallback answer now also includes text blocks that follow a `tool_use` in the same message. |
| `tool_loop.COWRITER_MAX_TOOL_ROUNDS` | `tool_loop.MAX_TOOL_ROUNDS` | Rename the import and the patch target. |
| `constants.COWRITER_PROVIDERS` | `constants.ProviderName`, a `StrEnum` with `CLAUDE`, `GROK`, `CODEX` | A member equals its value string, so `"claude" in ProviderName` is true and `"bob" in ProviderName` is false without raising; `frozenset(ProviderName)`, `sorted(...)`, f-strings, `json.dumps` and `str`-keyed dict lookups behave as with the old set. The members are `str` subclasses, so `repr(...)` shows the member and `type(x) is str` is false; `ProviderModelCatalogUnavailableError.provider` may now be a member, so compare by value, not by type. Iteration order is claude, grok, codex. A host contract that reads constants by AST must also read a class definition. |
| The tool messages `Co-Writer tool failed.`, `Co-Writer tool response was invalid.`, `Co-Writer tool-call limit was reached.` | `Tool failed.`, `Tool response was invalid.`, `Tool-call limit was reached.` | Map the `code` for product wording. `Tool response was invalid.` is also the `str()` of `TextToolCallRejectedError`. The other twelve messages are unchanged. |
| The catalog error `Unknown co-writer provider '<name>'` | `Unknown provider '<name>'` | Update any text match; the exception types are unchanged. |
| `ToolProtocolMarkup()` defaults `songmaker` / `Songmaker`; `ToolCatalog.markup` optional | Both fields and `ToolCatalog.markup` required | Declare `markup=ToolProtocolMarkup(tag_namespace="songmaker", product_name="Songmaker")`; the rendered prompt stays byte-identical. |
| `sandbox.paths.CODEX_IMAGE_TURN_DIRECTORY_PREFIX` = `"songmaker-cover-codex-"` | `sandbox.paths.CODEX_IMAGE_TURN_DIRECTORY_PREFIX` = `"agent-cli-codex-image-turn-"` | The kernel profile permits `<root>/agent-cli-codex-image-turn-*/codex-home/`, where `<root>` is the resolved `cli_working_directory_root`. Load it with the pin bump. |
| `sandbox.paths.CODEX_TOOL_TURN_DIRECTORY_PREFIX` = `"songmaker-codex-tool-"` | `sandbox.paths.CODEX_TOOL_TURN_DIRECTORY_PREFIX` = `"agent-cli-codex-tool-turn-"` | The same for `<root>/agent-cli-codex-tool-turn-*/codex-home/`. |
| `sandbox.paths.CODEX_SANDBOX_PROOF_DIRECTORY` = `"songmaker-codex-sandbox-probe"` | removed | A host's sandbox proof allocates its directory as an image turn does: `CODEX_IMAGE_TURN_DIRECTORY_PREFIX` and a random part, below `<root>`, inside the runtime that configures it. Its own profile rule goes. |
| `grok.transport.GROK_CLI_TURN_DIRECTORY_PREFIX` = `"songmaker-grok-cli-"` | `grok.transport.GROK_CLI_TURN_DIRECTORY_PREFIX` = `"agent-cli-grok-turn-"` | None; no profile names it. |
| `configure()` accepts any `cli_working_directory_root` | `configure()` raises `config.WorkingRootNotPrivateError`, naming a closed `config.WorkingRootViolation` and never the path, unless the root is an absolute path to an existing directory, not a symlink, owned by the process's effective user, with mode exactly 0700 | Create a private root (for example `install -d -m 0700 <root>`), never `/tmp` itself, in every process that configures. Keep its owner, mode and path for the process lifetime. The profile names its resolved path. |
| A Codex turn whose private home cannot be created or made private (`mkdtemp`, the home `chmod` or the `work` `mkdir` raises `OSError`) | Raised unchained: `ProviderUnavailableError` with `CLI_UNAVAILABLE` ("CLI is unavailable.") on the tool turn; `CodexImageCliError("could not create a private CLI home")` on the image turn | A full or unwritable `cli_working_directory_root` now reaches the host's CLI-failure path instead of its unexpected-error path. The message holds no path. Songmaker shows it as `JOB_ERROR_COVER_IMAGE_CLI_FAILED` (`src/songmaker_cli/jobs/_runtime.py:111-116`, `:139`). |
| `dispatch.stream_turn` and `dispatch.call_provider_once` take a required `model` of type `str` | `dispatch.stream_turn` and `dispatch.call_provider_once` take a required `model` of type `str` or `None` | Map "nothing chosen" to `None`. `None` runs the CLI's own default on the Claude, Grok and Codex CLI routes of `dispatch.stream_turn`, and on `dispatch.call_provider_once` for Claude without an Anthropic key; every API route, and `dispatch.call_provider_once` for Grok and Codex, refuses it with `ROUTE_FAILED`. `""` is still refused with `ROUTE_FAILED`. |
| `codex.image.generate_codex_image` with `model=""` (the default) ran the Codex CLI's own default | `codex.image.generate_codex_image` takes `model` of type `str` or `None`, default `None`, which runs the CLI's own default, and refuses `""` with `codex.image.CodexImageCliError` | Pass `None` or leave `model` out; a caller that passed `""` for "no model" must stop passing it. |
| `grok.transport.GrokCliToolTransport` and `codex.transport.CodexCliToolTransport` take `model` of type `str` | `grok.transport.GrokCliToolTransport` and `codex.transport.CodexCliToolTransport` take `model` of type `str` or `None`; `None` starts and resumes without `--model`, and `""` is refused with `errors.ProviderUnavailableError` (`ROUTE_FAILED`) before any credential home is made | None for a caller that passes a model; pass `None` where the CLI's default is wanted, never `""`. |
| `claude.provider.call_claude`, `claude.provider.acall_claude`, `claude.provider.acall_claude_with_mcp`, `claude.provider.acall_claude_with_mcp_stream`, `claude.adapter.call_claude_once` and `claude.adapter.stream_claude_turn` with `model=None` ran the configured `turns.claude_chat_model` | `claude.provider.call_claude`, `claude.provider.acall_claude`, `claude.provider.acall_claude_with_mcp`, `claude.provider.acall_claude_with_mcp_stream`, `claude.adapter.call_claude_once` and `claude.adapter.stream_claude_turn` with `model=None` run the Claude CLI's own default; with an Anthropic key, `call_claude` and `acall_claude` refuse it with `ROUTE_FAILED` | Pass a model wherever you relied on the configured one, and always on the API. `model=""` is now refused with `ROUTE_FAILED` on each of them; it used to reach the CLI or the API. `call_claude` without a model no longer needs `turns`. |
| `SafeRouteReasonCode.CLI_BINARY_UNAVAILABLE` = `"cli_binary_unavailable"` ("CLI is unavailable.") | `SafeRouteReasonCode.CLI_UNAVAILABLE` = `"cli_unavailable"` ("CLI is unavailable."), given only when the CLI cannot run here: its binary is missing, its private home or credential copy cannot be made, or (on the Codex image preflight) an image dependency is missing | Rename the code wherever it is stored, compared or typed. The message is unchanged. |
| A CLI fault's code depended on the surface. `catalog.probe_provider_route` read a missing binary as `NOT_CONFIGURED` with `CLI_LOGIN_NOT_CONFIGURED`, and an unreadable, linked, oversize or malformed credential as `DISTURBED` with `CLI_BINARY_UNAVAILABLE`, as did `dispatch.image_capability`. `catalog.list_provider_models` gave `CATALOGUE_PROTOCOL_ERROR` for every CLI fault and for a logged-out Grok CLI, and a Claude catalogue home that could not be made escaped it as `process.AgentCliUnavailableError`. The Grok and Claude CLI turns gave `CLI_PROTOCOL_ERROR` for such a credential and for a home or copy that could not be made; the Grok turn also for a missing binary. `claude.provider.acall_claude` raised a plain `claude.provider.UnavailableError` for a missing binary. The Codex turns gave `CLI_AUTH_REJECTED` or `codex.image.CodexImageLoginError` when the credential copy could not be written, and the Codex tool turn gave `CLI_PROTOCOL_ERROR` for a missing binary. | Every surface maps two faults by class, both `process.AgentCliUnavailableError`s. A login fault (`process.CliLoginUnusableError`: the credential is missing, unreadable, linked, not a plain file, too large or malformed, or the CLI reports no login status) gives `NOT_CONFIGURED` with `CLI_LOGIN_NOT_CONFIGURED` from `catalog.probe_provider_route`, `CLI_LOGIN_NOT_CONFIGURED` from `dispatch.image_capability`, `catalog.list_provider_models` and the Grok and Claude turns, and, as before, `CLI_AUTH_REJECTED` or `codex.image.CodexImageLoginError` on the Codex turns. An environment fault (`process.CliEnvironmentUnavailableError`, with a `cause`) gives `DISTURBED` with `CLI_UNAVAILABLE` from `catalog.probe_provider_route`, `CLI_UNAVAILABLE` from `dispatch.image_capability`, `catalog.list_provider_models` and the Claude, Grok and Codex tool turns, and `codex.image.CodexImageCliError` on the image turn. `catalog.list_provider_models` gives `CLI_LOGIN_NOT_CONFIGURED` for a Grok status of logged out, which includes a status run that did not complete and a probe that outlived its budget (they gave `CATALOGUE_PROTOCOL_ERROR`); for Claude and Codex it asks no login, so a missing credential there gives what the catalogue run gives. `claude.provider.acall_claude` and the other Claude calls raise `claude.provider.CliUnavailableError` or `claude.provider.CliLoginNotConfiguredError`, both `claude.provider.UnavailableError`s. | A host asks its user to sign in again only for a login fault, never for a full disk or a missing binary. A catch of `process.AgentCliUnavailableError` keeps working. A readiness view that read `NOT_CONFIGURED` as "no CLI installed" now gets `DISTURBED` for a missing binary. A test fake that raises a bare `process.AgentCliUnavailableError` for a login or environment fault must raise `process.CliLoginUnusableError` or `process.CliEnvironmentUnavailableError`: a surface that the real call cannot reach with the bare base no longer catches it. |
| `claude.provider.CliBinaryUnavailableError`, raised when the Claude CLI binary is missing | `claude.provider.CliUnavailableError`, raised when the binary is missing or the call's private home or credential copy cannot be made; a missing or unusable credential raises the new `claude.provider.CliLoginNotConfiguredError` ("Claude CLI is not signed in."). Both are `claude.provider.UnavailableError`s, and a failed tool-surface probe replays its own class. | Rename the import. A Claude CLI turn maps them to `CLI_UNAVAILABLE` and `CLI_LOGIN_NOT_CONFIGURED`. |
| `catalog.provider_configuration` answered `catalog.ProviderNotLoggedIn` when no API key was set and the CLI probe failed for any reason, a missing Claude binary and a private home or credential copy that could not be made included | `catalog.provider_configuration` answers the new `catalog.ProviderCliUnavailable` (`provider`, `cause`, `missing_environment_key`) for such an environment fault, where `cause` is a `process.CliEnvironmentCause` (`binary_missing`, `home_not_created`, `credential_copy_not_written`); Login faults retain `catalog.ProviderNotLoggedIn`; existing ambiguous probe outcomes (caller-budget expiry, and incomplete or non-zero status runs) remain unchanged. `catalog.ProviderConfiguration` gains the member. The WARNING naming the fault's class stays. Grok and Codex reach it too, see the next row. | Handle the new member wherever you branch on `catalog.ProviderConfiguration`. Say the CLI cannot run here and why (install it or fix its configured binary; or check space and permissions under `cli_working_directory_root`), Offer API-key setup and re-evaluate configuration; Claude may also require its API dependency. Never ask to sign in. |
| `catalog.provider_configuration` answered `catalog.ProviderReady` (`GROK_CLI` / `CODEX_CLI`) for a present Grok or Codex credential whatever the configured binary, or `catalog.ProviderNeedsKey` where the surface excludes the CLI method | `catalog.provider_configuration` answers `catalog.ProviderCliUnavailable(provider, process.CliEnvironmentCause.BINARY_MISSING, missing_environment_key)` when the credential is present and the configured binary does not resolve to an executable file; a missing credential, or a Grok or Codex credential that is unreadable (wrong-typed, malformed, not valid UTF-8), still answers `catalog.ProviderNotLoggedIn`, and an API key still answers `catalog.ProviderReady`. A surface that excludes the CLI method answers `catalog.ProviderCliUnavailable` for a missing binary, where it answered `catalog.ProviderNeedsKey`; with a runnable binary it still answers `catalog.ProviderNeedsKey`. `process.grok_cli_token_is_present` now raises `process.CliLoginUnusableError` for a credential that is not valid UTF-8, where it used to raise `UnicodeDecodeError`. | The branch for `catalog.ProviderCliUnavailable` that you wrote for Claude now also fires for Grok and Codex: say the CLI cannot run here, offer API-key setup and re-evaluate, never ask to sign in. A `catalog.ProviderReady` for a CLI method now also proves the binary resolves (not that a login is live). |
| `codex.protocol.run_reserved_codex_cli`, returning the run's outcome | `codex.protocol.run_reserved_codex_cli`, `run_reserved_codex_cli(reservation, home_reservation, child, *, stdin_payload, deadline, channel)`: `child` (a `spawn.ChildProcess`) replaces `command`, arbitrary keywords are no longer accepted, and it returns nothing | No known host calls it. It contains every exception, `KeyboardInterrupt` and `SystemExit` included, and publishes a failed outcome on the channel, so call it only on a runner thread, never on the main thread. |
| `claude.provider.list_cli_model_aliases`, returning a list of names; Songmaker patched `agent_providers.catalog.list_cli_model_aliases` (`tests/test_cowriter_catalog.py:48`) | `claude.provider.cli_model_catalog`, returning a `claude.provider.ClaudeCliModelCatalog` whose `aliases` is a tuple of the same names and whose `current_model_label` (`str` or `None`) is the CLI's own name for its default | Rename the call and read `.aliases`. Rename every patch target: patch `agent_providers.catalog.cli_model_catalog` and return `ClaudeCliModelCatalog(aliases=(...), current_model_label=None)` instead of a sequence |
| `catalog.ProviderRouteSnapshot` with nine fields | `catalog.ProviderRouteSnapshot` with a tenth, last and required field, `default_model_label` (`str` or `None`). It holds the Claude CLI's name for what a turn with no model runs, for display only and never a model to pass; it is `None` for Codex, Grok, a route that is not ready and every API route | A host that builds a snapshot passes the new last field (Songmaker `tests/test_cowriter_providers.py:1964`). A host that only reads snapshots needs nothing |
| `process.run_cli_bounded` takes the argument vector (a sequence of strings) and the keywords `cwd`, `extra_env`, `cleanup_margin_seconds` and `unset_env` | `process.run_cli_bounded` takes a `spawn.ChildProcess` and the four keywords are gone | Build `ChildProcess(binary=Path(argv[0]), arguments=tuple(argv[1:]), ...)` with an absolute executable, the complete `environment` with `HOME` (it inherits nothing, so name every variable the child needs and omit what `unset_env` removed) and an absolute `working_directory`. `prompt_file_arg_index` now indexes `child.arguments`, which excludes the binary, so an old index of 3 becomes 2. A test that replaces `codex.protocol.run_cli_bounded` receives the `ChildProcess` as its first argument and must still call `on_reaped` for a reaped child or `on_spawn_failed` for a failed spawn, as `process.RunnerReleases` (the `on_reaped` and `on_spawn_failed` callbacks `process.run_cli_bounded` is given) says: `kwargs["extra_env"]` becomes `child.environment`, `kwargs["cwd"]` becomes `child.working_directory`, and reads of the command vector become `child.command`. Songmaker sites: `tests/test_cover_runner.py:398, 460`; `tests/test_cover_suggestion_job.py:174, 357, 387, 432, 584, 607, 636, 663`, plus the reads at `:154` and `:225`. |
| `process.run_cli` | removed | Run a catalog or probe CLI through `process.run_catalog_cli` with a `ChildProcess`; it keeps the probe budget and the `CliRun` or `None` style outcome. Take `process.run_cli_bounded` only to own its options (`stdin_payload`, `read`, `deadline`) and its different result. No known host calls `run_cli`. |
| `process.scrubbed_env` | removed; a child gets a closed environment, so there is nothing to scrub | Delete any call and name what the child needs in its `ChildProcess`. No known host calls it. |
| `process.claude_cli_login` takes `binary` as `str` or `None`, and `None` returns `LOGGED_OUT`; `process.grok_cli_status` and `process.codex_cli_login` read a missing binary as logged out | `process.claude_cli_login` takes `binary` as an absolute `Path` or `None`, and `None` raises `CliEnvironmentUnavailableError`; `process.grok_cli_status` and `process.codex_cli_login` raise `CliEnvironmentUnavailableError` (`BINARY_MISSING`) for a missing binary | Pass `Path(binary)` with an absolute path to `claude_cli_login`, and catch `CliEnvironmentUnavailableError` around all three calls where logged out stood for a missing binary. |
| `claude.provider.verify_cli_tool_surface`, `claude.provider.averify_no_builtin_cli_tools` and `claude.provider.verify_no_builtin_cli_tools` return the binary path as `str` | `claude.provider.verify_cli_tool_surface`, `claude.provider.averify_no_builtin_cli_tools` and `claude.provider.verify_no_builtin_cli_tools` return it as `Path` | Keep the `Path` for filesystem and process APIs, and use `str(binary)` where a string is required (string methods, JSON). |
| `text_tool_protocol.TextToolProtocolError` takes no arguments | `text_tool_protocol.TextToolProtocolError` requires `rejection`, a `TextToolRejection` | Code that only catches it needs no change. A fake or a direct construction supplies a `TextToolRejection`, preferably obtained by driving the real parser on a malformed response. |
| `codex.image.ImageToolBlockedError` takes an optional message, which fills `args` and `str(error)` | `codex.image.ImageToolBlockedError` requires `reason`, an `ImageToolBlockedReason`, and its `args` and `str(error)` are empty | Existing catches stay valid. A direct construction or a fake supplies an `ImageToolBlockedReason`, and a consumer reads `.reason` instead of the exception text. |
