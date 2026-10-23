# Operator guide

For the operator who runs the agent service: what it enforces at start, who may
run what, what the configuration owns, and how to provide and renew the
subscription login. The service's deployment lives in edge, not here.
`agent_runtime_service.config` documents the configuration file, and the decision record
[0001](decisions/0001-agent-service.md) owns the rulings behind it. The model of
the system is [ARCHITECTURE.md](ARCHITECTURE.md); what an app sends is
[wire.md](wire.md).

## Starting the service

`agent-runtime-service --config PATH` takes a TOML file whose required keys have no
default; startup names the field of any unknown or missing one and exits 2. The
service runs only on Linux, as an unprivileged effective user, and makes itself
non-dumpable at startup before it reads its configuration; it refuses to start
otherwise. The restriction names the service only. The image must run it as a
non-root user with `cap_drop: ALL`.

The configuration is read once; a restart applies a change. The signing key
file and the Claude token file are read and checked with it.

## Who may run what

A caller presents a workload token bound to one registered app and a user token
bound to one principal (`agent_runtime_service.identity`). The checks run in this order:

1. The user must be a member of the app: one of their groups is `/apps/<app>` or
   a path below it (`/apps/notes-other` is not a member of `notes`). This holds
   for `POST /runs` and `GET /runs/{id}`; anyone else gets 403 `forbidden`,
   before the body is read.
2. Their role is `admin` in `/apps/<app>/admin` or below, else `user`; a
   request whose `roles` lacks it gets 403 `forbidden`.
3. An app without an MCP origin whose request names a tool gets 403
   `forbidden`.
4. `agent_runtime_service.routing.route` decides the route; the second picture in
   [ARCHITECTURE.md](ARCHITECTURE.md) owns the order. The keys that feed it: the
   subscription groups (`[identity]
   subscription_groups`), the profiles (`[profiles.<name>]`), the names the
   subscription lists (`[cli.claude] models`), and the API groups (`api_groups`,
   set together with `[route] api_alias`). A model name the subscription does not
   list sends that agent to the paid API for every caller with API rights and
   gives subscription-only callers `not_enabled`. The app chooses the model per
   agent and sends it on each run; the service stores nothing of the choice.

At most `[runs] max_parallel_cli_runs` (default 5) CLI runs are admitted; one
beyond gets 429 `busy` with `Retry-After` and never falls back to the API
route. The service has a runner for the CLI route only, with Claude as its
provider, and checks the version of each pinned CLI at startup. The API runner
is not built: a run admitted to the API route ends as `failed:
provider_unavailable`.

## App tools

An app that offers tools has `[apps.<app>]` with `workload_client` and
`mcp_origin`, one canonical https origin that no other app shares. The service
mints each admitted run a capability ([wire.md](wire.md)), signed with the
service's one RSA-3072 key, `[signing] key_file`. The service trusts the CA
bundle `[mcp] ca_file` names for these origins alone. The service never makes
its key; generation and rotation are the A1a4 ruling in the decision record.

## The subscription login

The agent service holds the one login per provider for all apps. Only the
subscriber himself may use it, because the provider's terms allow no use on behalf
of others. The service keeps no user list; Keycloak decides, so the operator keeps
the subscription groups (`subscription_groups`) to the subscriber alone. Everyone
else reaches the API route only as a member of `api_groups`; a person in neither
group is refused `not_enabled` ([ARCHITECTURE.md](ARCHITECTURE.md)).
The file is the operator's to make and to keep
fresh, named as `profiles.<name>.token_file`; the library never creates, renews
or rewrites it. Claude is the only profile today: the service refuses a profile
for another provider.

```toml
[profiles.subscription]
provider = "claude"
token_file = "/run/secrets/claude-token"
```

**Provide it.** The file holds one line, the token `claude setup-token` prints,
at a mode that gives nothing to group or others (0600 or 0400), at most 16 KiB.
JSON (a credentials file) and API keys (`sk-ant-api...`) are refused; a link is
followed and its target checked. Run `claude setup-token` once, sign in with the
subscription, and write the printed token into the file.

**Renew it.** The token lasts a year and renewing it is the operator's. Run
`uv run python scripts/replace_token_file.py PATH` from a checkout: it reads
the new token on standard input, checks it by the service's rules and swaps it
in atomically; a refused token leaves the old file untouched. The next run
reads the file again, so no restart is needed.

**When it fails.** A missing or refused file stops the service at startup, or
fails a later run as `provider_error` before any child starts. An expired token
also fails the run as `provider_error`.

An app that still calls the core directly names the same file in the provider's
block of its `ProviderRuntimeConfig`; that interim path reads Codex and Grok
login files under weaker rules than the service's.
