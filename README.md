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
turns and from the catalog's CLI paths, and the catalog never reports it as not
logged in; an API route needs no block, so with an API key
`catalog.provider_configuration()` answers `ProviderReady` (or
`ProviderCapabilityMissing` when the Anthropic extra is missing) before any CLI check. A host
that also runs turns adds `TurnRuntimeConfig` as `turns` (an optional
`McpServerSpec`, `turns.codex`, `turns.grok`); a turn path reached without its
block raises `TurnRuntimeNotConfiguredError` or `ProviderNotConfiguredError`
instead of running against a guess. These errors share the base `HostWiringError`
and `dispatch.stream_turn`, `dispatch.call_provider_once` and
`dispatch.image_capability` raise them unchanged. File-name prefixes in the
configuration (`McpServerSpec.config_file_prefix`,
`GrokTurnRuntime.prompt_file_prefix`) are 1 to 64 characters from
`A-Z a-z 0-9 . _ -`; the library appends random characters and creates the file
directly in the working root. Pydantic hides an invalid value only in the rendered
error text: `ValidationError.errors()` and `.json()` still contain it unless called
with `include_input=False`, which matters to a host that logs configuration errors. The field docstrings in `agent_providers.config` are
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

A host migrates at its pin bump; [docs/MIGRATING.md](docs/MIGRATING.md) has one section per release.

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
not from another process of the same user or from root, and the removal gives no
containment against concurrent moves by processes of the same Unix user. The tree is removed once,
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
and renewing it is the operator's: `uv run python scripts/replace_token_file.py PATH` reads the
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

## Checks

`AGENTS.md` lists the commands (sync, suite, ruff, `lint-imports`, the three-wheel
build) and what CI adds. The suite runs without a live provider; the service's
end-to-end proof, `tests/test_service_keycloak.py`, runs only where `KEYCLOAK_URL`
names the test Keycloak of the `keycloak-token-shapes` CI job.

## Licence

MIT — see [LICENSE](LICENSE).
