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
  nothing imports it.

## What it owns, and what it leaves to the host

It owns the machinery that is the same in every deployment:

| Module | Owns |
|---|---|
| `agent_providers.config` | The deployment facts every module reads, installed once per process via `configure()` |
| `agent_providers.catalog` | Live provider/route readiness probes and the model names each route offers, aliases or ids as the provider lists them (`probe_provider_route`, `list_provider_models`) |
| `agent_providers.dispatch` | Routing one turn to a provider and route, and the cover-image capability |
| `agent_providers.events` | The `StreamEvent` union a turn emits in-process (assistant text, tool call, tool result, final); not the service wire |
| `agent_providers.tool_loop` | The provider-agnostic tool loop driving a `ToolExecutor` over a `ToolTransport` |
| `agent_client.tools` | `ToolDeclaration`: one tool's name, description, JSON-schema arguments and `ToolEffect` |
| `agent_client.wire` | The one owner of the agent service's wire: the `RunRequest` body, the versioned run-event union, the `RunRejection` body and the `RunStatus` body of `GET /runs/{id}` |
| `agent_client.run_stream` | `parse_event` and `RunStream`: the rules a run's event stream keeps, and how a cut-short run ends |
| `agent_client.sse` | `encode_event` and `RunStreamDecoder`: one SSE frame per run event, decoded and checked against the wire and its stream rules |
| `agent_client.capability` | The format of the run capability the service mints and an app's MCP server verifies (A1m): token type `agent-capability+jwt`, at most 8,192 characters, the strict `CapabilityClaims`, and `McpOrigin`, the one decider of an app's canonical https MCP origin |
| `agent_providers.tools` | The `ToolCatalog` and its Anthropic/OpenAI schema projections; re-exports the identical `ToolDeclaration` |
| `agent_providers.spawn` | `ChildProcess` and the only calls in this library that create a child process |
| `agent_providers.process` | `run_cli_bounded`: the bounded runner over a `ChildProcess` (start, stdin, first line, deadline, process-group cleanup) |
| `agent_providers.images` | The `ImagePolicy` an image turn is held to |
| `agent_providers.sandbox` | The directory-name prefix constants a host's kernel sandbox profile must permit |
| `agent_service.app` | `POST /runs` and `GET /runs/{id}`: the order of checks, the typed rejections, the admitted run's event stream, and the run status that only the app and user that started a run may read |
| `agent_service.identity` | The two-token check: the workload token bound to one registered app, the user token to one principal |
| `agent_service.run_request` | The strict read of a `RunRequest` body: strict JSON, then its version, then its shape |
| `agent_service.config` | The service's TOML: identity, listener, run pacing and limits, subscription profiles, purposes and apps, the optional `[signing] key_file` the signing key is read from, each app's optional https MCP origin (canonical, one per app, needing `[signing]`), and the startup preflight that measures every app's capability against 8,192 characters |
| `agent_service.signing` | The service's one signing key: an RSA-3072 PEM (exponent 65537) read once at startup and never made by the service, refused when unsound; its `kid` is the RFC 7638 thumbprint, and `SigningKey.sign(claims, token_type)` writes a typed RS256 token. No capability is minted until A1a4-2b. Generation and rotation: see the A1a4 ruling in `docs/decisions/0001-agent-service.md` |
| `agent_service.capability` | The capability's claims and signing and its 8,192-character guard (`CapabilityTooLarge`), and the startup preflight that measures each app's capability with a subject reserve of 255 quotation marks, `iat = 9_999_999_999` and `exp = floor(iat + max_duration_s)`; the actual signed-token guard remains authoritative. `capability_too_large` (422) answers an oversized capability at admission. No run receives a capability until A1a4-2b |
| `agent_service.routing` | `route`: the one H5 decider between the CLI route, the API route and `not_enabled` |
| `agent_service.run_events` | The mapping of the core's turn events onto the wire frames, ending every run with exactly one terminal; also the run's tool-call and byte budget |
| `agent_service.run_liveness` | When frames go out: the heartbeat, the idle timeout and the maximum duration, enforced by a supervisor that does not depend on the client reading, and drops a client that stops taking frames |
| `agent_service.runners` | The `Runner` port a route's provider plugs into; production installs none yet |
| `agent_service.run_records` | The in-memory record of each run (owner, instants, events accepted, ending, client lost; never event text): readable by its own app and user up to one hour after the run ended, evicted earlier once 10,000 ended records are held, gone after a restart. Nothing records a run yet: recording arrives with A1a2-2, so every authenticated `GET /runs/{id}` answers `not_found` until then |

