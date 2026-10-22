# agent-runtime

The reusable agent provider layer: it routes a turn to Claude, Grok or Codex —
over each provider's CLI or its HTTP API — and owns the machinery a host
application should not rebuild: live model catalogs and readiness probes, the
streaming turn transport, the tool loop, a bounded subprocess runner and the
sandbox directory-name constants. It also hosts the agent service, the one
authenticated entrance through which apps start agent runs.

Three distributions are released together from one tag: the provider core
`overnightworks-agent-runtime`, the light client `overnightworks-agent-runtime-client`
for apps, and the agent service `overnightworks-agent-runtime-service`.

## Install

No distribution is on PyPI. Install the wheels that the
[latest release](https://github.com/overnightworks/agent-runtime/releases/latest)
lists: its page gives each as a `name @ URL#sha256=…` requirement for `pip install`
or `uv add`.

## Where to read

| You are | Read |
|---|---|
| anyone new | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the three distributions, the route decision, trust |
| a host developer integrating the library | [docs/host.md](docs/host.md): install, what the host supplies, the child environment, the sandbox |
| an app author starting agent runs | [docs/wire.md](docs/wire.md): wire v2, agent files, the tool contract, the MCP endpoint, what a run keeps |
| the operator running the service | [docs/operations.md](docs/operations.md): who may run what, the subscription login and its renewal |
| bumping a pin | [docs/MIGRATING.md](docs/MIGRATING.md): one section per release |

The code and its docstrings own behaviour. The decision record
[docs/decisions/0001-agent-service.md](docs/decisions/0001-agent-service.md)
owns the service's rulings, and `AGENTS.md` owns the layout, the boundaries, the
checks and the release rules.

## Checks

`AGENTS.md` lists the commands (sync, suite, ruff, `lint-imports`, the three-wheel
build) and what CI adds. The suite runs without a live provider; the service's
end-to-end proof, `tests/test_service_keycloak.py`, runs only where `KEYCLOAK_URL`
names the test Keycloak of the `keycloak-token-shapes` CI job.

## Licence

MIT — see [LICENSE](LICENSE).
