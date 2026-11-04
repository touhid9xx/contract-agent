# Decisions & Assumptions

Running log of architecture choices, assumptions, trade-offs and unresolved
conflicts. Newest sections are appended; nothing is silently deleted.

Priority order used whenever requirements conflict (from the governing
clarifications): security/privacy/legal → data integrity, isolation,
auditability → explicit NON-NEGOTIABLEs → approved architecture & gates →
features & visuals → convenience.

---

## Phase 0

### D0-01 — Mocked backend is a standalone FastAPI app, not a stub layer

- **Decision:** `demo/backend/main.py` is a self-contained FastAPI service that
  serves JSON from `demo/fixtures/*.json` and keeps mutations in memory.
- **Why:** the demo must be runnable with no MySQL, Kafka, Ollama, SMTP, SMS or
  voice provider, and must be replaceable without touching the UI.
- **Trade-off:** state resets on restart. Acceptable — it is a demo, and every
  response carries `X-Mocked: true` so nothing can be mistaken for real data.

### D0-02 — Demo mirrors the future `/api/v1` contract

- **Decision:** demo routes are already `/api/v1/...` with the same payload
  shapes, status codes (`202` vs `200`, `409` for stale version and for a
  blocked pipeline) and error envelope that Phase 1 will implement.
- **Why:** keeps `Web → API` shape stable so P2-I and F6 become wiring, not a
  rewrite. Also lets us prove the 409 conflict UX now, cheaply.

### D0-03 — Frontend is the real Next.js 16 app, not a mockup

- **Decision:** Tailwind 4, TanStack Query, Zustand, next-themes, lucide-react
  and the real component primitives (`Button`, `Card`, `Badge`, `Skeleton`,
  `EmptyState`, `ErrorState`) are used from day one.
- **Why:** GATE 1 is supposed to judge the real look and real interactions; a
  throwaway HTML mockup would have to be rebuilt anyway.

### D0-04 — Server data never enters Zustand

- **Decision:** TanStack Query owns all server state; Zustand owns only UI state
  (sidebar, shortcuts dialog, toasts).
- **Why:** mandated by the spec's state table, and it prevents stale duplicate
  copies of contract/notification data.

### D0-05 — Display-time PII masking in fixtures

- **Decision:** fixtures store phone/address already masked (`+8801******`,
  `Dhaka, BD`) rather than real-looking values that the UI hides.
- **Why:** a fixture file is the easiest place for PII to leak into git history,
  logs or screenshots. Masking at the source makes exposure impossible. Phase 1
  replaces this with Fernet encryption + role-gated reveal (F7).

### D0-06 — Deterministic simulated latency instead of random

- **Decision:** the mocked backend sleeps a fixed 90–320 ms per request (seeded
  per route), not a random range.
- **Why:** loading skeletons are visible and reproducible; random timing makes
  UI screenshots and test expectations flaky.

### D0-07 — Launcher auto-selects a free port

- **Decision:** `scripts/demo.mjs` prefers `8000`/`3000` but walks up to 20
  ports when they are busy, printing the port it chose.
- **Why (observed):** on the developer machine used for GATE 1, port `3000` is
  published by Docker Desktop, so `npm run demo` would otherwise fail on a
  machine where everything is installed correctly. Failure would have been
  indistinguishable from a broken demo.
- **Trade-off:** the printed URL is authoritative — `DEMO.md` and the banner say
  so explicitly.

### D0-08 — `pre-commit` is not available yet (known gap, not skipped silently)

- **Conflict:** the workflow requires `pre-commit run --all-files` before every
  commit, but `.pre-commit-config.yaml` is an F1 deliverable and starting Phase 1
  before GATE 1 is forbidden by the spec.
- **Resolution used now:** the GATE 1 commit is made after the executable
  checks that exist in this phase (`tsc --noEmit`, `next build`, runtime smoke
  test of every route). This deviation is stated in the GATE 1 block and in
  `STATUS.md` rather than hidden.
- **Proposed resolution for the human:** either (a) accept that F1 adds the hook
  set retroactively and re-run it over the Phase 0 tree, or (b) allow a
  Phase-0-only `.pre-commit-config.yaml` now. Recommendation: (a) — the hooks
  (ruff/mypy/bandit/gitleaks) target Phase 1 Python code and would need to be
  scoped anyway.

### D0-09 — Open defaults chosen for GATE 1 review (from Appendix E)

| # | Question | Default used |
| --- | --- | --- |
| Q1 | Demo run command | `npm run demo` (uvicorn + `next dev`) |
| Q2 | Escalation SLA | 48 h after escalation is raised |
| Q3 | Reminder semantics | T-60/T-30/T-14 are calendar-day boundaries compared by a daily scan; demo timestamps are illustrative |
| Q4 | Voice depth | AMD result and TTS script are mocked text events; no audio |
| Q5 | Channel mix | fixed 62 / 21 / 17 %, labelled MOCKED |

### D0-10 — Upload is deliberately disabled

- **Decision:** the "Upload contract" affordance is present but disabled, with a
  tooltip naming the milestone that implements it (`P2-A`).
- **Why:** the spec requires every visible control to work **or** be visibly
  disabled with an explanation. Upload cannot be faked without implying that
  ingestion works.

---

## Unresolved conflicts to raise at GATE 1

1. **`pre-commit` timing** — see D0-08.
2. **Voice channel legal review** — the demo shows voice + AMD, but the spec
   itself requires jurisdiction/lawful-basis/recording-consent configuration
   before voice is enabled at all. Voice remains disabled by default in
   production regardless of what the demo shows.
3. **Retention values** (7 y / 2 y / 1 y / 90 d) are provisional defaults, not
   legal mandates; they must be approved per purpose and jurisdiction before
   real customer data exists.

---

# Phase 1 — Foundation decisions (F1)

## D1-01 — `pre-commit` is installed into the project venv, not globally

- **Decision:** `.husky/pre-commit` execs `venv/Scripts/pre-commit.exe` (or
  `venv/bin/pre-commit`), falling back to a bootstrap hint. `npm run bootstrap`
  installs it from `backend/requirements-dev.txt`.
- **Why:** the repo must be self-contained on a machine where no global Python
  tooling exists. A globally-versioned `pre-commit` would drift between
  developers and CI.

## D1-02 — Resolves GATE 1 deviation D0-08

`pre-commit run --all-files` **is now runnable** and was run over the whole tree
including the Phase 0 demo files. D0-08 is closed.

## D1-03 — `gitleaks` is bound to the `manual` stage

- **Decision:** the gitleaks hook uses `language: docker_image` and
  `stages: [manual]`, so `pre-commit run --all-files` does not block on Docker.
- **Why:** the spec requires secret scanning, but requiring a running Docker
  daemon for every local commit is hostile and would push developers to
  `--no-verify`, which disables *all* hooks. The hook is still present, pinned
  and documented — run it with
  `pre-commit run gitleaks --all-files` (or in CI).
- **Residual risk:** a developer who never runs the manual hook relies on CI.
  Recorded rather than hidden.

## D1-04 — Product frontend runs on port 3100, demo on 3000

- **Decision:** `frontend/package.json` binds `next dev`/`next start` to 3100.
- **Why:** the Phase 0 demo owns 3000 (and the launcher may walk it higher).
  Keeping the real app off 3000 lets both run side by side during the Phase 1
  → Phase 2 transition without port guessing.

## D1-05 — `/api/*` is proxied through Next.js, not called cross-origin

- **Decision:** `frontend/next.config.ts` rewrites `/api/*` to `API_PROXY_URL`
  (default `http://127.0.0.1:8000`).
- **Why:** refresh-token cookies stay first-party, so `SameSite=Strict` + no
  CORS credentials hack is possible in F3. It also matches the demo's proven
  shape, so `lib/api.ts` survives the Phase 2 swap.

## D1-06 — Backend errors use a single envelope

- **Decision:** every error response is
  `{"error": {"code", "message", "request_id", "details"?}}`, produced by one
  handler set in `app/main.py`; unhandled exceptions never leak a traceback.
- **Why:** the frontend needs one parser, and correlation IDs must appear on
  failures as well as successes so a support ticket can be traced.

## D1-07 — `mypy --strict` on `backend/app`, not on tests

- **Decision:** hooks and CI type-check `backend/app` only.
- **Why:** strict mode over pytest fixtures produces noise that hides real
  errors; tests are covered by ruff and by actually running.

## D1-08 — Frontend checks run through a Node bridge script

- **Decision:** `scripts/run-frontend-check.mjs <typecheck|lint>` spawns npm in
  `frontend/`, allowlisting the task name.
- **Why:** pre-commit cannot express "run an npm script in a subdirectory"
  without `bash -c`, which does not exist on the Windows host. Allowlisting the
  argument prevents the script becoming a `run-anything` shim.

## D1-09 — `make` is a convenience wrapper, npm is the contract

