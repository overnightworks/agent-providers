# 0001 — One central agent service

- **Date:** 15.10.2026
- **Decided by:** the operator's rulings of 15.10.2026 and the head rulings H1–H12
  that answered the independent architecture review (Codex, three rounds).
- **Harvested from:** [#18](https://github.com/overnightworks/agent-providers/issues/18).

This record states decisions and their reasons. It describes where the system is
going, not what exists; each slice below makes its part true when it lands.

## Mental model

Apps stop talking to language models themselves. An app sends a **purpose** (a
closed value such as `songmaker.cowriter`), the turn data, and two tokens — the
end user's and its own. One agent service decides who is asking, whether they may
use the operator's subscription, and which route serves the purpose. The app
never names a provider, a model, a route or a user id, and never holds an LLM or
subscription credential.

There are exactly two routes:

- the **CLI route** runs an isolated `claude -p` under a configured subscription
  profile, for members of the Keycloak group `/ki-abo` only;
- the **API route** goes through LiteLLM, which alone maps aliases to upstream
  models and keys.

Tools stay with the app. When an app has tools, the service reaches them through
the app's one registered MCP endpoint with a short-lived capability bound to the
run.

```mermaid
flowchart LR
    App["App<br/>(webauth OIDC session, agent-client)"]
    KC["Keycloak<br/>groups /ki-abo, /apps/&lt;app&gt;"]
    subgraph internal["Internal network only"]
        Svc["Agent service<br/>verify user + app, policy (purpose table, /ki-abo, owner binding), route"]
        CLI["CLI route<br/>isolated claude -p, one profile"]
        Relay["per-run relay (loopback)"]
        API["API route<br/>service tool loop"]
    end
    LLM["LiteLLM (edge)<br/>upstream keys, budgets, aliases"]
    Up["Anthropic API (DPA), local models"]
    MCP["App's MCP endpoint<br/>(optional per app)"]

    App -- "purpose + turn data<br/>user token + workload token" --> Svc
    KC -. "issues both tokens" .-> App
    Svc --> CLI
    Svc --> API
    API -- "LiteLLM alias<br/>(per-app virtual key)" --> LLM
    LLM --> Up
    CLI -- "tool calls, no capability" --> Relay
    Relay -- "capability token" --> MCP
    API -- "capability token" --> MCP
    Svc -- "SSE events, GET /runs/{id}" --> App
```

atelier-2 is the explicit exception: it is the operator's own agent tool and
keeps its local executors (see H8).

## Operator rulings

- There is one central LLM gateway.
- The personal subscription is for the operator's own use only. Friends never go
  through it.
- There is one simple place to switch.
- No app container holds a subscription login.
- Who may use a subscription is configured in Keycloak, the one admin place.
- Subscription CLIs serve only the operator's own agents. Local API-key tools may
  use the gateway too.

## H1 Identity

**Context.** A configured-username fallback or a caller-supplied `user_id` lets
any app claim to be anyone.

**Decision.** The app forwards the end user's Keycloak access token, whose
audience includes `agent-service` and whose `azp` is the app's client. The
service checks signature, issuer, audience, expiry, `azp` and the full-path group
claim. The principal is `(iss, sub)`. The RS256 verifier comes from webauth W1
(overnightworks/webauth#51).

**Consequence.** There is one verifier owner and no copy of it here; no request
can name its own user.

### Token shapes (proven by A1a1k)

`tests/test_keycloak_token_shapes.py` fetches real tokens from Keycloak 26.8.1
serving the test realm `tests/keycloak/realm.json`, verifies them with the
webauth verifier, and holds these facts in CI (job `keycloak-token-shapes`):

- A service-account token has `azp == client_id ==` its client, carries realm
  role `agent-workload`, and lives at most 300 s (`exp - iat`).
- A user token never carries `agent-workload`; the role is granted only to
  service-account users. Whether a user token carries `client_id` is recorded,
  not relied on.
- A user token has `azp` equal to the client the user logged in through, `aud`
  containing `agent-service`, and `groups` as full paths (`/ki-abo`,
  `/apps/songmaker`). A user in no group has the claim absent or empty.
- A user token's `sub` differs from every service account's `sub`.
- With Keycloak's built-in default scopes (`basic`, `profile`, `email`,
  `roles`, `web-origins`, `acr`, `service_account`) that the platform realm
  keeps, a workload client is named only by `azp` and `client_id`. Two
  workload clients' service-account tokens also differ in the service-account
  user's own `sub` and `preferred_username`, and in `allowed-origins`, each
  client's web origins (besides `jti` and the times). `preferred_username` is
  `service-account-<clientId>`, so only `sub` is unknowable in advance. `clientAddress` and `clientHost`
  name the calling host; the test calls both clients from one host, so it
  leaves them equal. The workload binding must therefore check `azp` and
  `client_id` against the registry.

### A1a1 ruling (head, 15.10.2026)

**Context.** A1a1 builds the service's entrance, `POST /runs`, against the
token shapes above; the Codex plan review demanded the binding, precedence and
body rules be fixed before the build.

**Decision.**

- **The workload binding (U1').** A workload token binds its app only when it
  carries realm role `agent-workload` (which only service accounts hold), its
  `azp == client_id ==` one registered `workload_client`, `exp - iat` is at
  most 300 s, `sub` is present, and `azp` and `client_id` are non-empty
  strings. `client_id` alone proves nothing. An empty `sub` never reaches the
  binding: webauth's verifier refuses it as an invalid claim, so the token is
  an unusable credential. No refusal is decided from unverified claims. A
  `workload_client` registered for two apps stops startup.
  One process-scoped webauth `JwtVerifier` holds the key-set cache, refresh
  lock and cooldown, and runs off the event loop.
- **The order of checks.** The first failure answers. Verification of the
  workload token, then of the user token, precedes every binding check, so
  each token's 401 or 503 answers before any 403:

  | Check | Answer |
  |---|---|
  | The workload token is missing, or webauth refuses it while parsing it, before it looks up a key (among others: longer than 8192 characters, not a compact JWS whose header and claims set are strict JSON objects, a number that overflows to infinity, a header or claims set nested deeper than 8 levels, a forbidden header member (`crit`, `jku`, `jwk`, `x5u`, `x5c`, `b64`), an `alg` other than RS256, an invalid `kid`) | 401 `unauthorized`, one body, `WWW-Authenticate: Bearer` |
  | The service holds no current key set and cannot fetch one, or a token's `kid` is missing from a current key set and the refresh fails; this is decided when a token is verified (in practice the workload token, verified first) | 503 `unavailable`, `Retry-After: 30` |
  | The workload token fails its signature, issuer, `aud` ∌ `agent-service` or time check, has an empty `sub`, or carries no `kid` while the current key set holds more than one RS256 key; these are checked only once a current key set is held | 401 `unauthorized`, one body, `WWW-Authenticate: Bearer` |
  | The user token is missing or not `Authorization: Bearer <token>`, or fails verification as the workload token above; or it has no `sub`, a `sub` that is not valid Unicode text (A1a4 O7), or a malformed group claim | 401 `unauthorized`, one body, `WWW-Authenticate: Bearer` |
  | The workload binding above fails (among others: an absent workload `sub`, an absent or empty `azp` or `client_id`); the user's `azp` differs from the workload's; the user's `sub` equals the workload's | 403 `forbidden` |
  | The body, at most 256 KiB enforced before buffering, parsed as strict JSON (no `NaN`, no member twice): its `v` is absent, not an integer, or not 1 | 400 `unsupported_version` |
  | The body is not strict JSON or not UTF-8 (UTF-16 and UTF-32, with or without a BOM, are refused; a UTF-8 BOM is accepted), too large, or not exactly a `RunRequest` | 422 `invalid_request` |
  | The purpose is not in the app's list | 403 `forbidden` |
  | H5 routing answers `not_enabled`; no run starts | 403 `not_enabled` |
  | The run's capability would exceed 8,192 characters (A1a4 item 12): a subject wider than the preflight's reserve, or a wall clock past 2286 | 422 `capability_too_large` |
  | Admitted | 200 `text/event-stream` |

  Every rejection is a `RunRejection` with `Cache-Control: no-store`, and no
  answer or log line carries token text.
  A token whose `kid` is missing from a still-current key set answers 503
  `unavailable` when the key set cannot be refreshed (webauth v0.15.0 reports
  `KEY_SET_UNAVAILABLE`: the `kid` may be a rotated key it could not fetch),
  and 401 when a successful refresh lacks the `kid`. With no current key set
  the 503 row answers, whatever the `kid`.
  A token without a `kid` is read against the key set's only RS256 key and
  admitted when it verifies; against several RS256 keys it answers 401
  (webauth v0.15.0, OIDC Core 10.1). The `kid` only selects a key: the
  signature, issuer, audience, expiry and role checks still apply.
- **The policy.** The service reads one TOML file into frozen models that
  forbid unknown fields; a refused file stops startup naming the field.
  `[identity]` requires issuer, key-set URL (https), audience, subscription
  group and group claim, none with a default, and takes the clock leeway
  `leeway_seconds` (0 to 300), the only field that defaults, to 0;
  `[runs]` the run's pace and limits: `heartbeat_interval_s` (an integer, 1 to
  300, default 15), `stall_timeout_s` (default 30), `idle_timeout_s`
  (default 120) and `max_duration_s` (default 600), which must satisfy
  heartbeat < stall <= idle < maximum, and the
  budget `max_tool_calls` (default 30) and `max_bytes` (default 4 MiB,
  4194304), and `max_parallel_cli_runs` (default 5), all positive integers
  outside that ordering;
  `[profiles.<name>]` at most one profile, for Claude, with a required `token_file`;
  `[cli]` the absolute `claude_binary` and the `claude_version` (three integers)
  it must report, required by a Claude profile and probed at startup;
  `[purposes."<p>"]` an optional `cli_provider` and `api_alias`;
  `[apps.<app>]` its `workload_client`, its purposes, and its tools' effects,
  where an unlisted tool counts as side-effecting. `agent_service.routing.route`
  is the one pure H5 decider.
- **The stream.** A runner is a port per route; production installs none
  until A1b and A2b, so an admitted run on a route without one ends as
  `failed: provider_unavailable`, never as a success. `agent_service.run_events`
  owns the mapping from the core's turn events onto the wire: a tool call's
  input is deep-copied into its event, and an event that `encode_event` cannot
  write (a non-JSON value, too deep, too large) or that the service's own
  `RunStream` refuses ends the run as `failed: provider_error` before it is
  sent. An event past the run's tool-call or byte budget ends the run as
  `failed: limit_exceeded` before it is sent. The event is encoded and
  charged before `RunStream` records it, so a refused one never advances the
  sequence the client checks.
- **Ruled sentences.** S1 both tokens are required; S2 a user token cannot
  pose as the workload; S3 the request names only purpose and turn; S4 H5
  decides the route; S5 a clean stream ends with exactly one terminal; S6
  non-JSON tool input never reaches the wire; S7 no runner is never a fake
  answer. The threat checklist: token substitution, confused deputy via
  `azp`, cross-app replay, algorithm confusion and key-set DoS (both
  webauth's), token leakage into logs and bodies; stale groups for a token's
  lifetime are accepted.

**Consequence.** Tests prove each row with minted tokens over a real socket,
and `tests/test_service_keycloak.py` proves the entrance end to end with real
Keycloak 26.8.1 tokens in the `keycloak-token-shapes` job.

## H2 Whose subscription

**Context.** The operator wants the subscription settled "an einer zentralen
Stelle", without per-person mapping.

**Decision.** The Keycloak group `/ki-abo` alone decides who may use the
subscription route. Its members use the installation's subscription profiles;
everyone else gets the API route or `not_enabled`. The operator's test accounts
are members of `/ki-abo`.

**Consequence.** A friend put into `/ki-abo` would use the operator's own
subscription, which the consumer terms forbid (architectural reading). The
operator accepted this residual risk; avoiding it is the admin's responsibility.
Per-person subscription mapping waits for a named caller.

## H2b Subscription profiles are configuration

**Context.** The operator holds several subscriptions and wants them adjustable.

**Decision.** A profile is one subscription login, with a name, a provider,
whether background purposes may use it (H12), and its login. A Claude profile's
login is one `claude setup-token` token read from the profile's `token_file`
(A1b ruling, Q2 a). Until the additional provider routes arrive, the service
accepts at most one subscription profile, for Claude. It requires a `token_file`
and a `[cli]` section: at startup the service runs `claude_binary --version` in a
throwaway home, and a version other than `claude_version`, or none it can read,
exits 2 naming both versions (A1b-7a).
Codex and Grok profiles are refused at startup; their login contracts are decided
with their routes. The profile list is service configuration, kept as code
in edge; the login files live in `~/.secrets`, never in a repository.

**Consequence.** Nothing in the code is tied to the operator; another
installation lists its own profiles.

## H3 App identity and tools

**Context.** A user token alone does not prove which app sends the request, and
a request that names its own tools or MCP endpoint could reach anything.

**Decision.**

- Every request carries the forwarded user access token and a short-lived
  Keycloak client-credentials token of the app's own workload client. The
  service checks both; the workload client id must equal the user token's `azp`.
- "Apps hold no keys" means no LLM or subscription credential. The workload
  client secret is an ordinary app credential under the secrets owner.
- A static, server-side app registry maps each workload client to its allowed
  purposes, its own canonical MCP origin, which no other app shares (A1a4 O3), and
  a tool allowlist, the keys of its tools table (A1a4 O5). The request carries no
  MCP URL, MCP token, route, provider, model or user id; the one exception is the
  job ticket of H12.
- The capability is per run, not per call. When the service admits a run it
  mints one RS256 capability (token type `agent-capability+jwt`, at most 8,192
  characters; a larger one refuses the admission as `capability_too_large`) bound
  to the audience (the app's MCP origin), the app, the principal, the run id, the
  purpose and the tool allowlist. It expires at ⌊admission + `[runs]
  max_duration_s`⌋, whether or not the run ended earlier, and nothing revokes it
  (A1a4 O4). The CLI never gets it: a per-run relay on
  loopback owns the run's tool events, the run's MCP config names only the relay, and
  the capability reaches only the app (A1b ruling, Q1 a). The API-route tool loop
  sends the same capability to the app. The app's MCP server
  checks it on every request, opens a fresh DB session and re-checks ownership.
- Hardening: redirects are denied, egress is limited to the registered origins,
  Host and Origin are validated, and the service sits on an internal Docker
  network only, with no tunnel line and no Traefik route. The listener stays
  internal-only as defence in depth. mTLS/SPIFFE is out of scope.

**Consequence.** Tool reach is decided once, server-side, per app; a leaked
capability is useful only for one app, one user and one allowlist, until its run's
maximum duration has passed since admission, even after the run ended.

### A1a4 ruling (operator and head, 16.10.2026)

A1a4-1 settles the signing key. A1a4-2a (capability format, size preflight) and
A1a4-2b (minting at admission) extend this ruling; the rule numbers continue there.

**O1 Key generation and custody (operator, 16.10.2026).** The operator creates the
key by hand, never the service. The procedure creates a new file exclusively with
mode 0600 in an operator-controlled directory, refuses any existing destination
including a symlink, and generates rotation candidates at a distinct path before an
explicit replacement of the active key (bash, OpenSSL 3, GNU coreutils):

```bash
(
  set -eu
  umask 077
  dir="$HOME/.secrets/agent-service"
  dest="$dir/signing-key.pem"
  mkdir -p "$dir"
  chmod 700 "$dir"
  tmp="$(mktemp "$dir/.new-signing-key.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -pkeyopt rsa_keygen_pubexp:65537 -out "$tmp"
  ln -T "$tmp" "$dest"
)
```

`mktemp` creates the candidate exclusively, with mode 0600, inside the 0700
directory. `ln -T` (`link(2)`) publishes it under `dest` only when no entry of that
name exists, a dangling or live symlink included, and never follows one. The trap
removes the temporary name in every case. A rotation candidate is the same block
with `dest="$dir/signing-key.next.pem"`. The key is an unencrypted PEM private key
(the procedure writes PKCS#8), RSA-3072 with exponent 65537. `[signing] key_file`
names the absolute path the service sees; the service reads it once at startup and
never creates, rewrites or reloads a key.

**O2 Rotation and compromise (operator, 16.10.2026).** One key exists at a time.
Rotate on a suspected compromise or whenever the operator chooses; there is no
schedule. To rotate:

1. create a candidate at the distinct path (O1);
2. replace the active key with
   `mv -T "$HOME/.secrets/agent-service/signing-key.next.pem" "$HOME/.secrets/agent-service/signing-key.pem"`;
3. restart the service.

The `kid` changes with the key. Restarting stops the service's runs but does not
revoke their capabilities: previously issued tokens remain acceptable until expiry
wherever the old public key remains trusted. Compromise recovery also requires
removing that public key from every verifier and invalidating its cached trust, with
the executable procedure owned by A1m/A1d before live use. webauth, for one, keeps a
fetched key set for an hour. The holder of a compromised private key can mint new
tokens, so waiting until issued capabilities expire is not recovery. No previous
private signing key is kept. This item keeps and publishes no previous public
verification key; whether 24-hour tickets or a published key set need one after a
planned rotation is decided by A1a5 and A1m; such a grace period never applies after a
compromise.

**Decisions.**

- **D1 the cut.** The signing key first, then the capability in two items: A1a4-2a
  (format, `mcp_origin`, size preflight) and A1a4-2b (admission, hand-over, log boundary).
- **D2 where the key enters.** `[signing] key_file`, read and checked while the
  configuration validates, into `config.signing.key`: one read and one refusal path
  (exit 2), and `cli.py` is unchanged.
- **D3 the `kid`.** The RFC 7638 SHA-256 thumbprint of the public key; never configurable.
- **D4 direct dependencies.** `agent_service.signing` imports PyJWT and
  `cryptography`, so the service declares both rather than relying on webauth's extra.
- **D5 publishing.** Nothing is published in A1a4; A1m and A1a5 choose an endpoint or a file.
- **D15 explicit token types.** `SigningKey.sign(claims, token_type)` has no default
  type (RFC 8725 section 3.11). The capability uses `agent-capability+jwt`; A1a5 picks
  its own. webauth and PyJWT do not check `typ`, so a verifier must check it itself.

**Items.**

1. **Where the key comes from.** `[signing]` is optional and holds exactly one
   member, `key_file`, an absolute path (`~` is not expanded) without control or non-printing
   characters. The key is read and
   checked once, at startup. The service never creates, writes, re-reads or reloads
   a key file; a missing file is a refusal, never a generated key.
2. **What file is accepted.** The file is opened once with
   `O_RDONLY | O_NONBLOCK | O_CLOEXEC`; a symlink is followed. Type, mode, size and
   bytes all come from that descriptor; the path is never opened again. It must be a
   regular file granting nothing to group or others (`st_mode & 0o077 == 0`), at most
   16 KiB, holding exactly one unencrypted PEM private key that is RSA with 3072 bits
   and exponent 65537. Every refusal, including `OSError`, `ValueError`, `TypeError`
   and `UnsupportedAlgorithm`, stops startup with exit 2 and one line holding
   `signing.key_file`, the path and the service's own reason, never key bytes or a
   library message. Not covered: an owner who rewrites the opened file, and whether
   the directory holding it is trustworthy (operator and A1d).
3. **The key id.** The RFC 7638 SHA-256 thumbprint of the public key (members `e`,
   `kty`, `n`, sorted, no whitespace, unpadded base64url), 43 characters.
4. **Signing.** `SigningKey.sign(claims, token_type)` returns a compact RS256 JWS
   whose header is exactly `{"alg":"RS256","kid":<kid>,"typ":<token_type>}`; webauth's
   `JwtVerifier` accepts it.
5. **Secrecy.** No `repr`, `str` or log line of `SigningKey`, `SigningConfig` or
   `ServiceConfig` shows key material; a `repr` names only the `kid`. Tests generate
   keys at run time; no private key is committed.
6. **Generation and rotation** are those of O1 and O2 above: one key at a time, a
   restart is required, and a restart does not revoke issued capabilities.
7. **`[signing]` is optional.** An app with an MCP origin needs it (item 8), and each
   admitted run of such an app carries a capability (item 12).

**A1a4-2a rulings (operator and head, 16.10.2026).**

- **O3 MCP origins: https only, one per app.** `[apps.<app>] mcp_origin` accepts only
  `https://` origins, and two apps never share one, even at different paths. edge
  runs step-ca; Traefik obtains short-lived certificates for its internal TLS
  entrypoint from it (A1d). Wiring the host-configured CA file into the library's
  child environment is a separate library item.
- **O4 Capability lifetime.** A capability is valid from admission until
  `exp = floor(admission + max_duration_s)` on the service's wall clock, also after
  its run ended early. Nothing revokes it. Whoever holds the token can replay every
  call it permits, for that app, that user and that tool list, until `exp`; the run
  id in `sub` names a run and does not prove it live, and the tool-call budget does
  not count calls made with the token outside the run. The app's per-request
  ownership checks (H3) stay mandatory.
- **O5 Which tools.** Membership in an app's `tools` table grants capability
  permission, absence denies it, and an empty table grants none. The `tools` claim is
  the table's keys, sorted.
- **O6 Capability size.** `identity.py` gains no subject policy beyond O7's
  valid-Unicode rule. A capability is at
  most `MAX_CAPABILITY_CHARACTERS = 8192` characters, webauth's token limit. The
  configuration preflight measures every purpose of every app with an origin against
  a reserve; admission checks the actual compact token (A1a4-2b) and answers an
  oversized one with HTTP 422 `capability_too_large`, never `unavailable`,
  `unauthorized` or `invalid_request`.
- **D6 format owner.** `agent_client.capability`, strict and frozen like the wire.
- **D7 claims.** Exactly `iss`, `aud`, `sub` (the run id), `iat`, `exp`, `app`,
  `purpose`, `principal {iss, sub}` and `tools`; no `jti`, `nbf`, groups or turn text.
- **D8 issuer.** `CAPABILITY_ISSUER = "agent-service"`; installations are told apart
  by their keys.
- **D10 registry.** `mcp_origin` is required when an app lists tools and optional otherwise, and must be in its serialized form: a
  lower-case DNS host, a non-default port only, no path. A host whose last label is
  all decimal digits or `0x` followed by zero or more hexadecimal digits, and a bracketed IPv6
  address, are refused.
- **D13 expiry.** `iat = floor(now)` and `exp = floor(now + max_duration_s)` from one
  reading of the wall clock at admission. `exp - iat` can exceed `max_duration_s` by
  up to 1 s, so a verifier never checks it. The run's own maximum counts from
  `run_started` on the monotonic clock, later, so a capability can expire before the
  run is stopped; a call after `exp` fails closed.
- **D17 the cut.** A1a4-2a owns every public contract and the startup check;
  A1a4-2b owns admission, the hand-over to the runner and the log boundary.
- **D18 subject reserve.** The preflight measures with a subject of 255 quotation
  marks (255 characters at the widest printable-ASCII JSON encoding). A longer or
  wider subject is admitted whenever its actual capability fits.
- **D19 measuring by signing.** The preflight signs its measurement claims through
  the same `signed_capability` that admission uses and drops the token at once. A
  verifier rejects future-issued capabilities with its trusted clock, which rejects
  measurement tokens (`iat` 9,999,999,999) as `issued-in-future`.
- **D20 one-line refusals.** `secret_file.shown_text` is the one escaper of
  operator-supplied text in a refusal; `config.py` shows the config path and every
  location component through it.

**A1a4-2a items.**

8. **The registry names each app's MCP origin.** The value is decided by `McpOrigin`:
   `https://`, a lower-case DNS host (labels of 1 to 63 letters, digits and hyphens,
   none starting or ending with a hyphen, 253 in all, no trailing dot), a port only
   when it is not 443, in decimal digits
   from 1 to 65535 without leading zeros, and nothing else. Origins are unique, and an
   origin needs `[signing]`, and an app that lists tools needs an origin. A refusal names `apps.<app>.mcp_origin` and gives the
   canonical form where one exists and would itself load.
9. **The format.** `CapabilityClaims` (frozen, strict, `extra="forbid"`) with token
   type `agent-capability+jwt`; `principal.sub` is the exact verified subject, never
   truncated, normalized or substituted. A changed claim set gets a new token type.
10. **What a capability says.** `capability_claims` is the one place for the claim
    values and the expiry arithmetic; `signed_capability` signs them with the
    configured key.
11. **How long a capability can be.** `signed_capability` raises
    `CapabilityTooLarge` for a compact token over 8,192 characters. The preflight
    runs it for every purpose of every app with an origin with `PREFLIGHT_SUBJECT`
    and `iat = 9_999_999_999`, and refuses startup naming `apps.<app>.tools`, the
    purpose, the measured length and the limit. Passing it does not guarantee that
    every accepted principal fits; the guard at admission stays authoritative.
15a. **Documents.** This ruling, H3's registry bullet, the A0b codes table and the
    README rows `agent_client.capability`, `agent_service.config` and
    `agent_service.capability`.

**A1a4-2b rulings (operator and head, 16. and 17.10.2026).**

- **O7 A verified subject the capability cannot carry (operator, ruled (a),
  16.10.2026).** Identity refuses a user token whose `sub` is not valid Unicode
  text as `unauthorized` (401), on `POST` and `GET`, for every app. It applies
  `UnicodeText`, the one decider of valid wire text, through a module-level
  `TypeAdapter` in `identity.py`, right after the missing-`sub` check and before
  every 403. Such a token is an unusable credential, as an empty `sub` already is;
  RFC 7493 (I-JSON) §2.1 forbids such strings and RFC 8259 §8.2 calls their handling
  unpredictable; and every later token that names the user needs the same answer
  from one owner. No valid-Unicode subject is refused: length and characters stay
  free (O6). The alternative, a `forbidden` answer only for an app with an origin,
  was rejected. Counterexamples: a 500 with a traceback; a subject repaired,
  replaced or escaped into another string; `capability_too_large` for it.
- **D11 hand-off.** `AdmittedRun.capability: Capability | None`, with no default, so
  every construction names it. The alternative, `Runner.run(admitted, capability)`,
  would change every runner.
- **D12 wall clock.** One injected aware-UTC clock drives capability minting and the
  default run-record store: `create_app(..., wall_clock=utc_now)` builds
  `RunRecords(wall_clock)` and mints at `wall_clock().timestamp()`. A caller that
  injects a store supplies the same clock to both when it needs shared time. Token
  verification keeps webauth's own clock.
- **D16 the failure log line.** `run_liveness.log_failure` stays the one writer of
  failure lines, by class name and positions, never the message, notes, chained
  exceptions or `exc_info`. Admission adds no category table and no log helper. A
  runner that names an exception class or a function after run data is outside the
  guarantee; that runner's slice owns it (A1b, A2b, under H7).

**A1a4-2b items.**

12. **When a capability exists.** Identity, body, purpose and routing refusals
    happen before capability construction: no runner is reached and nothing is
    signed. For each admitted run of an app whose `capability_scope` is not `None`,
    on either route, the service draws the run id, reads the wall clock once, builds
    the claims with `capability_claims`, signs them with `signed_capability` and
    builds `AdmittedRun(..., capability=...)`. An app without an origin gets
    `capability = None`, and nothing is signed. When `signed_capability` raises
    `CapabilityTooLarge`, the answer is 422 `capability_too_large`: no runner is
    called, no stream starts and no record begins. One WARNING record names the app,
    the measured length and the limit, and neither the token nor the subject. With no
    runner installed, the capability is minted into `AdmittedRun` and dropped with it.
13. **The runner receives it.** `AdmittedRun.capability` is the capability itself, not
    a lookup from configuration. Its token verifies under webauth with `aud` equal to
    the app's origin and fails for another origin; `repr` of the admitted run shows
    no token.
14. **Never on the wire, in an answer, or in a log.** The token never appears, whole
    or as its signature segment, in a frame, a response header, a rejection body or a
    service log record (its message and its formatted output, uvicorn's included). The
    one new log line is item 12's WARNING, written from the app, the measured length
    and the limit, never `str` of the refusal and never `exc_info`. A runner's failure
    and its close's failure reach the log only through `log_failure`. Deferred to A1b
    and A2b under H7: content a runner yields; a runner whose `run` raises before it
    returns its generator (`runners.py` states the contract); and a recorder failure
    whose chain carries a yielded event.
15b. **Documents.** This ruling, H3's capability bullet and Consequence, the A1a1
    order table and the README rows `agent_service.app`, `agent_service.signing`,
    `agent_service.capability` and `agent_service.runners`.

### A1b MCP transport (head decision HD1, 17.10.2026)

A1b-9v. The relay of Q1 (a) speaks MCP to the CLI, and the service speaks MCP to
the app. This section records which implementation does that and vets the
alternative. HD1 is a head decision for internal machinery, not an operator
ruling; the operator may object. It serves Q1 (a), ruled by the operator on
17.10.2026 ("OK"), and keeps line 16 true: the capability never reaches the CLI.
It does not reopen the choice of the MCP SDK beyond vetting HD1. The sources of
the SDK were read and never executed (PyPI, 17.10.2026).

**c1 Redirects.** Both SDK versions follow same-origin, method-keeping redirects
themselves and ignore `follow_redirects` (2.3.0: `streamable_http.py:727-732`).
Only a raising `httpx2` response hook pre-empts that. H3 denies redirects, so the
SDK's default would have to be defeated.

**c2 SSE resumption.** 2.3.0 resumes an interrupted SSE answer with
`Last-Event-ID` (`streamable_http.py:511-514`). No parameter disables it.

**c3 The automatic GET stream.** After `initialized`, 2.3.0 opens a GET stream with
its own reconnect (`streamable_http.py:224-273`). The inspected SDK's
`handle_get_stream` returns immediately without a session ID
(`mcp/client/streamable_http.py:232`, mcp 2.3.0), so the automatic
GET applies only when a session ID exists. No parameter disables it. A hook
failure inside a request task ends the whole transport task group.

**c4 Logger names and what each logs.**

- The SDK uses the logger names `mcp.*` and also `client` (`client/session.py:71`;
  1.30.0: `:30`).
- It logs outgoing messages, including tool arguments, at DEBUG
  (`streamable_http.py:635`), and SSE messages at DEBUG (`:191`).
- It logs parse failures with `logger.exception` (`:212`, `:462`), redirect
  locations at WARNING (`:244`, `:292`, `:391`) and transport failures with
  `logger.exception` (`:693`).
- `httpx2` logs request lines at INFO. `httpcore2.*` logs the `repr` of trace data,
  response headers included, at DEBUG (`httpcore2/_trace.py:39-45`).
- A logger with `propagate = False` and no handler still reaches stderr through
  `logging.lastResort`.

**c5 The added dependencies.**

- `mcp` 2.3.0 adds four dependencies that are not locked: `mcp-types`,
  `jsonschema`, `sse-starlette` and `python-multipart`. Fact 12 of #115 named
  `opentelemetry-api` where `python-multipart` belongs. `mcp`'s `httpx2` is locked
  only through the core's optional `api` extra (`anthropic`,
  `pyproject.toml:17-18`), not as a service dependency. 1.30.0 needs `httpx<1`, `httpx-sse` and `pydantic-settings`.
- PyPI metadata of 2.3.0 (17.10.2026) lists, besides those: `opentelemetry-api`,
  `anyio`, `httpx2`, `pyjwt[crypto]`, `starlette`, `typing-inspection`, `uvicorn`,
  `pydantic` and `typing-extensions`. At `9b78f3a`, `uv.lock` has no package named
  `mcp-types`, `jsonschema`, `python-multipart`, `sse-starlette` or `mcp`;
  `opentelemetry-api` (through FastAPI) and the others named here are locked
  already.
- The core already installs `httpx` 0.28.1 (`pyproject.toml:13`), with `httpcore`
  1.0.9 and `h11` 0.16.0 below it (`uv.lock`). The service imports none of them
  and declares none (`packages/agent-service/pyproject.toml:9-20`).

**c6 `mcp-types`.** PyPI metadata of `mcp-types` 2.3.0 (17.10.2026) requires
`pydantic>=2.12.0` and `typing-extensions>=4.13.0`, with Python `>=3.10`; it
lists no other dependency, so by its metadata it installs alone. `mcp` 2.3.0
pins it with `mcp-types==2.3.0`. Whether it imports alone was not executed.
Decision: not taken. The pin inside `mcp` does not constrain a consumer that
installs `mcp-types` alone, and its published metadata supports standalone use:
envelope types, per-version validators and a protocol-version registry. HD1's own
client needs about seven messages, and a dependency for them is not worth its
maintenance.

**c7 What the pinned CLI needs from the relay** (section "A1b-0c the CLI as an
MCP client, no login", cells c1 to c9, claude 2.1.302).

- It connects before its first message and without a login, and calls
  `tools/list`. The request order is `POST server/discover`, `POST initialize`,
  `POST notifications/initialized`, `GET`, `POST tools/list`.
- It sends the `Authorization` value of the config's `headers` unchanged on every
  request, `GET` included.
- The `server/discover` probe (protocol `2026-07-28`) falls back to `initialize`
  when it gets a JSON-RPC error with status 200, or a plain `{"error":"unsupported"}`
  body with 400 or 404.
- `initialize` carries `protocolVersion` `2025-11-25`. If the server answers
  `2025-06-18` or `2025-03-26`, the CLI sends that version afterwards; with an
  unknown version (measured: `1999-01-01`) it stops and init reports the server
  `failed`.
- A plain `application/json` answer is accepted (`Accept: application/json,
  text/event-stream` on `POST`). A 202 with an empty body for
  `notifications/initialized` is accepted.
- `MCP-Protocol-Version` is absent on `initialize` and present on every later
  request. `Mcp-Session-Id` is echoed when the server issued one.
- The `GET` answered 405 is neither retried within 5 s nor counted as a failure.
- After a 401 on every `POST` it stops after requests 1 and 2 and init reports
  `failed`.
- An HTTP server config starts no process, and no `DELETE` and no
  `notifications/cancelled` arrives at the end of the session, so in the measured cases
  (EOF on stdin, `SIGTERM`) the relay cannot learn the session ended from the CLI.

**c8 The decision: HD1.** The service speaks a small JSON-only MCP profile itself,
with no MCP SDK dependency.

- *What it needs.* About seven messages. Towards the app: `initialize`,
  `initialized`, `tools/list` and `tools/call`. Towards the CLI, the same plus
  `ping` and `cancelled`.
- *Why not the SDK.* SDK 2.3.0 would need four of its behaviours defeated (c1 to
  c4: its redirects, its SSE resumption, its GET stream and its logging; the GET
  stream applies only when a session ID exists, and HD1 allows a profile without one) and it
  would bring four new dependencies (c5). The relay's measured needs (c7) are met
  by plain POST with JSON answers, a 405 answer to the `GET`, and a JSON-RPC error
  with status 200, or a 400 or 404, to `server/discover`.
- *What it is built on.* The client uses `httpx`, the stack the core already
  installs. The service declares it directly with the core's range, because it
  imports it; `httpx2` would be a second HTTP stack that reaches the service only
  through an optional extra. The relay uses `asyncio.start_server` with `h11`,
  already locked below `uvicorn` and `httpcore`. It uses no server framework, so
  the relay takes no signal handlers, no `sys.exit` and no access log. The locked
  `uvicorn` 0.54.0 would replace the SIGINT and SIGTERM handlers on the main thread
  (`Server.serve()` with `capture_signals()`, `uvicorn/server.py:88-90`,
  `:331-349`), call `sys.exit(STARTUP_FAILURE)` when the bind fails (`:188-192`),
  and cancel the remaining request tasks at shutdown without closing their
  transports (`:281-311`).
- *Interoperability.* It supports A1m's JSON-only profile only, not arbitrary
  Streamable HTTP servers: POST, `application/json` answers, the
  `MCP-Protocol-Version` header after `initialize`, 202 with an empty body for
  notifications, and an optional `Mcp-Session-Id`.
- *Cost.* About 400 lines, plus interoperability proofs against the pinned CLI
  (A1b-0c) and against A1m.
- *Neighbouring decisions.* HD2: results are released only after acceptance (line
  17); the relay answers the CLI's `tools/call` only after the runner has been
  resumed past that call's `ToolResultEvent`, and a refusal, cancellation or close
  prevents the answer. HD3: the init check detects; it does not gate (line 18).
- *The execution barrier (A1b-10b1a, 18.10.2026).* Only a call whose acceptance the
  service confirmed by resuming the runner past it is forwarded (line 17, ADR item
  20). Refusals answer `-32602` (`Invalid params`), the shape after which the pinned
  CLI continued (A1b-0e c7). The duplicate rule covers pending ids only, because ids
  restart on a new connection after a new `initialize`; such a request is a new call
  that needs its own acceptance (c5). At most `MAX_RELAY_CONNECTIONS - 1` calls are
  pending, because each holds a connection; that keeps the `-32602` refusal reachable
  on a fourth admitted connection, but reserves no capacity, because idle keep-alive
  connections count toward the cap too (c2 measured one response's calls in series on
  one connection; whether that bounds a production run is not measured). A close and
  `notifications/cancelled` both end a pending call (c8); A1b-10b1b1 builds the cancel and A1b-10b1b2 the close.
- *The CLI's cancel (A1b-10b1b1, 18.10.2026).* `notifications/cancelled` is routed to
  pending ids only; others are ignored, as the MCP specification allows (E5 named the
  call's id on another connection). A give-up, by a cancel or by the cancellation of
  the call's task, is recorded before it returns, also through every task whose await
  chain reaches the call through awaits that forward `Task.cancel()` at once (a direct await or
  `asyncio.gather`), so an acceptance in the same step
  forwards nothing. A call given up is never forwarded unless it already was, and its
  request is closed without a byte (`Unanswered`), because the MCP specification asks a
  receiver not to answer a cancelled request and the connection is needed for the next
  call. A handed-out call gets an `is_error` `ToolResultEvent` at its acceptance, so
  the app's client sees that it never ran. A forwarded call is not cancelled upstream
  (design item 10); only its waiter ends (head ruling 18.10.2026, decision A), and its
  outcome is retrieved exactly once before `settle()` returns. Inferred, not measured:
  that the CLI would not read an answer written after its cancel; E5's pairing of the
  cancel with the stream's timeout stays provisional (c8). Not measured: the CLI's
  reaction to the closed connection (A1b-12b).
- *The CLI's close (A1b-10b1b2, 18.10.2026).* While the owner works, the relay watches
  the connection: an end (FIN, half-close or RST) ends the request, because the pinned
  CLI closed a call it gave up on (A1b-0e E3); bytes do not, because a pipelined request
  is the next one (L7), and the watch keeps reading after them so a later close is still
  seen. The watch cancels a pending answer in the very step it sees the end, and the
  listener awaits the answer through an await chain that forwards `Task.cancel()` at once (`asyncio.gather`), so for a `tools/call` the barrier's give-up is
  recorded before any later acceptance (A1b-10b1b1, guarantee G). An answer that
  completed before the end is written, and the connection then ends. Retained unparsed
  bytes are bounded by `MAX_PIPELINED_BYTES` (one further request), counted over
  everything the connection holds unparsed, beyond which the request ends like a close.
  Not measured: the CLI's reaction to the relay closing its connection (A1b-12b).
- *H9.* Its import contract says "client → stdlib, pydantic, HTTP and the MCP SDK
  only". That sentence is about `agent-client` and is untouched; HD1 adds no
  MCP SDK to any package, so it stays true.

## H4 MCP is optional per app

**Context.** Not every app has tools.

**Decision.** Songmaker's co-writer gets a Streamable HTTP MCP endpoint built
from its existing handlers. Presentator needs none. There is no dynamic
discovery and no arbitrary endpoint.

**Consequence.** An app without tools needs no MCP server at all.

## H5 The one switch, principal-aware

**Context.** Provider, model and route choices were scattered across app
settings; the operator wants one place to switch.

**Decision.** An app sends only a closed purpose value. Each entry in the
service's one purpose table may hold an optional CLI provider or named profile,
and an optional LiteLLM alias. Routing order:

1. If a CLI entry exists, a profile for that provider exists, and the principal
   is in `/ki-abo` → CLI with that profile.
2. Otherwise, if an API alias exists and the user token carries the app's group
   (`/apps/<app>`) → API via LiteLLM.
3. Otherwise → `not_enabled`.

LiteLLM alone maps aliases to upstream models and keys.

**Consequence.** The app-local provider/model/route settings for a moved purpose
are removed from settings, API and UI.

## H6 Wire contract

**Context.** A streamed run must end unambiguously, and a broken stream must not
leave the client guessing whether side effects happened.

**Decision.** A versioned, discriminated SSE union. Every event carries a
protocol version, a run id and a monotonic sequence. Every run ends with exactly
one terminal event: `succeeded`, `failed` (a typed code and a safe message) or
`cancelled`, except that the stream of a dropped client is cut without one; the
run's ending is in its record (A1a2).

- A heartbeat runs while idle, and an idle timeout applies.
- Limits for bytes, time and tool calls are enforced.
- Cancellation is explicit; nothing is retried automatically once dispatched.
- Run status is queryable: `GET /runs/{id}`, authorised by the same two tokens,
  returns the run's `RunStatus`. Records live in memory, readable for up to one
  hour after the run ended, subject to earlier eviction once 10,000 ended
  records are held and to a process restart; there is no database.
- When a run's response ends, however it ends (the stream finished, the client
  disconnected, the response was cancelled, or sending failed), the service
  closes the run's event generator itself before the response call returns
  (`_RunResponse`, A1a2 item 11). It never relies on finalization by Starlette,
  CPython or the garbage collector. The close stops the runner once; on the CLI
  route that kills and reaps the process group (A1b). A disconnect, drop or
  supervision failure preserves an already accepted terminal. Only a run
  without an accepted terminal uses `cancellation_terminal()`: `cancelled`, or
  `failed: interrupted` once a side-effecting tool call was emitted (A0b wire
  ruling (a)). `client_lost` independently records the first loss cause.
- Presentator's speech events (`sentence`, `audio`, `done`) stay in Presentator
  and are not added to this contract.

**Consequence.** A client learns how a run ended from its terminal event, or
from `GET /runs/{id}` while the record is held (the status bullet above); once
it is gone, the ending is unknown, and a run that emitted a side-effecting call
may have changed state.

### A0b wire ruling (head, 15.10.2026)

**Context.** A0b had to enumerate the contract and settle whether the core's
existing `agent_providers.events` is that contract.

**Decision.**

- `agent_client.wire` is the one owner of the wire, and
  `agent_client.run_stream` owns its stream rules. Every event carries
  `v` (protocol version 1), `run_id` and `seq`; the union is closed and
  discriminated on `type`. A new variant or field is a new protocol version.
- Core events are wrapped, not replaced: `agent_providers.events.StreamEvent`
  stays the core's in-process provider-turn type, which hosts on v0.1.2 still
  consume. The mapper from it to the run events lands in A1a1 with its caller.
- (a) An explicit cancel follows the same interrupted rule as a disconnect.
  Whether a cut-short run ends as `cancelled` or `failed: interrupted` is
  decided by the effect class of its emitted tool calls (H9): one
  side-effecting call, counted from its emission, makes it `interrupted`.
- (b) `not_enabled` and `busy` are HTTP rejections before admission, never
  stream events. The rejection body is typed (`RunRejection`), and its codes
  map to these statuses:

  | Code | HTTP status |
  |---|---|
  | `not_enabled` | 403 |
  | `busy` | 429 with `Retry-After` |
  | `unsupported_version` | 400 |
  | `unauthorized` | 401 |
  | `forbidden` | 403 |
  | `invalid_request` | 422 |
  | `unavailable` | 503 with `Retry-After` |
  | `not_found` | 404 |
  | `capability_too_large` | 422 |

  Amended by A1a0 (head rulings U3 and U4, 15.10.2026): `unavailable` means
  the service could not fetch the key set it checks tokens against, and a
  client retries it, unlike `unauthorized`. `not_found` is the one answer for
  an unknown, an expired and a foreign run alike, so it leaks nothing.

  Amended by A1a4 (O6, 16.10.2026): `capability_too_large` means the run's
  capability would exceed 8,192 characters; it is refused before any runner,
  stream or record, and a client does not retry it.

- (c) A `failed` event with any other code does not say separately whether a
  side effect happened. The client sees the `tool_call` events, and after a
  lost stream it asks `GET /runs/{id}`.
- (d) The heartbeat is a typed event and counts in `seq` like every other
  event.
- (e) A call gets one answer: a second `tool_result` for it is
  `duplicate_tool_result`. `failed: interrupted` means the run may have
  changed state, so without an emitted side-effecting call it is
  `interrupted_without_side_effect`.
- (f) Framing (A0b2, `agent_client.sse`): one SSE frame per event, holding
  `id: <seq>`, `event: <type>` and one `data:` line of compact UTF-8 JSON
  with the whole event, ended by a blank line; no frame exceeds
  `MAX_EVENT_BYTES` (1 MiB). The decoder accepts LF, CRLF and CR line ends, a
  leading byte order mark, `:` comments and `retry:`, and names any other
  field as `malformed_frame`; a frame missing or repeating `id`, `event` or
  `data`, or a line that is not UTF-8, is `malformed_frame` too. It names a
  larger frame as `oversized_frame`, data that is not a JSON object as
  `invalid_json`, and an `event` or `id` that disagrees with the data's
  `type` or `seq` as `event_type_mismatch` or `id_mismatch`. The first
  violation ends the decoding.
  A `tool_call`'s `input` nests at most `MAX_TOOL_INPUT_DEPTH` (128) objects
  and arrays deep, so depth never stops a built input from encoding; a
  deeper one is refused when built, and a decoder names it `schema_mismatch`,
  or `invalid_json` once it nests past the JSON parser's own limit.

**Consequence.** A client validates a stream against one contract, and a
second wire owner cannot appear without contradicting this ruling.

### A1a3 liveness ruling (head, 16.10.2026)

The numbers 15 s heartbeat, 30 s stall, 120 s idle, 600 s maximum, 30 tool
calls and 4 MiB are provisional defaults of `[runs]`; A1b's live proof may retune them.
Piece 1 of A1a3 (`agent_service.run_liveness`):

1. One injected clock; the origin t0 is the reading when `run_started` is
   produced. The maximum is t0 + `max_duration_s`, whatever the runner does.
2. `run_started.heartbeat_interval_s` is the configured cadence.
3. A heartbeat is queued when the cadence passes with no frame queued and the
   client has taken every queued frame; otherwise that beat is skipped. It
   counts in `seq`.
4. Idle time is only time spent waiting on the runner. Every runner event,
   including an empty text fragment, resets it; a heartbeat and a stalled
   client do not.
5. After `idle_timeout_s` of waiting on the runner the run ends `failed:
   idle_timeout`.
6. At the maximum the decision `failed: limit_exceeded` is taken and the
   runner's cancellation starts at once, even when the client does not read.
   The terminal is queued after the runner's close has finished, to a client
   that has not been dropped.
7. One step is outstanding at a time; a heartbeat never cancels or restarts it.
8. At one instant: a client past its stall deadline is dropped first, then a
   runner event already yielded, then the maximum, then idle, then the
   heartbeat.
9. Every ending closes the runner and waits for the close before the terminal
   is queued; nothing but frames already encoded and the terminal follows the
   decision. A dropped client gets no terminal and no new frame.
10. Closing the stream stops the runner the same way, once, and returns only
    after the close has finished, however often the closer is cancelled.
11. A close that raises is logged at ERROR with the run id and changes
    nothing else. An unexpected error in the supervision stops the runner and
    then cuts the stream, so a decoder sees it truncated.
12. When a stream ends no task of the run is alive and the runner was closed
    once.
13. A cut-short run says the service's own fixed text and keeps its code even
    after a side-effecting `tool_call` (ruling (c) holds).
14. Bad numbers stop startup: strict types (no `true`, `15.5` or `inf`),
    heartbeat 1 to 300, and heartbeat < stall <= idle < maximum.

Piece 2 of A1a3 (`agent_service.run_events`, the budget beside `seq`):

15. A run sends at most `max_tool_calls` `tool_call` frames; the next one is
    not sent and the run ends `failed: limit_exceeded`. Results, text and
    heartbeats do not count.
16. The runner-caused frames (`assistant_text`, `tool_call`, `tool_result`,
    `succeeded`) total at most `max_bytes`, counted as the length of the SSE
    frame `encode_event` returns, the unit of `MAX_EVENT_BYTES`. A total equal
    to the limit is sent, one byte more is refused, a refused final answer
    ends the run `limit_exceeded`. `run_started`, heartbeats and `failed` are
    not counted, so the byte budget never keeps a terminal from being queued;
    a dropped client gets none. The per-frame cap stays
    `provider_error` and may exceed a small `max_bytes`.
17. An event the budget refuses is charged nothing, takes no `seq` and is never
    recorded by `RunStream`; the limit terminal takes the `seq` it would have had. The
    order is encode, budget (calls before bytes), stream rules, send.
18. A limit hit is returned as a terminal to a client that takes frames, so it uses piece 1's stop path
    (rule 9): the runner is stopped before the terminal is queued, even with
    the slot full. A run stalled behind a pending frame does not reach the
    breaching event until the client reads, is dropped, or the maximum ends
    the run.
    A yielded event is handled before the maximum (rule 8); at a double
    breach the tool-call text wins.
19. The texts are the service's own: "The run made too many tool calls." and
    "The run sent too much data."; the code survives a side-effecting call
    (rule 13). `max_tool_calls` and `max_bytes` are strict integers of at least
    1, outside the heartbeat < stall <= idle < maximum ordering.
20. A runner executes a tool only after the service has accepted its
    `tool_call`; a refused call (limit, stream rules, unsendable) is never
    executed, and stopping the runner at once is not an execution barrier.
    This anchors the promise that the event comes "before the tool runs"
    (`wire.py:211`). A1b (CLI runner) and A2b (API route and tool loop) own
    the obligation and one test each.

A runner must end when its step is cancelled or closed, and its close must
finish in bounded time (`runners.py`).

Piece 3 of A1a3 (`agent_service.run_liveness`, the slow client):

21. A frame that enters the one-slot queue must be taken within
    `stall_timeout_s` (default 30) of that instant. The deadline exists only
    while the slot is full; each new frame, including the held frame and the
    terminal an ending queues, starts its own. A heartbeat is a frame;
    `run_started` never enters the slot. A silent run's client is dropped at
    most `heartbeat_interval_s + stall_timeout_s` after its last take. The
    drop is proven in CI with the fake clock and a Starlette response whose
    `send` blocks; no socket test is in CI because when a real `send` blocks
    and the wall-clock pacing of a reader are not deterministic. The durable
    socket proof is A1b's live proof (epic #18, A1b row).
22. At the stall deadline the runner's stop starts at once through piece 1's
    stop path, exactly once, and one WARNING names the run id. No frame is
    queued afterwards: no heartbeat, no held frame, no terminal. The frame
    already in the slot may still be taken; then the stream ends without a
    terminal and the client's decoder reports `truncated`.
23. An ending already decided (`succeeded`, `provider_error`, `idle_timeout`,
    `limit_exceeded`) waits behind a stalled client under the stall deadline
    alone: the maximum, idle and heartbeat instants are past and ignored. The
    held frame and the terminal are queued one after the other, each with its
    own window; at the deadline what is not queued is discarded.
24. At one instant the drop comes before piece 1's order (rule 8): anything
    handled after the stall deadline could only become a frame nobody takes,
    and a handled event would charge the budget for a call the client never
    gets.
25. A client that takes each frame within its window is never dropped and sees
    no change; the deadline sequence may differ, because the supervisor wakes at
    deadlines and not when the slot falls vacant, so a stall instant set while
    the slot was full can still wake the supervisor after the client took the
    frame.
26. Closing the stream during or after a drop stops nothing twice and returns
    only after the runner's close has finished (rule 10).
27. After the drop's cleanup the pump and the supervisor are gone; only the
    client's consumer remains, until the response ends and closes the run's
    generator (A1a2 item 11). Residuals: the dropped client's connection stays
    open until it reads or leaves, because ASGI has no abort and asyncio
    flushes before it closes (owner: A1d, edge); a frame the supervisor had
    already encoded is accepted but never received, and the run's record
    counts it in `events` (A1a2 item 1). `stall_timeout_s` is a provisional
    default.

### A1a2 run-record ruling (head, 16.10.2026)

Pieces 1 and 2 of A1a2: the store and `GET /runs/{id}`
(`agent_service.run_records`, rules 1 to 6, A1a2-1), and recording (rules 7 to
13, A1a2-2).

Decisions:

- D2: one injected aware-UTC wall clock reads every timestamp and every expiry.
  A clock step moves every expiry with it: a backwards step lengthens a
  record's life (the bound caps how many records pile up), a forwards step
  shortens it.
- D4: `RunStatus` lives in `agent_client.wire` at protocol version 1, with no
  client method. H6 named `GET /runs/{id}` before v1 shipped, and a new body for
  a new endpoint changes no existing v1 body.
- D5: the retention (1 h) and the bound (10,000 ended records) are constructor
  defaults of `RunRecords`, not configuration.

Rules:

1. A record holds the run id, the app and principal that started it,
   `started_at` and `ended_at`, the number of events the service accepted
   (`events`), the ending (state and failure code) and `client_lost`. It holds
   no event text, no tool input or output, no `Failed.message`, no token and no
   group. `GET /runs/{id}` answers `RunStatus`: `v`, `run_id`, `state`
   (`running`, `succeeded`, `failed`, `cancelled`), `failure_code` (a
   `FailureCode` exactly when `failed`), `events` (at least 1),
   `started_at`, `ended_at` (null exactly while `running`) and `client_lost`
   (null, `dropped` or `disconnected`). Timestamps are aware UTC; an
   `ended_at` earlier than `started_at` is valid. The body carries neither the
   app nor the principal, and `events` counts what the service accepted, not
   what the client received.
2. An ended record is readable while `now() - ended_at` is less than the
   retention. A running record never expires and is never evicted; its hour
   starts when it ends. Ended records are indexed by `(ended_at,
   completion_sequence)` in timestamp order, so a backwards clock step cannot
   hide an expired record behind one that has not expired. Every store operation (`begin`,
   `accepted`, `over`, `lost` and `read`) purges expired entries before
   anything else; `over` enforces `max_ended`
   by evicting the ended record that comes first in that order. Running
   records do not count against the bound. No task, timer or sleep exists.
3. The writer API is `RunRecords.begin(started, app, principal) ->
   RunRecorder`, `RunRecorder.accepted(event)`, `RunRecorder.over(fallback)`
   and `RunRecorder.lost(reason)`. The opening event is counted by `begin`
   only. A terminal is kept as the pending ending and shows only after `over`;
   the fallback ends a run that accepted no terminal and is not counted. A bare
   `over`, a second `over`, an `accepted` after `over`, an `accepted` of
   `RunStarted` and a `begin` of a held id are errors. `lost` records the first
   cause only, never changes the ending, `events`, `ended_at` or the expiry
   order, and restores nothing once the record is gone. `read` builds a new
   frozen snapshot each time.
4. `read(run_id, app, principal)` returns the status only when the record's
   app, issuer and subject all equal the caller's; every other case, including
   an unknown, expired or evicted id, returns `None`. The lookup compares a
   real-or-dummy owner on all three fields on every read, with no
   existence-dependent early return, exception, log or I/O. No constant-time
   claim is made. No group or purpose is re-checked.
5. `GET /runs/{id}` checks both tokens first, with the same checks, order and
   answers as `POST /runs`, and looks at neither the id nor the store before
   that. Then only the RFC 9562 text form of a UUID (8-4-4-4-12 hexadecimal
   digits, any letter case) names a run; every other spelling answers
   `not_found`. A missing, foreign, expired or evicted run answers the one
   `not_found` (404, `Cache-Control: no-store`), byte-identical in body and
   headers except `date`. The owner gets 200 `application/json` with
   `RunStatus` and `no-store`. No answer carries token text; the handler and
   the store log nothing.
6. There is one `RunRecords` per `create_app`, never a module global. Nothing
   is persisted, restored or announced. Documentation promises retention for up
   to one hour after completion, subject to earlier capacity eviction and
   process restart.
7. A record begins when `run_started` is accepted, and it counts every accepted
   event. `_WireRun` calls `begin` right after accepting `RunStarted` and hands
   each later accepted event to the recorder, after `RunStream.accept` passes
   and before the frame is returned. A refused event reaches neither. A
   generator that is never advanced creates no record and never calls
   `runner.run`.
8. A run is over when its runner is closed, and the store hears it at once.
   The cleanup task (`_reap`) calls the writer's `run_over()` as soon as the
   runner's close has finished or failed, exactly once, whoever waits and
   however often a waiter is cancelled, and before any wait for frame
   delivery. `_supervise`'s finalizer guarantees that the slot closes; it is
   not the completion notification. `_stop` raises what the decider decides
   once the runner is stopped, so a recording defect cuts the stream the way a
   supervision failure does. No step of a run's ending raises its failure past
   the decider: the supervision (its clock, its drive and the drop report it
   makes) and the cleanup (the disconnect report and the record's end) record
   each failure in the run's one collection (`run_liveness.RunFailures`), in
   the order the failures happened. One pure decider (`run_liveness._exit_of`)
   reads that collection, any cancellation and the body's outcome: a
   cancellation always wins, whether it was caught during teardown or arrived
   in the body; then the first cleanup failure; then the first supervision
   failure, unless the body raised. Every other failure is logged exactly
   once. Every failure the service logs, a runner's and a failed close
   included, is logged by its class and its stack positions
   (`file:line:function`), never by its message, notes or chained exceptions.
   A propagated failure is not logged by the service: Starlette and the ASGI
   server log it in their own format, message and chain included. Every
   failure that can propagate comes from the service's own recorder, clock or
   loss report, so none carries provider or user text; a runner's failure is
   logged through the one helper instead and never propagates. With no
   runner, the run is recorded complete before its first frame is handed out.
9. The ending is the accepted terminal; `cancellation_terminal()` is used only
   when there is none. `run_over()` passes no fallback when a terminal was
   accepted. The fallback's state and code are all that is read: it is never
   sent, accepted or counted, and the record holds no provider text.
10. Loss causes are latched when detected, and the first one wins. A drop calls
    `client_lost(dropped)` before it awaits the stop; a supervision failure
    does the same before its `finally` stops the runner; `run_frames` catches
    `CancelledError` and `GeneratorExit` and latches `disconnected` before any
    cleanup is awaited. `dropped` means the service cut the stream;
    `disconnected` means the run's response ended before its stream finished:
    the client left, or the server cancelled or failed the response. It does
    not say whether the last frame arrived.
11. The service closes its run's frames when the response ends, however it
    ends. The owner is `_RunResponse` in `agent_service.app`, the one type a
    run's response has: a `StreamingResponse` whose `__call__` awaits
    Starlette's call inside `aclosing` on the run's generator. Starlette may
    return or raise (the stream finished, the client disconnected, the
    response task was cancelled, or `send` failed); in every case the
    generator's `aclose()` is awaited before the call returns or re-raises, and
    Starlette's own outcome propagates unchanged. A generator that is already
    closed makes `aclose()` return at once. The close stops the runner once and
    returns only after the runner's close has finished, so when the response
    call ends, a started run's runner is closed, its record is ended and no
    task of the run is alive. Nothing relies on finalization: no
    `gc.collect()`, no reference count reaching zero, no event-loop
    async-generator hook. Below ASGI spec 2.4, which is what uvicorn 0.54.0
    speaks, a disconnect is reported at `receive` and seen at once. Under ASGI
    spec 2.4 or later, Starlette does not listen for `http.disconnect`; it
    detects connection loss when an in-flight or subsequent `send` raises.
    There is no unconditional heartbeat-sized detection bound: runner cleanup
    can delay the next frame, and sending depends on the server's transport
    behavior. Once Starlette's call returns or raises, the service awaits
    closure of its event generator before its response call exits. Both paths
    reach the same close.
12. One private method of `RunStream` decides which ending a cut-short run must
    have; `cancellation_terminal` and the stream rules both ask it.
13. Recording reaches the app through `create_app`, which passes its store to
    the run. A record is gone after a restart, and no document promises that a
    client always learns the ending.

## H7 CLI isolation and admission

**Context.** The CLI route runs a powerful agent under the operator's login.

**Decision.** Each run gets a pinned, tested Claude Code version, an
unprivileged user, an empty private working directory, a strict per-run MCP
config with an exact tool allowlist, restricted built-in tools, no session
persistence, a finite max-turns, and a process-group kill and reap on cancel or
timeout. Tokens never appear in argv, events or logs. Admission allows a
service-wide limit of parallel subscription runs (default 5, `[runs]
max_parallel_cli_runs`, one limit for the whole service, not per profile or per registered app);
a CLI run takes a lease on one place before its run id or any signature exists.
Beyond the limit a request gets `429 busy` with `Retry-After: 10` at once. Refusals
and API runs take nothing. The lease is released once the run has stopped, even if
its record fails; a response that ends before its run began releases it too. Only a
lease that still holds its place can free it. The limit lives in one process,
because the service runs one replica with one worker (A1d). There is no queue and
no fallback to the API route.

**Consequence.** Subscription runs are bounded by that limit, and a full service says
so instead of waiting.

### A1b ruling (operator, 17.10.2026)

The operator ruled the open questions of A1b (the CLI runner) on 17.10.2026. The
words below are copied from the A1b item (#115, "Ruled by the operator"); the
journal links hold the operator's questions and the reasoning. The mechanism of
the parallel-run limit stands once, in H7's Decision.

- **Q3 What contains a run? Ruled (a) on 17.10.2026**, verbatim: "q3=a" ([journal](https://github.com/overnightworks/agent-providers/issues/115#issuecomment-6231933018), with the operator's question and the head's reasoning).
  - **What (a) is:** one non-root service that makes itself non-dumpable, starts Claude only with the CLI's own restrictions (`--tools ""`, `--strict-mcp-config`, `--setting-sources ""`) and reaps the run's process group. The model reads no file and runs no command; the CLI's restrictions plus the relay (Q1 a) are its whole reach. Lines 15 and 19-21 state the promise. The service reaps no child it did not start and traces none (no `waitpid(-1)`, `wait3` or `wait4`, no ignored `SIGCHLD`, no `SA_NOCLDWAIT`, no ptrace request), because the run's unreaped leader is what keeps the run's process-group id from being reused.
  - **Accepted residuals:** the pinned, checksum-verified Claude binary is trusted, and a faulty one could read the signing key. A process the CLI itself takes out of the run's process group, with `setsid` or with `setpgid`, survives until the container restarts, and its run's place is freed (Q-A (b), ruled 17.10.2026). The group reap contains accidental stragglers of the trusted CLI, not deliberate evasion; its remaining kernel edges are named as residuals under line 20, and the host properties they need are A1d conditions.
  - **If built-in tools are ever enabled,** the provider's own sandbox is switched on. A1b builds no sandbox of its own.
- **Parallel subscription runs. Ruled 5 on 17.10.2026**, verbatim: "nicht pro profile => der dienst soll das festlegen. kein automatisches ausweichen! default ist "hoch" keine ahnugn => 20 oder so? push back wo ich falsch liege!", then, after the head's pushback, "ja 5" ([journal](https://github.com/overnightworks/agent-providers/issues/115#issuecomment-6232221116)).
  - The mechanism of this ruling stands in H7's Decision. The measurement of memory per run before the default is ever raised is A1b-12a.
- **Q1, Q2 and Q8, and the ruled lines. Ruled on 17.10.2026**, verbatim: "OK", in answer to the head's two open questions ([journal](https://github.com/overnightworks/agent-providers/issues/115#issuecomment-6232435577)).
  - **Q1 How does a tool call wait for the service's acceptance? (a), the relay.** A per-run relay on loopback owns the run's tool events. The run's MCP config names only the relay, the relay's intercepted call is the one source of `tool_call` and `tool_result`, and the capability reaches only the app (line 16). The service speaks MCP itself (HD1); H3's capability sentence and #18's row change (A1b-R). The design is in the slice cards (A1b-10a1 to A1b-10b2).
  - **Q2 What form does a profile's login take? (a), the setup token.** One `claude setup-token` token per profile, "a one-year OAuth token" (code.claude.com/docs/en/authentication, 17.10.2026), passed only as `CLAUDE_CODE_OAUTH_TOKEN` in the child's closed environment, never as a file and never in any argv.
    - `[profiles.<name>] token_file` is read once at startup with the key's file rules (line 14), and an API-key shape is refused.
    - No higher-ranked credential can reach the child: Bedrock, Vertex, Foundry, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY` and `apiKeyHelper`. The environment admits only named variables, `--setting-sources ""` drops the settings files, and `apiKeySource` is checked on every run.
    - Renewal: you replace the file the O1 way and restart (line 2). An expired token ends runs with the closed cause of line 11. Whether the CLI writes or refreshes a credential is measured in A1b-0b1.
    - Codex and Grok use device-code logins whose renewal only the service may own; that is decided when their routes arrive (A1r3).
  - **Q8 What happens when termination cannot be confirmed? The quarantine, not an exit** (line 20). The place of a run whose end cannot be confirmed stays taken, and the service does not restart. It keeps sending `SIGKILL` and checking in the background, frees that one place once the group is empty, and logs one ERROR line. The other places keep working; if every place hangs, the service answers busy, and restarting the container is the way out; it also ends the remaining run processes. This replaces the earlier recommendation "the service exits", which dated from the single-slot design. The design is in the slice cards ("Design: containment lost", A1b-3f).
  - **The three principles stand as presented:** one long-lived token file per profile, whose absence or fault stops startup and which never appears in argv or a log, while the operator's own login stays untouched; Claude has no built-in tools and gets only the app's tools, every call accepted by the service first; at most 5 parallel subscription runs, and a place is reused only once its earlier run is confirmed ended.
  - **The lines:** 2, 14, 15, 16 and 17 are ruled as worded; 20 is ruled as reworded to the quarantine. Line 9 is ruled with the five places above.

#### The ruled lines 2, 9, 14, 15, 16, 17 and 20 (verbatim from #115's expectation list)

2. [ruled by the operator 17.10.2026: "OK"] **Ein Profil nennt seine Token-Datei. Ein neues Token heißt: Datei tauschen und den Dienst neu starten. Dein eigenes Claude-Login bleibt unberührt.**
   - *Erbt (Betreiber):* Kein App-Container hält ein Abo-Login (`:64`). *Erbt (Kopf):* H2b. Neu ist die Form des Logins (Q2 = a, das Setup-Token).
   - *Beispiel:* Läuft das Token nach einem Jahr ab, erzeugst du mit `claude setup-token` ein neues. Du ersetzt die Datei unter `~/.secrets/agent-service/` und startest den Dienst neu. Dein Claude im Terminal bleibt angemeldet, weil der Dienst `~/.claude` nie liest.
   - *Gegenbeispiel:* Der Dienst liest die Datei vor jedem Lauf neu, sodass ein Tausch ohne Neustart wirkt.
   - *Default:* entfällt; wie formuliert entschieden.
   - *Beleg:* A1b-0b1, A1b-6a.
9. [ruled by the operator 17.10.2026: "ja 5"] **Es gibt N Plätze für den ganzen Dienst, Default 5, in der Konfiguration des Dienstes. Erst wenn alle belegt sind, kommt sofort „beschäftigt“ mit Wiederholzeit. Kein automatisches Ausweichen auf den API-Weg.**
   - *Geerbt (Kopf):* H7 „one active subscription run … 429 Retry-After … no queue“ (jetzt: N parallele Abo-Läufe, ein Limit für den Dienst, nicht je Profil) und #68 „die Weigerung kommt vor der Capability“. Nur Abo-Läufe belegen einen Platz.
   - *Beispiel:* Mit den fünf Plätzen belegt will Songmaker einen sechsten Abo-Lauf: Er bekommt sofort „The service is busy; retry later.“ mit `Retry-After: 10`. Ein Lauf auf dem API-Weg, den er ausdrücklich verlangt, startet trotzdem und belegt nichts; ein abgewiesener Antrag belegt nichts. Steht in der Konfiguration `max_parallel_cli_runs = 2`, sind es zwei Plätze.
   - *Gegenbeispiel:* Der sechste Antrag wird nicht still auf den API-Weg umgeleitet, nicht in eine Warteschlange gestellt, und `Retry-After` nennt nicht die Restzeit des laufenden Laufs (bis zu 600 s).
   - *Default:* 5 Plätze (`[runs] max_parallel_cli_runs`, ganze Zahl ab 1, beim Start geprüft; 0 oder negativ wird abgelehnt); Wiederholzeit 10 s als Konstante neben `RETRY_UNAVAILABLE_AFTER_SECONDS` (`app.py:59`).
   - *Beleg:* A1b-1; A1b-12a misst den Speicher je Lauf, bevor der Default erhöht wird.
14. [ruled by the operator 17.10.2026: "OK"] **Eine fehlende, offene oder falsche Token-Datei stoppt den Start, ohne ihren Inhalt zu zeigen.**
    - *Erbt (Betreiber):* die Datei-Regeln des Signierschlüssels (O1 und A1a4 Punkte 1-2, `:327-341`). Neu ist ihre Anwendung auf das Abo-Token.
    - *Beispiel:* Ist die Datei für die Gruppe lesbar, ist sie ein Ordner, oder steht darin ein API-Schlüssel (`sk-ant-api…`) statt eines Abo-Tokens, startet der Dienst nicht. Er nennt `profiles.<name>.token_file` und den Pfad, nie den Inhalt.
    - *Gegenbeispiel:* Der Dienst startet, und nur die Abo-Läufe scheitern, bis die Datei stimmt.
    - *Default:* entfällt; wie formuliert entschieden.
    - *Beleg:* A1b-6a.
15. [ruled by the operator 17.10.2026: "OK"] **Das Modell eines Laufs liest keine Datei und führt keinen Befehl aus. Es erreicht nur die erlaubten Werkzeuge der App (Zeile 18). Dem gepinnten Claude-Programm selbst wird vertraut.**
    - *Erbt (Betreiber):* Q3 = (a), „q3=a“ (17.10.2026). *Erbt (Kopf):* H7 „unprivileged user, empty private working directory, restricted built-in tools“ und #18 „only the selected profile's login is visible“.
    - *Was (a) festlegt:*
      - Das Programm startet ohne eingebaute Werkzeuge (`--tools ""`), nur mit der MCP-Konfiguration des Laufs (`--strict-mcp-config`) und ohne Einstellungsdateien (`--setting-sources ""`). Meldet es beim Start trotzdem ein eingebautes Werkzeug, endet der Lauf (Zeile 18).
      - Der Dienst läuft nie als root und macht sich nicht auslesbar (`PR_SET_DUMPABLE` 0). Speicher und Umgebung des Dienstes sind darum für das Programm zu.
      - Werden eingebaute Werkzeuge je eingeschaltet, wird die Sandbox des Anbieters eingeschaltet. Eine eigene Sandbox bauen wir nicht.
    - *Angenommene Restrisiken:*
      - **Das Programm ist vertraut.** Es läuft mit dem Benutzer des Dienstes. Es selbst könnte den Signierschlüssel, die Token-Datei, die Ordner anderer Läufe und jede Datei des Dienstes lesen und ändern und dem Dienst Signale schicken. Ein fehlerhaftes Programm könnte mit dem Schlüssel Capabilities für jede App und jeden Nutzer ausstellen. Dagegen schützt allein, dass genau die gepinnte, per Prüfsumme geprüfte Version läuft (Zeile 7).
      - **Der Entkommene.** Ein Prozess, den das Programm selbst mit `setsid` oder `setpgid` aus der Prozessgruppe seines Laufs nimmt, lebt bis zum Neustart des Containers und reicht so weit wie das Programm (Zeile 19).
    - *Das Netz:* Der Lauf teilt das Netz des Dienstes und erreicht, was der Container erreicht. Dazu gehören der Port des Dienstes und das Relay seines Laufs auf dem lokalen Loopback, die internen Dienste in den Netzen des Containers (LiteLLM, die MCP-Server der Apps, step-ca) und die freigegebenen Ziele.
    - *Beispiel:* Bittet jemand im Co-Writer das Modell, den Signierschlüssel zu lesen oder `ls` auszuführen, hat es dafür kein Werkzeug. Es kann nur die Werkzeuge rufen, die `[apps.songmaker] tools` nennt.
    - *Gegenbeispiel:* Der Dienst baut um jeden Lauf eine eigene Sandbox, damit auch ein fehlerhaftes Programm den Schlüssel nicht sieht (Q3 d, nicht gewählt).
    - *Default:* entfällt; wie formuliert entschieden.
    - *Beleg:* A1b-4a, A1b-5, A1b-7n, A1b-12b.
16. [ruled by the operator 17.10.2026: "OK"] **Kein Token steht in einer Befehlszeile, im Stream, in einer Antwort oder im Log, und die Capability erreicht das Claude-Programm nie.**
    - *Erbt (Kopf):* H7 „tokens never in argv, events or logs“ und A1a4 Punkt 14. Neu ist, dass die Capability nur noch die App erreicht. Das ändert H3 (`:242-243`) und den Zeilentext von #18 (Q1 = a, das Relay).
    - *Beispiel:* Wer auf dem Rechner `ps` aufruft, sieht in keiner Befehlszeile ein Token. Das Abo-Token steht nur in der Umgebung des Programms; dort lesen es der Benutzer des Dienstes und root. Schickt die App die Capability in einer Antwort zurück, wird die Antwort nicht weitergegeben, und der Lauf scheitert.
    - *Gegenbeispiel:* Die Capability steht, wie H3 heute sagt, in der 0600-MCP-Datei des Laufs. Das Programm könnte sie lesen und bis zu 10 Minuten lang die Werkzeuge der App in deinem Namen aufrufen (Betreiber-Ruling O4).
    - *Default:* entfällt; wie formuliert entschieden.
    - *Beleg:* A1b-0b1, A1b-4a, A1b-9b1, A1b-9b2, A1b-9c, A1b-10a2, A1b-10b2, A1b-11.
17. [ruled by the operator 17.10.2026: "OK"] **Kein Werkzeug läuft, bevor der Dienst genau diesen Aufruf angenommen hat. Kein Ergebnis erreicht das Modell, bevor der Dienst genau dieses Ergebnis angenommen hat.**
    - *Erbt (Kopf):* A1a3 Punkt 20 und #38. Die Wirkung in der App ist unumkehrbar, darum wird gesprochen entschieden.
    - *Beispiel:*
      - Schickt das Programm zwei Aufrufe gleichzeitig und erlaubt das Budget nur noch einen, wird nur der erste in der App ausgeführt, der zweite nie.
      - Ist ein Ergebnis zu groß für das Byte-Budget, endet der Lauf, und das Modell sieht das Ergebnis nie.
      - „Angenommen“ heißt: Der Dienst hat das Ereignis geprüft, gezählt, aufgezeichnet und in den Ausgang gelegt (Fakt 4). Ob der Client es schon empfangen hat, sagt das nicht.
    - *Gegenbeispiel:* Das Programm ruft die App selbst auf, und der Dienst meldet den Aufruf nur nachträglich. So arbeitet der Kern heute mit `--allowedTools` (Fakt 9).
    - *Default:* entfällt; wie formuliert entschieden.
    - *Beleg:* A1b-10b1, A1b-10b2, A1b-11.
20. [ruled by the operator 17.10.2026: "OK", als Quarantäne nach Q8] **Kann der Dienst nicht bestätigen, dass die Prozessgruppe eines Laufs leer ist, bleibt der Platz dieses Laufs belegt. Der Dienst versucht es im Hintergrund weiter, gibt genau diesen Platz frei, sobald die Gruppe leer ist, und schreibt eine ERROR-Zeile. Die anderen Plätze arbeiten weiter; der Dienst beendet sich nicht und startet nicht neu.**
    - *Neu* (Q8). Ein Überlebender verbraucht das Abo und könnte Werkzeuge rufen; darum geht sein Platz an keinen neuen Lauf.
    - *„Bestätigt“ heißt:* Der Dienst schickt der Prozessgruppe des Laufs SIGKILL und sieht danach in `/proc` keinen lebenden Prozess dieser Gruppe mehr, innerhalb der Frist ab dem ersten SIGKILL. Das gilt, solange der Gruppenführer nicht abgeholt ist und kein Mount `/proc` verdeckt. Ein Prozess, den das SIGKILL erreicht hat, kann keinen neuen Prozess mehr mit `fork` oder `clone` anhängen; darum genügt eine Zählung nach dem SIGKILL, und jeder lebende Prozess, den sie in der Gruppe sieht, verhindert die Bestätigung. Der beendete, noch nicht abgeholte Gruppenführer zählt nicht als lebend; der Dienst holt ihn erst danach ab, und bis dahin vergibt der Kernel seine Nummer an keinen anderen Prozess und keine andere Gruppe, sodass jedes Signal des Dienstes nur diesen Lauf trifft. Zeigt `/proc` nicht die eigene Sicht des Dienstes (kein procfs, ein anderer PID-Namespace, `hidepid`), ist das Ende nicht bestätigt, und der Platz bleibt belegt. `killpg(pgid, 0)` kann das Ende nicht bestätigen: Der nicht abgeholte Gruppenführer hält die Gruppe ansprechbar, und ein abgeholter gibt die Nummer frei.
    - *Im Hintergrund:* Der Dienst behält den nicht abgeholten Gruppenführer, schickt der Gruppe in festem Abstand weiter SIGKILL und zählt danach jedes Mal neu, bis er keinen lebenden Prozess der Gruppe mehr sieht. Erst dann holt er den Gruppenführer ab und gibt den Platz frei. Die ERROR-Zeile nennt den Lauf und kommt einmal, nicht bei jedem Versuch.
    - *Angenommene Restrisiken:*
      - Die Bestätigung fängt versehentliche Nachzügler des vertrauten, gepinnten Programms, keine absichtliche Flucht (Zeile 15); jeder der folgenden Fälle endet spätestens mit dem Neustart des Containers.
      - Einen Prozess, der die Gruppe mit `setsid` oder `setpgid` verlässt, zählt diese Bestätigung nicht, und das SIGKILL an die Gruppe erreicht ihn nicht. Er und seine Nachkommen leben bis zum Neustart des Containers (Zeile 19), und der Platz seines Laufs wird trotzdem frei. Dass das Programm keinen solchen Prozess abkoppelt, beruht auf dem Vertrauen in die gepinnte Version (Zeile 15).
      - Wechselt ein Prozess zwischen dem SIGKILL und dem Ende der Zählung in die Gruppe oder aus ihr heraus, kann die Zählung ihn verfehlen; das gilt auch für einen, den das SIGKILL erreicht hat und der nur noch ein begonnenes `setpgid` oder `setsid` beendet, und für einen, den ein berechtigter Elternprozess mit `setpgid` verschiebt. Er gilt dann wie der Entkommene, auch wenn er in die Gruppe zurückkehrt.
      - Startet ein Prozess der Gruppe genau beim SIGKILL ein neues Programm (`exec`), kann die Zählung ihn einen Augenblick lang verfehlen, doch er hat das SIGKILL dann schon und kann keinen Prozess mehr mit `fork` oder `clone` anhängen.
      - Ein Mount über `/proc` oder über einem seiner Nummernverzeichnisse kann der Zählung einen lebenden Prozess verbergen; darum legt der Container keinen solchen Mount an (A1d-Bedingung).
      - Einen Prozess, den der Kernel den Dienst nicht mit SIGKILL beenden lässt (andere Rechte, eine Capability oder eine LSM-Regel), erreicht das SIGKILL nicht; darum setzt der Container `no-new-privileges`, und seine Rechte und LSM-Regeln lassen den Dienst seine eigenen Nachkommen desselben Benutzers mit SIGKILL beenden (A1d-Bedingungen).
      - Hängen alle Plätze, antwortet der Dienst auf jeden Abo-Lauf „beschäftigt“. Der Ausweg ist dann, den Container neu zu starten; das beendet auch die übrigen Prozesse der Läufe.
    - *Beispiel:* Von fünf Plätzen hängt einer, weil ein Prozess seines Laufs nach SIGKILL nicht endet. Im Log steht eine ERROR-Zeile mit diesem Lauf. Vier Abo-Läufe laufen nebeneinander weiter, ein weiterer bekommt „beschäftigt“. Endet der hängende Prozess zwei Minuten später doch, ist der Platz ohne Neustart wieder frei. Hängen alle fünf, bekommt jeder Abo-Lauf „beschäftigt“, bis du den Container neu startest.
    - *Gegenbeispiel:* Der Dienst beendet sich, und der Container startet neu (die frühere Empfehlung Q8 a); das bricht die vier gesunden Läufe ab. Oder der Platz wird trotzdem frei, und ein neuer Lauf startet neben dem Überlebenden (früher Q8 c).
    - *Default:* entfällt; als Quarantäne entschieden.
    - *Beleg:* A1b-3, A1b-3f, A1b-8.

### A1b measured: grok 1.0.60

Measured on 18.10.2026 on the installed `grok-1.0.60-linux-x86_64` (191,294,880
bytes, sha256 prefix `10e6af64b247`; the hash was the same after the last run,
so the self-updater never touched the binary). Operator ruling 18.10.2026: G0a
uses no login. No cell read, copied, hashed or printed an auth file or a token,
and none ran `login`, `logout` or `update` without `--check`. No model turn was
possible, so no quota was used. Cells that need a login are listed under
"Deferred to G0b / G0c" below.

#### A1b-G0a the Grok CLI without a login

**Harness (every cell).** Each run is the CLI under `bwrap` with
`--die-with-parent --new-session`, the network unshared (loopback only, except
E1), an environment of exactly `PATH`, `HOME`, `GROK_HOME` and `TMPDIR`
(`HOME=/probe/home`, `GROK_HOME=/probe/home/.grok`, both on a fresh empty
directory in scratch), a fresh empty working directory `/probe/work` that holds
only `prompt.txt` (`hello`), `/usr` and the needed `/etc` files bound
read-only, and the binary bound read-only as `/opt/grok/grok`. Wall deadline
30 s, then a process-group kill; after every run `/proc` is scanned for
processes carrying `GROK_HOME=/probe/home/.grok`. Every run held
`/tmp/probe-stack.lock`. `write_record` is the one writer of evidence: it drops
the run's random bearer string and every 12-character window of it. BASE is
`--prompt-file /probe/work/prompt.txt --output-format streaming-json --deny "*"
--max-turns 1 --no-subagents --disable-web-search`. The scripts follow
verbatim (`<scratch>` stands for the scratch directory). The only secret-shaped
value is the throwaway MCP server's bearer, which the driver passes through the
child's environment, and which is also never in a configuration file: the
config names `${PROBE_BEARER}` and `bearer_token_env_var`.

**Summary for the consumers.**

| Cell | Result |
|---|---|
| V1 | Version line parses with one regex; `[channel]` suffix is absent in an empty home. |
| V2 | Every BASE flag and `--no-leader` reach the login refusal; the control exits 2. |
| N1 | No login: one `{"type":"error","message":...}` line, exit 1, 0.33 s, no leader. |
| T1 | All six switches accepted by name; `--sandbox` cannot be driven inside this harness. |
| S1 | `--system-prompt-override` and `--rules` accepted; replace or append is deferred. |
| L1 | `--max-turns 1` accepted; `0` refused at parse (`1..=4294967295`). |
| A1 | The stream stops at the refusal; the ACP `initialize` result carries `availableCommands` and `modelState` with no login. |
| M1 | `mcp doctor` reaches a local HTTP server without a login and sends the bearer from the environment. |
| M2 | MCP tools are named `server__tool`; `MCPTool(...)` and `mcp__` rules; no MCP-only deny switch. |
| F1 | A first run writes `$GROK_HOME` (docs, logs, sessions, config); `HOME` unset falls back to the passwd home. |
| B1 | No process survives a refusal; `agent leader` creates a socket and lock under the home and lives until killed. |
| E1 | No hostname is looked up or connected to without a login; only `update --check` resolves `x.ai`. |

**V1 version and pin.** In the empty home, `grok --version` and `grok version`
print `grok 1.0.60 (01a8e1c591c0)` (exit 0, 0.04 s); `grok version --json` prints
`{"currentVersion":"1.0.60 (01a8e1c591c0)","channel":"unknown"}` and `grok
inspect` prints `Version: 1.0.60 [unknown]`. The one line the issue expected,
`grok 1.0.60 (01a8e1c591c0) [stable]`, appears only where an install records
its channel; the suffix is therefore optional. One regex that holds for both:
`^grok (\d+\.\d+\.\d+) \(([0-9a-f]{12})\)(?: \[(\w+)\])?$`. The first check of
the installed version at the start of the probe ran `grok --version` once with
the operator's ordinary environment (the line with `[stable]`) and no other
command did.

**V2 flags.** All `--help` outputs exit 0. Each token run alone beside
`--prompt-file` and `--output-format streaming-json`:

| Flag | Result |
|---|---|
| `--prompt-file`, `--output-format streaming-json`, `--deny "*"`, `--max-turns 1`, `--no-subagents`, `--disable-web-search` | accepted: each reaches the login refusal (exit 1) |
| `--no-leader` at the top level | accepted (exit 1 at the refusal) although `grok --help` does not list it; it is listed under `grok agent --help` |
| `agent stdio --no-leader` | refused: `unexpected argument '--no-leader'`; the flag belongs before the subcommand (`agent --no-leader stdio` exits 0) |
| `--invented-flag` (control) | exit 2, `error: unexpected argument '--invented-flag' found` |

A usage error exits 2 and the login refusal exits 1, so the two are told apart
by exit code. The self-updater has no flag: the bundled configuration reference
names the key `[cli] auto_update = false` and the environment variable
`GROK_DISABLE_AUTOUPDATER`. Without a login neither made a difference in E1
(the updater never ran). `grok leader kill` stops every leader of the user, so
this probe never ran it.

**N1 start-up with no login.** BASE and `--output-format json` end the same way. Stdout gets exactly one line, whose key
names are `type` and `message`:

```text
{"type":"error","message":"Not signed in. To authenticate without a browser, run:\n  grok login --device-code\n\nAlternatively, set the XAI_API_KEY environment variable or run `grok login` on a machine with a browser."}
```

stderr carries the same text after `Error: `; exit 1; wall 0.31 to 0.33 s; no
process was left. `-p hello --output-format plain` prints nothing on stdout
and the text twice on stderr (once bare, once with `Error: `), exit 1.
`grok agent stdio` with stdin closed exits 0 after 2.3 s and prints nothing.
`grok agent headless` (the WebSocket relay form) printed only `Grok Build (pager)
- v1.0.60 (01a8e1c591c0)` on stderr and ran into the 30 s deadline; it was
killed with its group and left no survivor. An adapter that detects a missing
login can match the `type: "error"` event, the exit code 1 and the phrase `Not
signed in`.

**T1 tool switches.** Accepted by name (each reached the refusal): `--deny "*"`,
`--tools ""`, `--disallowed-tools bash,edit`, `--permission-mode dontAsk`
(values: default, acceptEdits, auto, dontAsk, bypassPermissions, plan),
`--no-subagents`, `--disable-web-search`. `--sandbox <PROFILE>` (env
`GROK_SANDBOX`) is a profile name the CLI resolves before the login check: the
name `default` exits 1 in 0.05 s with `Custom sandbox profile 'default' not
found. Define it in ~/.grok/sandbox.toml or .grok/sandbox.toml`, and `--sandbox
workspace` exits 1 with `bwrap: Failed to mount tmpfs`, because the CLI starts
its own `bwrap` and that cannot run inside this harness. Whether tools are
really off is a model-turn fact: deferred.

**S1 system text.** Both flags are in `grok --help`, and each is accepted
(refusal exit 1): `--system-prompt-override <PROMPT>` with help text "Override
the agent's system prompt (compat alias: --system-prompt)" and `--rules
<RULES>` with "Extra rules to append to the system prompt". `grok inspect` in
the empty home lists no project instructions, no skills, no rules, no MCP
servers and three built-in agents (`general-purpose`, `explore`, `plan`). Whether
the override replaces the text is deferred.

**L1 turn limit.** `--max-turns 1` is accepted. `--max-turns 0` exits 2
(`invalid value '0' for '--max-turns <N>': 0 is not in 1..=4294967295`),
`x` exits 2 (`invalid digit found in string`), `-1` exits 2 (taken as an
unexpected argument). The value is therefore at least 1 and the library cannot
express "no turns". Whether it ends a run is deferred.

**A1 announcement.** The BASE stream stops at the refusal before any
announcement, so the start-up event is not reachable that way. The ACP form is:
`grok agent --no-leader stdio` speaks JSON-RPC lines. A client `initialize`
(`protocolVersion` 1) is answered with no login, with `result` keys
`_meta`, `agentCapabilities` (`_meta`, `auth`, `loadSession`, `mcpCapabilities`,
`promptCapabilities`, `sessionCapabilities`), `authMethods` (one entry, id
`grok.com`) and `protocolVersion`. `result._meta` carries `availableCommands`
(entries of `name`, `description`, `input`), `modelState`, `mcpServers`,
`defaultAuthMethodId`, `agentVersion` and `currentWorkingDirectory` among 18 keys. It is followed by a
notification `_x.ai/mcp/servers_updated`. `session/new` answers with a series of
`_x.ai/session/setup` notifications (`method`, `phase`, `sessionId`) and then an error,
code -32000, `Authentication required`. Tools and the plan are therefore not
announced without a login; commands are. Total 15 s wall for the exchange; no
survivor.

**M1 MCP client.** The configuration lives in `$GROK_HOME/config.toml`
(table `[mcp_servers.<name>]`: `url`, `headers`, `bearer_token_env_var`,
`bearer_token_file`, `enabled`, `command`, `args`, `env`, `cwd`) or, with
`grok mcp add --scope project`, in `./.grok/config.toml` of the working
directory (it is listed as `(project)` and does not create a file in the
home). `GROK_CONFIG=<file>` did not add the servers of that file to `mcp list`,
so a per-run configuration is the project file in the run's own working
directory. `mcp add` takes the bearer only as `-H "NAME: VALUE"` on argv, so
the probe wrote a placeholder-free config by hand: a header
`Authorization = "Bearer ${PROBE_BEARER}"` and a second server with
`bearer_token_env_var = "PROBE_BEARER"`; `mcp list` printed both, `mcp add` of a
third server printed `File modified: ~/.grok/config.toml`, and `disable` /
`enable` flipped `(disabled)`. `mcp doctor` connects without a login: per
server `server started`, `handshake OK (protocol 2025-11-25)`, `1 tools
discovered`, `Found 3 healthy, 0 failing`. The local server saw, per
connection, in order: `POST` `server/discover` (header `Mcp-Method`,
`Mcp-Protocol-Version: 2026-07-28`), `POST` `initialize` (`protocolVersion`
`2025-11-25`, `clientInfo` keys `name`, `version`), `POST`
`notifications/initialized` (header `Mcp-Session-Id` from the reply and
`Mcp-Protocol-Version: 2025-11-25`), `GET` (the server stream; the fake
server answers 405), `POST` `tools/list`, and at the end `DELETE` of the
session. Every request had `Accept: text/event-stream, application/json`
and `POST` bodies `Content-Type: application/json`. The request headers present
were `accept`, `authorization`, `content-length`, `content-type`, `host`,
`mcp-method`, `mcp-protocol-version`, `mcp-session-id`, `user-agent`. Both the
`headers` form and the `bearer_token_env_var` form delivered the exact bearer
(compared in memory: true for every request of those two servers). The server
added with only `-H "X-Probe: 1"` sent no `Authorization`; after its unauthenticated
`GET` the client also asked six `/.well-known/` OAuth discovery paths. A client
that gets a 405 on the `GET` is not stopped by it. The handshake before a
model turn (a turn's request order) is deferred.

**M2 MCP switches.** `grok mcp enable|disable <NAME>` have no options beyond
`--debug`. Rules are the global `--allow <RULE>` and `--deny <RULE>` (aliases
`--allowedTools`, `--disallowedTools`) and `--permission-mode`; the bundled
permissions guide gives the MCP syntax: `MCPTool(server__tool)` with glob
(`MCPTool(linear__*)`), the Claude spelling `mcp__server` /
`mcp__server__tool` is rewritten onto it, tool names carry no `mcp__`
prefix, and a bare `*` rule matches every tool. No switch exists that names
only MCP tools besides these rules. That `--deny "*"` blocks an MCP tool call is a
model-turn fact: deferred.

**F1 files.** `--version` writes nothing but the empty `tmp`. The first run
that starts the agent (BASE, `du`, `inspect`) creates under `$GROK_HOME`:
`README.md` (113 KB) and `docs/user-guide/` (27 Markdown files, 808 KB in
total, rewritten from the binary), `logs/unified.jsonl` (about 11 KB for a refused run, one
JSON object per line with the keys `ts`, `lvl`, `msg`, `pid`, `src`, `ctx`,
`ver`), `config.toml` (52 B, one table `[marketplace]`), `agent_id` (36 B),
`.metadata_version` (6 B), `sessions/session_search.sqlite` (36 KB),
`installed-plugins/registry.lock`, `.config-init.lock`, `managed_config.lock`
and empty `sessions/` and `installed-plugins/` directories. `du` reports
`~/.grok` as 812 KB. Nothing is written outside the home and `TMPDIR`. With
`HOME` and `GROK_HOME` both unset and no passwd entry in the sandbox, `inspect`
exits 0 and creates no `.grok`; with the passwd entry present it creates
`<passwd-home>/.grok`: the CLI falls back to the passwd profile like 1.0.4
(`process.py:1318`), so a run must always set `GROK_HOME`.

**B1 survivors.** A refused run (BASE, with and without `--no-leader`, with
`--leader-socket`) starts no leader: no socket, no lock, no process after the
exit. `grok agent leader --leader-socket <scratch>/leader-g0a.sock` created
`leader-g0a.sock` and `leader-g0a.lock` (7 B) in the throwaway home and ran
until the 30 s deadline killed it; the scan found no survivor. The leader
socket therefore follows `--leader-socket` and defaults to
`$GROK_HOME/leader.sock`, under the throwaway home. Without a login no leader
is started, so the question whether a turn leaves one is deferred; the group
reap needs a Grok rule only if that cell finds a survivor.

**E1 egress.** Under `strace -f -qq -s 200 -e trace=connect,sendto` with the
network shared, BASE with the updater default, BASE with
`GROK_DISABLE_AUTOUPDATER=1`, `agent stdio` and `--version` made no DNS query
and no network `connect`; the only `connect` was to
`/var/run/dbus/system_bus_socket` (ENOENT). `grok update --check` (the binary
bound read-only) resolved `x.ai` (A and AAAA, twice) and connected to port 443,
and printed `A new version of Grok Build is available: 1.0.60 -> 1.0.62
[stable]`. No update-check or telemetry host appears on the no-login path.

**Fresh and repeated.** Every cell ran on a fresh throwaway home. `--version` and
BASE ran a second time on new homes with the same exit codes and lines (0 and
`grok 1.0.60 (01a8e1c591c0)`; 1 and the same error line). The ACP exchange
ran twice with the same shapes.

**What the consumers may now rely on.** G1 (login a profile holds): the
refusal is `type: "error"`, exit 1, `Not signed in`; `GROK_HOME` must be set;
`authMethods` names `grok.com`. G2 (run spec): the BASE flags exist on 1.0.60
and `--no-leader` is accepted at the top level; `--max-turns` is at least 1; the
sandbox flag needs a named profile. G3 (event mapping): only the error event
and the ACP `initialize` / `session/new` shapes are known; no turn events.
G5 (relay attachment): MCP config is a toml table in the home or the working
directory, with bearer by environment variable, and `doctor` reaches an HTTP
server without a login. Adapter-port review of 4c: the version regex above and
the optional channel suffix.

**Deferred to A1b-G0b / G0c (need a login and quota).** N2 stream of a
completed turn, T-live, S-live, L-live, M-live (tool-call name form, parallel
calls, whether `--deny "*"` blocks MCP tools), F-live, E-live, L3, R1, R2, the
nested `--sandbox` profile, a leader left by a real turn, and the headless
WebSocket form.

**Scripts, verbatim.**

`g0a.py`:

```python
"""G0a harness: runs the grok CLI under bwrap with an empty throwaway home."""
import json, os, pathlib, secrets, shutil, signal, subprocess, sys, time

SCRATCH = pathlib.Path("<scratch>")
EVIDENCE = SCRATCH / "ev"
GROK = pathlib.Path(shutil.which("grok")).resolve()
BEARER = secrets.token_urlsafe(24)
DEADLINE_S = 30
BASE = ["--prompt-file", "/probe/work/prompt.txt", "--output-format", "streaming-json",
        "--deny", "*", "--max-turns", "1", "--no-subagents", "--disable-web-search"]


def write_record(name, line):
    for start in range(len(BEARER) - 11):
        line = line.replace(BEARER[start:start + 12], "<bearer>")
    line = line.replace(str(SCRATCH), "<scratch>").replace(str(pathlib.Path.home()), "<home>")
    with open(EVIDENCE / f"{name}.txt", "a") as out:
        out.write(line.rstrip("\n") + "\n")


def fresh_dirs(tag):
    home, work = SCRATCH / f"home-{tag}", SCRATCH / f"work-{tag}"
    for d in (home, work):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir()
    (home / "tmp").mkdir()
    (work / "prompt.txt").write_text("hello")
    return home, work


def survivors(home):
    found = []
    needle = b"GROK_HOME=/probe/home/.grok"
    for p in pathlib.Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            env = (p / "environ").read_bytes()
            cwd = os.readlink(p / "cwd")
            comm = (p / "comm").read_text().strip()
        except OSError:
            continue
        if needle in env and str(home) not in cwd and int(p.name) != os.getpid():
            found.append((int(p.name), comm))
    return found


def child_env(extra_env):
    return {"PATH": "/usr/bin:/bin", "HOME": "/probe/home", "GROK_HOME": "/probe/home/.grok",
            "TMPDIR": "/probe/home/tmp", **extra_env}


def sandbox_argv(home, work, argv, share_net, binary_args=True):
    cmd = ["bwrap", "--die-with-parent", "--new-session",
           "--unshare-user", "--unshare-ipc", "--unshare-uts"]
    if not share_net:
        cmd.append("--unshare-net")
    cmd += ["--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--ro-bind", "/etc/ssl", "/etc/ssl", "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf",
            "--ro-bind", "/etc/hosts", "/etc/hosts", "--ro-bind", "/etc/nsswitch.conf", "/etc/nsswitch.conf",
            "--ro-bind", "/etc/ca-certificates", "/etc/ca-certificates",
            "--proc", "/proc", "--dev", "/dev",
            "--bind", str(home), "/probe/home", "--bind", str(work), "/probe/work",
            "--ro-bind", str(GROK), "/opt/grok/grok", "--chdir", "/probe/work"]
    return cmd + ["/opt/grok/grok"] + argv if binary_args else cmd + argv


def run(tag, argv, share_net=False, extra_env=None, home=None, work=None, strace=False, raw=False, unset=(), passwd=False):
    home, work = (home, work) if home else fresh_dirs(tag)
    cmd = sandbox_argv(home, work, argv, share_net, binary_args=not raw)
    if passwd:
        cmd[1:1] = ["--ro-bind", "/etc/passwd", "/etc/passwd"]
    trace = SCRATCH / f"strace-{tag}.log"
    if strace:
        cmd = ["strace", "-f", "-qq", "-s", "200", "-e", "trace=connect,sendto", "-o", str(trace)] + cmd
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env={k: v for k, v in child_env(extra_env or {}).items() if k not in unset},
                            start_new_session=True, close_fds=True, text=True, errors="replace")
    timed_out = False
    try:
        out, err = proc.communicate(timeout=DEADLINE_S)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
    wall = time.monotonic() - started
    alive = survivors(home)
    for pid, _ in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return dict(exit=proc.returncode, out=out, err=err, wall=round(wall, 2), timed_out=timed_out,
                survivors=alive, home=home, work=work, trace=trace if strace else None)


def listing(root):
    rows = []
    for p in sorted(pathlib.Path(root).rglob("*")):
        rel = p.relative_to(root)
        kind = "dir" if p.is_dir() else ("sock" if p.is_socket() else "file")
        size = p.stat().st_size if kind == "file" else 0
        rows.append(f"{kind:4} {size:8} {rel}")
    return rows


def report(cell, label, result):
    write_record(cell, f"## {label}: exit={result['exit']} wall={result['wall']}s timeout={result['timed_out']} survivors={result['survivors']}")
    for stream in ("out", "err"):
        for line in result[stream].splitlines()[:40]:
            write_record(cell, f"  {stream}| {line[:1600]}")
```

`cells1.py`:

```python
import sys; sys.path.insert(0,".")
import g0a, re, json
from g0a import run, report, write_record as w, BASE
P="/probe/work/prompt.txt"
MIN=["--prompt-file",P,"--output-format","streaming-json"]

def v2():
    c="V2"
    for h in (["--help"],["agent","--help"],["agent","headless","--help"],["agent","stdio","--help"],["mcp","--help"],["mcp","add","--help"],["mcp","enable","--help"],["mcp","disable","--help"],["mcp","doctor","--help"],["inspect","--help"],["du","--help"],["update","--help"]):
        r=run("v2",h); w(c,f"## grok {' '.join(h)} exit={r['exit']} lines={len(r['out'].splitlines())}")
        (g0a.EVIDENCE/("help-"+"-".join(h).replace("--","")+".txt")).write_text(r["out"].replace(str(g0a.SCRATCH),"<scratch>"))
    tokens=[["--prompt-file",P],["--output-format","streaming-json"],["--deny","*"],["--max-turns","1"],["--no-subagents"],["--disable-web-search"],["--no-leader"],["--invented-flag"]]
    for t in tokens:
        argv = MIN+t if t[0] not in ("--prompt-file","--output-format") else (MIN if t[0]=="--prompt-file" else ["--prompt-file",P]+t)
        r=run("v2",argv); report(c,"top-level "+" ".join(t),r)
    for t in (["--no-leader"],["--invented-flag"]):
        r=run("v2",["agent","stdio"]+t); report(c,"agent stdio "+" ".join(t),r)

def t1():
    c="T1"
    for t in (["--deny","*"],["--tools",""],["--disallowed-tools","bash,edit"],["--permission-mode","dontAsk"],["--no-subagents"],["--disable-web-search"],["--sandbox","default"],["--invented-flag"]):
        r=run("t1",MIN+t); report(c,"M + "+" ".join(t),r)

def s1():
    c="S1"
    for t in (["--system-prompt-override","probe"],["--rules","probe"]):
        r=run("s1",BASE+t); report(c,"BASE + "+t[0],r)
    r=run("s1",["inspect"]); report(c,"inspect",r)

def l1():
    c="L1"
    for v in ("1","0","-1","x"):
        r=run("l1",MIN+["--max-turns",v]); report(c,"--max-turns "+v,r)

def n1():
    c="N1"
    r=run("n1",BASE); report(c,"BASE (top-level)",r)
    r=run("n1",["-p","hello","--output-format","plain"]); report(c,"-p hello plain",r)
    r=run("n1",["--prompt-file",P,"--output-format","json"]); report(c,"json",r)
    r=run("n1",["agent","stdio"]); report(c,"agent stdio, stdin closed",r)

def f1():
    c="F1"
    for tag,argv in (("version",["--version"]),("n1",BASE),("du",["du"]),("inspect",["inspect"])):
        r=run("f1",argv); report(c,"after "+tag,r)
        for row in g0a.listing(r["home"]): w(c,"   "+row)

def b1():
    c="B1"
    sock="/probe/home/.grok/leader-g0a.sock"
    for label,argv in (("BASE",BASE),("BASE --leader-socket",BASE+["--leader-socket",sock]),("agent stdio --no-leader",["agent","stdio","--no-leader"]),("agent leader --leader-socket",["agent","leader","--leader-socket",sock])):
        r=run("b1",argv); report(c,label,r)
        for row in g0a.listing(r["home"]): w(c,"   "+row)

if __name__=="__main__":
    import sys
    for name in sys.argv[1:]: globals()[name]()
```

`cells2.py`:

```python
import sys; sys.path.insert(0, ".")
import g0a
from g0a import run, report, write_record as w, BASE
def extra():
    for label, argv in (("agent --no-leader stdio, stdin closed", ["agent", "--no-leader", "stdio"]),
                        ("agent --no-leader --leader-socket stdio", ["agent", "--no-leader", "--leader-socket", "/probe/home/.grok/leader-g0a.sock", "stdio"]),
                        ("BASE --no-leader", BASE + ["--no-leader"]),
                        ("sandbox workspace", BASE + ["--sandbox", "workspace"]),
                        ("agent headless, stdin closed", ["agent", "headless"])):
        r = run("x", argv); report("B1x", label, r)
def repeat():
    for label, argv in (("repeat --version", ["--version"]), ("repeat BASE", BASE)):
        r = run("rep", argv); report("REP", label, r)
if __name__ == "__main__":
    for n in sys.argv[1:]: globals()[n]()
```

`fake_mcp.py`:

```python
"""Throwaway MCP-over-HTTP server run inside the sandbox; the bearer comes from the environment only."""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BEARER = os.environ["PROBE_BEARER"]
SEEN = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _record(self, body):
        auth = self.headers.get("Authorization", "")
        rpc = json.loads(body) if body else {}
        SEEN.append({"http": self.command, "path": self.path,
                     "headers": sorted(k.lower() for k in self.headers.keys()),
                     "bearer_matches": auth == "Bearer " + BEARER,
                     "accept": self.headers.get("Accept"), "content_type": self.headers.get("Content-Type"),
                     "rpc_method": rpc.get("method"), "has_id": "id" in rpc,
                     "protocol_asked": (rpc.get("params") or {}).get("protocolVersion"),
                     "client_info_keys": sorted(((rpc.get("params") or {}).get("clientInfo") or {}).keys()),
                     "mcp_protocol_header": self.headers.get("Mcp-Protocol-Version"),
                     "session_header": bool(self.headers.get("Mcp-Session-Id"))})
        return rpc

    def _send(self, code, payload=None):
        data = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(code)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "probe-session")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        rpc = self._record(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        method = rpc.get("method")
        if "id" not in rpc:
            return self._send(202)
        results = {"initialize": {"protocolVersion": (rpc.get("params") or {}).get("protocolVersion", "2025-03-26"),
                                  "capabilities": {"tools": {}}, "serverInfo": {"name": "probe", "version": "0"}},
                   "tools/list": {"tools": [{"name": "echo", "description": "probe",
                                             "inputSchema": {"type": "object", "properties": {}}}]},
                   "ping": {}}
        self._send(200, {"jsonrpc": "2.0", "id": rpc["id"], "result": results.get(method, {})})

    def do_GET(self):
        self._record(b"")
        self._send(405)

    def do_DELETE(self):
        self._record(b"")
        self._send(200)


def start():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]
```

`m1_inner.py`:

```python
"""Runs inside bwrap with its own loopback: starts the fake server, then drives grok mcp commands."""
import json, os, subprocess, sys, pathlib
sys.path.insert(0, "/probe/work")
import fake_mcp
port = fake_mcp.start()
url = f"http://127.0.0.1:{port}/mcp"
home = pathlib.Path("/probe/home/.grok"); home.mkdir(exist_ok=True)
(home / "config.toml").write_text(
    f'[mcp_servers.viaheaders]\nurl = "{url}"\nheaders = {{ "Authorization" = "Bearer ${{PROBE_BEARER}}" }}\n\n'
    f'[mcp_servers.viaenv]\nurl = "{url}"\nbearer_token_env_var = "PROBE_BEARER"\n')
def grok(*argv, timeout=30):
    p = subprocess.run(["/opt/grok/grok", *argv], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return {"argv": list(argv), "exit": p.returncode, "out": p.stdout[-1500:], "err": p.stderr[-800:]}
steps = [grok("mcp", "list"), grok("mcp", "doctor"),
         grok("mcp", "add", "added", url, "--transport", "http", "-H", "X-Probe: 1"), grok("mcp", "list"),
         grok("mcp", "disable", "added"), grok("mcp", "list"), grok("mcp", "enable", "added"), grok("mcp", "doctor")]
print(json.dumps({"steps": steps, "seen": fake_mcp.SEEN, "config": (home / "config.toml").read_text().replace("\n", "\\n")[:1500]}))
```

`m1.py`:

```python
import sys; sys.path.insert(0, ".")
import json, shutil, g0a
from g0a import run, write_record as w
home, work = g0a.fresh_dirs("m1")
for f in ("fake_mcp.py", "m1_inner.py"):
    shutil.copy(g0a.SCRATCH / f, work / f)
r = run("m1", ["/usr/bin/python3", "/probe/work/m1_inner.py"], extra_env={"PROBE_BEARER": g0a.BEARER}, home=home, work=work, raw=True)
w("M1", f"## inner exit={r['exit']} wall={r['wall']} survivors={r['survivors']}")
w("M1", "stderr| " + r["err"][-600:])
data = json.loads(r["out"]) if r["out"].strip().startswith("{") else {"raw": r["out"][-800:]}
for step in data.get("steps", []):
    w("M1", f"## grok {' '.join(step['argv'])} exit={step['exit']}")
    for line in step["out"].splitlines(): w("M1", "  out| " + line[:200])
    for line in step["err"].splitlines(): w("M1", "  err| " + line[:200])
for s in data.get("seen", []): w("M1", "seen| " + json.dumps(s, sort_keys=True))
w("M1", "config| " + data.get("config", ""))
w("M1", "tree|"); [w("M1", "   " + row) for row in g0a.listing(home)]
```

`m1b.py`:

```python
import sys; sys.path.insert(0, ".")
import g0a
from g0a import run, report
script = ("cd /probe/work; /opt/grok/grok mcp add --scope project --transport http pj http://127.0.0.1:9/mcp; echo add-exit=$?;"
          " find .grok -type f | sed 's#^#proj-file #'; /opt/grok/grok mcp list; echo list-exit=$?;"
          " printf '[mcp_servers.alt]\\nurl = \"http://127.0.0.1:9/mcp\"\\n' > /probe/work/alt.toml;"
          " GROK_CONFIG=/probe/work/alt.toml /opt/grok/grok mcp list; echo grokconfig-exit=$?;"
          " ls /probe/home/.grok | grep -c config.toml")
r = run("m1b", ["/usr/bin/sh", "-c", script], raw=True)
report("M1b", "project scope and GROK_CONFIG", r)
```

`e1.py`:

```python
import sys; sys.path.insert(0, ".")
import re, g0a
from g0a import run, report, write_record as w, BASE
def hosts(trace):
    seen = []
    for line in open(trace, errors="replace"):
        m = re.search(r'connect\(\d+, \{sa_family=AF_INET6?, sin6?_port=htons\((\d+)\)', line)
        if m and m.group(1) != "53": seen.append("connect port " + m.group(1))
        if 'htons(53)' in line and ("connect" in line or "sendto" in line):
            q = re.search(r'sendto\(\d+, "(.*?)", \d+', line)
            if q: seen.append("dns " + re.sub(r"\\\d+|\\x[0-9a-f]{2}", ".", q.group(1))[:80])
    return seen
cases = [("updater default", BASE, {}), ("updater off GROK_DISABLE_AUTOUPDATER=1", BASE, {"GROK_DISABLE_AUTOUPDATER": "1"}),
         ("agent stdio", ["agent", "stdio"], {}), ("--version", ["--version"], {}), ("update --check (installer path, binary bound read-only)", ["update", "--check"], {})]
for n, (label, argv, env) in enumerate(cases):
    r = run(f"e1{n}", argv, share_net=True, extra_env=env, strace=True)
    report("E1", label, r)
    for h in hosts(r["trace"]): w("E1", "   " + h)

import glob
for t in sorted(glob.glob(str(g0a.SCRATCH / "strace-e1*.log"))):
    w("E1", "raw " + t.rsplit("/", 1)[1] + " lines=" + str(sum(1 for _ in open(t))))
    for line in list(open(t, errors="replace"))[:25]:
        w("E1", "   raw| " + re.sub(r"inet6?_addr\(\"[0-9a-f:.]+\"\)", "ADDR", line.strip())[:220])
```

`f2.py`:

```python
import sys; sys.path.insert(0, ".")
import g0a
from g0a import run, report
script = "/opt/grok/grok inspect >/dev/null 2>&1; echo inspect-exit=$?; echo HOME=${HOME-unset}; find / -xdev -maxdepth 5 -name '.grok' -not -path '/proc/*' 2>/dev/null | sed 's#^#found #'; ls -d /root /home/* 2>&1 | sed 's#^#ls #'"
for label, passwd in (("HOME and GROK_HOME unset, no passwd entry", False), ("HOME and GROK_HOME unset, passwd entry bound", True)):
    r = run("f2", ["/usr/bin/sh", "-c", script], unset=("HOME", "GROK_HOME"), raw=True, passwd=passwd)
    report("F1b", label, r)
```

`acp_inner.py`:

```python
"""Inside the sandbox: speaks ACP to `grok agent --no-leader stdio` with no login; prints the key shape of each reply."""
import json, subprocess, sys, threading, time, queue
p = subprocess.Popen(["/opt/grok/grok", "agent", "--no-leader", "stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True)
lines = queue.Queue()
for stream, tag in ((p.stdout, "out"), (p.stderr, "err")):
    threading.Thread(target=lambda s=stream, t=tag: [lines.put((t, l.rstrip())) for l in s], daemon=True).start()

def shape(value, depth=0):
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) if depth < 2 else type(v).__name__ for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    return type(value).__name__

def send(msg):
    p.stdin.write(json.dumps(msg) + "\n"); p.stdin.flush()

def drain(label, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            tag, line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            doc = json.loads(line)
            body = {"method": doc.get("method"), "id": doc.get("id"), "keys": sorted(doc),
                    "result": {k: (sorted(v) if isinstance(v, dict) else shape(v)) for k, v in (doc.get("result") or {}).items()},"meta_keys": sorted((doc.get("result") or {}).get("_meta") or {}),"auth_methods": [m.get("id") for m in (doc.get("result") or {}).get("authMethods", [])],"cap_keys": sorted((doc.get("result") or {}).get("agentCapabilities") or {}), "error": {"code": (doc.get("error") or {}).get("code"),
                                                                 "message": (doc.get("error") or {}).get("message", "")[:160]} if doc.get("error") else None,
                    "params": shape(doc.get("params"))}
            print(label, tag, json.dumps(body, sort_keys=True), flush=True)
        except ValueError:
            print(label, tag, "non-json:", line[:160], flush=True)

send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientCapabilities": {}}})
drain("initialize", 5)
send({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/probe/work", "mcpServers": []}})
drain("session/new", 8)
p.stdin.close()
try:
    p.wait(timeout=10)
except subprocess.TimeoutExpired:
    p.kill()
print("exit", p.returncode)
```

`a1.py`:

```python
import sys, shutil; sys.path.insert(0, ".")
import g0a
home, work = g0a.fresh_dirs("a1")
shutil.copy(g0a.SCRATCH / "acp_inner.py", work / "acp_inner.py")
r = g0a.run("a1", ["/usr/bin/python3", "/probe/work/acp_inner.py"], home=home, work=work, raw=True)
g0a.report("A1", "ACP initialize and session/new over agent stdio", r)
```


### A1b measured: codex 0.163.0

Measured on 18.10.2026 on the installed `codex`, whose native executable prints
`codex-cli 0.163.0` (V1). No cell used a login, a token, an auth file or a model
turn; the probe never ran `login` or `logout`, never opened any auth file, and
every run had an empty throwaway home and an empty `CODEX_HOME`. Nothing here
reads `~/.codex`. Operator ruling 18.10.2026: C0a uses no login.

#### A1b-C0a the Codex CLI without a login (flags, stream, MCP client, files, egress)

**V1 the binary.** `codex` on the path is a symlink to a Node script
(`@openai/codex/bin/codex.js`) that starts a static musl executable from the
optional package `@openai/codex-linux-x64`, with a sibling `codex-code-mode-host`.
The pin, read-only commands run on 18.10.2026 after the series (paths shortened;
`<shim>` is the resolved `codex.js`, `<native>` the musl executable):

```text
$ readlink -f "$(command -v codex)"
<node-lib>/@openai/codex/bin/codex.js
$ stat -c %s <shim> <native>
8790
296865480
$ sha256sum <shim> <native>      (first 12 hex digits)
61b0194f3bb6  <shim>
399ff69c431c  <native>
```

The series itself recorded only the native executable, by `os.path.getsize` and
`sha256sum` in cell V1 of both passes (296,865,480 bytes, sha256 prefix
`399ff69c431c`, equal before and after); the shim size and hash have no
in-series record, only the command above, run once after the series. The
harness resolved the shim with `command -v codex` and executed neither it nor
`codex.js`: every cell ran the native executable directly, bound read-only as
`/opt/codex`, so the sibling host was not bound. `codex --version` as run by
the harness (native executable, in the sandbox) prints one line, exit 0:

```text
codex-cli 0.163.0
```

It is not recorded whether the first version line came from a run of the shim in
the ordinary environment; such a run would create `tmp/arg0-linux/` under the
real `CODEX_HOME` (F1) but reads no auth file. Outside the sandbox the harness ran
only `command -v codex`, `sha256sum` and `os.path.getsize` on the native
executable, `find`, `rm -rf` and `pgrep` in scratch, and `python3` with the
scripts below; every `codex` process ran inside `bwrap`, with `HOME` and
`CODEX_HOME` in scratch. The earlier logs `all1.log`, `all2.log` and `v2.log`
are development runs of the same scripts (one `B1` run died on a harness
`AttributeError`, fixed before the series); the evidence below is the two passes
of the locked series.

The regex `^codex-cli (\d+\.\d+\.\d+)$` parses it. `codex version` is not a
subcommand: the word is taken as the prompt of the interactive mode, which prints
`Error: stdin is not a terminal` and exits 1.

**Harness (every cell).** Each run is the native executable under `bwrap` with
`--die-with-parent` (and no `--unshare-pid`), the system directories bound
read-only, a throwaway home and an empty working directory in scratch (fresh per
cell: `run.sh` deletes the cell's directory, calls within a cell share it), the
network unshared (cells E1 share it, E1-dns also binds the resolver stub) and
exactly this environment: `PATH`, `HOME`, `CODEX_HOME`, `TMPDIR` and
`FAKE_MCP_SECRET` in every cell (a random value made per harness process, never
on argv; only M1 passes it on to a server and a `-c` switch). The driver runs inside the
sandbox, writes the prompt `hello` to stdin, closes stdin, waits at most `WAIT`
seconds (30 unless a cell says 8 or 15) and kills the process group; the outer
harness kills its own group 15 s later. After each cell `run.sh` counts
`pgrep -fc` matches of the `drive.py` path (0 each time); that check does not see a
leftover `codex` process, and the sandbox has no PID namespace of its own. One
locked series (`/tmp/probe-stack.lock`, via `both.sh` below) ran all cells, once
fresh and once repeated, with the load average checked per cell by `run.sh`
(limit 18). `BASE` is the argv of the issue:
`exec --json --sandbox read-only --skip-git-repo-check --ignore-user-config
--ignore-rules --ephemeral` plus the `-c` switches listed in cell T1, with the
prompt `-` (stdin). Every line of the `ev/` evidence passed `write_record`, which drops
any line holding the random bearer string or one of its 12-character windows; the
`strace` files in `raw/` (B1, E1) are written by `strace` itself and were not
passed through it.
The scripts, verbatim (scratch only, the repository gains none):

`drive.py`, run inside the sandbox (the fake MCP server is part of it, on an
ephemeral `127.0.0.1` port; it logs header names, whether `Authorization` equals
the expected bearer, and the JSON body):

```python
import os, sys, json, time, signal, secrets, subprocess, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
wait = float(sys.argv[1]); stdin_text = sys.argv[2]; serve_log = sys.argv[3]; cmd = sys.argv[sys.argv.index('--') + 1:]
SECRET = os.environ.get('FAKE_MCP_SECRET', ''); T0 = time.monotonic(); lock = threading.Lock()
def log(rec):
    with lock, open(serve_log, 'a') as f: f.write(json.dumps(rec) + '\n')
class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *a): pass
    def _record(self, body):
        names = [k for k, _ in self.headers.items()]
        auth = self.headers.get('Authorization')
        log({'t': round(time.monotonic() - T0, 3), 'method': self.command, 'path': self.path, 'header_names': names,
             'authorization_matches': None if auth is None else auth == 'Bearer ' + SECRET,
             'accept': self.headers.get('Accept'), 'content_type': self.headers.get('Content-Type'),
             'protocol_header': self.headers.get('MCP-Protocol-Version'), 'body': body})
    def _send(self, code, obj=None):
        data = b'' if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        if obj is not None: self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)) or 0).decode()
        try: body = json.loads(raw)
        except Exception: body = raw
        self._record(body)
        if not isinstance(body, dict) or 'id' not in body: return self._send(202)
        m = body.get('method')
        if m == 'initialize': res = {'protocolVersion': body['params']['protocolVersion'], 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fake-mcp', 'version': '0'}}
        elif m == 'tools/list': res = {'tools': [{'name': 'echo', 'description': 'echo', 'inputSchema': {'type': 'object', 'properties': {}}}]}
        else: return self._send(200, {'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32601, 'message': 'method not found'}})
        self._send(200, {'jsonrpc': '2.0', 'id': body['id'], 'result': res})
    def do_GET(self): self._record(None); self._send(405)
port = '0'
if serve_log != '-':
    server = ThreadingHTTPServer(('127.0.0.1', 0), H); port = str(server.server_address[1])
    threading.Thread(target=server.serve_forever, daemon=True).start()
cmd = [a.replace('{PORT}', port) for a in cmd]
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
def pump(stream, tag):
    for line in stream: print(f'{time.monotonic() - T0:8.3f} {tag} {line.rstrip()[:4000]}', flush=True)
th = [threading.Thread(target=pump, args=(p.stdout, 'out')), threading.Thread(target=pump, args=(p.stderr, 'err'))]
[t.start() for t in th]
if stdin_text: p.stdin.write(stdin_text + '\n')
p.stdin.close()
try: p.wait(timeout=wait)
except subprocess.TimeoutExpired:
    print(f'{time.monotonic() - T0:8.3f} --- deadline; killing process group', flush=True)
    os.killpg(p.pid, signal.SIGKILL); p.wait()
[t.join(timeout=2) for t in th]
print(f'{time.monotonic() - T0:8.3f} --- exit {p.returncode}')
```

`harness.py`, the outer side (`BASE`, the sandbox command line, `write_record`):

```python
import os, sys, re, json, time, secrets, signal, subprocess, pathlib
S = pathlib.Path(os.environ['C0A']); NATIVE = os.environ['CODEX_NATIVE']
BEARER = secrets.token_urlsafe(24); WINDOW = 12
WINDOWS = [BEARER[i:i + WINDOW] for i in range(len(BEARER) - WINDOW + 1)]
FEATURES = ['shell_tool', 'unified_exec', 'browser_use', 'computer_use', 'multi_agent', 'image_generation', 'plugins', 'hooks', 'code_mode_host', 'code_mode', 'code_mode_only']
BASE_HEAD = ['exec', '--json', '--sandbox', 'read-only', '--skip-git-repo-check', '--ignore-user-config', '--ignore-rules', '--ephemeral']
BASE_CONFIG = [('approval_policy="never"',), ('mcp_servers={}',)] + [(f'features.{f}=false',) for f in FEATURES[:6]] + [('web_search="disabled"',)] + [(f'features.{f}=false',) for f in FEATURES[6:]]
class Argv(list):
    label = None
def base(extra_config=(), strict=False, drop=()):
    a = Argv(BASE_HEAD + (['--strict-config'] if strict else []))
    for (c,) in BASE_CONFIG:
        if c.split('=')[0] not in drop: a += ['-c', c]
    for c in extra_config: a += ['-c', c]
    a.label = 'BASE' + (' --strict-config' if strict else '') + ''.join(f' -{d}' for d in drop) + ''.join(f' -c {e}' for e in extra_config)
    a.append('-')
    return a

def write_record(path, kind, text):
    kept = []
    for line in str(text).splitlines():
        if BEARER in line or any(w in line for w in WINDOWS): continue
        kept.append(f'{kind}: {line}')
    with open(path, 'a') as f: f.write('\n'.join(kept) + ('\n' if kept else ''))
def sandbox(cell, args, share_net=False, stdin_text='', serve=False, wrapper=(), wait=30, cap_drop=False, resolver=False):
    run = S / 'run' / cell; (run / 'home' / 'tmp').mkdir(parents=True, exist_ok=True); (run / 'work').mkdir(exist_ok=True)
    home = run / 'home'
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'CODEX_HOME': str(home / '.codex'), 'TMPDIR': str(home / 'tmp'), 'FAKE_MCP_SECRET': BEARER}
    (home / '.codex').mkdir(exist_ok=True)
    srvlog = str(run / 'server.jsonl') if serve else '-'
    bw = ['bwrap', '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
          '--ro-bind', '/etc', '/etc', '--tmpfs', '/home', '--ro-bind', NATIVE, '/opt/codex', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
          '--bind', str(S), str(S), *(['--ro-bind', '/run/systemd/resolve', '/run/systemd/resolve'] if resolver else []), '--share-net' if share_net else '--unshare-net', '--die-with-parent', *(['--cap-drop', 'ALL'] if cap_drop else []), '--chdir', str(run / 'work'),
          'python3', '-I', str(S / 'bin' / 'drive.py'), str(wait), stdin_text, srvlog, '--', *wrapper, '/opt/codex', *args]
    t0 = time.monotonic()
    p = subprocess.Popen(bw, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try: out, _ = p.communicate(timeout=wait + 15)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL); out, _ = p.communicate(); out += '\n--- outer deadline kill'
    ev = S / 'ev' / f'{cell}.txt'
    write_record(ev, 'cmd', 'codex ' + (getattr(args, 'label', None) or ' '.join(args)).replace(str(S), '<scratch>') + (' [cap-drop ALL]' if cap_drop else '') + (' [net shared]' if share_net else '') + (' [resolver bound]' if resolver else ''))
    write_record(ev, 'out', out.replace(str(S), '<scratch>'))
    if serve and os.path.exists(srvlog): write_record(ev, 'srv', open(srvlog).read())
    return out
def tree(cell):
    home = S / 'run' / cell / 'home'
    r = subprocess.run(['find', str(home), '-printf', '%y %s %P\n'], capture_output=True, text=True).stdout
    return sorted(l for l in r.splitlines() if l.split(' ', 2)[-1] not in ('', 'tmp'))

def tree_summary(cell):
    import collections
    entries = [l.split(' ', 2) for l in tree(cell)]
    shown, skills = [], collections.Counter()
    for kind, size, path in entries:
        path = re.sub(r'codex-arg0\w+', 'codex-arg0*', path)
        if path.startswith('.codex/skills/.system/'): skills[kind] += 1
        elif path.startswith('.codex/tmp/arg0-linux/'): shown.append(f'{kind} {size} {path}')
        else: shown.append(f'{kind} {size} {re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "<id>", path)}')
    shown.append(f'under .codex/skills/.system: {skills["d"]} dirs, {skills["f"]} files')
    return sorted(set(shown))
```

`cells.py`, one branch per cell, and `run.sh`, the per-cell wrapper (the series
held the lock around all cells of a pass):

```python
import os, sys, json, subprocess, shlex
sys.path.insert(0, os.path.dirname(__file__))
from harness import *
cell = sys.argv[1]
if cell == 'V1':
    for a in (['--version'], ['version']): sandbox('V1', a, wait=15)
    shim = os.environ['CODEX_SHIM']
    r = subprocess.run(['sha256sum', NATIVE], capture_output=True, text=True).stdout.split()[0][:12]
    write_record(S / 'ev' / 'V1.txt', 'native', f'size={os.path.getsize(NATIVE)} sha256_12={r}')
elif cell == 'V2':
    sandbox('V2', ['exec', '--help'], wait=15); sandbox('V2', ['--help'], wait=15)
    sandbox('V2', ['--invented-flag'], wait=15)
    sandbox('V2', base(['invented_key_c0a=1'], strict=True), stdin_text='hello', wait=30)
    sandbox('V2', base(['invented_key_c0a=1'], strict=False), stdin_text='hello', wait=8)
    for tok in BASE_HEAD[1:]:
        if tok.startswith('--') and tok != '--skip-git-repo-check':
            sandbox('V2', ['exec', '--skip-git-repo-check', tok] + (['read-only'] if tok == '--sandbox' else []) + ['-'], stdin_text='hello', wait=8)
    sandbox('V2', ['exec', '--skip-git-repo-check', '--invented-flag', '-'], stdin_text='hello', wait=8)
    sandbox('V2', ['exec', '--skip-git-repo-check', '-'], stdin_text='hello', wait=8)
elif cell == 'N1':
    sandbox('N1', base(), stdin_text='hello', wait=30)
elif cell == 'T1':
    sandbox('T1', ['features', 'list'], wait=15)
    for key_value in [x for (x,) in BASE_CONFIG] + ['features.invented_c0a=false']:
        sandbox('T1', ['exec', '--json', '--skip-git-repo-check', '--ignore-user-config', '--ephemeral', '--strict-config', '-c', key_value, '-'], stdin_text='hello', wait=8)
elif cell == 'S1':
    (S / 'instructions.md').write_text('Answer in one word.\n')
    for c in ('model_instructions_file="' + str(S / 'instructions.md') + '"', 'developer_instructions="x"', 'instructions="x"', 'invented_key_c0a="x"'):
        sandbox('S1', base([c], strict=True), stdin_text='hello', wait=8)
elif cell == 'L1':
    sandbox('L1', ['exec', '--help'], wait=15)
    for c in ('max_turns=1', 'agent.max_turns=1', 'model_max_turns=1', 'invented_key_c0a=1'):
        sandbox('L1', base([c], strict=True), stdin_text='hello', wait=8)
elif cell == 'M1':
    sandbox('M1', ['mcp', 'add', '--help'], wait=15); sandbox('M1', ['mcp', 'list', '--json'], wait=15)
    sandbox('M1', base(['mcp_servers.fake.url="http://127.0.0.1:{PORT}/mcp"', 'mcp_servers.fake.bearer_token_env_var="FAKE_MCP_SECRET"'], drop=['mcp_servers']), stdin_text='hello', serve=True, wait=30)
elif cell == 'M2':
    sandbox('M2', ['mcp', 'add', '--help'], wait=15)
    for c in ('mcp_servers.fake.enabled_tools=["echo"]', 'mcp_servers.fake.disabled_tools=["echo"]', 'mcp_servers.fake.invented_c0a=1'):
        sandbox('M2', base([c, 'mcp_servers.fake.url="http://127.0.0.1:1/mcp"'], strict=True, drop=['mcp_servers']), stdin_text='hello', wait=8)
elif cell == 'F1':
    for name, args in (('version', ['--version']), ('ephemeral', base()), ('persistent', [a for a in base() if a != '--ephemeral'])):
        sandbox(f'F1-{name}', args, stdin_text='hello', wait=30)
        write_record(S / 'ev' / 'F1.txt', f'tree[{name}]', '\n'.join(tree_summary(f'F1-{name}')))
elif cell == 'B1':
    trace = ['strace', '-f', '-qq', '-o', str(S / 'raw' / 'B1-execve.txt'), '-e', 'trace=execve,clone,clone3,unshare']
    sandbox('B1', base(), stdin_text='hello', wrapper=trace + ['setpriv', '--no-new-privs'], cap_drop=True, wait=30)
    trace2 = ['strace', '-f', '-qq', '-o', str(S / 'raw' / 'B1-control.txt'), '-e', 'trace=execve,clone,clone3,unshare']
    sandbox('B1-control', ['sandbox', 'linux', '--', 'true'], wrapper=trace2 + ['setpriv', '--no-new-privs'], cap_drop=True, wait=30)
elif cell == 'E1':
    trace = ['strace', '-f', '-qq', '-s', '200', '-o', str(S / 'raw' / 'E1-net.txt'), '-e', 'trace=connect,sendto']
    sandbox('E1', base(), share_net=True, stdin_text='hello', wrapper=trace, wait=30)
    trace2 = ['strace', '-f', '-qq', '-s', '200', '-o', str(S / 'raw' / 'E1-net-dns.txt'), '-e', 'trace=connect,sendto']
    sandbox('E1-dns', base(), share_net=True, resolver=True, stdin_text='hello', wrapper=trace2, wait=30)
```

```bash
#!/bin/bash
# usage: run.sh CELL ; load check, run, survivor check
S=<scratch>
load=$(cut -d' ' -f1 /proc/loadavg); awk -v l=$load 'BEGIN{exit !(l>18)}' && { echo "load $load too high"; exit 9; }
export C0A=$S CODEX_NATIVE=<native-codex> CODEX_SHIM=$(command -v codex)
rm -rf $S/run/$1* $S/ev/$1.txt $S/raw/$1-*
timeout 600 python3 -I $S/bin/cells.py $1; rc=$?
echo "cell $1 rc=$rc survivors=$(pgrep -fc "$S/bin/drive.py")"
```

`series.sh` (one pass over all cells) and `both.sh` (the lock wrapper that ran
two passes), with the scratch path replaced by `<scratch>`:

```bash
#!/bin/bash
S=<scratch>
pass=$1
rm -rf $S/ev $S/raw $S/run; mkdir -p $S/ev $S/raw
for c in V1 N1 V2 T1 S1 L1 M1 M2 F1 B1 E1; do $S/bin/run.sh $c; done
rm -rf $S/ev-$pass $S/raw-$pass; cp -r $S/ev $S/ev-$pass; cp -r $S/raw $S/raw-$pass
echo "pass $pass done"
```

```bash
#!/bin/bash
S=<scratch>
for pass in 1 2; do flock -o /tmp/probe-stack.lock $S/bin/series.sh $pass; done
```

The lock is therefore taken once per pass by `flock -o`, so no child inherits
it; `run.sh` itself takes no lock.

**V2 flags and config keys.** `codex exec --help` and `codex --help` exit 0. Each
head flag of `BASE` (`--json`, `--sandbox read-only`, `--ignore-user-config`,
`--ignore-rules`, `--ephemeral`, `--skip-git-repo-check`) was accepted: run alone
with `--skip-git-repo-check` it ran until the 8 s deadline instead of exiting 1 or 2
(with `--json` it printed `thread.started`). The control `--invented-flag` exits
2 with `error: unexpected argument '--invented-flag' found`, at the top level and
under `exec`. An unknown `-c` key under `--strict-config` exits 1 in 0.02 s with
`unknown configuration field` followed by the key. Without `--strict-config` the
same key is accepted and the stream carries two `item.completed` events whose
`item.type` is `error` and whose message says the setting is ignored. Flags
`exec --help` also lists and that C2 may need: `--strict-config`, `--enable` and
`--disable FEATURE` (short for `-c features.NAME=true|false`), `-s/--sandbox`,
`--approve-for-me`, `--dangerously-bypass-approvals-and-sandbox`,
`--dangerously-bypass-hook-trust`, `-C/--cd`, `--add-dir`, `-p/--profile`,
`--oss`, `--worktree`, `--thread-source`. No flag or key reached a "login
required" error, because without a login the CLI does not stop at start-up (N1).

**N1 start-up without a login.** The CLI does not refuse at start-up. With the
network unshared (DNS cannot answer) the stream is

| Order | Line | Keys |
|---|---|---|
| 1 | `{"type":"thread.started",...}` | `thread_id` |
| 2 | `{"type":"item.completed",...}` | `item.id`, `item.type` = `error`, `item.message` (the notice that code mode is unavailable because `code_mode_host` is off, from `BASE`) |
| 3 | `{"type":"turn.started"}` | none |
| 4 and on | `{"type":"error","message":"Reconnecting... n/5 (stream disconnected before completion: failed to lookup address information: Try again)"}`, first with `n` = 2 after about 15 s | `message` |

Stderr carries tracing `ERROR` lines (`failed to connect to websocket ...`) every
5 s from the start. The run had not ended when the 30 s deadline killed it
(exit -9 in the harness); the last counter seen was `Reconnecting... 4/5` at
26.7 s. With a resolver and the network shared (E1-dns) it ends
on its own after 16 s: five websocket reconnects, one `item.completed` error
`Falling back from WebSockets to HTTPS transport`, five HTTPS reconnects, an
`{"type":"error","message":...}` and
`{"type":"turn.failed","error":{"message":...}}`; exit 1. Every message names
`401 Unauthorized: Missing bearer or basic authentication in header` and the
URL `https://api.openai.com/v1/responses` (or its `wss` twin), plus a request id.
So the refusal is an HTTP 401 inside a started turn, not a login message: C1 must
treat `turn.failed` with a 401 text as the "no usable login" signal, not wait for
a start-up error. The wall time before the verdict is 16 s, mostly retries.

**T1 tool switches.** `codex features list` exists (exit 0, columns name, stage,
effective value) and lists every feature of `BASE` (`shell_tool`, `unified_exec`,
`browser_use`, `computer_use`, `multi_agent`, `image_generation`, `plugins`,
`hooks`, `code_mode_host` stable and on by default; `code_mode` and
`code_mode_only` under development and off). Each `-c` switch of `BASE`
(`approval_policy="never"`, `mcp_servers={}`, the eleven `features.*=false`,
`web_search="disabled"`) was run alone under `--strict-config`: all 14 reached
`thread.started`. The control `features.invented_c0a=false` under
`--strict-config` exits 1 with `unknown configuration field`, so a renamed or
removed feature name would be refused, not ignored. Nothing vanished or was
renamed on 0.163.0. Whether the tools are really off is a model-turn fact:
deferred to C0b.

**S1 system text.** Under `--strict-config` the keys `model_instructions_file`
(with a real file), `developer_instructions` and `instructions` were all accepted
by name (`thread.started`); the invented control was refused. Whether any
replaces or appends is deferred.

**L1 turn limit.** `max_turns`, `agent.max_turns` and `model_max_turns` are each
refused under `--strict-config` (`unknown configuration field`, exit 1; for
`agent.max_turns` the field named is `agent`). `exec --help` and `--help` name no
turn limit. Verdict: none found on 0.163.0 by these names; HQ6 must not rely on
a CLI turn limit.

**A1 start-up announcement.** None. Everything before the failing turn is the
three events of N1; no event lists tools or a surface. Whether a listing appears
after the model starts is not reachable without a login: deferred.

**M1 the CLI as an MCP client.** With
`-c mcp_servers.fake.url=http://127.0.0.1:PORT/mcp -c mcp_servers.fake.bearer_token_env_var=FAKE_MCP_SECRET`
and the variable only in the child's environment, the CLI contacts the fake
server without a login, at 0.48 s, at the same moment as `turn.started` and
before any model call. Three requests, all `POST /mcp`, in this order:

| # | Method | Notes |
|---|---|---|
| 1 | `initialize` (id 0) | `protocolVersion` `2025-06-18`; `capabilities` `{"elicitation":{"form":{},"url":{}}}`; `clientInfo` `{"name":"codex-mcp-client","title":"Codex","version":"0.163.0"}`; no `MCP-Protocol-Version` header |
| 2 | `notifications/initialized` | `MCP-Protocol-Version: 2025-06-18` |
| 3 | `tools/list` (id 1) | `params._meta.progressToken` 0; same header |

Header names present on every request: `user-agent`, `accept`, `content-type`,
`authorization`, `host`, `content-length` (plus `mcp-protocol-version` from request 2).
`Accept: text/event-stream, application/json` and `Content-Type: application/json`.
The `Authorization` value equals `Bearer` plus the random value on all three
(compared in memory, never recorded). The CLI opened no `GET` stream. The fake server answered plain `application/json`, with a
`202` for the notification. `codex mcp list --json` in an empty home prints `[]`
and exits 0. `codex mcp add --help` lists `--url`, `--bearer-token-env-var`,
`--env`, `--oauth-client-id`, `--oauth-client-secret`,
`--oauth-client-registration`, `--oauth-resource`, `-c`, `--enable`, `--disable`;
it has no approval option. `mcp add` itself was not run (it writes the config
file).

**M2 MCP deny and approval switches.** Config keys that exist and were accepted
under `--strict-config`: `mcp_servers.NAME.enabled_tools` and
`mcp_servers.NAME.disabled_tools` (control `mcp_servers.fake.invented_c0a` refused),
`approval_policy` (T1, `approval_policy="never"` under `--strict-config`),
`-s/--sandbox` (V2, as a flag), plus the flags `--approve-for-me` and
`--dangerously-bypass-approvals-and-sandbox`. The config key `sandbox_mode` was
not driven: the harness run `-c sandbox_mode="read-only"` under `--strict-config`
(empty throwaway home, no login) waited for `/tmp/probe-stack.lock` for over an
hour behind other probes and was withdrawn, so the verdict is "not driven (probe
lock unavailable)"; only the flag form `--sandbox read-only` is measured. Whether
any of them blocks an MCP tool call is a model-turn fact: deferred.

**F1 files in an empty `CODEX_HOME`** (type, bytes; the random directory suffix
shown as `*`; the harness also gives the process its `TMPDIR`):

| Flag set | Created |
|---|---|
| `--version` | `tmp/arg0-linux/codex-arg0*/` with `.lock` (0 bytes) and three symlinks (`apply_patch`, `applypatch`, `codex-linux-sandbox`) |
| `BASE` (with `--ephemeral`) | the same, plus `installation_id` (36 bytes), `shell_snapshots/` (one file, about 2.2 kB), `skills/.system/` (21 directories, 49 files, with a `.codex-system-skills.marker`), and the SQLite stores `goals_1`, `logs_2`, `memories_1`, `queue_1`, `state_5` (each with `-wal` and `-shm`; the databases 4 to 49 kB, the `-wal` files up to about 2 MB) |
| `BASE` without `--ephemeral` | all of the above, plus `sessions/YYYY/MM/DD/rollout-*.jsonl` (about 38 kB, from a run that the 30 s deadline killed, not a completed turn), `thread-writer-locks/` (a coordination lock and one lock per thread), `thread_history_1.sqlite` (with `-wal` and `-shm`) |

So `--ephemeral` removes the session file and the thread history only; the
installation id, logs, state databases and the unpacked system skills still land
in `CODEX_HOME`, which therefore must be a throwaway or per-run directory.

**B1 bubblewrap or user namespaces at start-up.** `setpriv --bounding-set=-all`
failed in the harness (`Operation not permitted`, exit 127), so the same condition was
set from outside: `bwrap --cap-drop ALL` plus `setpriv --no-new-privs` around
the CLI, traced with `strace -f -qq -e trace=execve,clone,clone3,unshare`. The
`BASE` start-up shows no `execve` of `bwrap` or `unshare`, no `unshare` call and
no `CLONE_NEW*` flag; its children are the shell-snapshot helpers
(`lsb_release`, `getconf`, `bash`, `locale`, `awk`, `sed` and friends). The
control `codex sandbox linux -- true` runs `codex-linux-sandbox`, which executes
`bwrap` (three `execve`s) and clones with `CLONE_NEWUSER`, `CLONE_NEWNS`,
`CLONE_NEWPID`, `CLONE_NEWIPC` and `CLONE_NEWNET`; `true` ran once, in bwrap's
namespace probe. The argv `sandbox linux -- true` then reached the second bwrap
with `CLONE_NEW*` and the re-executed `codex-linux-sandbox` tried to run
`linux -- true` (`execve` ENOENT three times), so it exited 101
(`Failed to execvp linux`); the requested inner `true` did not run. So bubblewrap and user namespaces are needed by the
sandboxed command path, not by the start-up of a turn with every feature off.
The half with a model turn is deferred to C0b.

**E1 egress.** `strace -f -qq -s 200 -e trace=connect,sendto` (no read or write
calls), no proxy variables. With the network shared but no resolver in the
sandbox: the only name looked up is `api.openai.com` (A and AAAA queries, UDP 53),
repeatedly; no connection follows. With the host resolver stub bound (E1-dns):
the same name, then `connect` to two Cloudflare IPv4 and two IPv6 addresses
(the port-65535 connects are address-selection probes) and to port 443; the only
host is `api.openai.com:443`. No update, telemetry or other host appeared before
the 401. The unauthenticated E1-dns request carried the prompt `hello` and Codex's
environment context, including the scratch working path, to OpenAI. The CLI
contacts the OpenAI API with no credential at all, so C1 and
the network policy must assume that a CLI started without a usable login still
calls out.

**Deferred to C0b and C0c (need a login and quota).** Stream of a completed
tool-free turn and its ending; tools really off under `BASE`; whether the three
system-text keys replace or append; whether any turn limit ends a run (none found
by name); MCP tool-call naming, parallel calls and whether `approval_policy`,
`--sandbox` or `disabled_tools` block MCP calls; files a completed run writes;
bubblewrap need in a model turn (HQ9, second half); the held reply and
JSON-only answers of M-live; a tool listing after the model starts (A1); hosts
of a model turn; token
lifetime and refresh by the child; refused or expired login.

**What each consumer may rely on.** C1: without a login the CLI starts a turn and
fails with `turn.failed` carrying a 401 text after about 16 s (exit 1), after
contacting `api.openai.com:443`. C2: `BASE` is accepted on 0.163.0 and
`--strict-config` refuses misnamed keys, so a run spec should pass it;
`CODEX_HOME` must be a per-run directory because even `--ephemeral` fills it.
C3: the event lines are `thread.started`, `item.completed` (with `item.type`
`error` for notices), `turn.started`, `error` and `turn.failed`. C5: the CLI
connects to a configured HTTP MCP server before the first model call, with
`initialize` at `2025-06-18`, the bearer from the named environment variable and
no `GET` stream. Adapter-port review: no CLI turn limit exists by name. HQ9: a
start-up with every feature off executes no `bwrap` and creates no namespace.

### A1b measured: claude 2.1.302

Measured on 17.10.2026 on `$HOME/.local/share/claude/versions/2.1.302`
(`claude --version` prints `2.1.302 (Claude Code)`), a 285,793,528-byte Bun
executable. No cell used a login, a token or an Anthropic account, except
A1b-0b1, A1b-0b2 and A1b-0e, which used the dedicated measurement token. Nothing here
reads `~/.claude`.

**Harness (every cell).** Each run is the CLI under `bwrap` with the network
unshared (loopback only), an empty environment and a throwaway home and working
directory in scratch, and the system directories bound read-only:

```text
bwrap --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/lib /lib \
  --symlink usr/lib64 /lib64 --ro-bind /etc /etc --tmpfs /home \
  --ro-bind <2.1.302> /opt/claude --proc /proc --dev /dev --tmpfs /tmp \
  --bind <scratch> <scratch> --unshare-net --die-with-parent --chdir <empty-work> \
  env -i PATH=/usr/bin:/bin HOME=<scratch>/home TMPDIR=<scratch>/home/tmp \
  DISABLE_AUTOUPDATER=1 /opt/claude ARGS
```

Unless a cell says otherwise, `ARGS` is `-p --input-format stream-json
--output-format stream-json --verbose`, and a small driver writes one
`{"type":"user","message":{"role":"user","content":"hello"}}` line to stdin 0.5 s
after the start and closes stdin 5 s later. "Init" is the `system/init` event.
Every run held `/tmp/probe-stack.lock`. The fake servers and proxies of the
cells below are scratch scripts on `127.0.0.1` inside the same unshared network.

**Driver.** `python3 -I drive.py DELAY -- /opt/claude ARGS` (`DELAY` is 0.5 unless
a cell says otherwise; `WAIT` is the seconds to wait after the write, default 5;
`END=term` sends `SIGTERM` instead of closing stdin). Each output line is printed
with its time since the start; the message line also carries `wall`, the driver's
`time.time()` at the write, which the MCP server's own `time.time()` log is
compared with. The file, verbatim:

```python
import sys, json, subprocess, time, threading
delay=float(sys.argv[1]); cmd=sys.argv[sys.argv.index('--')+1:]
t0=time.monotonic()
p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
def pump(stream,tag):
    for line in stream: print(f"{time.monotonic()-t0:8.3f} {tag} {line.rstrip()[:4000]}",flush=True)
th=[threading.Thread(target=pump,args=(p.stdout,'out')),threading.Thread(target=pump,args=(p.stderr,'err'))]
[t.start() for t in th]
time.sleep(delay)
msg=json.dumps({"type":"user","message":{"role":"user","content":"hello"}})
tw=time.monotonic()-t0
p.stdin.write(msg+"\n"); p.stdin.flush()
print(f"{tw:8.3f} --- user message written wall={time.time():.3f}",flush=True)
try: p.wait(timeout=float(__import__('os').environ.get('WAIT','5')))
except subprocess.TimeoutExpired:
    if __import__("os").environ.get("END")=="term":
        print(f"{time.monotonic()-t0:8.3f} --- still running; SIGTERM with stdin open",flush=True); p.terminate()
    else:
        print(f"{time.monotonic()-t0:8.3f} --- still running; closing stdin",flush=True); p.stdin.close()
    try: p.wait(timeout=20)
    except subprocess.TimeoutExpired: p.kill(); print("--- killed")
[t.join() for t in th]
print(f"{time.monotonic()-t0:8.3f} --- exit {p.returncode}")
```

Inside the sandbox the file lists of the home, the working directory and
`/tmp` (`find ... -printf '%y %s %P\n'`) were taken before and after the run and
diffed. The traces wrap the driver, verbatim from the harness scripts (`$S` is the scratch
directory, `$OUT` the cell's output directory):

```text
strace -f -qq -s 200 -e trace=connect,sendto,sendmsg,sendmmsg -o $OUT/strace.txt python3 -I $S/drive.py $delay -- /opt/claude "$@"   # A1b-0a c6
strace -f -qq -e trace=execve -o $OUT/execve.txt python3 -I $S/drive.py 0.5 -- /opt/claude -p --input-format stream-json --output-format stream-json --verbose --mcp-config $HOME/mcp.json "$@" > $OUT/run.txt 2>&1   # A1b-0c c7
```

#### A1b-0a flags and stream, no login

**c1 Flags.** Each flag below was added to `ARGS`. Every one reached init and the
unauthenticated result (c4); none printed an error. The control
`--invented-flag` exits 1 with `error: unknown option '--invented-flag'`, so an
accepted flag is not a silently ignored one.

| Flag | Result | Observable effect |
|---|---|---|
| `--max-turns 1` | accepted | none observable without a model; enforcement is A1b-0b2's |
| `--no-session-persistence` | accepted | see c5 |
| `--system-prompt-file F` | accepted | a missing `F` aborts before init with `Error: System prompt file not found: <path>`, so the file is read; its effect on the model is not observable without a login |
| `--strict-mcp-config` | accepted | no effect without `--mcp-config` |
| `--input-format stream-json` | accepted | needs `-p` and `--output-format stream-json`, which every run here sets |
| `--tools ""` | accepted | init `tools` is `[]` (22 entries without it) |
| `--setting-sources ""` | accepted | the empty value is taken; see c7 for what it does |

With all of `--tools "" --strict-mcp-config --setting-sources ""
--no-session-persistence --max-turns 1 --system-prompt-file F` init still lists
5 `agents`, 18 `skills`, 48 `slash_commands` and 3 `plugins` (all `builtin`)
beside `tools: []`. They are not tools, but a check that init is empty must say
which fields it means.

**c2 Init fields.** The init event carries these keys: `type`, `subtype`, `cwd`,
`session_id`, `tools`, `mcp_servers`, `model`, `permissionMode`,
`slash_commands`, `terminal_slash_commands`, `apiKeySource`,
`claude_code_version`, `output_style`, `agents`, `skills`, `plugins`,
`capabilities`, `analytics_disabled`, `product_feedback_disabled`, `uuid`,
`memory_paths`, `original_cwd`, `additional_directories`,
`messaging_socket_path`, `fast_mode_state`, `fast_mode_disabled_reason`,
`per_turn_effort_active`, `view_mode`. Values without a login: `apiKeySource`
`"none"`, `claude_code_version` `"2.1.302"`, `model` `"claude-opus-5-5"`,
`permissionMode` `"default"`, `mcp_servers` `[]`. The 22 default tools are
`Task, Bash, CronCreate, CronDelete, CronList, DesignSync, Edit, EnterWorktree,
ExitWorktree, ListAgents, NotebookEdit, Read, ReportFindings, ScheduleWakeup,
SendMessage, Skill, TaskStop, ToolSearch, WebFetch, WebSearch, Workflow, Write`.
`messaging_socket_path` is `/tmp/cc-socks-<uid>/<pid>.sock`: it is under `/tmp`,
not under `TMPDIR` or the home.

**c3 Init timing.** With stdin open and no message for 6 s, nothing was printed
on stdout or stderr. The driver wrote the message at 6.001 s; init arrived at
6.030 s (29 ms), then the `assistant` event at 6.054 s and the `result` at
6.056 s. With a 0.5 s delay init arrived 22 to 55 ms after the write in every run (0.523 to 0.556 s). The CLI prints init only after it holds the first
message (body fact 11 holds on 2.1.302). Section A1b-0c shows that it contacts a
configured MCP server before that message.

**c4 The unauthenticated run.** The events in order are `system/init`,
`assistant`, `result`. The `assistant` event has `error: "authentication_failed"`,
`is_api_error_message: true`, `model: "<synthetic>"`, zero token usage and the
text `Not logged in · Please run /login`. The `result` has `subtype: "success"`,
`is_error: true`, `terminal_reason: "api_error"`, `total_cost_usd: 0`,
`duration_api_ms: 0` and the same text. With `--input-format stream-json` the
process does not end after the `result`: it was still running 5 s later and
exited with code 1 when stdin was closed. `-p hello --output-format
stream-json --verbose` with stdin from `/dev/null` exits 1 after the same three
events; `-p hello` prints `Not logged in · Please run /login` and exits 1. No
network was needed for any of this (the network was unshared).

**c5 Files written with `--no-session-persistence`.** The working directory
stays empty. The home gained (names and sizes as measured):

```text
.claude.json                           852 B  (keys: firstStartTime, firstStartVersion,
                                               hasResetAutoModeOptInForDefaultOffer, machineID,
                                               migrationVersion, opusProMigrationComplete,
                                               pluginUsage, seenNotifications,
                                               sonnet1m45MigrationComplete, userID)
.claude/backups/.claude.json.backup.<ms>   84 B
.claude/sessions/                      empty directory
.claude/projects/<cwd>/memory/         empty directory
.claude/telemetry/1p_failed_events.<uuid>.<uuid>.json   224,562 B
$TMPDIR/claude-<uid>/                  empty directory
/tmp/cc-socks-<uid>/                   empty directory (outside the home and TMPDIR)
```

The `memory/` directory depends on `--system-prompt-file`, not on the other
flags. Re-run with exactly these flag sets (each with the default `ARGS`, no
login), by whether `.claude/projects/<cwd>/memory/` existed afterwards:

| Flags added to `ARGS` | `memory/` | Session `.jsonl` |
|---|---|---|
| `--no-session-persistence` | created | not written |
| `--max-turns 1`, or `--strict-mcp-config`, or `--tools ""`, or `--setting-sources ""` (each alone, A1b-0a c1) | created | written |
| `--tools "" --strict-mcp-config --setting-sources ""` | created | written (22,214 B) |
| `--tools "" --strict-mcp-config --setting-sources "" --no-session-persistence` | created | not written |
| `--system-prompt-file F` | not created | written (67,246 B) |
| `--system-prompt-file F --no-session-persistence` | not created | not written |
| `--tools "" --strict-mcp-config --setting-sources "" --no-session-persistence --max-turns 1 --system-prompt-file F` (A1b-0a c1) | not created | not written |

So the only flag measured to suppress `memory/` is `--system-prompt-file`; the
three flags `--tools "" --strict-mcp-config --setting-sources ""` do not, with or
without `--no-session-persistence`. The other entries of the list above were
created in every row (the re-runs listed only the home, so `/tmp/cc-socks-<uid>`
was not observed in those rows). Why `--system-prompt-file` suppresses it is not measured. A control
without the flag (the A1b-0c runs) did write
`.claude/projects/<cwd>/<session-uuid>.jsonl` although the run was unauthenticated.

**c6 Telemetry switches.** Same run, `--no-session-persistence`, under the c6 `strace` command of the harness. The network was unshared, so every
connection failed, but the lookups show the hosts:

| `DISABLE_TELEMETRY` | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | Hosts looked up | `telemetry/1p_failed_events.*.json` | `.claude.json` |
|---|---|---|---|---|
| unset | unset | `api.anthropic.com` only (DNS queries to the resolver `127.0.0.1:53`) | written (230,590 B) | 852 B |
| `1` | unset | `api.anthropic.com` | not written | 719 B |
| unset | `1` | none; no `connect` or send at all | not written | 719 B |
| `1` | `1` | none | not written | 719 B |

`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` also changes init: 21 tools instead
of 22 (`DesignSync` is gone), 16 `skills` instead of 18, 44 `slash_commands` instead of 48, and
`analytics_disabled` and `product_feedback_disabled` become `true`. Under
`DISABLE_TELEMETRY` alone only `analytics_disabled` becomes `true`; the tool,
skill and command counts stay 22, 18 and 48. The same hosts appear as `CONNECT`
targets in A1b-0d.

**c7 Managed settings and `--setting-sources ""`.** The host's `/etc` was not
touched. A scratch directory was bound read-only at `/etc/claude-code` inside
`bwrap` (with `--tmpfs /etc` plus read-only binds of `/etc/ssl`, `/etc/passwd`
and `/etc/hosts`), holding `managed-settings.json` =
`{"model":"managed-marker-model","permissions":{"defaultMode":"plan"}}` and
`managed-mcp.json` = `{"mcpServers":{"managed-srv":{"type":"http","url":"http://127.0.0.1:9/mcp"}}}`.

| Flags | init `model` | `permissionMode` | `mcp_servers` |
|---|---|---|---|
| none | `managed-marker-model` | `plan` | `managed-srv`, `failed`, source `enterprise` |
| `--setting-sources ""` | `managed-marker-model` | `plan` | `managed-srv`, `failed`, source `enterprise` |
| `--strict-mcp-config` | refused: `You cannot use --strict-mcp-config when an enterprise MCP config is present` (before init) | | |
| `--setting-sources "" --strict-mcp-config` | refused with the same sentence | | |

So a managed-settings file and a managed MCP config survive
`--setting-sources ""`, and `--strict-mcp-config` does not hide the managed MCP
server: with one present the CLI refuses to start. The image must hold neither
file (A1d conditions). Not measured: other managed locations (macOS, Windows,
`managed-settings.d`), which are outside the Linux image.

#### A1b-0c the CLI as an MCP client, no login

The fake Streamable HTTP server is a Python `http.server` script
(`ThreadingHTTPServer`, HTTP/1.1) on `127.0.0.1:18765` inside the unshared
network, started inside the sandbox before the CLI. It logs the wall time, the
time since its start, method, path, headers (the `Authorization` value reduced to
its length and whether it equals the expected dummy bearer) and JSON body of every
request. The whole script, verbatim (`mode` is its first argument; the default mode
is the plain run):

```python
import sys, json, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
mode, port, logpath = sys.argv[1], int(sys.argv[2]), sys.argv[3]
EXPECTED = "Bearer dummy-0c-token"; T0 = time.monotonic(); lock = threading.Lock()
def log(rec):
    with lock, open(logpath, "a") as f: f.write(json.dumps(rec) + "\n")
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _record(self, body):
        hdrs = {}
        for k, v in self.headers.items():
            hdrs[k] = f"<len={len(v)} matches_expected={v == EXPECTED}>" if k.lower() == "authorization" else v
        log({"wall": round(time.time(), 3), "t": round(time.monotonic() - T0, 3), "method": self.command, "path": self.path, "headers": hdrs, "body": body})
    def _send(self, code, obj=None, extra=None):
        data = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        if obj is not None: self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0).decode()
        try: body = json.loads(raw)
        except Exception: body = raw
        self._record(body)
        if mode == "401": return self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": 'Bearer realm="fake"'})
        if mode in ("d400", "d404") and isinstance(body, dict) and body.get("method") == "server/discover": return self._send(400 if mode == "d400" else 404, {"error": "unsupported"})
        if not isinstance(body, dict) or "id" not in body: return self._send(202)
        m = body.get("method"); extra = {"Mcp-Session-Id": "sess-0c-1"} if (mode == "session" and m == "initialize") else None; pv = mode[3:] if mode.startswith("pv:") else None
        if m == "initialize":
            res = {"protocolVersion": pv or body["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-mcp", "version": "0"}}
        elif m == "tools/list":
            res = {"tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object", "properties": {}}}]}
        elif m == "ping": res = {}
        else: return self._send(200, {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "method not found"}})
        self._send(200, {"jsonrpc": "2.0", "id": body["id"], "result": res}, extra)
    def do_GET(self): self._record(None); self._send(405, None, {"Allow": "POST"})
    def do_DELETE(self): self._record(None); self._send(200 if mode == "session" else 405, None)
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
```

Mode variants: `401` answers every `POST` with 401, body `{"error":"unauthorized"}` and
`WWW-Authenticate: Bearer realm="fake"`; `d400` and `d404` answer `server/discover`
with 400 or 404 and body `{"error":"unsupported"}`; `pv:<version>` answers
`initialize` with that `protocolVersion`; `session` is the session variant above.
`ARGS` also carries `--mcp-config F` where `F` is
`{"mcpServers":{"fake":{"type":"http","url":"http://127.0.0.1:18765/mcp","headers":{"Authorization":"Bearer dummy-0c-token"}}}}`;
the stdio control uses `{"mcpServers":{"fake":{"type":"stdio","command":"/usr/bin/sleep","args":["31"]}}}`.
Times in the request-order table are relative to the stdin write of the first
message: the server's `wall` (its `time.time()`) minus the driver's `wall=`.

Request order at the server, identical in every run that connected (three
plain runs and the variants with an `Mcp-Session-Id`, a 400 or a 404 answer to
the first request):

| # | Time | Request | `MCP-Protocol-Version` |
|---|---|---|---|
| 1 | -0.18 s | `POST server/discover` | `2026-07-28` (plus header `mcp-method: server/discover`) |
| 2 | -0.17 s | `POST initialize` | absent |
| 3 | -0.13 s | `POST notifications/initialized` | `2025-11-25` |
| 4 | -0.13 s | `GET` | `2025-11-25` |
| 5 | -0.12 s | `POST tools/list` | `2025-11-25` |

**The CLI connects before the first message and without a login**, about 0.18 s
before the message was written, which is also before init. Without a login the
CLI still contacts a configured MCP server and calls `tools/list`.

- **c1 Authorization.** Every request, including `GET`, carries the `Authorization`
  value from the config's `headers` unchanged (the server logged only its length and
  whether it equals the dummy `Bearer dummy-0c-token` of the config; the value
  is that dummy).
- **c2 `initialize`.** `protocolVersion` `2025-11-25`; `capabilities`
  `{"roots":{"listChanged":true},"elicitation":{"form":{},"url":{}}}`; `clientInfo`
  `{"name":"claude-code","title":"Claude Code","version":"2.1.302","description":"Anthropic's agentic coding tool","websiteUrl":"https://claude.com/claude-code"}`.
  Before it, request 1 (`server/discover`, protocol `2026-07-28`, the same client
  info and capabilities in `params._meta`) is a probe. The CLI fell back to
  `initialize` and connected when the probe got a JSON-RPC error with status 200,
  or a plain `{"error":"unsupported"}` body with 400 or 404.
- **Version negotiation.** If the server answers `initialize` with
  `2025-06-18` or `2025-03-26`, the CLI connects and sends that version in
  `MCP-Protocol-Version` afterwards. With `1999-01-01` it stops after request 2
  and init reports the server `failed`.
- **c3 `Accept`.** `Accept: application/json, text/event-stream` on every `POST`,
  `Accept: text/event-stream` on `GET`; `Content-Type: application/json`; `User-Agent:
  claude-code/2.1.302 (sdk-cli)`. A plain `application/json` answer is accepted:
  init reports the server `connected` and `tools/list` follows.
- **c4 `GET` and 405.** The CLI sends one `GET` right after `notifications/initialized`
  (with `MCP-Protocol-Version`, and with `Mcp-Session-Id` when the server issued
  one). After the 405 it neither retried within 5 s nor counted it as a failure:
  the server stayed `connected`.
- **c5 `tools/list` and `connected`.** `tools/list` is called once, after the
  `GET`. Init then reports `mcp_servers: [{"name":"fake","status":"connected","source":"dynamic"}]`
  and lists the tool as `mcp__fake__echo` (23 tools; with `--tools ""
  --strict-mcp-config --setting-sources ""` the list is exactly `["mcp__fake__echo"]`).
- **c6 After a 401.** When every `POST` answers 401 (with `WWW-Authenticate: Bearer`),
  the CLI sends requests 1 and 2 only, no retry, and init reports the server
  `failed` (22 tools). No further request was seen, so no OAuth flow ran
  against this server.
- **c7 No stdio process.** The `execve` trace of the harness on the HTTP run shows only
  the CLI's own children: `rg` (the CLI re-executing itself), `git`, `sh -c
  "uname -s ..."` and `uname`. A control run with
  `{"type":"stdio","command":"/usr/bin/sleep","args":["31"]}` shows an extra
  `execve("/usr/bin/sleep", ["/usr/bin/sleep", "31"])`. So the HTTP server config
  starts no process for the server. The CLI starts `git` and `rg` at startup; whether it runs
  without them is not measured. In the stdio control the CLI had not exited 20 s after
  stdin closed (killed by the driver; exit code not recorded).
- **c8 End of the session.** No `DELETE` and no `notifications/cancelled` reached
  the server when stdin closed (exit 1), nor on `SIGTERM` with stdin open (exit
  143), with or without an `Mcp-Session-Id` issued. In the session variant the
  CLI echoed `Mcp-Session-Id` on requests 3 to 5, so a server that issues one
  gets it back; in the measured cases (EOF on stdin after the unauthenticated
  result, and `SIGTERM`) it was not told the session ended.
- **c9 `MCP-Protocol-Version` and 202.** The header is absent on `initialize` and
  present with the negotiated version on every later request (`POST` and `GET`).
  A 202 with an empty body for `notifications/initialized` is accepted: the CLI
  went on to `GET` and `tools/list`.

#### A1b-0d egress and the pinned binary, no login

**c1 and c2 Hosts of an unauthenticated `-p` run.** `HTTPS_PROXY` and
`HTTP_PROXY` point at a scratch proxy on `127.0.0.1:18766` inside the unshared
network that records the request line and answers every `CONNECT` with 403.
The proxy script, verbatim:

```python
import socket, threading, sys, time, json
port, logpath = int(sys.argv[1]), sys.argv[2]
lock = threading.Lock()
def serve(c):
    try:
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = c.recv(4096)
            if not chunk: break
            data += chunk
        first = data.split(b"\r\n", 1)[0].decode("latin1")
        with lock, open(logpath, "a") as f: f.write(json.dumps({"wall": round(time.time(), 3), "request_line": first}) + "\n")
        c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    finally: c.close()
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", port)); s.listen(50)
while True:
    c, _ = s.accept(); threading.Thread(target=serve, args=(c,), daemon=True).start()
```

The sandbox sets `HTTPS_PROXY`, `HTTP_PROXY` and their lower-case forms to
`http://127.0.0.1:18766`.
`ARGS` is the default plus `--no-session-persistence`. The run ends with the same
unauthenticated result as A1b-0a in all four cases.

| `DISABLE_TELEMETRY` | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `CONNECT` targets in 5 s |
|---|---|---|
| unset | unset | `api.anthropic.com:443` (2 requests) |
| `1` | unset | `api.anthropic.com:443` (1 request) |
| unset | `1` | none |
| `1` | `1` | none |

A longer run (15 s, updater variants below) made 5 requests, all to
`api.anthropic.com:443`. No other host was contacted in any run. With the proxy
unset and the network unshared (A1b-0a c6), the lookups were also only
`api.anthropic.com`. Not measured: hosts a logged-in run contacts (inference,
authentication); they need a login.

**c3 `--version`.** Under the same proxy `claude --version` printed `2.1.302
(Claude Code)`, exited 0 and sent no request.

**c4 Download URL and checksum source of exactly 2.1.302.** The binary's strings
name `https://downloads.claude.ai/claude-code-releases`, a per-version
`/manifest.json` and a `/manifest.json.raw-sig.json` sidecar (RSASSA-PKCS1-v1_5
with SHA-512; the schema is `{version, platforms: {<platform>: {checksum, size,
binary}}}`). A read-only `curl` of the vendor's public storage, needing no
account, login or token, gave:

```text
curl -sS https://downloads.claude.ai/claude-code-releases/2.1.302/manifest.json
curl -sS https://downloads.claude.ai/claude-code-releases/2.1.302/manifest.json.raw-sig.json
curl -sSI https://downloads.claude.ai/claude-code-releases/2.1.302/linux-x64/claude        # 200, 285793528 bytes
curl -sSI https://downloads.claude.ai/claude-code-releases/2.1.302/linux-x64/claude.zst    # 200, 100901808 bytes
curl -sS  https://downloads.claude.ai/claude-code-releases/latest   # 2.1.302
curl -sS  https://downloads.claude.ai/claude-code-releases/stable   # 2.1.292
```

The manifest of 2.1.302 (commit `2ef144e6f98c1c99d082b735e4b38f50e3391e55`, build
date `2026-10-16T16:13:51Z`) lists, among eight platforms:

| Platform | SHA-256 | Size |
|---|---|---|
| `linux-x64` | `60d7eb7f12fbb28a52533fe8be5884097403562aa02dc73991a021c7067e29a5` | 285,793,528 |
| `linux-arm64` | `2bd4f4e5ed5cb09955b490098f745321cb5491e7ea8c752ba1e3bb010cdadb10` | 285,335,616 |
| `linux-x64-musl` | `39395c4d6fef3e38bf59d75890593a5474181c8e96534b54562312503685f787` | 279,426,576 |
| `linux-arm64-musl` | `58dd401b704351f4b70a0bb6faecbc1eb1405057b4dd4cf48a7beae93b4ca143` | 277,560,568 |

`sha256sum` of the local file is `60d7eb7f...e29a5`, the `linux-x64` entry, and its
size matches. The manifest says `manifestSignatureEnforcement: "flag"`. The
signature in the sidecar was not verified: the public key is inside the binary and
the check was not reproduced here. The pinned URL for CI and the image is therefore
`https://downloads.claude.ai/claude-code-releases/2.1.302/linux-x64/claude`, checked
against the manifest checksum above (the `-musl` entries are for a musl image;
the host binary is glibc-linked, body fact 11). `latest` and `stable` move, so
they must not be used for the pin. Not measured: whether the vendor keeps old
version directories for ever.

**c5 The binary path stays fixed with `DISABLE_AUTOUPDATER`.** The CLI was installed in
a throwaway home in the native layout (`~/.local/bin/claude` linked to
`~/.local/share/claude/versions/2.1.302`, a copy of the file) and run for 15 s
under the refusing proxy and the unshared network, once with `DISABLE_AUTOUPDATER=1`
and once with it unset. In both runs the link target, the contents of `versions/`
(one file, 285,793,528 B) and its SHA-256 were identical before and after, and the
only host contacted was `api.anthropic.com:443`. The unset run shows no update
attempt either, so this measurement does not show the updater acting; it shows
that with the switch set nothing changed. The updater's own behaviour with a
network and a newer release is not measured. The host's `versions/` directory
itself holds five versions (2.1.296, 2.1.299 to 2.1.302); whether a self-updating
install keeps old binaries in general is not measured. The image is to pin the file
and set the switch (A1d conditions).

#### A1b-0b1 the setup token as the login, live

Measured on 17.10.2026 on 2.1.302 (`claude --version` prints `2.1.302 (Claude Code)`)
with the operator's dedicated measurement token, authorized for this cell. It
reached the CLI only as `CLAUDE_CODE_OAUTH_TOKEN`, put into the child's environment
by a scratch script; no `ANTHROPIC_*` variable was set, and `<token>` below stands
for its value. Every stored output was redacted (the token value and any
`sk-ant-` string become `<token>`), and no stored output contained either. Billed
model calls: 1 (cap 2).

**Harness.** The A1b harness above with two changes: the network is not unshared
(`/run/systemd/resolve` is bound read-only so `/etc/resolv.conf` resolves), and the
scratch driver starts `bwrap` itself with the environment `PATH=/usr/bin:/bin`,
`HOME` and `TMPDIR` in a private home (mode 700), `DISABLE_AUTOUPDATER=1` and
`CLAUDE_CODE_OAUTH_TOKEN=<token>`. Inside the sandbox a script lists the home, the
working directory and `/tmp` before and after and runs the CLI under `strace -f -qq
-e trace=connect`. `ARGS` is `-p --input-format stream-json --output-format
stream-json --verbose --max-turns 1 --tools "" --no-session-persistence --model
haiku`; the message is `hello`, written 0.5 s after the start, and stdin is closed
once the `result` event has arrived. `--model haiku` was accepted and init reports
`model` `"claude-haiku-5-5"`. The private home was fresh, holding only the empty
`tmp`, for every run and was deleted afterwards.

**c1 The token is accepted.** The run reached an answer. The events in order are
`system/init`, `assistant`, `rate_limit_event`, `result`. The `assistant` event has
no `error` field and `model` `"claude-haiku-5-5"`; the `result` has `subtype:
"success"`, `is_error: false`, `terminal_reason: "completed"`, `stop_reason:
"end_turn"`, `num_turns: 1`, `total_cost_usd` 0.00058909 and `duration_api_ms` 911.
The `rate_limit_event` carries the keys `rate_limit_info`, `session_id`, `type` and
`uuid`. The process exited 0 after stdin was closed (no-login run, A1b-0a c4:
exit 1).

**c2 `apiKeySource`.** The init event of that run has `apiKeySource` `"none"`, the
same value as without a login (A1b-0a c2), although the token was accepted and the
call was billed. `apiKeySource` therefore does not tell a token login from no login
on 2.1.302; the `assistant` event without `error` and a `result` with `is_error:
false` do. A token login also reports `"none"`, so a check that `apiKeySource` is
`"none"` (Q2 (a), line 18) passes a token run; the value with an API key in the
child is not measured here.

**c3 No credential file.** The CLI wrote these files under the private home, fresh
and holding only the empty `tmp` (names and sizes; the token or `.credentials*`
appeared in no name, and a scan of every file's bytes for the token, `sk-ant-` and
`oat01` found none):

| Path under the home | Size |
|---|---|
| `.claude.json` | 51,875 |
| `.claude/backups/.claude.json.backup.<ms>` | 84 |
| `.claude/policy-limits.json` | 214 |
| `.claude/policy-limits.json.stamp.json` | 225 |
| `.claude/remote-settings.json` | 2 |
| `.claude/remote-settings.json.synced-at` | 13 |
| directories `.claude/projects/<cwd>/memory`, `.claude/sessions`, `tmp/claude-<uid>` | 4,096 each |

No file named `.credentials*` or holding a token exists, so with the token in the
environment the CLI writes no credential file. It does write `policy-limits.json`
and `remote-settings.json`, which the run of c5 (token rejected) did not: it wrote
only `.claude.json` (50,940 bytes) and its 84-byte backup. The two files appeared
only in the accepted run.

**c4 Refresh.** No credential was written (c3). Under `strace -f -e trace=connect`
the run connected to `127.0.0.53:53` (the resolver), and on port 443 to
`160.79.104.10` and `2607:6bc0::10` (both `api.anthropic.com`) and
`2600:1901:0:9e23::` (an address of `http-intake.logs.us5.datadoghq.com`, as is
`34.149.66.165`, which was only probed on port 0 during name resolution). The
accepted run made 12 `connect` calls to port 443, the rejected run of c5 13 and the
line-break run below 9, to the same hosts apart from the Datadog address, which the
line-break run did not use. The connect trace gives host and port, not request
paths, so it cannot tell a call to a token endpoint from an inference call; the run
made no connection to a host other than these, and no credential was written (c3);
whether any request refreshed something cannot be seen from host and port. Whether
the CLI refreshes an expiring token is not measured.

**c5 Invalid shape.** A token of 12 random alphanumeric characters (not
`sk-ant-oat`-shaped) was not rejected locally: the CLI contacted the API, retried
and gave a closed error. The events in order are `system/init`, `system/api_retry`
(`attempt` 1, `retry_delay_ms` 607, `error_status` 401, `error`
`"authentication_failed"`, `max_retries` 10), a second `api_retry` (`attempt` 2,
`retry_delay_ms` 1123, same status), `assistant`, `result`. The `assistant` has
`error: "authentication_failed"`, `is_api_error_message: true`, `model:
"<synthetic>"`, zero usage and the text `Failed to authenticate. API Error: 401
Invalid bearer token`. The `result` has `subtype: "success"`, `is_error: true`,
`terminal_reason: "api_error"`, `total_cost_usd: 0`, `duration_api_ms: 0` and the
same text; the `result` has no `error` field (the field is on the `assistant`).
`apiKeySource` is `"none"`. Stdin was closed at 3.2 s (after the `result`) and the
process exited 1 at 4.3 s. It reached `api.anthropic.com:443` and produced no model
answer (no billed call). The text did not echo the malformed value. The `error`
value `"authentication_failed"` is the same as without a login (A1b-0a c4), which
`claude/provider.py:465-466` (`_raise_for_api_error_message`) maps to
`CliLoginRefusedError`, so line 11's closed cause holds for a rejected token.

**c5a Token with line breaks (unplanned).** A `CLAUDE_CODE_OAUTH_TOKEN` value that
contains an interior line break is refused locally with `invalid_request`, exit 1,
no billed call. The CLI rejected it before any request for the answer: events
`system/init`, `assistant`, `result`; `assistant` has `error: "invalid_request"` and
the text `Invalid auth token · Fix external auth token · Invalid Authorization
header value from CLAUDE_CODE_OAUTH_TOKEN: it contains a line break at character <n>
(<n> characters on <n> lines).`; `result` has `subtype: "success"`, `is_error:
true`, `terminal_reason: "api_error"`, `total_cost_usd: 0`, `duration_api_ms: 0`;
exit 1 after stdin was closed. No model call was billed. The CLI does not remove
interior line breaks from the variable, so a token file that holds the token on one
line is a precondition of A1b-6a `token_file`; the landed `token_file` reader
(`agent_service.secret_file`) already refuses such a value before any run; the c1
run above used the file's content with all whitespace removed by the scratch script.
Not measured: whether a single trailing line break is accepted.

**c6 `cmdline`.** While the c1 run was alive, `/proc/<pid>/cmdline` of every process
of the sandbox tree (`bwrap`, the inner `sh`, `strace` and `/opt/claude`) was
compared in memory with the token before the message, 1 s after it and at the
`result`: `contains token: no` in all 12 samples. The CLI's cmdline is `/opt/claude
-p --input-format stream-json --output-format stream-json --verbose --max-turns 1
--tools  --no-session-persistence --model haiku`. `/proc/<pid>/environ` was not
read.

Not measured: the token's lifetime and what the CLI does when it expires or is
revoked; request paths (no proxy); a run with the token and an `~/.claude`
credential together.

#### A1b-0b2 the turn limit, live

Measured on 17.10.2026 on 2.1.302 (`claude --version` prints `2.1.302 (Claude Code)`,
checked without the token in the same sandbox) with the operator's dedicated
measurement token, authorized for this cell. It reached the CLI only as
`CLAUDE_CODE_OAUTH_TOKEN` in the child's environment (the A1b-0b1 route);
`<token>` stands for it, and the token-aware scan found it, the `sk-ant` prefix and every other
definite signature neither in the stored evidence nor in this section's diff. Live runs: 3 (cap 3), no retry.

**Harness.** A1b-0b1's harness (network not unshared, private home, empty working
directory, both deleted after each run, `--die-with-parent`) with these changes. The
environment is `PATH=/usr/bin:/bin`, `HOME`, `TMPDIR` (the home's `tmp`),
`DISABLE_AUTOUPDATER=1`, `MCP_TOOL_TIMEOUT=600000` (above every run's deadline, so a
held call cannot time out into an error result) and `CLAUDE_CODE_OAUTH_TOKEN=<token>`.
Inside the sandbox a script starts a loopback MCP server (A1b-0c's plain-mode server
with the barrier below) and then runs the CLI; `ARGS` is `-p --input-format
stream-json --output-format stream-json --verbose --max-turns N --tools ""
--strict-mcp-config --setting-sources "" --no-session-persistence --mcp-config
<home>/mcp.json --allowedTools mcp__fake__step --model haiku`, and `<home>/mcp.json`
names `http://127.0.0.1:<port>/mcp` with a dummy bearer. The one user message is
written to stdin, which is then closed. The CLI runs through the bounded runner
(`run_cli_bounded`) in its own process group with a deadline of 120 s (S1, S3) or
240 s (S5); the machine's probe lock is held for every run. Init reports `tools`
exactly `mcp__fake__step`, `mcp_servers` exactly `fake` `connected`, `model`
`"claude-haiku-5-5"` and `claude_code_version` `2.1.302` in all three runs; the
`modelUsage` key is `claude-haiku-5-5` in all three. bwrap binds only the scripts
read-only and the run's own directory read-write, at the same paths, instead of the whole
`<scratch>` directory; it keeps 0b1's `/run/systemd/resolve` bind.

**H, the additive hypothesis.** The CLI counts one turn per model response, whatever
`tool_use`, text or thinking blocks the response carries. `N` is the run's
`--max-turns`, `M` is `max_tool_calls`, `R` the model responses, `T` the calls the
model made and `S` the calls the fake server answered. The one free parameter is the
offset `d = S - N`, measured with one call per response (H admits `d` in {-1, 0, +1}).
H implies `max_turns = M + k` with `k = max(1 - d, 1)`, so that the service's limit
(rule 15) fires before the CLI serves call `M + 1`. Predictions: c1 (S1, `N` 3) fixes
`d` and has `R = 3 + max(d, 0)`, `S = 3 + d`; c2 (S3, `N` 3) has c1's counts; c3 (S5,
`N = 30 + k`) has `S = N + d`, at least 31. In every row each call is served except,
for `d = -1`, the call of response `N`.

**The response barrier.** The server starts with `P = N + 1` permits. Each `tools/call`
takes one permit and is answered at once while a permit remains; when none remains the
server sends no byte for it and logs `held`. `initialize`, notifications, `tools/list`,
`ping`, `GET` and `DELETE` take none. Under the assumption A (the CLI makes no model
request while a call is unanswered and none after a response without a call, and every
result it hands the model is the server's), a run has at most `P + 1` responses, so at
most 5, 5 and 33 (34 had k been 2), 43 here against the planned cap of 44. A's known breaches (a call the CLI answers locally, a
request it sends on its own) are detected in the stream, abort the run and stop the
series; none was detected. The bound counts the responses visible in the stream. The
quota exposure is larger by what the stream does not show (requests outside it, which
are not measured, retried requests, which abort the run, and responses after a breach)
and is bounded only by each run's deadline. The token is a subscription setup token:
the runs used plan quota and were not billed per call; `total_cost_usd` below is the
CLI's notional estimate, never a cap.

**The fixed measurement constants.** The tool is `step(n)` with `n` an integer. S1 and
S5 send:

> This is a measurement. Call the tool step with n=1. Each result names your next call; make exactly that call, one call per answer, and write no text. Stop only when a result says done.

and `step(n)` replies `Not done. Next: call step with n=<n+1>.`. S3 sends:

> This is a measurement. In each answer, first write one short sentence, then call the tool step. Start with n=1. Each result names your next call. Stop only when a result says done.

and `step(n)` replies `Not done. Next: write one short sentence, then call step with n=<n+1>.`.
No reply says "done", so a valid run ends by the CLI's limit.

**Why parallel calls and the mixed sequence are not measured.** The formula needs the
largest ratio of CLI turns to counted tool calls. One call per response is the worst
case for a CLI that counts responses: the ratio is exactly 1. A response carrying `p`
calls costs one turn if the CLI counts responses (ratio `1/p`) and `p` turns if it
counts calls (ratio 1), so parallel calls cannot raise the ratio above the single-call
loop's. One case escapes this and is named in c4, not measured.

**The runs.** Every run: layer 1 (evidence integrity) `intact`, layer 2 (sequence
compliance) `compliant`, layer 3 `complete` with terminal cause `ending` (the turn
limit, positively identified by `terminal_reason: "max_turns"`), teardown confirmed
(runner reason `complete`, process group gone, home and working directory deleted),
stop path none, `stop_series` false, exit code 1 after a natural end, no
`unprompted-response` and no `local-result`.

| | S1 (c1) | S3 (c2) | S5 (c3) |
|---|---|---|---|
| `--max-turns` N / permits P | 3 / 4 | 3 / 4 | 31 / 32 |
| R, `assistant` events | 3, 3 | 3, 6 | 31, 31 |
| T, `T_dup` | 3, 0 | 3, 0 | 31, 0 |
| text blocks, thinking blocks | 0, 0 | 3, 0 | 0, 0 |
| S, `S_dup`, `held`, `tail-unseen` | 3, 0, 0, 0 | 3, 0, 0, 0 | 31, 0, 0, 0 |
| U, `U_err` | 3, 0 | 3, 0 | 31, 0 |
| last call served | yes | yes | yes |
| `subtype` | `error_max_turns` | `error_max_turns` | `error_max_turns` |
| `is_error` | true | true | true |
| `terminal_reason`, `stop_reason` | `max_turns`, `tool_use` | `max_turns`, `tool_use` | `max_turns`, `tool_use` |
| `num_turns` | 4 | 4 | 32 |
| `total_cost_usd` (notional) | 0.00098875 | 0.00082068 | 0.00358051 |
| `d = S - N` | 0 | 0 | 0 |
| layer 4 | matches H | matches H, c1's `d` | matches H, c1's `d` |

The `result` of the CLI's limit differs from a completion's (A1b-0b1 c1: `subtype`
`success`, `is_error` false, `terminal_reason` `completed`, `stop_reason` `end_turn`):
it has `subtype: "error_max_turns"`, `is_error: true`, `terminal_reason: "max_turns"`,
`stop_reason: "tool_use"`, a numeric `total_cost_usd`, and the process exits 1.
`num_turns` is `N + 1` in all three runs although the CLI served `N` calls and made
`N` responses; this card does not explain the extra count and `num_turns` is no
substitute for `S`. The run's last `tool_result` arrived before the `result` (the CLI
served call `N`; it did not serve a call `N + 1`). Responses in S3 arrive as
two `assistant` events each (the text block, then the `tool_use` block) under one
`message.id`, so A1b-2's parser must group `assistant` events by `message.id` to count
responses: 6 events for 3 responses. A text block beside the call did not cost a turn.

**c1 S1, single-tool loop, `--max-turns 3`: matches, `d = 0`.** Three responses, three
calls, all served. The limit is checked after response 3's call ran, and no fourth
response was made. `k = max(1 - 0, 1) = 1`.

**c2 S3, text beside each call, `--max-turns 3`: matches c1's `d`.** The same counts as
c1 with a text block in each response (R 3, S 3). A text block does not cost a turn
of its own.

**c3 S5, the single-tool loop at `--max-turns 31` (`30 + k`): matches.** 31 responses,
31 calls, all served (`S = N + d = 31`, at least 31), ending in the CLI's limit. In a
one-call-per-response loop with `max_tool_calls = 30`, the service would therefore see
call 31 first and end the run `failed: limit_exceeded` (rule 15) before the CLI's limit.

**c4 The formula: established, within the measured sequences.** `d = 0`, `k = 1`, so
`max_turns = max_tool_calls + 1` (31 for the default 30). "Established" means H is
supported by one call per response, a text block beside each call, `N` 3 and 31, and
haiku, not proven for every response shape. **Handoff to A1b-4a, the whole-response
rejection gap.** A CLI that counted calls and refused a whole response whose calls
would cross `N` could stop up to `p - 1` calls before call `M + 1` (with `M = 30` and
`N = 31`, serve 29 calls and refuse a following three-call response, so the service
never sees call 31), and all three runs here could still match, because S1, S3 and S5
cannot tell response counting from call counting. The formula alone does not discharge
line 13's promise that the turn limit never fires first (#115: „greift nie vorher"); A1b-4a owns resolving that
remaining application condition before it makes the promise. Parallel calls (S2) stay
not measured.

**Responses used.** 3 + 3 + 31 = 37 visible model responses against the bound of 44
under A; no breach of A was detected. The notional cost of the three runs is 0.00538994.
Quota exposure beyond the visible responses is not measured (see above).

Not measured: other models, and a run that resumes after a max-turns ending; values
of `N` other than 3 and 31; parallel calls in one response, including the
call-counting CLI that refuses a whole response crossing `N`; a final answer inside a
tool loop (A1b-0b1 measured it at `N` 1 only); how an `isError` tool result counts (a
run that received the unexpected reply would not be driven); the cost of a thinking
block (haiku emitted none); any model request the CLI makes outside the stream, and
`MCP_TOOL_TIMEOUT`'s default; why `num_turns` is `N + 1`.

#### A1b-4a the turn counter in the 2.1.302 bundle (read, not run)

`ClaudeRun` (`agent_providers.claude.provider`) passes `--max-turns = max_tool_calls + 1`
(`TURNS_BEYOND_TOOL_CALLS`), the relation A1b-0b2 measured. This section settles the three
conditions that section left open. Nothing here was executed: it is a read of the pinned
`linux-x64` bundle of 2.1.302 (SHA-256 `60d7eb7f...e29a5`), whose JavaScript is plain text
inside the Bun executable. Offsets are byte offsets in that file.

The query loop:

- It starts with `guards:{...Gs,...,turnCount:1}` (251675819).
- The normal drain of `_e.streamingToolExecutor.getRemainingResults()` (251737344) runs every
  tool call of a response before the limit is checked.
- Only then it computes `rr=Bo+1,ts=E&&rr>E?E:void 0` (251743418), `Bo` being `turnCount` and
  `E` being `maxTurns`.
- If `ts` is set, it emits `max_turns_reached` and returns `{reason:"max_turns",turnCount:rr}`
  (251746191). Otherwise it recurses with `transition:{reason:"next_turn"}` and `turnCount:rr`
  (251746565).
- Only `next_turn` and `stop_hook_blocking` (`turnCount:V`, `V=Bo+1`, 251735883) raise the
  counter. `refusal_retry`, `max_output_tokens_recovery`, `truncated_response_recovery`,
  `malformed_tool_use_retry`, `thinking_only_retry` and the assignment at 251730657 (which selects
  `precomputed_compact_swap` or `reactive_compact_retry`) keep the old `turnCount`.
- The guard sets (`xr`, `Gs`, 251660178) carry no counter.
- The result serializer (251898628) copies `turnCount` into `num_turns` for `error_max_turns`, so
  the reported `num_turns` is `N + 1`, the figure A1b-0b2 could not explain.

Reproduce the reading with:

```text
python3 -I -c 'import mmap,sys;f=open(sys.argv[1],"rb");m=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ);[print(o,m[o-300:o+300].decode("utf-8","replace")) for o in map(int,sys.argv[2:])]' <binary> 251660178 251675819 251730657 251735883 251737344 251743418 251746191 251746565 251898628
```

**Condition 1, the whole-response rejection gap: resolved for 2.1.302.** The CLI counts one turn
per model response whose tool calls ran, never one per call, and checks the limit after all of
that response's calls ran. A call-counting CLI that refuses a whole response crossing `N` (the
A1b-0b2 c4 case) does not exist on the pinned version. The service's call limit fires first only
while every counter-raising transition includes a service-counted call; a blocking Stop hook and
a locally answered unknown-tool call break that, which condition 3 states. Parallel calls (A1b-0b2
S2) stay not run. A change of the pin reopens this reading (line 7, A1b-7a).

**Condition 2, no `--model` in production: resolved for the count.** The counter belongs to the
loop, not to the model. A thinking-only answer is retried without a turn, and output-limit
continuations do not count. What the default model emits stays model-dependent and falls under
condition 3; the first live runs on it are A1b-12a (tool-free) and A1b-12b (tools).

**Condition 3, k = 1 leaves no slack: a residual of line 13, with k kept at 1.** Turns the
service does not count can exhaust the CLI limit at any service call count, including zero. Two
kinds are known: a call to a name the CLI does not know, which it answers locally, and a blocking
Stop hook (`--setting-sources ""` drops settings hooks; whether the built-in plugins that init
lists register one was not read). The run then ends `error_max_turns`, which
`_raise_for_error_result` turns into `failed: provider_error`, never into a success. No call runs
that the service has not accepted, because the relay holds every call. The end-to-end proof is
A1b-8's. A larger k would be a guess no measurement supports and would weaken the backstop.

#### A1b-0e tool calls over http, live

Measured on 18.10.2026 on 2.1.302 (`claude --version` prints `2.1.302 (Claude Code)`,
checked without the token in the same sandbox) with the operator's dedicated
measurement token, authorized for this cell on 17.10.2026. It reached the CLI only as
`CLAUDE_CODE_OAUTH_TOKEN` in the child's environment (the A1b-0b1 route); `<token>`
stands for it, and the token-aware scan found it and every other definite signature
neither in the stored evidence nor in this section's diff. Live runs: 5 (cap 5), no
retry, E4 under its guard.

**Harness: A1b-0b2's, with these changes.** Same sandbox, bounded runner, private
`<home>` and empty working directory (deleted after each run), environment `PATH`,
`HOME`, `TMPDIR`, `DISABLE_AUTOUPDATER=1` and `<token>`, and the same `ARGS` with a
per-run `--max-turns N`, `--model haiku`, the tool `mcp__fake__step` without
annotations. `MCP_TOOL_TIMEOUT` is set per run (`600000`, `5000` or unset).
- The loopback server is a scripted fake, not the relay: per tool argument `n` and
  arrival it takes one action (reply, hold then reply, hold until teardown, JSON-RPC
  error, `isError` result, HTTP 500, drop), enforces a permit cap on answered calls,
  numbers connections, watches held sockets for a client close and logs integer
  millisecond times on the drive's clock. It stores no string taken from a request:
  header and `params` properties are logged as booleans, counts and members of fixed
  sets. It answers `initialize` with the pinned `2025-11-25`, the relay's only version.
- Fidelity: the JSON-RPC error is byte-equal in its body, and field-equal in its
  framing, to the relay's `answer_for(error_response(id, INVALID_PARAMS))`; the empty 500
  is field-equal to the relay's `_empty(INTERNAL_SERVER_ERROR)`; the other answers are the
  A1b-0b2 shape. Not reproduced: the relay's accept-time abort of a fifth connection. E2's
  fourth failure reads the request whole and then closes the socket: a disconnect after the
  request.
- A small anchor process starts the CLI as its child and hands the drive a pidfd for it
  (`os.pidfd_open`, `socket.send_fds`), so that E5 can signal the CLI alone through the
  pidfd and leave the group's reap to the runner. Production's leader is the CLI itself;
  here the leader is the sandbox, which would turn a SIGTERM into a SIGKILL a few
  milliseconds later. The anchor writes the CLI's exit code as one integer, which the drive
  reads after the teardown (`unknown` when it was not written in time).
- E5's planned stop: 2,000 ms after the server logs the first held arrival of n=2, the
  drive checks that n=2 is still pending, sends SIGTERM through the pidfd once, observes a
  window of up to 1 s (`CLI_TERMINATION_GRACE_SECONDS`, 10 ms ticks), then calls
  `request_abort()`.
- The probe lock was held for every run. The judge reads the stream and the server log
  through one set of identity rules: a model call is a distinct `tool_use` id; a request
  occurrence is one `tools/call` record; an occurrence is attributed to the model call
  whose `n` it carries when exactly one call carries that `n`, and only on a complete
  stream (reason `complete`, `aborted_by` `none`); on any other stream the association is
  provisional, and a class that reads it is `insufficient (provisional-attribution)` beside
  its direct observations. A re-send is a later occurrence of an attributed call, `same-id`
  or `new-id`.

**Hypotheses read from the binary (not facts).** A read-only `strings` search of the
pinned binary suggested: Hc2, an MCP tool without `readOnlyHint` is not concurrency-safe,
so the calls of one response run one at a time; Hc6, a per-call limit from
`MCP_TOOL_TIMEOUT` or the per-server `timeout` (default unresolved), a per-request HTTP
budget of `max(MCP_TOOL_TIMEOUT, 60000)` ms and an idle limit
set by the MCP tool client's idle-timeout variable, defaulting to 300,000 ms for an http server; Hc8, the string
`notifications/cancelled` appears 18 times.

**The fixed measurement constants.** The tool is `step(n)`. Every run's prompt, verbatim:

- E1 (ten calls in one response, `N` 1, 10 permits, 90 s, `MCP_TOOL_TIMEOUT=600000`):
  > This is a measurement. In your first answer, first write one short sentence, then call the tool step ten times in parallel in that same answer, with n=1, n=2, n=3, n=4, n=5, n=6, n=7, n=8, n=9 and n=10. Make no other call.

  Script: every `step(n)` is held 3,000 ms from its arrival, then answered `Recorded n=<n>.`.
- E2 (error answers, `N` 6, 9 permits, 150 s, `600000`):
  > This is a measurement. Call the tool step five times, one call per answer: first with n=1, then n=2, then n=3, then n=4, then n=5. Make the next call whatever the previous result says, even if it is an error. Write no text. After the fifth result, reply with the single word done and call no tool.

  Script, first arrival of each n: n=1 the relay's JSON-RPC error (-32602, `Invalid params`); n=2 the result `{"content":[{"type":"text","text":"Step failed."}],"isError":true}`; n=3 HTTP 500 with an empty body and no `Content-Type`; n=4 the request is read whole and the socket is closed without a byte; n=5 `Recorded n=5.`. A later arrival of n 1 to 4 would be answered `Recorded n=<n>.`.
- E3 (default timeout, `N` 2, 1 permit, 200 s, `MCP_TOOL_TIMEOUT` unset) and E4 (the
  same with `600000`):
  > This is a measurement. Call the tool step once, with n=1. If its result is an error, reply with the single word failed; otherwise reply with the single word done. Make no other call and write no other text.

  Script: every arrival of n=1 is held until 130,000 ms after the first arrival, then the earliest arrival whose connection is still open is answered `Recorded n=1.`. One bounded hold, not the longest a pending call can wait.
- E5 (short timeout then SIGTERM, `N` 2, 0 permits, 60 s, `5000`):
  > This is a measurement. Call the tool step with n=1. When its result arrives, whatever it says, call the tool step with n=2. Write no text and make no other call.

  Script: every arrival of n=1 and n=2 is held until teardown.

**Assumption B and the allowance.** B: the CLI counts every response against
`--max-turns N` whatever fed it. Under B a run makes at most `N + 1` visible responses,
18 over the series (15 without E4), a conditional allowance, not a guarantee. Used: 13
responses (1 + 6 + 2 + 2 + 2), the expected number; no run exceeded `N`, and no response
above `N` was seen, so B stayed unrefuted on these five runs (a first observation, not a
measurement of B). No breach and no `system/api_retry` was detected. Quota exposure,
apart: only responses visible in the stream are counted; requests the CLI makes outside it
are not measured, and the exposure is bounded only by each run's wall deadline (90, 150,
200, 200 and 60 s). The token is a subscription setup token: the runs used plan quota,
and `total_cost_usd` is the CLI's notional estimate, never a cap.

**The runs.** Init lists `tools` exactly `mcp__fake__step`, `mcp_servers` exactly `fake`
`connected` and `model` `"claude-haiku-5-5"` in all five runs; `modelUsage` model keys (`usage_models`) are
`claude-haiku-5-5` in E1 to E4, and E5 has no `result`. Every run's layer 1
(evidence integrity) is `intact`, layer 2 (sequence compliance) `compliant`, no run
stopped the series, every teardown was confirmed (`became_zombie` false), and every home,
working directory, `cli.exit` and `cli.sock` was deleted.

| Run | Layer 3 / cause | Responses (assistant events) | Calls made / requests / answered by the server / held / re-sent | Text blocks | `subtype`, `is_error`, `terminal_reason`, `stop_reason`, `num_turns` | Exit | `total_cost_usd` (estimate) | Stream complete |
|---|---|---|---|---|---|---|---|---|
| E1 | complete / ending | 1 (11) | 10 / 10 / 10 / 10 (3,000 ms each) / 0 | 1 | `error_max_turns`, true, `max_turns`, `tool_use`, 2 | 1 | 0.00106 | yes |
| E2 | complete / completion | 6 (7) | 5 / 5 / 4 / 0 / 0 | 1 | `success`, false, `completed`, `end_turn`, 6 | 0 | 0.00112 | yes |
| E3 | complete / completion | 2 (2) | 1 / 1 / 0 / 1 / 0 | 1 | `success`, false, `completed`, `end_turn`, 2 | 0 | 0.00068 | yes |
| E4 | complete / completion | 2 (2) | 1 / 1 / 1 / 1 / 0 | 1 | `success`, false, `completed`, `end_turn`, 2 | 0 | 0.00068 | yes |
| E5 | planned-stop / planned-stop | 2 (3) | 2 / 3 / 0 / 3 / later-new-id 1 | 0 | no `result` | `cli_exit` 143 | none | no |

E4 ran under its guard: E3's c6 was `fired` with `ms` 60,006, below 130,000. E2 and E5
each had a `thinking` block in response 2 (not counted as text). E5's stop path:
`aborted_by` `planned-sigterm`, precondition `n2-pending`, `signal_outcome` `sent`,
`signal_ms` 10,982, `exit_seen_ms` = `window_end_ms` = `abort_ms` = 11,516 (observation
times on the drive's clock), `abort_late_ms` 0, `window_complete` true, no `result` after
the signal (`signal-result` nothing), no after-window observation. Run times from the drive's
start to the `result`: E1 32.6 s, E2 9.1 s, E3 63.0 s, E4 132.7 s.

**Cells.** `insufficient` appears only where stated.

- **c1 Request shape** (all runs; 20 `tools/call` requests): `relay-compatible` in every
  run. Every `tools/call` carried `MCP-Protocol-Version: 2025-11-25`, a correct `Host`, no
  `Origin`, the bearer, no session id, and a body of `name`, `arguments` and `_meta` (no
  other `params` key; every `_meta` held a progress token and one further key, counted not
  named); ids are integers, the originals' ids are distinct (E5: provisional), the tool
  name is the bare `step`. `initialize` requested `2025-11-25` and was answered
  `2025-11-25`. Before `initialize` the CLI sends a `server/discover` request with
  `MCP-Protocol-Version: 2026-07-28`, and after `notifications/initialized` a `GET` (its
  `Accept` as 0c c3 recorded; the fake's POST-style both-types check does not apply to it);
  neither is a `tools/call`. For 11: the landed relay
  serves the pinned CLI's `tools/call`, and 11 c1 is built on the bare name. The relay
  was not run; its answers to `server/discover` and the `GET` are not part of this record.
- **c2 Concurrency and connections** (E1): `serial-one-connection(10)`. In E1's one
  response the ten calls arrived one after another, each 3,015 to 3,055 ms after the previous
  arrival (hold 3,000 ms), most outstanding 1, one connection, the first arrival at
  2,153 ms and the last answer at 32,592 ms (Hc2 predicted this). For 10b1: in this
  response the pinned CLI dispatched the ten calls one at a time on one connection; whether
  that bounds the pending calls of a production run is not measured. Not measured: tools
  with `readOnlyHint`.
- **c3 Re-sends** (E2 n 1 to 4): `none` for all four, on a complete run. E3: `none`.
  E4: no failure to read. E5 n=1: `none` (no later n=1 occurrence; the stream is not
  complete, so only the direct class stands). For 10b1: no re-send after a JSON-RPC
  error, an `isError` result, a 500, a drop or the 60 s give-up, so the duplicate rule
  guards only a misbehaving client. Separately observed in E5 (below, c5): `later-new-id 1`
  for n=2, provisional.
- **c4 Order of text and request** (E1): `text-first(133)`: the text event was received
  at 2,020 ms, the first request arrived at 2,153 ms (receipt lags the CLI's write, so the
  CLI wrote the text first). For 11: its fake CLI emits the text before the request.
- **c5 SIGTERM while a call waits** (E5): `exited-within-grace(143, 534)`: exit observed
  534 ms after the signal, code 143 (`cli.exit` written). Close: `closed-in-window(10985)`
  for the held n=2 occurrence (3 ms after `signal_ms`; the n=1 occurrence was closed at
  the same instant). Sent in the window: `other(GET, initialize, notifications/initialized,
  server/discover, tools/call)`, so no `notifications/cancelled` in the window. Directly
  observed (record seq 15): a new connection (conn 3) opened in the window. Its handshake
  had `server/discover`, `initialize`, `notifications/initialized` and `GET`, and no
  `tools/list`; then came a `tools/call` with n=2 and id 1 (11,051 ms, held, closed at
  11,509 ms), lower than the run's earlier `tools/call` ids 2 and 3. By the identity
  rules it is recorded as `later-new-id(1)` for n=2; no cell reads it (n=2's give-up is
  c5's). The ids restarted on the new connection, and no id was seen twice. After-window observations:
  none. Residuals: an exit between the last readability check and the signal cannot be told
  from one the signal caused; a close made before the signal and observed after it counts
  as pending (this run's close was observed 3 ms after `signal_ms`); an exit, close or
  request observed after `window_end_ms` is not counted; what the CLI wrote after the
  signal but the drive never consumed is unseen; times are observation times. For 10b2: its fake CLI's stop reproduces the outcome in c5 and c7. No design
  change follows: Q3 (a) sends SIGKILL after the grace either way. For 10b1: the
  observation is the `tools/call` with a lower id on a new connection within the window.
- **c6 The CLI's own call timeout**: E3 (unset) `fired(60006, closed)`: the CLI handed
  the model a `local-timeout` `is_error` result (receipt 62,184 ms), 60,006 ms after the
  first arrival, and the server saw the connection closed at 62,179 ms with no
  `notifications/cancelled` (Hc6 predicted about 60 s). E4 (`600000`): `survived-and-delivered`:
  the held call was answered at 131,846 ms, 130,066 ms after its arrival, with no close and
  no cancel, and the result reached the model as `server-reply` (receipt 131,858 ms). E5
  (`5000`, n=1): `insufficient (provisional-attribution)`, with the two sides apart: the
  stream's n=1 result is a `local-timeout` `is_error` result received at 7,292 ms, 5,004 ms
  after the first n=1 arrival; the server's n=1 occurrence ended `client-closed` at 10,985
  ms, and a `notifications/cancelled` for its id arrived at 7,303 ms. Only an
  E3 `fired` below 130,000 ms authorizes E4, and it did. For 11, in every outcome: the run
  environment is `ClaudeRun`'s (`_RUN_SWITCHES`), which sets no tool timeout. The default
  gave up at 60 s and `MCP_TOOL_TIMEOUT=600000` survived this 130 s hold, so 11 adds
  `MCP_TOOL_TIMEOUT` to `ClaudeRun`'s environment (or the per-server `timeout` to the
  relay's MCP config, which 11 chooses and proves) against the pending call's actual
  lifetime, up to `max_duration_s` (600 s by default), including the relay's acceptance and
  result-release waits, not `idle_timeout_s`; the 300 s idle hypothesis (Hc6) stays a
  residual for any wait above 300 s. For 10b1 and 10b2: E5 records the give-up at 5 s as
  its two sides, apart; E3's paired class on a complete stream is `fired(60006, closed)`.
- **c7 Error answers** (E2, per class): all four are `error-result-and-continued`. The
  server answered the original with the JSON-RPC error, the `isError` result, the 500
  and `dropped`; the CLI received `is_error` results of origin `server-error-text`
  (length 14), `server-error-text` (12), `local-other` (26, the 500) and `local-other` (135,
  the drop), none with an MCP error code, and each next call reached the server (n=5 on a
  new connection, after the drop, answered as `server-reply`). For 10b1 and 10b2: after the
  JSON-RPC error the next call still reached the server, so answering the ninth call, a
  duplicate or an off-list name with it does not cost the run its next call; the same
  holds after an `isError` result, an owner failure (500) and a dropped connection. The
  class that records a locally answered next call did not occur, so `later-server-use` has
  nothing to record.
- **c8 Cancel**: E2 n 1 to 4 `neither`; E3 n=1 `closed-only` (a close at 62,179 ms, no
  cancel); E4 n=1 `neither` (answered); E5 n=1 `cancelled-with-id` (a
  `notifications/cancelled` with a `reason` and a `requestId` equal to the n=1 occurrence's
  id arrived on conn 2 at 7,303 ms, 5,015 ms after n=1's arrival). Its pairing with the
  stream's 5 s give-up is provisional and unclaimed; the stream's timeout and the server's
  cancellation are kept separate. Conn 2 then carried n=2. `cancelled-other-id`
  0 in every run. For 10b1: two endings of a waiting call were
  observed. In E3 the connection was closed with no cancel. In E5 a `notifications/cancelled`
  for n=1's id arrived on conn 2, 5,015 ms after n=1, and the first connection closed only
  at 10,985 ms; how the cancel relates to the stream's timeout stays provisional. So the
  relay must treat both a client close and a `notifications/cancelled` as the end of a
  pending call, or state that a pending call is still forwarded after the CLI gave up. The two runs differ in
  `MCP_TOOL_TIMEOUT` and in whether a second call followed; the cause of the difference is
  not measured.
- **c9 The whole-response gap, a live check of 4a's reading** (E1, `--max-turns 1`):
  `whole-response-served`: S = 10, all ten calls of the one response had a server reply
  and a `server-reply` result; then `terminal_reason` `max_turns`, `subtype`
  `error_max_turns`, `num_turns` 2 (`N + 1`). The live run agrees with 4a's reading,
  condition 1: one ten-call response cost one turn on haiku, and no correction at
  `TURNS_BEYOND_TOOL_CALLS` follows.

**Not measured.** Tools with `readOnlyHint`; holds beyond 130 s, a pending call's lifetime
up to `max_duration_s`, and the 300 s idle hypothesis; `MCP_TOOL_TIMEOUT`'s exact default
and the per-server `timeout` field of the MCP config; the CLI's handling of a connection
aborted at accept (E2's n=4 observed a disconnect after the request); a SIGTERM to the
whole group in production (E5 signals the CLI alone, so its `rg` and `git` children were
not signalled); an SSE answer or a session id; other models; what the model writes after
an error. Every run was driven and none was withheld.

## H8 atelier-2

**Context.** The "one location" invariant could be read to cover the operator's
own tools.

**Decision.** The invariant covers apps that serve users (containers).
atelier-2, the operator's own agent tool, keeps its local executors, following
the ruling that subscription CLIs serve only the operator's own agents. A later
move onto the service is possible but not planned. This is a head default the
operator may overrule.

**Consequence.** The target names atelier-2 as the one explicit exception.

## H9 Package boundary and tool contract

**Context.** Apps need a light client; the service needs the provider core; the
tool contract needs one owner both can import.

**Decision.** This repository stays the one owner and becomes a uv workspace
with three packages:

- `agent-client`, which is light: the wire union (H6), the tool contract
  `ToolDeclaration` (name, description, JSON-schema input, and the effect class
  `read_only` or `side_effecting`, which defaults to side-effecting and never
  reaches a model) as its shared owner,
  the HTTP/SSE client, and the MCP server helper;
- the provider core `agent_providers`, whose `ToolCatalog` builds on
  `ToolDeclaration` from `agent-client`;
- `agent-service`.

Import contracts: client → stdlib, pydantic, HTTP and the MCP SDK only (pydantic
added by the A0a amendment below); core → client; service → core and client;
nothing imports the service. AGENTS.md will name both the library and the
service; deployment stays in edge (A1d).

**Consequence.** The repository changes from "a library, not a service" to the
home of both. The change takes effect with A0a and A1a, not with this record.

### A0a packaging ruling (head, 15.10.2026)

**Context.** A0a's plan had to settle how two distributions ship from one
repository without PyPI.

**Decision.**

- The repository becomes a uv workspace. The core distribution
  `overnightworks-agent-providers` stays at the root. A new member
  `packages/agent-client/` holds the distribution `overnightworks-agent-client`
  with import package `agent_client`. A1a1 later adds `packages/agent-service/`.
- Core pins the client exactly (`==X.Y.Z`) through `{ workspace = true }`, and
  the two are released in lockstep. One tag `vX.Y.Z` publishes exactly the two
  wheels. A test pins all version literals as equal, because uv ignores the pin
  inside a workspace.
- `ToolDeclaration` moves unchanged to `agent_client.tools`;
  `agent_providers.tools` re-exports the identical class.
- H9 is amended: the client depends on the stdlib, pydantic, HTTP and the MCP
  SDK. A0a adds pydantic only; httpx and mcp arrive with the slices that use
  them.
- Each consumer that moves to ≥0.2.0 lists both wheel URLs with sha256.
- The import name `agent_client` may collide with an unrelated PyPI
  `agent-client`; this is accepted because consumers install only pinned URLs.

**Consequence.** No consumer changes its imports. A consumer that listed only
the core wheel would make the resolver ask PyPI, where the names are
unregistered — a dependency-confusion surface — so both URLs are required.

## H10 Gateway proof

**Context.** LiteLLM holds the upstream keys and could leak prompts.

**Decision.** Postgres-backed virtual keys per app; the master key is never used
for inference. Budget reservation, plus provider-side spending caps. Content
callbacks, prompt and error persistence and debug logging are off, proven by a
unique canary that is absent from the DB, stdout and callbacks.

**Consequence.** The gateway is accepted only with that canary proof (A2a).

## H10b Budget overrun

**Context.** A budget checked after the fact can be overrun by requests in
flight.

**Decision.** Fail-closed worst-case reservation: a request is admitted only if
its maximum cost still fits the budget. Provider-side caps sit behind it.

**Consequence.** No in-flight overrun is possible; some requests that would have
fit are refused.

## H11 Interim, until the migrations land

**Context.** Until the apps move, subscription logins remain in containers.

**Decision.** Presentator's copresenter is operator-only or offline, a gate in
overnightworks/agent-presentator#197. Songmaker's AI paths (co-writer, judge,
cover) use the subscription only for the operator's own use. The operator ruled
on 15.10.2026:

- Songmaker gets no interim AI gate.
- Songmaker gets no friend accounts until A1s and A2c have landed.
- After that, the operator keeps only his own accounts in `/ki-abo`.

**Consequence.** No document claims the subscription has left the containers
until every mount is gone.

## H12 Background work (job tickets)

**Context.** Background jobs (Songmaker judge, cover) have no live user token,
and an app must never assert a principal on its own.

**Decision.** When a user starts such a job in a live request (two tokens, H3),
the app asks the service for a job ticket. The service signs it and binds it to
the workload client, the principal, the `/ki-abo` membership read from the
verified user token, the background purpose, the app's job id, and an expiry of
at most 24 hours. The background run later presents the workload token plus the
ticket. A CLI profile serves tickets only if it is marked `background: true`.
The ticket's token format, keys and rotation are settled in A1a4 (signing key and
capability, H3) and A1a5 (job tickets); one service-owned signing key covers both.

**Consequence.** Background runs carry the same verified identity as the live
request that started them, for at most a day.

## Slice order

Each slice becomes its own work item when it is dispatched; its plan review
settles the open points named here.

| Slice | Repo | Done when | Open points its plan settles | Depends on |
|---|---|---|---|---|
| #19 A0r Decision record (docs only) | agent-providers | see #19 (this record) | — | none |
| A0a Packages and tool contract | agent-providers | A uv workspace with core at the root and `packages/agent-client`. Lockstep 0.2.0. One tag publishes both wheels. `ToolDeclaration` lives in the client and is re-exported identically. Import gates run in CI. Existing tag pins are unchanged. | settled by the A0a plan (15.10.2026), recorded by #19 | #19 |
| A0b Wire protocol | agent-providers | The protocol is fully enumerated: every non-terminal and terminal variant with its payload, protocol version and rejection of unknown versions, sequence rules, SSE framing, and an SSE codec/state machine that validates a stream. The run id is delivered before any fallible body. A tool's effect class (read-only or side-effecting) is part of `ToolDeclaration`, and `failed: interrupted` is defined by it. The existing `agent_providers.events` is either the core's internal event type or replaced, never a second wire owner. | settled by the A0b wire ruling (15.10.2026) | A0a |
| #28 A1a0 Wire additions | agent-providers | see #28 | — | A0b |
| #30 A1a1k Real Keycloak token shapes | agent-providers | see #30; its facts stand under H1 "Token shapes" | — | — |
| A1a1 Thin authenticated run | agent-providers | `packages/agent-service` exists with its entry point. `POST /runs` checks the workload token (realm role `agent-workload`, `azp == client_id ==` the registered workload client, `exp - iat` ≤ 300 s) and then the user token, applies H5 routing as one pure function, refuses every forbidden field, and streams from a scripted fake runner. The end-to-end proof uses real Keycloak tokens from #30. | Recorded with the slice | A1a0, A1a1k, webauth#51 |
| A1a2 Run records | agent-providers | In-memory records for 1 h. `GET /runs/{id}` requires the run's exact workload client and principal and answers `not_found` otherwise. Disconnect is handled, and the loss on restart is documented. | — | A1a1 |
| A1a3 Liveness and limits | agent-providers | Heartbeat, idle timeout, maximum duration, tool-call and byte limits, and slow clients. This row owns H6's limits. | — | A1a1 |
| A1a4 Signing key and capability | agent-providers | An RS256 capability is minted at admission and handed to the runner, with one key, its `kid` and a documented rotation. The registry gains the MCP origin. | — | A1a1 |
| A1a5 Job tickets | agent-providers | `POST /job-tickets` (own audience, at most 24 h), ticket runs and the `background` flag. The ticket binds `/ki-abo` and the app group (H12). | — | A1a4 |
| A1b CLI runner | agent-providers | H7 isolation, and only the selected profile's login is visible to its run. The service-wide limit of parallel subscription runs (default 5) is taken, and a request beyond it gets 429 `busy` with `Retry-After`. Cancel and disconnect kill and reap the process group. The run's MCP config names only the relay, which owns the run's tool events; the capability reaches only the app, and no token appears in argv or logs. | — | A1a1 |
| A1m MCP helper | agent-providers | The `agent-client` Streamable HTTP MCP server helper verifies the capability (format from A1a4), denies redirects, validates Host/Origin, and refuses a bad audience, an expired capability, a wrong run or a tool off the allowlist. | — | A0b, A1a4 |
| A1d Deployment | edge | A tagged service image is built from the agent-providers release. It runs in edge compose on an internal network with egress limited to the registered origins and LiteLLM. The profile config is code, each profile's login sits in its own volume from `~/.secrets`, and Keycloak has the `agent-service` audience plus the workload clients. A live smoke test with the operator's account reaches a terminal event. The container runs the service under an init (`init: true`, so the service is not PID 1), sets `no-new-privileges`, mounts nothing over `/proc` or its numeric directories, and gives the service credentials and an LSM policy that let it SIGKILL its own same-uid descendants (A1b-3 conditions, line 20). | — | A1b, A1m, edge#12 live |
| A1s Songmaker co-writer moves | songmaker | The co-writer runs through the service with the operator's real login, and its tools work over MCP with ownership checks. The in-process co-writer route is removed, along with the local provider/model/route choice. | — | A1d, Songmaker on Keycloak (K12) |
| A1r1 Judge moves | songmaker | The judge runs with job tickets (H12). | — | A1s |
| A1r2 Probes | songmaker | Decided at its pull: move or retire. | move/retire | A1s |
| A1r3 Cover image | songmaker, agent-providers | The service gains a Codex image route (the first non-Claude provider), and covers run through it. | — | A1s |
| A1r4 Credential mounts removed | songmaker | No Songmaker container mounts a subscription credential (checked in CI). | — | A1r1–A1r3 |
| A2a Gateway | edge | LiteLLM is up with H10 and H10b proven, including the canary. | — | edge#12 live |
| A2b API route | agent-providers | The service's API route and tool loop serve API-eligible principals. | — | A1a1, A2a |
| A2c "KI noch nicht freigeschaltet" | songmaker | A `not_enabled` result renders the message, and a UI test pins it. | — | A2b, A1s |
| A3 Presentator copresenter moves | agent-presentator | The copresenter goes through the service, and the H11 gate is lifted. | — | A2b, K13 |

## Out of scope at this size

- arbitrary MCP endpoints and dynamic discovery;
- per-user LiteLLM keys;
- a generic policy engine;
- queues and high availability;
- any provider beyond Claude before A1r3.
