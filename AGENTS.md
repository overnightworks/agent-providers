# Repository agent guidance

`agent-providers` is a library and the agent service built on it; every change here is a change to a public contract that a tag has already frozen elsewhere. Overview: README; decisions: `docs/decisions/`; breaking changes: `docs/MIGRATING.md`.

## Boundaries

`.importlinter` owns the layering and the forbidden imports; `uv run lint-imports`
proves it. The traps it cannot show:

- The service's wire has one owner, `agent_runtime_client.wire`.
- `agent_runtime.events` is the core's in-process turn event, never a second wire.
- The `[api]`, `[image]` and `[mcp-server]` extras stay optional: their libraries are imported only inside the route or module that needs them.

## Code patterns

- Deployment facts come from the host, never from an environment variable or a
  module default. A host that runs the catalog or turn paths installs one
  `ProviderRuntimeConfig` per process with `configure()`, and those paths read it
  back with `current_config()`; they gain no explicit-argument twin that skips
  it. The agent service installs no core runtime: it reads its own config file
  and passes each fact as an argument to the subscription run (`ClaudeRun`,
  which validates at construction; `run_directories`, whose root is a
  `PrivateWorkingRoot` validated at construction and whose file names are
  checked on entry), none of which reads installed configuration — a test pins
  each such entry working after `reset_config()`. No path invents a default for a
  host fact.
- Host obligations are ports, passed in. The tool set (`ToolExecutor`,
  `ToolCatalog`), the MCP server (`McpServerSpec`), and the image constraints
  (`ImagePolicy`) arrive from the host; the library never invents them.
- One spawn owner, one bounded runner. Every child is a `ChildProcess`
  (complete environment, absolute binary, explicit `cwd`) created only in
  `agent_runtime.spawn`; the bounded runner
  `agent_runtime.process.run_cli_bounded` owns start, stdin, first line,
  deadline, and process-group cleanup, and the async Claude turns run on
  `spawn.AnchoredChild`, whose leader only its own handle reaps.
- Tests use the builders in `tests/provider_test_support.py` rather than a
  second set of fakes.

## Checks

```bash
uv sync --all-packages --extra dev --extra image
uv run pytest tests -q
uv run ruff check src tests packages
uv run lint-imports
uv build --all-packages
```

The suite needs no live provider, and `[image]` is required at collection
(one test imports Pillow at module load). CI's "Host vocabulary" step greps
`src packages tests docs` for host words; `.github/host-vocabulary-allowed.txt`
owns the allowed hits. The required check is `quality / verdict`
(`quality/config.toml`, `.github/workflows/ci.yml`).
`tests/test_service_keycloak.py` skips without `KEYCLOAK_URL`, which CI's `keycloak-token-shapes` job sets.

## Releasing

`main` is protected.

A release is a tag, and the three distributions move in lockstep. On a `v*`
tag `.github/workflows/release.yml` builds the three wheels, checks that their
names carry the tag's version, and attaches exactly those three wheels to the
GitHub release; hosts pin those wheel URLs and never install by name from PyPI. A release whose `docs/MIGRATING.md`
holds `## Migrating to <version>` gets a release page that opens with a link to
that section, so a breaking release writes that section before the tag. A tag
is never moved — a broken release gets the next patch tag — so the pull
request that precedes the tag raises all seven version literals together:
the three `pyproject.toml` files, `agent_runtime.__version__`, core's exact
`overnightworks-agent-runtime-client` pin, and the service's exact pins on core and
client.
`tests/test_distribution_contract.py` fails while they disagree, because uv
ignores those pins inside the workspace.
