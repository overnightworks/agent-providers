# Repository agent guidance

`agent-providers` is a library and the agent service built on it: host
applications install the library, and apps reach the service over HTTP. The
core routes a turn to Claude, Grok or Codex over each provider's CLI or HTTP
API, with live model catalogs, readiness probes, a streaming turn transport,
the tool loop, a bounded subprocess runner, and the sandbox directory-name
constants. The
service's deployment lives in edge, not here. Every change here is a change to
a public contract that a tag has already frozen somewhere else.

Python 3.12+, managed with uv. Extracted from
[overnightworks/songmaker](https://github.com/overnightworks/songmaker)
(issue #825) with its history.

## Repository layout

The repository is a uv workspace. Its root holds only what a tool must find
there — the core's package manifest, which also declares the workspace, the
one lockfile, the import-boundary configuration, `.gitignore`,
`.python-version`, `sonar-project.properties`, the licence, and the tool
configuration directories `.aco/`, `.github/` and `quality/` — and the entry
documents `README.md`, `AGENTS.md` and `CLAUDE.md`. Everything else lives in
the directory of its owner: the provider core (distribution
`overnightworks-agent-providers`) under `src/agent_providers/`, the light
agent client (distribution `overnightworks-agent-client`) under
`packages/agent-client/`, the agent service (distribution
`overnightworks-agent-service`) under `packages/agent-service/`, the suite for
all three under `tests/`, and the decision records under `docs/decisions/` —
among them the decision that this repository hosts the agent service and how
its packages ship.

## Boundaries

- The library reaches back into no host. The core depends on the agent
  client, `pydantic`, `httpx` and `anyio` at runtime, and on nothing else — no
  host application, no web framework, no database. Everything host-specific
  arrives from the host, as the first two Code patterns say.
- The agent client is light: its base depends on `pydantic` only and owns the tool
  contract `ToolDeclaration` in `agent_client.tools`. The core builds on it and
  re-exports the identical class from `agent_providers.tools`; the client never
  imports the core.
- The agent service's wire has one owner, `agent_client.wire` (the `RunRequest`
  body and the streamed run events), with its stream
  rules in `agent_client.run_stream`. `agent_providers.events` is the core's
  in-process turn event and never a second wire; `agent_service.run_events`
  maps it onto the wire.
- The service, started as `agent-service --config PATH`, builds on the core and
  the client, and nothing imports it. It
  alone verifies user and workload tokens, through webauth's `JwtVerifier`; an app
  verifies its run capabilities through `agent_client.mcp_server`. It alone depends
  on a web framework (FastAPI, uvicorn).
- `.importlinter` forbids `songmaker_cli` in all three packages and `webauth`
  in the core and the client, and layers `agent_service` above
  `agent_providers` above `agent_client`; `lint-imports` proves it in CI.
- Three optional extras exist: `agent-providers[api]` for the Anthropic SDK
  the Claude HTTP-API route uses, `agent-providers[image]` for the Pillow the
  Codex image path uses, and `agent-client[mcp-server]` for the PyJWT with
  cryptography that `agent_client.mcp_server` verifies run capabilities with;
  no base module imports that module. Neither package installs them by default.
- Application concerns — the MCP tool set and its execution, prompts, settings
  storage, and domain ownership — belong to the host, not here.

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
  `agent_providers.spawn`; the bounded runner
  `agent_providers.process.run_cli_bounded` owns start, stdin, first line,
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

CI's "Host vocabulary" step greps `src packages tests docs` for host words; `.github/host-vocabulary-allowed.txt` is the one owner of the allowed hits.

The suite is fast and needs no live provider: the CLI processes are faked and
the tool-surface probe is patched. `[image]` is required at
collection because one test imports Pillow at module load.

CI also runs the `quality` gate (`overnightworks/quality`, pinned by commit in
`ci.yml`); its required check is `quality / verdict`. Its secret allowance and
mandatory jobs live in `quality/config.toml`, its SonarCloud scope in
`sonar-project.properties`, and the `test` job feeds it `coverage.xml`.
`test` runs on every declared Python minor (3.12 to 3.15) and only the 3.12
leg feeds `coverage.xml`. `quality/config.toml` makes each `test (<minor>)`
leg, the `checks` aggregate, `keycloak-token-shapes` and `dependency-floors`
(the suite on the lowest declared direct dependencies at 3.12) mandatory.

CI also proves the built wheels outside the workspace: in a fresh venv, the
client wheel alone imports every `agent_client` module, client and core
together import `agent_providers.dispatch` with the identical
`ToolDeclaration`, and all three install together, the installed
`agent_service.app_tools` discovers a tool from a local TLS fake, and the
installed `agent-service` refuses a bad config. The `keycloak-token-shapes`
job serves the test realm from Keycloak and runs
`tests/test_service_keycloak.py`, the service's end-to-end proof with real
tokens; elsewhere that suite skips.

## Releasing

`main` is protected.

A release is a tag, and the three distributions move in lockstep. On a `v*`
tag `.github/workflows/release.yml` builds the three wheels, checks that their
names carry the tag's version, and attaches exactly those three wheels to the
GitHub release; hosts pin those wheel URLs and never install by name from PyPI. A release whose README
holds `## Migrating to <version>` gets a release page that opens with a link to
that section, so a breaking release writes that section before the tag. A tag
is never moved — a broken release gets the next patch tag — so the pull
request that precedes the tag raises all seven version literals together:
the three `pyproject.toml` files, `agent_providers.__version__`, core's exact
`overnightworks-agent-client` pin, and the service's exact pins on core and
client.
`tests/test_distribution_contract.py` fails while they disagree, because uv
ignores those pins inside the workspace.
