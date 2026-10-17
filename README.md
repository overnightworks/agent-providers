# agent-providers

The reusable agent provider layer: it routes a turn to Claude, Grok or Codex —
over each provider's CLI or its HTTP API — and owns the machinery a host
application should not rebuild. Live model catalogs and readiness probes, the
streaming turn transport, the tool loop, a bounded subprocess runner, and the
sandbox directory-name constants all live here; everything that touches a host's
own truth reaches the library through the ports the host supplies. The agent
service, the one authenticated entrance through which apps start agent runs,
lives here too.

Three distributions ship from this repository, released together from one tag:

- `overnightworks-agent-providers`, import package `agent_providers` — the
  provider core.
- `overnightworks-agent-client`, import package `agent_client` — the light
  client an app installs; it depends on `pydantic` only and owns the tool
  contract `ToolDeclaration` and the agent service's run contract: the
  `RunRequest` that starts a run and the events it streams.
- `overnightworks-agent-service`, import package `agent_service` — the agent
  service, run as `agent-service --config PATH`. It builds on the other two and
  nothing imports it. The service runs only on Linux, as an unprivileged
  effective user, and makes itself non-dumpable at startup before it reads its
  configuration; it refuses to start otherwise. The restriction names the
  service only, not the client or the core. The image must run the service as a
  non-root user; that user and `cap_drop: ALL` belong to A1d (#18).

## What it owns, and what it leaves to the host

It owns the machinery that is the same in every deployment:

| Module | Owns |
|---|---|
| `agent_providers.config` | The deployment facts every module reads, installed once per process via `configure()`, which refuses a working root that is not private |
| `agent_providers.catalog` | Live provider/route readiness probes and the model names each route offers, aliases or ids as the provider lists them (`probe_provider_route`, `list_provider_models`) |
| `agent_providers.dispatch` | Routing one turn to a provider and route, and the image capability |
| `agent_providers.events` | The `StreamEvent` union a turn emits in-process (assistant text, tool call, tool result, final); not the service wire |
| `agent_providers.tool_loop` | The provider-agnostic tool loop driving a `ToolExecutor` over a `ToolTransport` |
| `agent_client.tools` | `ToolDeclaration`: one tool's name, description, JSON-schema arguments and `ToolEffect` |
| `agent_client.wire` | The one owner of the agent service's wire: the `RunRequest` body, the versioned run-event union, the `RunRejection` body and the `RunStatus` body of `GET /runs/{id}` |
| `agent_client.run_stream` | `parse_event` and `RunStream`: the rules a run's event stream keeps, and how a cut-short run ends |
| `agent_client.sse` | `encode_event` and `RunStreamDecoder`: one SSE frame per run event, decoded and checked against the wire and its stream rules |
| `agent_client.capability` | The format of the run capability the service mints and an app's MCP server verifies (A1m): token type `agent-capability+jwt`, at most 8,192 characters, the strict `CapabilityClaims`, `McpOrigin`, the one decider of an app's canonical https MCP origin, and `MCP_ENDPOINT_PATH`, the one path an app's MCP server serves and the service's client calls |
| `agent_providers.codex_login` | `assess_codex_login`: the one decider of whether a Codex login can start a child, and the blanked copy it receives |
| `agent_providers.tools` | The `ToolCatalog`, its host-declared `ToolProtocolMarkup` and its Anthropic/OpenAI schema projections; re-exports the identical `ToolDeclaration` |
| `agent_providers.spawn` | `ChildProcess` and the only calls in this library that create a child process |
| `agent_providers.process` | `run_cli_bounded`: the bounded runner over a `ChildProcess` (start, stdin, first line, deadline, process-group cleanup); its line channel ends in exactly one outcome before the run returns, `cli_settle_deadline(deadline)` is the one instant by which every wait ends, and a failed step is reported by name and exception class only |
| `agent_providers.procfs` | `group_census`, one scan of one process group's live members from `/proc`, unavailable unless `/proc` is visibly a procfs of the process's own PID namespace with nothing hidden |
| `agent_providers.images` | The `ImagePolicy` an image turn is held to |
| `agent_providers.sandbox` | The directory-name prefix constants a host's kernel sandbox profile must permit |
| `agent_service.app` | `POST /runs` and `GET /runs/{id}`: the order of checks (a CLI run takes an admission place before anything is signed), the typed rejections, the admitted run's event stream (`_RunResponse`, which closes the run whenever its response ends), and the run status that only the app and user that started a run may read; an admitted run of an app with an MCP origin hands its runner one capability, and an oversized one answers 422 `capability_too_large` before any runner, stream or record |
| `agent_service.admission` | The service's admission limit: at most N parallel subscription runs (default 5, from `[runs] max_parallel_cli_runs`); a CLI run takes a `Lease` before its run id or signature, one beyond the limit gets 429 `busy` with `Retry-After: 10` and never falls back to the API route; refusals and API runs take nothing, the run's stop releases its lease, as does a response that ends before its run began, and only a lease that still holds its place can free it |
| `agent_service.identity` | The two-token check: the workload token bound to one registered app, the user token to one principal |
| `agent_service.run_request` | The strict read of a `RunRequest` body: strict JSON, then its version, then its shape |
| `agent_service.config` | The service's TOML: identity, listener, run pacing and limits, at most one subscription profile, only for Claude, with a required `token_file` read once at startup and never shown, the `[cli]` section (the absolute `claude_binary` and the exact `claude_version` it must report, required by a Claude profile), purposes and apps, the optional `[signing] key_file` the signing key is read from, each app's https MCP origin (canonical, one per app, needing `[signing]`, and required when the app lists tools), and the startup preflight that measures every app's capability against 8,192 characters |
| `agent_service.process_hardening` | The first startup step: `harden_process` refuses a platform other than Linux and an effective uid 0, then makes the process non-dumpable (`PR_SET_DUMPABLE` 0) before the configuration is read, so a same-UID child cannot open the service's `/proc/<pid>/environ` or `mem`; any refusal is one line and exit 2 |
| `agent_service.cli` | `agent-service --config PATH`: hardening, configuration, version preflight, then `close_transport_loggers`, which sets `httpx`, `httpcore` and every existing `httpcore.*` logger above CRITICAL (children made later inherit it) so no call the dependencies make creates a record; uvicorn's logging configuration leaves it closed, a refused start changes no logging, and the core's HTTP tracing is lost in the service process |
| `agent_service.claude_version` | The pinned Claude version: the version grammar, the parser of `claude --version`, and the startup probe that spawns the program through the core's `ChildProcess` and bounded runner and refuses a start when it reports another version |
| `agent_service.secret_file` | How the service reads a secret kept in a file: opened once, type, mode (nothing for group or others) and size (16 KiB) taken from that descriptor; refusals name the path and the reason, never the content. A profile token must be one printable line and not an `sk-ant-api` API key |
| `agent_service.signing` | The service's one signing key: an RSA-3072 PEM (exponent 65537) read once at startup and never made by the service, refused when unsound; its `kid` is the RFC 7638 thumbprint, and `SigningKey.sign(claims, token_type)` writes a typed RS256 token. The key signs each admitted run's capability. Generation and rotation: see the A1a4 ruling in `docs/decisions/0001-agent-service.md` |
| `agent_service.capability` | The capability's claims and signing and its 8,192-character guard (`CapabilityTooLarge`), and the startup preflight that measures each app's capability with a subject reserve of 255 quotation marks, `iat = 9_999_999_999` and `exp = floor(iat + max_duration_s)`; the actual signed-token guard remains authoritative. `capability_too_large` (422) answers an oversized capability at admission; `Capability.appears_in(data)` is the one predicate that decides whether bytes carry the capability, whole or as its signature segment (a split, encoded or reversed token is not detected) |
| `agent_service.routing` | `route`: the one H5 decider between the CLI route, the API route and `not_enabled` |
| `agent_service.run_events` | The mapping of the core's turn events onto the wire frames, ending every run with exactly one terminal, except the cut stream of a dropped client (H6); also the run's tool-call and byte budget |
| `agent_service.run_liveness` | When frames go out: the heartbeat, the idle timeout and the maximum duration, enforced by a supervisor that does not depend on the client reading, and drops a client that stops taking frames |
| `agent_service.runners` | The `Runner` port a route's provider plugs into; production installs none yet; an admitted run carries its capability |
| `agent_service.run_records` | The in-memory record of each run (owner, instants, events accepted, ending, client lost; never event text): readable by its own app and user up to one hour after the run ended, evicted earlier once 10,000 ended records are held, gone after a restart. The record is written as the run's events are accepted and ended when its runner is closed |
| `agent_service.mcp_wire` | The JSON-only MCP wire profile, pure and free of sockets, clients and the relay: strict JSON-RPC models, the one supported protocol version (`2025-11-25`) and its negotiation, the `MCP-Protocol-Version` header rule (an absent header is refused with 400), the `initialize` result built from constants, `tools/list` with a cursor, `tools/call` read as text with `is_error`, the `application/json`-only answer rule, 202 with an empty body for a notification, and one closed `McpWireError` that echoes nothing of its input; the writers `error_response` (the `JsonRpcErrorCode` members `METHOD_NOT_FOUND` -32601 and `INVALID_PARAMS` -32602, closed messages, only the id echoed) and `tools_list_response` (kept definitions by alias, a `None` description omitted, no `nextCursor`); a body over `MAX_EVENT_BYTES` and a batch array are refused |
| `agent_service.app_tools` | The service's client of an app's MCP endpoint over the JSON-only profile: one session per run, opened with the run's `Capability` as an object (the token is read once, by a private `httpx.Auth`, when the `Authorization` header is written), that handshakes, lists tools with bounded pagination and calls one tool once; TLS is verified through a caller-supplied `ssl.SSLContext`, answers are read identity-only and bounded at `MAX_EVENT_BYTES`, and wire, protocol, HTTP-status and transport failures leave as closed `UpstreamError` members that name no URL, header, body or `httpx` object: `UpstreamRedirected` (any 3xx, never followed), `UpstreamRefused`, `UpstreamProtocolError` (an event-stream answer is refused at its headers), `UpstreamUnreachable` (connect, TLS or pool: no byte was sent) and `UpstreamCallLost` (any other `httpx.HTTPError`: the call may have run and is never re-sent); an `httpx` failure when the client closes leaves as its closed error (`UpstreamCallLost` for a `CloseError`) without its request, unless an exception is already leaving; the client sends POST only, with no GET, no DELETE and no read deadline |
| `agent_service.tool_relay` | The per-run loopback relay: an `asyncio` listener on `127.0.0.1` with `h11` that judges every request head in order (framing, `Host`, `Origin`, the per-run bearer secret, path, method), answers each refusal empty, keeps the connection after a bodyless 404 or 405 only where `h11` allows another cycle, refuses a body over `MAX_EVENT_BYTES` (declared or counted) with 413 and invalid body framing with `h11`'s status, hands an admitted request's whole body and `MCP-Protocol-Version` to the `RelayOwner` port and writes its `HttpAnswer` as given (an owner failure answers an empty 500), and keeps the connection after a fully consumed answer unless the request must close (a request with both `Transfer-Encoding` and `Content-Length` always does); serves at most `MAX_RELAY_CONNECTIONS` (4) connections and aborts any further one at once, aborts a request head not complete within `HEAD_RECEIVE_SECONDS` (5 s, from the phase start or the first byte) and a body not complete within `BODY_RECEIVE_SECONDS` (10 s, from the head) on the run's `RunClock` without a response, leaving idle keep-alive connections, response writes and the owner's time unbounded, and closes in one shared attempt bounded by `RELAY_CLOSE_SECONDS` (1 s): `close()` returns when every relay task ended and otherwise raises `RelayCleanupIncomplete` with the outstanding tasks still tracked, so the next `close()` retries; a connection accepted while closing is aborted and never served; `McpRelayOwner` is its first `RelayOwner`: it refuses a CLI response, error or batch with 400, exempts only `initialize` from the version-header rule (so `server/discover` with `2026-07-28` and any headerless request get 400), answers `initialize`, `ping` and every notification from constants, answers an unknown method with -32601, serves `tools/list` narrowed to the capability's `tools` claim in the offer's order (a repeated allowed name raises `RepeatedToolOffer` at construction), hands a parsed `tools/call` to the `ToolCalls` port, and passes every answer through one exit that refuses an owner already refused and checks the body with `Capability.appears_in`; a hit drops the body, refuses the owner for good and answers an empty 500 with one content-free log line; the relay installs no signal handler and logs by class only an owner failure and an unexpected connection failure, and nothing about a request |

It leaves the application everything the library cannot know: the MCP tool set
and its execution, the prompts, where settings are stored, and domain
ownership. The core depends on the agent client, `pydantic`, `httpx` and
`anyio` at runtime and on nothing else — no host application, no web framework,
no database; the client depends on `pydantic` only and never imports the core.
Import-linter contracts forbid either package reaching back into
`songmaker_cli` or the sibling `webauth` library and layer the service above
the core above the client; only the service verifies tokens, so only it imports
`webauth`. `lint-imports` proves them in CI.

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
| working root | The host's private directory below which the library creates every private directory and file (`cli_working_directory_root`). |

## Install

No distribution is published on PyPI, and PyPI is not planned: wheels pinned by
URL and hash from the tag's GitHub release give byte-exact, hash-checked wheels
with no PyPI name to race for; their dependencies still resolve from PyPI. An app
installs the client and the core:

```bash
pip install \
  "overnightworks-agent-client @ https://github.com/overnightworks/agent-providers/releases/download/v0.2.0/overnightworks_agent_client-0.2.0-py3-none-any.whl#sha256=13ef14197d90d3af3b134424ca3493320b7b47f153a2b10c7c6f34ba7718a988" \
  "overnightworks-agent-providers @ https://github.com/overnightworks/agent-providers/releases/download/v0.2.0/overnightworks_agent_providers-0.2.0-py3-none-any.whl#sha256=0bc8090d7d4b412a628d03b8cf2285f1eacf6112ed00028f4acdd4d130654a17"
```

The core pins the client to its own version exactly, so both URLs name the same
tag. An app that needs only the tool contract installs the client wheel alone.

Two optional extras of the core cover the paths that import a heavier
dependency lazily; in URL form they sit between the name and the `@`:

- `[api]` pulls in the Anthropic SDK (`anthropic`) used by the Claude HTTP-API
  route.
- `[image]` pulls in Pillow (`pillow`) used by the Codex image path.

```bash
pip install \
  "overnightworks-agent-client @ https://github.com/overnightworks/agent-providers/releases/download/v0.2.0/overnightworks_agent_client-0.2.0-py3-none-any.whl#sha256=13ef14197d90d3af3b134424ca3493320b7b47f153a2b10c7c6f34ba7718a988" \
  "overnightworks-agent-providers[api,image] @ https://github.com/overnightworks/agent-providers/releases/download/v0.2.0/overnightworks_agent_providers-0.2.0-py3-none-any.whl#sha256=0bc8090d7d4b412a628d03b8cf2285f1eacf6112ed00028f4acdd4d130654a17"
```

A uv host lists the same URLs in its `pyproject.toml`; `uv lock` records each wheel's sha256 in the host's own `uv.lock`, and `uv sync` checks it.

A host that pins a git commit instead of a release resolves the client only
under uv, which reads the workspace source from the repository; pip would look
the client up on PyPI, so a pip-based host pins both wheel URLs.

Tags are never moved; a broken release gets the next patch tag.

## The child environment is closed

Every child this library starts is described by one immutable `ChildProcess`:
an absolute binary, an explicit working directory, and the child's *complete*
environment. Nothing is inherited and nothing is merged onto a host baseline,
because an optional set of extras on an inherited environment is a denylist
seen from the other side. `HOME` is always set — an agent CLI with no `HOME`
falls back to the passwd home, which is worse than a wrong one.

**This is process configuration, not access control.** A closed environment
stops discovery by convention — `HOME`, `PATH`, `CODEX_HOME`, `GROK_HOME`,
`XDG_*` — and nothing else. It does not stop an absolute
`open("/…/auth.json")`, traversal out of the working directory, or the network,
and UID, GID, umask, resource limits, namespaces and mounts are inherited
whatever this library does. A host that needs containment brings the sandbox
described below; this layer only stops the child from *finding* the operator's
credential directory by convention.

The host names each provider credential *file*. Catalog probes copy the needed
file into a private home for their bounded run. Every Claude or Grok turn uses
a fresh private home below
`cli_working_directory_root`: directories are mode 0700 and credential copies
are mode 0400. The source is opened only for reading. A turn's home remains
until its direct child has exited, including a delayed spawn or background
reap; a grandchild that outlives that child is not waited for, and the
synchronous call's timeout kills only the direct child. A process group that
survives SIGKILL for 60 s is logged once, and its reservation and home stay held
until the process exits, when the exit sweep removes the home. A catalog home
lasts for its bounded probe. Each Codex turn uses a fresh private home below
`cli_working_directory_root`, with its blanked login copy at mode 0600. Removal is
requested when the image turn returns or at the tool turn's `aclose`; like a Claude
or Grok turn's home, the home remains until the turn's direct child has exited. A host
test that replaces `codex.protocol.run_cli_bounded` must call `on_reaped` or
`on_spawn_failed`, as the runner does, or the Codex turn home stays until it is
collected. Removal never changes a call's outcome: a home that cannot be
removed is logged by error class alone, without its path. Removal is tried at
`close()`, then by the finalizer, then by one exit-time sweep; after that the copy
stays under the host's root, logged at error level. A direct overwrite of a 0400 copy fails, while a CLI can create and
rename a replacement in its disposable directory; that renewal vanishes at
cleanup. This is not evidence that a CLI refreshes credentials in practice.

## Safe diagnostic codes

The library logs closed diagnostic codes for rejected Grok tool-protocol and
Codex image-gate paths. They identify a stable rejection class without copying
prompts, arguments, paths, IDs, or exception text into logs. The Codex image
route's three codes are `unexpected_event`, `blocked_item` and
`unexpected_item`, logged as
`Codex image tool rejected route=cli code=<code> types=[...]`; a completed turn
that drew nothing is logged as `Codex image not created route=cli types=[...]`.
`types` is the turn's event sequence as closed event and kind *names* from the
gate's own sets, never event contents: anything outside them is `other`,
consecutive repeats read `name*N`, and a long turn keeps its last 32 names
behind a leading `...+K`. `ImageToolBlockedError` describes this library's
image-gate decision and carries the same names as `observed`; it is not an
administrative diagnosis from Codex. The earlier Songmaker #899 evidence
cannot reconstruct the concrete Grok or image cause, so a new provider run is
needed to observe these codes.

## The ports a host supplies

Nothing is configured by default. A host installs one `ProviderRuntimeConfig`
per process with `configure()`, and every module reads it back through
`current_config()`; until then `current_config()` raises rather than guessing a
binary path.

The configuration is cut along the two ways into this library. `ProviderRuntimeConfig`
carries what the catalog path reads — the CLI binaries, the search path a bare
binary name is resolved against, the credential files, and the working root
below which every private directory and temporary file is created. A host that
also runs turns adds a `TurnRuntimeConfig` as `turns`: the
Codex mounts and process caps, the prompt-file names, and an optional
`McpServerSpec` describing the host's MCP server. A catalog-only host passes no
`turns` at all and never invents a value it does not have; a host that runs no
MCP server passes `mcp_server=None` inside `turns`. A host migrates at its pin bump: see [Migrating to 0.3.0](#migrating-to-030).

A turn path reached
without `turns` raises `TurnRuntimeNotConfiguredError` instead of running against a
guess.

`configure()` refuses a `cli_working_directory_root` that is not private: it
must be an absolute path to an existing directory, not a symlink, owned by the
process's effective user, with mode exactly 0700. It raises
`WorkingRootNotPrivateError`, whose `violation` is a member of the closed
`WorkingRootViolation` and whose text never holds the path. Every call
inspects, so an identical re-install does too, and a refusal installs nothing.
Create the root with `install -d -m 0700 <root>`, never `/tmp` itself, in every
process that configures, and keep its owner, mode, path and the process's
effective identity for the process lifetime. The check proves privacy only on a
filesystem with POSIX ownership and modes, and gives no protection against a
hostile replacement after startup.

The other host obligations arrive as arguments where they are needed:

- `ToolExecutor` (`agent_providers.tool_loop`) — the port that runs one tool
  call and returns its `ToolOutcome`. Its docstring states what a failure is,
  what raising means and what the loop logs.
- `ToolCatalog` (`agent_providers.tools`) — the tool declarations, projected to
  each provider's schema, and the host-declared `ToolProtocolMarkup` whose
  brand the text protocol's markers wear.
- `McpServerSpec` (`agent_providers.config`) — how the host's MCP server is
  launched and which tools it exposes.
- `ImagePolicy` (`agent_providers.images`) — the constraints an image turn is
  held to.
- `TextToolCallRejectedError` (`agent_providers.errors`) — a
  `ProviderUnavailableError` a Grok or Codex turn raises when the model's text tool
  call breaks the protocol. Its `rejection` names the rule and where the call
  broke (`offset`, `length`), plus a piece of the refused text of at most 1,024
  code points, masked as a `SecretStr` (`rejection.excerpt.get_secret_value()`).
  `SecretStr` masks display and pydantic's JSON, but not pickle, copy or
  `vars()`, and traceback locals can hold the whole response. The library logs
  the rule and the numbers, never the text, and keeps no copy; whether the text
  is written anywhere is the host's decision.
- The `StreamEvent` union (`agent_providers.events`) — what a turn emits
  in-process, which a host calling the core consumes. The agent service's wire
  is `agent_client.wire`, not this union.

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
| `codex.protocol.run_reserved_codex_cli`, returning the run's outcome | `codex.protocol.run_reserved_codex_cli`, which now also takes the home reservation and the line channel and returns nothing | No known host calls it. It contains every exception, `KeyboardInterrupt` and `SystemExit` included, and publishes a failed outcome on the channel, so call it only on a runner thread, never on the main thread. |

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
random part, below the root, inside the runtime that configures it.

The Codex image gate admits the events Codex CLI 0.163.0 measurably emits for
a cover turn, and no command at all. One real turn on 0.163.0 (16.10.2026)
under a production sandbox profile streamed `thread.started`, `turn.started`,
three `item.completed:agent_message`, `turn.completed`, left one PNG under
`generated_images/` and exited 0: the image call is invisible to
`exec --json`, and no `command_execution` item appears. A command can only add
capability to a turn, so none is admitted, and none is required: a
`command_execution` item ends the turn as `blocked_item`. Requiring the skill
read the older CLI made was a canary, not a guard, and it blocked every cover
on 0.163.0. The supported CLI is 0.163.0, where this was measured; 0.147.0
read the skill through the shell, which is now refused as `blocked_item`, so a
host still on 0.147 must move to 0.163. If a later CLI reads the skill through
the shell again, the logged `types` show `item.started:command_execution`, and
that path is measured before it is admitted. The residual: on 0.163.0 the
gate's block checks cover only the tool calls the CLI *reports*. The image call
itself is not reported, and this measurement does not establish whether other
tools invoked through the same path are reported either. A returned PNG is tied
to the turn only by its location under the private `CODEX_HOME` and by the
re-encode under the host's `ImagePolicy`. The gate is therefore defence in
depth; the containment is the read-only sandbox plus the host's AppArmor and
seccomp profile, which the host owns.

## Development

```bash
uv sync --all-packages --extra dev --extra image
uv run pytest tests -q
uv run ruff check src tests packages
uv run lint-imports
uv build --all-packages
```

The suite runs without a live provider: the CLI processes are faked and the
tool-surface probe is patched. `[image]` is needed at collection time because
one test imports Pillow at module load; the `[api]` path is exercised without
the Anthropic SDK installed, which is the library's implicit proof that it never
imports `anthropic` eagerly.

`uv sync --all-packages` installs the client and the service from the
workspace, so one environment and one suite under `tests/` cover all three
packages. The service's end-to-end proof, `tests/test_service_keycloak.py`,
runs only where `KEYCLOAK_URL` names the test Keycloak of the
`keycloak-token-shapes` CI job. `uv build --all-packages` writes the three
wheels to `dist/`; the release attaches exactly those three.

## Licence

MIT — see [LICENSE](LICENSE).
