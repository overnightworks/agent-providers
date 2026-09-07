# Repository agent guidance

`agent-providers` is a library, not an application: it is installed by host
applications and has no deployment of its own. Every change here is a change to
a public contract that a tag has already frozen somewhere else.

- Before every repository edit, use the globally installed `agent-claim` CLI to
  check the live ledger and claim the exact write scope. Subagents remain within
  their parent's live claim and do not take overlapping claims.
- Keep the shared `main` checkout clean. Build in an isolated external worktree,
  and never `git stash` — the stash stack is shared across worktrees.
- `main` is protected: every change lands through a pull request with green CI.
  Nobody pushes to `main`.

## Repository layout

The repository root holds only what a tool must find there — the package
manifest and its lockfile, the import-boundary configuration, the licence — and
the entry documents `README.md`, `AGENTS.md` and `CLAUDE.md`. Everything else
lives in the directory of its owner: the library under `src/agent_providers/`,
its suite under `tests/`. No catch-all directory (`tooling/`, `misc/`) and no
deeper hierarchy than the owner needs.

## Boundaries

- The library reaches back into no host. It depends on `pydantic`, `httpx` and
  `anyio` at runtime, and on nothing else — no host application, no web
  framework, no database. Everything host-specific arrives through the
  `ProviderRuntimeConfig` a host installs with `configure()` and the ports it
  supplies. `.importlinter` forbids `songmaker_cli` and `webauth`, and
  `lint-imports` proves it in CI.
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
uv run ruff check src tests
uv run lint-imports
```

The suite is fast and needs no live provider: the CLI processes are faked and
the tool-surface probe is patched. Run it whole. `[image]` is required at
collection because one test imports Pillow at module load.

## Releasing

A release is a tag. `.github/workflows/release.yml` builds the wheel on a `v*`
tag and attaches it to the GitHub release; host applications pin that asset URL.
A tag is never moved — a broken release gets the next patch tag — so the version
in `pyproject.toml` and `agent_providers.__version__` is raised in the pull
request that precedes the tag.
