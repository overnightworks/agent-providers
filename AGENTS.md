# Repository agent guidance

`agent-providers` is a library and the agent service built on it: host
applications install the library, and apps reach the service over HTTP. The
service's deployment lives in edge, not here. Every change here is a change to
a public contract that a tag has already frozen somewhere else.

- Before every repository edit, use the globally installed `aco` CLI to
  check the live ledger and claim the exact write scope. Subagents remain within
  their parent's live claim and do not take overlapping claims.
- Keep the shared `main` checkout clean. Build in an isolated external worktree,
  and never `git stash` — the stash stack is shared across worktrees.
- `main` is protected: every change lands through a pull request with green CI.
  Nobody pushes to `main`.

## Repository layout

The repository is a uv workspace. Its root holds only what a tool must find
there — the core's package manifest, which also declares the workspace, the
one lockfile, the import-boundary configuration, the licence — and the entry
documents `README.md`, `AGENTS.md` and `CLAUDE.md`. Everything else lives in
the directory of its owner: the provider core (distribution
`overnightworks-agent-providers`) under `src/agent_providers/`, the light
agent client (distribution `overnightworks-agent-client`) under
`packages/agent-client/`, the agent service (distribution
`overnightworks-agent-service`) under `packages/agent-service/`, the suite for
all three under `tests/`, and the decision records under `docs/decisions/` —
among them the decision that this repository hosts the agent service and how
its packages ship. No
catch-all directory (`tooling/`, `misc/`) and no deeper hierarchy than the
owner needs.

## Boundaries

- The library reaches back into no host. The core depends on the agent
  client, `pydantic`, `httpx` and `anyio` at runtime, and on nothing else — no
  host application, no web framework, no database. Everything host-specific
  arrives from the host: through the
  `ProviderRuntimeConfig` it installs with `configure()`, through the ports it
  supplies, or, for the subscription run the agent service drives without a
  core runtime, as explicit arguments validated at construction.
- The agent client is light: it depends on `pydantic` only and owns the tool
  contract `ToolDeclaration` in `agent_client.tools`. The core builds on it and
  re-exports the identical class from `agent_providers.tools`; the client never
  imports the core.
- The agent service's wire has one owner, `agent_client.wire`, with its stream
  rules in `agent_client.run_stream`. `agent_providers.events` is the core's
  in-process turn event and never a second wire; `agent_service.run_events`
  maps it onto the wire.
- The service builds on the core and the client, and nothing imports it. It
  alone verifies tokens, through webauth's `JwtVerifier`, and it alone depends
  on a web framework (FastAPI, uvicorn).
- `.importlinter` forbids `songmaker_cli` in all three packages and `webauth`
  in the core and the client, and layers `agent_service` above
  `agent_providers` above `agent_client`; `lint-imports` proves it in CI.
- Two optional extras exist for lazily-imported paths: `agent-providers[api]`
  for the Anthropic SDK the Claude HTTP-API route uses, and
  `agent-providers[image]` for the Pillow the Codex image path uses. Core
  installs neither.
- Application concerns — the MCP tool set and its execution, prompts, settings
  storage, and domain ownership — belong to the host, not here.

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
the tool-surface probe is patched. Run it whole. `[image]` is required at
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

A release is a tag, and the three distributions move in lockstep. On a `v*`
tag `.github/workflows/release.yml` builds the three wheels, checks that their
names carry the tag's version, and attaches exactly those three wheels to the
GitHub release; host applications pin the asset URLs. A release whose README
holds `## Migrating to <version>` gets a release page that opens with a link to
that section, so a breaking release writes that section before the tag. A tag
is never moved — a broken release gets the next patch tag — so the pull
request that precedes the tag raises all seven version literals together:
the three `pyproject.toml` files, `agent_providers.__version__`, core's exact
`overnightworks-agent-client` pin, and the service's exact pins on core and
client.
`tests/test_distribution_contract.py` fails while they disagree, because uv
ignores those pins inside the workspace.
