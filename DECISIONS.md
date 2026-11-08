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

## D1-10 — Line endings are pinned in `.gitattributes`, because `core.autocrlf` outranks the hook

- **Decision:** the repo carries a `.gitattributes` whose only rule is
  `* text=auto eol=lf` — one line, plus the comment explaining why. Git's own
  binary detection is left on rather than enumerated per extension, because the
  tree currently holds no binary files at all and a list would rot.
- **Why:** `mixed-line-ending --fix=lf` normalizes the worktree *after* checkout,
  but `core.autocrlf` decides what the checkout writes in the first place, and it
  is `true` on a default Windows install. The hook therefore rewrites files git
  has just written, and the two settings only agree once the hook has run at
  least once. The tree looked LF while the checkout contract said CRLF.
- **Why that is a gate failure and not cosmetics:** `.husky/pre-commit` and
  `.husky/commit-msg` are shell scripts. A fresh Windows checkout hands `sh` a
  shebang reading `#!/bin/sh\r`, which it cannot resolve, so the hook dies with
  "bad interpreter" and the commit gate fails *open* and reports nothing. This
  project has already shipped one gate that was silently not running (Gap 13),
  so the checkout is pinned rather than left to chance.
- **Verified, not assumed:** `git checkout-index -a` into a scratch prefix gave
  `.husky/pre-commit` 14 CR bytes without the file and 0 with it, while
  `git add --renormalize .` staged nothing — the index was already LF, so this
  adds a rule without rewriting a single byte of content.
- **Limit:** `eol=lf` governs checkouts. It does not retro-fit a clone that
  already holds CRLF files; those stay CRLF until the `mixed-line-ending` hook
  rewrites them on its next run. And the rule can only constrain what git
  writes — an editor configured for CRLF can still reintroduce them, which is
  why the hook stays as the second line of defence.

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

> **Superseded in part by D9-06.** The two routes this entry deferred now exist.
> One premise below — "`AgentDecision` has no `version` column" — was retired by
> migration `0003` (`0f5da36`). The rest is kept as written, because the argument
> that a deterministic selector cannot be *uncertain* is still why the policy
> itself never sets the flag; D9-06 only adds who may.

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

### D9-06 — Operator corrections edit the newest decision; an override chooses *among* permitted channels

- **Decision:** two routes, both addressed by **contract** id rather than decision
  id and both requiring an operator role:
  `POST /agent/decisions/{contract_id}/override` (body: `channel`, `version`,
  optional `reason`) and `POST /agent/decisions/{contract_id}/flag-review`
  (body: `version`, optional `reason`). Each edits the newest `AgentDecision` for
  that contract in place under the `version` column from `0003`, returning `409`
  with the current row when the version is stale and `409 NO_DECISION` when the
  contract has no decision yet. An override re-runs `permitted_channels` against
  the contract's **current** state and refuses
  (`CHANNEL_NOT_PERMITTED`) any channel outside it. `original_channel` is written
  once, on the first override, from the value the policy chose, and never
  rewritten; `overridden_by` holds the acting user's id.
- **Why:** `0003` made the row lockable but left the correction unwritable, so the
  only supported fix for a wrong decision was to append another one — which reads
  as "the policy changed its mind" and loses the fact that a person disagreed.
  Editing under an optimistic lock records that disagreement where a reader will
  look. Re-deriving the permitted set at override time rather than trusting the
  decision's own snapshot is the point of the least-privilege rule: an override
  *redirects* a choice the rules already allow, and must never *create*
  permission — a contract that revoked consent after the decision was taken
  refuses the override rather than quietly honouring a stale allowance. Flagging
  deliberately skips the contact gates, because taking a recommendation out of
  the send path should never be blocked by policy.
- **Trade-off:** this makes `AgentDecision` no longer append-only, so its history
  is "appends plus two audited edits" rather than a pure log; `rerun_count` is
  stamped at append time and is therefore a snapshot of history at that moment,
  not a live aggregate. Every correction also needs an `AuditLog` row to be
  reconstructable, which is why the routes write one and the policy does not.

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
  Gap 20 — the plaintext `normalized_value` — was **still open and untouched** when
  this entry was signed, and has since been closed by **D10-11**, which seals the
  column on both write paths and adds the `reseal-fields` repair path. Read the
  sentence above as the state at the F10 commit.

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
- **Proven against a socket, not only against a double.** `test_senders.py` drives a
  fake client and pins the envelope, the call order, the timeout argument and the
  redaction. That is not the same as completing a conversation, so
  `test_smtp_socket.py` runs a loopback SMTP server built from `socket` and
  `threading` — no container and no new dependency, because the claim is that the
  client speaks the protocol, not that a particular mail server is installed. Five
  things the fake client could not show, and this does:
  - The body goes out only after the relay answered `354`, and the run ends with a
    real `QUIT`: `EHLO → MAIL → RCPT → DATA → QUIT`.
  - A refused recipient arrives as `smtplib.SMTPRecipientsRefused`, which is a
    *different* exception class from the `SMTPResponseException` the unit tests
    raise. Its text is the relay's own words with `<address>` quoted inside, so the
    redaction is now proven against the exception a relay actually produces: the
    stored string keeps `550` and `recipient address rejected` and loses the address.
  - A re-send is **byte-identical on the wire**, not merely equal in Message-ID.
  - The timeout is a real ceiling. A relay that accepts and then says nothing for
    30 s fails the send in well under 2 s, so a silent provider cannot hold a locked
    row open. Falsified: dropping the `timeout=` argument makes that test take the
    relay's full 30 s and fail.
  - `STARTTLS` is a real handshake over a real certificate, and `AUTH PLAIN` is
    proven to cross only the encrypted socket — the loopback relay refuses
    authentication in the clear with a `530`, so the test cannot pass by the client
    merely *calling* `starttls()` first. Falsified: removing the `starttls()` call
    makes the conversation fail with no AUTH advertised.
- **What this still does not prove,** stated rather than buried. The relay is ours,
  so what is proven is that the client speaks SMTP to something that answers SMTP,
  not that it is compatible with a particular provider's quirks. The certificate is
  self-signed and `smtplib` negotiates TLS through an unverified context, so the
  handshake and the ordering are proven but authentication of the relay is not —
  which matches the code: `SMTP.starttls()` is called with no context, so peer
  verification is off by default and a man in the middle between us and a real relay
  would not be caught. Tightening that is a code change (`starttls(context=...)` with
  a CA bundle) and therefore a decision, not a test.

> **Superseded in part by D10-10.** That last paragraph described the code as it
> stood at `6f4cb0b`. `starttls()` now receives a verifying context; the residue it
> named — that the socket test proves a handshake but not peer authentication — is
> what D10-10 closes and re-measures. The rest of this entry still holds.

### D10-10 — TLS verifies the relay; `None` means the system trust store, never "no checks"

- **Decision:** `SmtpSender` builds a verifying `ssl.SSLContext` and passes it to
  `starttls(context=...)`. The context comes from `build_ssl_context(settings)`, which
  returns `ssl.create_default_context(cafile=SMTP_CA_BUNDLE)` when a bundle is
  configured and `ssl.create_default_context()` otherwise. `None` — the constructor
  default, and the only value a caller who does not care will pass — resolves to the
  *system trust store*, so there is no state in which this transport falls back to an
  unchecked handshake. `SMTP_CA_BUNDLE` **replaces** the trust store rather than
  disabling verification.
- **The defect this closes was measured, not assumed.** `SMTP.starttls` is declared
  `starttls(self, *, context=None)`, and `smtplib` turns `None` into
  `ssl._create_stdlib_context()`, whose `verify_mode` is `CERT_NONE` and whose
  `check_hostname` is `False`. So the pre-D10-10 code encrypted the session to
  *whatever answered the socket* and then handed it `SMTP_USERNAME`/`SMTP_PASSWORD`.
  The connection was confidential and the peer was unauthenticated — confidential to
  an unknown party is the shape of failure that reads as success in a log.
- **Why a bundle and not a "verify" switch:** a boolean would have offered
  `verify=False` as a supported configuration, which is the same defect with a config
  key in front of it. Naming a CA bundle is the only way to say "trust this instead"
  without also saying "trust nothing". The socket suite depends on this: its
  certificate is self-signed, so a verifying client rejects the relay the test needs
  unless the test hands over the certificate it generated.
- **Both misconfigurations are boot errors, because the context is built on the send
  path.** `build_sender` runs inside the request or the scheduler tick, and the
  scheduler's send happens under the row's `FOR UPDATE` lock (D10-08), so a context
  that throws would surface as a relay fault on the first message with a row held
  open. `Settings` therefore refuses, at start-up: a bundle path that is not a file; a
  file `ssl` cannot load (checked with an actual `load_verify_locations`, since an
  empty or truncated PEM passes `is_file` and then raises when the context is built);
  and a bundle set while `SMTP_STARTTLS=false`, where the CA would never be consulted
  and the configuration would read as "TLS on". The pairing check runs **before** the
  bundle is parsed: when both are wrong, "this setting does nothing" is the fix the
  operator needs.
- **The falsification is the reason the new test exists.** Deleting
  `context=self._context` from the `starttls` call makes
  `test_a_relay_with_an_untrusted_certificate_is_refused` fail with
  `DID NOT RAISE` — while `test_the_credentials_cross_only_an_encrypted_socket`
  **still passes**. That is direct evidence that the encrypted-socket test, which
  `6f4cb0b` treated as the TLS proof, never proved peer authentication at all: it
  observed a handshake and always would. The refusal test is the one that can fail.
- **Trade-off, stated rather than buried:** the default is the system trust store, so
  an internal relay behind a private CA needs `SMTP_CA_BUNDLE` set or every send
  fails. That is the correct direction to fail — a misconfigured trust store stops
  reminders rather than silently accepting any certificate — and it is a boot error
  only when the file is unloadable, not when the relay simply is not trusted yet.
- **What this still does not prove:** no test here has verified a certificate issued
  by a public CA over a real network, and none exercises hostname mismatch against a
  provider. What is proven is that a client configured without the matching CA
  refuses the relay (socket suite) and that the context handed to `starttls` is
  `CERT_REQUIRED` with hostname checking on (unit suite). The rest is a provider
  agreeing with `ssl`'s opinion of it, which the first deployment will be first to
  observe.

### D10-11 — `normalized_value` is sealed too, and a token is recognised by shape

- **Decision:** `ExtractedField.normalized_value` holds Fernet ciphertext, like the
  `raw_value` beside it. Both write paths seal it — `extract_contract`'s create *and*
  overwrite branches, and `_apply_correction` in `app/api/v1/review.py` — and the one
  reader that needs the plaintext, `recipient_for` in `app/workers/dispatch.py`,
  decrypts it through a cipher it now takes as a parameter. The decision policy
  (`_usable_fields` in `app/workers/decision.py`) still reads the column directly, but
  only to ask *"is a value present?"*: it tests `normalized_value is not None` and
  never consumes the text, so ciphertext satisfies it and the decision path stays out
  of the PII entirely. Only the send sees an address.
- **Why encrypting this column and not building the blind index gap 20 proposed:** the
  alternative recorded was "encrypt it and compare on a hash". No blind index was
  built, because nothing in this build searches fields by value — the review queue
  orders by confidence and dispatch resolves a recipient for a row already chosen — so
  an index column would have no reader, double the write cost, and imply a
  searchability that does not exist. What encrypting actually costs is SQL
  comparability, which this codebase never used for this column.
  `extraction.py`'s docstring, which described `normalized_value` as "the column
  downstream code compares against in SQL", was corrected rather than left standing:
  it was true when written, and this entry is the change that made it false.
- **Why shape and not a failed decrypt:** `is_sealed()` recognises a token by its
  Fernet shape — `gAAAAA` prefix, minimum length, base64url alphabet — instead of
  attempting decryption and inspecting the exception. A failed decrypt is ambiguous
  between two states that need *opposite* handling: plaintext written before this
  slice (seal it) and a token sealed under a different key (leave it alone, because
  sealing a token wraps ciphertext in ciphertext and destroys the value). Only the
  shape distinguishes them before the key is applied. Ten cases in
  `backend/tests/test_crypto.py` pin the boundary, including a `gAAAAA`-prefixed
  string too short to be a token.
- **The repair path is operator-initiated, and it is deliberately not a migration.**
  Databases that predate this entry hold plaintext in that column.
  `python -m app.cli reseal-fields` (`--ids`, `--limit`, `--dry-run`) seals them.
  Three outcomes are counted separately because they need different answers: plaintext
  is sealed, already-sealed-and-readable is left alone, and already-sealed-but-
  unreadable is reported and left untouched, with the run exiting `EXIT_UNREADABLE`
  (4) instead of continuing — continuing past it would rewrite a correct database with
  the wrong key's output. A data migration was rejected on two grounds: it would need
  `ENCRYPTION_KEY` inside Alembic, and a migration that runs on `upgrade head` cannot
  be dry-run against production. Fifteen tests in `backend/tests/test_reseal.py`
  cover the outcomes, including second-run idempotency and the wrong-key refusal.
- **Fail-closed, and what that costs.** A process without `ENCRYPTION_KEY` can no
  longer read a recipient, and each layer answers in its own vocabulary rather than
  crashing: `cipher_or_503` in `app/api/deps.py` — shared by `review` and
  `notifications` so the two routes answer the same status instead of one 500 where
  the other says 503 — turns a missing key into 503 on the reveal and dispatch routes;
  `dispatch_notification` raises `REFUSAL_RECIPIENT_UNAVAILABLE`, which is a member of
  `SUPPRESSING_REFUSALS`, so a scheduler tick parks the row `suppressed` rather than
  burning the retry budget; and `NotificationScheduler.run_once` returns before
  claiming anything when `has_encryption_key` is false, mirroring the early return the
  no-transport case already had. The cost is stated plainly: a misconfigured key stops
  reminders instead of sending them somewhere unintended.
- **Trade-off, stated rather than buried: key rotation is unsupported.** A changed key
  makes every stored value unreadable, and `reseal-fields` will refuse rather than
  repair it. Re-encryption needs a two-key read window, which is a slice of its own;
  nothing here claims it, and an operator who rotates `ENCRYPTION_KEY` today loses
  every recipient until the old key is restored.
- **What this closes and what it does not.** Gap 20's contradiction — a cleartext copy
  of the same PII one column over from the ciphertext — is gone, and the write path can
  no longer reintroduce it. Not closed: existing rows stay plaintext until the operator
  runs `reseal-fields`, and nothing schedules or alerts on that; and the SMTP path still
  has only ever spoken to a relay this suite started (D10-10).

> **Superseded in part by D10-13.** The trade-off above described the code as it stood
> at `99b5c4b`. The two-key read window it named — `ENCRYPTION_KEY_PREVIOUS`, retired
> keys that unseal but never seal, and a `rewrap` outcome in `reseal-fields` — now
> exists, so a rotated `ENCRYPTION_KEY` no longer strands stored recipients. The rest of
> this entry still holds, including the shape-based `is_sealed`, the decision to seal
> `normalized_value`, and the fail-closed path for a process with no key at all.

### D10-12 — Two silent states get one read-only `doctor`, and it exits non-zero

