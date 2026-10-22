# App author guide

For the developer of an app that starts agent runs through the agent service:
the request an app sends, the events it reads back, the agent files it ships,
the tool contract it declares and the MCP endpoint it serves. This is a
reference; `agent_client.wire` owns the wire (its stream rules sit in
`agent_client.run_stream` and `agent_client.sse`), `agent_client.agents` the agent
file, `agent_client.tools` the tool contract and `agent_client.mcp_server` the
tool endpoint, and their docstrings are exact where this file is short. The
decision record [0001](decisions/0001-agent-service.md) owns the rulings behind
them. Install the client as [host.md](host.md) describes; who may run what is the
operator's configuration ([operations.md](operations.md)).

## The run

`POST /runs` takes a `RunRequest` and answers with the run's event stream
(SSE), ending every run with exactly one terminal event except the cut stream
of a dropped client. A call carries two tokens: the end user's, and the app's
workload token in the `Agent-Workload-Token` header.

`RunRequest`, wire version 2:

| Field | Meaning |
|---|---|
| `v` | The protocol version, `2`. Any other body answers 400 `unsupported_version`. |
| `purpose` | The agent's name, `[a-z][a-z0-9-]{0,63}`. It labels the run and its capability and keys the app's model override; it chooses no route. |
| `turn` | The system prompt and the messages (`Turn`, `TurnMessage`). |
| `roles` | The agent's allowed roles, exactly `["admin"]` or `["admin", "user"]`; `AgentDefinition.request` fills it from the agent file. |
| `tools` | A map from each tool the run may call to its `ToolEffect`; required, possibly `{}`. |
| `model` | Optional, `<provider>/<model>` such as `claude/sonnet`. |

`GET /runs/{id}` returns the run's `RunStatus`, readable only by the app and
user that started it ([What a run keeps](#what-a-run-keeps) says what the record holds).

The stream's events are `run_started`, `assistant_text`, `tool_call`,
`tool_result`, `heartbeat` and one terminal event: `succeeded`, `failed`
(with a `FailureCode`) or `cancelled`. A run cut short after a side-effecting
tool call was emitted ends as `failed: interrupted`, not `cancelled`
(`ToolEffect`).

A request the service refuses answers a `RunRejection` with one `RejectionCode`:

| Code | Status | Meaning |
|---|---|---|
| `unauthorized` | 401 | A user token and a workload token are both required and must be valid. |
| `forbidden` | 403 | The user is no member of the app, the request's `roles` lack the caller's role, the request names a tool while the app has no MCP origin, or the two tokens do not belong together (`agent_service.identity` lists every cause). |
| `not_enabled` | 403 | No route is enabled for this caller and model. |
| `unsupported_version` | 400 | `v` is not 2. |
| `invalid_request` | 422 | The body is not a valid `RunRequest`. |
| `capability_too_large` | 422 | The run's capability would exceed 8,192 characters. |
| `busy` | 429 | The subscription route's places are taken; `Retry-After` is set and the run never falls back to the paid route. |
| `unavailable` | 503 | The service cannot check tokens right now. |
| `not_found` | 404 | No such run, or not yours. |

## Agent files

An app declares its agents as Claude Code subagent files,
`src/<pkg>/agents/<name>.md`, that `agent_client.agents.load_agent` reads into
an `AgentDefinition`: YAML front matter (`name`, `description`, `tools`, plus
`model` as `<provider>/<model>` and the extension `appRoles`, exactly `[admin]`
or `[admin, user]`) and the Markdown body as the prompt. `AgentFileError` names
the file and the field. `offered_to(role)` is a display filter, not the
security boundary: the service checks the role.

## The tool contract

`agent_client.tools.ToolDeclaration` is the one declaration of a tool: name,
description, arguments and a `ToolEffect`, `read_only` or `side_effecting`. The
effect is for the service and never reaches a prompt; it defaults to
side-effecting, because a tool nobody classified must be assumed to change
state. The core's `ToolCatalog` builds on the same class.

## App tools through MCP

An app that offers tools declares one canonical https MCP origin in the
service's configuration. The app names the tools of each run in its request;
the service mints each admitted run a capability (an RS256 token) that grants exactly those
names and stamps the request's effects onto the run's tool calls (a tool
outside the map counts as side-effecting).

The app installs the client with `[mcp-server]` and serves
`McpServer.in_front_of(app)` as its outermost ASGI app, never a `Route` or
`Mount` in its router (the service refuses their slash redirects). It passes
the bytes of its public key (one PEM `PUBLIC KEY` block holding an RSA-3072 key
with exponent 65537), and each `McpTool` handler checks ownership with a fresh
database session. `verify_capability` returns the claims or one closed
`CapabilityRefusal` and never raises for token content; a body that carries
the capability's token or signature is replaced by an empty 500.
`agent_client.mcp_server` documents the order of the checks.

## What a run keeps

The service stores nothing of a turn: it has no database, and the prompt and
the answer pass through its memory only.

**The run's place.** Each subscription run gets a private tree below the
operator's working root: a home, which is also the CLI's temporary directory, and
an empty working directory. The home holds the system prompt, and the tool relay's
connection file when the run offers tools, each at mode 0600, plus whatever the CLI
writes there. The service removes the tree when the run ends and its process group
is confirmed gone. While a group is not confirmed gone, the service quarantines the
place: the whole tree stays, system prompt included, and the service asks again every
five seconds and never gives up. The tree is removed on the first confirmation. If
the service stops first, nothing removes it, and the same holds for a tree whose
removal fails (the service logs that by class, without the path): it stays until the
host clears the working root ([host.md](host.md), "A subscription run's directories").

**The run record.** The record behind `GET /runs/{id}` holds the run's owner, its
two instants, the number of events, its ending and whether the client was lost. It
holds no prompt, answer, tool input or result, failure message, token or process
group. It lives in memory only: a running record never expires, an ended one is
kept up to an hour, sooner when the ended records exceed the service's capacity and
the oldest go first, and none survives a restart.

**The logs.** The service's own log lines carry classes, source positions, counts,
ids and process-group numbers, never a prompt, an answer or CLI output; the CLI's
error stream is read and discarded.

**Tokens.** The end user's token and the app's workload token are checked on the
request and neither stored nor passed to the CLI. The CLI receives the profile's
subscription login, read from the operator's file at every run
([operations.md](operations.md), "The subscription login"), and a per-run secret for
the tool relay.

**What the CLI writes.** Every CLI call passes `--no-session-persistence` and the
switches that turn off the CLI's telemetry, nonessential traffic and
auto-update. Measured without a login, the CLI then writes into the run's home, which
goes with the place, and creates an empty directory `/tmp/cc-socks-<uid>/` outside it
([decision record 0001](decisions/0001-agent-service.md), A1b-0a c5). What a
logged-in turn writes is not measured yet; issue #115, step 12b, owns that
measurement.

**The provider.** The turn's text reaches the provider (Anthropic for Claude) under
the operator's subscription. What the provider keeps follows its terms, not this
service.
