# App author guide

For the developer of an app that starts agent runs through the agent service:
the request an app sends, the events it reads back, the agent files it ships,
the tool contract it declares and the MCP endpoint it serves. This is a
reference; `agent_client.wire` owns the wire, `agent_client.agents` the agent
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
| `v` | The protocol version, `2`. Any other body answers 400 `unsupported_version`; a 0.5.0 app and a 0.6.0 service refuse each other. |
| `purpose` | The agent's name, `[a-z][a-z0-9-]{0,63}`. It labels the run and its capability and keys the app's model override; it chooses no route. |
| `turn` | The system prompt and the messages (`Turn`, `TurnMessage`). |
| `roles` | The agent's allowed roles, exactly `["admin"]` or `["admin", "user"]`; `AgentDefinition.request` fills it from the agent file. |
| `tools` | A map from each tool the run may call to its `ToolEffect`; required, possibly `{}`. |
| `model` | Optional, `<provider>/<model>` such as `claude/sonnet`. |

`GET /runs/{id}` returns the run's `RunStatus`, readable only by the app and
user that started it. The record is in memory, kept up to an hour after the run
ended, and gone after a restart.

The stream's events are `run_started`, `assistant_text`, `tool_call`,
`tool_result`, `heartbeat` and one terminal event: `succeeded`, `failed`
(with a `FailureCode`) or `cancelled`. A run cut short after a side-effecting
tool call was emitted ends as `failed: interrupted`, not `cancelled`
(`ToolEffect`).

A request the service refuses answers a `RunRejection` with one `RejectionCode`:

| Code | Status | Meaning |
|---|---|---|
| `unauthorized` | 401 | A user token and a workload token are both required and must be valid. |
| `forbidden` | 403 | The user is no member of the app, the request's `roles` lack the caller's role, or the request names a tool while the app has no MCP origin. |
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
the service mints each admitted run a capability that grants exactly those
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