- **Decision:** `python -m app.cli doctor` counts the three states that need a human and
  currently raise nothing: outbox rows parked `failed` (gap 22's residue), notifications
  that ended `failed` or `suppressed` (gap 20/22's shared residue), and extracted-field
  values still stored as plaintext (gap 20's residue). It prints one report and returns
  `EXIT_ATTENTION` (5) when any count is non-zero, `EXIT_OK` otherwise. It writes
  nothing, and it never logs a value — only ids and columns.
- **Why one command and not three checks:** they are one defect wearing three hats — a
  state that needs a person and is invisible until someone hand-writes a query. Three
  commands means three cron entries, three alerts and three things to forget; one exit
  code means a single scheduled run either fails loudly or does not.
- **Why the exit code carries the signal, not the log line:** the log already said
  `doctor_clean` before this existed, and nobody was watching. A non-zero exit is the
  only thing an external alerter, a CI step or `&&` can act on without parsing text.
  Zero is the *clean* answer, not the default one — the inversion matters, because a
  command that exits 0 on "found something" is a command nobody can schedule.
- **Why counts for the outbox, and pages for the fields:** `dead_lettered()` returns a
  bounded page, which cannot answer "how much is parked"; `dead_letter_count()` is an
  exact table-wide `COUNT`. The field scan is the opposite and says so: it is a single
  ordered pass capped by `--limit`, so a full page means the unsealed number is a
  **floor**. That is reported as `field_scan_truncated` rather than rounded away,
  because "0 unsealed" from a truncated scan is the one answer this command must never
  give confidently. `test_the_scan_limit_can_hide_a_plaintext_row_beyond_it` asserts the
  weakness instead of glossing it.
- **Why it runs without `ENCRYPTION_KEY`:** `is_sealed` is shape-based (D10-11), so a
  keyless run still reports plaintext as plaintext. A key-dependent check would go
  silent at exactly the moment the key was wrong — the moment the database was least
  trustworthy. The mirror property is also pinned: a token sealed under a rotated key
  must **not** be reported, because flagging it would send the operator to
  `reseal-fields`, which would then refuse with `EXIT_UNREADABLE` and teach them to
  ignore the command.
- **Why read-only:** an inspection command that seals, retries or resolves on its own
  initiative is a repair script wearing the wrong name. Sealing is `reseal-fields`'
  job; it needs a key, a decision and a dry run. `test_running_it_changes_nothing`
  asserts the plaintext row is still plaintext afterwards.
- **Trade-off, stated rather than buried: nothing schedules it.** The state is now
  observable and machine-checkable instead of requiring a hand-written query; it is not
  *delivered*. No cron entry, no alert route, no pager is added here, and a command
  nobody runs is worth little more than the log line it replaces. This narrows the GATE
  2 clauses; it does not close them.

### D10-13 — `ENCRYPTION_KEY` can be rotated, through a ring of decrypt-only keys

- **Decision:** `FieldCipher` takes a *ring*: one current key plus zero or more retired
  ones. The current key seals everything; retired keys can only unseal.
  `ENCRYPTION_KEY_PREVIOUS` carries the retired keys as a comma-separated list, oldest
  first, and `Settings` validates it at start-up. `reseal-fields` gains a fourth outcome
  beside seal / leave-alone / unreadable: a token sealed under a retired key is
  **re-wrapped** under the current one and counted as `rewrapped`. This supersedes the
  trade-off D10-11 recorded — rotation was "a slice of its own", and this is that slice.
- **Why retired keys are decrypt-only:** if a retired key could also encrypt, then two
  processes running different configurations would write tokens under different keys,
  and "which key sealed this row?" would stop having one answer. Worse, the end of a
  rotation — clearing `ENCRYPTION_KEY_PREVIOUS` — would be unsafe, because a stale
  process could still be minting tokens with the key about to be dropped. Sealing from
  exactly one key means the moment `rewrapped` reaches zero, the retired keys are
  provably unused by stored data, and clearing the variable is a fact rather than a
  hope. `has_retired_keys` exists for the same reason: it is a property of the
  *configuration*, never of a token.
- **Why `rewrap` lives in `reseal-fields` and not in a second command:** that command
  already owns the question "make the stored tokens conform to the current key". It
  already resolves the cipher, already has `--dry-run`, `--ids` and `--limit`, and
  already has an exit code per outcome. A `rotate-key` command would need its own copy
  of the key parsing, its own dry run and its own unreadable exit — three chances to
  disagree with the repair path about what an unreadable token is. The four outcomes
  are counted in one `reseal_run` log line, so one run answers both "is any plaintext
  left?" and "does anything still depend on the old key?".
- **Rotation is two steps, not one, because a restart is not atomic.** The order is:
  set `ENCRYPTION_KEY` to the new key *and* `ENCRYPTION_KEY_PREVIOUS` to the old one;
  deploy and restart; run `reseal-fields` until its log says `rewrapped=0`; then clear
  `ENCRYPTION_KEY_PREVIOUS` and restart again. The documented sequence in
  `.env.example` says the same thing. A single-step rotation is not offered because it
  would require every process to switch keys at the same instant, and the failing
  state — a process holding only the new key while rows are still under the old one —
  is exactly the outage this entry exists to prevent.
- **The ring is validated at boot, and the errors name positions, not values.** A
  retired key with no current key, the current key also listed as retired, a duplicate
  entry, and more than `MAX_RETIRED_ENCRYPTION_KEYS` (4) are all start-up failures. A
  malformed entry is reported as *which* entry (`ENCRYPTION_KEY_PREVIOUS[2]`), never by
  quoting it — a validation message is a log line, and this file's logging rule is that
  key material never reaches one. The cap is 4 rather than unbounded because the ring
  is walked on every failing decrypt: an unbounded list would turn a config that never
  shrinks into permanent work on the error path, and "I will clean up the old keys
  later" is the habit the cap is meant to interrupt.
- **A row sealed under a key that is no longer in the ring stays readable as
  *unreadable*.** Dropping a key before re-wrapping its rows is not repaired and not
  re-sealed: `rewrap` raises `DecryptionError`, the run counts it and exits
  `EXIT_UNREADABLE` (4). Sealing a token it cannot read would wrap ciphertext in
  ciphertext and destroy the value, which is the same reasoning D10-11 used for the
  plaintext case. The handler's message names `ENCRYPTION_KEY_PREVIOUS` as the fix,
  matching what `decrypt` already said.
- **Trade-off, stated rather than buried:** rotation is a manual, operator-driven
  procedure with no audit trail of who rotated or when, and no hosted-KMS integration —
  keys are still environment variables in the process's memory. The ring also widens the
  blast radius of a *misconfigured* rotation: pasting the wrong key into
  `ENCRYPTION_KEY_PREVIOUS` does not break reads of current rows (retired keys are tried
  last), but it does mean a run can look tidy while an older key is silently absent.
- **What this proves and what it does not.** Ten crypto tests and six CLI tests
  cover the ring: decrypting through a retired key, refusing an unknown key, trying
  several retired keys in order, encrypting with the current key only, both `rewrap`
  paths, a real end-to-end rotation of a stored row, byte-identical output when the row
  is already current, `--dry-run` reporting without writing, and the dropped-key case
  still exiting `EXIT_UNREADABLE`. Not proven: rotation under concurrent writers (no
  test runs `reseal-fields` against a live API process), and interoperability with any
  key service. And as with `doctor`'s field scan, `rewrapped=0` is a statement about
  the rows one run saw — under `--limit` it is a floor, so the command to clear
  `ENCRYPTION_KEY_PREVIOUS` is only earned by a run that was not truncated.

### D10-14 — A consent sheet is validated whole, then written per row

- **Decision:** `python -m app.cli consent-import --file <sheet.csv> [--dry-run]` applies
  the four consent booleans (`consent_email`, `consent_sms`, `consent_voice`,
  `do_not_contact`) to many contracts in one run. The header is
  `contract_id,consent_email,consent_sms,consent_voice,do_not_contact` and is derived
  from `CONSENT_FLAG_COLUMNS` rather than retyped. Outcomes are counted as `updated`,
  `unchanged` and `missing`; `--dry-run` reports the first two and writes nothing. This
  is the buildable half of gap 18 — the consent *screen* is still unbuilt.
- **Why a CLI command and not a bulk HTTP route:** `contract_id` is the table's primary
  key, so a sheet can name any tenant's contract, and an HTTP route would therefore need
  a cross-tenant collection endpoint — precisely what the tenant predicate exists to
  prevent. The two operator commands that already reach across tenants (`extract`,
  `reseal-fields`) are CLI commands for the same reason. The tenant each *write* lands
  in is taken from the contract row, never from the sheet, so a hand-edited id cannot
  move a write into another organisation.
- **Why the sheet is the complete state and not a patch:** it mirrors
  `PUT /contracts/{id}/consent`, which is a full-state replacement because a partial
  `PATCH` cannot express a withdrawal (D9-01). An all-false line therefore revokes, and
  that has to be a normal line rather than an omission — an import that treated a missing
  column as "leave alone" would make revocation the one edit the sheet could not express.
- **Why the whole file is parsed before anything is written:** an importer that stopped at
  the first bad cell would already have applied the good lines above it, leaving the
  database holding half a sheet with nothing recording which half. So the reader returns
  the rows *and* every problem it found, and a non-empty problem list writes nothing and
  names the line. A ragged row, a non-numeric id, a duplicate id and a missing column are
  all reported this way.
- **Why the boolean vocabulary is closed:** `bool("false")` is `True`, so a naive cell
  parser reads a revocation as a grant, and a typo like `ture` would import as consent
  given. Only `true/yes/1` and `false/no/0` are accepted, compared after stripping and
  lower-casing. A blank cell is an **error**, not `false`: consent is stated, so an empty
  cell is an unanswered question, and defaulting it to `false` would silently revoke a
  channel the sheet never mentioned.
- **Why a row that already agrees writes nothing:** no version bump, no audit row, no
  `consent_updated_at` move — the same no-op rule the route uses. That is what makes
  re-running an export from a spreadsheet harmless, and what makes the command safe to
  re-run after it refused a sheet.
- **Why an unknown contract id is reported but the rest still apply:** the rows are
  independent, so failing the whole sheet over one stale id punishes 40 correct lines. But
  the run exits `EXIT_NO_MATCH`, not `EXIT_OK`, because "consent recorded for 40 of 41
  customers" must not look like success to a scheduler or a `&&`.
- **Why the audit row says `system` with no `actor_id`:** an operator with database access
  is not a principal, so there is no `actor_id` to record and borrowing one would fabricate
  the fact the audit log exists to carry. One correlation id is minted for the run, so
  "what did this import change?" is a single query rather than a guessed time range, and
  the row's `reason` names the sheet's **file name** and line — enough to identify the
  import, without recording an operator's directory layout.
- **Why no contact value column, and why no `ENCRYPTION_KEY`:** the address and phone live
  in the sealed `ExtractedField` columns, and a sheet carrying them would be a new copy of
  exactly the PII F7 and gap 20 sealed. The command reads and writes four booleans and an
  id, so it needs no key and cannot be blocked by a missing one.
- **Trade-off, stated rather than buried:** this is not the consent UI, and gap 18 stays
  open because of it. Somebody still has to produce and own the sheet, and consent still
  cannot be granted from a screen — the command replaces a hand-written `UPDATE`, which
  was what a consent sheet produced before this existed and which left no audit row and
  could not distinguish a revocation from a re-grant. There is also no import-provenance
  table: the audit rows are per-contract, so "which sheet authorised this change" is
  answered only by the shared correlation id, not by a first-class record. And a
  `StaleDataError` rolls the whole run back rather than reporting a partly-applied sheet;
  the retry is the operator's, because only they can see what changed underneath.
  **Amended, not deleted (`D10-33`):** the first clause is no longer true — a consent editor
  now ships on `/[locale]/contracts`, so consent *can* be granted from a screen. Everything
  else in this trade-off still stands, and the two are complements rather than rivals: the
  editor writes one contract from the row it was displayed against, the sheet writes many
  in one audited run, and only the sheet can backfill a tenant. The missing provenance
  record is a property of the command, so the editor does not make it worse.
- **What this proves and what it does not.** Fifteen tests in
  `backend/tests/test_consent_import.py` cover the apply path, the audit shape, a
  revocation, a multi-row sheet, an idempotent re-run, `--dry-run`, a malformed cell, a
  blank cell, a missing column, a duplicate id, a header-only sheet, an unreadable file, an
  unknown id alongside applied rows, a spreadsheet BOM, and the `yes/no/1/0` spellings.
  Not proven: nothing tests the concurrency path (`StaleDataError` is handled but no test
  forces it), and no test runs the command against a live API process — the same gap
  `reseal-fields` carries.

### D10-15 — Bootstrap is an operator CLI, and the password never travels in argv

- **Decision:** `python -m app.cli create-admin --tenant-slug SLUG --email ADDRESS
  [--tenant-name NAME] [--password-stdin]` creates a tenant (only when the slug does not
  exist yet) and one `ADMIN` user in it, in one transaction, audited as a system actor.
  This closes gap 10: `POST /auth/users` is admin-only, an admin token needs a user, and a
  user needs an admin, so before this a fresh database could not be logged into at all.
- **Why the operator-CLI branch and not an env-var bootstrap:** gap 10 named the two
  options. A migration-safe env-var bootstrap reads the first password from the
  environment, where it is visible to anything that can read the process environment,
  copied into every container sharing that config, and left behind in deployment
  manifests after it has served its purpose. The CLI runs beside the relay with the same
  database access the previous hand-written snippet had, and leaves nothing behind but an
  Argon2id hash and one audit row. No migration was needed: `Tenant` and `User` already
  existed.
- **Why the password is stdin or a prompt, never an argument:** argv is in the process
  list (`ps` shows it to every user on the box) and in shell history and CI logs, which is
  exactly where a recycled bootstrap password ends up. `--password-stdin` reads the first
  line of standard input — bracketed by a shell pipe, not by the argument vector — and the
  default path asks twice through `getpass`. The twice-typed prompt exists because there
  is no way to read an Argon2id hash back: a mistyped bootstrap password on a
  one-account database is discoverable only by failing to log in, which reads as
  "authentication is broken" rather than "the password was mangled".
- **Why the stdin read is `rstrip("\r\n")` and not `strip()`:** the newline a shell adds
  is not part of the password; a trailing space the operator typed is. Trimming the latter
  would store a hash for a password the login form cannot reproduce, and the symptom would
  be a wrong-password error for the credential that was just set.
- **Why the bounds are imported, not restated:** `MIN_PASSWORD_LENGTH` and
  `MAX_PASSWORD_LENGTH` come from `app.core.security`, the same constants
  `POST /auth/users` validates against, so the CLI cannot drift from the API's policy and
  there is one answer to "what is an acceptable password here". The check happens before
  the database is touched, so an out-of-bounds password never costs a tenant row.
- **Why the slug is lower-cased and shape-checked:** `POST /auth/login` resolves the
  tenant with `strip().lower()`, so a tenant stored as `Acme` is a tenant nobody can log
  into. The stored slug is therefore always what the login form will reproduce, and
  `^[a-z0-9][a-z0-9-]*$` (≤64) rejects the shapes a later URL or log line would have to
  escape.
- **Why the tenant is created only when `--tenant-name` is given:** the two intents are
  different operations — "bootstrap this deployment" and "we lost the only admin of an
  existing tenant" — and inferring the first from a slug that merely looks new would let a
  typo create a second organisation with a fresh admin. On an existing tenant,
  `--tenant-name` must match the stored name or the run is refused, so a mistyped name is
  reported rather than silently ignored.
- **Why a duplicate address is `EXIT_EXISTS` (6) and not success or a reset:** a quiet
  success would leave the operator believing the password had just been set, when nothing
  was written. A reset would make this command a way to take over an admin account without
  knowing its password — it is a bootstrap, not an account-recovery tool. The distinct
  exit code exists so a provisioning script can tell "already done" (do not fail the
  deployment) from "rejected" (fail it). Creating a *different* address in an existing
  tenant still works, because "we lost the only admin" is a real and recoverable state.
- **Why every refusal precedes the first write:** `session_scope` commits whatever it finds
  on a normal return, so a `return` after a flush would persist a half-built bootstrap — a
  tenant with no admin in it, which is the exact state this command exists to get out of.
  An inactive tenant is refused for the same family of reasons: the row would be created
  successfully and the account still could not log in.
- **Why the audit row says `system` with a nil `actor_id`:** an operator with database
  access is not a principal, so there is no id to record and borrowing one would fabricate
  the fact the audit log exists to carry. This is also the only account in the system
  created with no principal behind it at all, so the log row is the only evidence of the
  creation. It carries the role and `tenant_created` and nothing about the person — no
  address, no hash — so the audit trail cannot become a second copy of the PII the rest of
  the system seals.
- **Trade-off, stated rather than buried:** this is not account lifecycle. There is still
  no password reset, no self-service change and no deactivate (gap 8), so a forgotten
  password for the last admin still means a new address or a database operator. It is also
  not a deployment hook: nothing runs it automatically, and an operator who forgets to run
  it gets a blank login screen rather than a failure at boot. And no test runs the command
  against a served port — the login proof goes through the ASGI app, the same limit
  `reseal-fields` and `consent-import` carry.
- **What this proves and what it does not.** Thirty-five tests in
  `backend/tests/test_create_admin.py` cover tenant+admin creation, a real
  `POST /auth/login` with the created credential, Argon2id-only storage, trailing-space
  fidelity, slug lower-casing, a second admin on an existing tenant, the duplicate refusal
  (with the original password still working and no audit row written), a mismatched
  `--tenant-name`, an unknown slug without a name, an inactive tenant, eight bad slugs,
  seven bad emails, three out-of-bounds passwords, empty stdin, a mismatched confirmation,
  the prompt path, and the audit row's shape and non-disclosure. Not proven: nothing tests
  a concurrent second invocation creating the same address, and nothing tests behaviour
  when the database is unreachable mid-run.

### D10-16 — Login admission control is per-account, hashed, and written in its own transaction

- **Decision:** `POST /auth/login` counts failures in a new `login_attempts` table (migration
  `0005`), keyed by a SHA-256 digest of `(tenant slug, email)`, and refuses with `429` +
  `Retry-After` once the window holds `login_max_attempts` (default 5) failures. The policy
  lives in `app/core/login_throttle.py` as pure functions, the route keeps only the wiring.
  This closes gap 9 for the per-account case.
- **Why per-account and not per-address:** the key is derived from the values the caller
  submitted, and nothing else. `X-Forwarded-For` was rejected on purpose: it is
  caller-controlled unless a trusted proxy strips it, so an attacker could mint a fresh
  empty counter with every request and switch the control off — a limiter the attacker can
  disable is worse than none, because it reads as protection. The consequence is stated
  rather than hidden: password *spraying* (one password, many accounts) is untouched by
  this and still belongs at the edge, proxy or WAF, where a client address can be judged
  honestly.
- **Why the key is a digest and the table has no `tenant_id`:** the throttle only needs
  equality, so storing `(slug, email)` would buy nothing and turn a security control into a
  second unencrypted copy of a customer address — the same argument that sealed
  `normalized_value` (D10-11). A side effect worth having: the table cannot be used to
  enumerate which addresses exist. The missing tenant column is the second deliberate
  exception after `IdentityDirectory` (the first is read-only for exactly this reason): an
  attempt is counted *before* a tenant is resolved, and the common case of a wrong slug has
  no tenant row to point at. The exception is safe because no statement in
  `LoginAttemptRepository` can read or write a tenant-owned row.
- **Why rows and not a counter:** a counter needs a lock and a separate "when did the window
  start" column that is easy to reset wrongly, and a lost update silently loses an
  increment. Rows append, so two racing failures both land and the count sees both; the
  window is a `WHERE attempted_at >= ?`, and the block lifts by itself when the oldest row
  ages out — there is no lockout flag a bug or a crash can leave stuck on. Growth is bounded
  by pruning that key's aged-out rows at the next write, so the table tracks *distinct
  recently-failing keys*, not total attempts.
- **Why the failure row is committed on a transaction of its own:** this is the defect the
  slice exists around. `get_request_session` never commits, and the `HTTPException` that
  ends a failed login rolls its session back — so a counter written there would be undone by
  the very refusal it was meant to record, leaving a throttle that reads as protection and
  enforces nothing. `_record_failed_attempt` and `_clear_failed_attempts` therefore open
  their own `session_scope` and are best-effort: a lost row costs one uncounted attempt,
  never a 500 in place of a correct 401 and never a denied valid session.
- **Why `429` and not a friendlier `401`:** a caller has to be able to tell "wrong password,
  try the right one" from "stop trying for a while", or the limiter just makes the app look
  broken. The refusal is raised **before** any password work, so a flood costs a digest and
  a count instead of an Argon2id verification. That makes a throttled request measurably
  faster, which is not an oracle: the answer depends only on the caller's own input, so a
  real account and a fictional one reach it at the same speed.
- **Why `Retry-After` is derived from the oldest failure and floored at 1:** the honest
  answer is "until the oldest counted failure ages out", so that is what is returned;
  `max(1, ...)` keeps it from ever being `0`, which invites an immediate retry loop. It is
  advisory by construction — under concurrent overruns the caller may be refused once more
  with a smaller value — and inventing a precise figure would cost a second query. `decide`
  says so in its docstring rather than in a comment the caller never reads.
- **Why the store is MySQL and not Redis:** `.env.example` has named Redis for this since
  F1, and it stays unwired. The trade is stated plainly: every login now costs one extra
  query, and a database outage takes the throttle with it — but the alternative is a second
  dependency the operator must run for a control that only has to be roughly right, and a
  limiter whose state vanishes on restart is not obviously better than one that is slightly
  slow.
- **Why `record` floors its own timestamp:** MySQL's `DATETIME` has second precision and
  *rounds* on write, so a value carrying 0.7s is stored a moment in the future. Window
  arithmetic would then measure "how long until the oldest failure ages out" from an instant
  that has not happened, and `Retry-After` would outlive the configured window. The model
  default already floors, but the route passes an explicit `utc_now()`, so the repository is
  the single point that covers every caller. The same trap `truncate_to_second` documents
  for outbox eligibility, arrived at independently.
- **What this proves and what it does not.** Forty-one tests in
  `backend/tests/test_login_throttle.py` cover the digest's shape and the absence of a
  readable email, case/whitespace folding and tenant separation, the `decide` boundaries, the
  sixth attempt returning `429` with `Retry-After`, the counter **surviving the 401**, success
  clearing it, an unknown account throttled identically, and other accounts unaffected. Two
  new MySQL integration tests cover what SQLite cannot: that `0005` really built
  `(key_hash, attempted_at)` in that order (read back from `information_schema.STATISTICS`,
  not inferred from the model), and that this server really does round a `.7s` fraction
  forward while `record` floors it — both halves asserted, so the fix is not guarding an
  imaginary hazard. Not proven: no test forces a concurrent `record` race (the append-only
  design is what makes it safe, but the race is argued, not demonstrated), no test covers
  spraying, and the read path is not best-effort — if the count query fails the login 500s
  rather than failing open.

### D10-17 — Refresh tokens are opaque, single-use, and grouped into a revocable family

- **Decision:** `POST /auth/login` returns a refresh token beside the access token and
  `POST /auth/refresh` spends it for a successor; `POST /auth/logout` revokes the session.
  Tokens are random strings stored only as a SHA-256 digest in a new `refresh_tokens` table
  (migration `0006`), grouped by `family_id`, with a `token_epoch` snapshot and a fixed
  `expires_at`. The policy lives in `app/core/refresh.py` as pure functions
  (`mint_refresh_token`, `refresh_token_hash`, `refresh_expiry`) and the routes keep the
  wiring. This closes gap 6.
- **Why an opaque token and not a JWT:** a signed JWT is self-validating, which sounds like
  the whole advantage until the question "has this been revoked?" arrives — and it cannot
  answer that without exactly the server-side lookup table an opaque token needs anyway. It
  would additionally introduce a second algorithm-and-claims surface next to the access
  token. A random value plus a row can be revoked at any instant, which is the property
  that actually matters for a credential with a week-long life. Gap 6 was recorded as a
  usability limit rather than a security one, and this keeps it that way.
- **Why a plain SHA-256 with no salt and no keyed function:** the input is 256 bits from
  `secrets`, so there is no dictionary to attack and no work factor to add — that is what
  Argon2 buys a human-chosen password, not a random token. A salt would actively defeat the
  one query the table exists to answer, "which row is this token?", because the salt cannot
  be recovered from the request. The digest is namespaced (`refresh-token.v1`) so a value
  lifted from this table means nothing anywhere else and a future scheme cannot alias onto
  an old row. The constant is named `REFRESH_DIGEST_NAMESPACE` rather than
  `REFRESH_TOKEN_NAMESPACE` because both secret scanners this repo runs (`ruff` S105,
  `bandit` B105) match on the *variable name*; renaming is the honest fix where a `nosec`
  would have blinded the check for the file.
- **Why rotation rather than reuse:** a stolen long-lived token that stays valid until
  expiry gives the server nothing to notice. With rotation, every redemption spends the
  presented token and mints a successor in the same family, so any individual token the
  client holds is worthless after one use — and a *second* presentation of a spent token is
  positive evidence that someone else has a copy. A plain JWT could not express this
  either: it has no "already used" state.
- **Why reuse revokes the whole family rather than the replayed token:** refusing only the
  replayed value would leave the attacker holding whichever successor they had already
  rotated in, so the victim's session would end while the thief's continued. Killing the
  family logs out both. The cost is stated rather than glossed: a client that races itself
  across two tabs is indistinguishable from a thief and has to log in again, because the
  server genuinely cannot tell the two apart.
- **Why the authoritative check is a conditional `UPDATE` and not a Python `if`:** reading
  the row and then writing it lets two concurrent redemptions of the same token both see
  `used_at is None` and both mint a successor — two live sessions from one token, the exact
  outcome rotation exists to prevent. `claim_for_rotation` issues one
  `UPDATE … WHERE used_at IS NULL AND revoked_at IS NULL` and trusts `rowcount`: the
  database decides, and the loser is treated exactly like a replay. `revoked_at` is part of
  the predicate rather than a courtesy, so a token whose family was already killed cannot be
  spent by a caller still holding a stale copy.
- **Why the repository flushes before those bulk updates:** the request session is built with
  `autoflush=False`, so a row added earlier in the same transaction is still pending and a
  bulk `UPDATE` cannot see it. A revoked-token test caught `claim_for_rotation` answering
  `True` for a row that had already been revoked in that transaction. Fixed with an explicit
  `flush()` at both bulk-update sites; turning autoflush on globally would have changed
  behaviour for every other repository to fix one method.
- **Why a successor inherits `expires_at` instead of getting a fresh window:** a new window
  per rotation makes the session immortal as long as the client keeps refreshing, and
  `refresh_token_expire_days` would then bound nothing. Inheriting the deadline means the
  configured lifetime bounds the whole session from login, with no "how long can this
  actually last" arithmetic to get wrong.
- **Why entitlement is re-read from the database on every redemption:** a refresh token that
  outlived a deactivated account or a deactivated tenant would make both meaningless. The
  route therefore re-resolves the user by id, checks the row's `tenant_id` against the
  user's, checks `is_active` on both, and compares `token_epoch` — the same posture
  `get_current_principal` takes, minus the bearer token, because here the refresh token *is*
  the credential. The token's own claims are never a fallback.
- **Why one 401 with one message for every failure:** forged, expired, spent and revoked must
  be indistinguishable to the caller, or the refusal becomes a probe for which values were
  ever issued. It is `401` and not `403` because the caller presented a credential and it
  bought nothing. Logout follows the same rule from the other side: it takes the *refresh*
  token (the moment a caller most needs to log out is when their access token has expired)
  and returns `204` even for a value that never existed.
- **Why the claim and the successor insert commit together:** a crash between them would
  leave the presented token spent with no successor, ending the session for a caller who did
  nothing wrong. The routes commit once, after the refresh row exists, so a failed commit can
  never return tokens the database cannot later honour.
- **Why the model keeps a redundant `tenant_id`:** the user already has one. Keeping it
  follows D2-02 and makes a future "revoke every session in this organisation" one indexed
  query instead of a join, and it lets `ON DELETE CASCADE` from either parent remove a live
  session. The prune in `create` is scoped to `user_id` for the same reason — housekeeping
  for one account must not touch another's session.
- **What this proves and what it does not.** Forty-four tests in
  `backend/tests/test_refresh.py` cover the policy shape, digest properties (not a bare
  hash, contains neither the token nor the namespace, a bumped namespace cannot alias),
  expiry flooring, rotation returning a new pair, a spent token revoking its successor,
  `token_epoch` bumps and deactivated users refused, the expiry boundary, per-user pruning,
  and the login/refresh/logout route contract including the non-oracle `204`. Five new MySQL
  integration tests cover what SQLite cannot: the unique `token_hash` index and exact column
  types read back from `information_schema`, a duplicate digest rejected with errno 1062,
  cascading delete, `claim_for_rotation` really taking an exclusive lock (errno 1205 on a
  second worker) and returning `False` for the spent token, and `revoke_family` reporting
  3 then 0 while a sibling family survives. Not proven: `/auth/refresh` is not rate-limited
  (single use is the only limit, and the login throttle's key does not apply), ~~there is no
  "sign out everywhere" route — that remains an operator `token_epoch` bump~~ — **superseded
  by D10-18, which adds `POST /auth/logout-all`; the clause is struck rather than deleted
  because it is what that slice was written against** — no global sweeper prunes expired rows
  for an account that never logs in again, and a stolen *access* token is still usable for
  its remaining minutes. The storage decision on the client side
  (httpOnly cookie versus memory) is left open, deliberately: nothing consumes the token yet
  (gap 7), and the response shape does not force either.

### D10-18 — An account is recoverable, and ending a session means both `token_epoch` and the rows

- **Decision:** four routes make an account administrable. `POST /auth/logout-all` ends every
  session of the caller's account, `POST /auth/password` replaces the caller's password after
  proving the current one, and two admin routes add `PATCH /auth/users/{id}` (role and active
  state) and `POST /auth/users/{id}/password-reset` (a generated password, returned once). Every
  one of them ends sessions through a single helper, `_end_sessions`, which bumps
  `user.token_epoch` **and** calls `RefreshTokenRepository.revoke_all_for_user` — never one
  without the other. This closes gap 8.
- **Why the two halves are welded and not left to each caller:** two mechanisms exist because two
  kinds of credential exist, and neither reaches both. `token_epoch` is the only thing that can
  invalidate an **access** token before its fifteen minutes are up — a bearer is verified without
  a database write, so a minted one cannot be deleted, but it carries the epoch it was minted
  under and `get_current_principal` refuses a mismatch. Stored refresh rows are the only thing
  that reaches **refresh** tokens, which are rows and could simply be deleted — they are stamped
  instead, so the log keeps the fact that the session existed. Either half alone leaves a
  credential that still works, and the failure mode of a half-done revocation is an administrator
  who believes they killed a session that is still live. So it is one function rather than a
  sentence in each route's docstring.
- **Why `/logout-all` takes an access token when `/logout` deliberately takes a refresh one:**
  `/logout` takes the refresh token because its whole point is to work when the access token has
  already expired. This route is the opposite case: the caller holds a valid access token, has
  decided it may have been seen, and wants the account to hold no live session at all. Requiring
  the refresh token of the device they are worried about would withhold the remedy exactly when
  it is needed. It ends the caller's own session too, so the very next request is a 401 — a
  "sign out everywhere" that left the button it was pressed from alive would be a lie in the one
  case that matters.
- **Why `/password` rotates the caller's session instead of killing it:** a password change
  happens because the old password may be known to someone else, so leaving the sessions it
  opened alive secures the door and leaves the windows open. But signing the caller out of the
  device they are sitting at teaches users to avoid the button, which is worse than the risk it
  avoids. They are demonstrably holding both the old password and a live token, so they get a
  fresh pair; every other session dies.
- **Why the successor gets a new family rather than continuing the current one:** reusing the
  family would leave its earlier members stamped `used_at`, and a later replay of any of them is
  read as theft and revokes the whole family — including the replacement the user is now relying
  on. A new family is a clean room, so a replay of a pre-change token kills nothing the user
  needs.
- **Why revoking happens before issuing:** issuing first would let the bulk `UPDATE` stamp the
  new row `revoked_at` in the same transaction and hand back a token that cannot be redeemed — a
  one-line ordering bug whose symptom is a session that looks fine and dies on first use. The
  order is written in a comment at the call site because the code reads equally well either way.
- **Why `/password` shares `/login`'s admission control, and through one helper:** a stolen
  access token would otherwise be a free oracle for guessing the password behind it, converting
  fifteen minutes of access into permanence. `_throttle_decision` was extracted so the two routes
  ask the same question in the same place — same key (`login_key_hash(tenant_slug, email)`), same
  window, same arithmetic — because two copies would drift apart at the first tuning change. The
  failure is counted in its own transaction through `_record_failed_attempt`, for the reason
  D10-16 records: the request session is never committed and the 401 would roll the insert back,
  so the counter would never move and the throttle would be decorative.
- **Why a demotion or a deactivation has to end sessions too:** the role travels *inside* the
  access token and `require_roles` reads it from there, so a demoted administrator would keep
  every permission they had for the rest of the token's life — an hour-long window in which the
  revocation has not happened yet. A deactivation needs it for the same reason plus one of its
  own: the epoch only moves forward, so reactivating later cannot resurrect a token minted before
  the account was switched off.
- **Why a PATCH that changes nothing ends nothing:** the routes compare values before acting, so
  "set the role to the role it already has" is idempotent by *value*, not merely by status. The
  alternative ends sessions on a client retry, which is exactly the kind of invisible cost that
  makes callers stop retrying.
- **Why cross-tenant is `404` and not `403`:** `UserRepository` is built from the principal's
  tenant, so another organisation's user id resolves to `None` — byte-identical to an id that
  never existed. Distinguishing the two would make the route a directory probe for which user
  ids exist elsewhere.
- **Why self-change is refused and it is a `409`, not a `400`:** a tenant whose only
  administrator deactivates themselves has no recovery path — `create-admin` refuses a slug that
  already exists (D10-15), so nobody is left who can promote anyone back. `409` because the
  request is well-formed and the *state* is the conflict; a colleague is required, which
  incidentally puts a second pair of eyes on a privilege change.
- **Why a password reset generates the value instead of accepting one:** an administrator who
  chooses the value knows both halves of someone else's credential and can use the account after
  the user changes it. `generate_password()` is 18 bytes from `secrets` (not `random`, which is
  not a CSPRNG), url-safe, fixed width so it is pasteable and checkable, returned **once** in the
  response body and stored only as an Argon2id hash. It is deliberately not in the audit row, not
  in the log line, and not on the user summary.
- **Why reset refuses a deactivated target:** the response would carry a password that login is
  configured to reject, so the administrator would hand over a credential that cannot work and
  the real problem — the account is switched off — would surface as "the password you gave me is
  wrong". Reactivate first, then reset.
- **Why `UserSummary` gained `is_active`:** the route that changes the field answered a
  deactivation and a reactivation with byte-identical bodies, so the caller had to infer the
  outcome from a 200 that both acts return. `/me` reports it as `True` by construction —
  `get_current_principal` refuses an inactive user or tenant before any body runs — so the single
  shape is truthful on both routes.
- **Why only three of the four routes write an audit row:** `user.updated`,
  `user.password_changed` and `user.password_reset` change the account, so they record `role`,
  `is_active` and `token_epoch` — the observable consequence, never the hash. `/logout-all`
  writes nothing, matching `/logout`: nothing about the account changed, and per-session events
  are exactly the volume an audit log exists to keep out. The epoch bump is visible in the row
  for anyone lining a session's death up against it.
- **Why the audit constants are named for the credential while the stored strings say
  "password":** `ruff` S105 and `bandit` B105 match on the *variable name*, not the value, so
  `ACTION_USER_PASSWORD_CHANGED` is reported as a hard-coded secret. Renaming the identifier is
  the honest fix where a `nosec` would blind both checks for the file — the same trade as
  `REFRESH_DIGEST_NAMESPACE` in D10-17, and the same user-facing strings survive unchanged.
- **Why `revoke_all_for_user` flushes before its bulk update:** the request session is built with
  `autoflush=False`, so a refresh row added earlier in the same transaction is invisible to a
  bulk `UPDATE`. Without the explicit `flush()` a password change could revoke five sessions and
  report four. This is the third site to need it; the pattern is now established enough that any
  new bulk update should be assumed to need it until shown otherwise.
- **What this proves and what it does not.** Thirty-six tests in
  `backend/tests/test_account_lifecycle.py` cover all four routes, both revocation halves
  asserted *separately* per case rather than inferred from one success, the audit rows and their
  snapshots, the throttle shared with `/login`, the cross-tenant 404, self-change refusal, the
  deactivated-target reset, and the ordering rule proven by redeeming the successor the password
  change hands back. Not proven: a reset leaves no "must change at next login" mark, so the
  generated password keeps working until the user replaces it — a `must_change_password` column
  is the missing half and it is a migration, not a route; there is still no delete endpoint
  (`is_active` is a switch and the address stays reserved); the temporary password has to be read
  to the user by a human because there is no mail transport, which is why it is generated rather
  than typed but is still a spoken secret; `/password` inherits the per-account login budget and
  nothing per-address (gap 9); and nothing consumes these routes yet — no screen calls `/auth/*`
  (gap 7), so the whole lifecycle is exercised only by `TestClient`.

### D10-19 — The browser holds the session in memory, and rotation is serialised on purpose

Gap 7 is the first slice where a screen calls `/auth/*`, which forces the question D10-17 deferred:
**where does the browser keep the token pair?** `STATUS.md` recorded it as open because the API does
not force either answer — `/auth/login` returns the token in the response body, so a cookie and a
JavaScript-held value are both possible.

- **The choice: memory only. Neither `localStorage` nor `sessionStorage` nor a cookie.** The pair
  lives in a module-level variable in `frontend/src/lib/session.ts` and dies with the tab. This is
  the option that needs no backend change, adds no CSRF surface, and is not a security downgrade
  relative to storage: web storage is readable by any injected script, which is exactly the
  property an XSS bug converts into a stolen refresh token.
- **Why not a cookie now.** The httpOnly refresh cookie is the better *end state* — script cannot
  read it at all — but it is not a frontend-only change: it needs `Set-Cookie` on login and refresh,
  `SameSite` and `Secure` decisions per deployment, a CSRF story for the state-changing routes, and
  the CORS/credentials story turned on. `next.config.ts` already proxies `/api/*` "so cookies stay
  first-party", which is that road half-paved and not mine to finish in a UI slice. Recorded here as
  the intended successor, not as a rejected alternative.
- **The cost is stated rather than hidden: a reload signs you out.** There is no rehydration, so
  every navigation that reloads the document returns to `/login`. That is the honest price of
  memory-only, and it is why the session store is a separate module behind `getSession`/`setSession`
  rather than `useState` in the provider — swapping the storage strategy becomes a one-file change
  that no component has to know about.
- **Rotation is single-flight, and that is a correctness requirement, not an optimisation.** Refresh
  tokens are single-use (D10-17) and a second redemption of an already-spent token is
  indistinguishable from a replay: the backend revokes the whole family. So two concurrent 401s that
  each tried to refresh would log the user out *and* kill every other session from the same login.
  `refreshSession()` therefore caches the in-flight promise and hands it to every caller; the whole
  family-revocation mechanism would otherwise fire on ordinary concurrent page loads. This is the
  first place the client has to know a server-side invariant, and it is written in the module that
  owns the token rather than left to callers to remember.
- **`ApiError` is the only failure shape.** `request()` parses the `{"error": {...}}` envelope once
  and maps a non-JSON body (a proxy error page, a dead connection) to a synthetic
  `network_error` with status `0`. Callers branch on `isUnauthorized` / `isRateLimited` /
  `isOffline` instead of inspecting numbers, so the three failures a sign-in form must distinguish —
  wrong credentials, throttled, API unreachable — each get their own sentence in both locales.
- **The liveness probe is proxied separately.** `/health` is unversioned and outside `/api/v1` on
  purpose, so the existing `/api/:path*` rewrite does not match it; `next.config.ts` gained two
  explicit rules. The readout is worth the two lines because "the API is down" and "your password is
  wrong" are otherwise the same experience at a sign-in form.
- **What this proves and what it does not.** Proven: `npm run typecheck` and `eslint` accept the new
  modules; every wire shape is declared against what `app/api/v1/auth.py` actually returns
  (`SessionResponse`, `UserSummary` with `is_active`, the `error_payload` envelope), so a route
  rename is a compile error rather than a runtime 404. Not proven: **no request has been made against
  a running backend from this environment**, so the client is unverified end to end and the existing
  `pytest` suite remains the only evidence that the server side behaves as typed. Also absent: no
  route is protected (the only authenticated view is a card on `/`, and any page can be loaded
  without a session — the API refuses, which is the real boundary); there is no password-reset or
  user-administration screen, so `PATCH /auth/users/{id}` and the reset route still have no caller;
  and the consent screen (gap 18) is a separate owner decision, not folded in here.
  **Amended, not deleted (`D10-33`):** the consent editor is now built — but not folded in
  *here*. It was taken as its own slice, on `/[locale]/contracts`, which is why this
  paragraph's list of missing callers is still being paid down one screen at a time.

### D10-20 — A reply and an escalation are two separate facts, and the SLA is a server-derived clock

Gap 15 is the slice that makes `ContractStatus.RESPONDED` and `ContractStatus.ESCALATED`
reachable. Both columns and both tables (`customer_responses`, `escalations`) have existed since
migration `0001` and had **no writer** — the enum members were dead ends, `STATUS.md` listed
"nothing records a reply or opens an SLA clock" as an open gap, and the two statuses could only be
produced by editing the database by hand. This slice adds the writers. It needs **no migration**:
every column it fills already existed and is unused by any other code path.

- **Policy lives in `app/workers/escalation.py`, the route is transport.** The same split as
  `dispatch.py` and `decision.py`: `refusal_to_raise`, `refusal_to_assign`, `refusal_to_resolve`,
  `refusal_to_transition`, `sla_deadline`, `is_overdue` and `status_after_response` take their
  `now` and their rows as arguments, so the state machine is testable with no session and no HTTP
  client. Seventeen test functions in `backend/tests/test_escalations.py` — twenty-one cases, since
  three of them are parametrised — call this module alone, and that is where the real edge cases
  live: double-open, resolve-twice, reopen-after-resolve, a reply arriving after the deadline. The
  remaining twenty-eight cases drive the same rules through the ASGI app.
- **`EscalationRefusedError` subclasses `DecisionRefusedError`.** A refusal already has exactly one
  shape in this API — a stable `code`, a prose message, and `details` that never carry a field
  value — and `app/api/v1/agent.refusal` already maps it to the 409 envelope. Subclassing rather
  than copying means a new policy surface cannot drift into a third error vocabulary, which is the
  same trade `DispatchRefusedError` made. The alternative — a fresh `EscalationRefusedError(Exception)`
  with its own handler — would have produced a 409 that looks different to every client for the
  same class of "a rule said no".
- **Recording a reply and resolving an escalation are deliberately *not* the same act.** A customer
  reply (`POST /contracts/{id}/response`) closes the open escalation **and** moves the contract to
  `responded`, in one transaction: the two are one event. A human resolving an escalation
  (`PATCH /escalations/{id}` with `status: resolved`) closes the queue item and leaves the contract
  `escalated`. "Someone on the team worked this" and "the customer answered" are different facts,
  and collapsing them would let an operator silently mark a contract as responded on a customer's
  behalf. `test_resolving_by_hand_closes_the_queue_item_and_leaves_the_contract_escalated` pins
  that separation, because it is the kind of thing a later refactor would tidy away.
- **The SLA is 48 hours from the moment the escalation is raised**, per Q2, and it is derived on
  the server: `sla_deadline` is stored on the row so the queue can sort by it, but `is_overdue` is
  a comparison against a caller-supplied `now` and **`sla_breached` is never stored by this slice**.
  There is no background job here and the endpoint does not write on read. The queue reports an
  `overdue` count instead. That is the honest trade: a stored `sla_breached` would need a poller to
  flip it and would immediately be wrong for any row the poller had not reached yet; a derived flag
  is always right and simply needs someone to look. The window is half-open (`<`, never `<=`), the
  same reading `quiet_hours_until` uses, so an instant belongs to one side of the boundary.
- **`escalation_sla_hours` is configuration, not a constant.** Default `48`, bounded `1..720`. It is
  documented in `config.py` as a business clock rather than a security parameter — the bounds exist
  to reject a typo (`0` would make every row instantly late, `100000` would make the queue useless),
  not to enforce a policy.
- **The reply body is never stored.** `POST /contracts/{id}/response` takes `channel` and
  `received_at`; a `message` field in the body is ignored and appears in neither the row nor the
  audit snapshot. The reply is a *fact of contact* — the thing the escalation clock needed — and
  storing free text would put customer prose into the one table with no review gate, no encryption
  and no redaction path. The test asserts the absence, so "we could add a body later" is a decision
  someone has to make on purpose rather than a field that quietly appears.
- **A late reply is still recorded.** A response arriving after the deadline is not refused: it
  closes the escalation and moves the contract, and `is_overdue` on the *previous* row still shows
  the breach. Refusing it would punish the customer for the operator's lateness. Likewise a reply
  against a `renewed` or `expired` contract is recorded but does **not** rewrite the contract's
  status: `status_after_response` returns `None` there, because "they answered after the term
  ended" must not erase the fact that the term ended.
- **A resolved escalation cannot be reopened, and a no-op PATCH writes nothing.** Reopening would
  leave `resolved_at` and `resolution_note` populated on a row that is open again — a queue item
  whose own columns disagree about whether it is closed. The action an operator actually wants is
  a *new* escalation on the same contract, which the route already allows and which gets its own
  deadline. Re-PATCHing the status a row already holds is a silent no-op with no audit row, so the
  audit log records changes rather than requests.
- **Cross-tenant access is a 404, not a 403**, on both the contract and the escalation id, and an
  `assigned_to` naming another tenant's user is a 404 too. A 403 confirms the row exists, which is
  the thing tenant isolation is supposed to hide. This matches every existing router.
- **What this proves and what it does not.** Proven: 49 new tests, `pytest backend/tests` at
  **780 collected — 779 passed, 1 skipped, 0 failed** against real MySQL 8.4 (`KAFKA_LIVE` is the
  only skip) and **780 — 766 passed, 14 skipped, 0 failed** with `MYSQL_TEST_URL` cleared; the
  19-test `-m integration` leg passes. The queue and both mutation routes go through the real ASGI
  app, so tenancy, roles, the 409 envelope and the audit rows are exercised end to end. Not proven:
  **nothing flips an overdue row**, so an operator has to look at the queue for the SLA to mean
  anything — `sla_breached` stays unreachable as a *stored* status until a poller exists (gap 5/17
  territory, not this slice); there is no UI — no screen calls any of these five routes, so the
  whole surface is exercised only by `TestClient`; escalation is manual, so the "two weeks of no
  response" trigger that the Phase-0 demo shows is still a human decision rather than a rule; and
  the response record has no deduplication, so two identical replies are two rows (correct for an
  audit trail, but a caller retrying on a timeout will double-record).

### D10-21 — Silence is measured from a notification, and the trigger never flips a flag

Gap 15 gave `customer_responses` and `escalations` a writer and made `responded` and
`escalated` reachable — but only through a route a human called. `STATUS.md` recorded
the residue in exactly those words: "what is still missing is a *trigger*, not a
writer: nothing scans for silence and raises an escalation by itself, so 'two weeks
with no reply' remains an operator's judgement." This slice is that trigger:
`EscalationScanner` (`app/workers/escalation_scanner.py`) on top of one new
repository function, `claim_silent_contracts`. It needs **no migration** — every
column it reads or writes already existed.

- **The evidence is a notification, not a contract status.** `ContractStatus` has a
  `reminder_sent` member that **nothing writes**: `dispatch_notification` sends the
  message and does not move the contract. A scanner that waited for that status would
  wait forever, and one that *set* it would assert a second fact ("a reminder went
  out") from inside a loop about a third ("nobody answered"). So "we contacted them at
  T" is read from `notifications.sent_at`, the column dispatch actually writes, and
  the contract status is only used to exclude states escalation is illegal from. The
  unwritten status is **not fixed here** — making dispatch write `reminder_sent` means
  deciding whether a transport may advance a business state, which is a different
  slice with a different blast radius. It is recorded in `STATUS.md` as its own
  finding instead of being papered over by the scanner.
- **Two clocks, and conflating them breaks both directions.** `escalation_no_response_days`
  (default `14`, bounded `1..90`) decides *whether* to escalate; `escalation_sla_hours`
  (default `48`, bounded `1..720`, D10-20) starts counting only *after* one is raised.
  Using the SLA as the silence window would escalate on day two; using the silence
  window as the deadline would hand the operator a deadline that began before they
  were told. The deadline is also computed **once per pass**, from the scan instant,
  rather than per contract from that contract's own last contact — one tick, one
  deadline, so two contracts escalated together are due together and a test can
  assert a single value.
- **`ACTOR_WORKER = "worker"` is a third actor type.** `user` already meant "a person
  did this" and `system` "an operator command did this". A scanner is neither: it is a
  process deciding while nobody watched. Reusing `system` would have cost nothing
  today and destroyed the one question the audit log exists to answer — *did a human
  open this?* — so the distinction is added now, while there are only two writers of
  `escalation.opened` to keep consistent.
- **The claim is the third deliberately unscoped query, and for the same structural
  reason.** `claim_next_due` and `claim_due` are unscoped because one process serves
  every tenant and cannot name a tenant before picking a row; this one is the same
  case, which is why it is documented as a *category* rather than an exception. The
  tenant comes **from the claimed row**, exactly as
  `NotificationScheduler._dispatch_locked` takes it, and every follow-up query is
  scoped by it. "Silent" is four conditions — contacted before the cutoff, **not**
  contacted since it, no reply, no open escalation — and the two contact clauses are
  built by one shared `_contacted()` helper so they cannot drift into disagreeing
  about which notification statuses count. That drift is the specific bug that would
  escalate a contract halfway through its own reminder sequence.
- **Both contact clauses are `EXISTS`, not a `MAX(sent_at) < cutoff` select column.**
  They mean the same thing and read the same way; only this shape keeps `FOR UPDATE`
  on a single table, which is what makes `SKIP LOCKED` safe to reason about. The lock
  is taken on the contract rows the caller is about to mutate, not on a joined
  notification set whose size nobody bounded.
- **`escalated` is excluded from the scannable states.** D10-20 makes a hand-resolved
  escalation leave the contract in `escalated`. A scanner that read that state as
  "still silent, raise another" would re-open, every poll interval, a queue item an
  operator deliberately closed — the worker undoing a human's decision is the worst
  failure available to an unattended process, because nobody is watching it happen. A
  contract that must be chased again gets a *new* escalation from a person, which gets
  its own deadline. Also excluded: `responded` (the customer answered), and
  `renewed`/`expired` (the file is closed).
- **A refusal is logged, not raised.** The claim query already excludes everything
  `refusal_to_raise` refuses, so a refusal means the query and the policy have drifted
  apart. The policy wins, the contract is skipped, the rest of the batch proceeds, and
  `ScanResult.refused` carries the codes so the drift is visible rather than
  swallowed. Raising would roll back every good escalation beside it: a refusal writes
  nothing, so the batch's transaction has nothing to protect.
- **One transaction per pass.** The claim and all its writes commit or roll back
  together, so a tick either happened or did not. That is also why
  `escalation_scan_batch_size` (default `50`, bounded `1..1000`) matters: it is not a
  throughput knob, it is the bound on how long row locks are held.
- **`sla_breached` is still never written**, unchanged from D10-20. Overdue stays a
  derived reading of `sla_deadline` via `is_overdue`. A stored flag that can disagree
  with its own deadline gives a queue two answers to "is this late", and the one it
  displays would be the stale one. The scanner, which is the obvious candidate to flip
  it, deliberately does not.
- **What this proves and what it does not.** Proven: 25 new tests
  (`tests/test_escalation_scanner.py`) covering the trigger, the two clocks, the audit
  row, and every case that must be *left alone*; the suite at **805 collected — 804
  passed, 1 skipped, 0 failed** against real MySQL 8.4 (`KAFKA_LIVE` is the only skip)
  and **805 — 791 passed, 14 skipped, 0 failed** with `MYSQL_TEST_URL` cleared; the
  19-test `-m integration` leg passes 19/19. Not proven: **the locking is argued, not
  demonstrated** — the SQLite leg runs the same claim query with no row locks at all,
  so `SKIP LOCKED` on this query is exercised only by the MySQL leg, and even there
  two concurrent scanners are never actually raced; ~~**nothing runs the scanner** — no
  compose service, unit or cron invokes `python -m app.workers.escalation_scanner`, so
  the trigger now exists but has no daemon, which is the same residue the outbox relay
  and the notification scheduler still carry~~ (amended by `D10-25`, and it was also
  wrong about the relay: the relay has had a compose service since F2, which is the whole
  reason publishing worked while escalating did not. Two workers lacked a daemon, not
  three. `docker-compose.yml` now defines `scanner` and `scheduler` services, so the
  local stack starts both; what survives is narrower — no unit, no cron, no orchestrator,
  `restart: unless-stopped` alone, and a running scanner still finds nothing to escalate
  until a contract has actually been contacted, which is gap 5);
  `sla_breached` stays unreachable as a stored status; ~~there is still no screen for the
  queue~~ (closed by `D10-23`: `/[locale]/escalations` reads the queue and works it);
  and `reminder_sent` remains an enum member with no writer.

### D10-22 — The review screen mirrors the route's shape, and a conflict reloads instead of patching

Every slice since gap 7 that produced a *route* left a matching residue in `STATUS.md`:
"the `review`, `agent`, `notification` and now `escalation` routes have still only ever
been reached by `TestClient`". That is not a cosmetic complaint. F8 is the gate the
non-negotiables rest on — no field below the confidence threshold may reach a customer
until a person decides about it — and a gate that only a test can operate is a gate the
product does not have. `frontend/src/app/[locale]/review/page.tsx` is the first screen
that can *complete* work rather than report it, so the decisions worth recording are the
ones about how it talks to `app/api/v1/review.py`.

- **The client type is the route's shape, not a convenience around it.** `ReviewItem`
  in `frontend/src/lib/api.ts` has no `value` field and no field to add one to, because
  the queue route has none: a value comes from `POST /review/{id}/reveal`, which writes
  an audit row. Adding one "for display" would put PII into whichever screen reused the
  type and would make the audited reveal look optional. The same rule covers
  `FieldName`/`FieldStatus`/`ContractStatus`, which are copied from `app.db.enums` and
  are the reason a new enum member is a compile error here rather than a raw string on
  screen.
- **A `409` invalidates the queue; it never patches it.** A decision carries the
  `version` the reviewer saw. On success the decided row leaves the queue, so the page
  the reviewer was reading is no longer the page the server would answer — and a spliced
  list would keep answering with the *old* `next_cursor` for the rest of the visit,
  skipping or repeating rows on the next page. So both the success path and the stale-
  write path call `invalidateQueries`. The reviewer loses their scroll position; the
  alternative is a paging bug that only appears under concurrency, which is exactly when
  nobody is reading the diff.
- **Reveal is a per-row action, and the revealed value is not cached.** It lives in a
  `Record<fieldId, string>` in component state, dies with the visit, and is never
  written to `sessionStorage` or the query cache. Caching it would mean a re-render, a
  back-navigation or a window restore re-displays a value whose reveal was audited once
  — the audit row would then describe one read while the screen performed several.
- **Every card carries its own error.** One shared banner cannot say *which* decision
  failed, and a reviewer deciding three rows would have to guess. The error is attributed
  by comparing the mutation's `variables` with the card's `field_id`, which is also why
  the mutation takes an object rather than a bare id: the variables *are* the
  attribution.
- **The refusal vocabulary is translated, not assumed away.** `viewer` sees the queue and
  cannot decide; the screen hides the buttons, but the boundary is the route's `403`, so
  `isForbidden`, `isConflict`, `isOffline` and `422` are each named in the reviewer's
  language and everything else falls back to one sentence. A UI that treats a refusal as
  impossible turns a permission bug into a spinner that never stops.
- **The `bn as Messages` cast in `frontend/src/i18n/messages.ts` is deleted.** It was
  the mechanism by which a namespace added to `en.json` and forgotten in `bn.json` stayed
  a runtime gap — a raw key path on a Bengali screen. Uncast, `Record<Locale, Messages>`
  makes the omission a compile error. The two message files are at exact parity (107 keys
  each, 65 of them under `review`) as of this commit, which is the state that keeps the
  check meaningful rather than noise.
- **The link on the home page is shown whether or not there is a session.** Hiding it
  would leave a signed-out visitor no way to learn the screen exists; the queue itself
  asks for a session and offers the sign-in link, so the worst case is one extra click.
- **What this proves and what it does not.** Proven: `tsc --noEmit` and `eslint` accept
  it, and the shapes it types were read out of the routes rather than guessed. Not
  proven: **no browser has run it** — there is no frontend test runner and no e2e suite,
  so "the buttons work" is an argument from the API contract, not an observation; the
  optimistic-concurrency path is written but has never been raced by two real reviewers;
  and the read-only `viewer` path has never been seen with a real viewer session. The
  residue this closes is one held item, not the whole of gap 15's "no screen" bullet:
  `agent`, `notification` and `escalation` are still `TestClient`-only.

### D10-23 — The escalation screen separates "someone worked it" from "the customer answered"

`frontend/src/app/[locale]/escalations/page.tsx` is the second screen that can complete
work, and it is the one that makes the previous two slices usable: `6fe6158` taught the
system to *notice* a silent contract on its own, so a queue nobody can work is a strictly
worse state than no queue at all. The interesting decisions are not about styling; they
are about which distinctions the screen refuses to collapse.

- **Resolving is not responding.** `PATCH /escalations/{id}` closes a *queue item*; the
  contract keeps `escalated` because nobody has heard from the customer. Only
  `POST /contracts/{id}/response` moves it to `responded`, and it closes the open
  escalation as a side effect in the same transaction. The screen therefore shows two
  visually distinct actions with different vocabulary — "Close the queue item" and
  "Customer replied on …" — because a single "done" button would let an operator report
  a customer answer they never received. The reply's outcome is reported at page level,
  not on the card, precisely because recording it usually deletes that card: a message
  rendered inside a row would vanish with the row.
- **Late is read, not recomputed.** `overdue` comes from the server, which decided it
  against the same clock that wrote `sla_deadline`; a client computing it from the
  deadline would disagree with the row it is displaying for anyone whose clock is off.
  For the same reason `sla_breached` is neither a filter nor a settable status: it is
  derived, and `_require_a_change`/`refusal_to_transition` refuse it. The working queue
  filters to `open` + `in_progress`, and lateness is a badge.
- **A reply has no body.** There is no field on this screen for what the customer said,
  because the request model has no field for it: `customer_responses` records *that* they
  answered, on which channel and when. A textarea here would put the largest PII surface
  in the app into a table that every other decision since gap 20 went out of its way to
  keep free of plaintext.
- **Channel labels come from a record, not from string surgery.** `CHANNEL_KEYS` is
  `as const satisfies Record<ContactChannel, string>`, so `Channel` gaining a member is a
  compile error here. `t("…channel" + value)` would have resolved to a missing key on a
  screen with no test runner to catch it.
- **A `409` refetches; it never splices.** Same rule and same reason as D10-22: a
  resolved row leaves the working queue, so the loaded page is no longer what the server
  would answer. The `onError` path invalidates on `409` and `404` as well, and the two
  409 codes are translated separately — `ESCALATION_CLOSED` ("someone closed this under
  you") and `INVALID_TRANSITION` ("that is not a move this queue allows") are different
  facts, and one shared "conflict" sentence would erase the difference.
- **A row's error belongs to that row.** Attribution again uses the mutation's
  `variables` (`update.variables?.id`), and one mutation serves claim and resolve so the
  busy state is `update.variables?.id` rather than a per-card flag that can outlive its
  request.
- **One `Intl.DateTimeFormat`, used for both timestamps.** `resolved_at` was initially
  rendered as a raw ISO string while `sla_deadline` was localised — a plausible-looking
  inconsistency, and the kind that only shows up in a locale nobody tests.
- **The message files are at parity, measured not assumed.** `en.json` and `bn.json` are
  165 flattened keys each with zero one-sided keys, 58 of them under `escalations`; the
  flattened-key comparison was run rather than eyeballed, and it caught an unused
  `statusLabel` that was then deleted from both files instead of shipped as dead weight.
  The home page links this screen with the same reasoning as the review link (D10-22):
  shown signed-out, because hiding it leaves a visitor no way to learn it exists.
- **What this proves and what it does not.** Proven: the TypeScript language service
  reports no error in any edited file, and every wire shape (`EscalationView`,
  `EscalationListResponse`, `EscalationUpdateRequest`, `RecordResponseResponse`, the
  repeated `?status=` query, the `overdue`/`sla_hours` echoes) was read out of
  `app/api/v1/escalations.py` rather than guessed. At authoring time **`npm run typecheck`
  and `eslint` had not been run on it** — the shell was unavailable for the whole of the
  session that wrote the screen, so no process could start, and "the language service is
  quiet per file" is a weaker statement than "the project compiles and lints". **Since
  resolved:** the screen was committed as `00e9b54`, whose pre-commit hook ran both over
  the whole project and passed. Still not proven: **no browser has run it** — there is
  still no frontend test runner, so this slice adds no test count at all; no two
  operators have collided on a `409`; no `viewer` session has loaded the queue; and
  ~~nothing in the repo starts the scanner that raises the rows this screen lists (no
  compose service, systemd unit or cron entry), so in a real deployment the queue fills
  only if an operator runs the worker by hand~~ — amended by `D10-25`, which adds a
  `scanner` service to `docker-compose.yml`: on that stack the queue now fills on its own.
  What survives: no unit, cron entry or orchestrator definition exists outside compose,
  and a scanner that is up still raises nothing until a contract has actually been
  contacted (gap 5), so a freshly seeded database still shows an empty queue.

### D10-24 — The decision screen offers the choice and lets the policy refuse

`frontend/src/app/[locale]/agent/page.tsx` closes the third route family that had only
ever been reached by `TestClient`. It is the one with the shortest distance to a
customer: `POST /notifications` schedules from a **stored** decision, so until this
screen existed a recommendation nobody could inspect was a recommendation that decided
by default.

- **The client never re-derives the permitted channels.** It offers all three and lets
  `permitted_channels` answer, then reports what the answer was: `CHANNEL_NOT_PERMITTED`
  carries `details.permitted` (`agent.py:483`), and `permittedChannelsFrom` narrows that
  from `unknown` and filters it. The alternative — recomputing the permitted set here
  from the consent flags — would put a second copy of the contact policy in the browser,
  where it would drift from the copy that actually refuses. A refusal therefore has to
  be able to name the channels that *would* work, which is why the screen reads `details`
  at all: it is the only place the client learns consent's answer.
- **A re-run appends; a correction edits.** "The policy changed its mind" is a new fact
  and gets a new row (`POST /agent/decisions` → 201). "A person disagrees with *this*
  row" is a fact about that row, so the override and flag routes edit it in place with
  `original_channel` preserved. Collapsing the two into one button would destroy the
  history F9 exists to leave behind, so the appending action is styled apart from the
  correcting ones and the screen shows `rerun_count` to make repeated re-rolls visible.
- **Corrections appear only on the current recommendation.** The override and flag
  routes resolve "the latest decision for this contract" server-side, so offering them
  on a superseded row would send a version the server has already moved past — every
  click would be a `409`. `latestIds` restates that rule locally over an already
  newest-first page: the first row seen for a contract *is* its latest, because every
  newer one would have sorted above it. Superseded rows get a sentence explaining why no
  buttons are offered rather than silently looking broken.
- **A refusal is reported on the row that was acted on, and only that row.** TanStack
  hands the failing call's `variables` back with the error, so the obvious filter is
  `variables.contractId === row.contract_id` — and it is wrong: a contract with three
  decisions on the page would print one operator's refusal under all three, blaming two
  rows nobody touched. The predicate is therefore `isLatest && …`, which is what the
  buttons already require, so the card that offered the action is the card that explains
  it. Progress (`busy`, `correcting`) is narrowed the same way for the same reason.
- **A refusal code is translated individually, not collapsed into "failed".**
  `app.workers.decision` gives each refusal its own machine-readable code on purpose, so
  `REFUSAL_KEYS` maps the five policy codes to five sentences: "the customer consented to
  nothing" and "a field is still waiting for a person" call for different next actions.
  An unlisted code still yields a generic sentence, so a new refusal degrades to vague
  rather than to blank.
- **A `409` refetches; it never splices.** Third time, same rule (D10-22, D10-23). The
  `onError` path invalidates on `409` *and* `404`, because a contract that moved out of a
  decidable state returns as a refusal rather than as a missing row.
- **The channel label map is a type, not a concatenation.** `CHANNEL_KEYS` is
  `as const satisfies Record<ContactChannel, string>`, so a new `Channel` member is a
  compile error. This is the same guard D10-23 introduced, applied here because this
  screen builds labels for both `channel` and `original_channel`.
- **`ApiError.details` is `unknown`, narrowed only where the route is known.** The
  envelope's `details` shape differs per route (`{"permitted": [...]}` here,
  `{"current": {...}}` for a lost `version` race, `{"blocking": n, "fields": [...]}` for
  a blocked pipeline), so a union of every route's shape would be a lie the compiler
  could not check. Each caller narrows what it actually reads.
- **The message files mirror each other key for key.** `agent` carries 58 keys in
  `en.json` and 58 in `bn.json`, and all 58 are reachable from this page: 50 literal
  `t("agent.…")` calls, three channel labels via `CHANNEL_KEYS`, and five refusal
  sentences via `REFUSAL_KEYS`. Nothing was added to either file that no code path can
  render — the unused-key check that caught `statusLabel` in D10-23 was applied here
  before writing the Bengali block rather than after.
- **What this proves and what it does not.** Proven: every wire shape
  (`AgentDecisionView`'s fourteen fields, `AgentDecisionListResponse`'s `items`/`count`/
  `contract_id`, `DecisionOverrideRequest`, `FlagReviewRequest`, the `/decisions`,
  `/decisions/{contract_id}/override` and `/decisions/{contract_id}/flag-review` paths,
  the `?contract_id=` filter) was read out of `app/api/v1/agent.py`, not guessed; the
  five policy refusal codes and the two route-level codes were read out of
  `app/workers/decision.py` and `agent.py`; and the TypeScript language service reports
  no error in any edited file. **The project-wide gate did eventually run:** the session
  that wrote this code could not start any process (the harness echoed each command's first
  byte and exited 1), so at authoring time nothing was gated and nothing was committed;
  restarting the IDE cleared the fault, the commit then ran all 17 hooks green — including
  `tsc --noEmit` and `eslint` — and the slice landed as `25886aa`. Not proven: **no browser
  has run it** — nothing has clicked a button, so the per-file and project-wide type checks
  are the only evidence about behaviour — and there is still no frontend test runner, so
  this slice adds no test count; the gate is typecheck plus lint, which is weaker than any
  test in `backend/tests`. No two operators have raced a `409` here, no `viewer` session has
  loaded the list, and the override path has never been exercised against a customer whose
  consent was withdrawn between page load and click.

### D10-25 — Every poller is a compose service, and a worker stops reporting a port it does not serve

Three modules in `app/workers/` are written to run forever: `outbox_relay`,
`notification_scheduler`, `escalation_scanner`. All three already end identically — a
`main() -> int`, an `if __name__ == "__main__"` guard, `SIGINT` and `SIGTERM` bound to a
`threading.Event` that the loop tests instead of `sleep`, and `engine.dispose()` in a
`finally` — so the code for an unattended deployment was written, and the tests drive
`tick()` directly, which is why a suite of 805 tests never noticed that nobody started
two of them. The gap was in `docker-compose.yml`: it defined `api` and `relay`, and that
is all. The notification scheduler (gap 22) and the escalation scanner (gap 15) had no
service anywhere, so a reminder went out on schedule only if a person ran the module by
hand, and `escalated` was reachable without an operator only in tests. A worker that is
never started is indistinguishable from a worker that is broken; both are silent.

- **The daemon is a `command`, not a second image and not a second copy.** Each worker is
  the API image with a different `command` — `python -m app.workers.<name>` — so worker
  code cannot drift from the application code that the API serves. Five services share
  one `Dockerfile.backend`; the differences between them are the entry points.
- **The shared half is one `x-worker` anchor, and the relay moved onto it.** Three
  near-identical service blocks are exactly where a `read_only: true` or a
  `no-new-privileges:true` line goes missing in one of them, and the failure is invisible:
  one worker is quietly less contained than the other two and nothing reports it. YAML
  anchors make the shared half exist once. The relay was rewritten onto the anchor in the
  same commit rather than left as the fourth hand-copied block, because the value of the
  anchor is that *no* worker is the exception.
- **A worker's healthcheck is switched off, not repointed.** This is the defect the slice
  found, and it was live since F2. `Dockerfile.backend` declares a `HEALTHCHECK` that
  fetches `http://127.0.0.1:8000/health`, which only uvicorn answers. Every worker
  inherits it from the image, so `relay` has reported **unhealthy for its entire life
  while working correctly**, and any `depends_on: {condition: service_healthy}` pointed at
  a worker would have waited forever. Two wrong answers were available: a second
  healthcheck that asserts nothing (a probe of a worker's own loop from inside the same
  process proves the process is scheduled, which `PID 1` already implies), or a real
  endpoint on every worker. Both add a listening surface to a process that has no reason
  to have one, so the correct answer is `healthcheck: {disable: true}` — the probe is
  removed rather than made to lie. The API keeps its liveness probe, and the note above it
  explaining why it is `/health` and not `/health/ready` still stands.
- **`make logs` followed `api` only, and was missing from `make help`.** With two services
  that hid little; with five it hides a crash-looping worker from the first command an
  operator reaches for. It now follows every service, and `make logs S=api` narrows it to
  one, which is the same shape as `docker compose logs -f`.
- **`D10-21` was wrong about which workers lacked a daemon, and is corrected rather than
  quietly left.** It said "nothing runs the scanner … the same residue the outbox relay
  and the notification scheduler still carry". Two carry it, not three: the relay has had
  a compose service since F2, which is exactly why publishing works and escalating did
  not. The error is worth recording because it is the failure mode of a residue written
  from memory — a caveat copied forward stops being checked and starts being a claim. The
  escape hatch is that the record now names the commit that fixed it.
- **Both new services are default-on.** `docker compose up` now starts an escalation
  scanner and a notification scheduler against whatever `DATABASE_URL` `.env` names. For
  this project that is the intent — the stack should behave like the product — but it is a
  behaviour change for an `.env` that predates the slice, and it means the local stack can
  now write `Escalation` rows without anyone asking for one.
- **What this proves and what it does not.** Proven, by running it:
  `docker compose config --services` lists `mysql api relay scanner scheduler`; the JSON
  render confirms each worker's `command` ends at the right module, `healthcheck.disable`
  is `true` on all three workers and unset on `api`, and the anchor really merges
  `read_only: true`, `restart: unless-stopped` and `no-new-privileges:true` into every one
  of them. `env_file: .env` means the render fails on a tree with no `.env`, as this one
  has none, so a temporary gitignored `.env` with placeholder passwords was used for the
  checks and removed after each run. Not proven: **no container was built or started**, so
  "the workers have a daemon" means compose *will* start them, not that they ran; the only
  evidence about the running system is still `tick()`, called by tests. `restart:
  unless-stopped` is the whole supervision story — no unit, no cron, no orchestrator, no
  alert on a crash loop — and compose's 10-second stop grace is untuned, so a pass that
  outlives it takes `SIGKILL` and rolls back without the graceful path finishing (an
  exposure the relay already carried). Two scanner replicas are safe by argument, not by
  test: the claim uses `SKIP LOCKED`, and no test races two scanners. And a daemon with
  nothing to do still does nothing — the scheduler idles until something other than a
  person creates a `Notification`, which is gap 5, and the scanner finds nothing to
  escalate until a contract has actually been contacted. No `backend/` file changed, so
  the suite stays at **805 collected — 804 passed, 1 skipped** and the migration head at
  `0006`; this slice adds no test count of its own, which is the same limit `D10-22`,
  `D10-23` and `D10-24` recorded and the reason the verification above is a table of
  commands rather than a count.

### D10-26 — Ingestion keeps the document, sealed and addressed by its own hash

`Contract.storage_path` has existed since migration `0001` (line 144) with **no
writer and no reader**, and that single fact was the whole of gap 12. The
metadata route accepts a JSON body, so it knows what a contract *is* and has
never held what it *says*; `extract_contract` takes a `document_text` that
nothing in `backend/app` can produce. This is why work on gap 5 stalled: the
consumer of `contract.ingested` has no correct work today, because the payload
carries ids and dates and the extraction it would drive needs text. The first
thing to build was therefore not the consumer but the input it would consume.

- **A sibling route, not a wider one.** `POST /contracts` takes JSON metadata and
  fingerprints *the request*; `POST /contracts/document` takes opaque bytes and
  fingerprints *the file*. Folding them together would mean one schema with a
  mutually exclusive body, so the second is its own route with its own media
  type, sharing the pieces that must not drift: `_clean_customer_ref`,
  `_clean_timezone`, `ContractSummary`, and the five payload keys of the outbox
  event, so a consumer cannot tell which route produced a fact it reads.
- **The media type is declared rather than inferred.** FastAPI decides how to read
  a body from its content type, and anything `application/json` is parsed as
  JSON. A client that sent a PDF under the sensible-looking JSON header would get
  a decode error instead of its document stored — a confusing answer to a
  plausible mistake — so the route names `application/octet-stream` explicitly.
- **The key is the hash of the plaintext, not of the ciphertext.** Content
  addressing makes the store idempotent for free: the same file uploaded twice is
  one blob, and a re-upload recognises it instead of re-sealing it under whatever
  key ring the new process happens to hold. The digest is also what
  `contracts.sha256` was already for, and its uniqueness is `(tenant_id, sha256)`
  — so two tenants uploading one file share the *bytes* but keep their own rows,
  which is right: the blob is the immutable input, the row is the tenant's
  relationship to it.
- **Sealed by the same key ring as field values, or not stored at all.** A PDF
  holds every field on the page at once — name, address, date of birth — so a
  document kept in the clear would undo gap 20's entire argument about
  `ExtractedField.raw_value`. Rather than a second cipher and a second rotation
  story, `FieldCipher` grew `encrypt_bytes`/`decrypt_bytes` and `encrypt`,
  `decrypt`, `rewrap`, `_from_retired` and `_decrypt_with` were rebuilt on top of
  them, so there is **one** ring walk rather than two copies of it. A process
  with no `ENCRYPTION_KEY` cannot accept a document; `document_store_or_503`
  reuses the field-value refusal, whose message now says "encrypted data" because
  documents joined that set and the operator needs the variable name, not the
  kind.
- **A stored key is untrusted input.** It is a string in a column, and a row
  edited by hand must not be able to name `../../etc/passwd`. `_path_for` checks
  the shape (`<two hex>/<sha256>`) *and* resolves and re-checks containment inside
  the root, because a shape rule cannot see a symlink planted in a shard
  directory. The refusal never echoes the offending value: a key is a path, and a
  path read back from a row is exactly the string that must not reach a log.
- **The bytes are written before the row that names them is committed.** A crash
  between the two leaves an unreferenced blob — harmless, deduplicated, and
  reachable again the moment the same document is re-uploaded. The opposite order
  leaves a row pointing at nothing, which no retry can repair. The digest is also
  computed *first*, so a duplicate is refused without sealing a document no row
  will ever reference.
- **Nothing is parsed here.** Storing and reading are separate concerns, and PDF
  text extraction is unbounded CPU work that does not belong inside a request
  that is otherwise one write. `storage_path` is the handoff and this route's
  guarantee is the narrow, testable one: a document that arrives is sealed,
  named by its commit, and addressed by its own hash.
- **`DOCUMENT_STORE_PATH` must be absolute outside development, and
  `Path.is_absolute()` was the wrong test for it.** `pathlib.Path` is flavoured by
  the host OS, so `Path("/var/lib/contract-agent/documents").is_absolute()` is
  **`False` on Windows** — the validator would have rejected a correct POSIX
  deployment path on every Windows developer's machine while passing in CI. This
  is the same host-flavour trap `D6-03` recorded for `ZoneInfo`, and it was caught
  by a new config test rather than by reasoning. `_is_absolute_path` asks
  `PurePosixPath` *and* `PureWindowsPath`, so the answer depends on the string and
  not on the machine reading it.
- **`cipher_or_503` was a function, not a dependency, and nothing said so.** This
  is the defect the new tests found, and it is worth recording because the code
  looked fine. `review.py:345,443` and `notifications.py:440` call
  `cipher_or_503(settings)` **directly**, passing settings they got from
  `Depends(app_settings)` themselves, so the parameter had never been introspected
  by FastAPI. The moment this route used it as `Depends(cipher_or_503)`, the bare
  `settings: Settings` became a **second body field**: every request answered 422
  naming `body.settings` and `body.payload`, and the body could not even be read as
  raw bytes. The fix is `= Depends(app_settings)`, which direct callers ignore. The
  general lesson is the one to keep: a helper that is only ever *called* is not
  thereby a *dependency*; those are two different contracts, the first is exercised
  by every test that calls it and the second only by a test that goes through the
  ASGI app.
- **413, 422 and 409 answer three different mistakes.** An oversized body is
  `413` with `DOCUMENT_TOO_LARGE` and **both** numbers, because "too large" without
  the limit is a message the caller cannot act on; a bad query value is `422`,
  the status FastAPI would have used had it been in the body; a duplicate is `409`
  naming the contract id that already holds that content, and no second outbox
  event is created. An empty body is structurally unreachable through this route
  (FastAPI rejects it as a missing field) and is kept in the store anyway,
  because the store is also used where no request parser runs first.
- **The cap bounds what is stored, not what is read.** `DOCUMENT_MAX_BYTES`
  (10 MiB, ceiling 100 MiB) is checked before anything is written, but FastAPI has
  already buffered the body by the time the store sees it, and the store holds the
  plaintext and the sealed copy in memory at once. The real defence against a
  memory-exhausting upload is a request-size limit at the proxy; this number is
  the bound on what the installation will keep, and it should not be read as the
  former.
- **The volume is on every service, because the API container is read-only.**
  `x-worker` and `api` both carry `read_only: true` with only `/tmp` on tmpfs, so
  a document write would fail at runtime rather than at boot — the worst kind of
  deployment bug. A named `documents` volume is mounted at an absolute
  `/var/lib/contract-agent/documents` on the API and all three workers, and
  compose names that absolute path as the default while `.env.example` keeps the
  relative `var/documents` that a checkout wants.
- **This doubles the producers of events that a default stack cannot publish.**
  With `EVENT_TRANSPORT=none`, `UnavailablePublisher` (`outbox_relay.py:60`) raises
  on every publish, `record_outcome` retries to `outbox_max_attempts` and then
  parks the row `failed`, so **every** ingested contract — and now every uploaded
  document — dead-letters and `python -m app.cli doctor` exits `EXIT_ATTENTION`.
  That is a pre-existing property of the default configuration, not a defect this
  slice introduced, but this slice adds a second way to reach it and the record
  should say so rather than let the next reader discover it from a red doctor run.
- **What this proves and what it does not.** Proven by running: `pytest` from
  `backend/` is **827 collected — 812 passed, 15 skipped**, of which
  `tests/test_documents.py` contributes **20** and `tests/test_config.py` two, the
  previous baseline being 805 collected / 804 passed / 1 skipped. The skips are
  environmental: 13 MySQL-unreachable, 1 Kafka live-broker opt-in, and 1 symlink
  test that Windows will not create without privileges. The blob on disk is
  asserted to contain no plaintext; a tampered blob raises rather than returning
  garbage; a round-trip is readable after a key rotation through the retired ring;
  and the upload route is exercised end to end through the real ASGI app for 201,
  409, 413, 422, role refusal, tenant sharing and the no-key 503. **No migration
  was added**: `storage_path` and `sha256` exist in `0001`, `ContractSource.PDF`
  already exists, so the head stays `0006`. Not proven: **no real PDF has ever been
  stored** — the payloads are plausible-sized byte strings, so "PDF" here means
  bytes with that content type and nothing has parsed one; **`storage_path` still
  has no reader**, so gap 5 remains blocked and this slice closes only the input
  half of gap 12; there is no upload screen, no proxy request-size limit, no
  per-tenant quota, no garbage collection for the unreferenced blobs the
  write-before-commit order can leave behind, and no re-wrapping of already sealed
  blobs during a rotation (they stay readable through `ENCRYPTION_KEY_PREVIOUS`
  until re-uploaded). Finally, two corrections to what this entry first
  recorded, kept visible rather than smoothed over. The harness's shell was dead
  while most of it was written — the same fault `D10-24` recorded — so the counts
  above came from an earlier run in the same session and the commit was left
  undone. Restarting the IDE cleared it: the suite was re-run from `backend/` and
  **exited 0 with 827 tests, 0 failures, 0 errors, 15 skipped**, matching the
  figures above (read from `--junit-xml`, because this shell drops pytest's final
  summary line under redirection). The slice landed as `a5b0f88`, and `git
  commit` ran the full 17-hook gate green — `ruff`, `ruff format`,
  `mypy --strict`, `bandit`, and the root and demo `tsc --noEmit` and `eslint`
  hooks all reported **Passed**, not "(no files to check)". So "not run" became
  "passed", which is the only way that record was allowed to change.

  One thing observed and deliberately not fixed: `test_escalation_scanner::
  test_run_forever_stops_when_the_event_is_set` failed **once in five full runs**
  with a SQLAlchemy `OperationalError`, and passed both in isolation and on every
  rerun. It is pre-existing, untouched by this slice, and recorded here rather
  than quietly re-run until green — a test that fails one time in five is a fact
  about the suite, not noise to be pressed out of the transcript.
  *(Diagnosed later, in `D10-30`: the fault was the suite's shared `StaticPool`
  connection, not the scanner. The test is fixed; this record is left as written,
  because "pre-existing" was true and merely incomplete.)*

### D10-27 — The contract aggregate gets a read side, and the cursor is its id

`GET /api/v1/contracts` now exists. Until this slice the aggregate root was
write-only: a contract could be ingested and its id echoed back once, and every
other per-contract route in v1 took that id on faith. The listing is what makes
the rest of the API reachable — an operator can find a contract without reading
its id out of an ingest response.

- **The cursor is `id`, and that is a correctness argument rather than a
  convenience.** `AgentDecisionRepository.recent` pages by `created_at` and needs
  a second column to break ties, because `created_at` is a MySQL `DATETIME`
  without fractional seconds: two rows written in the same second store the same
  timestamp, so "newest" stops being a total order and a keyset walk can skip or
  repeat a row. `Contract.id` is autoincrement, so `ORDER BY id DESC` *is*
  insertion order — a single column that is both the sort key and the cursor, with
  no tie to break. Paging is `WHERE id < after`, which touches each contract once
  even while new ones are being ingested underneath the scroll. `OFFSET` cannot
  give that, and it is the same reasoning the review queue uses (D8-06).
- **The cursor is a position, not an authorisation.** It is only ever combined
  with the tenant predicate from `scoped()`, so a guessed id from another
  organisation narrows this tenant's own rows instead of disclosing someone
  else's. Nothing in the route trusts the cursor to mean "this row is yours".
- **`limit + 1` rows are fetched so `next_cursor` is a fact about the database.**
  Asking for one row beyond the page is what distinguishes "there is more" from
  "the page looked full". Filling a page exactly is the common case, and inferring
  completeness from it would hand back a `next_cursor` that leads to an empty page
  every time a tenant's count is a multiple of `limit`.
- **The query parameter is `contract_status`, aliased to `status`.** The module
  imports `fastapi.status` and uses `status.HTTP_201_CREATED` throughout; a
  parameter named `status` would shadow it inside the function and turn the next
  such reference into an `AttributeError`. The alias keeps the wire name the
  obvious one without the local name being a trap.
- **`storage_path` is withheld and `has_document` answers the real question.**
  The path is an absolute path inside the API container: a viewer has no use for
  it, and it maps the deployment's filesystem for anyone who does. The only thing
  a caller needs from it is whether a document exists behind the row. The test
  asserts the path is absent from the raw response text, not merely from the
  model, so a field added by hand later cannot reintroduce it quietly.
- **`created_by` is withheld too.** An internal user id that nothing renders, and
  exposing it would let a viewer enumerate accounts by scrolling a contract list.
- **Viewer may read the listing.** Nothing in the payload is a customer value:
  `customer_ref` is the pseudonymous label the model already documents, and
  identity lives in encrypted `ExtractedField` rows this route never touches. The
  consent test proves `may_contact` and `consent_channels` track `PUT /consent`
  without any identity ever appearing — and because both are derived from the
  model rather than recomputed here, the flags a caller sees cannot disagree with
  what the decision policy will refuse.
- **`source`, `risk` and `status` are echoed as their enums, not strings.** The
  response model reuses `ContractStatus`, `ContractSource` and `RiskLevel`, so an
  unknown value fails validation at the boundary rather than reaching a client
  that has no branch for it.
- **`created_at` is re-attached to UTC in one place.** MySQL's `DATETIME` discards
  the offset, so `as_utc` is where it is put back — the same single point the
  decision view uses (D2-01).
- **No migration.** The status and consent columns are in `0001`; the only new
  code is a repository method, a page-limit pair and the route, so the head stays
  `0006`.

**What this proves and what it does not.** Proven by running:
`tests/test_contracts.py` collects **33 cases, 13 of them new** — ten new test
functions, two of which are parameterised (four `limit`/`cursor` bounds and one
status filter) — against 20 before. The full suite is **840 collected / 825
passed / 15 skipped / 0 failed, exit 0**, from a 827 / 812 / 15 baseline; the
skips are the same environmental fifteen. `ruff check` reports all checks passed
and `mypy` on both changed modules reports no issues.

  One figure was wrong on the way into this entry and is corrected rather than
  quietly overwritten: the new cases were first counted as *ten*, which happened
  to agree with the file's total because the pre-existing count was
  simultaneously taken as *twenty-three*. Two wrong numbers summing to the right
  answer is the failure mode that survives review, so the counts above were read
  out of `--junit-xml` per test case rather than inferred from the totals.

  The new tests cover keyset paging across a page boundary, `next_cursor` being
  `None` exactly at the end, a `limit` that exactly equals the remaining row
  count, status filtering, tenant isolation of the cursor, the absence of
  `storage_path` from the response text, viewer access, and the flags tracking a
  consent write. Not proven, stated rather than buried: **the frontend
`/contracts` screen has not been touched** — this slice is backend-only and
`frontend/src/lib/api.ts` has no consumer for the route yet, so nothing in the
product renders this listing; there is no search, no ordering other than
newest-first, and no `end_date` filter, so "which contracts expire soon" — the
question the product exists to answer — is still not answerable from this route.
**Gap 5 remains open**: the `contract.ingested` consumer is unwritten and still
blocked on an owner decision, and this listing is only its prerequisite.

  The slice landed as `67bf8f6`, and `git commit` ran the full 17-hook gate green
  over it — `ruff`, `ruff format`, `mypy --strict`, `bandit`, and the root and demo
  `tsc --noEmit` and `eslint` hooks each reported **Passed**, not "(no files to
  check)". The figures above were measured before that commit and no `backend/`
  file changed in between, which is the only reason they are still a statement
  about the commit and not merely about a snapshot of the working tree. The gate
  row in this entry's `STATUS.md` counterpart, and the hash itself, were added by
  the follow-up `docs(status)` commit rather than back-dated into the slice.

### D10-28 — The contract list gets a face, and it is deliberately read-only

`D10-27` gave the aggregate root a read side over HTTP and left it with no consumer:
`frontend/src/lib/api.ts` had no client for `GET /api/v1/contracts`, so the listing could
be curled and nothing in the product showed it. `/[locale]/contracts` is that consumer —
a status-filtered, cursor-paged list of the caller's tenant's contracts, linked from the
home screen rather than reachable only by typing a URL.

- **It reuses the data path the other screens already proved.** `useAuth().isAuthenticated`
  for the gate, `authorized()` from `@/lib/session` for the call, `useInfiniteQuery` for
  the pages — the same triple `review`, `escalations` and `agent` use. The page never sees
  a token: `authorized` hands one to the callback, dedupes a concurrent rotation behind a
  single in-flight promise, and retries once on 401. A screen that read the session store
  itself would be a second implementation of refresh, and the one place it would differ is
  the day the token expires mid-scroll.
- **The wire type is the redaction.** `ContractListItem` in `api.ts` omits `storage_path`
  and `created_by` because `ContractListItem` in `app/api/v1/contracts.py` withholds them.
  Modelling the omission rather than the server's full row makes rendering either one a
  compile error instead of a review catch — the same argument `D10-27` made for asserting
  the path's absence from the response text rather than from the model.
- **The enum→message-key maps hold nested paths, and the first draft of this slice got
  that wrong.** `STATUS_KEYS` and its three siblings are
  `as const satisfies Record<ContractStatus, string>`, so a status the backend adds is a
  compile error here rather than a raw key on screen. But they were first written against
  **flat** keys (`statusActive`, `sourcePdf`, …) while the translations were written as
  **nested** namespaces (`statuses.active`, `sources.pdf`, …). Both halves were valid JSON,
  both halves typechecked — `t()` receives a template literal here, not a literal, so
  nothing checks the value against the catalogue — and the screen would have rendered
  `contracts.statusActive` as visible text on every row. The maps now carry the nested
  paths. This is the class of defect a type system does not reach, which is why the slice
  was verified with a key audit (flatten both locales, intersect with what the page
  reaches) rather than by reading the screen.
- **Calendar dates and instants are formatted differently on purpose.** `start_date` and
  `end_date` are calendar dates — a contract that ends on the 31st ends on the 31st for
  everyone — so they are formatted with `timeZone: "UTC"`. `created_at` is an instant and
  is formatted in the reader's zone. Formatting a calendar date in local time moves it a
  day for anyone west of UTC, which is the same trap `D6-03` recorded for validating
  timezones by shape rather than by host rules.
- **Contactability is read, never recomputed.** The card shows `may_contact` and
  `consent_channels` as returned. Deriving them from `do_not_contact` and the four channel
  flags would be a second copy of the contact policy — and the copy on the screen is the
  one an operator would believe.
- **Read-only is a decision, not an omission.** No create action, because `POST /contracts`
  and `POST /contracts/document` have no screen and a button that posted a contract would
  be inventing an ingestion flow (fingering, source, timezone, dates) that no one has
  designed. **Partly superseded by `D10-32`, and left standing:** that flow has since been
  designed and the form now lives on this screen. What survives is narrower and still true —
  the *listing* completes nothing, and the choice that made it read-only was about not
  inventing a flow, not about ingestion being undesigned forever. No expiry countdown,
  because "expires in N days" needs the contract's own `timezone` and a product
  definition of the window, and the route accepts neither an
  `end_date` sort nor a filter to ask it. No search, because filtering one keyset page
  client-side would claim to search the tenant's contracts while holding a page of them.
- **`statusLabel` was deleted rather than left dead; `riskLabel` earned a field.** The
  screen shows status as a pill and the filter chip group already labels itself "Status",
  so a third "Status" string was a duplicate. Risk was rendering as a bare `low` after the
  source; it now has its own `<dt>`. Both locales were edited together, because
  `Messages = typeof en` makes `bn` a compile-time obligation rather than a convention.
- **The nav link leads, and the home copy was amended rather than deleted.** Every other
  per-contract screen takes an id, and until `D10-27` the only place to get one was an
  ingest response — so the listing is the entry point, and it is listed first. `home.body`
  said "three screens can complete work today"; that is still true (this one completes
  nothing), but a fourth screen now exists that the sentence implied did not, so it names
  it instead of being silently superseded.
- **No backend change, no migration, no new dependency.** `alembic` head stays `0006`, and
  the screen is built from `Button`, `Card` and the existing `globals.css` tokens.

**What this proves and what it does not.** Proven by running: a key audit over both
locales — **271 keys each**, zero only-in-en, zero only-in-bn, zero unused, zero
referenced-but-absent, zero placeholder drift, 47 `contracts` keys reached by the page.
Not proven, and stated rather than buried: **this repo has no frontend test runner**, so
no test covers this screen and none was written that could run; nothing in this session
rendered it in a browser or called the API from one. The screen is `next-intl` + TanStack
Query code that has passed a compiler and a linter and nothing more. The type/lint gate
and the commit hash are recorded by the follow-up `docs(status)` commit, the same
convention `D10-27` used. Also unchanged: **gap 5** — the `contract.ingested` consumer is
still unwritten and still blocked on an owner decision. A listing does not give a consumer
work it could correctly do; it only gives an operator a way to find a contract id.

  The slice landed as `07c31ab`, and `git commit` ran the full 17-hook gate green over it:
  `ruff`, `ruff format`, `mypy --strict`, `bandit`, `check json` (this slice edits two
  message catalogues) and the root **and** demo `tsc --noEmit` and `eslint` hooks each
  reported **Passed**, not "(no files to check)". The key audit figures above were measured
  before that commit and no `frontend/` source file changed in between — which is the only
  reason they still describe the commit rather than a snapshot of the working tree. The
  hash and the gate rows in this entry's `STATUS.md` counterpart were added by the
  follow-up `docs(status)` commit rather than back-dated into the slice.

  One correction to how the audit is described: the script that produced those counts lives
  in `.agent-cache/`, which is gitignored. The counts are reproducible, the command is not
  — a reader with this repo cannot re-run it as written, so `STATUS.md` says so instead of
  citing a path that does not exist in the tree.

### D10-29 — The notification queue gets a face, and the screen is not the policy

`notification` was the last v1 router that only a `TestClient` had ever called. That was
not a cosmetic gap: `NotificationScheduler` raises and parks rows on its own, so the queue
was a worker doing something an operator could neither read nor correct — the same shape
as `review` before `D10-22` and `escalation` before `D10-23`. `/[locale]/notifications`
is the consumer: the caller's tenant's rows, newest first, filterable by status and
channel, with a send and a requeue.

- **It reuses the data path the other screens already proved.** `useAuth().isAuthenticated`
  for the gate, `authorized()` from `@/lib/session` for the calls, `useQuery` +
  `useMutation` with the mutation's own `variables` for per-row error attribution. Same
  rule as `D10-22`: a page-level banner would blame every visible row for one refusal.
  `api.ts` is the only module that touches `fetch`; the page never sees a token.
- **The list is bounded, not paged, and that is the route's design rather than an
  omission.** `GET /notifications` calls `recent(limit=…)`; there is no cursor. Contracts
  needed one because a tenant may hold many contracts for years. A notification row is
  generated by a milestone and retired by dispatch, so the operational question is "what
  is stuck *now*", and a bounded newest-first window answers it. Writing `useInfiniteQuery`
  here would have implied a paging contract the server does not offer.
- **The recipient is never rendered.** The route does not return one — a row records
  *that* a contact was attempted, not who it reached — and the screen does not reconstruct
  it. The audited reveal on the review screen stays the only door to a contact value, which
  is the same line `D10-21` drew for the escalation queue.
- **`template` is displayed verbatim.** It is the milestone label (`T-60`…`T-14`), a
  configured string rather than a closed enum. Mapping it through a message key would
  silently miss for any cadence a deployment configures, so the stored value is shown.
- **`scheduled_date` is a calendar date and is formatted in UTC; instants are local.**
  Same split as `D10-28`, for the same reason: the row's date is the customer's reminder
  day as the server computed it, and re-rendering it in the reader's zone can move it a day.
- **Sending asks first; requeue does not.** `POST /{id}/dispatch` contacts a real customer
  immediately and cannot be undone, so it takes a confirmation that names the consequence.
  `POST /{id}/retry` only moves a parked row back to `pending` — the scheduler still has to
  find it due — and sending nothing. A dialog there would train people to click through
  dialogs before the one that matters.
- **The refusal map cannot be `satisfies`-checked, so it carries a fallback.** The status
  and channel maps are `as const satisfies Record<Enum, string>` and a new enum member is a
  compile error (`D10-28`). Refusal codes are plain `str` on the wire by design — the
  policy surface is open, and both `dispatch.py` and `notifications.py` mint codes — so the
  map is `Record<string, string>` and an unmapped code falls through to the generic failure
  sentence rather than rendering a raw key. The vocabulary was verified rather than
  assumed: **14** dispatch codes in `app/workers/dispatch.py` (`NOT_PENDING`,
  `NOT_YET_DUE`, `NOT_IN_WINDOW`, `MISSING_END_DATE`, `UNKNOWN_TIMEZONE`, `NEEDS_REVIEW`,
  `CHANNEL_NOT_CONSENTED`, `ATTEMPTS_EXHAUSTED`, `SENDER_UNAVAILABLE`,
  `RECIPIENT_UNAVAILABLE` plus the F9 four) and **5** in the route. The screen maps all 14
  plus the two its buttons can provoke (`NOT_FAILED`, `CONCURRENT_UPDATE`);
  `NOTIFICATION_EXISTS`, `NO_DECISION` and `DECISION_MISMATCH` come only from
  `POST /notifications`, which has no button here, and `TENANT_MISMATCH` only ever lands on
  `last_error`.
- **`refusalNeedsReview` said the wrong thing, and the loose end was closed by reading the
  raise sites.** `NEEDS_REVIEW` (`dispatch.py:500-504`) means *the agent decision is
  flagged for human review*; `PIPELINE_BLOCKED` (`dispatch.py:509-518`) means *extracted
  fields are unapproved*. The message first described the second as the first, which would
  have told an operator to re-review a field when the thing to clear was a flagged
  decision. Both sentences now match the code that raises them.

**The defect this slice caught in a screen that was already committed.** `useTranslations()`
is called with no argument, so `t` is root-scoped. The filter chips passed
namespace-relative map values — `t(STATUS_KEYS[candidate])` → `"statuses.pending"` →
rendered that literal string as the chip label. Grepping the pattern found the identical
bug at `contracts/page.tsx:199`, shipped in `07c31ab`, in a file that prefixes correctly two
lines away for the row status. Both now use the prefixed template, so the map stays
compile-checked and the key resolves. Neither `tsc` nor `eslint` could catch this: `t()`
takes a template literal here, so there is nothing to match against the catalogue — which is
why the slice was verified with a key audit rather than by reading the screen.

**What this proves and what it does not.** Proven by running: a key audit over both locales
— **337 keys each**, 66 under `notifications`, zero one-sided, zero unused, zero
referenced-but-absent, zero placeholder drift, zero bare map keys passed to `t()`. Not
proven, and stated rather than buried: **this repo has no frontend test runner**, so no test
covers this screen and none was written that could run, and nothing rendered it in a browser
or called the API from one. It has passed a compiler and a linter and nothing more. The
audit script lives in `.agent-cache/` and is deleted before commit, so the counts are
reproducible only on the machine that ran them — the same caveat `D10-28` recorded.

- **No backend change, no migration, no new dependency.** `alembic` head stays `0006`.
  `notifications` needed no route added; it needed a reader.
- **Gap 25 is now closer, not further.** A default stack runs `EVENT_TRANSPORT=none`, parks
  every event, and `doctor` exits `EXIT_ATTENTION`. This screen makes the *notification*
  half of that state legible and correctable; it does nothing for the outbox half.

**Commit gate, recorded after the fact.** The slice landed as `bcfce75`. Nineteen hooks are
defined in `.pre-commit-config.yaml`; `gitleaks` carries `stages: [manual]` and never joins a
commit, so the gate is eighteen hooks — of which **seventeen reported Passed** and
`forbid-submodules` reported `(no files to check) Skipped`. Saying "the 17-hook gate" without
that sentence would round a skipped hook into a passed one; the earlier slices' shorthand is
correct in the count of *passes* but was not specific about the remainder. The figures above
were measured before the commit and no `frontend/` source file changed in between, which is
the only reason they are a statement about `bcfce75` rather than about a working tree that
resembled it. The hash and the gate rows in this entry's `STATUS.md` counterpart were added
by the follow-up `docs(status)` commit rather than back-dated into the slice — the same
convention `D10-26`…`D10-28` used.

### D10-30 — The scanner "flake" was a harness fault, and `run_forever` was never wrong

Five slices recorded the same line and nobody diagnosed it: `test_escalation_scanner::
test_run_forever_stops_when_the_event_is_set` failed about **once in five full runs** with a
SQLAlchemy `OperationalError`, then passed in isolation and on every rerun. Each record called
it *pre-existing* and *left visible*. That was honest about who owned it and silent about what
it was — a placeholder for a diagnosis not yet made. It is now diagnosed, and the diagnosis
exonerates the code under test.

**The mechanism, measured rather than inferred.** `db_engine` builds
`sqlite+pysqlite:///:memory:` with `poolclass=StaticPool`. `StaticPool` is deliberate: without
it a second Session gets a *different* empty database, and the suite's multi-session tests
could not work at all. The cost is that every Session in a test — in **every thread** — is
handed the **same** `sqlite3.Connection`. `.agent-cache/probe_staticpool_threads.py` printed
identical connection object ids from two threads, then drove one connection from two threads
concurrently until it faulted: `SystemError: <built-in method commit of sqlite3.Connection …
returned NULL`, at roughly **one statement in a thousand**. That rate is exactly why it looked
like noise: a test that runs a few statements has a small chance of tripping it, and a full
suite has enough chances to trip it once in five runs.

**The old test was the trigger.** It started `run_forever` in a daemon thread and, from the
**main** thread, polled `escalations_for(...)` in a loop while the worker committed — two
threads on one connection, precisely the pattern the probe showed to be unsafe. The failure was
the harness's, not the scanner's: `run_forever` already honours `stop.wait()` and already backs
off between passes, and **no production file changed** in this slice.

**It was one site, not a pattern.** The only other test that starts a thread is
`test_outbox_relay::test_run_forever_processes_rows_then_stops`, and it polls
`publisher.published` — an in-memory list the publishing thread appends to — so it never races
the database. Checking that before changing anything is what kept this from becoming a
suite-wide refactor of a non-problem.

**The fix is in the observation, not the subject.** The test now wraps `run_once` via
`monkeypatch` so the **worker's own thread** sets a `threading.Event` when a pass opens an
escalation, waits on that event from the main thread, and reads the escalation rows **after**
`join()`. It still fails if the loop does not do the work — the assertion is on behaviour with a
10-second ceiling, not on elapsed time — but the main thread no longer touches the database
while the worker does. `conftest.py`'s `db_engine` docstring now states the single-connection
constraint outright and names the one test that starts a thread, so the next person adding a
threaded test meets the rule before the fault rather than after it.

**Verified by running.** The repaired test passed **30/30 rounds** under an in-process repeat
driver (`.agent-cache/repeat_test.py`), which amortises interpreter and plugin startup so thirty
repetitions cost one suite's worth of time. The full suite then ran from `backend/` and exited
**0: 840 tests, 0 failures, 0 errors, 15 skipped — 825 passed** (JUnit XML, because this shell
drops pytest's summary line under redirection). That is **identical to the pre-fix baseline**,
and the identity is the point: the test was never legitimately failing, so removing a fault from
it changes no count. A predicted 826 passes, stated earlier in this session, was an arithmetic
error on my part — 840 collected minus 15 skipped is 825, and there was no skipped test to
revive.

**Two limits, stated rather than smoothed over.** First, the A/B control — the *old* body run
under the same repeat driver to measure its per-run failure rate directly — was written as
`tests/test_zz_flake_probe.py`, and it was **deleted unrun** before any commit, because it was
an untracked module inside the `tests` package and would have been collected by the suite. So
the "~1 in 1000 statements" figure rests on the probe's contention experiment, not on a measured
failure rate for that specific test across runs. The structural argument does not depend on it:
the probe demonstrated the shared connection and the fault, and the old test demonstrably did
the unsafe thing. Second, this slice touched **two test files and no documentation outside this
entry and its `STATUS.md` counterpart**; the backend suite was re-run here because test code
changed, and the commit gate result for it is recorded in the follow-up `docs(status)` commit,
not back-dated into this one.

### D10-31 — The account routes get a screen, and provisioning stops being a hand-written request

The four `/auth/users` routes were the last ones only `curl` had reached. That was not a
cosmetic gap either, and it was a sharper one than `D10-29`'s: they are **load-bearing**.
`login` fails for a tenant with no user, so the first account comes from `create-admin` or from
`POST /auth/users` and nowhere else, and every later colleague — a promotion, an account
switched off, a temporary password after a lockout — was a request someone typed by hand.
`/[locale]/users` is the consumer: the caller's tenant's accounts, a create form, three role
chips per row, activate/deactivate, and a password reset.

- **It reuses the data path the other five screens already proved.** `useAuth()` for the
  session, `authorized()` from `@/lib/session` for the calls, `useQuery` + `useMutation` with the
  mutation's own `variables` for per-row error attribution, `api.ts` as the only module that
  touches `fetch`. Same rule as `D10-22`/`D10-29`: one shared banner would blame every visible
  row for one refusal.
- **Admin only, and the page says why.** All four routes are `require_roles(UserRole.ADMIN)`.
  The query is not even *fired* for anyone else (`enabled: isAdmin`) — a 403 an operator can do
  nothing about is noise, not information — and the non-admin branch explains the rule instead
  of showing an empty list that looks like an organisation with no accounts.
- **The caller's own row offers no controls, and that is not the enforcement.** The server
  answers `409 _SELF_CHANGE_REFUSED` on both `PATCH /users/{id}` and
  `POST /users/{id}/password-reset`. Hiding the buttons only stops the screen offering an action
  whose single possible outcome is a refusal; the route still decides, which is why the card
  also keeps the reason visible (`users.selfNote`) rather than silently missing controls.
- **A role change and a deactivation are one action with one consequence.** Both end the
  target's sessions: `update_user` calls `_end_sessions`, which bumps `token_epoch` and revokes
  the refresh family, because the role travels *inside* the access token and `require_roles`
  reads it from there. The screen states that once, above the list, instead of pretending the
  change is invisible until the next request.
- **A reset is a handover, not a password choice.** The request takes no body: the server calls
  `generate_password()` (24 url-safe characters, past `MIN_PASSWORD_LENGTH = 12`) and the
  response is the **only** copy that will ever exist — the hash is all that is stored, nothing
  logs it, and there is no mail transport to resend it. So the screen renders it at **page
  level**, above the list, because a row-level block would vanish with the row on the next
  refetch and take the one copy of the value with it.
- **The role chips are the whole control.** Three buttons showing the current value by being
  `primary` — the idiom the status filters already use — because this design system has no
  `<select>` primitive and inventing one for this screen would be a new pattern for one page.
  `ROLE_KEYS` is `as const satisfies Record<UserRole, string>`, so a new member of
  `app.db.enums.UserRole` becomes a compile error here instead of a missing key at run time, and
  the call site is a **prefixed** template (`t(\`users.${ROLE_KEYS[candidate]}\`)`) — the
  root-scoped-`t()` defect from `D10-29` cannot recur in this file.
- **Errors key on status, never on English prose.** These four routes carry human `detail`
  strings rather than machine codes, so matching text would break the first time a message was
  reworded. The two 409s the screen can still provoke (own account, inactive target) are
  already prevented by the cards that offer the buttons, so 409 gets one generic message.
- **422 is the real validation status, and this slice corrected a claim that said 400.**
  `CreateUserRequest.password` carries `Field(min_length=MIN_PASSWORD_LENGTH)`, and
  `main.py`'s `RequestValidationError` handler answers **422** with the app's error envelope —
  so a too-short password or a malformed email lands on 422. The first draft of this screen
  mapped 400 only, which meant the likeliest first mistake an administrator makes fell through
  to the generic failure. 400 is kept for `_NOTHING_TO_CHANGE`, which this screen cannot
  provoke because every mutation sends at least one field.

**The parity rule earned its keep here.** `i18n/messages.ts` deliberately does **not** cast `bn`
to `typeof en`, so a namespace present in one locale and absent from the other is a *compile*
error rather than a raw key on a Bengali screen. That is exactly what a missing `users` block
turned out to be — found by the typechecker, not by a user. Measured after the fix: **44** keys
under `users` in each locale, **0** one-sided, **0** referenced-but-absent, **0** present-but-
unused, **382** keys total in each file, **0** placeholder drift across the whole file. `bn`'s
`home.body` was also stale — it still said the notifications screen was pending, and described
three working screens — while `en`'s equivalent had already been corrected in `bcfce75`; both
now agree with the five-screens-plus-admins-sixth structure the app actually has.

**No backend change and no migration.** The routes, the audit actions
(`ACTION_USER_UPDATED`, `ACTION_USER_CREDENTIAL_RESET`) and the refusal constants all shipped
with the account-lifecycle slice; this adds a client, four typed methods in `api.ts`
(`users.list/create/update/resetPassword`) and the `PasswordReset` wire type. The
`MIN_PASSWORD_LENGTH = 12` in the page is a **hint only** — the server is the authority, the
constant is not on the wire, and the submit button stays enabled so the screen cannot be wrong
the day the server's floor moves.

**Verified, and one honest limit.** The typecheck and the linter pass on the three touched
files, and the key counts above were measured rather than estimated. There is still **no
frontend test runner in this repo**, so nothing here is tested and no browser has rendered the
screen: "verified" means a compiler and a linter, not a rendered page.

### D10-32 — Ingestion gets a client, and an upload needs a body the client could not send

`/contracts` could list contracts that nothing in the browser was able to create. The two routes
that mint the aggregate every other screen takes by id — `POST /contracts` and
`POST /contracts/document` — had only ever been reached by `curl`, so the first row on a fresh
deployment still came from a hand-written request. `/[locale]/contracts` now carries the form.

- **The client physically could not send a document.** `request()` had one body kind: it
  `JSON.stringify`'d whatever it was given and announced `application/json`.
  `POST /contracts/document` declares `DOCUMENT_MEDIA_TYPE = "application/octet-stream"`, parses
  raw bytes, and takes its four metadata fields as **query parameters** — not multipart, not
  JSON. So `RequestOptions` gained `upload?: { blob, contentType }` as a *sibling* of `body`
  rather than a flag on it: two body kinds, one of which is not encodable, and a boolean would
  have left `JSON.stringify` one `if` away from a corrupt upload. `upload` wins if both are set,
  and `contentType === undefined` is what "no body at all" means, so the header spreads on it.
- **Two routes, one form.** They take the same four content fields and differ only in transport,
  so attaching a file is what chooses the route. Two screens would have been two copies of the
  date and time-zone rules, and the second copy is the one that drifts.
- **No source picker, on purpose.** The server stamps `pdf` on the document route and defaults to
  `api` on the metadata one. A picker would let a client label a metadata-only row as having come
  from a document that was never sent — a claim about provenance the server is better placed to
  make, since it is the only party that knows whether bytes arrived.
- **Nothing about the term is validated in the browser.** `_clean_timezone`, `_check_date_order`
  and pydantic's date parsing already refuse a bad zone, an inverted term and a malformed date,
  for both routes, in one place. A client-side copy would be the copy that goes stale — the same
  reasoning that kept `MIN_PASSWORD_LENGTH` a hint in `D10-31`.
- **Empty is omitted, not sent empty.** An untouched date input yields `""`, which is not a date
  and would be a 422 rather than "not recorded", so `submitIngest` removes the key instead.
- **Refusals key on status, never on prose.** Neither route returns a machine code; both answer
  with a human `detail` string. The four a person can provoke: **409** (already held — by
  metadata fingerprint on one route, by document hash on the other, which is why the message
  names both), **413** (over the store's cap), **422** (dates or zone), **503**
  (`document_store_or_503` — no `ENCRYPTION_KEY` or no `DOCUMENT_STORE`, a deployment state
  rather than a mistake in what was typed, so it gets its own sentence).
- **Role-gated by hiding, not by trusting.** Both routes are `require_roles(ADMIN, OPERATOR)`; a
  viewer gets 403. The form is not rendered for one, and a note explains the absence — the same
  choice as the review screen, and the route still decides.
- **The file input is remounted to be cleared.** A file input's value cannot be controlled, so
  clearing `document` in state leaves the filename on screen for a file already uploaded; the
  input is keyed on a counter bumped in `onSuccess`.

**Gap 12 is only half closed, and the slice says so.** `POST /contracts/document` now has a
caller and the document is sealed, addressed and kept — but nothing parses it. `app/cli.py`
still documents that `extract` takes a `--text-file` because there is no upload-to-text path, so
a PDF uploaded here reaches extraction only after a shell step. The form therefore promises that
the *original* is kept, not that its contents are read; `contracts.ingestedNote` says extraction
is a separate step. **Amended, not deleted (`D10-38`):** `extract` now takes the document's own
stored bytes when `--text-file` is omitted, so "nothing parses it" and "there is no
upload-to-text path" are no longer true. What survives is the narrower claim both sentences were
really about: nothing *automatic* parses it — the step is still a command, not an upload hook,
and `contracts.ingestedNote` still says extraction is separate.

**No backend change and no migration.** Two typed methods in `api.ts`
(`contracts.ingest/ingestDocument`), the `ContractSummary` and `ContractIngestRequest` wire
types, one `upload` branch in `request()`, and the form. Measured after the change: **402**
leaf keys per locale, **0** one-sided, **0** placeholder drift, **20** `contracts.ingest*` keys
present in both files and all **20** referenced by the page.

**Verified, with the usual limit.** Typecheck and lint pass on the four touched files; the key
counts above came from a script over both catalogues, not from reading. There is still no
frontend test runner here, so nothing tests this form and **no browser has rendered it**.

### D10-33 — Consent gets an editor, and the screen refuses to re-derive contactability

`PUT /contracts/{id}/consent` was built in F9, tested in two suites, version-checked and audited
— and had no caller in a browser. The four flags and `do_not_contact` all default to `false`, so
on a fresh deployment every contract is un-contactable. That is the right fail-closed default
and it is not a state a product can ship in: the decision worker's answer to everything is
`NO_CONSENT`, and until now the only ways to change that were `curl` and
`python -m app.cli consent-import`. The per-contract editor now sits on each card at
`/[locale]/contracts`. Gap 18's owner decision — which `D10-19` explicitly deferred — is taken.

- **The whole state, every time.** All four flags plus the `version` the row was rendered from.
  The route *replaces* consent rather than patching a flag (D9-01), because a partial body
  cannot express a withdrawal and cannot be told apart from one. The version travels from the
  row rather than from component state, so it cannot age differently from the flags beside it.
  The consequence: the route's 422 (a missing flag, `version < 1`) is **unreachable from this
  form** — there is no free-text input to get wrong, which is why the screen maps only 409, 403,
  404 and offline.
- **`do_not_contact` is not a fourth channel.** It outranks all three grants, so listing it among
  them would suggest a customer can be opted out of email while still consented to SMS. It gets
  its own control and its own sentence.
- **Contactability is read, never recomputed.** `may_contact` and `consent_channels` are
  properties of the model, and they are what the decision policy enforces. Rebuilding either
  from four booleans in the browser would be a second contact policy free to disagree with the
  first, and the disagreement would show up as a reminder sent to somebody who had not agreed to
  it.
- **A no-op is a reassuring answer, not a failure.** The route writes nothing and answers the
  same `version` when the submitted flags already match what is stored — no version bump, no
  audit row. The editor compares the returned version with the one it sent: equal means "already
  recorded", higher means "recorded". Without that distinction, saving unchanged flags and a
  swallowed click look identical on screen.
- **409 reloads before the next attempt.** On a version conflict the page invalidates the
  contract list, which re-renders the editor from the stored row (keyed on `row.version`), so the
  draft cannot be re-submitted against a picture that has already changed. The 409 body carries
  `details.current`, and it is deliberately **not** painted into the draft: a draft patched from
  a conflict response would be the browser deciding what the new consent state should be. The
  server already decided; the screen's job is to show it.
- **404, not 403, for another tenant's contract.** The repository's `get` returns `None` outside
  the caller's tenant, so the id cannot be used to probe which contracts exist elsewhere. A
  viewer does get 403 — from the route, which is the party that decides.
- **Role-gated by hiding, the route still deciding.** `require_roles(ADMIN, OPERATOR)`; a viewer
  keeps the read-only derived contact line and gets a note in place of the controls, so nothing
  is withheld except the writing.
- **This is not a bulk tool.** `consent-import` stays the only backfill: ticking four boxes per
  contract across a sheet would be a bulk write to the most sensitive state in the product,
  without the dry run, the all-or-nothing validation or the closed cell vocabulary that make the
  CLI path auditable. The editor and the sheet are complements, not duplicates.

**No backend change and no migration.** `RequestOptions.method` gained `"PUT"` — the union did
not carry it — `ConsentState` mirrors `ConsentView` including the two derived fields,
`ConsentUpdateRequest` makes all four flags **required** beside `version`, and
`contracts.updateConsent` wraps the route. `alembic` head stays `0006`.

**Verified, with the same limit `D10-32` recorded.** Typecheck and lint pass; both catalogues
carry 14 `contracts.consent*` keys at the same offsets and the page reaches all 14. There is
still no frontend test runner in this repo, so nothing tests this editor, and **no browser has
rendered it or saved consent from it** — a compiler and a linter, nothing more.

**Gap 18 is narrowed, not closed.** Consent is now changeable from the product one contract at a
time; a fresh database still consents nothing by default, and the bulk backfill still needs the
sheet and somebody to own it.

### D10-34 — A contract gets an address, and the detail read is the listing's own serializer

Ingestion, the consent editor and the review queue all address a contract **by id**, and until now
an id read off the contracts list was usable nowhere: there was no route that read one contract and
no screen to open it on. Every per-contract route below took its id on faith. `GET
/contracts/{contract_id}` and the page at `/[locale]/contracts/[id]` close that: a listing row is
now an address, and the card that shows a contract links to the page that explains it.

- **One serializer, two callers.** The route answers `ContractListItem` — the listing's own model,
  built by the same `_list_item` — rather than a second "detail" model. A detail model is how a
  summary and the page it links to start disagreeing field by field, and the fields most likely to
  drift are the derived ones: `may_contact`, `consent_channels` and `has_document` are model
  properties, and a second model would be free to compute them its own way. The consequence is
  testable as an assertion about the whole body: the detail read equals the listing's first item,
  as a dict, after a consent write.
- **The widest read role is `VIEWER`.** The listing already shows these columns to a viewer, so
  refusing the single read would hide nothing — it would only make the id un-openable. `storage_path`
  and `created_by` are withheld once, in `ContractListItem`, and are therefore withheld here.
- **404, not 403, for another tenant's contract.** `TenantScopedRepository.get` returns `None`
  outside the caller's tenant, so the route cannot be used to probe which contract ids exist
  elsewhere — the same answer `/contracts/{id}/fields` already gives, and the reason the page's
  `isForbidden` branch is reachable only by a genuinely under-privileged role.
- **Registered before the literal paths.** `/{contract_id}` is declared above `POST ""`,
  `POST /document` and the `/{contract_id}/…` routes. FastAPI matches in registration order and a
  `POST`/`PUT` cannot be shadowed by a `GET`, but keeping the int path above the literal ones means
  the ordering argument does not have to be re-derived the day one of them changes method.
- **`ContractRepository` is used only as the tenant-scoping seam.** No repository method was added:
  `get` already exists on the base class and already scopes. The slice is a route, a model reuse and
  a test block, not a second query path.
- **The fields read existed and had no client.** `GET /contracts/{id}/fields` was built in F7 and
  never called, so the extraction state of a contract was invisible from the product. `ContractField`
  carries **no `value`** and there is no field to add one to: the route is value-free by `D7-05`, and
  revealing one value is the audited `POST /review/{id}/reveal`. The page says that in place of the
  column rather than leaving an empty cell that reads as a defect.
- **`blocks_pipeline` and `blocking` are read, not counted.** Both come from the model, so the page
  and the review queue cannot disagree about whether a contract can be contacted. Counting
  `status === "approved"` in the browser would be a second definition of the pipeline gate.
- **What the page deliberately is not.** It does not edit consent — one screen owns that write, so
  there is one place where a stale version can be submitted and one place to fix, which is why the
  four flags appear as read-only text with a pointer to the list. It does not re-derive
  contactability from those flags. It does not compute "expires in N days": that is measured on the
  **contract's** clock, which is why the row carries a time zone, and deriving it in the browser
  would use the reader's zone and could be a day out. It does not link to agent decisions,
  notifications or escalations, because none of those screens reads a contract out of its address
  yet — a query string would look like a filter and silently show everything, so the card says why
  instead. **Amended, not deleted (`D10-35`):** the card now states all three reads' counts, and its
  links still go unfiltered, for exactly the reason above.
- **Field vocabularies are borrowed, not copied.** Field names and review states are labelled from
  the `review.*` namespace. Duplicating them into `contracts.*` would give one enum two
  vocabularies, and the screen that decides a field and the screen that reports it would eventually
  call the same state different things.
- **The route segment is validated, not coerced.** `Number.parseInt` plus a `/^\d+$/` guard, because
  the path parameter is an `int` server-side: `"12abc"` must be an invalid id rather than silently
  reading as 12, and an empty segment must never become 0.

**No schema change and no repository method.** `alembic` head stays `0006`. `ContractField` and
`ContractFields` mirror `ExtractedFieldSummary`/`ContractFieldsResponse`, and `contracts.get` /
`contracts.fields` are the two new calls.

**Verified.** `pytest tests/test_contracts.py` → **39 passed**, six of them new: the whole-dict
equality with the listing, `storage_path` withheld from body and raw text, cross-tenant 404 in
**both** directions, an unknown id as 404, a viewer reaching the row, and an unauthenticated read as
401. Both catalogues carry **434** leaf keys with `enOnly`/`bnOnly` empty and no placeholder drift
in either direction; the page reaches **68** message keys, all defined in both locales, **18** of
them new under `contracts.*` and every one of them reached. No bare map value is passed to `t()`
(the `07c31ab` trap).

**The same limit `D10-32` and `D10-33` recorded.** There is no frontend test runner in this repo, so
nothing tests this page, and **no browser has rendered it** — a compiler and a linter, nothing more.
The key counts are a script's output, not a reading of two translated files by somebody who reads
Bengali. Gap 12 stays narrowed rather than closed: an uploaded document now has a contract page, but
nothing parses it — `extract` still takes `--text-file`. **Amended, not deleted (`D10-38`):** the
last two clauses are now false. `extract` takes the contract's own stored document when
`--text-file` is omitted, so something does parse it; what survives is that nothing parses it
*without being asked* — no upload triggers extraction.

### D10-35 — The detail page stops saying its neighbours' reads are unreachable

`D10-34` left the new page with a card that explained, in prose, why it offered no links: agent
decisions, notifications and escalations each had their own screen, and none of those screens read a
contract out of its address. Two of the three reads **already took a contract filter** —
`GET /agent/decisions?contract_id=` and `GET /notifications?contract_id=` were built with the routes
that own them. The escalation queue was the one that could not be narrowed, so "which of the tenant's
escalations are *this* contract's" was answerable only by reading the whole queue and matching
`contract_id` by eye. The slice is one query parameter, one response field, and the card that can now
answer.

- **The filter lives in `queue()`, not in a second `for_contract` method.** `EscalationRepository.queue`
  gained `contract_id: int | None`, and "oldest deadline first" stays decided in exactly one place.
  A parallel method would be a copy of the ordering rule, free to drift from the queue screen's — and
  the whole point of this card is that it and the queue cannot disagree about what is late.
- **The filter is applied after `scoped()`.** The tenant predicate is built first and the contract id
  narrows what it returned, so another tenant's id is an **empty list** rather than their rows and
  cannot be used to probe which contract ids exist. That is the same opacity `GET /agent/decisions`
  and `GET /notifications` give their own filters.
- **Naming a contract returns closed rows too.** `?status=` defaults to "every state" and a working
  queue filters it down; a contract page asks the opposite question — "what happened here" — so
  hiding a resolved escalation would answer it with half a history. The route's docstring says so,
  because the two filters read alike and answer differently.
- **Empty list, not 404, for a foreign id — and the single read stays 404.** These look
  inconsistent and are not. `GET /contracts/{id}` is an *address*: it either names a row the caller
  may open or it does not, so 404 is the honest answer. A list filter is not an address; it can only
  say "no rows match", and answering 404 would require a second existence query the queue does not
  make — turning a filter into a permission probe while pretending not to.
- **`contract_id` is echoed on the list response.** Same convention as `AgentDecisionListResponse`:
  without the echo a page cannot tell "this contract has none" from "the queue is empty", and would
  have to trust its own query string instead of the answer it received.
- **Three separate queries on the page, not one combined call.** The card and the queue screen parse
  the same response types from the same routes, and nothing is folded into a bespoke endpoint: a
  contract summary that aggregated all three would be a sixth read path with its own serializer, and
  the first thing to drift. They are separate `useQuery` calls because they fail independently — a
  failure in one must not blank the other two.
- **Every number is the server's.** The counts are each response's own `count`, and "past their
  deadline" is each row's `overdue`, summed by the route. Nothing is derived in the browser from
  `sla_deadline`: that column is a timestamp the server compares against *its* clock, and a client
  that recomputed it would disagree with the queue screen by whatever the clock skew is.
- **The links still go unfiltered, deliberately.** The three screens do not read a contract out of
  their address, so `?contract=12` would look like a filter and show the tenant's whole queue. The
  card says that in the note under its heading rather than presenting three links that lie. Adding
  the address filters would be a routing decision on the screens that own them, not a ride-along here.
  **Amended, not deleted (`D10-36`):** that routing decision was then taken on all three screens, so
  the card's links now carry `?contract=` and the note says what the scope does rather than why there
  is none. The clause is kept because it is the reason the parameter had to be honoured *before* the
  links could be filtered — not the other way round.
- **`D10-34`'s "it does not link" bullet is amended, not deleted.** The clause was true when written
  and the reason it gave — a query string that silently shows everything — is still why the links are
  plain. **Amended again (`D10-36`):** the links are no longer plain; the reason survives as the
  precondition that made filtering them safe.

**No schema change and no repository method beyond the one parameter.** `alembic` head stays `0006`.
`EscalationQuery` in `api.ts` gained `contractId`, and `escalations.queue` grew the options object the
notifications client already used, which is why its one existing call site changed.

**Verified.** `pytest tests/test_escalations.py` → **52 passed**, three of them new: narrowing returns
only that contract's rows in deadline order *including* the resolved one, with the echo; a
cross-tenant id answers an empty list and echoes the caller's own id back; and a viewer may read a
filtered queue. Both catalogues carry **444** leaf keys with `enOnly []`, `bnOnly []` and **0**
placeholder drift; the page reaches **86** message keys, all defined in both locales, and all **16**
`contracts.detail*` keys are reached.

**The same limit again, recorded rather than smoothed over.** No frontend test runner, so nothing
tests this card and **no browser has rendered it**. The counts above are a script's output, not a
reading of the Bengali catalogue by somebody who reads Bengali. The filter is additive: the queue
screen still reads the tenant-wide list, so nothing on that screen exercises the new parameter. Gap 5
is untouched. **Amended, not deleted (`D10-36`):** the queue screen now reads `?contract=` out of its
own address, so the parameter has a caller in the product and the sentence above is history.

### D10-36 — A contract's scope is a fact about the address, not about the page

`D10-35` ended with a card whose three links were deliberately plain, because no screen read a
contract out of its address. The wire had been ready for two of the three since the routes that own
them were built, and the escalation queue gained the parameter in `5421338` — which left a
parameter with tests and **no caller in the product at all**. This slice gives it three callers and
the contract page's three links, so "which of these are *this* contract's" is answerable by clicking
a link instead of by reading a whole queue and matching ids by eye.

- **The scope lives in the query string, not in `useState`.** Local state survives a click and
  nothing else: not a reload, not a bookmark, not the back button, and not the copy-paste that sends
  the address to a colleague. A link whose filter silently evaporates is worse than no link, which is
  exactly the objection `D10-35` recorded, so the parameter had to be honoured *before* the links
  could be filtered rather than after.
- **One module spells it, in both directions.** `frontend/src/lib/contract-scope.ts` holds
  `readContractScope` (address → scope) and `withContractScope` (scope → address) beside
  `contractIdFromInput` (typed id → scope). A parameter written by one screen and parsed by another
  is the classic place for a rename to half-land, so the name is a constant and there is no second
  spelling of `"contract"` anywhere: the detail page builds its links with the same function the
  screens parse with.
- **The parameter is `contract`, not the wire's `contract_id`.** The address bar is the reader's
  vocabulary; `contract_id` is the route's. Naming the address after the wire would freeze a transport
  name into every bookmark, and the agent screen's route already proves the two can differ without
  anyone being confused.
- **Three outcomes, not `number | undefined`.** "The address names no contract" and "the address
  names something that cannot be a contract" call for two different screens, so the type is
  `none | contract | invalid`. Falling through to the tenant-wide list on a malformed filter would
  answer a question nobody asked — the same lie as ignoring the filter, only louder — so an invalid
  address shows a card saying so and the query stays `enabled: false` rather than widening.
- **Parsing is hand-checked, for `D10-34`'s reason.** `/^[1-9][0-9]*$/` then `Number.isSafeInteger`,
  never `Number(raw)`: `Number("12abc")` is `12` and `Number("")` is `0`, so a malformed address would
  quietly become a real filter on a contract nobody named. The server's path parameter is an `int`
  and its query parameters are `gt=0`, so the browser tests the shape the server enforces.
- **The way in and the way out test the same shape.** `contractIdFromInput` is the single answer to
  "what is a usable typed id", and `inputNamesAContract` disables the submit button with that same
  predicate — so a typed id can never be one the screen then calls malformed on its way back out.
  A second regex for the button would have been free to drift from the one that parses.
- **`?resolved=1`, and only `1`.** The escalation queue already had a resolved toggle; it is now the
  address rather than local state, for the same reason as the scope. Anything other than `1` — `0`,
  `true`, a typo — means the working queue: a value this module did not write is not a filter
  somebody asked for, and reading `resolved=false` as "yes" would be the surprise.
- **`router.replace`, not `push`.** Toggling a filter twice should not leave two history entries for
  one question. The address is the description of what is listed; it is not a trail of keystrokes.
- **Filters compose, so applying one cannot drop the other.** The escalations form navigates to
  `withResolved(withContractScope(base, id), showResolved)`. Setting a contract through a helper that
  answered only its own parameter would have cleared the resolved flag on the way.
- **The scope sentence reads the server's echo.** `EscalationQueue` and `NotificationList` both carry
  `contract_id`, so the screen says "these are contract 12's" from the answer it received rather than
  from its own query string. If the two ever disagree, the server's filter is the one that happened.
- **The draft box follows the address.** The input is `useState` seeded from the address and re-synced
  by an effect on it, so a link, the back button or a typed address moves the box without a remount —
  and the box is a draft until submitted, so half-typed digits never navigate.
- **Each route is a `Suspense` shell over a screen.** `useSearchParams` suspends while the address is
  resolved, so the boundary has to exist for the route to prerender. Keeping the shell thin means the
  hooks live in one component and the boundary is stated once per route instead of being rediscovered
  the next time a page reaches for the address.

**No backend change and no schema change.** `contract_id` was already on both list routes before this
slice and the escalation parameter landed in `5421338`; `alembic` head stays `0006`. `api.ts` gained
`NotificationList.contract_id` — a field the route has always sent.

**Verified.** Both catalogues carry **464** leaf keys with `enOnly []`, `bnOnly []` and **0**
placeholder drift; every `t()` key the four screens reference exists in both, **249** keys reached.
Neither file contains a duplicate object key — checked separately, because `json.load` keeps the last
one and a duplicate would make a parity count read clean while a string went missing. The audit's
"bare map value" line lists two sites, `agent.refusalKey` and `notifications.key`; both are guarded
partial-record lookups (`if (refusalKey !== undefined)`) interpolated as a key *suffix*, not the
`07c31ab` trap of handing a translated string back to `t()`.

**The honest limit, and the one place it costs the most.** There is still no frontend test runner, so
`contract-scope.ts` is exercised by nothing but the compiler. This is the slice where that gap is
most expensive: the module is pure, total and free of React, so a table of a dozen addresses would
have pinned the whole vocabulary in a way no amount of typechecking does — and `ReadableParams` is a
structural `{ get(name): string | null }` precisely so such a test could pass a stub instead of a
router. It is written to be testable and remains untested. **Amended, not deleted (`D10-37`):** that
table now exists, the runner is in the commit gate, and the stub type is used by it; the paragraph is
kept because it is the reason the module was shaped the way it was.

**No browser has rendered any of these screens**, the Bengali keys are mine and unread by anyone who
reads Bengali, and gap 5 is untouched.

### D10-37 — The compiler is not a test: the scope vocabulary gets a runner

`D10-36` ended by naming the limit it could not pay for. `frontend/src/lib/contract-scope.ts` is
pure, total and free of React — the one module in the frontend whose entire job is branching on
strings — and nothing but `tsc` exercised it. It had even been shaped for the test that did not
exist: `ReadableParams` is a structural `{ get(name): string | null }` rather than
`URLSearchParams`, precisely so a table of addresses could be passed in without a router. This slice
writes that table. It is a small slice on purpose: closing the gap is worth doing, inventing a
frontend test *culture* in the same commit is not.

- **Vitest, because every alternative costs a config file.** The spec needs to import a `.ts` module,
  call it, and compare plain objects. Vitest transforms TypeScript out of the box, defaults to a
  `node` environment, and brings no DOM, no JSX and no mocking layer into a module that needs none.
  A runner that required a build config to read one pure module would have been more machinery than
  the thing it checks.
- **Named imports, never globals.** The spec imports `describe`, `it` and `expect` from `"vitest"`
  instead of enabling `globals: true`, so **no ambient types are declared** and no `tsconfig.json`
  entry changes. That matters because `tsc --noEmit` already includes `**/*.ts`: the spec is
  typechecked by the same gate as the source it tests, for free, and a mistake in the test is a
  compile error rather than a silently skipped file.
- ~~**No `vitest.config.ts` exists.** The spec imports `./contract-scope` relatively, so the `@/`
  alias never has to be taught to a second resolver. A config file would be one more place for the
  module graph to be described differently from what `tsc` and Next.js see.~~ — **amended by
  `D10-41`, not deleted:** this was true only while the one spec happened to import relatively, and it
  stopped being true the moment a module that reaches `@/lib/api` became worth a test. `D10-41` adds
  `frontend/vitest.config.mts`, which declares the alias and nothing else, so the "second resolver"
  worry survives as the limit on what that file is allowed to contain.
- **The runner is in the gate, not just on disk.** A test runner nobody runs rots: the first failing
  expectation is then discovered by whoever finally types `npm test`, months later. A `frontend-test`
  hook joins the two frontend checks in `.pre-commit-config.yaml`, and `npm test` joins
  `typecheck`/`lint` in both `package.json` files, so the same command that exists locally is the one
  the gate runs. The hook fires only on `frontend/**/*.{ts,tsx}` and the whole suite takes well under
  a second.
- **What the table pins.** `readContractScope` over absent, empty, whole numbers up to
  `Number.MAX_SAFE_INTEGER`, and a malformed set chosen to be the ones `Number()` would *rescue* —
  `"0"`, `"00"`, `"012"`, `"12abc"`, `"1.5"`, `"+12"`, `" 12"`, `"12 "`, `"0x10"`, `"1e3"`, `"12,"`
  and `"9007199254740992"`. Plus `readResolved` (only `"1"` is yes), both writers, a round-trip that
  reads back every address the writers produce, and the composition that keeps `?contract=` when
  `?resolved=1` is added.
- **The table found a real asymmetry, and it was mine.** The first run failed on
  `contractIdFromInput(" 12 ")`, which is `12` — so the malformed list cannot be shared between the
  two paths. The difference is deliberate and was previously only implied by two separate regexes:
  **the address is read verbatim, the typed box trims.** Whitespace is now stated as its own
  expectation instead of being folded into "malformed", which is the version of the rule a future
  edit is least likely to break by accident.
- **Teeth, checked by breaking the code.** A spec that passes is not evidence that it would fail.
  Replacing `/^[1-9][0-9]*$/` with `/^[0-9]+$/` and dropping the `Number.isSafeInteger` guard made
  **2 of the 19** tests fail (`raw=0` read as contract `0`); the source was then restored and
  `git diff` on `contract-scope.ts` is empty, so only the spec and its plumbing are in this slice.

**What this deliberately does not test.** The three screens. `agent/page.tsx`,
`notifications/page.tsx` and `escalations/page.tsx` need React, a router, a query client and a
browser; a jsdom renderer standing in for those would prove that the components mount, not that the
screens behave, and claiming otherwise is the overclaim every record in `D10-36` avoided. So the
frontend still has **no component tests, no browser test, and no integration test** — what it now
has is one honest unit test of the one module that is pure enough for a unit test to mean something.

**Dependency cost, stated.** `vitest@^5.1.0` as a `devDependency` of `frontend/`; the lockfile grows
by roughly a thousand lines because a runner brings its own bundler. `npm install` printed **7
moderate severity advisories** for the tree. ~~I did **not** complete an `npm audit` run — it hung and
wedged the shell — so nothing is claimed here about whether those are new, pre-existing, or
development-only.~~ **Amended, not deleted (`D10-37`, `5a30256`):** the audit has now been run, in a
job that can be killed rather than a command that can wedge a session, and the answer is that **this
slice introduced none of them**. All seven are **one chain** — `typescript-eslint` (a direct
`devDependency`) → `@typescript-eslint/{eslint-plugin,parser,type-utils,typescript-estree,utils}` →
`minimatch` — carrying advisory **GHSA-g3hv-75wg-m249**, a denial of service via globparts
amplification, range `>=7.0.0 <11.0.1`, against the nested `minimatch@10.2.6`. The lockfile at
`7cc8541`, the commit this slice branched from, already carries **both minimatch entries at the same
versions** (`10.2.6` nested under `typescript-estree`, `3.1.5` hoisted and outside the range), so the
advisories predate the runner. **No `high`, no `critical`, nothing in the runtime bundle**: the
`dependencies` block of `frontend/package.json` is untouched — the diff adds one `scripts` entry and
one `devDependencies` entry. `npm audit` does report a fix available, which means bumping
`typescript-eslint`; that is a slice of its own and not a side effect of adding a test runner.
`alembic` head stays `0006`, and no backend file changed.

### D10-38 — The bytes decide the format: `storage_path` gets its content reader

`D10-26` gave `Contract.storage_path` a writer. `D10-27` gave it an *existence* reader — the
listing reports `has_document` by testing the column for `None`. Neither gave it a **content**
reader, and the consequence was structural rather than cosmetic: `extract_contract` has taken a
`document_text` argument since `F7`, so somebody had to supply that text, and in practice that
somebody extracted a PDF's text in a shell window and passed `--text-file`. The bytes the system
had just gone to the trouble of sealing, addressing and keeping were read by nobody. This slice is
`app/core/document_text.py` and the one flag change that lets it be reached: `extract` takes the
contract's own stored document when `--text-file` is omitted.

- **The bytes decide the format; the media type is not stored at all.** `POST /contracts/document`
  accepts a content type and discards it, because a column recording what a client *claimed* is
  worth less than the two magic bytes recording what the file *is*. Anything that is not a PDF is
  read as UTF-8 — which is exactly what `--text-file` already did — so the pre-existing path keeps
  working and there is no second rule to keep in sync with the first.
- **`%PDF-` is searched in a 1024-byte window.** ISO 32000 permits the header anywhere in the first
  kilobyte: a whole-file search would hand a *text* file that merely mentions `%PDF-` to the PDF
  parser, and an offset-zero check would reject the producers that emit a small preamble first.
- **A scanned page and an unreadable page must not look alike.** A PDF page with no text layer
  yields `""`. The module returns that as text and reports `pages`, so a caller can say "4 pages, no
  text" instead of "nothing found" — the difference between *the contract never stated an email
  address* and *we never read the contract* is the entire reason the review queue exists. An empty
  document is not a malformed one, and this is the module that refuses to conflate them.
- **An encrypted PDF is refused, not guessed at.** There is no configured password, and attempting
  the empty one would silently return partial text for a document whose owner expected it closed.
  The refusal names the fix: decrypt it and store it again.
- **The parser's exceptions are this module's refusals.** These bytes come from an upload, so they
  are untrusted input; a PDF built to break a parser must produce a `DocumentTextUnavailableError`
  this module owns, not a traceback. No refusal carries any part of the document into its message —
  the bytes are PII by definition, which is why they were sealed.
- **The store decrypts, this module parses.** `extract_document_text` takes plaintext, not a store
  key, so neither side has to know the other's failure modes and a test can pass bytes directly.
- **Two sources, one set of checks.** The empty and oversized checks sit *above* the branch in
  `_extract`, so what counts as an unreadable, empty or oversized document cannot depend on where
  the bytes came from. `--text-file` and the stored document are alternatives, not layers: a
  contract has one authoritative document.
- **Pages are joined with a single newline.** `FieldCandidate.source_line` means "a line of the
  document", and the pattern extractor measures label windows in lines and characters. A blank-line
  page break would inflate that unit by one per page for a reason no reader of the document would
  recognise.
- **`pypdf` is imported lazily, like `kafka-python`.** A deployment that never parses a PDF should
  not pay the import, and a process without the wheel must fail with a sentence naming the fix
  rather than an `ImportError` at start-up.
- **The isolated hook env had to be told about it.** The `mypy` hook installs its own environment
  from a pinned list rather than using the venv, and that list names every stub-shipping dependency
  for exactly this reason (`PyJWT`, `argon2-cffi`, `cryptography`). `pypdf==7.0.0` joins it,
  otherwise strict mode would fail on a missing implementation rather than on anything true about
  the parser.

**The bug on the first run was in the fixture, not the module.** Two tests failed because the
hand-built PDF drew its text as raw bytes with no `BT`/`ET` and no `Tf`: `pypdf` then read
`2026-01-31,` as a `NumberObject`, warned `invalid; use 0 instead`, and the page produced literal
bytes instead of a text run — so no `END_DATE` candidate appeared. The fix was to emit a real
text-showing operation (`BT /F1 12 Tf 72 720 Td (…) Tj ET`) and escape `(`, `)` and `\` in a
`_pdf_string` helper. `app/core/document_text.py` was not modified to make a test pass, and that is
the point of recording it: the module was right and the double was wrong, which is the opposite of
the usual way a failing test gets "fixed".

**What this deliberately does not do.** Nothing calls it automatically — there is no upload hook,
so extraction remains a command an operator runs, and gap 5's `contract.ingested` consumer is still
blocked on the same owner decision. **No real contract PDF has ever been parsed**: the fixture is
one page carrying one text object, built by hand in the test, which proves the seam and not the
world. A scanned PDF produces `""` and the command refuses it as empty, which is honest but is not
OCR. No schema change and no new column: `alembic` head stays `0006`.

**Verified, and the limit of that verification.** `tests/test_document_text.py` holds **11 tests** —
nine for the reader (format decided by bytes not by a name, a PDF becoming the text its page draws,
that text being what the extractor reads, UTF-8 passthrough, a page with no text layer, unsupported
bytes, an empty document, a corrupt PDF, and no refusal carrying the document into its message) and
two that drive `cli extract` with **no `--text-file`**, sealing a real PDF through
`DocumentStore.save`, stamping `storage_path`, and asserting both the extracted `END_DATE`/`EMAIL`
and the refusal of a contract with no document stored. That suite was run green (`pytest
tests/test_document_text.py -q` → **11 passed**, exit 0) in the session that wrote it. **It is not
the gate**, and the sentence that first stood here said the gate had not run at all: the shell in
that session had wedged behind a `git diff` pager, every later command queued behind it, and no
commit existed. **Amended, not deleted — the gate has since run against this exact tree**, from the
repository root, with the results rather than the intention recorded: `ruff check .` → *All checks
passed*; `ruff format --check .` → *106 files already formatted*; `mypy app` under `strict` (59
source files) → *no issues found*; `bandit -c pyproject.toml -r backend/app` → exit 0 with the three
expected `nosec B104` notices in `config.py`; `pytest -q` → **860 tests, 0 failures, 0 errors, 15
skipped**, exit 0, the fifteen being 13 MySQL unreachable, 1 symlink privilege and 1 Kafka opt-in.
`ruff format` was the one real break and it was mine: `document_text.py` and its test were
unformatted, and the formatter — not a hand edit — fixed them. What that verification does *not*
cover is unchanged by it: the fifteen skips mean the outbox and dispatch paths were proven on SQLite
only this run.

This entry carried a commit type and no hash for as long as the gate was unrun, which no other entry
here does; the `docs(status)` commit that lands this text appends it — **`f3a1bb1`**, `feat(backend)`.

### D10-39 — An alarm that fires on a default install is one nobody reads

`doctor` turned the invisible-failure states into an exit code, which is the hook an external alerter
needs — and the record said plainly what it was not: a command nobody schedules is worth little more
than the log line it replaces. This slice gave the check a process. `app/core/attention.py` now holds
the counting, `app/workers/attention_watch.py` runs it on a timer, and `doctor` keeps only the part
that needs an exit code. One behaviour genuinely changes, and the change is the slice: **a park that
the configuration explains is counted and not failed.**

- **The counting moved because there are two callers with different jobs.** The CLI turns the reading
  into an exit code for a cron entry or a CI step; `AttentionWatch` turns the same reading into a log
  line every few minutes. A second copy of the arithmetic would be a second answer to "what needs a
  human", and the entire value of this check is that there is one.
- **`AttentionReport` is a record, not the dictionary the check used to return.** With two callers, a
  key spelled correctly in one and wrongly in the other is a `KeyError` at runtime that no test of the
  other caller can catch. `as_dict()` keeps the flat, JSON-safe shape both callers already emit, so
  the log line's key names did not change with the type.
- **An explained park is counted, and not failed.** With `EVENT_TRANSPORT=none` — the default —
  `build_publisher` returns the refusing stand-in, so the relay parks *every* event it claims. On a
  fresh installation that is the designed behaviour. A check that called it a fault would exit
  non-zero on the first `docker compose up` that ran a reminder, and be muted shortly after, which is
  how a real alarm stops being read. `parks_explained` is `outbox_parked` when `bus_disabled` and
  zero otherwise; `outbox_parked` itself is unchanged and present on every log line. **What changes
  is the verdict, not the count**, so nothing is hidden by not alarming.
- **Everything else is a reason, whatever the transport is.** A notification that ended `failed` or
  `suppressed` and a field value still stored in the clear will not clear themselves. `needs_attention`
  is a property computed from the record rather than a field each caller sets, so the rule lives in
  one place and the two callers cannot disagree about it.
- **The transport travels on the report.** `event_transport` and `bus_disabled` are part of the
  record so a line in a log can be read months later without knowing what the deployment was
  configured with that day; the explained/unexplained distinction is meaningless without them.
- **`PII_COLUMNS` moved here, and it is the point of the move.** `reseal-fields` seals this tuple and
  the attention check verifies it. A column added to one without the other is exactly the failure the
  constant exists to prevent, and it now has one definition instead of two.
- **The worker logs; it does not exit non-zero.** Its consumer is an orchestrator with
  `restart: unless-stopped`, where a non-zero exit is not a signal but a crash loop — and a container
  restarting every few seconds buries the line that says what is wrong. The states being reported (a
  plaintext value, a suppressed notification) do not improve by being restarted. So the verdict is a
  log event with a stable name and the report's fields attached, and the process exits 0 unless
  something unexpected raised.
- **A switched-off bus is named once, at startup, not on every tick.** It is a configuration fact,
  not a finding; repeating it every five minutes is the noise that makes the one line that matters
  filterable. The per-tick lines can then be read without knowing the deployment's configuration.
- **A clean board logs at `debug`.** A process whose only product is a log line, saying "nothing to
  do" every five minutes forever, is how its `error` line gets filtered out by whoever sets the log
  level.
- **A failed tick is logged, not fatal**, matching the relay and the scheduler: the database is the
  only dependency, and a worker that dies on a transient connection error stops watching at exactly
  the moment something is wrong.
- **Five minutes, and a stated ceiling.** `attention_poll_interval_seconds` defaults to `300` with an
  upper bound of an hour, because these states do not become more urgent between ticks and an
  interval longer than an hour delivers the line after the operator who needed it has gone home.
  `attention_field_scan_limit` defaults to `500`, matching `DEFAULT_RESEAL_LIMIT`, so a scheduled
  check and a hand `doctor --limit` scan the same number of rows and cannot report different answers
  to the same question by default.

**The one test that had to change, and why that is the evidence.** `doctor`'s park test asserted
`EXIT_ATTENTION` for a parked event, and under the default transport every park is now explained, so
it was rewritten rather than adjusted to keep passing: a park under `none` **exits 0 and is still
counted**, and the same park under a configured transport still exits `EXIT_ATTENTION`. A behaviour
change that no test noticed would have been a behaviour change nobody chose. Its own teeth were
checked by breaking them: flipping the clean verdict from `debug` to `info`, and making
`needs_attention` count an explained park again, produced **exactly the three intended failures** and
no others; both files were then restored and confirmed byte-identical by SHA-256.

**Verified, and the limit of that verification.** Measured on the tree committed as `2572f53`, from
the repository root: `ruff check .` → *All checks passed*; `ruff format --check .` → *109 files
already formatted*; `mypy app` under `strict` → *no issues found in 61 source files*; `bandit -c
pyproject.toml -r backend/app` → exit 0 with only the three expected `nosec B104` notices in
`config.py`; `pytest -q` → **871 tests, 0 failures, 0 errors, 15 skipped**, exit 0, counted from the
JUnit XML rather than from the progress dots. `tests/test_attention_watch.py` is **9 tests** and
`tests/test_doctor.py` is **21**, 30 together. The fifteen skips are unchanged in kind — 13 MySQL
unreachable at `127.0.0.1:3306`, 1 symlink privilege, 1 Kafka opt-in — which means the outbox and
dispatch paths were proven on SQLite only, again.

**The honest limit of the distinction.** It is drawn from the *current* configuration, so events
parked while a transport was live and then switched off read as explained. The count is still on the
line and an operator who did that knows they did it. A sharper version would classify each row by the
error the relay recorded on it, which needs a query that reads row text — not worth it for a state
this rare, and recorded here rather than hidden.

**What this deliberately does not do, and what the next step would cost.** It does not deliver
anything. A log line reaches a person only if something reads the log, and **nothing in this stack
reads it**. Gap 22 is narrowed, not closed. The obvious next step is blocked rather than skipped: an
HTTP route serving this same report cannot be written without deciding who may see counts that
`dead_letter_count`, `undelivered_counts` and `fields_for_reseal` deliberately read **across tenants**
— each of their docstrings says that scoping them to one tenant would be "actively misleading".
Tenant-scoping the route contradicts three written decisions; serving it unscoped leaks other
tenants' counts and their field primary keys. That is an owner decision, not a patch. No schema
change and no migration: `alembic` head stays `0006`.

The feature landed as `feat(backend)` `2572f53`; this record is written from the code and the
measurements taken on that tree, and the `STATUS.md` regions it names were amended by the follow-up
`docs(status)` commit rather than back-dated into the slice — the same convention `D10-26`…`D10-28`
and `D10-38` used.

### D10-40 — The person who runs the deployment is a recipient, not a principal

`D10-39` left one link open and named it: the attention verdict was a log line, and nothing in this
stack reads the log. The recorded next step was an HTTP route serving the same report. On reading the
three queries that feed it, that step is not unbuilt — it is **blocked**, and the block is a decision
about who may ask the question rather than about how to render an answer.

**Why the route cannot be written, stated precisely.** `dead_letter_count`, `undelivered_counts` and
`fields_for_reseal` each say in their own docstring that tenant-scoping them would be *actively
misleading*: parked events belong to every tenant and a relay problem is not a tenant's; a CLI operator
has a database connection, not a principal, so there is no tenant to scope to. They are **operator**
queries. A route authenticates a **tenant** principal. The mismatch is therefore not a scoping bug to
work around — the route would be answering an operator's question with a customer's credentials.
Scoping it contradicts three written decisions; serving it unscoped hands one tenant another tenant's
counts and field primary keys. That stays an owner decision, and this record does not make it.

**So the message goes to a person instead of a principal**, and the tenant question is never reached.
The recipient is `ATTENTION_ALERT_TO` — deployment configuration, like `SMTP_HOST`, not customer data —
so no principal is impersonated, no count has to be scoped, and the three cross-tenant queries are left
exactly as they are. This *sidesteps* gap 22's obstacle rather than removing it, and the distinction is
kept in the prose rather than smoothed over.

**It alerts on a change of verdict, never on the verdict itself.** At the default five-minute interval a
standing finding is 288 messages a day, which is a mailbox filter by Thursday — and an alarm nobody
reads is the exact failure `D10-39` exists to prevent. The worker remembers the last verdict it
successfully told the operator about and speaks only when the answer differs: once when a finding
appears, once when the board is clear again, nothing while it stands.

**The "cleared" message is not politeness.** Without it an operator cannot distinguish "fixed" from
"the check never fired", and the first thing they do is go and look by hand — the behaviour the whole
worker exists to remove. It is also the reason the protocol has two verbs rather than one: a single
`send(report)` would have to re-derive the direction from state the caller already has.

**A clean board on the very first tick sends nothing.** The remembered verdict starts **calm**, because
nothing has ever been sent and "calm" is the only thing it is reasonable for the operator to believe. A
deployment that boots *broken* therefore alerts on its first tick instead of staying silent until
something changes. The asymmetry is deliberate and fails in the direction that matters.

**State advances only after a send that returned normally.** A down relay therefore retries on the next
tick rather than being recorded as delivered — the same rule the reminder path holds, where a normal
return means the relay accepted the message and anything else is a `SenderError`. A failed send is
logged and does not kill the tick: a watcher that dies when it cannot speak stops watching at exactly
the moment it had something to say.

**The subjects are constant, and carry no counts.** A mail rule has to match the tenth recurrence as
well as the first, and a subject that changes with the numbers cannot be matched at all. The counts are
in the body, where a human reads them.

**No `Message-ID`, and this is the one place the alert differs from a reminder.** `SmtpSender` derives
its id from the idempotency slot so that a re-send after a crash is recognisable as a duplicate. An
alert has no slot and is never re-sent; deriving an id from the verdict would make the second finding
of the week look like the first to any provider that de-duplicates on that header — the one way this
message could be silently dropped.

**The body is rendered from `AttentionReport.as_dict()`**, the same dictionary the log line carries, so
mail and log cannot drift: a count added to the report appears in both places or in neither. It is also
why nothing here selects from the database — the report holds ids and counts only, so the mail cannot
leak a contract value even by accident.

**One transport, reused rather than copied.** `SmtpSender.deliver` was split out of `send` so the alert
gets the verifying TLS context, the login, the bounded socket and the address-scrubbed error text
without a second copy of that block to forget one of them. There is still exactly one relay to
configure, one set of credentials to rotate, and one socket path already covered by
`test_smtp_socket.py`.

**An address with nothing behind it is refused at boot, after the relay is checked.** Two ways to be
wrong — not an address, or an address with `NOTIFICATION_TRANSPORT` set to something other than `smtp`
— both fail at start-up rather than at the first finding, which is precisely when it would matter. The
check sits *after* the SMTP block on purpose: a deployment with both mistakes is told "you have no
relay" first, because that is the fix that also makes the address work.

**Unset means log-only, which is what it was.** `build_attention_alerter` returns `None` rather than a
refusing stand-in — the opposite of `build_sender` — because an unset address is a legitimate
configuration, not a misconfiguration. The worker then has no alerting code path at all, and the
default install behaves exactly as it did under `D10-39`.

**Deliberately not a notification.** No consent lookup, no quiet hours, no per-tenant template. Those
gates exist to protect a *data subject* the system decided to contact; this contacts the person who
chose to run the system, at an address that person configured. Routing an operator alert through the
customer-consent path would be a category error that also required choosing which tenant's consent to
consult. Nor is it a pager: one address, no escalation ladder, no acknowledgement tracking — a
deployment that wants a pager points this address at the pager's mail gateway.

**Verified.** Measured on the tree committed as `d9530b5`, from the repository root: `ruff check .` →
*All checks passed*; `ruff format --check .` → *107 files already formatted*; `mypy app` under
`strict` → *no issues found in 62 source files*; `bandit -c pyproject.toml -r backend/app` → exit 0
with only the three expected `nosec B104` notices in `config.py`; `pytest -q` → **888 tests, 0
failures, 0 errors, 15 skipped**, counted from the JUnit XML rather than the progress dots. The
fifteen skips are unchanged in kind — 13 MySQL unreachable at `127.0.0.1:3306`, 1 symlink privilege, 1
Kafka opt-in — so the outbox and dispatch paths were proven on SQLite only, again.

**Honest limits, three of them.** (1) The remembered verdict is **process memory**: it survives between
ticks and not across a restart, so a worker that restarts while a finding stands re-announces it once.
The repair is a small table (`0007`) holding the last notified verdict, and it is declined here for the
same reason `D10-39` declined row-level park classification — it is not worth a schema change on the
thinnest data this system has until a restarting worker is actually observed to be noisy. (2) The
default install is unchanged and still log-only: the alert reaches a person only when the deployment
sets the address, so gap 22 is narrowed for deployments that configure it and not at all for those that
do not. (3) The blocked route is no less blocked. This addresses a person instead of a principal, which
sidesteps the cross-tenant question rather than answering it. No schema change and no migration:
`alembic` head stays `0006`.

The feature landed as `feat(backend)` `d9530b5`; this record is written from the code and the
measurements taken on that tree, and the `STATUS.md` regions it names were amended by the follow-up
`docs(status)` commit rather than back-dated into the slice — the same convention `D10-39` used for
`2572f53`.

### D10-41 — The runner learns the alias: the session store is the second module with a test

`D10-37` closed the frontend's test gap as narrowly as it could and said so: a runner, **one** spec,
for the one pure module that happens to import relatively. That last clause was load-bearing and was
recorded as a virtue — *no `vitest.config.ts` exists, so the `@/` alias never has to be taught to a
second resolver* — which held exactly as long as the only test was a test of `contract-scope.ts`. The
next module worth a test is `session.ts`, and it reaches `@/lib/api`; under that constraint it could
not be tested at all. A constraint that forbids the test is not a constraint, so this slice pays for
the config and keeps the residue honest about what is left.

- **The alias, and nothing else.** `frontend/vitest.config.mts` declares `@` → `./src` and
  `environment: "node"`. `tsconfig.json` already owns that mapping and Next already honours it; this
  teaches the third resolver the same map rather than having the test import by a path the app never
  uses. A config that grew a second purpose would be the thing `D10-37` was right to fear.
- **`.mts`, because Vite says so and it is right.** Written as `vitest.config.ts` the file loads as
  CommonJS carrying ESM syntax, and Vite prints a forward-compatibility warning naming `.mjs` or
  `"type": "module"` as the fix. Renaming was the smaller of the two: adding `"type": "module"` to
  `frontend/package.json` would change how Next reads the package in order to silence a warning about
  a twenty-line config. **The cost, stated:** `tsc --noEmit` includes `**/*.ts`, which does not match
  `.mts`, so the gate no longer typechecks this file. That is a trade for a file of literals, not an
  oversight, and it is why the file holds no logic.
- **`environment: "node"` is now explicit.** It was Vitest's default and is unchanged, but it is
  written down because the next person to add a test will wonder — and because `jsdom` is precisely
  the dependency this slice does **not** add. A component test needs it; that is a decision, not a
  line of config.
- **One seam mocked, and the real `ApiError` kept.** Only `auth.refresh` is replaced, through
  `importOriginal`, so `ApiError` stays the class `api.ts` exports. That is not tidiness:
  `authorized` branches on `instanceof ApiError` and on `error.isUnauthorized`, so a hand-rolled error
  double would exercise a code path the app never takes and prove nothing about the one it does.
- **What the 18 tests pin — four rules, not four methods.** The server snapshot never claims a live
  session, because hydration compares it with the first client render; `setSession` replaces wholesale
  and notifies each subscriber once, stopping on unsubscribe; **two concurrent `refreshSession()`
  calls share one redemption**, so `auth.refresh` is called once; a refused rotation ends the session
  and tells subscribers rather than leaving a dead one on screen; and `authorized` retries **exactly
  once** — never for a non-401, once after a successful rotation, and not again when the retry is
  itself refused.
- **Teeth, checked by breaking the code.** A green suite is not evidence it would go red. Deleting
  `if (inFlight) { return inFlight; }` from `session.ts` made the concurrency test fail with *expected
  "vi.fn()" to be called 1 times, but got 2 times*; the source was then restored and `git status` on
  it is empty, so only the spec and its config are in this slice.

**Why this module, and not a screen.** `session.ts` is framework-free on purpose — the React binding
subscribes to it with `useSyncExternalStore` instead of holding its own copy — so it is the second
module in the app about which a unit test can mean something. The screens are untouched and still
unproven: they need React, a router and a query client, and a jsdom renderer standing in for those
would prove that components mount, not that screens behave. The single-use refresh token also makes
this the frontend's sharpest correctness rule, and `D10-17` is why: a second redemption is
indistinguishable from theft, and the backend answers by revoking the whole family.

**Honest limits, four.** (1) The frontend still has **no component, browser or integration test**; the
residue is narrower by one module, not closed. (2) **No dependency was added** — `vitest` has been a
`devDependency` since `D10-37` — so the lockfile is unchanged and there is no new advisory surface to
report. (3) The config file itself is not typechecked: `tsc --noEmit` includes `**/*.ts` and
`**/*.tsx`, which does not match `.mts`. It is not unexercised, though — the gate runs
`pre-commit run --all-files` (`.husky/pre-commit`), so `frontend-typecheck` and `frontend-test` run on
every commit and vitest loads this config each time. An earlier draft of this entry claimed the
opposite, reasoning from the hooks' `^frontend/.*\.(ts|tsx)$` key that a commit touching *only*
`vitest.config.mts` would wake none of them. The key is real, but it filters files rather than diffs,
which `--all-files` makes moot; the inference was false and is corrected here rather than left
standing as a phantom limit. (4) ~~Nothing here tests `api.ts`: `request()`'s envelope parsing, its
status branches and its `network_error` path are still exercised by reading, not by execution.~~
**Amended by `D10-42`, not deleted:** true of this slice, and false of the tree after the next one. The
third spec pins exactly those three properties, and it needed no change to `api.ts` to reach them.

**Verified.** On the tree this slice produced, from `frontend/`: `npx vitest run` → **2 files, 37
tests, 0 failed** (19 in `contract-scope.test.ts`, 18 in `session.test.ts`), with the Vite
config-loader warning gone after the `.mts` rename. No backend file changed and no schema change:
`alembic` head stays `0006`.

**Landed as** `test(web)` `8f65fbf` — the two files above and nothing else. This record is written from
the code and the measurements taken on that tree, and the `STATUS.md` regions it names were amended by
the follow-up `docs(status)` commit rather than back-dated into the slice, the same convention `D10-40`
used for `d9530b5`. One correction is carried by that commit rather than by the code: an earlier draft
of limit (3) claimed a config-only commit would wake no hook, reasoning from the hooks' file filter,
and the gate's `--all-files` invocation makes that false.

### D10-42 — The HTTP client's own contract: the third test is about failure, not success

`D10-41` closed its honest limits with the sentence that named this slice's subject — *nothing here
tests `api.ts`* — and `api.ts` is the single seam between every screen and the wire. Every screen's
failure handling is a branch on a status this module assigns or a code it reads out of the envelope,
so a bug here is the hardest kind to see by hand: a misclassified outage renders as "wrong password",
and a 200 with an unreadable body renders as a contract with no fields. It is also the cheapest module
in the app to pin — one `fetch`, no React, no router, no query client — and the test needed **no change
to the module at all**.

- **`api.ts` is byte-identical.** After both mutation checks below,
  `git diff --stat -- frontend/src/lib/api.ts` is empty. A module that has to be reshaped to be
  testable is a design problem worth recording rather than paying for quietly; this one was already
  the seam it claims to be.
- **Through the exported surface, because `request` is private.** The 21 cases call `auth`,
  `contracts`, `notifications`, `review` and `escalations`. That is deliberate rather than forced: the
  subject is the join between a screen and the wire, so a test that reached past an endpoint would pin
  a function no screen calls.
- **Only `fetch` is replaced**, through `vi.stubGlobal`. The envelope parsing, the header decisions and
  `ApiError` itself are the real code; a hand-rolled double of any of them would pin the double, which
  is the mistake `D10-41` avoided by keeping the real `ApiError` behind `importOriginal`.
- **Four kinds of decision, not 21 methods.** What a **refusal** is (the envelope's code, message,
  request id and `details`, all preserved — `details` typed `unknown` and left untouched, because only
  the calling route knows its shape, and a consent 409 carries the row that won the race); what a
  **non-answer** is (status `0`, which is the module's stated reason for existing: "backend is down"
  and "wrong password" must never share a message); what a **204** means (nothing to parse, not a
  parse failure — `POST /auth/logout` succeeds whether or not the token existed); and what a **200
  with an unreadable body** means (`invalid_response`, a refusal rather than an empty success).
- **Three of the assertions are negative, which is why the teeth check below mattered.** No
  `Authorization` on login, because there is no principal yet; no `Content-Type` on a bare `GET`,
  because the header is spread on the body so that a request without one cannot lie to a proxy; and
  `status`/`channel` dropped from the query when `contract_id` is set, because the route ignores them
  and a query string describing a filter the answer ignored is worse than a shorter one.
- **Teeth, checked by breaking the code twice.** Deleting `cache: "no-store"` failed *never lets an
  intermediary answer from cache* with `expected undefined to be 'no-store'`; replacing the conditional
  `Content-Type` spread with an unconditional header failed *sends no body and no content type on a
  bare GET* with `expected { … } not to have property "Content-Type"`. Each broke exactly one test and
  the source was then restored, so those two negative assertions are not vacuous.

**The sharpest limit, stated plainly.** The cases assert against literals written from the backend
route code, so this pins *this client's reading* of the envelope and not *the server's sending* of it.
A route that renamed `request_id`, or answered 204 where this client expects a body, would leave all
21 green and fail in a browser. Only an end-to-end or contract test closes that, and this app still
has none — which is why the sentence above is not "the client is proven".

**Honest limits, four.** (1) The **screens are still hand-tested**: nothing here proves a page maps a
409 to the message it should, only that the status arrives as a 409. (2) **`jsdom` was not added**; the
component, browser and integration gap is untouched and remains a decision rather than a config line.
(3) `API_BASE` reads `process.env.NEXT_PUBLIC_API_BASE` at module load, so every case asserts the
default `/api/v1`; varying it would mean re-importing the module per test, and that harshness is not
worth paying for a value `next.config.ts` rewrites anyway. (4) **React Query is not exercised** — no
test here proves a mutation invalidates the key a screen depends on.

**Verified.** On the tree this slice produced, from `frontend/`: `npx vitest run` → **3 files, 58
tests, 0 failed** (19 in `contract-scope.test.ts`, 18 in `session.test.ts`, 21 in `api.test.ts`). No
dependency, no lockfile change, no backend file, no schema change: `alembic` head stays `0006`.

**Landed as** `test(web)` `cb7b1b4` — the one new file and nothing else. The `STATUS.md` regions this
entry amends were amended by the follow-up `docs(status)` commit rather than back-dated into the
slice, the convention `D10-40` and `D10-41` both used.
