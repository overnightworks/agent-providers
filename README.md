# agent-providers

The reusable agent provider layer: it routes a turn to Claude, Grok or Codex —
over each provider's CLI or its HTTP API — and owns the machinery a host
application should not rebuild. Live model catalogs and readiness probes, the
streaming turn transport, the tool loop, a bounded subprocess runner, and the
sandbox directory-name constants all live here; everything that touches a host's
own truth reaches the library through the ports the host supplies.

Distribution `overnightworks-agent-providers`, import package `agent_providers`.

## What it owns, and what it leaves to the host

It owns the machinery that is the same in every deployment:

| Module | Owns |
|---|---|
| `agent_providers.config` | The deployment facts every module reads, installed once per process via `configure()` |
| `agent_providers.catalog` | Live provider/route readiness probes and model listings (`probe_provider_route`, `list_provider_models`) |
| `agent_providers.dispatch` | Routing one turn to a provider and route, and the cover-image capability |
| `agent_providers.events` | The `StreamEvent` union a turn emits (assistant text, tool call, tool result, final) |
| `agent_providers.tool_loop` | The provider-agnostic tool loop driving a `ToolExecutor` over a `ToolTransport` |
| `agent_providers.tools` | The `ToolCatalog` and its Anthropic/OpenAI schema projections |
| `agent_providers.process` | `run_cli_bounded`: the one bounded subprocess layer (start, stdin, first line, deadline, process-group cleanup) |
| `agent_providers.images` | The `ImagePolicy` an image turn is held to |
| `agent_providers.sandbox` | The directory-name prefix constants a host's kernel sandbox profile must permit |

It leaves the application everything the library cannot know: the MCP tool set
and its execution, the prompts, where settings are stored, and domain
ownership. The library depends on `pydantic`, `httpx` and `anyio` at runtime and
on nothing else — no host application, no web framework, no database. An
import-linter contract forbids reaching back into `songmaker_cli` or the sibling
`webauth` library, and `lint-imports` proves it in CI.

## Install

```bash
pip install overnightworks-agent-providers
```

Two optional extras cover the paths that import a heavier dependency lazily:

- `[api]` pulls in the Anthropic SDK (`anthropic`) used by the Claude HTTP-API
  route.
- `[image]` pulls in Pillow (`pillow`) used by the Codex image path.

```bash
pip install "overnightworks-agent-providers[api,image]"
```

The wheel is also published as an asset on each tag's release:

```bash
pip install https://github.com/overnightworks/agent-providers/releases/download/v0.1.0/overnightworks_agent_providers-0.1.0-py3-none-any.whl
```

Tags are never moved; a broken release gets the next patch tag.

## The ports a host supplies

Nothing is configured by default. A host installs one `ProviderRuntimeConfig`
per process with `configure()`, and every module reads it back through
`current_config()`; until then `current_config()` raises rather than guessing a
binary path. The config carries the deployment facts — CLI binaries, credential
mirrors, mounted directories, models, process caps, the secret-env key set —
and an optional `McpServerSpec` describing the host's MCP server. A host that
runs no MCP server passes `mcp_server=None`.

The other host obligations arrive as arguments where they are needed:

- `ToolExecutor` (`agent_providers.tool_loop`) — the callable the tool loop
  invokes to run one requested tool and return its `ToolOutcome`.
- `ToolCatalog` (`agent_providers.tools`) — the tool declarations, projected to
  each provider's schema.
- `McpServerSpec` (`agent_providers.config`) — how the host's MCP server is
  launched and which tools it exposes.
- `ImagePolicy` (`agent_providers.images`) — the constraints an image turn is
  held to.
- The `StreamEvent` union (`agent_providers.events`) — what a turn emits, which
  the host consumes.

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
uv sync --extra dev --extra image
uv run pytest tests -q
uv run ruff check src tests
uv run lint-imports
uv build
```

The suite runs without a live provider: the CLI processes are faked and the
tool-surface probe is patched. `[image]` is needed at collection time because
one test imports Pillow at module load; the `[api]` path is exercised without
the Anthropic SDK installed, which is the library's implicit proof that it never
imports `anthropic` eagerly.

## Licence

MIT — see [LICENSE](LICENSE).