It leaves the application everything the library cannot know: the MCP tool set
and its execution, the prompts, where settings are stored, and domain
ownership. The core depends on the agent client, `pydantic`, `httpx` and
`anyio` at runtime and on nothing else — no host application, no web framework,
no database; the client depends on `pydantic` only and never imports the core.
Import-linter contracts forbid either package reaching back into
`songmaker_cli` or the sibling `webauth` library and layer the service above
the core above the client; only the service verifies tokens, so only it imports
`webauth`. `lint-imports` proves them in CI.

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
synchronous call's timeout kills only the direct child. A catalog home lasts for
its bounded probe. Removal never changes a call's outcome: a home that cannot be
removed is logged by error class alone, without its path. Removal is tried at
`close()`, then by the finalizer, then by one exit-time sweep; after that the copy
stays under the host's root, logged at error level. A direct overwrite of the copy fails, while a CLI can create and
rename a replacement in its disposable directory; that renewal vanishes at
cleanup. This is not evidence that a CLI refreshes credentials in practice.

## Safe diagnostic codes

The library logs closed diagnostic codes for rejected Grok tool-protocol and
Codex image-gate paths. They identify a stable rejection class without copying
provider events, prompts, arguments, paths, IDs, or exception text into logs.
`ImageToolBlockedError` describes this library's image-gate decision; it is not
an administrative diagnosis from Codex. The earlier Songmaker #899 evidence
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
also runs turns adds a `TurnRuntimeConfig` as `turns`: the chat model, the
Codex mounts and process caps, the prompt-file names, and an optional
`McpServerSpec` describing the host's MCP server. A catalog-only host passes no
`turns` at all and never invents a value it does not have; a host that runs no
MCP server passes `mcp_server=None` inside `turns`. A host migrates at its next
pin bump (both models refuse unknown fields):

| Before (`ProviderRuntimeConfig`) | Now |
|---|---|
| `claude_chat_model` | `turns.claude_chat_model` |
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

A turn path reached
without `turns` raises `TurnRuntimeNotConfiguredError` instead of running against a
guess.

The other host obligations arrive as arguments where they are needed:

- `ToolExecutor` (`agent_providers.tool_loop`) — the callable the tool loop
  invokes to run one requested tool and return its `ToolOutcome`.
- `ToolCatalog` (`agent_providers.tools`) — the tool declarations, projected to
  each provider's schema.
- `McpServerSpec` (`agent_providers.config`) — how the host's MCP server is
  launched and which tools it exposes.
- `ImagePolicy` (`agent_providers.images`) — the constraints an image turn is
  held to.
- The `StreamEvent` union (`agent_providers.events`) — what a turn emits
  in-process, which a host calling the core consumes. The agent service's wire
  is `agent_client.wire`, not this union.

## Sandbox

`agent_providers.sandbox.paths` holds directory-name prefix constants —
`CODEX_IMAGE_TURN_DIRECTORY_PREFIX`, `CODEX_TOOL_TURN_DIRECTORY_PREFIX`,
`CODEX_SANDBOX_PROOF_DIRECTORY`, `CODEX_HOME_DIRECTORY_NAME`. They are the
contract between this package and a host's kernel sandbox profile: every
confined Codex run works inside Bubblewrap with one writable place named by one
of these prefixes. This library ships the constants, not a profile — a host
brings its own AppArmor/seccomp profile that permits exactly those prefixes and
derives it from these values, so a rename becomes a red test rather than a
silently refused mount.

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
