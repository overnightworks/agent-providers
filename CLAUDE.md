# agent-providers — Claude Code Config

## Project

The reusable agent provider layer: routes a turn to Claude, Grok or Codex over
each provider's CLI or HTTP API, with live model catalogs, readiness probes, a
streaming turn transport, the tool loop, a bounded subprocess runner, and the
sandbox directory-name constants. It also hosts the agent service that apps
start runs through. A uv workspace of three distributions released in lockstep
from one tag:

- `overnightworks-agent-providers`, import package `agent_providers` — the
  provider core, at the root.
- `overnightworks-agent-client`, import package `agent_client` — the light
  client (pydantic only; PyJWT in its `[mcp-server]` extra) that owns the tool contract `ToolDeclaration` and
  the agent service's run contract (`agent_client.wire`: the `RunRequest`
  body and the streamed run events).
- `overnightworks-agent-service`, import package `agent_service` — the agent
  service (`agent-service --config PATH`), which builds on both.

**Python**: 3.12+ | **Package manager**: uv | **Core**: `src/agent_providers/` |
**Client**: `packages/agent-client/` | **Service**: `packages/agent-service/` |
**Suite**: `tests/`

Extracted from [overnightworks/songmaker](https://github.com/overnightworks/songmaker)
(issue #825) with its history. Hosts install from this repository's tagged
release by pinning the wheel URLs, never by name from PyPI.

**Agent policy:** [AGENTS.md](AGENTS.md) is the provider-neutral entrypoint and
owns the layout, boundary, check and release rules. Read it first.

## Setup & Checks

```bash
uv sync --all-packages --extra dev --extra image
uv run pytest tests -q       # the whole suite; no live provider needed
uv run ruff check src tests packages
uv run lint-imports          # host boundary and service > core > client layers
uv build --all-packages      # the three wheels the release attaches
```

## Code patterns

- **Deployment facts come from the host, never from an environment variable
  or a module default.** A host that runs the catalog or turn paths installs
  one `ProviderRuntimeConfig` per process with `configure()`, and those paths
  read it back with `current_config()`; they gain no explicit-argument twin
  that skips it. The agent service installs no core runtime: it reads its own
  config file and passes each fact as an argument to the subscription run
  (`ClaudeRun`, which validates at construction; `run_directories`, whose root
  is a `PrivateWorkingRoot` validated at construction and whose file names are
  checked on entry), none of which reads installed configuration — a test pins
  each such entry working after `reset_config()`.
  No path invents a default for a host fact.
- **Host obligations are ports, passed in.** The tool set (`ToolExecutor`,
  `ToolCatalog`), the MCP server (`McpServerSpec`), and the image constraints
  (`ImagePolicy`) arrive from the host — the library never invents them.
- **One spawn owner, one bounded runner.** Every child is a `ChildProcess`
  (complete environment, absolute binary, explicit `cwd`) created only in
  `agent_providers.spawn`; the bounded runner
  `agent_providers.process.run_cli_bounded` owns start, stdin, first line,
  deadline, and process-group cleanup, and the async Claude turns run on
  `spawn.AnchoredChild`, whose leader only its own handle reaps.
- **No inline comments.** Names carry the meaning; a comment explains only a
  non-obvious *why*.
- **Tests name the behaviour they pin** and use the builders in
  `tests/provider_test_support.py` rather than a second set of fakes.
