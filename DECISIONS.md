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
