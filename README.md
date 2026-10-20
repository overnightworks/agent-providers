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
| `agent_providers.config` | The deployment facts the catalog and turn paths read, installed once per process via `configure()`, which refuses a working root that is not private; `PrivateWorkingRoot`, a root checked by the same rule at construction, without installing a runtime |
| `agent_providers.catalog` | Live provider/route readiness probes and the model names each route offers, aliases or ids as the provider lists them (`probe_provider_route`, `list_provider_models`); a snapshot also names the Claude CLI's own default (`default_model_label`) |
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
| `agent_providers.spawn` | `ChildProcess` (its `repr` omits the environment field) and the only calls in this library that create a child process. `open_anchored_pipes` returns an `AnchoredChild`, an async child whose exited leader only its own handle reaps, so its process group can be signalled safely until `release()`; its start runs synchronously on the event loop, as asyncio's own door does, and blocks that loop until the child has executed its binary; `AnchoredLeader.kill_and_census()`, also on `AnchoredChild`, sends SIGKILL to the leader's process group and then counts the group's live members in `/proc` (`procfs.group_census`), both inside one hold of the anchor, so every count follows a kill that could reach only this run's group; its `EMPTY` says nothing about a process that left the group with `setsid` or `setpgid`, and it neither publishes nor releases the leader's exit. `AnchoredChild.kill_until_gone(within=)` repeats that kill and count in a worker thread, `GROUP_CENSUS_INTERVAL_SECONDS` (0.1 s) after each live count, and returns a `KillUntilGoneResult` whose `confirmed` is true only for an `EMPTY` count that ended no later than the bound; the bound is measured from just before the first SIGKILL, the first attempt is unconditional (so a zero bound makes one kill and count and practically never confirms on a real clock), no subsequent attempt starts at or after it, and a count that ends after it is reported as it is but never confirms, so the call can return later than the bound by one wait's overrun and one attempt. The host must not reap children it did not start. After a resource failure or an interruption inside `open_anchored_pipes`, during its start or after the start and before the handle is returned, the host stops using the library and ends the process (see **Interruptions**). `open_blocking_anchored_pipes` returns a `BlockingAnchoredChild`, its blocking twin for a caller's own thread, with the same anchor, `kill_until_gone(within=)` on `time.monotonic`, and `wait_for_exit(timeout)` |
| `agent_providers.process` | `run_cli_bounded`: the bounded runner over a `ChildProcess` (start on an anchored leader, stdin, first line, deadline, and a cleanup that SIGKILLs and counts the group on every exit and frees only after a confirmed count); its line channel ends in exactly one outcome before the run returns, `cli_settle_deadline(deadline)` is the one instant by which every wait ends, and a failed step is reported by name and exception class only; `LastHolderRemoval`: one removal, claimed exactly once by the last of a closure and its reservations and run outside its lock, never retried; `run_directories`: one subscription run's private home and separate working directory below a `PrivateWorkingRoot`, removed once after the run and its pool slot have let go |
| `agent_providers.anchored_turn` | `CliProcessPool` (the process-wide slots of one CLI, built with a limit, a name and `ReapBounds`, with no default for any of them) and `anchored_turn_child`: one async turn's child on an anchor that holds a pool slot, reaped behind a shield whatever ends the turn. The reap always sends SIGKILL to the turn's process group and counts it (`AnchoredChild.kill_until_gone`), also after a normal exit; it frees the slot only on the `True` of `lost_groups.release_leader_if_gone`, which needs the group counted empty within the bound and the leader's exit published, and otherwise retains the leader, which keeps its slot and the reservation released with it (a private home or a run's directories), and the turn raises `ContainmentLost` (survivors in the group are then possible). `ReapBounds` refuses a bound that is not finite and non-negative when it is built. |
| `agent_providers.outlasting` | The one owner of what a waiter that must outlast its own cancellation raises. `wait_outlasting_cancellation(task)` waits until the task is done; no cancellation of the waiter, direct or AnyIO's level one, cuts the wait short or reaches the task. It returns the waiter's first direct cancellation and leaves the task's own outcome, a failure or a cancellation, on the task. `level_cancellation()` delivers and returns an AnyIO level cancellation pending on the calling task, or returns `None` at once. `outlast(task)` waits the same way, then raises the task's exception, otherwise the first direct cancellation, otherwise the level one, otherwise returns the task's result. A direct cancellation the task's exception replaces is not retracted (`uncancel()`). The reap of `anchored_turn_child` and `agent_service.run_tools`' close use `outlast`. |
| `agent_providers.cli_run` | `astream_cli_run(root, adapter)`: one subscription run of any provider's CLI in its own `run_directories` on `anchored_turn_child`; stdout lines become events through the adapter's decoder, stderr is drained unkept, and the final answer comes only after the group is confirmed gone and the tree's removal attempted; `CliRunAdapter` is the port; no installed configuration is read; `RelayAccess(url, secret)` is one run's relay as its adapter attaches it (the secret is hidden from `repr`), and `relay_authorization(secret)` is the one form of its `Authorization` value, which the agent service's relay compares; a `RelayAccess` is trusted only as the run's own relay built it, because the CLI sends the header before init, so a host that builds one itself owns the trust in its destination (ADR A1b-5r); `verdict_of` judges a `RunSurface`, what a run's CLI announced at its start, as a `SurfaceVerdict`: only the run's relay and its tools, no other MCP server, no credential source beside the profile's login. A decoder ends the run when it reads a refused announcement, with `RunSurfaceRefused`, which carries only the verdict; `cli_run.read_line` is the one stdout line reader (an over-long line raises `CliStreamLineTooLong`) and `cli_run.settle_drain` the one stderr-drain settlement; the old `claude.provider.acall_claude_with_mcp_stream` uses both |
| `agent_providers.lost_groups` | `retain_lost_group`, `confirm_group_gone` and `release_leader_if_gone`, the one decider of a leader's release (only on a count of empty and a known exit, both passed by the caller): the registry of process groups a reap could not release, and the one attempt that may free their place |
| `agent_providers.transcript` | `flatten_messages` and `stdin_prompt`: the single prompt a CLI turn reads from stdin |
| `agent_providers.secret_file` | How the service and the core read a secret kept in a file: opened once, type, mode (nothing for group or others) and size (16 KiB) taken from that descriptor; refusals name the path and the reason, never the content. A profile token must be one printable line, not an `sk-ant-api` API key and not JSON (a line opening an object or array, or a JSON scalar) |
| `agent_providers.procfs` | `group_census`, one scan of one process group's live members from `/proc`, unavailable unless `/proc` is visibly a procfs of the process's own PID namespace with nothing hidden; `holds_mount_point`, whether a mount table names a mount point at or below a directory |
| `agent_providers.images` | The `ImagePolicy` an image turn is held to |
| `agent_providers.sandbox` | The directory-name prefix constants a host's kernel sandbox profile must permit |
| `agent_service.app` | `POST /runs` and `GET /runs/{id}`: the order of checks (a CLI run takes an admission place before anything is signed), the typed rejections, the admitted run's event stream (`_RunResponse`, which closes the run whenever its response ends), and the run status that only the app and user that started a run may read; an admitted run of an app with an MCP origin hands its runner one capability, and an oversized one answers 422 `capability_too_large` before any runner, stream or record |
| `agent_service.admission` | The service's admission limit: at most N parallel subscription runs (default 5, from `[runs] max_parallel_cli_runs`); a CLI run takes a `Lease` before its run id or signature, one beyond the limit gets 429 `busy` with `Retry-After: 10` and never falls back to the API route; refusals and API runs take nothing, the run's stop releases its lease, as does a response that ends before its run began, and only a lease that still holds its place can free it; a run whose process group is not confirmed gone passes its place to the quarantine as a new lease (`Lease.transfer()`), which only the quarantine releases |
| `agent_service.identity` | The two-token check: the workload token bound to one registered app, the user token to one principal |
| `agent_service.run_request` | The strict read of a `RunRequest` body: strict JSON, then its version, then its shape |
| `agent_service.config` | The service's TOML: `[identity]` (`issuer`, `jwks_url`, `audience`, `subscription_group` and `group_claim` are all required, none has a default), listener, run pacing and limits, at most one subscription profile, only for Claude, with a required `token_file` read once at startup and never shown, the `[cli]` section (the required, service-wide `working_root`, read at load into a `PrivateWorkingRoot`, so a root that is not private stops startup naming only its violation; and one `[cli.<provider>]` table per provider whose version reader is built, today only `[cli.claude]`, with the absolute `binary` and the exact `version` (three integers, as a string) it must report, required by a Claude profile. A table for another provider, and the former flat keys `claude_binary` and `claude_version`, stop startup naming the key), purposes and apps, the optional `[signing] key_file` the signing key is read from, each app's https MCP origin (canonical, one per app, needing `[signing]`, and required when the app lists tools), and the `[mcp] ca_file` (an absolute path to the PEM bundle of the CA that signs the apps' MCP origins, required as soon as any app declares an origin and trusted for those origins alone; an unreadable bundle stops startup naming `mcp.ca_file`), and the startup preflight that measures every app's capability against 8,192 characters |
| `agent_service.process_hardening` | The first startup step: `harden_process` refuses a platform other than Linux and an effective uid 0, then makes the process non-dumpable (`PR_SET_DUMPABLE` 0) before the configuration is read, so a same-UID child cannot open the service's `/proc/<pid>/environ` or `mem`; any refusal is one line and exit 2 |
| `agent_service.cli` | `agent-service --config PATH`: hardening, configuration, version preflight, then installs the CLI runner (`installed_runners`), then `close_transport_loggers`, which sets `httpx`, `httpcore` and every existing `httpcore.*` logger above CRITICAL (children made later inherit it) so no call the dependencies make creates a record; uvicorn's logging configuration leaves it closed, a refused start changes no logging, and the core's HTTP tracing is lost in the service process |
| `agent_service.cli_version` | The pinned programs' startup probe: one output reader per adapter (today Claude's `<x.y.z> (Claude Code)`), and the probe that spawns each pinned program once through the core's `ChildProcess` and `run_catalog_cli`, in one `run_directories` tree below `[cli] working_root`, kept until the probe's spawn fails or its group is confirmed gone. It refuses a start when the program reports another version or none its reader accepts, and it names only how many characters were printed |
| `agent_service.signing` | The service's one signing key: an RSA-3072 PEM (exponent 65537) read once at startup and never made by the service, refused when unsound; its `kid` is the RFC 7638 thumbprint, and `SigningKey.sign(claims, token_type)` writes a typed RS256 token. The key signs each admitted run's capability. Generation and rotation: see the A1a4 ruling in `docs/decisions/0001-agent-service.md` |
| `agent_service.capability` | The capability's claims and signing and its 8,192-character guard (`CapabilityTooLarge`), and the startup preflight that measures each app's capability with a subject reserve of 255 quotation marks, `iat = 9_999_999_999` and `exp = floor(iat + max_duration_s)`; the actual signed-token guard remains authoritative. `capability_too_large` (422) answers an oversized capability at admission; `Capability.appears_in(data)` is the one predicate that decides whether bytes carry the capability, whole or as its signature segment (a split, encoded or reversed token is not detected) |
| `agent_service.routing` | `route`: the one H5 decider between the CLI route, the API route and `not_enabled` |
| `agent_service.run_events` | The mapping of the core's turn events onto the wire frames, ending every run with exactly one terminal, except the cut stream of a dropped client (H6); the terminal is committed after the runner's close, and a containment loss ends the run `failed: provider_error`; also the run's tool-call and byte budget |
| `agent_service.run_liveness` | When frames go out: the heartbeat, the idle timeout and the maximum duration, enforced by a supervisor that does not depend on the client reading, and drops a client that stops taking frames; a bare `ContainmentLost` is reported by the quarantine's one ERROR line, not by the runner-failure or close-failure line; leaving a run raises the closer's cancellation, direct or AnyIO's level one, once the runner's close, the quarantine's hand-over and the record's end are done, and every failure of the ending is then logged once |
| `agent_service.quarantine` | `Quarantine`: at a run's one end, after its runner's pump has ended, the run's admission place passes to the quarantine for every `ContainmentLost` group the pump met; every `QUARANTINE_RETRY_SECONDS` (5 s) it awaits `confirm_group_gone` for each group not yet confirmed (a raising attempt counts as not confirmed), frees exactly that place once every one answered `True` and writes one INFO line; it never gives up, a cancelled retry releases nothing, and the service never exits or signals a group itself. One ERROR line per held run names only the run id and each group's pgid, never an exception, the generation or provider text; an API run has no place, but its groups are retried too |
| `agent_service.runners` | The `Runner` port a route's provider plugs into, and `Runners`, which answers the runner of a route for an app: production installs the CLI runner when `[cli]` is set, which serves every app, with or without tools, and no API runner yet; an admitted run carries its capability |
| `agent_service.cli_runner` | The CLI route's runner: it reads the profile the route names, builds that profile's provider run (only Claude has an adapter; a provider without one cannot have a profile) from the request's system text and messages, the service's `[cli]` program and working root, the profile's login and `[runs] max_tool_calls`. When the run's capability grants tools it first opens the run's tool set (`RunTools`) and hands the adapter only the relay's address and secret; it yields the core's text and ending and the relay's tool events, drops the CLI's own tool lines, and accepts a relay event only after the service asked for the next one; its exit path closes the tool set with the core's reap as the injected reap, and a containment loss from that reap still reaches the supervision. The capability reaches no adapter. The tool set trusts exactly the CA bundle `[mcp] ca_file` names (`mcp_tls_context`, never the environment). Closing the runner closes the core entry at once, so the run's group is reaped and a removal of its directories attempted before the record ends. A Claude run with a relay gets `MCP_TOOL_TIMEOUT` equal to `[runs] max_duration_s` in milliseconds; a final answer is held while a forward still runs |
| `agent_service.run_records` | The in-memory record of each run (owner, instants, events accepted, ending, client lost; never event text): readable by its own app and user up to one hour after the run ended, evicted earlier once 10,000 ended records are held, gone after a restart. The record is written as the run's events are accepted and ended when its runner is closed |
| `agent_service.mcp_wire` | The JSON-only MCP wire profile, pure and free of sockets, clients and the relay: strict JSON-RPC models, the one supported protocol version (`2025-11-25`) and its negotiation, the `MCP-Protocol-Version` header rule (an absent header is refused with 400), the `initialize` result built from constants, `tools/list` with a cursor, `tools/call` read as text with `is_error`, `notifications/cancelled` read as its `requestId` (`CancelledParams`), the `application/json`-only answer rule, 202 with an empty body for a notification, and one closed `McpWireError` that echoes nothing of its input; the writers `error_response` (the `JsonRpcErrorCode` members `METHOD_NOT_FOUND` -32601 and `INVALID_PARAMS` -32602, closed messages, only the id echoed), `tools_list_response` (kept definitions by alias, a `None` description omitted, no `nextCursor`) and `tool_result_response` (a `tools/call` result written by alias); a body over `MAX_EVENT_BYTES` and a batch array are refused |
| `agent_service.app_tools` | The service's client of an app's MCP endpoint over the JSON-only profile: one session per run, opened with the run's `Capability` as an object (the token is read once, by a private `httpx.Auth`, when the `Authorization` header is written), that handshakes, lists tools with bounded pagination and calls one tool once; TLS is verified through a caller-supplied `ssl.SSLContext`, answers are read identity-only and bounded at `MAX_EVENT_BYTES`, and wire, protocol, HTTP-status and transport failures leave as closed `UpstreamError` members that name no URL, header, body or `httpx` object: `UpstreamRedirected` (any 3xx, never followed), `UpstreamRefused`, `UpstreamProtocolError` (an event-stream answer is refused at its headers), `UpstreamUnreachable` (connect, TLS or pool: no byte was sent) and `UpstreamCallLost` (any other `httpx.HTTPError`: the call may have run and is never re-sent); an `httpx` failure when the client closes leaves as its closed error (`UpstreamCallLost` for a `CloseError`) without its request, unless an exception is already leaving; the client sends POST only, with no GET, no DELETE and no read deadline |
| `agent_service.tool_relay` | The per-run loopback relay: an `asyncio` listener on `127.0.0.1` with `h11` that judges every request head in order (framing, `Host`, `Origin`, the per-run bearer secret, path, method), answers each refusal empty, keeps the connection after a bodyless 404 or 405 only where `h11` allows another cycle, refuses a body over `MAX_EVENT_BYTES` (declared or counted) with 413 and invalid body framing with `h11`'s status, hands an admitted request's whole body and `MCP-Protocol-Version` to the `RelayOwner` port and writes its `HttpAnswer` as given (an owner failure answers an empty 500), and keeps the connection after a fully consumed answer unless the request must close (a request with both `Transfer-Encoding` and `Content-Length` always does); serves at most `MAX_RELAY_CONNECTIONS` (4) connections and aborts any further one at once, aborts a request head not complete within `HEAD_RECEIVE_SECONDS` (5 s, from the phase start or the first byte) and a body not complete within `BODY_RECEIVE_SECONDS` (10 s, from the head) on the run's `RunClock` without a response, leaving idle keep-alive connections, response writes and the owner's time unbounded, and closes in one shared attempt bounded by `RELAY_CLOSE_SECONDS` (1 s): `close()` returns when every relay task ended and otherwise raises `RelayCleanupIncomplete` with the outstanding tasks still tracked, so the next `close()` retries; a connection accepted while closing is aborted and never served; `McpRelayOwner` is its first `RelayOwner`: it refuses a CLI response, error or batch with 400, exempts only `initialize` from the version-header rule (so `server/discover` with `2026-07-28` and any headerless request get 400), answers `initialize`, `ping` and every notification from constants, answers an unknown method with -32601, serves `tools/list` narrowed to the capability's `tools` claim in the offer's order (a repeated allowed name raises `RepeatedToolOffer` at construction), answers a `tools/call` for a name it did not list with -32602 and never hands it on, hands every other parsed `tools/call` to the `ToolCalls` port, and passes every answer through one exit that refuses an owner already refused and checks the body with `Capability.appears_in`; a hit drops the body, refuses the owner for good and answers an empty 500 with one content-free log line; the relay installs no signal handler and logs by class only an owner failure and an unexpected connection failure, and nothing about a request; `ToolCallBarrier` is its `ToolCalls`: each `tools/call` waits as one pending call under the relay-minted id `call-<n>`; the runner hands pending calls out one at a time in arrival order (`next_event()`), and `accept(event)`, once the service's resume confirmed acceptance, forwards exactly that call through `AppTools.call_tool` as the task `upstream:<run_id>:call-<n>`, which a cancelled request never cancels and `settle()` waits for; a duplicate pending id, a call beyond `MAX_PENDING_TOOL_CALLS` (`MAX_RELAY_CONNECTIONS - 1`, so 3; it reserves no connection, because idle ones count toward the cap), and every call after `stop()` are answered -32602 at once and never forwarded; a name the owner did not list is answered -32602, never reaches the barrier's `call`, and the run then ends `provider_error` (`UnofferedToolCalled`); an owner may answer `Unanswered`, which closes the request's connection without a byte; `McpRelayOwner` routes `notifications/cancelled` to `ToolCalls.cancel`, which matches pending ids only (every notification still gets 202); a call the CLI cancelled, or whose request the relay's close ended, is given up before the cancel or the task cancellation returns, frees its pending slot and is never forwarded unless it already was: a waiting one is never handed out, a handed-out one is never forwarded and its `accept` makes `next_event()` return an `is_error` `ToolResultEvent` with `CALL_GIVEN_UP_TEXT`, and a forwarded one keeps running upstream while only its waiter ends; `settle()` returns once every forward's outcome was retrieved exactly once by its one owner; while the owner works, the listener watches the connection: an end (FIN, half-close, RST, or more than `MAX_PIPELINED_BYTES` retained unparsed bytes, one further request) cancels a pending answer in the step it is seen and writes nothing, also after pipelined bytes, which otherwise stay the next request in order; an answer that completed before is written, and the connection then ends; for a `tools/call` the cancel is the barrier's give-up, so a call the CLI closed is never forwarded unless it already was, even when it is accepted before the listener resumes; a forward's outcome becomes the call's one `ToolResultEvent`, retrieved exactly once by one completion owner (an upstream failure as `CALL_FAILED_TEXT`, logged once by class; a cancelled forward yields none and closes a waiting request without a byte), unless its decoded text carries the capability, which yields no event and no answer and makes `next_event()` raise `CapabilityReflected`; `next_event()` hands results out like calls, after a pending given-up result in the order events became ready, and every event stays handed out until `accept` takes exactly it (same kind and id); `has_ready_event()` is true while a queued event, a ready call not given up, an unoffered call or a reflected outcome waits, and makes no promise after `stop()` or while an event is handed out (where `next_event()` raises `ToolCallsMisused` at once), assuming the merger accepts the handed-out event before it asks for the next and production stops the barrier only at the close; the run's merged event source hands a ready core event on only while it is false, and a ready final also only while `has_running_forward()` is false; `call()` awaits one response future from admission to its answer, so a give-up is recorded at once in every phase, and a result's answer exists only when `accept` takes that result while the CLI still waits; a give-up withholds only the answer; `stop()` refuses at once every admitted call whose answer was not released, a running forward included, which keeps running upstream; a call holds its pending slot until its `call()` returns: released, refused or given up (`cancel()` frees it at once; a forward's completion alone never does), and a cancelled call never releases a result; `stop_listening()` closes the listening socket ahead of the connections and `abort_forwards()` cancels every running forward, both `run_tools`' close steps; a capability hit on any answer is reported to the calls (`reflected()`) and ends the run like a reflected result; `has_running_forward()` is true while a forward runs upstream; `cli.main` serves tool apps through `CliRunner` |
| `agent_service.run_tools` | A run's tool set and its one close: `RunTools.open` enters `open_app_tools`, lists the app's tools and starts the relay with `McpRelayOwner` over a `ToolCallBarrier` (a failure after the client opened leaves it within `UPSTREAM_ABORT_SECONDS` and propagates); `close(reap)` stops the listener and the barrier, awaits the injected reap to its own bound, cancels the forwards and waits for `settle()` within `UPSTREAM_CLOSE_SECONDS` (2 s), leaves the app's client within `UPSTREAM_ABORT_SECONDS` (0.5 s) and closes the relay within `RELAY_CLOSE_SECONDS` (1 s), each deadline read on the run's `RunClock` when its step starts; a step that fails or overruns is logged once by class (`UpstreamCleanupIncomplete`, `RelayCleanupIncomplete`), its task is cancelled once and never awaited past the bound, and the close goes on; the reap's exception (`ContainmentLost`) is raised after the last step, never logged; a caller's cancellation, direct or AnyIO's level one, is outlasted and delivered after the close unless the reap's exception wins; a returned close means each step's bounded attempt finished, not that a stalled cleanup's socket is closed; its production caller is `agent_service.cli_runner` |

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
| working root | The host's private directory below which the library creates every private directory and file: `cli_working_directory_root` for the catalog and turn paths, a `PrivateWorkingRoot` for a subscription run's `run_directories`. |

## Install

No distribution is published on PyPI, and PyPI is not planned: wheels pinned by
URL and hash from the tag's GitHub release give byte-exact, hash-checked wheels
with no PyPI name to race for; their dependencies still resolve from PyPI. An app
installs the client and the core:

```bash
pip install \
  "overnightworks-agent-client @ https://github.com/overnightworks/agent-providers/releases/download/v0.3.0/overnightworks_agent_client-0.3.0-py3-none-any.whl#sha256=546b93895f0545ff3ac3d1b819a439b5ab8a132346e5db81984f8078e99d5cbf" \
  "overnightworks-agent-providers @ https://github.com/overnightworks/agent-providers/releases/download/v0.3.0/overnightworks_agent_providers-0.3.0-py3-none-any.whl#sha256=373aed1ca02c670dd7c6f2755c6863210393804dd8b11f755af4724516753146"
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
  "overnightworks-agent-client @ https://github.com/overnightworks/agent-providers/releases/download/v0.3.0/overnightworks_agent_client-0.3.0-py3-none-any.whl#sha256=546b93895f0545ff3ac3d1b819a439b5ab8a132346e5db81984f8078e99d5cbf" \
  "overnightworks-agent-providers[api,image] @ https://github.com/overnightworks/agent-providers/releases/download/v0.3.0/overnightworks_agent_providers-0.3.0-py3-none-any.whl#sha256=373aed1ca02c670dd7c6f2755c6863210393804dd8b11f755af4724516753146"
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
are mode 0400. The source is opened only for reading. A Codex or Grok turn's home on the bounded runner remains until its group is confirmed gone (below), including a delayed spawn or background reap. The synchronous `call_claude` turn's timeout kills only the direct child. The async Claude turns
on the CLI route (`acall_claude` without an `api_key`, which uses the API
otherwise, `acall_claude_with_mcp` and `acall_claude_with_mcp_stream`) run on a
`spawn.AnchoredChild`: when the turn ends, its reap sends SIGTERM to the leader's
process group if the leader still runs and waits up to 1 s for its exit. Then, on
every exit, a normal one included, it sends SIGKILL to the group and counts its
live members in `/proc` until none is left or 5 s from that first SIGKILL have
passed. The turn's core pool slot and home are freed only after a count that
found the group empty within those 5 s, the leader's exit published (awaited for
at most another 5 s) and the leader released. After such a successful
containment, no member of the group is alive when the turn returns or raises its
own error, a descendant that ignores SIGTERM or whose parent ended first
included; a process that left the group with `setsid` or `setpgid` is not
counted and keeps running. Otherwise the reap retains the group and the turn
raises `ContainmentLost(group)`, and survivors are then possible: the library
keeps ownership of a group it has not shown empty. A group is retained when a
member is still alive 5 s after the first SIGKILL, when its count ended after
those 5 s (a late empty count confirms nothing), when `/proc` cannot count it (no
procfs, another PID namespace, hidden pids, an unreadable `/proc` or a leader
`/proc` does not show), when the leader's exit is not published within 5 s after
an empty count, when its anchor was lost, when the exited leader could not be
released because the kernel did not reap it, or when signalling, counting or
releasing failed with another error. The slot and home stay held until
`confirm_group_gone(group)` returns `True` or the library process exits, when the
exit sweep removes the home. The 1 s and 5 s are the Claude pool's `ReapBounds`
(`CLI_TERMINATION_GRACE_SECONDS`, `CLI_GROUP_CONFIRMATION_SECONDS`). `acall_claude_with_mcp_stream` ends its stderr drain before that reap starts, also when the host closes the stream early with `aclose` after a partial read.
A bounded run (`process.run_cli_bounded`) sends SIGTERM to a leader that still runs and waits up to 1 s, then on every exit SIGKILLs its group and counts it in `/proc` until none is left or 5 s from that first SIGKILL have passed. It calls `on_reaped` only after an empty count within those 5 s, the leader's exit published (within another 5 s) and the leader released by `lost_groups.release_leader_if_gone`. Otherwise it keeps the unreaped leader and kills and recounts it in the background for up to 60 s; a group still not confirmed, or one `/proc` cannot count, is logged once, and its reservation and home stay held until the process exits, when the exit sweep removes the home. `cli_settle_deadline(deadline)` is the deadline plus 11.1 s. A catalog home
stays until its probe's process group is confirmed gone, as a turn's home does, including a delayed spawn, an overran settle or a background reap. A resource failure after the start and before the door returns the handle is the exception: it frees the home beside a running child, and is a host obligation (see **Interruptions**). Each Codex turn uses a fresh private home below
`cli_working_directory_root`, with its blanked login copy at mode 0600. Removal is
requested when the image turn returns or at the tool turn's `aclose`; like an async Claude
turn's or a Grok turn's home, the home remains until the turn's process group is confirmed gone. A host
test that replaces `codex.protocol.run_cli_bounded` must call `on_reaped` or
`on_spawn_failed`, as the runner does, or the Codex turn home stays until it is
collected. Removal never changes a call's outcome: a home that cannot be
removed is logged by error class alone, without its path. Removal is tried at
`close()`, then by the finalizer, then by one exit-time sweep; after that the copy
stays under the host's root, logged at error level. A direct overwrite of a 0400 copy fails, while a CLI can create and
rename a replacement in its disposable directory; that renewal vanishes at
cleanup. This is not evidence that a CLI refreshes credentials in practice.

The permission repair that makes a child's read-only
directories removable never follows a child-controlled symlink and, once a
directory is acquired, acts on that acquired object; it promises no containment
against concurrent same-user relocation. A home whose own path was replaced by a symlink is logged and left. The repair
needs Linux's `O_PATH` and a mounted `/proc/self/fd`, which import and a removal
that needs no repair do not.

A subscription run's directories come from `process.run_directories(root, files)`, a context manager that reads no installed configuration. `root` is a `config.PrivateWorkingRoot`, whose privacy is checked when it is constructed; the names in `files` are checked when the context is entered, before anything is created. Below the root the run gets one tree, `agent-run-XXXXXXXX/`, holding `home/` (the child's `HOME` and `TMPDIR`) and a separate, empty `work/` (its working directory), each mode 0700 whatever the umask. Each entry of `files`, a single-component name and its bytes, is written exclusively and whole into `home/` at mode 0600. A file may carry a secret, so no name, byte or path of one appears in a log line or an error text. The modes keep a run's files from every other user; they do not separate it from another process of the same user or from root. An invalid name raises `ChildProcessSpecificationError` before anything is created. After a tree exists, a setup failure makes one removal attempt; a setup `OSError` then becomes `CliEnvironmentUnavailableError` with `HOME_NOT_CREATED`, unchained and without a path, and any other exception propagates unchanged. The tree is removed by one attempt once the context has ended and no reservation from `RunDirectories.reserve()` still holds it; the attempt is claimed exactly once, and a failed removal never makes it claimable again. `anchored_turn_child` takes such a reservation as its `home_reservation` and releases it with the turn's pool slot, so the directories of a retained group stay until `confirm_group_gone(group)` returns `True`. The removal never follows a link and repairs a read-only directory as a home's removal does. Before it deletes anything it reads the mount table once, and it refuses a tree with a mount point at or below it, or a table it cannot read, because a recursive delete would cross into the mounted filesystem. That is detection at one instant: the host must keep the mount topology at and below the working root unchanged for the whole of every removal, and, as for the permission repair above, the removal promises no containment against concurrent same-user relocation. The agent service's container is required to meet this with a working root that is its own tmpfs, a non-root user without capabilities, and nothing mounted below the root or propagated into it while the service runs; that is a deployment condition, not something the library checks. A tree that cannot be removed, or whose removal is refused, is logged once at error level by class or reason, never with its path; it raises nothing, holds no later run and is never retried: there is no finalizer and no exit sweep, so it stays under the root until the host clears the root (the agent service's root is a tmpfs that empties at restart). An asynchronous interruption of the kind the Interruptions obligation below describes, arriving between the removal's claim and its call, can leave the tree with neither an attempt nor a log line, and nothing retries or sweeps it; the obligation (stop using the library and end the process) applies. A setup exception and a task cancellation are not such interruptions: each still makes its one attempt.

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

Nothing is configured by default. A host that runs the catalog or turn paths
installs one `ProviderRuntimeConfig` per process with `configure()`, and those
paths read it back through `current_config()`; until then `current_config()`
raises rather than guessing a binary path. The subscription run the agent
service drives (`cli_run.astream_cli_run(root, adapter)`, with `claude.provider.ClaudeRun` as Claude's adapter, in directories from `process.run_directories` below a `config.PrivateWorkingRoot`) takes its facts as explicit
arguments instead and reads no installed configuration. A Claude run given a relay (`ClaudeRun(relay=RelayAccess(...), tool_timeout_seconds=...)`) sets `tool_timeout_seconds` (required, finite and positive) as `MCP_TOOL_TIMEOUT` in milliseconds (rounded up) and writes its MCP config as the run file `relay-mcp.json` in `home/`. The config names only the server `relay`, with its http URL and `Authorization` header. The run passes the file by path with `--mcp-config`, together with `--allowedTools mcp__relay__*`, and the secret is in no argument, environment variable, stdin line or `repr`.

The configuration is cut along the two ways into this library. `ProviderRuntimeConfig`
carries what the catalog path reads — the CLI binaries, the search path a bare
binary name is resolved against, the login sources, and the working root
below which the catalog and turn paths create every private directory and temporary file. A provider
the host runs through its CLI has its own block: `claude` holds
`ClaudeCliRuntime(binary, binary_search_globs, token_file)` and `grok` holds
`GrokCliRuntime(binary, token_file)`, and `codex` holds
`CodexCliRuntime(binary, token_file)`; a host that does not run a provider
leaves its block `None`. `token_file` is the provider's login
source; the provider decides its format (Grok copies its `auth.json` whole, Codex
copies its `auth.json` blanked).
A Claude, Grok or Codex call under a configuration without its block raises `ProviderNotConfiguredError(provider, block)` naming
the provider, in turns and in the catalog alike; the catalog never reports it as
not logged in. A provider with an API key is ready without its block, and its
CLI is not looked for. A host that
also runs turns adds a `TurnRuntimeConfig` as `turns`: the
an optional `McpServerSpec` describing the host's MCP server, the Codex mounts
and process caps in `turns.codex`
(`CodexTurnRuntime(code_mode_host_binary, resources_directory,
max_concurrent_processes, max_concurrent_image_runs)`), and the Grok prompt-file
names in `turns.grok`
(`GrokTurnRuntime(prompt_file_prefix, prompt_file_placeholder)`); a Codex or Grok
turn without its block raises `ProviderNotConfiguredError("codex", "turns.codex")`
or `ProviderNotConfiguredError("grok", "turns.grok")`. A Claude-only host sets
neither block. A catalog-only host passes no
`turns` at all and never invents a value it does not have; a host that runs no
MCP server passes `mcp_server=None` inside `turns`. `McpServerSpec.config_file_prefix` and `GrokTurnRuntime.prompt_file_prefix` are file-name stems of 1 to 64 characters from `A-Z a-z 0-9 . _ -`: the library appends random characters and creates the file directly in `cli_working_directory_root`. A value outside that set, including any path separator, is refused when the model is built, and the error's rendered text never holds it (`ValidationError.errors()` and `.json()` still do unless called with `include_input=False`). For every field of `TurnRuntimeConfig` and `ProviderRuntimeConfig` the rejected input payload is hidden as well; the location of the error stays. The check guards the configured values only: a caller of `process.run_cli_bounded` that passes its own `prompt_file_prefix` owns that prefix. A host migrates at its pin bump: see [Migrating to 0.4.0](#migrating-to-040) and [Migrating to 0.3.0](#migrating-to-030).

A turn path reached
without `turns` raises `TurnRuntimeNotConfiguredError` instead of running against a
guess. These errors and `ProviderRuntimeNotConfiguredError` share the base
`HostWiringError`, and `dispatch.stream_turn`, `dispatch.call_provider_once` and
`dispatch.image_capability` raise them unchanged instead of
reporting `ROUTE_FAILED`, so a host sees which part of its wiring is missing.

`configure()` refuses a `cli_working_directory_root` that is not private: it
must be an absolute path, written without a `..` component, to an existing
directory, not a symlink, owned by the process's effective user, with mode
exactly 0700. A `..` is refused even where it would resolve harmlessly, because
the temporary-file functions return the path with `..` collapsed textually, and
after a symlinked component that is not the directory the kernel wrote to. Write
the root as its resolved path. It raises
`WorkingRootNotPrivateError`, whose `violation` is a member of the closed
`WorkingRootViolation` and whose text never holds the path. Every call
inspects, so an identical re-install does too, and a refusal installs nothing.
Create the root with `install -d -m 0700 <root>`, never `/tmp` itself, in every
process that configures, and keep its owner, mode, path and the process's
effective identity for the process lifetime. The check proves privacy only on a
filesystem with POSIX ownership and modes, and gives no protection against a
hostile replacement after startup. `PrivateWorkingRoot(path)` runs the same
inspection at construction and raises the same error, so a host that installs
no runtime checks its root once at startup; it is checked at construction, not
kept private, and the same obligations hold.

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
- `ContainmentLost` and `RetainedGroup` (`agent_providers.errors`) — an async Claude turn on the CLI route raises `ContainmentLost(group)` when its reap cannot show the turn's process group gone and release its leader, in one of these cases: a member of the group was still alive 5 s after the first SIGKILL, or its count ended after those 5 s (expiry); `/proc` could not count the group (uncountable); the leader's exit was not published within 5 s after an empty count (publication timeout); the leader's anchor was lost, because something else reaped it (identity loss); releasing the exited leader failed because the kernel had not reaped it (failed leader release); or signalling, counting or releasing failed with another error, which is then the `__cause__` (reap failure). In every case members of the group may still be alive, and the library keeps ownership of the group. It is a plain `Exception`, neither a `ProviderUnavailableError` nor an `UnavailableError`; after a turn error it carries that error as `__context__`. The library logs nothing for it. `group` names this one retention: its `pgid`, and a generation valid only in the process that minted it, so never persist it, pass it to another process, or use it after a restart. In every case the core pool slot and the home stay held. Whether the leader is still unreaped and its pipes still open depends on the case: an identity loss found while the exited leader is being released has already closed the three pipes. Call `agent_providers.lost_groups.confirm_group_gone(group)` until it returns `True`: a coroutine awaited on the loop that opened the child, or on any loop after that one closed. A call admitted as a new attempt while the leader's release has not yet succeeded sends one SIGKILL to the group and counts it once in `/proc`, in its own worker thread. It releases the leader and runs the slot callback only if the census is EMPTY and the leader's exit is published; otherwise it returns `False`. Once the leader's release has succeeded, a failing slot callback is retried alone by the next admitted attempt, with no signal and no count. A call for an unknown or already confirmed group returns `True` and one made while another attempt for the same group runs returns `False`, and neither signals. `False` or an exception means "not confirmed", and the slot and home stay held. A group whose anchor was lost raises `ChildIdentityLost` on every call and stays held until the library process exits. If it never returns `True`, restart the container to stop the remaining run processes. `shutdown_tool_surface_background_tasks()` leaves a retained group as it is.
- The `StreamEvent` union (`agent_providers.events`) — what a turn emits
  in-process, which a host calling the core consumes. The agent service's wire
  is `agent_client.wire`, not this union.

**Interruptions.** Python can raise an exception asynchronously inside a library call: a signal handler raises one between two bytecode instructions of the main thread, as the default `SIGINT` handler raises `KeyboardInterrupt`, and `PyThreadState_SetAsyncExc` injects one into any thread. Python's documentation says that most code, the standard library included, cannot be made robust against this ([Note on Signal Handlers and Exceptions](https://docs.python.org/3/library/signal.html#note-on-signal-handlers-and-exceptions)), and this library is no exception. A host that needs orderly shutdown or continued service uses signal handlers that do not raise for the whole time library calls can run, its startup included, and never injects an exception into a thread that runs library code. After such an interruption, stop using the library and end the process: a later call may wait forever on a lock the interruption left held, and graceful shutdown may stall. A resource failure such as `MemoryError` inside `start_anchored_leader`'s ownership handoff or rollback likewise requires the host to stop using the library and end the process. Normal interpreter exit attempts the private-home cleanup without taking any of this library's locks, but the cleanup is not guaranteed when shutdown cannot complete or a removal fails.

## Migrating to 0.4.0

A host migrates at its pin bump to 0.4.0; the configuration model refuses
unknown fields, so a host that still sets a removed name fails at startup and
the error names the field.

| Before | Now |
|---|---|
| `claude_cli_binary` | `claude.binary` |
| `claude_cli_binary_search_globs` | `claude.binary_search_globs` |
| `claude_cli_auth_file` | `claude.token_file`, still the credentials file until the setup-token contract lands |
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
| `dispatch.stream_turn` and `dispatch.call_provider_once` raised `ProviderUnavailableError` with `ROUTE_FAILED` when a provider block, `turns`, or the configuration itself was missing | both raise `ProviderNotConfiguredError(provider, block)`, `TurnRuntimeNotConfiguredError` or `ProviderRuntimeNotConfiguredError` unchanged, all `config.HostWiringError`s; a host that caught `ProviderUnavailableError` for a wiring fault catches `HostWiringError` too |
| `dispatch.image_capability` for the Codex CLI route always answered with the Codex fields set | it raises `ProviderNotConfiguredError("codex", block)` when `codex` or `turns.codex` is missing |
| `process.run_cli_bounded` read the Grok prompt-file prefix itself | the caller passes `prompt_file_prefix` together with `prompt_file_bytes` and `prompt_file_arg_index`; the Grok transport passes `turns.grok.prompt_file_prefix` |
| `configure()` and `PrivateWorkingRoot(path)` accepted a root written with a `..` component | both raise `WorkingRootNotPrivateError` with `WorkingRootViolation.PARENT_COMPONENT` (`"parent_component"`); write the root as its resolved path |
| `McpServerSpec(config_file_prefix=...)` and `GrokTurnRuntime(prompt_file_prefix=...)` accepted any non-empty string, an absolute path, one with `/`, a space or a non-ASCII letter included | both raise pydantic's `ValidationError` naming the field, without the value in its rendered text, unless the prefix is 1 to 64 characters from `A-Z a-z 0-9 . _ -` (a bare `.` or `..` stays accepted). Use a plain stem such as `notes-mcp-`; the file is created in `cli_working_directory_root` whatever the old value pointed to. `TurnRuntimeConfig` and `ProviderRuntimeConfig` errors also no longer display the rejected input payload of any field (its location stays). No known host uses a refused value. |
| `process.HomeReservation` | `process.Reservation`, the one-method protocol a runner's end releases; a pool slot (`anchored_turn.CliProcessReservation`) is one too; no alias remains |
| `process.PrivateCredentialHomeReservation` | `process.PrivateHomeReservation`, the reservation of the base `PrivateHome`; no alias remains |
| `spawn.open_pipes(child, stderr=)` returned a `subprocess.Popen` (`spawn.PipedProcess`) | `spawn.open_blocking_anchored_pipes(child, stderr=)` returns a `BlockingAnchoredChild`, with no `wait`, `poll` or `communicate`: wait with `wait_for_exit(timeout)`, signal the group with `signal_group(sig)` or `kill_until_gone(within=)`, and reap only with `release()` once the exit is published; never reap its pid yourself (no known host calls it) |
| `process.cli_settle_deadline(deadline)` was the deadline plus 2.1 s | plus 11.1 s, `process.cli_settle_margin_seconds()`; `CLI_PROBE_CALLER_TIMEOUT_SECONDS` grows by 9 s |
| `on_reaped(pid, True)` also followed a background reap whose group check failed | `on_reaped` follows only a confirmed count; an unconfirmed group keeps its reservation until the process exits |
| `codex.protocol.run_reserved_codex_cli(...)` looked the process pool up itself | `codex.protocol.run_reserved_codex_cli(process_pool, reservation, home_reservation, child, *, stdin_payload, deadline, channel)` takes the `CodexProcessPool` that issued the reservation as its first argument; pass that pool, so one turn reserves and reaps on one reference (no known host calls it) |
| `claude.provider.acall_claude_with_mcp_stream` answered from its assistant text when the CLI ended with exit 0 and no result line | it raises `UnavailableError`, and `claude.adapter.stream_claude_turn` raises `ProviderUnavailableError` with `CLI_PROTOCOL_ERROR`. Handle the failure like any other CLI failure of the turn; no `FinalEvent` is synthesized, and previously streamed events may already have reached the caller |
| `claude.provider.acall_claude_with_mcp_stream` raised a bare `ValueError` for a CLI output line over its 4 MiB limit, a stderr read error escaped it, and the end of the turn waited for stderr to close | an over-long line raises `UnavailableError`, with no part of the line in the message, a stderr read error no longer fails the turn, and the end of the turn no longer waits for stderr to close. Catch `UnavailableError` (or let `stream_claude_turn` translate it) and drop any `ValueError` handler written for this |

`[runs] max_duration_s` is refused above 2,147,483 seconds (about 24.8 days), a conservative whole-second limit under the CLI's tool timer; the startup error names `runs.max_duration_s`.

The agent service's `[cli]` section (exit 2 naming the field until migrated):

| Before | Now |
|---|---|
| `[cli] claude_binary` | `[cli.claude] binary` |
| `[cli] claude_version` | `[cli.claude] version` |
| (absent) | `[cli] working_root`, required, a private directory (mode 0700, owned by the service user, not a link, written without `..`) |
| (absent) | `[mcp] ca_file`, required once any app declares `mcp_origin`: an absolute path to the PEM bundle of the CA that signs the apps' MCP origins; the service trusts no other CA for them |

### Dependencies

| Before | Now |
|---|---|
| `uvicorn>=0.30,<1` (agent-service) | `uvicorn>=0.32.1,<1`; uvicorn 0.30.0 to 0.32.0 never tell a streaming app that its client left, so a run would not stop when the client does; a host on an older uvicorn upgrades it with the pin |

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
| `process.run_cli_bounded` takes the argument vector (a sequence of strings) and the keywords `cwd`, `extra_env`, `cleanup_margin_seconds` and `unset_env` | `process.run_cli_bounded` takes a `spawn.ChildProcess` and the four keywords are gone | Build `ChildProcess(binary=Path(argv[0]), arguments=tuple(argv[1:]), ...)` with an absolute executable, the complete `environment` with `HOME` (it inherits nothing, so name every variable the child needs and omit what `unset_env` removed) and an absolute `working_directory`. `prompt_file_arg_index` now indexes `child.arguments`, which excludes the binary, so an old index of 3 becomes 2. A test that replaces `codex.protocol.run_cli_bounded` receives the `ChildProcess` as its first argument and must still call `on_reaped` for a reaped child or `on_spawn_failed` for a failed spawn, as the bounded-runner section above says: `kwargs["extra_env"]` becomes `child.environment`, `kwargs["cwd"]` becomes `child.working_directory`, and reads of the command vector become `child.command`. Songmaker sites: `tests/test_cover_runner.py:398, 460`; `tests/test_cover_suggestion_job.py:174, 357, 387, 432, 584, 607, 636, 663`, plus the reads at `:154` and `:225`. |
| `process.run_cli` | removed | Run a catalog or probe CLI through `process.run_catalog_cli` with a `ChildProcess`; it keeps the probe budget and the `CliRun` or `None` style outcome. Take `process.run_cli_bounded` only to own its options (`stdin_payload`, `read`, `deadline`) and its different result. No known host calls `run_cli`. |
| `process.scrubbed_env` | removed; a child gets a closed environment, so there is nothing to scrub | Delete any call and name what the child needs in its `ChildProcess`. No known host calls it. |
| `process.claude_cli_login` takes `binary` as `str` or `None`, and `None` returns `LOGGED_OUT`; `process.grok_cli_status` and `process.codex_cli_login` read a missing binary as logged out | `process.claude_cli_login` takes `binary` as an absolute `Path` or `None`, and `None` raises `CliEnvironmentUnavailableError`; `process.grok_cli_status` and `process.codex_cli_login` raise `CliEnvironmentUnavailableError` (`BINARY_MISSING`) for a missing binary | Pass `Path(binary)` with an absolute path to `claude_cli_login`, and catch `CliEnvironmentUnavailableError` around all three calls where logged out stood for a missing binary. |
| `claude.provider.verify_cli_tool_surface`, `claude.provider.averify_no_builtin_cli_tools` and `claude.provider.verify_no_builtin_cli_tools` return the binary path as `str` | `claude.provider.verify_cli_tool_surface`, `claude.provider.averify_no_builtin_cli_tools` and `claude.provider.verify_no_builtin_cli_tools` return it as `Path` | Keep the `Path` for filesystem and process APIs, and use `str(binary)` where a string is required (string methods, JSON). |
| `text_tool_protocol.TextToolProtocolError` takes no arguments | `text_tool_protocol.TextToolProtocolError` requires `rejection`, a `TextToolRejection` | Code that only catches it needs no change. A fake or a direct construction supplies a `TextToolRejection`, preferably obtained by driving the real parser on a malformed response. |
| `codex.image.ImageToolBlockedError` takes an optional message, which fills `args` and `str(error)` | `codex.image.ImageToolBlockedError` requires `reason`, an `ImageToolBlockedReason`, and its `args` and `str(error)` are empty | Existing catches stay valid. A direct construction or a fake supplies an `ImageToolBlockedReason`, and a consumer reads `.reason` instead of the exception text. |

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
