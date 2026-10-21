# agent-providers

The reusable agent provider layer: it routes a turn to Claude, Grok or Codex —
over each provider's CLI or its HTTP API — and owns the machinery a host
application should not rebuild: live model catalogs and readiness probes, the
streaming turn transport, the tool loop, a bounded subprocess runner and the
sandbox directory-name constants. It also hosts the agent service, the one
authenticated entrance through which apps start agent runs.

**Readers.** A *host developer* pins the wheels and integrates the library or
the client; the sections on the distributions, the host obligations, the child
environment and the install are theirs. The *operator* runs the agent service;
the service and login sections are theirs. This file states concepts and
contracts. The code and its docstrings own behaviour, and the decision record
[`docs/decisions/0001-agent-service.md`](docs/decisions/0001-agent-service.md)
owns the service's rulings (identity, routing, admission, wire, tool relay);
`AGENTS.md` owns the layout, the boundaries, the checks and the release rules.

## The three distributions

Released together from one tag:

- `overnightworks-agent-providers`, import package `agent_providers` — the
  provider core. It depends on the agent client, `pydantic`, `httpx` and `anyio`,
  and on nothing else: no host application, no web framework, no database.
- `overnightworks-agent-client`, import package `agent_client` — the light
  client an app installs. It depends on `pydantic` and `ruamel.yaml` (PyJWT with
  cryptography in `[mcp-server]`) and never imports the core. It owns the tool
  contract `ToolDeclaration`, the agent-file reader (`agent_client.agents`), the
  service's wire (`agent_client.wire`: the `RunRequest` that starts a run and the
  streamed run events, with their stream rules in `agent_client.run_stream` and
  `agent_client.sse`), the run capability's format (`agent_client.capability`) and
  the app side of the MCP profile (`agent_client.mcp_server`, `mcp_wire`).
- `overnightworks-agent-service`, import package `agent_service` — the agent
  service, run as `agent-service --config PATH`. It builds on the other two and
  nothing imports it.

Import-linter contracts layer the service above the core above the client and
keep `songmaker_cli` out of all three; `webauth` is imported only by the
service, which alone verifies user and workload tokens, while an app verifies
run capabilities through `agent_client.mcp_server`. `lint-imports` proves it
in CI.

## Terms

| Term | Meaning |
|---|---|
| turn | One exchange: a system prompt and messages in, a stream of events out (`stream_turn`). |
| tool | A capability the host declares in its `ToolCatalog`. |
| tool call, tool result, tool round | One requested invocation, its outcome, and one provider response's batch of calls. |
| tool executor | The host's port that runs one tool call (`ToolExecutor`). |
| host | The application that embeds the library. |
| host data | Anything a host supplies or a host tool returns: system prompt, messages, tool arguments and results, an exception raised by host code. The library forwards it where its contract says and never logs it. |
| provider | `claude`, `grok` or `codex` (`ProviderName`). |
| route | `cli` or `api` (`ProviderRoute`); a *provider route* is one provider on one route. |
| one-shot completion | A tool-free single request bounded by a caller-owned deadline (`call_provider_once`). |
| image turn | A Codex CLI turn that produces one image under an `ImagePolicy`. |
| tool markup | The tag namespace and product name a host's text-protocol tools wear (`ToolProtocolMarkup`). |
| user | The person a turn runs for (`user_id`). |
| working root | The host's private directory below which the library creates every private directory and file: `cli_working_directory_root` for the catalog and turn paths, a `PrivateWorkingRoot` for a subscription run's `run_directories`. |

## Install

No distribution is published on PyPI, and PyPI is not planned: wheels pinned by
URL and hash from the tag's GitHub release give byte-exact, hash-checked wheels
with no PyPI name to race for; their dependencies still resolve from PyPI. An app
installs the client and the core; the core pins the client to its own version
exactly, so both URLs name the same tag. An app that needs only the tool contract
installs the client wheel alone.

```bash
pip install \
  "overnightworks-agent-client @ https://github.com/overnightworks/agent-providers/releases/download/v0.5.0/overnightworks_agent_client-0.5.0-py3-none-any.whl#sha256=9019e18411e6338629a5397a4f58b0b3cbd70f4b2cbd8184b97a3d53ee8ed94a" \
  "overnightworks-agent-providers @ https://github.com/overnightworks/agent-providers/releases/download/v0.5.0/overnightworks_agent_providers-0.5.0-py3-none-any.whl#sha256=16dd220515e00a45fdb00cb92f62a56e46a30d0cfdd7cfdf59529cd0b60d9789"
```