- **Decision:** the `Makefile` targets delegate to `node scripts/*` / venv
  commands and are documented as optional.
- **Why:** `make` is not installed on the primary Windows machine. The
  documented one-command path (`npm run bootstrap`) must not depend on it.

---

## Phase 1 — F2 (persistence, outbox, relay)

### D2-01 — Stored timestamps are naive UTC

- **Decision:** every `DATETIME` column stores a naive UTC value.
  `utc_now()` returns `datetime.now(UTC).replace(tzinfo=None)`, and `as_utc()`
  is the single place that re-attaches `UTC` for serialisation.
- **Why:** MySQL `DATETIME` discards `tzinfo`, so an aware datetime comes back
  naive and any `aware - naive` arithmetic raises at runtime in production but
  not in tests. One convention everywhere beats a mixed one that only fails
  sometimes.
- **Trade-off:** presentation needs the tenant's timezone, which is not stored
  here — it arrives with tenant settings later.

### D2-02 — `tenant_id` is repeated on every tenant-owned table

- **Decision:** tables carry their own `tenant_id` column even when the owner
  can be reached through a join (e.g. `extracted_fields` → `contracts`).
- **Why:** tenant scoping becomes one predicate in one place
  (`TenantScopedRepository`), which is auditable. Reaching the tenant through a
  join means every new query has to re-derive the rule, and one missed join is a
  cross-tenant leak.
- **Trade-off:** a redundant column and the obligation to keep it consistent
  with its parent. Accepted — correctness of isolation outranks normalisation.

### D2-03 — Outbox claiming uses a row lease, not a Redis lock

- **Decision:** `SELECT … FOR UPDATE SKIP LOCKED` plus a `locked_until` lease.
  No Redis, no singleton relay.
- **Why:** a Redis singleton lock would (a) cap publishing throughput at one
  process and (b) turn a Redis outage into an outage of *event delivery*, which
  is worse than the problem it solves. A row lease is correct with N replicas
  and survives a crashed worker by expiring. The lease doubles as a fencing
  token: `record_outcome` refuses to write for a row this worker no longer owns.

### D2-04 — The relay knows nothing about Kafka

- **Decision:** `OutboxRelay` publishes through an `EventPublisher` protocol.
  `UnavailablePublisher` — the stand-in until a real transport lands in F4 —
  **raises** `PublisherUnavailableError` rather than returning successfully.
- **Why:** a publisher that silently no-ops would let the relay mark rows
  `published` while nothing was sent — exactly the log-and-drop failure the spec
  forbids. Failing loudly retries with backoff and eventually parks the row in
  `failed`, where an operator can see it.
- **Updated (F4):** the stand-in has been superseded in practice —
  `build_publisher` returns `KafkaPublisher` when `EVENT_TRANSPORT=kafka`
  (D4-01…D4-05). The decision itself stands unchanged: `OutboxRelay` still knows
  only the protocol, and `UnavailablePublisher` still refuses rather than no-ops
  when no transport is selected.

### D2-05 — The outbox has no foreign key to `tenants`

- **Decision:** `outbox.tenant_id` is a plain indexed `CHAR(36)`, unlike every
  other tenant-owned table, which cascades from `tenants`.
- **Why:** the outbox records something that has already happened. If a tenant
  row is ever purged, cascading would delete undelivered events — losing the
  very record that proves delivery was owed. The envelope also has to remain
  publishable if the tenant row is mid-cleanup.
- **Trade-off:** no referential integrity on that column; the relay treats
  `tenant_id` as data, not as a lookup.

### D2-06 — Timestamps are floored before they are written

- **Decision:** `enqueue_event` stores `truncate_to_second(available_at or
  utc_now())`, not the raw value with microseconds.
- **Why:** a real defect, caught by the MySQL integration test rather than by
  reasoning. MySQL's `DATETIME` (no fractional-seconds specifier) **rounds** a
  value carrying microseconds — so a row enqueued at `12:00:00.7` is stored as
  `12:00:01` and is *not yet due* at `12:00:00.9`. The relay would skip an event
  it had just written and only pick it up on a later tick. SQLite keeps the
  microseconds, so the SQLite suite could never have found this. Regression test:
  `test_enqueued_timestamp_is_floored_to_stored_precision`.
- **Rejected alternative:** widening the columns to `DATETIME(6)`. It fixes the
  symptom but requires a dialect-specific type (`mysql.DATETIME(fsp=6)`) that
  SQLite does not have, and the system has no requirement finer than a second —
  the backoff floor is 2 s.

---

## Phase 1 — F3 (authentication, tenant binding)

### D3-01 — Argon2id, with parameters read from the stored hash

