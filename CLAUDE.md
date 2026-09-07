# agent-providers — Claude Code Config

## Project

The reusable agent provider layer: routes a turn to Claude, Grok or Codex over
each provider's CLI or HTTP API, with live model catalogs, readiness probes, a
streaming turn transport, the tool loop, a bounded subprocess runner, and the
sandbox directory-name constants. Distribution `overnightworks-agent-providers`,
import package `agent_providers`.

**Python**: 3.12+ | **Package manager**: uv | **Library**: `src/agent_providers/` |
**Suite**: `tests/`

Extracted from [overnightworks/songmaker](https://github.com/overnightworks/songmaker)
(issue #825) with its history; songmaker installs the wheel from this
repository's tagged release.

**Agent policy:** [AGENTS.md](AGENTS.md) is the provider-neutral entrypoint and
owns the layout, boundary, check and release rules. Read it first.

## Setup & Checks

```bash
uv sync --extra dev --extra image
uv run pytest tests -q       # the whole suite; no live provider needed
uv run ruff check src tests
uv run lint-imports
uv build                     # what the release attaches
```

## Code patterns

- **Deployment facts come from `ProviderRuntimeConfig`.** Nothing reads
  configuration from an environment variable or a global; the host installs one
  config per process with `configure()` and every module reads it back with
  `current_config()`.
- **Host obligations are ports, passed in.** The tool set (`ToolExecutor`,
  `ToolCatalog`), the MCP server (`McpServerSpec`), and the image constraints
  (`ImagePolicy`) arrive from the host — the library never invents them.
- **One bounded subprocess layer.** Every CLI spawn goes through
  `agent_providers.process.run_cli_bounded`, which owns start, stdin, first
  line, deadline, and process-group cleanup.
- **No inline comments.** Names carry the meaning; a comment explains only a
  non-obvious *why*.
- **Tests name the behaviour they pin** and use the builders in
  `tests/provider_test_support.py` rather than a second set of fakes.