A uv host lists the same URLs in its `pyproject.toml`; `uv lock` records each
wheel's sha256 in the host's own `uv.lock`, and `uv sync` checks it. A host that
pins a git commit instead resolves the client only under uv, which reads the
workspace source from the repository; pip would look the client up on PyPI, so a
pip-based host pins both wheel URLs. Tags are never moved; a broken release gets
the next patch tag.

Optional extras sit between the name and the `@` in URL form
(`overnightworks-agent-providers[api,image] @ …`):

- `[api]` (core) pulls in the Anthropic SDK used by the Claude HTTP-API route.
- `[image]` (core) pulls in Pillow, used by the Codex image path.
- `[mcp-server]` (client) pulls in PyJWT with cryptography, used by
  `agent_client.mcp_server` to verify a run capability.

## What the host supplies

Nothing is configured by default; the library never invents a host fact.

**Runtime configuration.** A host that runs the catalog or turn paths installs one
`ProviderRuntimeConfig` per process with `configure()`, and those paths read it
back through `current_config()`, which raises until then rather than guessing a
binary path. `claude`, `grok` and `codex` blocks (`ClaudeCliRuntime`,
`GrokCliRuntime`, `CodexCliRuntime`: binary, login `token_file`, for Claude also
the binary search globs) describe the providers the host runs through their CLI;
a provider left `None` raises `ProviderNotConfiguredError(provider, block)` from
turns and catalog alike, and the catalog never reports it as not logged in. A host
that also runs turns adds `TurnRuntimeConfig` as `turns` (an optional
`McpServerSpec`, `turns.codex`, `turns.grok`); a turn path reached without its
block raises `TurnRuntimeNotConfiguredError` or `ProviderNotConfiguredError`
instead of running against a guess. These errors share the base `HostWiringError`
and `dispatch.stream_turn`, `dispatch.call_provider_once` and
`dispatch.image_capability` raise them unchanged. File-name prefixes in the
configuration (`McpServerSpec.config_file_prefix`,
`GrokTurnRuntime.prompt_file_prefix`) are 1 to 64 characters from
`A-Z a-z 0-9 . _ -`; the library appends random characters and creates the file
directly in the working root. The field docstrings in `agent_providers.config` are
the reference.

The subscription run the agent service drives (`cli_run.astream_cli_run(root,
adapter)`, with `claude.provider.ClaudeRun` as Claude's adapter, in directories
from `process.run_directories`) takes every fact as an explicit argument and reads
no installed configuration.

**The working root.** `configure()` refuses a `cli_working_directory_root` that is
not private: an absolute path written without `..`, to an existing directory, not
a symlink, owned by the process's effective user, with mode exactly 0700 (never
`/tmp` itself). It raises `WorkingRootNotPrivateError`, whose `violation` is a
member of the closed `WorkingRootViolation` and whose text never holds the path.
`PrivateWorkingRoot(path)` runs the same inspection at construction, so a host
that installs no runtime checks its root once at startup. The check proves privacy
only on a filesystem with POSIX ownership and modes, and the host keeps the
root's owner, mode, path and the process's effective identity for the process
lifetime.

**Ports passed in.** `ToolExecutor` (`agent_providers.tool_loop`) runs one tool
call; `ToolCatalog` (`agent_providers.tools`) holds the tool declarations and the
host-declared `ToolProtocolMarkup`; `McpServerSpec` (`agent_providers.config`)
says how the host's MCP server is launched; `ImagePolicy` (`agent_providers.images`)
constrains an image turn. Their docstrings state what a failure is and what the
loop logs. A turn emits the in-process `StreamEvent` union
(`agent_providers.events`); the service's wire is `agent_client.wire`, not that
union.

**Rejected tool-call text.** `TextToolCallRejectedError` carries a `rejection`
naming the rule and where the call broke, plus at most 1,024 code points of the
refused text as a `SecretStr`. `SecretStr` masks display and pydantic's JSON, not
pickle, copy or `vars()`, and traceback locals can hold the whole response. The
library logs the rule and the numbers, never the text; whether the text is
written anywhere is the host's decision.