- **Decision:** passwords are hashed with Argon2id (`argon2-cffi`), parameters
  coming from `Settings` (`argon2_time_cost` 3, `memory_cost` 65536 KiB,
  `parallelism` 4 — argon2-cffi's defaults, above the OWASP minimum).
  `verify_password` always uses the parameters **stored inside the hash**, never
  the current settings; `password_needs_rehash` compares the two.
- **Why:** cost factors must be raisable as hardware improves. If verification
  used the *configured* cost, raising it would invalidate every existing hash and
  lock out every user. Reading the stored parameters means a cost increase is
  invisible to users: an old hash still verifies, and the next successful login
  transparently re-hashes it at the new cost (`auth.py` rehashes best-effort and
  never fails the login if the write fails).
- **Trade-off:** two sources of truth for the cost, which is exactly what
  `password_needs_rehash` exists to reconcile.

### D3-02 — The tenant slug in the login body is the only client-supplied tenant

- **Decision:** `POST /auth/login` takes `{tenant, email, password}`. It is the
  sole endpoint that accepts a tenant from the client. Every other route derives
  `tenant_id` from the authenticated principal.
- **Why:** login happens *before* a principal exists, so there is nothing else to
  derive a tenant from — the caller has to say which organisation they are aiming
  at. Crucially the slug **selects, it does not authorise**: the token that comes
  back carries the `tenant_id` of the tenant that owned the *matched user row*,
  so a mis-targeted slug cannot mint a token for another organisation, and
  `get_current_principal` re-checks that the row still belongs to the tenant the
  token claims.
- **Trade-off:** a login attempt reveals nothing, but the slug is public
  knowledge within the deployment (it is in URLs). Accepted: the secret is the
  password, not the tenant name.

### D3-03 — The user row outranks the token on every request

- **Decision:** every authenticated request re-reads the user row and rejects on
  any of: row missing, row's `tenant_id` ≠ claim, user `is_active` false, tenant
  `is_active` false, `user.token_epoch` ≠ claim. A token is not a capability.
- **Why:** without those checks a token is valid until it expires, so firing
  someone, deactivating an organisation, or a "log out everywhere" request could
  not take effect until expiry — and the only workaround, a server-side denylist,
  is state this system would then have to keep correct and purge. One indexed
  primary-key read per request buys immediate revocation with no extra
  infrastructure. `token_epoch` is the revocation primitive: bumping it
  invalidates every outstanding token for that user.
- **Trade-off:** one DB read per request (eager-loads the tenant so it is one
  round trip, not two). Accepted — a token is stateless only in the direction
  that hurts security.

### D3-04 — `IdentityDirectory` is the single audited exception to tenant scoping

- **Decision:** `IdentityDirectory` (in `app/db/repositories/users.py`) offers
  tenant-agnostic, **read-only** lookups (`tenant_by_slug`, `user_by_id`).
  Everything else goes through `TenantScopedRepository`. The class docstring
  states the rule and the module docstring explains why.
- **Why:** authentication is inherently pre-tenant — you cannot scope a query to
  a tenant you do not yet know. Rather than weaken `TenantScopedRepository` with
  an `allow_unscoped=True` escape hatch (which every future caller could then
  use silently), the exception is a separate class that cannot be reached by
  accident and cannot write.
- **Trade-off:** two access paths into `users`. Accepted, because the exception
  is now visible in one file, is read-only, and a write method added to it would
  read as a cross-tenant write primitive in review.

### D3-05 — A short-lived access token, and no refresh token yet

- **Decision:** HS256 bearer tokens, 15 minutes by default
  (`access_token_expire_minutes`, bounded 1–120). No refresh token, no cookie
  session. One algorithm is allow-listed at decode time; `alg: none` and any
  other algorithm are rejected, as are wrong-issuer tokens and a refresh-type
  token presented as an access token (`typ` claim).
- **Why:** a refresh-token flow is real state — rotation, reuse detection,
  storage — and doing it badly is worse than not having it. Fifteen minutes plus
  `token_epoch` gives revocation on the next request without that surface. The
  token also carries `tid`, but D3-03 means it is a hint the row must confirm,
  not the authority.
- **Trade-off:** a user idle for 15 minutes logs in again. Acceptable for an
  internal ops tool; a refresh flow is a later milestone, and the `typ` claim is
  already there so it cannot be replayed as an access token when it arrives.

### D3-06 — Every login failure is one indistinguishable 401, and a corrupt hash is a wrong password

- **Decision:** unknown tenant, unknown user, wrong password and deactivated
  account all return the same 401 body after spending the same Argon2id work
  (`spend_password_hash_time` hashes a dummy password when no account matched).
  A hash that cannot be parsed makes `verify_password` return `False`; it does
  **not** raise.
- **Why:** distinct messages or distinct response times turn login into an oracle
  for which addresses and organisations exist. The second half was a real defect
  found while writing the tests: `argon2.exceptions.InvalidHashError` subclasses
  `ValueError`, **not** `Argon2Error`, so catching only `Argon2Error` let a
  corrupted or truncated hash propagate as a 500 that named the affected
  account — the exact leak the module's docstring promised to prevent. Both
  exception types are now caught, in `verify_password` and
  `password_needs_rehash` alike.
- **Trade-off:** a genuinely corrupt hash is invisible in the response. It is
  logged (`login_failed`, reason `bad_password_or_inactive_account`) so operators
  keep the signal the client does not get.

---

## Phase 1 — F4 (event transport)

Resolves D2-04: a real transport now exists, and the relay still does not know
its name.

### D4-01 — Publishing is synchronous and acknowledged, so "returned" means "delivered"

- **Decision:** `KafkaPublisher.publish` calls `producer.send(...)` and then
  blocks on `record.get(timeout=kafka_publish_timeout_seconds)`. The producer is
  built with `acks="all"` and `linger_ms=0`. Exceptions are **not** caught.
- **Why:** the relay's whole failure model rests on one inference — a publish that
  returned normally was delivered, anything that raised was not. A fire-and-forget
  `send` (which is what `kafka-python` gives by default) breaks that inference:
  the broker could be down, the send buffered and dropped, and the row marked
  `published`. That is the log-and-drop failure the specification forbids. `acks="all"`
  means an ack survives a leader crash immediately after the write; `linger_ms=0`
  adds nothing to lose, because the relay already waits per row.
- **Trade-off:** throughput is one in-flight record per relay. At the outbox's
  scale (a daily expiry scan) that is irrelevant, and batching is available later
  by relaxing only `linger_ms` — the ack wait is what must not be relaxed.

### D4-02 — Envelope identity travels in the body *and* the headers

- **Decision:** `event_id`, `tenant_id`, `event_type`, `schema_version` and
  `correlation_id` appear both in the JSON body (`encode_envelope`) and as Kafka
  record headers (`encode_headers`); the partition key stays `partition_key`
  (the contract id). The body is serialised with `sort_keys=True`,
  `separators=(",", ":")` and `default=str`, so the same event always produces
  byte-identical output.
- **Why:** headers are what a router or a filter needs *before* parsing, and a
  consumer that reads only the value must still be able to de-duplicate on
  `event_id` and scope itself to `tenant_id` without a schema-specific parser.
  Deterministic bytes make dedup and the tests stable; `default=str` keeps a UUID
  or datetime inside a payload from raising mid-publish, after the event has
  already been claimed.
- **Trade-off:** the same fact exists twice on the wire. Accepted — they are
  written from one row by one function, and a divergence would be a bug in a
  single place.

### D4-03 — `EVENT_TRANSPORT` defaults to `none`, and nothing forces it to `kafka`

- **Decision:** the relay's transport is selected by `EVENT_TRANSPORT`
  (`none | kafka`) in `build_publisher`. `none` keeps `UnavailablePublisher`, so
  rows retry and then park as `failed`. Selecting `kafka` is **not** required in
  staging/production, and the relay logs `outbox_relay_transport` at start-up.
- **Why:** `docker-compose.yml` runs the API and the relay from one image with
  one environment, and the API never publishes. A production-only rule would
  either force a Kafka dependency into the API's configuration or fail a
  deployment whose relay is intentionally disabled. Refusing to guess is safe
  because the failure is loud and bounded (bounded backoff, then a parked row,
  never a silently dropped event). The start-up log line is the operator's answer
  to "is this box delivering or parking?".
- **Trade-off:** a misconfigured production relay fails events rather than
  failing the deploy. Mitigated by the log line and by the parked rows carrying
  their last error. Flagged as an open item for GATE 2.

### D4-04 — `kafka-python`, imported lazily, with a local mypy override

- **Decision:** `kafka-python==2.2.15` is a runtime dependency, but
  `app/workers/publishers.py` imports it **inside** `create_kafka_producer`. The
  publisher depends on a narrow `Producer` protocol rather than on the library,
  and `pyproject.toml` gives `kafka` and `kafka.*` an `ignore_missing_imports`
  override.
- **Why:** pure Python, so it resolves on the project's CPython 3.14 toolchain —
  checked before it was added, not after. The local import means a deployment
  running `EVENT_TRANSPORT=none`, and every test that never reaches a broker,
  never pays for or needs the library. The `Producer` protocol is what lets
  `test_publishers.py` exercise the real publish path (including the ack wait and
  error propagation) against a fake, with no broker in CI. The mypy override is
  honest rather than convenient: the library ships no `py.typed`, so a strict
  check on it would assert types that do not exist.
- **Trade-off:** a *missing* `kafka-python` install fails at relay start-up (when
  `create_kafka_producer` runs) rather than at module import, so `EVENT_TRANSPORT=kafka`
  in an image built without it is a runtime fault. Covered by the transport log
  line and by `build_publisher`'s test.

### D4-05 — The publish timeout must stay below the outbox lease

- **Decision:** `_validate_consistency` rejects `kafka_publish_timeout_seconds >=
  outbox_lease_seconds`, and rejects an empty `KAFKA_BOOTSTRAP_SERVERS` when the
  transport is `kafka`.
- **Why:** the lease is a fencing token (D2-03). If one publish could outlive it,
  a second relay would reclaim the row and publish it while the first is still
  blocked on the ack — a duplicate that at-least-once delivery is supposed to be
  the *result* of a crash, not a steady state. Both guards are reachable through
  configuration (the default 10 s publish timeout passes the default 120 s lease;
  `KAFKA_PUBLISH_TIMEOUT_SECONDS=120` fails), unlike the Argon2
  memory/parallelism guard that was deleted after F3 because its own `Field`
  bounds made it unreachable — validation that cannot fire reads as protection
  without being any.
- **Trade-off:** a broker slower than 10 s per record fails the publish and
  retries. That is the correct outcome: a broker that slow is broken, and the
  retry is visible in `attempts` on the row.

## Phase 1 — F5 (readiness, operability)

Closes the two gaps F4 left open and made worse: a process nobody could ask
"are you actually able to serve?" and parked rows nothing could un-park.

### D5-01 — Liveness stays dependency-free; readiness probes only what a request needs

- **Decision:** `/health` is unchanged — it reads process state and touches no
  dependency. `/health/ready` runs `check_database` and, **only when
  `EVENT_TRANSPORT != none`**, `check_event_transport`. Both run sequentially in
  the threadpool under `readiness_timeout_seconds`; a failed or timed-out check
  returns 503 with `{"status": "not_ready", "checks": {...}}`.
- **Why:** Docker's `healthcheck` drives `restart: unless-stopped`.

  A liveness probe that fails when MySQL is down restarts a perfectly healthy
  API, which is precisely the outage-amplification the split exists to prevent —
  so `docker-compose.yml` keeps its healthcheck on `/health` and a deliberate
  comment says why (D1-06's cousin: one rule, one place). The event-transport
  check is skipped rather than reported green when no broker is configured:
  `check_event_transport` returns `ok/not_configured` for its own callers, but
  `readiness_report` omits the entry entirely, because a `"database": ok`
  neighbour would make `"event_transport": ok` read as "the broker is healthy"
  when no broker is in use (same reasoning as D4-03).
- **Trade-off:** readiness can take up to `len(checks) * timeout` (2 × 5 s by
  default) on a wedged dependency. Sequential is a choice: there are at most two
  checks and a concurrent gather would make it harder to attribute which one
  hung. Bounded per-check timeouts keep an abandoned Kafka thread from piling up
  one per scrape.

### D5-02 — Readiness reasons are coarse, and the detail goes to the log

- **Decision:** a failing check carries one of exactly three reasons —
  `unavailable`, `timeout`, `not_configured` — never the driver's exception text.
  The full `f"{type(exc).__name__}: {exc}"` is logged through the existing
  redacting logger.
- **Why:** `/health/ready` is unauthenticated and lives outside the versioned
  prefix, so anyone who can reach the port can read it. A raw PyMySQL or Kafka
  error string leaks hostnames, ports, user names and sometimes fragments of a
  connection URL — exactly the class of data `app/core/logging.py` spends effort
  masking. The reason enum is enough to route the alert; the log has the rest.
- **Trade-off:** an operator must correlate by request id / timestamp to learn
  the underlying driver error. Accepted: the logs already carry correlation ids
  (F1) and the body stays useful without them.

### D5-03 — Dead-letter replay is an operator CLI command, not an HTTP endpoint

- **Decision:** `python -m app.cli outbox-replay [--list | --dry-run | --ids N,N]`
  requeues `failed` outbox rows via `replay_dead_lettered`. It resets `attempts`
  to 0, floors `next_attempt_at` to the stored second precision (the D2-06
  lesson), and returns the previous error text so the operator sees what they are
  overriding. It refuses to touch `published` or `pending` rows.
- **Why:** replay is a privileged, rarely-run repair action with no worker
  identity and no lease — giving it an HTTP route would mean inventing an
  authorization scope and a rate-limit story for something an operator does by
  hand after reading a log. A CLI can be run inside the same container/network as
  the relay with the same credentials, needs no new attack surface, and its exit
  codes (`EXIT_OK` / `EXIT_NO_MATCH`) make it scriptable. `--dry-run` and
  `--list` exist because the first question after "a row is parked" is "what is
  it, and did I mean to requeue it?".
- **Trade-off:** replay requires shell access to a host with the app image. For
  this project that is the correct bar — a parked row is an operations event, not
  a customer-facing one. Flagged as an open item for GATE 2: there is still no
  automatic replay, and no alert fires when rows park.

## Phase 1 — F6 (contract ingestion, first real producer)

The outbox was built in F2, exercised by tests and fakes in F4, and made
recoverable in F5 — but until this milestone no *business write* had ever produced
an event. `POST /contracts` is that first producer: one row and one event,
committed together. That pairing is the milestone; extraction and review follow.

### D6-01 — Ingestion is an operator write, and the tenant cannot be in the body

- **Decision:** `POST /{prefix}/contracts` is gated to `admin` and `operator`
  through the existing `require_roles` dependency. `tenant_id` is not a field of
  `ContractCreateRequest`, `ContractRepository` receives it from
  `principal.tenant_id`, and a `tenant_id` a client sends anyway is silently
  ignored (Pydantic's default `extra="ignore"`).
- **Why:** ingestion starts a reminder clock, so it is not a read. A `viewer`
  holding a valid token must not be able to create obligations — the same
  reasoning that puts `POST /auth/users` behind `require_roles(admin)`. The field
  is omitted rather than accepted-and-validated because there is nothing to
  validate: tenant identity has exactly one source (F3, D2-02). A field that
  exists only to be rejected still teaches a client that it is a parameter worth
  probing, so rejecting it with a 422 would be marginally more informative and
  marginally worse.
- **Trade-off:** a client that believes it is sending `tenant_id` gets no
  feedback that it was ignored; its contract is created in its own tenant, which
  is what a correct client wanted anyway. The refusal path is covered by
  `test_a_viewer_may_not_ingest`, which also asserts that nothing was written.

### D6-02 — One topic per aggregate, partitioned by contract id

- **Decision:** every contract event goes to the single topic
  `contract.events`, with `event_type` (`contract.ingested` for now) in the
  envelope, and `partition_key=str(contract.id)`.
- **Why:** a consumer of contract lifecycle facts wants *all* of them, in order,
  for a given contract — ingested, then extracted, then notified. The partition
  key is therefore the aggregate id, and ordering within a contract is Kafka's
  per-partition ordering rather than a consumer-side reorder buffer. A topic per
  event type would scatter one contract's history across N partitions and force
  every consumer to grow a subscription with every new event. The version lives
  in the payload and on the row (`Contract.version`), not in the topic or the
  event name, so a consumer keeps its subscription while the payload grows.
- **Trade-off:** this is a constraint on future work, not a present cost —
  unrelated event families (billing, audit) must *not* be added to
  `contract.events`. The topic is scoped to the contract aggregate; a second
  aggregate gets a second topic.

### D6-03 — Timezone is validated by shape, because the runtime has no tz database

- **Decision:** `timezone` is checked against
  `^[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*$` and rejected when it does
  not match. It is **not** resolved through `zoneinfo.ZoneInfo`.
- **Why:** `ZoneInfo("UTC")` raises `ZoneInfoNotFoundError: No time zone found
  with key UTC` on this host — verified, not assumed. CPython on Windows reads the
  zone database from the `tzdata` package, which is not a declared dependency
  here, so the obvious validator would reject *correct* input. A shape check still
  rejects the empty/blank string, path traversal (`../../etc/passwd`),
  injection-shaped strings and doubled separators — everything that matters
  before the value is consumed. Nothing reads the zone until the deterministic
  quiet-hours check, which is exactly where a real tz database has to arrive;
  validating against a database the image does not ship would be validation that
  cannot fire (the D4-05 lesson).
- **Trade-off:** a syntactically plausible but nonexistent zone (`Mars/Olympus`)
  is accepted at ingest and will fail later, at the quiet-hours computation. That
  is the correct seam: ingestion records what the caller asserted, and the check
  that needs the database runs where the database exists. `tzdata` belongs in the
  change that first needs to *resolve* a zone, not in this one.

### D6-04 — A duplicate ingestion is a 409 that names the existing row

- **Decision:** the content fingerprint is SHA-256 over a fixed, sorted key set of
  the request's content fields, stored in `contracts.sha256` under the existing
  `(tenant_id, sha256)` uniqueness. A repeat POST returns **409** carrying the id
  of the row that already holds that content, and writes neither a second contract
  nor a second outbox event. A concurrent race that slips past the advisory
  lookup is caught by `IntegrityError`, rolled back, and reported as the same 409.
- **Why:** a retried POST did not perform a creation, so `200`/`201` would be a
  lie the client cannot detect, and `500` would be an error it cannot act on.
  Naming the existing id turns a dead end into a redirect: the caller can read what
  it already created. The lookup-then-constraint split is deliberate — the lookup
  produces the useful message, the constraint is what actually decides, and the
  rollback is why a losing race leaves no orphan event (the atomicity
  `enqueue_event` exists for, pinned by
  `test_a_losing_race_leaves_neither_a_row_nor_an_event`).
- **Trade-off:** the fingerprint covers metadata only for now. When PDF upload
  lands, `contracts.sha256` will hold the file's bytes instead — see the ingestion
  route docstring. Both schemes are SHA-256 over different preimages, so mixing
  them still answers correctly for each path (the same document is one row, the
  same metadata is one row), but a contract uploaded as a PDF *and* posted as
  metadata will legitimately appear twice. Closing that means the upload path must
  hash the same canonical metadata; it is noted for the milestone that adds
  upload, not solved here.

## Phase 1 — F7 (field extraction)

### D7-01 — Encryption at rest is a write condition, not a convention

- **Decision:** `extract_contract` takes a `FieldCipher` as a *required*
  argument, and `ExtractedField.raw_value` only ever receives
  `cipher.encrypt(value)`. `build_cipher` raises `MissingEncryptionKeyError` when
  `ENCRYPTION_KEY` is empty, so "there is no plaintext mode" is true by
  construction: no branch in the module stores a value without a key. A key that
  is *present but malformed* is refused at start-up by a `Settings` model
  validator (right alphabet/wrong length included), because a bad key would
  otherwise fail mid-write at the first encrypt instead of at boot. An empty key
  stays legal — the API process never stores field values, so requiring one would
  stop a read-only deployment from starting; `build_cipher` refuses at the point
  of use.
- **Why:** the alternative, "encrypt when a key happens to be configured", makes
  a security property depend on an operator remembering a variable. Fail-closed is
  the only version of this rule that survives a misconfigured deployment.
- **Trade-off:** the operator CLI is now the only writer of extracted fields, and
  it exits `3` without a key (`extract_refused_without_encryption_key`). That is
  intended: a run that cannot store its result safely must not look like it ran.

### D7-02 — No cloud LLM is representable

- **Decision:** `ExtractionBackend = Literal["pattern", "ollama"]`. A hosted API
  is not a member, so `EXTRACTION_BACKEND=openai` is a start-up
  `ValidationError` rather than a code path someone could add by accident;
  `build_extractor` is then total over the literal.
- **Why:** the non-negotiable is "local Ollama only". A rule enforced by a type
  has no runtime branch that can be forgotten or overridden by an environment
  variable. Documenting the rule is not the same as making it unrepresentable.
- **Trade-off:** adding a second local runtime (vLLM, llama.cpp) means editing the
  literal. That is a deliberate one-line cost which forces the change to be
  reviewed as a boundary move rather than appearing as a config value.

### D7-03 — `pattern` is the default; the two backends are alternatives, not layers

- **Decision:** the default backend is the deterministic `PatternExtractor`.
  `EXTRACTION_BACKEND=ollama` replaces it wholesale; results are never merged.
- **Why:** there is no Ollama service in `docker-compose.yml`, so an LLM-only
  default would be a milestone whose behaviour cannot be proven in this
  repository. Merging two backends needs a per-field precedence rule, and
  inventing that rule before either backend has run against a real document would
  be guessing presented as design.
- **Trade-off:** `pattern` reaches 5 of the 7 field types (email, phone,
  start_date, end_date, contract_value). `address` and `dob` are reachable only
  through Ollama, so the default path cannot produce a reviewable address. This is
  a stated limit, recorded as a gap in STATUS.md, not an oversight.

### D7-04 — Extraction cannot approve a field

- **Decision:** `extract_contract` writes only `auto_accepted` (at or above
  `EXTRACTION_REVIEW_THRESHOLD`) or `pending_review`. Rows already in
  `approved`/`rejected`/`unreadable` are skipped, counted as `skipped_reviewed`,
  and never overwritten. Re-extraction updates in place, bumping `version`.
- **Why:** approval is a human act (spec: no low-confidence field reaches the
  notification pipeline). If the worker could write `approved`, the review queue
  would be advisory instead of binding. Skipping rather than overwriting a
  reviewer's row is the same rule seen from the other side: a person's decision
  outranks a re-run, and the skip is reported rather than silent.
- **Trade-off:** a corrected document needs the reviewer to reset the field before
  re-extraction can change it — intentional friction on a path that should be
  rare, and visible in the run's counts.

### D7-05 — The fields API never returns a value

- **Decision:** `GET /api/v1/contracts/{id}/fields` returns field name, status,
  confidence, `version`, provenance (`source_page`/`source_line`), review state
  and a `blocking` count — never `raw_value` or `normalized_value`. A cross-tenant
  id is `404`, not `403`. Viewers may read; only admin/operator may ingest.
- **Why:** a list endpoint is the easiest place to leak PII in bulk, and revealing
  a value belongs to a review *detail* action that has to be auditable rather than
  incidental. `404` avoids confirming that an id exists in another tenant, which a
  `403` would.
- **Trade-off:** the review UI cannot render a value from this route, so the
  audited decrypt path (`decrypt_for_review`) must be exposed before the review
  screen is wired. That was the next milestone (STATUS.md gap 14, closed by F8 —
  see D8-03), not this one.

## Phase 1 — F8 (human review)

### D8-01 — Review writes an `AuditLog`, not an outbox event

- **Decision:** `PATCH /review/{id}` and `POST /review/{id}/reveal` append an
  `AuditLog` row through `AuditLogRepository.record(...)`. Neither publishes to the
  outbox. The event type for a decided field does not exist yet, and no consumer
  would receive it.
- **Why:** the outbox is a *delivery* guarantee for events something downstream
  acts on; an audit row is *evidence* that a person did something. Publishing
  `field.reviewed` before a consumer exists would park rows in the dead-letter
  queue the moment the default `EVENT_TRANSPORT=none` stand-in refused them
  (D4-03), i.e. manufacture gap 5 traffic to record a decision nobody reads. The
  Phase-0 timeline already mocks review decisions as log entries, so this is also
  the shape the UI expects. The event belongs to the slice that first consumes it.
- **Trade-off:** the audit trail is not replayable through Kafka, so a downstream
  system that later wants review history reads the `audit_log` table. When a
  consumer exists, the review write grows an outbox row inside the same
  transaction — the atomicity pattern is already there to copy.

### D8-02 — A correction keeps the extractor's confidence

- **Decision:** `action="edit"` replaces `raw_value` (encrypted) and
  `normalized_value` and sets the status to `approved`; it does **not** touch
  `confidence`, which stays whatever the extractor produced.
- **Why:** the confidence score is a fact about *the extraction*, and the
  extraction did fail — that is why a person intervened. Rewriting it to `1.0`
  would destroy the only measurement of how often the extractor is wrong, which is
  exactly the number needed to tune it (D7-03's stated limit is "the pattern
  backend reaches 5 of 7 fields"; nothing measures that without honest scores).
  Review state carries the correction: `status=approved` plus `reviewed_by` and
  `reviewed_at`.
- **Trade-off:** a row can read `confidence=0.31, status=approved`, which looks
  contradictory in a raw table dump. Anything that treats confidence as a proxy
  for trustworthiness must read `status` first — the review queue already orders
  by confidence but filters on `pending_review`, so it is unaffected.

### D8-03 — Revealing a value is a `POST`

- **Decision:** `POST /api/v1/review/{field_id}/reveal` returns
  `{field_id, field_name, value, version}`. It is not a `GET`, and it is not a
  query parameter on the queue.
- **Why:** it writes a `field.value_revealed` audit row, so it is not a safe
  method. `GET` would be re-issued by a browser refresh, a link prefetcher, an
  HTTP cache or a retry-on-timeout — producing either duplicate audit rows or a
  reveal that was never recorded, and both destroy the property the route exists
  for. A `GET` can also be reached by an `<img src>` from any page the reviewer
  visits, which is a real exfiltration path for the one route in the API that
  returns decrypted PII.
- **Trade-off:** the route cannot be bookmarked or cached, and the frontend has to
  fire it from an explicit user gesture. Accepted: a value reveal should never be
  incidental.

### D8-04 — No value in the queue, and none in the audit trail

- **Decision:** `ReviewItem` extends the F7 value-free `ExtractedFieldSummary`
  (one definition of "a field without its PII", imported from
  `api/v1/contracts.py` rather than re-declared). The audit snapshots hold
  `field_name`, `status`, `version` and `confidence` only. `AuditLog` has no
  column encryption, so a phone number written there would sit in plain text next
  to ciphertext in `extracted_fields`.
- **Why:** a queue listing is the easiest place to leak PII in bulk (the same
  reason as D7-05), and an audit log is read by every operator. Encrypting one
  column while copying the same value into an unencrypted sibling would make the
  F7 encryption theatre.
- **Trade-off:** the audit trail cannot answer "what was the value before the
  correction", only "who changed which field, from what status, at which version".
  Anyone needing the old value must rely on the fact that it was never stored
  anywhere else — which is the point.

### D8-05 — Viewers read review *state*; only admin/operator touch values

- **Decision:** `GET /review/queue` admits `ADMIN`, `OPERATOR` and `VIEWER`.
  `PATCH /review/{id}` and `POST /review/{id}/reveal` admit `ADMIN` and `OPERATOR`
  only, and a refused viewer writes nothing. A cross-tenant field id is `404`, not
  `403`.
- **Why:** the split follows what each route exposes. The queue is review state —
  the same information `GET /contracts/{id}/fields` already gives a viewer. The
  decision is the write that can eventually put a document in front of a customer,
  so it inherits F6's operator-only rule (D6-01) for the same reason; the reveal is
  a PII access event. `404` rather than `403` because the repository returns
  `None` outside the tenant, and confirming that an id exists in another tenant is
  the BOLA leak `test_review.py` pins down (mirrors D7-05).
- **Trade-off:** a read-only auditor cannot resolve the queue; they can see that
  work is pending but not what it says. That is deliberate — an auditor who needs a
  value goes through the same audited reveal as everyone else, which means their
  access is logged too. It does require the deployment to have at least one
  operator, so an installation staffed only by viewers accumulates blocked
  contracts.

### D8-06 — The queue pages by keyset, and the cursor is deliberately unsigned

- **Decision:** `GET /review/queue` accepts `?cursor=` and returns `next_cursor`
  (`app/api/v1/review.py`). The token names the sort key of the last row on the
  page — `(confidence, created_at, field_id)` — and
  `ExtractedFieldRepository.awaiting_review(after=...)` applies it as a keyset
  predicate over exactly the triple the query orders by. It is not an offset. A
  token that does not decode is a `422`, never a silent return to page one. The
  token is URL-safe base64 over versioned JSON, and **unsigned**: it carries no
  secret, no tenant and no HMAC.
- **Why:** the queue is *written to while it is read*. A reviewer deciding a field
  removes it from the predicate, so every later row shifts up by one and an
  `OFFSET` skips an item nobody ever saw — the failure mode is silent, and it is
  the normal case rather than a race. Re-reading the sort key is stable under
  those writes, and stable under insertion too, which is why it beats a
  "snapshot then page" scheme. The predicate is three `OR` terms rather than a
  row-value comparison `(a,b,c) > (?,?,?)` because row-constructor support differs
  between SQLite and MySQL versions and the two must read identically; the terms
  mirror the `ORDER BY` term for term, because a predicate that disagrees with the
  sort order silently drops or repeats rows *at the boundary*, which is the one
  place a paging bug hides. Whether a next page exists is answered by fetching
  `limit + 1`, not by a second `COUNT()`: a count over a moving set is a number
  from a different instant than the page, so it can promise a page that is not
  there. Signing the cursor would imply it grants something; forging one can only
  move within your own queue, because the tenant predicate is applied by the query
  regardless of what the token says — `test_review.py` pins that a *forged
  foreign-tenant cursor* returns `[]`, i.e. it proves the query-level predicate
  rather than cursor integrity, which is the property that actually matters.
- **Trade-off:** three real limits. `oldest_minutes` is a max over the rows
  returned, so on page two it no longer describes the queue's oldest item — a
  client displaying "oldest waiting" must read page one (stated in the response
  docstring, because it is a trap otherwise). There is no total count and no page
  number, by construction. And the cursor is not a capability, so it must never be
  used to authorise anything: it is a position, and the only thing that protects
  the row behind it is the tenant scope on the query.
- **Found while building it — a `NUMERIC(4, 3)` defect, not a bad test.**
  `confidence` is declared at scale 3. MySQL enforces that; SQLite stores the float
  it is handed. `0.1 * 3` is `0.30000000000000004`, so it was stored in full and
  read back rounded, and the keyset term `confidence == cursor.confidence` then
  matched a row whose stored value differed from its read value — page two
  re-served the last row of page one. The paging test caught it, and the fix is
  `CONFIDENCE_SCALE` + a `@validates("confidence")` quantiser on `ExtractedField`
  (`app/db/models/contract.py`), which makes the declared scale true on both
  dialects for the same reason `truncate_to_second` floors timestamps (D2-06).
  Precision was never read by any policy, so this narrows no behaviour; on MySQL it
  should be a no-op, which is what the unrun MySQL suite would confirm.

## Phase 1 — F9 (agent channel decision)

### D9-01 — Consent is a column set on `Contract`, and one `PUT` writes it

- **Decision:** `do_not_contact`, `consent_email`, `consent_sms`, `consent_voice`
  and `consent_updated_at` are columns on `contracts` (migration `0002`), not a
  `consent` side table. All three flags default to `false` in the model *and* as
  the migration's `server_default`. `PUT /api/v1/contracts/{id}/consent` is the
  only route that writes them: a full-state replacement, version-checked, audited
  through `AuditLogRepository`, and a no-op when the body matches what is stored.
- **Why:** the column set is closed — `Channel` has three members — so a boolean
  per channel is queryable and cannot be typo'd into a channel that does not
  exist, whereas a side table buys a join plus a uniqueness constraint to become
  equally safe without ever being easier to read. Fail-closed defaults mean a
  contract that was ingested and never consented to cannot be contacted by any
  route; backfilling `true` in `0002` would have granted consent nobody gave.
  `PUT` rather than `PATCH` because consent is a *state*, not a stream of deltas:
  a partial update leaves a caller unable to express "this channel is withdrawn"
  without knowing the previous value, and a stale body can silently resurrect a
  grant. `do_not_contact` is its own column rather than a fourth channel flag
  because it is a veto, not a route — it has to be able to overrule a mistaken
  grant, which is what `may_contact` and the selector's gate order are for.
- **Trade-off:** every contract that existed before `0002` is un-contactable until
  an operator records consent, so the migration carries a one-off data task rather
  than a silent policy change. The version check runs **before** the no-op
  short-circuit, so a caller holding a stale version gets a `409` even when its
  body would have changed nothing — the alternative reports success against a
  snapshot the caller no longer holds, which is precisely the lost update the
  version column exists to catch.

### D9-02 — Channel selection is deterministic, not model-backed

- **Decision:** `app.workers.decision` picks the channel by applying the fixed
  `CHANNEL_PRIORITY` (email, then SMS, then voice) to
  `contract.consent_channels` intersected with the channels that have a usable
  value. No prompt, no model call, no network. The stored row records
  `llm_model="deterministic"` and `prompt_version="policy.channel.1"`.
- **Why:** the same reason D7-03 chose the pattern extractor — no live Ollama has
  ever answered in this repository, so a selector that depended on one could not
  be tested here, and "the channel is whatever the model said" is not a rule an
  operator can predict or a reviewer can check. A second, sharper reason applies
  to the provenance columns: `AgentDecision` exists to record *what produced this
  recommendation*, so writing `qwen2:7b` for a decision no model made would be a
  fabricated row in the table whose whole purpose is provenance. Priority order is
  fixed data rather than a score because a score invites tuning before anything
  measures the outcome.
- **Trade-off:** the policy cannot weigh context a model could — a customer who
  has never answered email still gets email first. Changing the order is a code
  change plus a `CHANNEL_POLICY_VERSION` bump, not a config value; that is
  deliberate, because the priority by which a customer is contacted is a product
  decision rather than a deployment knob. A model-backed selector can genuinely be
  unsure, and it is the slice that gets to set `needs_review` (D9-05).

### D9-03 — Quiet hours belong to dispatch, not to channel selection

- **Decision:** `select_channel` reads consent and opt-out and nothing about time.
  `Contract.timezone` is not consulted. The quiet-hours window is checked
  immediately before a message is sent, in the dispatch slice.
- **Why:** the two are different kinds of input. Consent decides *whether* a
  channel may be used and is stable for days; quiet hours decide *when* a message
  may leave and are stale within hours. A decision made at 14:00 that will be sent
  at 09:00 the next morning would have been validated against the wrong instant,
  so checking the clock during selection produces a row that looks compliant and
  is not. It is also unimplementable here as things stand: D6-03 kept
  `Contract.timezone` a regex-validated string with no tz database at runtime, so
  "is it 21:30 in Asia/Dhaka" has no code path yet.
- **Trade-off:** a decision may name a channel that must not be used at the moment
  it is dispatched, so the rule has to be enforced by the sender. Until that slice
  exists, nothing enforces quiet hours at all — recorded as a gap in STATUS.md
  rather than implied to be covered by the consent gate.

### D9-04 — Every refusal is a `409` with a stable `code`, and writes nothing

- **Decision:** `CONTRACT_CLOSED`, `PIPELINE_BLOCKED`, `CONTACT_REFUSED`,
  `NO_CONSENT` and `NO_CONTACT_VALUE` all return `409` from
  `POST /agent/decisions`, distinguished by `detail.code`. A refusal creates no
  `AgentDecision` row and no `AuditLog` row, and is logged at **info** with the
  contract id and the reason.
- **Why:** these are not five different HTTP semantics, they are one — "this
  contract cannot be decided on right now" — so inventing a status code per reason
  would encode policy in the transport. The `code` is what a caller can act on, and
  the Phase-0 UI already branches on `PIPELINE_BLOCKED`, so matching that vocabulary
  keeps the eventual screen a straight port. No row is written because an
  `AgentDecision` is an *assertion that a channel was chosen*; absence is the
  record of a refusal, and a row with a null channel would make every
  `for_contract` reader handle a shape that means "nothing happened". Info rather
  than warning because every closed contract and every opted-out customer takes
  this path — logging it as a fault would train operators to ignore the log.
- **Trade-off:** there is no queryable history of refusals, so "how often are
  contracts blocked, and on what" is answerable only from logs. A slice that wants
  that as data needs its own table; the alternative — overloading
  `AgentDecision` — would make the decision history ambiguous for every reader.

### D9-05 — `needs_review` is always `false`; override and flag-review wait (gap 19)

- **Decision:** `build_decision` sets `needs_review=False` unconditionally and
  stores no overridden/flagged state. Re-running the policy for a contract appends
  a second `AgentDecision` rather than editing the first. There is no override or
  flag-review route in this slice.
- **Why:** a deterministic selector cannot be *uncertain* — it either finds a
  permitted channel or raises. Setting the flag to mean "a model would have had
  more to say" would make it permanently `true` and therefore carry no information,
  while a reader would reasonably treat `true` as "a human should look at this".
  Override and flag-review are operator *edits to an existing decision*, and
  `AgentDecision` has no `version` column, so an optimistic-locked edit has nothing
  to lock against; two operators resolving the same flagged decision would silently
  overwrite each other. That is a migration plus a UI, so it belongs to the slice
  that needs it rather than to this one.
- **Trade-off:** the demo screens' re-run, override and flag-review affordances are
  only partly met: re-run works by posting again (appending), the other two do not
  exist. Adding a version column later means backfilling `1` on an append-only
  table, which is cheap precisely because nothing edits these rows today.

## Phase 1 — F10 (notification dispatch)

### D10-01 — `tzdata` becomes a runtime dependency, and quiet hours are the customer's clock

- **Decision:** `backend/requirements.txt` pins `tzdata==2026.6` as a **runtime**
  dependency. Quiet hours are evaluated by converting *now* into
  `Contract.timezone` with `zoneinfo` and comparing the **local wall-clock hour**
  against the half-open window `[quiet_hours_start, quiet_hours_end)`, which wraps
  midnight when `start > end`. A zone that cannot be resolved refuses the dispatch
  (`UNKNOWN_TIMEZONE`) instead of falling back to UTC.
- **Why:** the previous slice deferred this step (D9-03) and cited two blockers:
  a window check belongs immediately before a send, and `Contract.timezone` had no
  tz database behind it. The second was verified, not assumed — on this host
  `ZoneInfo("Europe/London")` raised `ZoneInfoNotFoundError`, because no `tzdata`
  package and no system zone files exist on Windows. Installing `tzdata` made real
  DST behaviour observable (`21:00Z` → `21:00` London in winter, `22:00+01:00` in
  summer, `03:00+06:00` Dhaka), so the caveat is retired rather than repeated.
  Wall clock, not UTC, because "do not call at 03:00" is a statement about the
  customer's clock; evaluating it in server time would be wrong by up to fourteen
  hours. Refusing an unresolved zone rather than assuming UTC is the same
  fail-closed argument as the consent flags: `UTC` is a *guess* that can authorise
  a 03:00 call, while a refusal costs one retry and is recoverable.
- **Trade-off:** the deployment now ships tz data, and it is pinned, so a customer's
  quiet window cannot move under a silent tz refresh. Two sub-hours offsets that
  the IANA database changes at short notice (as some jurisdictions have) require a
  dependency bump rather than a config change. The window is also global rather
  than per tenant: one deployment, one `[20:00, 08:00)`, so two customers with
  genuinely different expectations cannot both be expressed.

### D10-02 — Quiet hours **defer** the row; they do not refuse it

- **Decision:** when a dispatch reaches the quiet-hours check, the worker moves
  `Notification.scheduled_at` to the next window opening and returns
  `DispatchOutcome(sent=False, deferred_until=…)`. The route answers `200`, the row
  keeps `status=pending` and its slot, and the transport is never reached. A future
  `scheduled_at` is a separate, earlier refusal (`NOT_YET_DUE`) that changes nothing.
- **Why:** these are two different facts and a caller acts on them differently.
  "Not yet" is the normal, expected outcome of a policy the customer benefits from —
  it needs no operator, and a `409` would make a compliant system look broken for
  the eight hours a day it is not allowed to send. "Refused" means a human has to do
  something. Collapsing them into one status would also lose the information the
  caller needs to reschedule, which is exactly `deferred_until`. Deferral happens
  before the transport is consulted, so quiet hours cannot be bypassed by a
  provider that would have accepted the message.
- **Trade-off:** nothing loops the deferred row. ~~There is no scheduler in this repo
  (the outbox relay polls events, not notifications), so a deferred row is resent
  only when something dispatches it again~~ — **superseded by `4e7dd5c`**
  (`STATUS.md`, "A scheduler for due and deferred notifications"): a
  `NotificationScheduler` now claims `pending` rows whose `scheduled_at` is past, so
  the deferral this decision writes is what makes the row resume. The decision
  itself is unchanged and still correct; only its consequence was temporary. What
  survives of the trade-off is narrower: a row parked `failed` or `suppressed` is
  still only moved by an operator, and nothing alerts when this happens — the same
  "operator-initiated" shape as DLQ replay (D5-03).

### D10-03 — Consent is re-read at send time, never trusted from the decision

- **Decision:** `dispatch_notification` calls `assert_may_contact` again against the
  **contract as it is now**, and `build_notification` calls the same function. One
  definition, two call sites. A decision row's channel is used only to name the
  route; whether that route is still permitted is asked fresh.
- **Why:** an `AgentDecision` is append-only history. A decision made yesterday
  records that a channel *was* permitted then; a customer who telephoned this morning
  and asked not to be contacted has changed the contract row and nothing else. A
  sender that trusted the decision would keep emailing someone who opted out, and the
  failure would be invisible in the decision table — the record would still look
  correct. Two call sites sharing one gate is the only structure that guarantees the
  second call cannot be weaker than the first.
- **Trade-off:** a send can now fail on grounds the decision never mentioned, so the
  refusal vocabulary is wider than F9's (`CHANNEL_NOT_CONSENTED` is distinct from
  `NO_CONSENT`, because "withdrew one channel" and "never consented to anything" are
  different operator tasks). The tests pin the gate *order*, since a contract with
  several problems is told about exactly one of them.

### D10-04 — `NotificationTransport` has exactly one member: `none`

> **Superseded in part by D10-09.** `smtp` is now a second member, added in the slice
> that implemented it, so the rule this entry states is still the rule that was
> followed; it is the *inventory* below that has moved. `sent: true` is now reachable
> through the API **when `NOTIFICATION_TRANSPORT=smtp`** and the relay accepts the
> message, and unreachable in the shipped default (`none`). Read the trade-off
> paragraph here as the state at the F10 commit.

- **Decision:** `notification_transport: Literal["none"] = "none"`, and
  `build_sender` returns `UnavailableSender` unconditionally. That stand-in reports
  `available = False`, and a dispatch against it raises `SENDER_UNAVAILABLE`, having
  already recorded the attempt on the row. There is no `smtp`/`sms`/`voice` member.
- **Why:** the same choice `EventPublisher` (D4-03) and `DocumentExtractor` (D7-03)
  already made, for the same reason: a literal naming a transport that does not exist
  is a lie a config file can tell. A no-op sender would be worse than a refusing one
  — it would let the state machine reach `sent` with nothing delivered, which is
  precisely the row that looks like contact happened. The seam is a `Protocol`
  (`MessageSender`) plus a `build_sender(settings)` factory, so a real adapter is an
  addition, not a rewrite.
- **Trade-off:** **`sent: true` is unreachable through HTTP in this build.** Every
  route-level dispatch is either a `409 SENDER_UNAVAILABLE` or a quiet-hours
  deferral, and the send path is only exercised by calling `dispatch_notification`
  with an injected fake. So "F10 sends notifications" is false; what F10 delivers is
  a writer, an idempotent slot, a state machine and an enforced contact policy.

### D10-05 — No `Notification.version` lock: gap 21, not a migration mid-slice

- **Decision:** `Notification` stays without a `version` column and without
  `__mapper_args__["version_id_col"]`. Concurrent dispatches of the same pending row
  are not serialised. The mitigation shipped instead is the unique
  `idempotency_key` — derived from `(contract_id, template, scheduled_date)` only,
  never from a timestamp — which is passed to the transport so a real provider can
  deduplicate.
- **Why:** every other mutable row here (`Contract`, `ExtractedField`, `Escalation`)
  is optimistic-locked, so this is a known gap rather than an oversight, and it is
  the right one to record: the idempotency key is the *correct* dedupe primitive for
  a send (a provider dedupes on it; a version column would only make one of two
  racers lose), so adding a lock would not close the double-send hole on its own.
  Adding `version` is also a schema change plus a backfill, and the slice that needs
  it (a dispatcher loop, or an operator "resend") does not exist yet.
- **Trade-off:** two simultaneous `POST /notifications/{id}/dispatch` calls can each
  reach the transport and each increment `attempts`, so a provider without idempotency
  support would double-send. Recorded as gap 21, alongside the plaintext
  `ExtractedField.normalized_value` (**gap 20**) that `_recipient_for` reads — the
  recipient is taken from an unencrypted column while `raw_value` beside it is
  Fernet-ciphertext, which is a PII-at-rest inconsistency this slice inherits and
  does not fix. **Superseded in part by `0f5da36`:** the `version` column and the
  `FOR UPDATE` claim *were* added, in the slice that needed them, so the "not a
  migration mid-slice" reasoning expired with F10 and the gap it recorded is closed.
  The idempotency-key argument above stands on its own and was kept — the key is
  still what a real provider dedupes on, and the lock is the second mechanism, not a
  replacement for it. Two details in this entry are now stale: `_recipient_for`
  became `recipient_for` in `app/workers/dispatch.py` (`4e7dd5c`), and the
  "dispatcher loop does not exist yet" clause was true until that same commit.
  Gap 20 — the plaintext `normalized_value` — is **still open and untouched**.

### D10-06 — No template table; the transport renders the body

- **Decision:** `Notification.template` stores the milestone name (`T-60` / `T-30` /
  `T-14`) and nothing else. There is no `templates` table, no rendering step, and no
  stored body in this build. Composing the message is the transport adapter's job.
- **Why:** a template table is a content-management feature, and content decisions
  (tone, locale, legal wording) belong to the slice that first sends something real.
  Building it now would produce a schema whose shape is guesswork, and the demo's
  fixture bodies are not an approved source of customer-facing copy. The milestone
  name is enough for a transport to select from its own configuration, and it is
  already one of the `idempotency_key` hash inputs (D10-05).
- **Trade-off:** a real adapter cannot be written without deciding where the body
  comes from — it will need config-supplied templates or a new migration. This is
  the same "decision, not a patch" shape as D10-04: the seam exists, the content
  does not. **The reference is live in code** (`app/api/v1/notifications.py` docstring
  names D10-06); this entry exists to keep that reference resolvable.

### D10-07 — The scheduler claims unscoped, and enforces tenancy in the worker

- **Decision:** `NotificationRepository.claim_next_due` is **not** tenant-scoped —
  `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` over `status = pending AND
  (scheduled_at IS NULL OR scheduled_at <= now)`, globally. It is the second
  unscoped read in the repositories, following the precedent set by
  `OutboxRepository.claim_due`. The tenant check moves
  into the worker, where it is **not** an explicit id comparison: the contract is
  read through `ContractRepository(session, row.tenant_id)`, so a contract belonging
  to another tenant is not found, and the row is parked `TENANT_MISMATCH`.
- **Why:** `TenantScopedRepository` derives its predicate from an authenticated
  principal, and a background poller has no principal — there is no tenant to scope
  by until a row has been claimed and its contract resolved. The alternatives were
  worse: a `tenant_id` column duplicated onto `Notification` (denormalisation to
  satisfy a query shape), or iterating tenants and claiming once per tenant (N
  queries per tick, and a hot tenant starves the rest). Moving the *check* rather
  than the *predicate* keeps the isolation guarantee without inventing either.
- **Trade-off:** the invariant "tenant scoping lives in the repository" now has one
  documented exception, and the guard that replaces it is application code that a
  future refactor could drop. It is the first thing the scheduler's test file covers
  for that reason. Also accepted: `SKIP LOCKED` is a no-op on SQLite, so on the
  suite's database two pollers could claim the same row. That was recorded as gap 23
  and **closed against MySQL in D10-08** — the clause is still a no-op on SQLite, but
  it is no longer unverified on the dialect where it does something.
- **Companion decisions in the same slice, recorded here rather than as three
  entries:** `SENDER_UNAVAILABLE` is **not** suppressible (the shipped transport is
  `none`, so suppressing it would park every pending row in a human-only state for a
  one-env-var fix); a **failed** send stops the drain (otherwise the loop re-claims
  the same still-due row and burns the whole retry budget in one tick); and
  `notification_retry_max`'s floor moved `ge=0` → `ge=1` (at `0` the policy parks
  every row before the transport is ever consulted, so sending was impossible by
  configuration).

### D10-08 — The dispatch lock is proven by a timeout, not by waiting

- **Decision:** the two `FOR UPDATE` claims that stop a double send —
  `NotificationRepository.claim_for_dispatch` (the dispatch route, `0f5da36`) and
  `claim_next_due` (the scheduler, `4e7dd5c`) — are verified by two-connection
  integration tests in `backend/tests/test_mysql_integration.py`, and the pair is
  made to **raise** rather than to wait. Session `lock_wait_timeout` is shortened to
  one second, and MySQL **errno 1205** is the *positive* assertion: the test passes
  because a lock-wait timeout was raised. Gap 23 is closed on this evidence, not on a
  re-reading of the SQL.
- **Why a raised error and not a timing assertion:** "blocked" and "finished quickly"
  are the same observation if you only measure wall-clock. A test that asserts
  "B waited ≥ Nms" is flaky on a loaded machine and silently passes if the lock
  degrades into a no-op under a fast commit. Forcing the timeout turns the two
  hypotheses into distinguishable outcomes: a correctly-locking claim raises 1205
  inside the bound, a broken one returns a row promptly and the test fails on
  "DID NOT RAISE". The regression message names the defect rather than a duration.
- **Why the second test asserts row identity, not a call count:** `SKIP LOCKED` means
  *the other row is handed over*. Asserting that only one row was claimed would pass
  even if both workers grabbed the same one and one of them lost a race later; the
  test asserts worker B receives the *other* due row, which is only true if A's row
  was actually skipped.
- **Why only MySQL:** `with_for_update()` is a silent no-op on SQLite, and every
  session in `test_notifications.py` shares one in-memory connection (`StaticPool`),
  so a competing commit *cannot* land behind an in-flight request there. The offline
  suite is not a weaker proof of this property — it is no proof at all, which is why
  "the lock is untested" was recorded as a gap instead of as a passing table row.
- **Trade-off:** these tests **skip, not fail**, when MySQL is unreachable, so the
  default suite stays green on a machine with no Docker daemon and the guarantee
  remains unverified there. A skip is not a pass, and CI without a MySQL service
  reproduces exactly the condition gap 23 described. The gap is closed by a test that
  *must be run*, and the status record has to carry the run rather than the command.
- **Both claims were falsified before being trusted:** deleting `.with_for_update()`
  makes the dispatch test fail at `test_mysql_integration.py:410` ("DID NOT RAISE"),
  and deleting `skip_locked=True` makes the scheduler test report that the same row
  went to two schedulers. Restoring each and re-running returned green.
- **Corollary, measured rather than assumed:** the `CONFIDENCE_SCALE` quantiser from
  D8-06 is a **no-op** on MySQL. A raw-SQL insert of `0.1 * 3` — bypassing the
  `@validates` hook — reads back `Decimal('0.300')` because `NUMERIC(4, 3)` is
  enforced there rather than merely declared. That quantiser is load-bearing only on
  the dialect that ignores the scale, which is the dialect the offline suite runs.

### D10-09 — `smtp` is a real transport; the envelope is derived, not invented

- **Decision:** `NotificationTransport` gains a second member, `smtp`, and
  `build_sender` returns an `SmtpSender` for it. The adapter lives in the new
  `app/workers/senders.py`, beside `publishers.py` and for the same reason: the
  protocol (`MessageSender`) and the factory stay with the policy in `dispatch`, and
  only the transport imports `smtplib`. `dispatch.py` still never imports `smtplib`,
  which is what lets the consent and quiet-hours tests run with no transport at all.
  `smtp` was added to the literal **after** the adapter existed, which is D10-04's
  own rule — a literal naming a transport that does not exist is a lie a config file
  can tell — kept rather than waived. This is the "addition, not a rewrite" D10-04
  predicted.
- **Why SMTP and not an email API or a portal stub:** `smtplib` is stdlib, so the
  only new runtime dependency is none at all; the credentials fit the existing
  fail-fast `Settings` style; and `DATA` being answered with a 2xx is a genuine
  provider-side acceptance, which is what makes `sent: true` an observation instead of
  a claim. A portal stub could not honestly set `sent` at all — it would have to mean
  "a row exists", which is the state the row was already in.
- **The Message-ID is derived from the idempotency key, not from
  `email.utils.make_msgid`.** Delivery here is at-least-once: the row commits after
  `send` returns, so a process dying in between re-sends on the next tick. A random
  Message-ID would make the second attempt a *different message* to every relay that
  de-duplicates on that header, so `make_msgid` would manufacture the one duplicate
  this transport is able to make collapsible. The id is
  `sha256(idempotency_key)@<sender domain>`, so both attempts are byte-identical in
  the fields a provider can collapse them by. The `X-Idempotency-Key` header travels
  alongside it; it is non-standard on purpose and free to add, because an unknown
  `X-` header is ignored.
- **Why one conversation per message:** the scheduler sends one row per tick, so a
  pooled connection would mostly hold sockets open against a relay that had already
  timed them out, and a broken connection could then affect a message it never
  touched. `create_smtp_sender` therefore takes a *factory*. Building the sender
  opens no socket, which is asserted: a sender that dialled on construction would
  fail the API process when the relay was merely restarting.
- **The socket timeout is a correctness bound, not tuning.** `SMTP_TIMEOUT_SECONDS`
  reaches `smtplib` as the socket timeout, so connect, TLS, auth and `DATA` all share
  one ceiling. The send happens *inside* the transaction holding the row's
  `FOR UPDATE` lock (`claim_next_due` → `_dispatch_locked`, D10-08), so an unbounded
  send would hold a locked row — and the serial tick loop behind it — open for as long
  as a provider cared to stay silent. That is why the value is validated at start-up
  (`gt=0, le=60`) rather than defaulted and forgotten.
- **Relay refusals are redacted before they are stored.** A refusal routinely quotes
  the failing address (`550 5.1.1 <someone@example.com>: recipient rejected`), and
  that text goes to `Notification.last_error`, which `GET /notifications` renders to
  any viewer — including one who may not see contact values anywhere else. The detail
  is passed through `redact_text` before it is kept, while the SMTP status code and
  the diagnostic survive it. An error column must not become a second place a contact
  value leaks out of.
- **Trade-off, stated rather than buried:** the transport implements **email only**.
  `sms` and `voice` are *refused*, not substituted — delivering a voice reminder by
  e-mail would be a different promise kept than the one the decision recorded. The
  refusal is a `SenderError`, so the row spends its retry budget and parks `failed`
  where an operator sees it. The body is also still a placeholder naming the contract
  reference; it carries no extracted value, since the counterparty and the dates stay
  in the database. Real rendering waits on a template table, as D10-06 already
  recorded.
- **What this does not prove:** the tests drive a fake client, so what is pinned is
  the envelope, the call order, the timeout argument and the redaction — not a socket.
  No test in this repo has spoken SMTP to a real relay. The first deployment does that
  for the first time.
