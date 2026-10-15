# Repository agent guidance

`agent-providers` is a library, not an application: it is installed by host
applications and has no deployment of its own. Every change here is a change to
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
`packages/agent-client/`, the suite for both under `tests/`, and the decision
records under `docs/decisions/` — among them the decision that this
repository will also host the agent service and how its packages ship. No
catch-all directory (`tooling/`, `misc/`) and no deeper hierarchy than the
owner needs.

## Boundaries

- The library reaches back into no host. The core depends on the agent
  client, `pydantic`, `httpx` and `anyio` at runtime, and on nothing else — no
  host application, no web framework, no database. Everything host-specific
  arrives through the `ProviderRuntimeConfig` a host installs with
  `configure()` and the ports it supplies.
- The agent client is light: it depends on `pydantic` only and owns the tool
  contract `ToolDeclaration` in `agent_client.tools`. The core builds on it and
  re-exports the identical class from `agent_providers.tools`; the client never
  imports the core.
- The agent service's wire has one owner, `agent_client.wire`, with its stream
  rules in `agent_client.run_stream`. `agent_providers.events` is the core's
  in-process turn event and never a second wire.
- `.importlinter` forbids `songmaker_cli` and `webauth` in both packages and
  layers `agent_providers` above `agent_client`; `lint-imports` proves it in
  CI.
- Two optional extras exist for lazily-imported paths: `agent-providers[api]`
  for the Anthropic SDK the Claude HTTP-API route uses, and
  `agent-providers[image]` for the Pillow the Codex image path uses. Core
  installs neither.
- Application concerns — the MCP tool set and its execution, prompts, settings
  storage, and domain ownership — belong to the host, not here.

## Checks

```bash
uv sync --extra dev --extra image
uv run pytest tests -q
uv run ruff check src tests packages
uv run lint-imports
uv build --all-packages
```

The suite is fast and needs no live provider: the CLI processes are faked and
the tool-surface probe is patched. Run it whole. `[image]` is required at
collection because one test imports Pillow at module load.

CI also proves the built wheels outside the workspace: in a fresh venv, the
client wheel alone imports every `agent_client` module, and both wheels
together import `agent_providers.dispatch` with the identical
`ToolDeclaration`.

## Releasing

A release is a tag, and the two distributions move in lockstep. On a `v*` tag
`.github/workflows/release.yml` builds both wheels, checks that their names
carry the tag's version, and attaches exactly those two wheels to the GitHub
release; host applications pin both asset URLs. A tag is never moved — a broken
release gets the next patch tag — so the pull request that precedes the tag
raises all four version literals together: both `pyproject.toml` files,
`agent_providers.__version__`, and core's exact `overnightworks-agent-client`
pin. `tests/test_distribution_contract.py` fails while they disagree, because
uv ignores that pin inside the workspace.
