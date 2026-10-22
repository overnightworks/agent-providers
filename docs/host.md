# Host developer guide

For the developer of a host application that installs the library or the
client: how to install it, what the host supplies, and what the library does
with it. This is a reference. The code and its docstrings own behaviour; the
mental model is in [ARCHITECTURE.md](ARCHITECTURE.md), the changes between
releases in [MIGRATING.md](MIGRATING.md).

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
with no PyPI name to race for; their dependencies still resolve from PyPI. The
release page lists each wheel as a `name @ URL#sha256=HASH` requirement. An app
installs the client and the core; the core pins the client to its own version
exactly, so both URLs name the same tag. An app that needs only the tool contract
installs the client wheel alone.

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

A host migrates at its pin bump; [MIGRATING.md](MIGRATING.md) has one section per release.

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
and leaves no home behind (the error is `CliLoginNotConfiguredError`); a token the provider
rejects ends a streaming turn with the safe code `cli_auth_rejected`. Every Claude child also gets the run switches
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