**Lost process groups.** An async Claude turn on the CLI route raises
`ContainmentLost(group)` (`agent_providers.errors`, a plain `Exception`) when its
reap cannot show the turn's process group gone and release its leader; members of
the group may then still be alive, and the library keeps ownership of the group,
its core pool slot and its home. The causes (expiry, uncountable, publication
timeout, identity loss, failed leader release, reap failure) are the
`ContainmentLost` docstring's. Through `dispatch.stream_turn` it arrives as
`ProviderUnavailableError(ROUTE_FAILED)` with the `ContainmentLost` as
`__cause__`. The host calls `agent_providers.lost_groups.confirm_group_gone(group)`
until it returns `True`; `False` or an exception means not confirmed. A group
whose anchor was lost raises `ChildIdentityLost` on every call and stays held until
the process exits. `group` is valid only in the process that minted it: never
persist it or pass it to another process. If it never confirms, restart the
container to stop the remaining run processes. The service's `Quarantine`
(`agent_service.quarantine`) does this for the runs it admitted.

**Interruptions.** Python can raise an exception asynchronously inside a library
call: a signal handler raises one between two bytecode instructions of the main
thread, as the default `SIGINT` handler raises `KeyboardInterrupt`, and
`PyThreadState_SetAsyncExc` injects one into any thread. Python's documentation
says that most code, the standard library included, cannot be made robust against
this ([Note on Signal Handlers and
Exceptions](https://docs.python.org/3/library/signal.html#note-on-signal-handlers-and-exceptions)),
and this library is no exception. A host that needs orderly shutdown or continued
service uses signal handlers that do not raise for the whole time library calls
can run, its startup included, and never injects an exception into a thread that
runs library code. After such an interruption, stop using the library and end the
process: a later call may wait forever on a lock the interruption left held, and
graceful shutdown may stall. A resource failure such as `MemoryError` inside
`start_anchored_leader`'s ownership handoff or rollback likewise requires the host
to stop using the library and end the process. Normal interpreter exit attempts
the private-home cleanup without taking any of this library's locks, but the
cleanup is not guaranteed when shutdown cannot complete or a removal fails.

A host migrates at its pin bump: see the `Migrating to` sections below.

## The child environment is closed

Every child this library starts is one immutable `ChildProcess` (created only in
`agent_providers.spawn`): an absolute binary, an explicit working directory, and
the child's *complete* environment. Nothing is inherited and nothing is merged onto
a host baseline, because an optional set of extras on an inherited environment is
a denylist seen from the other side. `HOME` is always set — an agent CLI with no
`HOME` falls back to the passwd home, which is worse than a wrong one. Its `repr`
omits the environment, since it can hold a token.

**This is process configuration, not access control.** A closed environment stops
discovery by convention — `HOME`, `PATH`, `CODEX_HOME`, `GROK_HOME`, `XDG_*` — and
nothing else. It does not stop an absolute `open("/…/auth.json")`, traversal out of
the working directory, or the network, and UID, GID, umask, resource limits,
namespaces and mounts are inherited whatever this library does. A host that needs
containment brings the sandbox described below.

**Private homes and logins.** Every Claude, Grok or Codex turn, catalog probe and
subscription run gets a fresh private home below the working root (directories
0700, credential copies 0400, a Codex turn's blanked copy 0600); the source is
opened only for reading. Grok and Codex copy the host-named file into the home. A
Claude turn gets no copy: the setup token in `claude.token_file` reaches the child
only as `CLAUDE_CODE_OAUTH_TOKEN`, never in its arguments, and is read through
`secret_file` at every child start. A file that is missing, open to group or
others, over 16 KiB or holding JSON fails the turn with `CLI_LOGIN_NOT_CONFIGURED`
and leaves no home behind. Every Claude child also gets the run switches
(`constants.CLAUDE_RUN_SWITCHES`) and every `-p` run passes
`--no-session-persistence`, so no Claude child keeps a session on disk and its
telemetry and nonessential traffic stay off; the model request itself still
reaches the provider, and a Claude managed-settings export the host configures is
not overridden. Home removal never changes a call's outcome: a home that cannot be
removed is logged by error class alone, without its path.

**Containment of the process group.** The async Claude turns on the CLI route
(`acall_claude` without an `api_key`, `acall_claude_with_mcp`,
`acall_claude_with_mcp_stream`) run on a `spawn.AnchoredChild`. When the turn
ends, its reap sends SIGTERM to the leader's group if the leader still runs, waits
up to 1 s, then on every exit sends SIGKILL and counts the group's live members in
`/proc` until none is left or 5 s from that first SIGKILL have passed
(`ReapBounds`: `CLI_TERMINATION_GRACE_SECONDS`, `CLI_GROUP_CONFIRMATION_SECONDS`).
The slot and home are freed only after an empty count within the bound, the
leader's exit published and the leader released; otherwise the group is retained
and the turn raises `ContainmentLost`. A process that left the group with `setsid`
or `setpgid` is not counted and keeps running. The bounded runner
(`process.run_cli_bounded`) follows the same rule, keeps an unreaped group and
recounts it in the background for up to 60 s, and holds its reservation and home
until the process exits; `cli_settle_deadline(deadline)` is the deadline plus
11.1 s. The synchronous `call_claude` turn's timeout kills only the direct child.
`spawn`, `process`, `anchored_turn` and `lost_groups` carry the exact contracts.

**A subscription run's directories.** `process.run_directories(root, files)`
reads no installed configuration. `root` is a `PrivateWorkingRoot`; the names in
`files` are checked on entry, before anything is created. Below the root the run
gets one tree, `agent-run-XXXXXXXX/`, with `home/` (the child's `HOME` and
`TMPDIR`) and a separate empty `work/`, each 0700, and each file is written
exclusively into `home/` at 0600. The modes keep a run's files from other users,
not from another process of the same user or from root. The tree is removed once,
after the context ended and the last reservation was released, so a retained group
keeps its directories until `confirm_group_gone` returns `True`. Removal reads the
mount table once and refuses a tree with a mount point at or below it, because a
recursive delete would cross into the mounted filesystem; that is detection at one
instant, so the host keeps the mount topology at and below the working root
unchanged during every removal. The service's container meets this with a working
root that is its own tmpfs, a non-root user without capabilities and nothing
mounted into the root; that is a deployment condition the library does not check.
A tree that cannot be removed is logged once by class or reason, never with its
path, is never retried and stays until the host clears the root. The process
docstrings carry the rest.

## Sandbox

`agent_providers.sandbox.paths` holds directory-name prefix constants —
`CODEX_IMAGE_TURN_DIRECTORY_PREFIX`, `CODEX_TOOL_TURN_DIRECTORY_PREFIX`,
`CODEX_HOME_DIRECTORY_NAME`. They are the contract between this package and a
host's kernel sandbox profile: every confined Codex run works inside Bubblewrap
with one writable place, and its directory lies directly below the host's
private `cli_working_directory_root`. This library ships the constants, not a
profile. A host brings its own AppArmor/seccomp profile and derives it from
these values: it permits exactly
`/oldroot<R>/<prefix>*/codex-home/ -> /newroot<R>/<prefix>*/codex-home/` for the
image and tool prefixes, where `R` is the root's resolved path. A renamed prefix
or a moved root needs the profile reloaded with the pin, so a rename becomes a
red test rather than a silently refused mount. A host's sandbox proof allocates
its directory as an image turn does: `CODEX_IMAGE_TURN_DIRECTORY_PREFIX` plus a
random part, below the root.

The Codex image gate admits the events Codex CLI 0.163.0 measurably emits for
an image turn, and no command at all: a `command_execution` item ends the turn
as `blocked_item`. A host still on 0.147.0, whose CLI read the skill through the
shell, must move to 0.163.0. If a later CLI reads the skill through the shell
again, the logged `types` show `item.started:command_execution`, and that path is
measured before it is admitted. On 0.163.0 the image call itself is invisible to
`exec --json`, so the gate's block checks cover only the tool calls the CLI
*reports*. A returned PNG is tied to the turn only by its location under the
private `CODEX_HOME` and by the re-encode under the host's `ImagePolicy`. The gate
is therefore defence in depth; the containment is the read-only sandbox plus the
host's AppArmor and seccomp profile, which the host owns.

## Safe diagnostic codes

The library logs closed diagnostic codes for rejected Grok tool-protocol and Codex
image-gate paths. They identify a stable rejection class without copying prompts,
arguments, paths, IDs or exception text into logs. The Codex image route's codes
are `unexpected_event`, `blocked_item` and `unexpected_item`, logged as
`Codex image tool rejected route=cli code=<code> types=[...]`; a completed turn
that drew nothing is logged as `Codex image not created route=cli types=[...]`.
`types` is the turn's event sequence as closed event and kind *names*, never
contents: anything outside the gate's sets is `other`, consecutive repeats read
`name*N`, and a long turn keeps its last 32 names behind a leading `...+K`.
`ImageToolBlockedError` carries the same names as `observed`.

## The agent service

The service is the one authenticated entrance through which apps start agent
runs. It runs only on Linux, as an unprivileged effective user, and makes itself
non-dumpable at startup before it reads its configuration; it refuses to start
otherwise. The restriction names the service only. The image must run it as a
non-root user with `cap_drop: ALL`; that belongs to A1d (#18), and the service's
deployment lives in edge, not here. `agent-service --config PATH` takes a TOML file
whose required keys have no default; startup names the field of any unknown or
missing one and exits 2. `agent_service.config` documents the file, and the
decision record owns the rulings behind it.

**Routes.** `POST /runs` takes a `RunRequest` and answers with the run's event
stream (SSE), ending every run with exactly one terminal event except the cut
stream of a dropped client. `GET /runs/{id}` returns the run's status, readable
only by the app and user that started it (the record is in memory, kept up to an
hour after the run ended, and gone after a restart). `agent_client.wire` owns both
bodies and the typed rejections.

**Who may run what.** A caller presents a workload token bound to one registered
app and a user token bound to one principal (`agent_service.identity`).
`agent_service.routing.route` decides the route from the caller's groups alone,
never the purpose: a member of a subscription group (`[identity]
subscription_groups`) takes the CLI route under the subscription profile; a member
of an API group (`api_groups`, set together with `[route] api_alias`) who also
holds the app's own group takes the paid API route; anyone else is not enabled.
`RunRequest.purpose` (`[a-z][a-z0-9-]{0,63}`) labels the run and its capability
and routes nothing. At most `[runs] max_parallel_cli_runs` (default 5) CLI runs
are admitted; one beyond gets 429 `busy` with `Retry-After` and never falls back
to the API route. Today only the CLI runner is installed, with Claude as its only
provider; the version of each pinned CLI is checked at startup.

**Agent files.** An app declares its agents as Claude Code subagent files,
`src/<pkg>/agents/<name>.md`, that `agent_client.agents.load_agent` reads into an
`AgentDefinition`: YAML front matter (`name`, `description`, `tools`, plus
`model` as `<provider>/<model>` and the extension `appRoles`, exactly `[admin]` or
`[admin, user]`) and the Markdown body as the prompt. `AgentFileError` names the
file and the field. `offered_to(role)` is a display filter, not the security
boundary: the service checks the role.

**App tools through MCP.** An app that offers tools declares one canonical https
MCP origin in the service's configuration; the service mints each admitted run a
capability (an RS256 token signed with the service's one RSA-3072 key,
`[signing] key_file`, at most 8,192 characters) and trusts the CA bundle
`[mcp] ca_file` names for those origins alone. The app installs the client with
`[mcp-server]` and serves `McpServer.in_front_of(app)` as its outermost ASGI app,
never a `Route` or `Mount` in its router (the service refuses their slash
redirects). It passes the bytes of its public key (one PEM `PUBLIC KEY` block
holding an RSA-3072 key with exponent 65537), and each `McpTool` handler checks
ownership with a fresh database session. `verify_capability` returns the claims or
one closed `CapabilityRefusal` and never raises for token content; a body that
carries the capability's token or signature is replaced by an empty 500.
`agent_client.mcp_server` documents the order of the checks.

**Signing key and generation.** The service never makes its key; generation and
rotation are the A1a4 ruling in the decision record.

### How a host provides each provider's login

Each provider reads its login from one file that the host names. The file is the
host's to make and to keep fresh; the library never creates, renews or rewrites
it. The agent service holds the one login per provider for all apps — anyone with
the KI-Abo in Keycloak may use it — and names the file as
`profiles.<name>.token_file`. Today only Claude has a profile:

```toml
[profiles.subscription]
provider = "claude"
token_file = "/run/secrets/claude-token"
```

An app that still uses the core directly names the service's login file in the
provider's block of its `ProviderRuntimeConfig`. This is the interim path, with the
gaps listed below; #18 A1r5 removes it.

**Claude.** The file holds one line, the token `claude setup-token` prints, at a
mode that gives nothing to group or others (0600 or 0400), at most 16 KiB. JSON
(a credentials file) and API keys (`sk-ant-api...`) are refused; a link is
followed and its target checked. Run `claude setup-token` once, sign in with the
subscription, and write the printed token into the file. The token lasts a year
and renewing it is the operator's: `scripts/replace_token_file.py PATH` reads the
new token on standard input, checks it by the service's rules and swaps it in
atomically. The next turn or run reads the file again, so no restart is needed.
On expiry a streaming turn ends with the safe code `cli_auth_rejected` ("CLI login
was rejected or has expired."); a turn without a stream says "Claude CLI is
unavailable. Check server logs for details." (gap L6); a service run fails with
`provider_error`. On core turns a missing or refused file raises
`CliLoginNotConfiguredError`, mapped to `cli_login_not_configured`; in the service
it prevents startup or fails a later run with `provider_error`, before any child
starts. The model list asks the CLI without a login and names no default.

**Codex and Grok.** The file is the `auth.json` that `codex login` or `grok
login` writes; the interim core path accepts it. How the shared service login is
created and kept fresh is undecided (#115 HQ2), the service has no Codex or Grok
profile yet, and the token lifetime and expired-login behaviour are unmeasured
(G0b). A Codex child's copy has an empty `refresh_token`; a Grok child gets the
full copy, `refresh_token` included, and whether it can renew or rotate the login
with it is not measured (#255 S4g decides).

**What does not yet hold on the interim core path:**

- **L1.** Codex and Grok files are read without a mode check, up to 256 KiB.
  Grok's readiness check reads its file with no size limit.
- **L2.** The Codex and Grok turn copy refuses a link (stricter, not unsafe).
  Grok's readiness check follows a link without checking the target.
- **L3.** Codex and Grok read the file once per turn; a swapped file reaches the
  next turn, not the running one.
- **L4.** Grok's format is checked only for readiness. A turn or catalog copies
  any readable file, even `{}`.
- **L5.** Grok's child gets the full copy.
- **L6.** A Claude turn without a stream reports a rejected token as unavailable.
- **L7.** Claude's readiness check reports a rejected token as signed in.

#18 A1r5 deletes this core path. L1 to L4, L6 and L7 go with it; the service reads
its files under the rules of the Claude paragraph above.

## Migrating to 0.6.0

A host migrates at its pin bump to 0.6.0.

| Before | Now |
|---|---|
| `[identity] subscription_group`, one string | `subscription_groups`, a non-empty array; write `["/ki-abo", "/ai-subscription"]` while tokens still carry the old name, and drop the old entry once they expire; a leftover `subscription_group` stops startup naming it |
| `[purposes.<purpose>]` tables (`cli_provider`, `api_alias`) and `[apps.<app>] purposes` | removed; a leftover `purposes` or `apps.<app>.purposes` stops startup naming the field. Keycloak decides the route through the caller's groups: a subscription group selects the CLI route, and an API group together with the app's own group selects the paid route |
| (absent) | `[identity] api_groups` and `[route] api_alias`, optional and set together; without them nobody reaches the paid route |
| `RunRequest.purpose`, any non-empty text the app's `purposes` list names | `agent_client.wire.Purpose`: `[a-z][a-z0-9-]{0,63}`, matched against the whole string; anything else answers 422 `invalid_request`. Any valid purpose from any registered app is admitted; it labels the run and its capability and routes nothing |
| `agent_client.mcp_wire.object_without_duplicate_members` | removed, no alias remains; `agent_client.mcp_wire.read_strict_json(text)` reads JSON that repeats no member and holds no `NaN` or infinity, and raises only `ValueError` |

## Migrating to 0.5.0

A host migrates at its pin bump to 0.5.0. The only row is the module move
below; nothing else a host imports or configures changed shape.

Two changes need no action. Every core Claude child (turns, the tool-surface
probe, `auth status` and `-p /model`) now starts with the run switches
(`constants.CLAUDE_RUN_SWITCHES`), and every core `-p` run passes
`--no-session-persistence`; [The child environment is
closed](#the-child-environment-is-closed) says what that switches off. The
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
| an async Claude turn on the CLI route whose leader had not exited 5 s after SIGKILL logged one ERROR, handed it to a background reaper and raised nothing for it | the turn raises `errors.ContainmentLost(group)`, a plain `Exception`, whenever its reap cannot show the group gone (see `ContainmentLost` above). `claude.adapter.stream_claude_turn` passes it unchanged; `dispatch.stream_turn` raises `ProviderUnavailableError` with `ROUTE_FAILED` whose `__cause__` is the `ContainmentLost`. Either way the pool slot and the home stay held, with nothing freeing them in the background, until `lost_groups.confirm_group_gone(group)` returns `True` or the process exits; call it until it does |
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
| `process.run_cli_bounded` takes the argument vector (a sequence of strings) and the keywords `cwd`, `extra_env`, `cleanup_margin_seconds` and `unset_env` | `process.run_cli_bounded` takes a `spawn.ChildProcess` and the four keywords are gone | Build `ChildProcess(binary=Path(argv[0]), arguments=tuple(argv[1:]), ...)` with an absolute executable, the complete `environment` with `HOME` (it inherits nothing, so name every variable the child needs and omit what `unset_env` removed) and an absolute `working_directory`. `prompt_file_arg_index` now indexes `child.arguments`, which excludes the binary, so an old index of 3 becomes 2. A test that replaces `codex.protocol.run_cli_bounded` receives the `ChildProcess` as its first argument and must still call `on_reaped` for a reaped child or `on_spawn_failed` for a failed spawn, as the child-environment section above says: `kwargs["extra_env"]` becomes `child.environment`, `kwargs["cwd"]` becomes `child.working_directory`, and reads of the command vector become `child.command`. Songmaker sites: `tests/test_cover_runner.py:398, 460`; `tests/test_cover_suggestion_job.py:174, 357, 387, 432, 584, 607, 636, 663`, plus the reads at `:154` and `:225`. |
| `process.run_cli` | removed | Run a catalog or probe CLI through `process.run_catalog_cli` with a `ChildProcess`; it keeps the probe budget and the `CliRun` or `None` style outcome. Take `process.run_cli_bounded` only to own its options (`stdin_payload`, `read`, `deadline`) and its different result. No known host calls `run_cli`. |
| `process.scrubbed_env` | removed; a child gets a closed environment, so there is nothing to scrub | Delete any call and name what the child needs in its `ChildProcess`. No known host calls it. |
| `process.claude_cli_login` takes `binary` as `str` or `None`, and `None` returns `LOGGED_OUT`; `process.grok_cli_status` and `process.codex_cli_login` read a missing binary as logged out | `process.claude_cli_login` takes `binary` as an absolute `Path` or `None`, and `None` raises `CliEnvironmentUnavailableError`; `process.grok_cli_status` and `process.codex_cli_login` raise `CliEnvironmentUnavailableError` (`BINARY_MISSING`) for a missing binary | Pass `Path(binary)` with an absolute path to `claude_cli_login`, and catch `CliEnvironmentUnavailableError` around all three calls where logged out stood for a missing binary. |
| `claude.provider.verify_cli_tool_surface`, `claude.provider.averify_no_builtin_cli_tools` and `claude.provider.verify_no_builtin_cli_tools` return the binary path as `str` | `claude.provider.verify_cli_tool_surface`, `claude.provider.averify_no_builtin_cli_tools` and `claude.provider.verify_no_builtin_cli_tools` return it as `Path` | Keep the `Path` for filesystem and process APIs, and use `str(binary)` where a string is required (string methods, JSON). |
| `text_tool_protocol.TextToolProtocolError` takes no arguments | `text_tool_protocol.TextToolProtocolError` requires `rejection`, a `TextToolRejection` | Code that only catches it needs no change. A fake or a direct construction supplies a `TextToolRejection`, preferably obtained by driving the real parser on a malformed response. |
| `codex.image.ImageToolBlockedError` takes an optional message, which fills `args` and `str(error)` | `codex.image.ImageToolBlockedError` requires `reason`, an `ImageToolBlockedReason`, and its `args` and `str(error)` are empty | Existing catches stay valid. A direct construction or a fake supplies an `ImageToolBlockedReason`, and a consumer reads `.reason` instead of the exception text. |

## Checks

`AGENTS.md` lists the commands (sync, suite, ruff, `lint-imports`, the three-wheel
build) and what CI adds. The suite runs without a live provider; the service's
end-to-end proof, `tests/test_service_keycloak.py`, runs only where `KEYCLOAK_URL`
names the test Keycloak of the `keycloak-token-shapes` CI job.

## Licence

MIT — see [LICENSE](LICENSE).
