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
  in `.agent-cache/`, which is gitignored, so `STATUS.md` cites no path that the tree does not
  have. ~~The counts are reproducible, the command is not — a reader with this repo cannot
  re-run it as written.~~ **Amended by `D10-86`:** the properties that script checked are now
  asked by committed specs running in CI, so a reader can re-derive the parity, the drift and
  the composed reachability; `0 unused` still cannot be re-derived, and which is which is
  recorded there rather than left as a caveat.

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
audit script lives in `.agent-cache/` and is deleted before commit ~~— so the counts are
reproducible only on the machine that ran them, the same caveat `D10-28` recorded.~~
**Amended by `D10-86`, which strikes the same inference in `D10-28`:** the counts' parity,
placeholder drift and composed reachability are now held by specs CI runs; the unused-key
verdict and the absolute figures are not, and that entry says which is which.

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

*(Both scripts named above are gone, and neither figure ever depended on them: `D10-87` rebuilt
each from committed sources and re-measured, and added `InterfaceError` to the exception classes
`conftest.py` names.)*

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
- **Parsing is hand-checked, for `D10-34`'s reason.** `/^0*[1-9][0-9]*$/` then
  `Number.isSafeInteger`, never `Number(raw)`: `Number("")` is `0`, and an exponent, hex or
  space-wrapped spelling of a real id survives it — `"1e3"`, `"0x10"`, `" 12"` and `"12 "` read
  1000, 16, 12 and 12 — so a malformed address would quietly become a real filter on a contract
  nobody named. ~~A zero-padded spelling is one of those traps~~ — **`D10-76` measured it the one
  spelling that cannot change what it names**: `"012"` was always contract 12 to the server, so the
  guard admits it now, at the cost of the `0*` in front and nothing else.
  ~~`Number("12abc")` is `12`~~ That one comes back `NaN`, not 12:
  reading it as twelve is `Number.parseInt`'s doing, so the digit test is what refuses a tail, not
  the conversion. ~~The server's path parameter is an `int` and its query parameters are `gt=0`, so
  the browser tests the shape the server enforces.~~ `D10-59` measured both premises true and the
  inference built on them false — nothing on the server refuses a padded id, and `GET /contracts/01`
  answers **200** — so the rule is the address bar's own, not a copy of the server's;
  it was recorded as `D10-55` and ruled in `D10-76`.
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
  `Number.MAX_SAFE_INTEGER`, and a malformed set — `"0"`, `"00"`, `"012"`, `"12abc"`, `"1.5"`,
  `"+12"`, `" 12"`, `"12 "`, `"0x10"`, `"1e3"`, `"12,"` and `"9007199254740992"`. Plus
  `readResolved` (only `"1"` is yes), both writers, a round-trip that reads back every address the
  writers produce, and the composition that keeps `?contract=` when `?resolved=1` is added. ~~chosen
  to be the ones `Number()` would *rescue*~~ That criterion is one this set does not meet:
  `Number()` reads `"012"`, `"+12"`, `" 12"` and `"12 "` as 12, `"1e3"` as 1000 and `"0x10"` as 16,
  but answers `NaN` for `"12abc"`, `"abc"` and `"12,"` and a non-integer for `"1.5"`. The set spans
  what the coercion performs *and* what it refuses, which is the better rule — a refusal that held
  only for the rescued spellings would be measuring the conversion rather than the shape (`D10-61`).
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
commit existed. **`D10-81` turns this into a rule rather than a footnote** — the fix it taught was
left here as history, and history is not what the next session reads. **Amended, not deleted — the
gate has since run against this exact tree**, from the repository root, with the results rather than
the intention recorded: `ruff check .` → *All checks passed*; `ruff format --check .` → *106 files
already formatted*; `mypy app` under `strict` (59 source files) → *no issues found*;
`bandit -c pyproject.toml -r backend/app` → exit 0 with the three expected `nosec B104` notices in
`config.py`; `pytest -q` → **860 tests, 0 failures, 0 errors, 15 skipped**, exit 0, the fifteen
being 13 MySQL unreachable, 1 symlink privilege and 1 Kafka opt-in. `ruff format` was the one real
break and it was mine: `document_text.py` and its test were unformatted, and the formatter — not a
hand edit — fixed them. What that verification does *not* cover is unchanged by it: the fifteen
skips mean the outbox and dispatch paths were proven on SQLite only this run.

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
reads it**. Gap 22 is narrowed, not closed. ~~The obvious next step is blocked rather than skipped:
an HTTP route serving this same report cannot be written without deciding who may see counts that
`dead_letter_count`, `undelivered_counts` and `fields_for_reseal` deliberately read **across
tenants** — each of their docstrings says that scoping them to one tenant would be "actively
misleading". Tenant-scoping the route contradicts three written decisions; serving it unscoped leaks
other tenants' counts and their field primary keys. That is an owner decision, not a patch.~~
**Amended by `D10-79`, not deleted:** the route was written, and the fork above was a false binary.
Those three docstrings are arguments about the *operator's* board — the reader with no principal —
and they stay true of it; a tenant asking about **their own** rows is a reader they never described.
`D10-79` added that second reader beside them and left the operator queries byte-identical. No
schema change and no migration: `alembic` head stays `0006`.

The feature landed as `feat(backend)` `2572f53`; this record is written from the code and the
measurements taken on that tree, and the `STATUS.md` regions it names were amended by the follow-up
`docs(status)` commit rather than back-dated into the slice — the same convention `D10-26`…`D10-28`
and `D10-38` used.

### D10-40 — The person who runs the deployment is a recipient, not a principal

`D10-39` left one link open and named it: the attention verdict was a log line, and nothing in this
stack reads the log. The recorded next step was an HTTP route serving the same report. ~~On reading
the three queries that feed it, that step is not unbuilt — it is **blocked**, and the block is a
decision about who may ask the question rather than about how to render an answer.~~ **Amended by
`D10-79`, not deleted:** the step was not blocked, it was built — a tenant-scoped *second* reader
beside the three operator queries rather than a narrowing of them. What survives from this paragraph
is the diagnosis, which `D10-79` keeps: the route must not answer an operator's question with a
customer's credentials.

**~~Why the route cannot be written~~ Why *the operator's* route cannot be written, stated
precisely.** `dead_letter_count`, `undelivered_counts` and
`fields_for_reseal` each say in their own docstring that tenant-scoping them would be *actively
misleading*: parked events belong to every tenant and a relay problem is not a tenant's; a CLI operator
has a database connection, not a principal, so there is no tenant to scope to. They are **operator**
queries. A route authenticates a **tenant** principal. The mismatch is therefore not a scoping bug to
work around — the route would be answering an operator's question with a customer's credentials.
Scoping it contradicts three written decisions; serving it unscoped hands one tenant another
tenant's counts and field primary keys. ~~That stays an owner decision, and this record does not
make it.~~ **Amended by `D10-79`:** the choice was never between those two. A *second* reader over
the same tables, scoped from the principal, in new functions that leave these three untouched, is
neither branch of the fork — and it is what `D10-79` built. The owner decision this record declined
was whether to *narrow an operator query*; that one is still not taken, and still not needed.

**So the message goes to a person instead of a principal**, and the tenant question is never reached.
The recipient is `ATTENTION_ALERT_TO` — deployment configuration, like `SMTP_HOST`, not customer data —
so no principal is impersonated, no count has to be scoped, and the three cross-tenant queries are left
exactly as they are. ~~This *sidesteps* gap 22's obstacle rather than removing it, and the
distinction is kept in the prose rather than smoothed over.~~ **Amended by `D10-79`, not deleted:**
sidestepping is what this record did; `D10-79` then removed the obstacle for two of the three counts
by adding the reader this record assumed could not exist. The distinction this sentence protects is
still the right one — an operator alert is not a tenant route — it just stopped being the last word.

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
do not. (3) ~~The blocked route is no less blocked. This addresses a person instead of a principal,
which sidesteps the cross-tenant question rather than answering it.~~ **Amended by `D10-79`, not
deleted:** a route now exists — `GET /api/v1/attention`, admin only, two counts — as a *second*
reader scoped from the principal, beside the three operator queries and narrowing none of them. What
this limit was protecting, that an *operator alert* is not a *tenant route*, is now carried by two
readers instead of by the absence of one. No schema change and no migration: `alembic` head stays
`0006`.

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

**Honest limits, four.** (1) ~~The frontend still has **no component, browser or integration test**;
the residue is narrower by one module, not closed.~~ **Amended by `D10-43`, not deleted:** the residue
was narrower by two after `D10-42` and by three after `D10-43`, which adds the first component test —
so the *component* half of that sentence has stopped being true, while the browser and integration
halves have not. (2) **No dependency was added** — `vitest` has been a
`devDependency` since `D10-37` — so the lockfile is unchanged and there is no new advisory surface to
report. (3) ~~The config file itself is not typechecked: `tsc --noEmit` includes `**/*.ts` and
`**/*.tsx`, which does not match `.mts`.~~ **Amended by `D10-43`, not deleted:** `include` now
carries `**/*.mts`, so `npm run typecheck` covers this file and the limit is closed rather than
narrowed. What survives of it is the reason nobody noticed for two slices: a config matched by no
`include` pattern is invisible to `tsc` without ever being reported as skipped. It is not
unexercised, though — the gate runs
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
409 to the message it should, only that the status arrives as a 409. (2) ~~**`jsdom` was not added**; the
component, browser and integration gap is untouched and remains a decision rather than a config line.~~
**Amended by `D10-43`, not deleted:** the decision was taken and paid for, so `jsdom` *is* added now —
but only to the spec that needs it, through a per-file `// @vitest-environment jsdom` first line rather
than a changed default, so the three pure specs above still run without a DOM.
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

### D10-43 — The first component test: paying for a DOM, and the transform that decides it

`D10-41` limit (1) and `D10-42` limit (2) both named this subject in the same words — *no component,
browser or integration test* — and both deferred it for the same reason: `jsdom` is a dependency, not
a config line. This slice takes the decision and pays for it, because `auth-provider.tsx` is where
the correctness work of the last two slices becomes visible. `session.test.ts` proves the store is
right; nothing proved a component *sees* it. The provider is the only place React reads those
tokens, and it does so through `useSyncExternalStore` rather than `useState` on purpose — one copy of
the truth, readable by an in-flight request without waiting for a render. Every rule it enforces is a
comment in the source and, until now, a claim nothing checked.

- **Three devDependencies, and the lockfile change is the slice's bulk.** `jsdom`, `@testing-library/react`
  and `@testing-library/dom` (+526 lines in `frontend/package-lock.json`). `npm audit` stays at **7
  moderate**, one existing chain (`typescript-eslint` → `typescript-estree` → `minimatch`); the
  install added no new advisory surface. This is the cost the two prior slices declined to pay
  quietly, now recorded rather than absorbed.
- **A per-file environment pragma, not a moved default.** The spec's first line is
  `// @vitest-environment jsdom`; `vitest.config.mts` keeps `environment: "node"`. A DOM is right for
  one file and wrong for three, and moving the default would charge every pure spec for a document
  it never touches. This is the cheapest of the three ways to give one file what it needs.
- **`oxc.jsx`, and specifically not `esbuild.jsx`.** Vitest 5 runs on Vite 8, which transforms with
  oxc. An `esbuild: { jsx }` block is accepted by the config schema and then ignored at runtime —
  Vite warns that oxc wins — so the obvious-looking fix does nothing at all and fails *silently*.
  The other half is `tsconfig.json`'s `"jsx": "preserve"`, ~~which is correct for Next (it compiles JSX
  itself)~~ and wrong for Vitest, where nothing downstream of the transformer does: a `.tsx` spec
  reaches Node as raw JSX and dies parsing. Asking oxc for the automatic runtime (`react/jsx-runtime`,
  already a `react` dependency) is what makes a `.tsx` file runnable here at all. **The struck clause
  is corrected by `D10-47`, not deleted:** this Next version does not keep `preserve` either — its own
  build rewrites the value to `"react-jsx"` on every run, which is why the committed `tsconfig.json`
  ships `react-jsx`. The rest of this bullet is unaffected and stays load-bearing, because the runner
  reads `vitest.config.mts` and not `tsconfig.json`.
- **Only `auth` is replaced, through `importOriginal`.** The same seam `D10-41` used and for the same
  reason: `authorized` branches on `instanceof ApiError`, so a hand-rolled double could not satisfy
  that honestly. The session module is the real one; the store, the single-flight rotation and the
  retry are all the production code.
- **Twelve cases that pin what a user sees, not what a function returns.** No session is stored while
  the server is still deciding; a refused sign-in leaves a working session untouched rather than
  clearing it; a sign-out clears locally *before* the server answers; a refused revocation still
  leaves the user signed out rather than half-signed-in; a session replaced anywhere in the app
  re-renders the tree; and `useAuth` outside the provider throws rather than handing back `null`.
- **`**/*.mts` added to `tsconfig.json`'s `include`, closing `D10-41` limit (3) rather than narrowing
  it.** `tsc --noEmit` matched `**/*.ts` and `**/*.tsx` only, so the runner's own config was the one
  file in the repo no compiler looked at. What survives of that limit is why nobody noticed for two
  slices: a file matched by no `include` pattern is invisible without ever being reported as skipped.

**Teeth, checked by breaking the code twice.** Converting `signOutEverywhere`'s `finally` to a `catch`
failed **three** tests, not one: *hands the access token to the server and ends the session*
(`expected { access_token: 'access-1', … } to be null`), *ends the session even when the call fails*
(`promise resolved "undefined" instead of rejecting`) and *refuses to call the server when nobody is
signed in* (same). A `catch` swallows the rejection as well as skipping the clear, so it breaks
strictly more than the block it replaces — worth stating, because an earlier draft of this record
said "two" from memory and the run said three. Removing the `oxc` block failed the whole `.tsx` file
(**58 passed, 1 file failed**) with `Failed to parse source for import analysis … make sure to not set
jsx to preserve`, which is the paragraph above proven rather than asserted. Both mutants were
reverted; `git diff` on `auth-provider.tsx` and on `vitest.config.mts` is empty of them.

**The sharpest limit, stated plainly.** This proves the auth binding mounts, subscribes and clears —
not that any screen renders. There is no router, no `QueryClientProvider` and no network: the wrapper
is the provider alone. So the residue is narrower by one module and by one *kind* of test, and the
word "component" has stopped being true in those two older sentences while "browser" and
"integration" have not. A page that maps a 409 to the wrong message is still found by hand.

**Honest limits, four.** (1) **One component is tested, of nine screens plus a shell** — and the one
that is tested is the provider, not a page. `D10-43` narrows the gap; it does not touch the screens.
(2) **No browser and no integration test.** `jsdom` is a DOM implementation, not a browser: no
layout, no CSS, no real navigation, no `fetch`. Nothing here proves the client and the server agree.
(3) **`useSyncExternalStore` is exercised through its behaviour, not its internals** — the tests
assert that a re-render happens, not how React schedules it; a React upgrade could change when the
tree re-renders without changing what these cases assert. (4) **The `oxc` block is proven only by
deletion.** The config comment says the block is load-bearing and the removal run agrees, but no test
pins it, so a future Vitest upgrade that removes the need would leave a comment asserting a
requirement that no longer exists — the failure mode of every "this is load-bearing" claim.

**A diagnostic that was already stale.** The work was briefly spent chasing a `TS2769` on
`vitest.config.mts:36` — `No overload matches this call` — which read as a real structural mismatch,
and an explanation was even constructed for it before the compiler was asked directly. The project's
own settings reproduce clean and the editor then reported no errors: the diagnostic was simply
stale. Two habits came out of it and are worth recording because both cost time before they paid:
re-run the failing command rather than reading a cached warning, and prefer `npm test` over
`npx vitest`, since `npx` resolved a *different* vitest out of its cache and reported results from a
runner this project does not use.

**Verified.** On the tree this slice produced, from `frontend/`: `npm test` → **4 files, 70 tests, 0
failed** (19 in `contract-scope.test.ts`, 18 in `session.test.ts`, 21 in `api.test.ts`, 12 in
`auth-provider.test.tsx`). No backend file changed and no schema change: `alembic` head stays `0006`.

**Landed as** `test(web)` `16803d0` — the new spec, the three dependencies and their lockfile
entries, the runner config and the one-word `include` change, and nothing else. `STATUS.md`'s stale
"no component test" regions were amended by the follow-up `docs(status)` commit rather than
back-dated into the slice, the convention `D10-40`, `D10-41` and `D10-42` all used.

### D10-44 — The first screen test: what a paid-for harness buys, and what a DOM cannot prove

`D10-43` limit (1) was precise: *one component is tested, of nine screens plus a shell* — and the one
component tested was the provider, not a page. This slice tests the first page. The choice of page was
not arbitrary: `login/page.tsx` is the only screen where the client names a tenant, and it is the door
every other screen sits behind, so a defect there is both the most expensive to hit and the hardest to
notice, because it presents as "the app is broken" rather than "the form is wrong".

- **No new dependency, and that is the point.** `D10-43` paid for `jsdom` and Testing Library; this
  slice spends that purchase rather than repeating it. `package.json`, the lockfile, `tsconfig.json`
  and `vitest.config.mts` are all untouched. `jest-dom` was considered and declined: it exists to add
  matchers such as `toBeDisabled`, and the disabled state is read off the element in one line. A
  fourth dependency for one assertion is the kind of cost that is invisible at install time and
  permanent afterwards.
- **The real message catalogue, not a stub.** The wrapper is `NextIntlClientProvider` with the real
  `getMessages(locale)` output, and the cases assert against `getMessages("en").auth.*`. That makes
  each assertion a statement about the sentence a user reads. A hand-written catalogue would have
  passed against a page that rendered the wrong key, which is exactly the failure the screen has.
- **The real `AuthProvider` and the real session store; only `auth.login` is replaced.** Replaced
  through `importOriginal`, the seam `D10-41` and `D10-43` both used and for the same reason: the
  screen branches on `instanceof ApiError` and on the `isRateLimited` / `isOffline` / `isUnauthorized`
  getters, so a hand-rolled double could satisfy neither honestly. The test therefore exercises the
  screen, the provider, the store's subscription and `ApiError` together — the actual chain.
- **The router is a double; `next/link` is not.** There is no Next runtime under `jsdom`, so
  `useRouter` has to be replaced. `next/link` is left real, because replacing it would hide a broken
  `href` — the back link is part of the screen's correctness and nothing else checks it.
- **Eleven cases, each a rule the source states in prose and nothing had checked.** The slug and the
  email are trimmed while the password is sent verbatim — whitespace is part of a password and
  trimming it locks people out. Success navigates with `replace`, so the credentials form is not one
  Back press from the area just entered. The destination is the locale segment, not a hardcoded
  `/en`; the `bn` case exists to catch exactly that regression. 429, unreachable and 401 stay
  distinguishable, mapped by status rather than by matching the server's English sentence. The
  refusal message for a rejected password names no field (`expect(text).not.toMatch(/tenant|email|password/i)`),
  because the API deliberately does not say which one was wrong and naming one would convert an
  intentional opacity into a hint. A failed attempt navigates nowhere. The form is disabled *and* the
  handler returns early, since a programmatic submit goes straight past the `disabled` attribute —
  the test calls `submit` a second time to prove the guard rather than the attribute. A stale error
  is cleared on retry, because "it says wrong password but it logged me in" is what that bug looks
  like from the outside.

**Teeth, re-measured in this session rather than copied from the slice's own notes.** Four mutants,
reverted afterwards, with `git diff` on the page empty at the end. (1) `router.replace` →
`router.push`: **5 of 11 failed** — every case that asserts where the user lands, plus the ones that
assert where they do not. (2) In one pass, `password` → `password.trim()`, deleting the
`if (submitting) return` guard, and deleting `setError(null)`: **exactly 3 of 11 failed, and exactly
the three that name those rules** — *trims the slug and the email but sends the password exactly as
typed*, *disables the form and ignores a second submit*, *clears the previous error when the user
tries again*. The second result is the more informative one: a mutant batch that fails precisely the
cases that claim to cover it, and no others, is evidence that the cases are not merely green but
independent.

**Verified.** On the tree this slice produced, from `frontend/`: `npm test` → **5 files, 81 tests, 0
failed** (19 in `contract-scope.test.ts`, 18 in `session.test.ts`, 21 in `api.test.ts`, 12 in
`auth-provider.test.tsx`, 11 in `login/page.test.tsx`). No backend file and no schema change:
`alembic` head stays `0006`.

**Honest limits, five.** (1) **One screen of nine, and it is wired rather than working.** `jsdom` is a
DOM implementation, not a browser: no layout, no CSS, no real navigation and no `fetch`. What is
proven is that the page calls `router.replace` with the right path; that Next then navigates is not
proven, and could not be here. (2) **The eight remaining screens need a harness this slice did not
build.** Every one of them imports `useSearchParams` or route params and TanStack Query, so each needs
a `QueryClientProvider` and often a `Suspense` boundary; the review queue, the escalation queue and the
contract detail page are three different harnesses, not one. Naming them here is deliberate — the next
screen test is a slice, not a copy of this file. (3) **React Query is still unexercised.** `D10-42`
limit (4) stands untouched: nothing yet proves a mutation invalidates the key a screen depends on, and
this page performs no query at all. (4) **Only the two static locales are covered.** `en` and `bn` are
both read from the catalogue, but a locale added later with a missing key fails at render, not here.
(5) **The router double narrows what "navigation" means.** The test asserts the call and its argument;
it cannot assert the resulting route, so a page that computed the right path and a router that ignored
it would still pass.

**Landed as** `test(web)` `ef2aebd` — one new file, 353 lines, and nothing else: no dependency, no
lockfile entry, no config change, no source change. `STATUS.md`'s surviving "no screen" regions were
amended by the follow-up `docs(status)` commit rather than back-dated into the slice, the convention
`D10-40` through `D10-43` all used.

---

### D10-45 — The review queue: the first screen that can *finish* work, and the first test to run TanStack Query

`frontend/src/app/[locale]/review/page.test.tsx`, **16 cases, 550 lines, one new file**. No source
change, no dependency, no lockfile entry, no backend file, no schema change. A one-line summary of
the whole slice: the harness `D10-44` paid for is spent, not re-bought.

**Why this screen, when eight were still open.** `D10-44` left a precise residue — one screen of nine
tested, eight not — and the obvious temptation is to test the cheapest next one. The review queue was
chosen for the opposite reason: it is the **only** frontend path by which a field leaves `pending` and
becomes `approved`, and therefore the only one that *completes* work rather than inspects it. If any
screen deserved the harness's second outing it is the one whose job is to finish the pipeline.

**The harness. No new dependency, and this is the point.** `D10-43` bought `jsdom` and
`@testing-library/*`; `D10-44` proved they reach a page. What this screen needed beyond the login form
was **state**, and that is where a harness usually starts cheating. Instead: a **real**
`QueryClientProvider` with a **fresh client per test**, so no cache survives between cases; the real
`AuthProvider` over the real session store, so the viewer gate is exercised through `useAuth` rather
than stubbed; the real `NextIntlClientProvider` and the real catalogue, so each assertion is about a
sentence a user reads. Only three functions are replaced — `review.queue`, `review.decide`,
`review.reveal` — and each through `importOriginal`, so the genuine `ApiError` class survives and
`instanceof` still means something.

**One deviation from the app's own provider, recorded rather than buried: `retry: false`.** The
shipped client retries once. That is right for a user on a flaky link and wrong for a test whose
subject is *how many times the screen asked*, because a retry turns every "it asked once" assertion
into one that is either false or unfalsifiable. The deviation is confined to the test's client and
the test says so where it is constructed; nothing about production behaviour is asserted by it.

**What the sixteen cases pin**, all rules the source states in prose and nothing had checked: the
decided **version is echoed** from the row rather than assumed; an error is scoped to **the row that
failed** and not shared across the queue; a **`409` on one row refetches the queue** so the stale
version is replaced; the **viewer role is read-only** — the controls are absent, not merely inert; a
correction is **trimmed** before it is sent; and `isPending` gates a second submission.

**Teeth, re-measured on this tree — and the earlier pair of numbers was wrong.** I had recorded
"3 of 16, then 2 of 16". Both were under-measured, because I had **batched** mutants:

- **Pass A** — drop the viewer gate, drop the `trim()`, scope the failure to the mutation instead of
  the row: **3 of 16**, exactly the three cases that name those rules and nothing else.
- **Pass B1** — `version: item.version` becomes `version: 0`: **3 of 16**.
- **Pass B2** — delete the conflict refetch: **1 of 16**.

The old "2 of 16" was **B1 and B2 in one pass**, so it was really 3 and 1 added together — and the
version mutant alone moves three cases, which is worth knowing and was invisible while they were
batched. **A second correction, on method:** my first per-row-error mutant was badly chosen.
`describeFailure(decide.error)` with `undefined` returns the generic sentence and paints an alert on
*every* row, so it failed **5 of 16** and tested "always-on" rather than "shared". Re-run as
`decide.variables ? ...`, it isolates the rule. A mutant that fails too many tests is not a stronger
signal; it is a different question. All five mutants were reverted and `git diff` on the page is empty.

**A defect the commit gate caught that I had shipped into my own draft.** `noUncheckedIndexedAccess`
makes a page element possibly `undefined`, so `frontend typecheck` failed and the commit was refused.
Fixed properly in the answer-builder by seeding the first answer as a parameter — the nullish
fallback beside it is unreachable, and the comment says so — rather than by casting the index away.
Worth recording for its own sake: the harness that reports "No errors reported" is not the same as
a clean tree, because a type error only `tsc` can see is not an error any editor had shown.

**Verified.** On the tree this slice produced, from `frontend/`: `npm test` gives **6 files, 97 tests,
0 failed** (19 `contract-scope`, 18 `session`, 21 `api`, 12 `auth-provider`, 11 `login`, 16 `review`).
`alembic` head stays `0006`; no backend file, no dependency, no lockfile line changed.

**Honest limits, four.** (1) **Two screens of nine, and both wired rather than working.** `jsdom` is a
DOM, not a browser: no Next runtime, no layout, no real navigation, no `fetch`. The queries and
mutations are real TanStack Query, but **the server behind them is a mock**, so nothing here proves
that client and server agree on the *shape* of a `409` — only that the screen handles the shape it
expects. (2) **This retires `D10-44` limit (3), and cannot retire its successor.** React Query is no
longer unexercised: refetch, invalidation and `isPending` gating now run. What is still unproven is
that the *keys* are right — that the invalidation a screen relies on is the key another screen reads.
(3) **The harness did not need what the remaining screens need.** This page needs no `useSearchParams`
and no `Suspense` boundary; the escalation and agent screens need both, so porting is three harnesses
rather than one loop. (4) **The viewer gate is the client's opinion.** The role is read through the
real `useAuth`, but the *server's* authorization is a different suite; nothing proves the two agree.

**Landed as** `test(web)` `3b16d9a` — one new file and nothing else. `STATUS.md`'s surviving
"one screen" and "no screen's behaviour is proven" regions were amended by the follow-up
`docs(status)` commit, the convention `D10-40` through `D10-44` all used.

### D10-46 — CI: the gate stops being a suggestion, and the two checks the hook could never express

Every gate in this project has run from a developer's machine. `.husky/pre-commit` invokes
`pre-commit run --all-files` on each commit, and since `4a9e8e9` that is real rather than decorative —
but it is **optional**: `git commit --no-verify` bypasses it, a second clone without `npm install` has
no hook at all, and a fork inherits a config nothing enforces. Worse, two things were never gated
anywhere: the **pytest suite is not a pre-commit hook**, so ~470 tests ran only when somebody
remembered; and the **MySQL integration suite skips silently** when no server answers, which is the
normal state of a machine that has not started the compose stack. Neither was a known unknown — both
are recorded in `STATUS.md` — but until now the only mechanism that could ever have caught them was a
person's memory. This slice adds `.github/workflows/ci.yml`.

- **Mirror the existing gate; do not invent a second one.** Every job runs the *same command* its hook
  already runs: `ruff check backend`, `ruff format --check backend`, `python -m mypy`,
  `bandit -c pyproject.toml -r backend/app -q`, `npm run typecheck`, `npm run lint`, `npm test`. A
  parallel pipeline that checks something subtly different is worse than none, because green stops
  meaning one thing. The only difference is deliberate: **the hooks' `--fix` flags are dropped**, since
  a runner must report a defect and never repair it and then pass. `ruff format --check` is the CI
  spelling of the `ruff-format` hook, which fails precisely when it rewrote a file.
- **Separate jobs, because attribution is the product.** One job running everything is one round trip
  to learn that the failure was in `mypy` and not `eslint`. Six jobs also let a reader see four-of-six
  at a glance rather than a single red cross over 19 checks.
- **The two new gates are the point of the file.** `backend-tests` runs `pytest -m "not integration"`,
  which nothing had ever run automatically. `mysql-integration` runs `pytest -m integration` against a
  `mysql:8.4` service container — the same tag `docker-compose.yml` ships — so the six guarantees the
  integration module lists in its docstring (SKIP LOCKED claiming, real `ENUM`s, the dispatch lock,
  `NUMERIC(4, 3)` scale, refresh-token row locks, cascading FKs) stop being things that were proven
  once by hand in `c99c792` and become things that are re-proven on every push.
- **A skip on the MySQL job is a failure.** That suite *skips* rather than fails when it cannot
  connect, which is right for a laptop and wrong for a service container: a run that skipped would be
  green while proving nothing, and green-but-empty is the failure mode this whole slice exists to
  remove. The step pipes pytest through `tee` (so `pipefail` keeps its exit status) and greps for the
  word `skipped`, with `-rs` so a red run prints the reason. It is checked in the same step on purpose:
  a separate "did it skip" step after a failing pytest would report a skip check that never ran.
- **`gitleaks` is promoted from `manual` to mandatory.** D1-03 bound it to the manual stage because it
  needs a Docker image, so it has never gated anything — gap 1's residue is explicit that "one green
  run is evidence about the history, not a new automatic check". `ubuntu-latest` provides Docker, so
  the hook's own documented command (`pre-commit run gitleaks --all-files`, run by name) finally runs
  where it cannot be forgotten, against `fetch-depth: 0` so the scan sees history rather than one
  commit.
- **Versions come from other files, not from taste.** Node **22** because `@types/node` is `^22` and
  `engines.node` says `>=20`, so 22 is the version the frontend is actually typed against. Python
  **3.13** because that is what `Dockerfile.backend` ships — CI should exercise the interpreter the
  artifact carries. That is a deliberate mismatch with `requirements.txt`'s comment ("verified on
  CPython 3.14 / Windows"), which records where the pins were tested, not a second supported runtime;
  `pyproject.toml`'s `py312` is a floor for ruff and mypy, not a different interpreter.
- **`npm ci`, never `npm install`.** All three lockfiles are committed, so CI must test the resolved
  tree a developer has, not a fresh resolution.
- **One required check instead of six.** A `gate` job with `needs` on all six and `if: always()` fails
  unless every one of them reports `success`; branch protection then needs to name one context, and
  adding a seventh job later does not require touching the setting. `if: always()` is what makes it
  meaningful — a *skipped* required check is a green light, which is how a gate becomes advice.

**Deliberately omitted, with the reason rather than an apology.** (1) ~~**`next build`.** It is the one
check that would catch a server/client-boundary error `tsc` cannot see, and it is not here because it
is unverified on a clean machine: it needs the whole Next toolchain plus whatever the build reads at
build time, and a pipeline that is red on its first run teaches people to ignore it — the same
argument `docker-compose.yml` makes about healthchecks pointing at readiness. It is the first
candidate for the next slice.~~ **Amended by `D10-47`, not deleted, and in exactly the order this
paragraph argues for:** the check was taken as the next slice, run locally until it was green
(`20ccc7c`), and only then added to the frontend job — so the-pipeline-is-red-on-its-first-run was not
paid as the price of adding it. Running it first found that the build had **never** worked at all,
which is a stronger argument against this omission than the one written for it. Its own justification
also turns out to be measured rather than reasoned: no hook in this repository renders an RSC pass, so
19 green gates could not have seen the failure. (2) **A coverage threshold.** `pytest-cov` is a dev dependency and
`[tool.coverage]` is configured, but a percentage gate on a suite whose frontend half is two screens
would enshrine the wrong number. (3) **A migration drift check.** `alembic check` is available and the
MySQL job is the right place for it; it is left out only because it is one more unverifiable step in
a slice that already cannot be run here.

**Verified — and this is the limit that matters most.** The workflow **has not been run.** There is no
runner on this machine, so the artifact's correctness is *read*, not *observed*: the YAML parses under
PyYAML, the three workflow-expression sites survived the write intact, and every command in it was
copied from a file in this repository rather than recalled. That is the strongest claim available and
it is weaker than a green run. Three specific consequences are recorded rather than waved away:
(1) **The backend suite has never been gated, so CI's first run may legitimately be red** — that would
be the finding, not a regression, and it is exactly why this slice is worth landing before the suite
is trusted further. (2) **`--no-verify` still works.** The workflow exists; making it *mandatory* is a
repository setting (required status checks on `main`), which is not a file and cannot be committed, so
"D10-46 makes the gate unbypassable" is false until an admin enables it — the honest claim is that the
gate now exists somewhere other than one laptop. (3) **The scan has nothing new to scan yet:** 113
commits are unpushed, so `gitleaks` in CI is evidence about what reaches the server, and until this
lands remotely it is scanning a history no runner has seen.

**Two risks worth naming while they are cheap.** Pinned wheels are verified on Windows/3.14 and now
installed on Linux/3.13; every pin here is pure-Python or `abi3`, so this should resolve, but "should"
is the correct word and the first run is the test. And the `gate` job uses `jq`, which `ubuntu-latest`
ships — a runner image change would be a one-line fix, and is the sort of dependency a self-hosted
runner would silently break.

**Landed as** `ci: .github/workflows/ci.yml` `2cc9cba` — one new file, no dependency, no lockfile line, no
application source, no migration (`0006` stays head). `STATUS.md`'s "gitleaks still gates nothing"
residue and the F1 table were amended by the follow-up `docs(status)` commit, the convention `D10-40`
through `D10-45` all used.

### D10-47 — The production build: a check that had never run, and the two lines of config it silently needed

`npm run build` had never been run against `frontend/`. Not "seldom" — never, by anything, including
the 19-hook commit gate: the gate runs `tsc --noEmit`, eslint and vitest, and none of the three
renders a page in a React Server Component pass. The first run failed **every** page with `Couldn't
find next-intl config file`, which means the app as committed could not produce a production artifact
at all and no check in this repository could say so. That is the shape of the defect: `D10-46`
deliberately omitted `next build` (unverified on a clean machine, and a pipeline that is red on its
first run teaches people to ignore it); this slice supplies the evidence first and adds the step
second — the reverse of the order `D10-46` was criticised for.

- **Two causes, and both were required.** `src/i18n/request.ts` is the server-side request config:
  `/[locale]/layout.tsx` hands `NextIntlClientProvider` an explicit locale and catalogue, so every
  screen already renders from props — but the provider still consults the request config while
  rendering on the server, and the app had no such file. Adding it alone did not fix the build;
  `next.config.ts` also had to be wrapped in `createNextIntlPlugin()`, which is what tells next-intl
  where to load it from. Recorded because shipping the first cause alone would have left the second to
  fail in someone else's first build.
- **`defaultLocale` rather than a throw.** This app has no next-intl middleware: locale selection is
  the route segment's job (the client names a *tenant*, not a language), and the layout validates the
  segment. The request config's locale is therefore a fallback rather than a decision, and making it
  throw would turn a request the layout would have handled into a 500.
- **`tsconfig.json` is hand-edited, because the build rewrites it.** `next build` reformats the file
  and flips `"jsx": "preserve"` to `"react-jsx"` on every run — re-measured here, on the committed
  tree: the diff it produced expanded every array and changed `jsx`. Letting the build mutate the file
  it validates would have put a 40-line reformat in front of a two-line setting, so the committed value
  is the one the build writes. `.next/dev/types/**/*.ts` joined `include` for the same reason: it is
  where this Next version puts the generated route types.
- **This corrects a claim, not only a file.** `D10-43`'s record says `tsconfig.json`'s
  `"jsx": "preserve"` is *right for Next*; the build disagrees, and insists on `react-jsx`. The
  `oxc.jsx` block in `vitest.config.mts` is untouched and still load-bearing, because it is Vitest that
  reads that file and not `tsconfig.json`: the runner still has to be told how to transform JSX.
- **CI grows the fourth frontend check.** The frontend job now runs `npm run build` with
  `NEXT_TELEMETRY_DISABLED`, and needs no backend: the `/api` and `/health` rewrites are resolved per
  request, not at build time. `D10-46`'s "deliberately omitted" note is amended rather than deleted —
  it was right to wait for evidence and wrong to leave the check out afterwards.

**Verified.** From `frontend/` on the committed tree: `npm run typecheck` 0 errors, `npm run lint` 0
errors, `npm test` **97 passed across 6 files** (19 + 18 + 21 + 12 + 11 + 16), `npm run build` exit 0
with **19 routes prerendered**, and a second consecutive build confirming `tsconfig.json` is no longer
rewritten. The backend suite was run with the exact command `D10-46`'s `backend-tests` job uses —
`pytest -m "not integration"` — before that job had executed anywhere: **867 passed, 2 skipped, 19
deselected**. Both skips are legitimate (`symlinks-without-privileges`, a Windows-only probe, and the
opt-in `KAFKA_LIVE` socket test) and will not skip on `ubuntu-latest`.

**Honest limits, and the first one is the point of the entry.** (1) **The build is verified on Windows
with Node 22, not on `ubuntu-latest`.** There is no runner here, so CI's build step is still *read*:
the local green proves the step is correct, not that it passes where it will run. (2) **A build proves
the app renders, not that it works.** It renders each page once — no backend, no auth, no data — so it
proves the RSC boundary, the i18n wiring and the module graph, and nothing about what a signed-in user
sees. **(3) One of the twelve app routes produces no HTML** — `/[locale]/contracts/[id]`, the only app
path absent from the build's prerender set; the other eleven render **19 HTML files** across two
locales. This limit was first written as *"two of the 19 routes are marked Dynamic (`/contracts/[id]`
and the review queue)"* and was re-counted against `.next/server/app` and
`app-path-routes-manifest.json` before this entry was committed, because the review queue is in fact
prerendered inside its `Suspense` boundary. `/contracts/[id]` needs a path parameter, so being dynamic
is expected — but it is *read off the build output* rather than chosen in a decision, and it is the one
screen a production build still cannot render. (4) **`npm audit` is unchanged at 7 moderate** on the pre-existing
`typescript-eslint` to `minimatch` chain, and no dependency or lockfile line changed in this slice.

**Landed as** `fix(web)` `20ccc7c` — one new file, two edits and one CI step: no dependency, no
lockfile line, no backend file, no migration (`0006` stays head). `STATUS.md`'s claims that the build
was deliberately declined, and that `preserve` is right for Next, are amended by the follow-up
`docs(status)` commit, the convention `D10-40` through `D10-46` all used.

### D10-48 — One journey over a real socket, because every other check is an in-process call

Every test in this repository reaches the application through
`fastapi.testclient.TestClient`, which is an in-process ASGI call. That is the right tool for
asserting behaviour and it is not evidence about the thing `docker compose up` starts: no socket is
opened, no `uvicorn` exists, and nothing between two processes can disagree. The residue had been
recorded for several slices — *the server behind these mocks is a mock* — and it is a residue no
number of additional unit tests can shrink, because the missing piece is a process boundary rather
than a branch. `backend/tests/smoke_journey.py` supplies that boundary: it boots `create_app` under a
real `uvicorn` on a free loopback port and drives **17 steps / 108 checks** with `httpx`, which was
already a dev dependency.

- **What it asserts that nothing else can.** (1) The error envelope. `frontend/src/lib/api.ts` reads
  `error.code` and `error.message` off every non-2xx body; a server answering 401 with a bare
  `{"detail": ...}` passes most of the backend suite while breaking every screen, so `envelope()` is a
  real assertion in both directions. (2) The operator's bootstrap order: `create-admin` reads its
  password from stdin (never argv) and here runs against a server that is **already accepting
  requests**, which is the order a deployment actually experiences and no test exercised. (3) The
  whole pipeline once, over HTTP: ingest → duplicate ingest → upload → `extract` → queue → reveal →
  stale-version 409 → accept → consent → decide → schedule → escalate → customer reply → refresh
  rotation → logout. (4) That a `500` anywhere fails the walk-through, because an unhandled exception
  escaping a route the UI calls is exactly what a happy path is best at finding.
- **A file-backed database, and the reason is the harness.** `conftest.py`'s in-memory SQLite uses
  `StaticPool`, which hands every session the same DBAPI connection so several sessions can share one
  database — and which cannot be used from two threads at once. `uvicorn` runs sync routes in a worker
  thread, so reusing that fixture would have made the *harness* the flaky component. The script
  creates a real file, sets `check_same_thread=False`, and enables `PRAGMA foreign_keys=ON` for the
  same reason `conftest.py` does.
- **Both transports off, and not as a shortcut.** `EVENT_TRANSPORT=none` and
  `NOTIFICATION_TRANSPORT=none` mean no step can reach a broker or a mail server, so the journey stays
  a statement about the HTTP seam. `argon2_*` is lowered exactly as `conftest.py` lowers it: the code
  path is identical and only the work is smaller.
- **Not pytest-collected, deliberately.** The file is `smoke_journey.py`, not `test_*.py`, so the
  suite does not import it and the journey runs once, as a journey, rather than as 108 separately
  failing cases behind one broken step. It exits 0 on success; on failure it prints the failing step
  and the response body, and **keeps** the temporary database so the rows behind the broken step can be
  inspected. On success the database, document store and key are removed.
- **CI only, and the omission is reasoned.** The job sits after `pytest` in the backend job. There is
  **no pre-commit hook**, because the commit gate runs `pre-commit run --all-files` and does not run
  `pytest` either: gating the journey alone would put a `uvicorn` boot on every commit while the
  867-test suite still ran only in CI. Consistent, and stated rather than left as an omission.

**The mutation passes, because a first-run green is when to distrust it.** Two deliberate breakages,
each reverted with `git diff` confirmed empty before the commit that landed the script:

1. `app/main.py`'s `return {"error": error}` → `{"detail": error}`. The journey failed at the envelope
   step on the real 401. `envelope()` has teeth.
2. `_fingerprint` in `api/v1/contracts.py` gained a `uuid4()` nonce. The journey failed with
   *duplicate ingest returned 201*.

**The second pass produced something the entry would be wrong to omit:** disabling the `by_sha256`
pre-check did **not** break the journey — the duplicate's 409 comes from the unique constraint further
down, so the pre-check is an optimisation and not the guarantee. Mutating the fingerprint is what
actually tests idempotency. That distinction was invisible before the mutation was tried.

**Two defects in the script itself, both caught by gates rather than by rereading.** A `# noqa: S105`
was dead (`backend/tests/**` already ignores `S105`); and `SmokeFailure` violated ruff's `N818`
(exception names must end `Error`) and became `SmokeJourneyError`, which keeps `AssertionError` as its
base because that is what it is — a documented exception to nobody.

**Verified.** Journey **108/108**, exit 0. `ruff check backend` — all checks passed; `ruff format
--check backend` — 108 files already formatted; `mypy` — *Success: no issues found in 62 source
files*; `pytest -m "not integration"` — **867 passed, 2 skipped, 19 deselected**. The journey's own
output carries one `refresh_reuse_detected` warning line, which is the deliberate password-grant-style
replay being logged, not noise from the harness.

**Honest limits.** (1) **Verified on Windows with SQLite and never on `ubuntu-latest`** — the CI step
is read, not observed, exactly like `D10-46` and `D10-47`. (2) **It is not a substitute for the
suite**: one happy path and a handful of refusals, with no Kafka, no SMTP, no MySQL and no browser, so
it says *the served API works as wired* and not *every branch is correct*. (3) **`mypy` does not cover
it** — `[tool.mypy]`'s `files` is `backend/app`, so the script is linted and executed but not
type-checked, exactly as the rest of `backend/tests` is; adding it would have meant a config change
whose only customer is one file. (4) **A real socket is still not a real deployment**: no TLS, no
proxy, no container, and `127.0.0.1` on a laptop is the whole network topology it has seen.

**Landed as** `test(smoke)` `5f2b085` — one new file, one CI step and one corrected workflow comment
(which still claimed `next build` was deliberately omitted, false since `20ccc7c`). No dependency, no
lockfile line, no application source, no migration (`0006` stays head). `STATUS.md`'s *the server
behind these mocks is a mock* residue is narrowed by the follow-up `docs(status)` commit, the
convention `D10-40` through `D10-47` all used.

### D10-49 — The workflow's first defect was in the workflow, and only running it could find it

`D10-46` wrote six jobs and copied every command out of a file in this repository, and `D10-48` added
a seventh *step* with the same rule. Both entries said the same thing about the result: read, never
observed. This entry is what happened when the commands were finally **executed** instead of compared.
Every one of them now has an exit code behind it, and the first red job CI would have produced was
`hygiene` — for a reason that has nothing to do with any file in the tree.

- **The defect.** The `hygiene` job ran
  `pre-commit run --all-files check-yaml check-toml check-json …` — eleven hook ids in one
  invocation. `pre-commit run` takes a **single** hook-id positional (`nargs="?"`, *"A single
  hook-id to run"*), so everything after the first is an unrecognized argument. Reproduced before it
  was fixed: `pre-commit: error: unrecognized arguments: check-toml check-json`, **exit 2**, raised
  by `argparse` before a single hook ran. On a runner that is a red `hygiene` job, and because
  `gate` lists `hygiene` in `needs` and runs `if: always()`, it is a red **pipeline** — on the first
  push, for a reason unrelated to the commit being pushed. Which is precisely the failure `D10-46`'s
  own third design rule warns about: *a pipeline that is red on its first run teaches people to
  ignore it.*
- **Why reading could not have found it.** The line looks like the command the local gate runs, and
  the local gate is `pre-commit run --all-files` — valid, because it names *no* hooks. The bug lives
  only in the difference between *run all* and *run these*, and that difference is invisible to every
  gate in this repository: `.husky/pre-commit` never passes a hook id, `yaml` parses fine, and
  `check-yaml` (which does run) validates syntax, not `argparse`. It is the same shape as `D10-47`:
  a file that is *syntactically* correct and *semantically* unrunnable, found only by executing it.
- **The fix is the fix a shell gives you.** One hook per invocation, in a `for` loop, over the same
  eleven ids in the same order. `bash -e` is the default shell for a `run:` block, so the loop stops
  at the first hook that fails instead of reporting the rest. The comment above it now says why the
  loop exists, in the same place a future reader would otherwise "simplify" it back into a list.
- **Verified by executing the loop in real bash**, not by reading it: all eleven hooks passed,
  `LOOP_EXIT=0`. That run also settled a question the list form had hidden — `forbid-submodules`
  reports `(no files to check)Skipped` on a tree with no submodules, and **still exits 0**, so a
  skipped hook is not a failed job. Under `-e` that mattered; a non-zero skip would have failed
  `hygiene` on a correct repository.
- **The other six commands now have exit codes too**, run verbatim as the workflow writes them:
  `ruff check backend` (all checks passed), `ruff format --check backend` (108 files already
  formatted), `python -m mypy` (62 source files, no issues), `bandit -c pyproject.toml -r
  backend/app -q` (exit 0, three `B104` `nosec` warnings at `core/config.py:50`), and `npm ci` in
  **both** node trees — `frontend/` (273 packages, in sync) and `demo/frontend/` (54 packages, in
  sync). The two `npm ci` runs are the ones worth naming: `npm ci` fails on a lockfile that has
  drifted from `package.json`, and `D10-43` added eight dev dependencies, so an out-of-sync lockfile
  was the second candidate for a first-push red. It is not out of sync.

**Honest limits.** (1) **Still never run on a runner.** These are the same commands on Windows; the
first runner execution remains unobserved, and `ubuntu-latest` differs in shell, paths and line
endings. (2) **Four of the seven jobs were not executed end to end.** `mysql-integration` needs a
MySQL 8.4 service container, which does not exist here; `gitleaks` needs Docker; `gate` needs `jq`
and real `needs` results. (3) **`npm ci` was run with `--ignore-scripts`** locally to keep the
verification to lockfile resolution — reading both `package.json` files confirmed neither defines a
`prepare` script, so plain `npm ci` adds no lifecycle step, but that equality is inference from the
files rather than an executed run. (4) **This proves the commands are runnable, not that the pipeline
is green**: a runner may still fail for a reason no local execution can reach, and the honest
statement about CI remains that its first real result is unknown.

**Landed as** `ci(workflows)` `93fdd56` — one job's `run:` block replaced by a loop plus its
rationale comment, and no other file. No dependency, no lockfile line, no application source, no test,
no migration (`0006` stays head). The `STATUS.md` claims that CI's every command is *read, not observed*
are amended by the follow-up `docs(status)` commit, the convention `D10-40` through `D10-48` all used.

### D10-50 — The third screen test, and the first whose state is the address

`D10-45`'s fourth limit said porting this harness was *three harnesses rather than a loop*, because
the escalation and agent screens import `useSearchParams` and `Suspense` and the review queue did
neither. This slice builds the first of those three. It is not the cheapest screen left — the detail
page would have been simpler — but it is the one that tests the harness rather than the page: if a
screen whose entire filter state lives in the address can be pinned in `jsdom`, the remaining ports
are copies of a proven shape.

**What is being pinned, and why it needed its own file.** `contract-scope.test.ts` proves the parser
round-trips and proves nothing about whether a *page* obeys it. `D10-36` moved both filters into the
query string on the promise that the back button, a bookmark and a shared link all keep working;
until this commit that promise was prose in a comment. The rules the source states and nothing had
checked: the working queue asks for `open` + `in_progress` and **omits** `contractId` rather than
sending an `undefined` one; a scoped address narrows that read and names the contract; an address
naming something that cannot be a contract performs **no read at all** — falling through to the
tenant-wide queue would answer a question nobody asked; each navigation **carries the other filter**
instead of dropping it; a typed id is submitted trimmed and its unusable form is never navigated;
`claim` sends no `status`, because the server derives the transition; `resolve` echoes the `version`
it was shown and **omits an empty note** rather than storing a note that says nothing; resolving is
not responding — the reply route is called with a channel and its outcome is reported on the page
because the card usually disappears with it; the two `409` codes are named separately; lateness is
the server's `overdue` flag; and an outage never renders as an empty queue.

**The harness adds no dependency and no new pattern.** The `QueryClientProvider` is real with a
client built per test, so one test's cache cannot answer another's question. The message catalogue is
real, so every assertion is against the string a user reads. `AuthProvider` and the session store are
real, because the role gating is read from the session and not from a prop. Only
`escalations.queue`, `escalations.update` and `escalations.recordResponse` are replaced, each through
`importOriginal` so the genuine `ApiError` survives — the screen branches on `instanceof` and on
`cause.code`. `retry: false` is the one deviation from the shipped provider and it is recorded for
the same reason `D10-45` recorded it: these tests ask *how often the screen asked*, and a retry policy
would answer that question in its place.

**`next/navigation` is the second double, and it is where this screen differs.** There is no Next
runtime under `jsdom`, so `useSearchParams` reads a **mutable address the test writes**. That is a
real limit and it is also the right shape for these assertions: the screen must derive what it asks
the server from the address alone, so a test that writes the address is exercising exactly the input
the rule is about, and a real router would only add a rendering pass to the same question. What it
cannot prove is stated below rather than implied away — `router.replace` being *called* is not Next
acting on it, so a navigation test asserts the URL the screen asked for and then writes that URL
itself before re-rendering. The seam is the request, not the transition.

**Teeth were measured by breaking the screen, and all four breakages were reverted afterwards.**
Four mutants, four *different* tests failing:

1. `enabled: !scopeIsInvalid` → `enabled: true` failed *refuses to read anything when the address
   names an impossible contract* — which is the assertion that the no-read rule is load-bearing
   rather than decorative.
2. The resolved toggle dropping `withContractScope` failed *filters by navigating, carrying the other
   filter with it* — it navigated to `?resolved=1` where the test wanted `?contract=12&resolved=1`.
3. `resolution_note: trimmed` with the conditional spread removed failed *resolves with the version
   it was shown and omits an empty note* — the request carried `resolution_note: ""` where the route
   accepts a note only alongside the transition.
4. Recomputing lateness as `new Date(row.sla_deadline).getTime() < Date.now()` failed *takes lateness
   from the server rather than recomputing it*, which is `D10-20`'s derived-not-stored rule reaching
   the browser.

All four were reverted with `git restore` and the page is **byte-identical to `HEAD`**
(`git diff HEAD --exit-code`), which is the check that matters: a mutation pass that ends with a
mutated tree proves the opposite of what it claims.

**One defect found in the test itself, and the screen was left alone.** A card's title was being
derived from a row whose `status` the test had overridden, so the assertion looked for `· open` on a
card rendering `· in progress` — the test was wrong and the page was right. It was fixed in the test
rather than accommodated in the source, which is the only direction that keeps a pin honest.

**Verified.** The spec is **25/25**; the whole frontend suite is **122 passed in 7 files** (was 97 in
6, so the new file accounts for all 25); the commit gate ran all nineteen hooks green on the exact
tree that landed, including `frontend tests (vitest run)` and `frontend typecheck (tsc --noEmit)`.

**Honest limits, four.** (1) **`jsdom`, not a browser** — no layout, no real navigation, no `fetch`,
so this proves the screen's half of the seam and not the server's, and no screen test in this
repository puts a real screen in front of a real server. (2) **The router is a double and the
assertion stops at the request** — `replace` being called is not Next honouring it, and the `Suspense`
boundary is present but a `jsdom` fallback render is not a streaming pass. (3) **The `409` envelope is
a fixture here** — `api.test.ts` pins the client's parse and `smoke_journey.py` pins the server's
bytes, so the shape is proven from both ends and still not across. (4) **Rules, not branches** — this
pins the behaviour the source states in prose; it is not a coverage figure. Of the six screens still
without a test, **two need this harness** — the agent screen and notifications, the only other
`page.tsx` files importing `useSearchParams` (checked by search, not recalled) — and both are
untouched; the contract detail page doubles route params instead, which is a related but different
double. `D10-45`'s prediction named the escalation and agent screens; it was right about the shape and
incomplete about the count.

**Landed as** `test(web)` `dd41c04` — one new file, 718 insertions, and **no source change**: the
screen already behaved this way and the spec exists to keep it that way. No dependency, no lockfile
line, no backend file, no migration (`0006` stays head). `STATUS.md`'s *two screens of nine* claims,
and `D10-44`'s *React Query is still unexercised*, are amended by the follow-up `docs(status)`
commit, the convention `D10-40` through `D10-49` all used.

### D10-51 — The first run, and the secret scan that was never scanning

`D10-46` through `D10-50` all carried the same limit, in their own words: **the workflow has never
run**. At `20:03Z` on 2026-11-08 that was true and stated plainly; at `20:43Z` the same tree reached
`origin/main` and run #1 executed all seven jobs. The limit is now closed, and what closed it is
worth recording separately from the defect it exposed, because the two have different lessons.

**What run #1 established.** Six of seven jobs green on `ubuntu-latest`: `python static` (ruff, `mypy
--strict` over 62 files, bandit), `backend tests` (the SQLite suite, then the smoke journey on a real
uvicorn socket — the one artifact `pytest` never collects and no suite would have noticed breaking),
`backend integration tests (mysql 8.4)` against a live service container **with skips treated as
failures**, so the guard proves the MySQL-only behaviour actually ran rather than skipping politely,
`frontend` (typecheck, eslint, vitest, and `next build`), and `demo frontend`. For the first time this
repository has evidence from the platform it claims to run on, in bash, on Linux paths and LF endings,
rather than from Windows and PowerShell. `D10-49`'s eleven-hook `for` loop ran as the fix intended.
And **`gate` went red for the reason it exists**: one job failed, so the single required check reported
the failure. That is the first end-to-end demonstration of the gate doing its job, and it is worth as
much as a green would have been.

**The red step, and how it identifies itself.** `hygiene + secret scan` failed at `gitleaks` — with
`started_at` and `completed_at` **the same second**, `20:43:24Z`. A scan that had pulled an image,
walked the tree and found a secret cannot finish in zero seconds. Two independent signals then pinned
it: the step's duration, and the command the step ran. Reproduced locally, first attempt, verbatim:

```
$ pre-commit run gitleaks --all-files
No hook with id `gitleaks` in stage `pre-commit`
exit 1
```

**`pre-commit run <id>` filters the named hook by stage too.** The hook is `stages: [manual]`
(`D1-03`), so `--all-files` alone makes pre-commit look for it in the `pre-commit` stage, not find it,
and refuse — exit 1, before the hook environment is built, before Docker is touched, before anything
could have been scanned. The error message names the stage it searched, which is the entire diagnosis
in one line. Adding `--hook-stage manual` makes the hook *found and attempted*: verified locally, it
then fails on a different error entirely, because Docker Desktop is stopped on this machine and this
hook is a `docker_image` hook. That difference — "no hook with that id" versus "cannot reach the
Docker daemon" — is what distinguishes a refusal from a scan that ran.

**Why nothing in this repository could see it.** The hook is manual, so no commit ever ran it. `D10-49`
fixed the eleven-id loop in the same job and deliberately left this separate one-line step alone — but
it verified that step by *comparing* it to the hook's own comment, and the comment carried the same
omission: both said `pre-commit run gitleaks --all-files`. **Two documents agreeing is not evidence
that either is correct**, and here the agreement was the artefact that made the defect look verified.
It is the same shape as `D10-49` one layer down: a command that reads perfectly and cannot execute.

**The fix is two lines and the reason is written next to both.** `--hook-stage manual` in the workflow
step and in `.pre-commit-config.yaml`'s own instruction to the operator, with a comment recording that
the flag is required rather than decoration and quoting the error a future reader will otherwise
re-derive from scratch.

**Honest limits, four — one of which `D10-68` later closed and one it narrowed.** (1) ~~**`gitleaks`
has still never scanned anything**~~ — **closed:** run #2 and every run since are green, and the
head run lists `gitleaks` as a succeeded step, so the first real execution happened and found
nothing. (2) **The log text is unreadable from here** — GitHub returns `403` for job logs to an
unauthenticated client and `gh` is not installed, so the diagnosis rests on a local reproduction of
the identical command plus the timestamp argument, not on the runner's own words. `D10-68` narrows
that: the log *text* is still out of reach, the *conclusions* never were. (3) **Required status
checks are still not enabled on `main`**, so a red `gate` has never actually blocked a merge; the
branch is pushed to directly and the gate has only been observed failing, never observed gating.
(4) **The action versions are untouched** — `checkout@v4`, `setup-python@v5` and `setup-node@v4` all
ran with Node 20 deprecation warnings; cosmetic, and bumping them is a change that deserves its own
green run rather than a rider on this one.

**Landed as** `ci(workflows)` `0eb80d5` — two files, and neither is application code. No dependency, no
lockfile line, no backend source, no test, no migration (`0006` stays head).

### D10-52 — The fourth screen test, and the two actions it must not confuse

The **agent decision screen** is the one the notification slice reads from: `POST /notifications`
schedules from a *stored* decision, so a recommendation nobody can inspect is a recommendation that
decides by default. Until now that route family had only ever been reached by `TestClient` — which can
prove the routes refuse the right things, and cannot prove the screen asks for the right things.

**What this screen carries that no other screen could reach.** `D10-45` predicted *three harnesses
rather than a loop* and named the escalation and agent screens; the count was right and the shape of
the work was not identical. The agent screen is the second whose state is the address, but it is the
first where **two different actions look interchangeable and are not**:

* **a re-run appends; a correction edits.** "The policy changed its mind" is a new fact and gets a new
  row; "a person disagrees with *this* row" is a fact about that row, so the row is rewritten in place
  with `original_channel` preserved. The two hit different routes, and the screen reports their
  progress and their refusals separately — matching on `contract_id` alone would light up every
  historical row of the same contract.
* **the correction routes are addressed by contract, not by decision.** The server resolves "the latest
  decision for this contract" itself, so acting on a superseded row would send a `version` it has
  already moved past — every click a `409`. The page restates the server's `latest_for_contract` rule
  over a list it already holds ordered newest-first, which is what lets it hide corrections the server
  would refuse.
* **a refusal may carry `details.permitted`, and the client must not compute it.** Re-deriving the
  permitted set from consent flags in the browser would put a second copy of the contact policy where
  it could drift from the one that actually refuses. The screen offers all three channels, lets the
  policy answer, and then reports what the policy said.

**The harness is reused, not rebuilt** — the one `escalations/page.test.tsx` established: a real
`QueryClientProvider` with a fresh client per case and `retry: false` (these tests ask *what the screen
asked and how often*, and a retry policy would answer that in the screen's place), the real
`NextIntlClientProvider` with the real catalogue, the real `AuthProvider` over the real session store
so role gating is read rather than passed in. Only `agent.decisions` / `decide` / `override` /
`flagReview` are replaced, each through `importOriginal`, so the genuine `ApiError` survives — the
screen branches on `instanceof` and on `cause.code`, so a re-exported class would silently make every
refusal generic. `next/navigation` is the second double, and the *point* of the spec: there is no Next
runtime here, so `useSearchParams` reads a mutable address the test writes, which is how the suite can
see that every request the screen makes is derived from the address alone. **No new dependency and no
provider the previous harness did not already pay for.**

**23 cases**, pinning: a signed-out visitor is asked to sign in and the API is asked nothing; a viewer
reads decisions and is offered no way to change one; the unscoped read sends no contract filter and a
scoped address narrows that same read; an address naming something that cannot be a contract performs
**no read at all**, rather than falling through to the tenant-wide history under a filter that never
ran; the scope sentence quotes the server's echo rather than the address; the filter navigates with
`replace` and a trimmed id, and an id that cannot be a contract is never navigated; a re-run calls the
appending route and reloads the list, and reloads even when it is refused, because the list has gone
stale; an override echoes the `version` it was shown and **omits** a reason that says nothing;
`CHANNEL_NOT_PERMITTED` names the permitted channels in the reader's language and degrades to a generic
sentence when the envelope carries no usable list; corrections appear on a contract's newest row only,
and an older row says so; a row states what was overridden, by whom, and how many re-runs preceded it;
and *the API is down* never looks like *nothing has been decided*.

**Mutation evidence, three passes, each applied to `page.tsx` and reverted.** `enabled:
!scopeIsInvalid` → `enabled: true` fails **1** case; removing `latestIds`' dedup fails **9**; replacing
the override's omit-when-empty spread with a plain `reason` field fails **1** — the case named *"echoes
the version it was shown and omits an empty reason"*. The third is worth recording for a reason beyond
its count: it was the pass that **caught the harness rather than the harness catching it**, because it
was still applied live when the previous turn ended. The tree carried it overnight and `git status`
carried it, which is the argument for running the mutant *and* reverting it inside the same turn rather
than trusting a note that says the file is clean.

**Honest limits, five.** (1) The screen's rules are proven against a **doubled API and a doubled
address**: `jsdom`, no browser, no real router, no server. (2) The `409` envelopes, including
`CHANNEL_NOT_PERMITTED`'s `details.permitted`, are **fixtures pinned from the screen's side**; the
route's shape is asserted here and not verified against `backend/app/api/v1/agent.py`. (3) *Wired is
not behaving* still holds — `router.replace` being called is not Next acting on it. (4) **Five of nine
screens still have no component test**; notifications is now the only remaining `page.tsx` importing
`useSearchParams`, so the next port is a copy of a proven shape, and the four left are a different
shape rather than a harder one. (5) The viewer gate remains the client's opinion of a role the server
decides in another suite.

**Landed as** `test(web)` `585dcd7` — one new file, 665 insertions, **no source change**: the screen
already behaved this way and the spec exists to keep it that way. No dependency, no lockfile line, no
backend source, no migration (`0006` stays head). `STATUS.md`'s *three of nine* claims are amended in
place by the follow-up `docs(status)` commit, the convention `D10-40` through `D10-51` all used.

### D10-53 — The fifth screen test, and the last screen that needed this harness

The **notification queue** is where the contract in `D10-52` is cashed: `POST /notifications` schedules
from a *stored* decision, and a scheduled reminder that nobody can inspect is a reminder that leaves
by default. This is also the **last `page.tsx` importing `useSearchParams`** (checked, not recalled:
agent, escalations, notifications — three, and this was the third), so the harness `D10-50` built and
`D10-52` reused is now **finished rather than started**. The prediction in `D10-45` — *three harnesses
rather than a loop* — is spent: every address-and-`Suspense` screen has a test.

**What this screen carries that the other four could not reach.** It is the first screen with a
**narrow, status-gated action**: a row may be dispatched only from `pending`, so the control must
disappear rather than fail on every other status — the difference between a button that is absent and
a button that is always wrong. It is also the first where **the same mutation has two failure
meanings**: a refusal can park the row `failed` *before* answering, and that transition is committed,
so a `409` and a `404` both mean *the page the operator is reading is no longer the page the server
would answer*. And **quiet hours are the server's verdict, not a calculation**: the screen renders
deferral the dispatch policy decided in the customer's timezone, because a second copy of that rule
in the browser would drift from the one that actually defers.

**Mutation evidence, three passes, each applied and reverted inside this turn, each caught by exactly
one case.** `canSend = status === "pending"` → `!== "failed"` fails **1** — the status-permission case.
Moving the deferral check *after* `sent` fails **1** — the quiet-hours case. Dropping `409` from the
stale-page reload fails **1**, at `page.test.tsx:663`. **One mutant, one guard is the honest reading
here, and it cuts both ways**: it says each rule is pinned, and it says each rule is pinned *once*
— unlike `D10-52`'s `latestIds` mutant, which failed nine cases because the rule it broke was
load-bearing in nine places. A rule with a single guard is a rule that a single deleted test would
silently free.

**The third mutant was live at the start of this turn, again.** `D10-52` recorded this exact
procedural failure and argued the mutant must be run and reverted in the same turn; the same thing
happened once more, in the next slice, one screen later. The note was written and not obeyed, which
is the finding: **the argument needs to be a check in the workflow, not a lesson in a decision
record.** What caught it was mechanical — `git status --porcelain` at the top of the turn reported
`M .../notifications/page.tsx` before any work began. That command, not the previous paragraph, is
what did the work.

**Honest limits.** (1) As with all five: the rules are proven against a **doubled API and a doubled
address** — `jsdom`, no browser, no real router, no server. (2) `NotificationRow` and
`DispatchOutcome` are **fixtures pinned from the screen's side**, asserted here and not checked
against `backend/app/api/v1/notifications.py`; the refusal codes the screen translates are the ones
the fixtures declare. (3) *Wired is not behaving* still holds for every tested screen. (4) The
deferral case proves the screen **believes the server**, which is the rule — it does not and cannot
prove the server is right about quiet hours; that is `test_notifications.py`'s job. (5) **Four of
nine screens still have no component test** — the contract list, the contract detail, the home page
and users — and none of them needs `useSearchParams`, so the remaining work is a different shape
rather than the same one applied four more times.

**Landed as** `test(web)` `5d9a5de` — one new file, 787 insertions, **no source change**: the screen
already behaved this way. No dependency, no lockfile line, no backend source, no migration (`0006`
stays head). Full frontend suite **172 passed in 9 files**, from 145 in 8. `STATUS.md`'s *four of
nine* claims are amended in place by the follow-up `docs(status)` commit, the convention `D10-40`
through `D10-52` all used.

### D10-54 — The sixth screen test: a write onto the row being read

The **contract list** is the sixth screen under test and the first that owns a **write onto a row it
also lists** — a consent write lands on a row the same page drew, so the outcome belongs to that
row rather than to a global toast. It is also the **second** screen spec in the set that **mocks no
router at all** — `review/page.test.tsx` (`D10-45`) is the first — and the first draft of this record
said otherwise: `useSearchParams` appears in exactly three `page.tsx` files and all three were
finished by `D10-53`, but only **four** of the five specs before this one carry a
`vi.mock("next/navigation")` (login, escalations, agent, notifications), because the review page reads
no router hook and so its spec needs no double either. What is new here is not the absence of the
double but its cause: this page reads nothing out of the address at all, so the harness is now the
reusable part — a doubled API and two real providers — with nothing router-shaped left inside it.
So the prediction from `D10-45` and `D10-50` — that the screens left are *a different shape rather
than the same one applied again* — is tested here rather than asserted: the sixth is the first screen
whose state is entirely its own.

**What this screen carries that the five before it could not reach.** One form, **two routes**:
`POST /contracts` and `POST /contracts/document` take the same four content fields and differ only
in transport, so attaching a file is what switches routes — nothing on screen says *upload*. An
**empty optional field is omitted rather than sent empty**, because `""` is not a date and the route
answers it 422, which makes the payload's *shape* the thing to assert rather than its values. A
consent write **carries the version the row was drawn with**, never a copy the draft kept, because
the token means *the state I was looking at when I decided*. A **no-op save is a visible success** —
an identical submission answers the version it was given rather than a higher one, and that
difference is the only way to tell an operator their click changed nothing *because the flags
already matched*. And **the page appends**: paging hands the opaque `next_cursor` straight back, so
a scroll touches each contract once even while new rows arrive underneath.

**Mutation evidence, five passes, each measured in this turn, each reverted with a byte-identity
assertion.** Sending `metadata.start_date` unconditionally fails **2** cases; forcing the
`consent.data.version` comparison to `false` fails **1**; widening `canRecordConsent` to
`user !== null` so a viewer is offered the write fails **1**; returning no cursor from
`getNextPageParam` fails **1**; and dropping the `409` reload fails **1**. The first is the only
multi-case mutant, and the asymmetry is informative: the omitted-field rule is load-bearing in two
places while the other four are pinned once, so four of these five rules are rules that a single
deleted test would silently free — the same trade `D10-53` recorded, now the majority rather than
the exception.

**No mutant crossed a turn boundary this time, and the reason is procedural rather than
diligence.** `D10-52` and `D10-53` both ended with a live mutation, and both wrote that it must not
happen again. What changed here is that the *first* command of the turn was `git status
--porcelain`, which is exactly what `D10-53` concluded — the argument belongs in the workflow, not
in a record. It reported a clean tree, so there was nothing to revert. The check did the work; the
resolution did not.

**Two false greens were caught while gathering that evidence, and they are worth more than the
counts.** (1) `git diff --exit-code` against a path that **does not exist** exits `0`, so a
*page.tsx unchanged* check run from `frontend/` with a repo-relative path compared **nothing** and
reported success. The trustworthy proof is `git diff --name-status b4e5cb1 HEAD`, which lists
exactly one added file. A green check that checked nothing is indistinguishable from a green check
that passed, which is why the count of gates is a weaker claim than the identity of what each gate
read. (2) A verification script using text-mode `read_text`/`write_text` rewrote the source's
**CRLF as LF**, so the "reverted" file was byte-different from the committed one; `git status
--porcelain` reported it and the fix is byte-level read/write with an equality assertion after
every restore. The `[locale]` bracket glob bit a third time as well — `-LiteralPath`,
`--literal-pathspecs` and `git diff --name-status` are what make these checks mean anything. (3) This
record's own first draft claimed the contract spec was **the first** in the set with no router double
*and* that the five specs before it each carried one; counting the `vi.mock` calls showed **four** of
five, with `review/page.test.tsx` carrying none because the review page reads no router hook. A claim
about a set is worth exactly the count behind it, and that count had not been run when the sentence
was first written — caught here before the commit rather than after, which is the only difference
that matters.

**Honest limits.** (1) The rules are proven against a **doubled API** — `jsdom`, no browser, no
router, no server. (2) `ContractListItem` and `ConsentState` are **fixtures pinned from the screen's
side** and were not checked against `backend/app/api/v1/contracts.py`; the refusal codes the screen
translates are the ones the fixtures declare, and the *503* sentence is a fixture — that a keyless
deployment answers 503 is the backend's claim, not this spec's. (3) Date formatting is asserted
through the same `Intl` call the screen makes, so the join and the zone choice are pinned, not
locale output. (4) *Wired is not behaving* still holds. (5) The viewer gate remains the client's
opinion of a role the server decides in another suite. (6) **Three of nine screens still have no
component test** — contract detail, home, users — and none of the three is this shape again: the
detail screen reads `useParams`, the home page reads `usePathname`, and the users page reads nothing
from the router at all.

**Landed as** `test(web)` `72538e2` — one new file, 901 insertions, **no source change**:
`frontend/src/app/[locale]/contracts/page.test.tsx` is the only path in
`git diff --name-status b4e5cb1 HEAD`. No dependency, no lockfile line, no backend source, no
migration (`0006` stays head). Full frontend suite **204 passed in 10 files**, from 172 in 9.
`STATUS.md`'s *five of nine* claims are amended in place by the follow-up `docs(status)` commit, the
convention `D10-40` through `D10-53` all used.

### D10-55 — The seventh screen test: five reads, and the one rule they all share

The **contract detail** screen is the seventh page under test and the first that **fires five
independent reads at once** — the contract, its extracted fields, its decisions, its notifications and
its escalation queue — each a separate `useQuery` with its own lifecycle. That is what the 45 cases
mostly spend themselves on: not a screen with more to say, but a screen where **four things can be
failing while a fifth succeeded**, so "the page is broken" is not a state it has. It is also the
**first screen that needs a `useParams` double**, which puts it back in the group `D10-54` had just
left: that amendment said the three remaining screens were shaped *differently from each other*, and
this one spends the first of the three — confirming the shape prediction rather than extending the
harness a seventh time. The harness therefore needed exactly one new piece: a `useParams` that returns
a **mutable id**, so the invalid-address cases can be driven without remounting. Two of nine remain,
home and users.

**The rule the whole screen hangs on is the route parse, and it is stricter than it looks.**
`Number.parseInt` runs first, but the integer is not trusted until `/^\d+$/.test(raw)` passes, so
`"12abc"` is **refused** rather than silently read as 12 — the exact coercion `parseInt` performs — and
`"0"` is refused by a separate `> 0` clause, and `""` never reaches a query at all. All five reads are
gated on the same computed `validId`, so the parse is one decision rather than five, and the spec pins
the refusal rather than the number: it asserts **no read is issued**, which is what distinguishes a
guard from a coercion that happens to produce a plausible id.

**Three other rules, each pinned once.** Each related section reports **the count its own response
returned** — `decisions.data.count`, not `items.length` — because the page receives a bounded page and
the total is the server's number; a fixture whose count exceeds its items makes the difference
visible. `may_contact` **decides** contactability and the consent flags only **explain** it, pinned
with a fixture where the two disagree, so the page cannot grow its own opinion about consent; the doc
comment at `page.tsx:355` argues this and the test is the argument's enforcement. And `404` is
described as *not found* **ahead of** the generic failure branch, while one failing read leaves the
other four intact — the ordering, not just the mapping.

**Mutation evidence, three passes, each measured and reverted inside this turn.** Dropping
`&& contractId > 0` from the route guard fails **1** case; replacing `may_contact` with `consent_email`
fails **1**; replacing `decisions.data.count` with `.items.length` fails **1**. Three mutants, three
catches, one case each — the same trade `D10-53` and `D10-54` both recorded, now unanimous rather than
a majority: **every rule this spec pins is pinned exactly once**, so deleting a single test frees a
rule silently. The reverts are proven by `git diff --name-status HEAD` printing **nothing**, not by
`--exit-code` against a path, which is `D10-54`'s false-green lesson applied rather than re-learned.

**Two harness defects, both mine, both found by the run rather than by reading.** (1) Four cases failed
in **4 ms** because a card-scoped query (`within(relatedCard())`) ran before `contracts.get` resolved,
so the card it scoped to did not exist — the fix is awaiting the contract title before scoping, in nine
places rather than the four that failed, since the same race was latent in five passing tests. A fast
failure is a harness signal rather than a screen signal, and the duration was the clue. (2) The locale
case looked related links up **by their English label**, which is translated in `bn`, so it passed in
`en` for the wrong reason; the links are now found by **the `?contract=` parameter they carry**, which
incidentally makes the assertion about `withContractScope` rather than about copy. The second defect is
the more interesting one: a test that reads a translated string to find an element is a test that only
holds in one locale.

**A mutant did cross a turn boundary, and this record differs from the one the previous three
predicted.** This slice's first turn ended with `row.consent_email` staged in the source at
`page.tsx:359` and the spec green at 45 cases. What prevented a bad commit was not the check —
`D10-52` through `D10-54` all said `git status --porcelain` must be the turn's first command, and that
command could not run, because the turn had already ended. It was the **written record of the mutation
in the previous turn's summary**, naming the exact line and the exact revert, so the next turn's first
action was the revert. Procedural memory carried what the gate did not. A workflow rule that only
holds when a turn ends cleanly is not yet a rule.

**Two things recorded rather than fixed, both needing an owner.** (1) `contracts.detailRelatedNote` in
**both** `en.json` and `bn.json` still tells the reader the related links open *"unfiltered: none of
them reads a contract out of its address"* — false since the links were changed to carry `?contract=`
through `withContractScope`, and the screen's own doc comment at `page.tsx:130` repeats the same stale
claim. The JSX comment above the note already admits both sentences are false; the **copy the reader
sees was never updated**. It was not changed here because a string edit is a product decision: the spec
asserts the **linkage**, not that sentence. (2) The route guards **disagree across the codebase**:
~~this screen accepts `/^\d+$/` and a positive integer, so `"07"` reads as contract 7, while
`contract-scope.ts` — which every related link is built with — uses `^[1-9][0-9]*$` and refuses it.
Both behaviours are pinned by tests, so the divergence looks deliberate rather than accidental, and
reconciling it is a product call rather than a test's.~~
**It was reconciled in `D10-76`**, on the side `D10-60` had already measured as the one matching the
API: the module now reads a padded id as the same contract, so a related link can carry
`?contract=07` without the bar refusing it.

**This entry had to be written twice, and the measurement that caught the first attempt is the
lesson.** The first pass produced a `D10-55` that ended mid-sentence with the **whole existing document
appended beneath it**: `git diff --stat HEAD` reported **6182 insertions across two files that should
have taken about one hundred and ninety**. Nothing was lost — the spec commit was already in, and both
files were restored from `HEAD` and the prose re-authored — so the cost was a turn rather than content.
What generalises is the check: on a docs commit, read `git diff --stat` before `git add`, because a
duplicated document contains no single wrong-looking line. The mechanism is not established here and is
not guessed at.

**Landed as** `test(web)` `f598214` — one new file, 926 insertions, **no source change**:
`frontend/src/app/[locale]/contracts/[id]/page.test.tsx` is the only path in `git show --stat HEAD`.
No dependency, no lockfile line, no backend source, no migration (`0006` stays head). Verification: the
spec **45/45**, the full frontend suite **249 passed in 11 files** (was 204 in 10, so the new file
accounts for every added case and nothing that existed changed), all nineteen pre-commit gates green
and commitlint passed. The honest limits: the rules are proven against a **doubled API and a doubled
router** — `jsdom`, no browser, no real router, no server; `ContractListItem`, `ContractField`,
`AgentDecision`, `NotificationRow` and `EscalationQueue` are **fixtures pinned from the screen's side**
and not verified against `backend/app/api/v1/contracts.py`; the `404`/`403`/offline envelopes the
screen translates are fixtures; date formatting is asserted through the same `Intl` calls the screen
makes, pinning the join and the zone rather than locale output; *wired is not behaving* still holds;
and **two of nine screens still have no component test** — home and users.

### D10-56 — The eighth screen test: rules that hold only because of who is asking

The **users admin** screen is the eighth page under test and the first whose subject is **permission
rather than data**: almost every rule it has is a statement about what one account may do to another.
It is also the third of the three shapes `D10-54` named — `useParams`, `usePathname`, and **no router
at all** — so that sentence is now *spent* in full rather than extended a third way: the spec doubles
exactly one module, `@/lib/api`, and `vi.mock("next/navigation")` is not in the file at all. What 37
cases against a 519-line screen spend themselves on is the pair of rules that only exist between two
accounts — **an admin sees the list and a viewer never learns it exists**, and **the caller's own row
carries no controls** — plus the one write that returns a secret it can never return again. One of the
nine remains: home, which is the screen that reads `usePathname`.

**The role gate is on the query, not on the controls.** `isAdmin` is computed once (`page.tsx:85`) and
`users.list` is gated by `enabled: isAdmin` (`page.tsx:98`), so an operator who is not an admin issues
**no request at all** and reads one sentence instead of a failed one. The spec therefore asserts the
absence — *asks the route nothing* — rather than an error rendering, which is the same distinction
`D10-55` drew about `validId`: a gate is proven by the read that never happens, not by the message that
follows one. Mutating `enabled: isAdmin` to `enabled: true` fails the case named for it, 1 of 37.

**The self row is the other rule that needs a second account to exist.** `canReset = !isSelf &&
row.is_active` (`page.tsx:429`) hides the reset on the caller's own row — because changing your own
password is `change-password`, not an admin reset — and on a deactivated row, because the server will
refuse that and the screen should not offer what will be refused. The two ends are pinned separately:
one case finds **no control at all** on the self row, another finds the deactivated row *saying* it
cannot be reset instead of offering it. Dropping `!isSelf` fails the first, 1 of 37, and each rule is
pinned exactly once — the trade `D10-52` through `D10-55` all recorded, and this is the fifth
slice in that row.

**`MIN_PASSWORD_LENGTH = 12` is a hint and the server is the gate, which is pinned as a round trip.**
The create button is disabled on `create.isPending || email.trim() === "" || password === ""`
(`page.tsx:248`) — **presence**, not length — so a short password still **reaches the server**, and the
screen's job is to translate the `422` it gets back rather than to pre-empt it. That is deliberate: a
second copy of the floor in the client is a second number that can drift from
`backend/app/api/v1/auth.py`. The spec asserts the short password is *sent* and the `422` is
translated, and separately that the hint names the server's floor, so the number the page shows and the
number the route enforces are one claim in two places rather than two policies. The update path is
pinned the same way in the opposite direction: `users.update` sends **only the changed field**, never
both, so a role change cannot silently rewrite `is_active`.

**Then the finding this slice is worth recording for: one mutant survived, and the test that let it
through had been passing for the wrong reason.** Three mutants were caught on the first measurement;
the fourth — `email.trim() === ""` → `email === ""` at `page.tsx:248` — **survived**, with the case
named *keeps the button disabled for an email that is only whitespace* still green. The cause is not
the screen. The field is `<input type="email">`, and the value sanitization algorithm the HTML spec
gives that input type **empties a value that is only whitespace**, so `fireEvent.change` with `"   "`
leaves `input.value === ""` and the two predicates are indistinguishable. That was proven *outside* the
suite (one `node -e` running jsdom directly, printing `""` for `"   "` and `"a@b.test"` for
`"  a@b.test  "`) and then reproduced by re-running the mutant, not inferred from the failure. The fix
is the value the guard actually exists for: **U+00A0**, which survives the sanitizer and is still
removed by `String.prototype.trim()` — the paste-from-a-word-processor case. Both whitespace cases now
type `NBSP` (`page.test.tsx:92`, used at 404 and 423) and the mutant fails. A guard can be pinned by a
test that cannot tell it from its replacement; only a mutation run tells those apart, and a green test
is not the evidence.

**A harness fact that cost thirteen type errors because it was assumed rather than checked: there is no
`jest-dom` here.** `toBeDisabled`, `toBeEnabled`, `toHaveValue` and `toHaveAttribute` do not exist —
the grep returns nothing — and every earlier spec uses `getAttribute(...)` and the raw `.disabled`
property for exactly that reason. The file was written assuming the matchers, typechecked into thirteen
errors, and finished by converting them rather than by adding a dependency. One grep before the first
assertion would have cost nothing, which is the whole of the lesson.

**Two initial run failures, both the test's and neither the screen's**, found by reading the screen
rather than guessing: an **empty** organisation renders the empty sentence, but a lone **self** row
still renders a count, and one case had conflated the two — they are now two cases (*gives an empty
organisation its own words instead of a count*, and *counts the caller's own row like any other*, which
expects `1`). And the `bn` case awaited `users.title`, which renders **before** the list resolves, so
it could pass or fail on timing; it now awaits the account's email first and only then asserts the
title and the `/bn` home link. The second is `D10-55`'s locale defect in a new shape: a test that
awaits translated chrome is a test that can run ahead of its own data.

**Four mutation passes, four catches, each reverted inside the turn** — `enabled: isAdmin`,
`!isSelf && row.is_active`, `email.trim() === ""`, and `email: email.trim()` — with each revert proven
by `git diff --stat HEAD` printing **nothing**, which is `D10-54`'s false-green lesson still applied
rather than re-learned. **Landed as** `test(web)` `d3c84bb` — one new file, 822 insertions, **no source
change**: `frontend/src/app/[locale]/users/page.test.tsx` is the only path in `git show --stat HEAD`.
No dependency, no lockfile line, no backend source, no migration (`0006` stays head). Verification: the
spec **37/37**, the full frontend suite **286 passed in 12 files** (was 249 in 11, so the new file
accounts for every added case and nothing that existed changed), all nineteen pre-commit gates green
and commitlint passed.

`STATUS.md`'s *seven of nine*, *two of the nine* and *two of nine* claims are amended in place by
the follow-up `docs(status)` commit, in four regions, rather than left to drift.

**The honest limits.** The rules are proven against a **doubled API** — `jsdom`, no browser, no router,
no server; `UserSummary` fixtures are **pinned from the screen's side** and not verified against
`backend/app/api/v1/auth.py`; the `422` and the two `409` envelopes the screen translates are
**fixtures**; the one-time password is proven to survive a refetch of the list, not to be absent from
every later render; *wired is not behaving* still holds; and **one of nine screens still has no
component test** — home.

### D10-57 — The ninth screen test: the screen that reads no tenant data

The **home** page is the ninth and last locale screen to get a component test, so
`frontend/src/app/[locale]` has **nine of nine** ~~and the set is closed~~. **Amended by `D10-80`,
not deleted:** nine of nine was true of the nine that existed, and the *closure* was the claim that
expired — `D10-80` added a tenth screen, the attention board, with its spec in the same commit. The
count is now **ten of ten**, and the rule this sentence was really about is unchanged and still
guarded: `frontend/src/app/screens.test.ts` fails the suite when a screen ships without a spec. It
is also the smallest subject in the chain and the one that had waited longest, because it is the
only screen whose rules are not about data at all: it performs **no query**, which is why its spec
deliberately supplies **no `QueryClientProvider`** — providing one would hide a screen that began
reading tenant data without saying so.

**One question, and only one.** The liveness line asks `/health` and nothing else, and the spec
asserts that single probe in all four states (checking, ok, down, and signed in) plus the sentence
each produces. That is what lets the page render for a visitor whose API is dead, so the failure
that matters is not *the read failed* but *the page still stands*: a dead API must not blank the
links, the theme chips or the sign-in card.

**The two sign-out buttons fail differently, on purpose, and both guards are pinned separately.**
`signOut` clears the session locally *before* it tells the server and swallows the revocation's
failure — the token expires on its own and a failed sign-out must never look broken —
while `signOutEverywhere` is the remedy for a suspected leak, so its refusal is the one thing the
user must be told. The spec asserts both halves: `signOut` calls `auth.logout` with the refresh
token and shows no alert on a refusal, and `signOutEverywhere` sends the **access token**, never
calls `auth.logout`, reports `actionFailed`, and still drops the session in its `finally`. Mutation
evidence for the in-flight guard is deliberately two mutants rather than one — both buttons to
`disabled={false}` fails **1** case at line 423, and only the wider one fails **1** case at line 424
— because a single assertion covering both would have pinned the pair, not each button.

**The visitor is never gated, and the account card prints the wire role.** A signed-out visitor
gets all six working-screen links plus `/en/login`, in that order, every one locale-aware; hiding
them would leave nobody a way to learn the screens exist, and each screen asks for a session for
itself. The card prints the **wire** role (`admin`) while the users screen translates it — pinned
as current behaviour so a later translation becomes a deliberate change rather than an accident.

**The locale switcher is a page load, not a route push.** It calls `window.location.assign`
(swapping the `[LegacyUnforgeable]` descriptor in jsdom and restoring it in `afterEach`), marks
exactly one chip with `aria-current`, keeps the rest of the path (`/en/notifications` →
`/bn/notifications`) and drops the doubled slash (`/en/` → `/bn`). Mutating `segments[0] = target`
into `segments.unshift(target)` fails **3** cases, the first reporting `Received: "/bn/en"`.

**Two lessons, one about the DOM and one about the instrument.** The first is `D10-56`'s lesson in a
new shape and was found by *running* the spec rather than reading it: the connectivity heading
renders as `"Backend connectivity:"` — the colon lives in the same text node — so an exact
`getByText(EN.home.healthHeading)` threw; it is a substring match now, with the reason in a comment.
The second is about measurement rather than code: a wedged Python REPL left every command returning
fragments of the *previous* command's output, which is how an earlier turn's mutant run appeared to
produce nothing at all. The shell was re-probed (`exit()`) and the measurement repeated, and the rule
recorded is that a terminal must be shown to **echo what was sent** before any number taken through
it is trusted. A harness fact from `D10-56` was applied rather than re-learned: there is no
`jest-dom` here, so every assertion uses `getAttribute(...)` or raw `.disabled`.

**Landed as** `test(web)` `d8bc237` — one new file, 524 insertions, **no source change**:
`frontend/src/app/[locale]/page.test.tsx` is the only path in `git show --stat HEAD`. Spec **23/23**
against a 211-line screen; full frontend suite **309 passed in 13 files** (was 286 in 12, so the new
file accounts for every added case and nothing that existed changed); all nineteen pre-commit gates
green and commitlint passed. No dependency, no lockfile line, no backend source, no migration
(`0006` stays head).

`STATUS.md`'s living counts — *one of the nine*, *eight of nine*, *eight have one, and one does not*
and *eight of the nine* — are amended in place by the follow-up `docs(status)` commit, in five
regions, rather than left to drift.

**The honest limits.** The rules are proven against a **doubled API, a doubled `usePathname` and a
doubled `next-themes`** — `jsdom`, no browser, no next runtime, no server; the liveness sentence
proves the screen **asks** `/health`, not that the API answers; the `next-themes` double means a
chip's *request* for a theme is pinned while the document's colour (that library's promise) is not;
the wire-role assertion pins a difference from the users screen rather than a decision this slice
made; *wired is not behaving* still holds of all nine; and the root redirect at
`frontend/src/app/page.tsx` has no spec, because it is a redirect rather than a screen.

### D10-58 — The copy that told the reader the opposite of what the code does

The related-reads card on the contract detail screen links three screens, and those links have
carried `?contract=<id>` since they were built through `withContractScope`. The sentence beneath the
card's heading said the opposite — that the links open those screens *"unfiltered: none of them reads
a contract out of its address"* — and `D10-55` recorded it rather than fixing it, on the ground that
a string edit is a product decision. That was the right call for the half it was about and the wrong
reading of this half: **the first sentence of that note was already true and the second was already
false**, and a false sentence the reader sees is not a choice between two products.

**What made it a defect rather than a preference.** Two comments in the same file had already
conceded the point. The JSX comment above the note says *"both are now false, so the note says what
the card does"* — and then the note said the old thing anyway. The page's doc comment still listed
*"does not filter the other screens"* as one of the rules the screen holds itself to, which is the
inverse of what line 592 does. So the code carried three copies of one fact: two of them updated, one
of them the copy the reader actually sees. A comment that says the string is wrong is a note to the
next turn, not a fix, and it had survived two turns of reading past it.

**Both locales, and the doc comment, in one change.** `en.json:327` and `bn.json:327` now end the
note at *"the links carry that same id, so each screen opens already filtered to this contract"*, and
the doc comment's bullet is restated as *"does not own another screen's filter"* — this page hands
over the id, applying it stays the target screen's job, which is true of all three screens and states
the same fact without claiming this page does work it does not. The Bangla is a re-statement rather
than a translation of the new English: both sentences were written to say the same thing in the two
languages' own idiom, and the *"would look like a filter while quietly showing everything"* warning
survives nowhere, because the links do not.

**Verification.** `npx vitest run` in `frontend/` — **309 passed in 13 files**, exit 0, the same count
as before the change, which is the expected result and the point: no spec asserts
`contracts.detailRelatedNote`, so the suite that pins the *linkage* (`D10-55`'s 45 cases, which find
each related link by the `?contract=` it carries) stays green while the words beside it change. Both
message files still parse as JSON; each differs from HEAD at exactly one line, 327.

**The second half of `D10-55`'s note stays open, deliberately.** ~~The route guards still
disagree — `contracts/[id]/page.tsx` accepts `/^\d+$/` so `"07"` reads as contract 7, while
`contract-scope.ts` uses `^[1-9][0-9]*$` and refuses it. That one really is a product decision: it
changes what an address means, and both behaviours are pinned by tests. It is not touched here.~~
**It was left alone here and ruled on in `D10-76`**, which moved `contract-scope.ts` to the page's
side — the side the server already answered.

**Landed as** `fix(i18n)` `2cf97e2` — three files, 7 insertions and 6 deletions, **no behaviour
change**: no link, query, request or render path was touched. No dependency, no lockfile line, no
backend source, no migration (`0006` stays head).

**The lesson, and it is the chain's oldest one in its cheapest form.** `D10-56` and `D10-57` both
recorded *read what the thing actually holds* — the DOM, the suite, the terminal. This one is the same
rule applied to the file being edited: the JSX comment saying "the note now says what the card does"
was read as evidence that the note had been fixed, when the only thing it proved was that someone had
meant to. **A comment claiming a string was updated is not the string.** The check is one grep for
the sentence, and it would have cost less than the comment did.

### D10-59 — The id rule is stricter than the server, and the comment claiming otherwise is gone

`contract-scope.ts` justified its `^[1-9][0-9]*$` with *"the server's path parameter is an `int` and
its query parameters are `gt=0`, so the same rule holds here: whole digits, no leading zero, greater
than zero."* Both premises are true and the conclusion does not follow, so the sentence was measured
against the real ASGI app rather than argued about.

**What the server actually does** — a temporary `backend/tests/test_zz_id_probe.py`, run and then
deleted, using the existing `client` and `token` fixtures and one really ingested contract:

| Request | Answer |
| --- | --- |
| `GET /api/v1/contracts/1` | 200 |
| `GET /api/v1/contracts/01` | **200** — a padded id reads as contract 1 |
| `GET /api/v1/contracts/0`, `/contracts/-1` | 404 — the bare `int` lets both reach the handler |
| `GET /api/v1/contracts/12abc` | 422 |
| `GET /api/v1/agent/decisions?contract_id=01` | **200** |
| `?contract_id=0`, `?contract_id=-1`, `?contract_id=12abc` | 422 |

Neither surface refuses a leading zero. An `int` does not mean "no leading zero", and `gt=0` accepts
seven whether it arrived spelled `7` or `01`. The strict regex is therefore **the address vocabulary's
own choice**, one step narrower than the API it talks to, and the comment now says that instead of
claiming the server forced it.

**No behaviour change.** `readContractScope` returns exactly what it returned before; the module's
~~cases still pin `"012"` invalid~~, and `contracts/[id]/page.test.tsx:408` still pins `"07"`
reading as contract 7. ~~The divergence is untouched and remains the open product call `D10-55`
named.~~ What changes is that the reason on record was false, and an owner ruling given on a false
reason is a ruling about nothing. **`D10-76` then took the open call the way this slice had posed
it:** the module's cases pin `"012"` as contract 12 now, and `"07"` reads as contract 7 on both
sides.

**Landed as** `docs(web)` `8eecdd7` — one file, a comment replaced, **no behaviour change**: no
regex, no branch and no return value was touched, and the module's own cases were not edited to
match the new wording.

### D10-60 — The third copy of a false reason, crediting a line that cannot do the job

`contracts/[id]/page.tsx` justified its id guard with a sentence that named the wrong line and the
wrong reason: *"The route segment is a string. `Number.parseInt` is deliberate over `Number`: the
server's path parameter is an `int`, so `12abc` must be an invalid id rather than silently reading as
12, and the empty string must never become 0."* Two claims, and neither survives being measured.

**The conversion cannot refuse a tail.** Measured, not recalled:

| Expression | Value |
| --- | --- |
| `Number.parseInt("12abc", 10)` | **12** |
| `Number("12abc")` | `NaN` |
| `Number("")` | 0 |
| `Number.parseInt("", 10)` | `NaN` |
| `Number.parseInt("07", 10)` | 7 |

So `Number.parseInt` reads `"12abc"` as twelve exactly as `Number` would have, and the comment
credited it with a refusal performed one line below by `/^\d+$/`. Split the two shapes apart and
the argument for `parseInt` over `Number` disappears with the claim: the only input on which the two
functions differ *here* is the empty segment, `Number` reading it as 0 and `parseInt` as `NaN` — and
the empty segment already fails the shape test. **On every string the guard admits, the two agree**,
which makes the conversion a plain cast. It is left as `Number.parseInt` because removing it changes
nothing observable; the record says swapping it for `Number` would be behaviour-neutral, so a reader
may do it without fear rather than being told it is load-bearing.

**`int` was never the reason, and `D10-59` had already proved it.** The second claim — that the
server's `int` path parameter forces a whole-digit rule — is the identical inference `D10-59`
measured and found false: `GET /api/v1/contracts/01` answers **200**. So the same false reason that
was removed from `contract-scope.ts` survived here in the third place, carrying an extra error of its
own. This is `D10-58`'s shape again with a longer fuse: one fact in three places, two of them
attended to, the third reading to anyone who opens the file.

**What it settles for the open call, and what it does not.** Splitting the guards by server
behaviour: this page's `/^\d+$/` admits `"07"` and reads it as contract 7, which is what the server
does with `/contracts/01`, so **this is the guard that matches the API**. `contract-scope.ts` refuses
~~a padded id~~, which makes it the address bar's own narrower convention. ~~The divergence `D10-55`
recorded therefore stays open — it is still a product call about what an address means —~~ but
*which side deviates from the API* is no longer undecided, and it is not the side the earlier
reasoning implied. **That is what decided `D10-76`:** the side the measurement had already named is
the side the ruling kept.

**No behaviour change.** One doc comment replaced: **23 insertions, 3 deletions, every changed line
inside `/** ... */`**, no regex, branch, return value or query key touched, and no spec edited to
match the new wording. The frontend suite is **309 passed in 13 files**, the same count as before,
which is the expected result for a comment: nothing asserts the sentence that changed. No dependency,
no lockfile line, no backend source and no migration; `alembic` head stays `0006`.

**The tooling lesson, which cost two corrupted files.** The replacement text was originally written
with the shape test quoted between backticks, and `edit_file` applies a replacement through
`String.replace`, where a dollar sign immediately followed by a backtick means *"the portion of the
input before the match"*. The file's own header was therefore spliced into the middle of the comment
— **271 syntax errors**, and the tool's echoed region showed it plainly as a comment fused to the
first line of the module. Restored with `git checkout -- <path>`, re-read, retried, corrupted again
the same way; the third attempt used slash-delimited regexes, which cannot contain that sequence, and
landed. Both recoveries were a restore and a re-read, never a hand repair of the mangled text, and
the change was trusted only after `git diff --stat` showed nothing outside the comment. A replacement
string is pattern syntax, not literal text.

**Landed as** `docs(web)` `ef21e47` — one file, comment-only, **no behaviour change**.

### D10-61 — The fourth copy, and a list that fails its own membership test

`D10-60` corrected the sentence in the file it had open, and stopped there. The sentence was
`Number("12abc")` is `12`; it was still standing in two frontend files — `contract-scope.ts` and two
comments in the `contracts/[id]` spec — and in three ledger paragraphs, none of which had been
opened since. Searching for the sentence rather than for the file it was found in turns up six
sites, and one of them was already right:

| Site | Said | Measured |
| --- | --- | --- |
| `contract-scope.ts:45` | `Number("12abc")` is `12` | `NaN` |
| `[id]/page.test.tsx:12`, `:356` | `Number` reads `"12abc"` as 12 | `NaN`; `parseInt` gives 12 |
| `DECISIONS.md:3406` (`D10-34`) | `Number("12abc")` is `12` | `NaN` |
| `STATUS.md:446` | `Number("12abc")` is `12` | `NaN` |
| `STATUS.md:4753` | `Number("12abc")` is `12` | `NaN` |
| `contract-scope.test.ts:50` | lists only the rescued spellings | **correct already** |

**The one that was right is the tell.** The spec's own table has always listed only the inputs
`Number()` really rescues — 012, a space-padded 12, 1.5, 1e3, 0x10 and the empty string — and
quietly omitted `"12abc"`, because whoever wrote it was looking at measured output rather than at a
sentence three files away. So one site was correct, five asserted the reverse, and the suite stayed
green throughout: **nothing asserts a comment.** This is `D10-58`'s shape for the third time in this
chain, with the twist that the correction itself did not travel — fixing a claim where you found it
is not fixing the claim.

**The second defect is a criterion a list does not satisfy.** Two paragraphs introduced the
malformed set as *"chosen to be the ones `Number()` would rescue"*, and it is not: `"12abc"`,
`"abc"` and `"12,"` come back `NaN`, and `"1.5"` comes back a non-integer that no id can be.
Measured over the whole set of fourteen:

| Inputs | `Number(...)` |
| --- | --- |
| `"0"`, `"00"` | 0 |
| `"012"`, `"+12"`, `" 12"`, `"12 "` | 12 |
| `"1e3"` | 1000 |
| `"0x10"` | 16 |
| `"12abc"`, `"abc"`, `"12,"` | `NaN` |
| `"1.5"` | 1.5 |
| `"-1"` | -1 |
| `"9007199254740992"` | 2^53 |

So the set spans what the coercion performs *and* what it refuses, which is the stronger rule and
the one the module needs: a refusal that held only for the rescued spellings would be measuring
`Number` rather than the shape. The amendment strikes the criterion and states the wider one,
because deleting the words would lose the fact that anyone once believed them.

**No behaviour change.** Two files, comment-only: **13 insertions, 6 deletions**, every changed line
inside a comment or doc comment, no regex, branch, return value, query key, fixture or assertion
touched. The frontend suite is **309 passed in 13 files**, the same count as before, which is the
expected result for a comment. No dependency, no lockfile line, no backend source and no migration;
`alembic` head stays `0006`.

**What this does not decide.** ~~`D10-55` is untouched: the page's `/^\d+$/` still admits `"07"` and
`contract-scope.ts` still refuses it, which `D10-60` established is this page matching the API while
the address bar holds a narrower convention of its own. That remains a product call.~~
**`D10-76` came back to it**, and it went the way `D10-60` had already pointed: this page's guard
matches the API, so the address bar was moved to match it rather than the other way round.

**The tooling lesson, second in a row and the same one.** The replacement text here is applied by a
script that proves the wrapped paragraph holds the same words in the same order before it writes —
the check `apply_d59b.py` introduced — because a hand-wrapped paragraph is a silent edit waiting to
happen: earlier attempts in this chain produced a 139-column line and then a 214-column one. The
other new check is negative. Every criterion a sentence asserts about a list is now measured over
the whole list, because the sentence about the malformed set had been false in two files for as long
as it had existed.

**Landed as** `docs(web)` `2f78a0d` and this `docs(status)` commit — comment-only plus ledgers,
**no behaviour change**.

### D10-62 — Fifteen wire shapes read against what emits them

Every screen spec in this app doubles `@/lib/api`, so what they assert against is not the API: it
is a fixture written on the screen's side. The ledgers say so repeatedly, in both files, and each
restatement was true when it was written and then copied forward rather than re-measured — the
shape `D10-58` names, where one fact in many places is many places to fix and every copy but the
first goes stale unread.

So it was measured. Every interface in `frontend/src/lib/api.ts` that carries a route's response
body was read against the declaration its own docstring names — field names, field count, order —
and all nine wire enums member for member. No HTTP was involved: the claim was that two
declarations agree, so two declarations were read.

| Type | TS fields | Server declaration | Verdict |
| --- | --- | --- | --- |
| `UserSummary` | 6 | `auth.UserSummary` | same 6, same order |
| `Session` | 7 | `auth.SessionResponse` | same 7, same order |
| `ContractSummary` | 12 | `contracts.ContractSummary` | same 12, same order |
| `ContractListItem` | 18 | `contracts.ContractListItem` | same 18, same order |
| `ConsentState` | 9 | `contracts.ConsentView` | same 9, same order |
| `ContractField` | 9 | `contracts.ExtractedFieldSummary` | same 9, same order |
| `ReviewItem` | 17 | `review.ReviewItem` | 8 own + 9 inherited |
| `ReviewQueue` | 5 | `review.ReviewQueueResponse` | same 5, same order |
| `AgentDecision` | 14 | `agent.AgentDecisionView` | same 14, same order |
| `AgentDecisionList` | 3 | `agent.AgentDecisionListResponse` | same 3, same order |
| `Escalation` | 9 | `escalations.EscalationView` | same 9, same order |
| `EscalationQueue` | 5 | `escalations.EscalationListResponse` | same 5, same order |
| `NotificationRow` | 12 | `notifications.NotificationView` | same 12, same order |
| `DispatchOutcome` | 4 | `notifications.DispatchResponse` | same 4, same order |
| `ErrorEnvelope` | ~~4~~ 5 | `app.main.error_payload` | ~~4 of 5~~ same 5, same order |

~~Fourteen matched and one did not, and the one that did not is the envelope every screen reads on
every failure.~~ **Amended by `D10-72`, not deleted: fifteen match and none does not.** The row
that did not agree was the envelope every screen reads on every failure: `error_payload` always
emits four keys — `code`, `message`, `request_id`, `trace_id` — and adds `details` only when the
route supplied some, while the interface declared `code`, `message`, `request_id`, `details`, in
the same relative order, with `trace_id` absent from every failure path alike. `api.ts` now
declares it and surfaces it as `ApiError.traceId`, so the shape half of the standing limit in
`STATUS.md` is spent rather than narrowed.

The omission is harmless at run time, which is exactly why it survived. A client reading named
fields off a JSON object ignores a key it never asked for, so no screen could have noticed and no
test could have failed. It is still drift: `trace_id` is bound into the log context for the
request and echoed back in an `x-trace-id` header that CORS explicitly exposes
~~so the client is declining the one value that would tie a user's error report to the server's log
line. Whether to declare it is an owner decision — a field nothing reads is weight, but a
correlation id is what a support conversation needs — so it was recorded rather than changed.~~
**Struck by `D10-83`, not deleted: the choice was made twice and neither time by an owner.**
`D10-72` declared the field and surfaced it as `ApiError.traceId`; `D10-83` then gave it eleven
call sites on nine screens. So "the client is declining" describes what `D10-62` found rather than
what the tree does, and "a field nothing reads is weight" stopped being the state of things. The
correlation id did turn out to be what a support conversation needs; nothing was waiting on a
ruling for it. This slice touched no code.

`ReviewItem` is the row whose counts do not line up by copying, and the one that shows the reading
was done rather than assumed: the interface lists seventeen fields flat, while the server composes
eight of its own onto the nine it inherits from `ExtractedFieldSummary`. The two sets are disjoint,
so seventeen is right on both sides and the flat interface is a faithful flattening rather than a
second list free to drift. Which group is listed first differs between the two; a JSON object's key
order is not something either side reads, so the shapes agree.

The nine enums agree member for member, values included: `UserRole` (3), `ContractStatus` (6),
`ContractSource` (2), `RiskLevel` (3), `FieldName` (7), `FieldStatus` (5), `ContactChannel` (3),
`NotificationStatus` (7), `EscalationStatus` (4). `FieldName` is the sharpest of the nine, being
the closed set the extractor may produce: `email`, `phone`, `address`, `dob`, `start_date`,
`end_date`, `contract_value` on both sides.

What this does **not** establish, stated so the entry is not read as more than it is:

1. **Fields agree; which route fills them was not checked.** A model may declare a field that a
   route answers `None` for. Shape parity says nothing about population.
2. **The envelope's key set was compared, its vocabulary was not.** The `code` *strings* the
   screens translate — `CHANNEL_NOT_PERMITTED` and its `details.permitted`, the two `409`s, the
   `422` — were not matched against the routers that raise them.
3. **Nothing keeps this true.** No gate fails if a server field is renamed tomorrow. A Python
   test could pin the server side of this seam, but pinning a third copy of a field list is the
   hazard `D10-58` names, so it was not done here — an owner decision, not a tidy-up.

**Landed as** this `docs(status)` commit — ledgers only, **no code, no test, no behaviour change**.

### D10-63 — Eighteen refusal codes read against the buttons that can raise them

`D10-62` closed the shape half of the wire seam and named the strings half as its own
limit: the refusal-code vocabulary the screens translate was not enumerated against
the routers that raise it. This slice enumerated it — from the constant, to the
`raise`, to the button that produces it.

The vocabulary is 27 `REFUSAL_*` constants in five files: `decision.py` 6,
`dispatch.py` 10, `escalation.py` 5, the `notifications` route 5,
`notification_scheduler.py` 1. That is 26 distinct strings, because
`CONTRACT_CLOSED` is defined in two of them, and one of the 27 never becomes a
response at all: `TENANT_MISMATCH` is written to `Notification.last_error` by the
scheduler and goes no further.

Twenty-two button-reachable *sites* cover eighteen *distinct* codes: `CONTRACT_CLOSED`,
`CONTACT_REFUSED`, `NO_CONSENT` and `PIPELINE_BLOCKED` are each raised on two screens.
Every one of the eighteen is mapped at every site that can meet it:

- **agent** — 7: the six decision-policy refusals (`CONTRACT_CLOSED`,
  `PIPELINE_BLOCKED`, `CONTACT_REFUSED`, `NO_CONSENT`, `NO_CONTACT_VALUE`,
  `CHANNEL_NOT_PERMITTED`) plus `NO_DECISION` when there is no row to correct.
- **escalations** — 2: `ESCALATION_CLOSED` and `INVALID_TRANSITION`, the only two
  `PATCH /escalations/{id}` can raise. The raise path's three codes
  (`ALREADY_ESCALATED`, `RESPONSE_RECORDED`, `CONTRACT_CLOSED`) belong to a route
  this screen never calls.
- **notifications** — 13: the four consent gates, `PIPELINE_BLOCKED`, six dispatch
  verdicts (`NOT_PENDING`, `NOT_YET_DUE`, `UNKNOWN_TIMEZONE`, `ATTEMPTS_EXHAUSTED`,
  `SENDER_UNAVAILABLE`, `RECIPIENT_UNAVAILABLE`), and `NOT_FAILED` /
  `CONCURRENT_UPDATE` on retry.

The other six screens branch on status — 401, 403, 409, 422 — and never read
`code`, so they have no vocabulary to drift.

**One claim was false.** `notifications/page.tsx` grouped four codes under "these
four are the consent gates, reached through `assert_may_contact`". Three are
`assert_may_contact`'s; the fourth gate inside that function is
`CHANNEL_NOT_CONSENTED`, not `PIPELINE_BLOCKED`, which the worker raises itself one
gate below (`dispatch.py:591`) when a field is still awaiting approval. So the
comment named a mechanism that does not produce the code beside it, while the code
that mechanism does produce sat four lines down with no comment at all. Fixed by
naming the four real gates and giving `PIPELINE_BLOCKED` its own line, its own
mechanism, and the line number.

**Three of the sixteen mapped codes cannot be provoked from this screen.**
`NOT_IN_WINDOW`, `MISSING_END_DATE` and `NEEDS_REVIEW` come from
`build_notification` — the schedule path — and `api.ts` sends only `/dispatch` and
`/retry`. That is over-coverage, not drift: the sentences exist for an action that
does not. Now recorded in the file so the map is not read as proof that a button
can produce them.

What this slice does not establish. That the translated sentences say the right
thing: copy is not a code reading, and nothing here checks a locale string. That a
new `raise` cannot drift silently: no test pins the vocabulary, so a twenty-eighth
code would be exactly as invisible as this one was. And that a 409 carrying a bare
`detail` and no `code` is distinguishable from a version conflict — it is not; the
reveal route and the escalation `StaleDataError` both land on one conflict
sentence, on purpose.

**Landed as** a `docs(web)` comment fix plus a `docs(status)` record, on top of
`5e4b204`.

### D10-64 — Both catalogues pinned against each other, five relational cases

`i18n/messages.ts` already makes a missing translation key a compile error: `bn` is
assigned to `Record<Locale, Messages>` with no `as` cast, so a namespace or key added
to `en.json` and forgotten in `bn.json` fails `tsc`. That guard is real, and it stops
at the strings. A compiler cannot see inside a value, which leaves three defects free
to reach a Bengali screen while every English one looks perfect: an argument renamed
on one side (`{count}` to `{total}`), which renders the literal braces and drops the
number; a key present only in `bn.json`, which assignability allows because excess
properties are not checked on a non-fresh object; and an empty string, which renders
as nothing rather than as a gap.

`frontend/src/i18n/messages.test.ts` closes those three. Every assertion in it is
relational — no key name, count or sentence is written down — because a test that
listed the keys would be a third copy of the catalogue, and the copy is what drifts.
It flattens both files, compares the key sets in both directions, compares each
message's ICU arguments by name and kind, compares plural categories where a message
branches, and asserts no value is blank. A floor of 300 leaves keeps it honest: a
broken flattener fails instead of passing over an empty map.

Measured: 464 leaf keys on each side, 11 namespaces, zero one-sided, zero empty, zero
argument drift. Forty-three messages carry braces and all 43 are plain `{name}`
interpolation — there is no ICU plural or select anywhere in either file, so the
category comparison cannot fire today. The fifth case therefore runs the reader over a
synthetic plural and over two real strings, so that tripwire is proven rather than
merely intended.

Mutation-proven, seven mutations to `bn.json` applied one at a time and restored: two
deletions (top level and nested), a key only `bn.json` has, a renamed argument, a plain
argument turned plural, and a whitespace-only value. All seven turned the suite red with
exactly one of five cases failing — the count is the evidence, since an import or JSON
error fails all five — and the restored file returned five passing. Frontend suite 314
passing / 14 files, up from 309/13.

What this slice does not establish. The 25 template call sites whose suffix comes from a
local map or a server enum (`contracts.${STATUS_KEYS[row.status]}`) are not resolved: two
attempts to model them read `timeZone: "UTC"` and `style: "medium"` as message keys, so
their output was discarded rather than repaired. Copy is not checked at all — a Bengali
sentence that is fluent and wrong passes every case here. And the floor is 300, not 464,
deliberately: so that adding or removing keys never forces an edit to a number the
catalogue, not the test, owns.

**Landed as** a `test(web)` spec plus a `docs(status)` record, on top of `2e064b4`.


### D10-65 — The composed keys read where the keys are written, not where they are used

`D10-64` closed the catalogue seam and named one hole in its own record: 25 call sites
compose a key from a map value — `contracts.${STATUS_KEYS[row.status]}` wrapped in
`t(...)` — and two attempts to resolve them by parsing call expressions both read
`timeZone: "UTC"` and `style: "medium"` as message keys. Neither was repaired; both were
discarded.

The fix was to stop reading the call and read the table. Every such map is declared
`as const satisfies Record<SomeEnum, string>`, which makes its values enumerable and
makes the `satisfies` clause prove exhaustiveness — a new enum member with no entry is a
type error in the screen, not a silent gap here. So the spec collects the maps declared
in each `page.tsx`, collects the ``t(`<namespace>.${<TABLE>[…]}`)`` compositions in the
same file, and asks each composed key to exist in both catalogues. It pairs a site with
a table **from its own file** and fails when one cannot be found, so the guard cannot
quietly shrink to nothing.

Measured by the shipped spec: 10 screens, 25 sites, 96 keys, every one present in
`en.json` and `bn.json`. Both directions are checked, so a key in one catalogue only is
a failure rather than a tolerated extra.

Mutation-proven on the spec itself, not only on the throwaway resolver that preceded it:
deleting `contracts.statuses.renewed` from `en.json` turned **exactly one of two cases**
red, and the failure named all three findings — two sites on the contracts list, one on
contract detail. Restored, both passed. Frontend suite 316 passing / 15 files, up from
314/14.

Three claims of my own were wrong and are corrected in the file: the floor comment said
"27 sites"; the run says 25. The same comment block called the raw-wire sites "four
prefixes" where there are five, and warned that "a seventh" `EscalationStatus` would go
unnoticed — that enum has four members, so the next one is the fifth. Both are now
measured rather than recalled.

What this slice does not establish. Five sites still compose a key from a raw wire value
with no table in between — `review.fields.${item.field_name}`, `review.statuses.…`,
`review.contractStatuses.…`, `escalations.statuses.…` and
`escalations.contractStatuses.…` — so there is no literal for the spec to read, and a
fifth `EscalationStatus` on the server would be caught by nothing in this suite. They
were resolved by hand once, through the resolver: five prefixes, 28 wire values, every
prefix fully present. That measurement is a snapshot, not a guard. And copy remains
unchecked, exactly as `D10-64` said: a fluent Bengali sentence that says the wrong thing
passes this spec and its neighbour.

**Landed as** a `test(web)` spec plus a `docs(status)` record, on top of `47a5e2b`.

### D10-66 — The report the guard could never fill in

`D10-65` landed a spec with one defect in it, found by reading the file rather than
by a run: the branch that reports a composition whose table is not in its own file
printed `no table undefined` — the one word a reader needs, gone.

The cause is a shorthand, not a typo. The sites were paired with their tables as

    { ...site, path: file.path, table: file.tables.get(site.table) }

The spread copies the site's table *name*, and the property written immediately after
it overwrites that name with the lookup's result. The report is built from
`paired.filter((site) => site.table === undefined)`, so the field the message reads is
the one field guaranteed to be `undefined` in every row it is ever asked about. A
diagnostic that runs only once something else has broken is the code path nothing
exercises, and it was written in the shape that reads best.

`paired` now carries the name in a field of its own and the report reads that field.
The comment above it says why the two are kept apart, because spreading the site is
the thing that will be written again.

Proven by breaking a real call site rather than by reasoning about the string: the
first ``t(`contracts.${STATUS_KEYS[…]}…)`` in
`frontend/src/app/[locale]/contracts/page.tsx` was renamed to `STATUS_KEYZ`, the spec
went red on exactly one of two cases, and the failure read

    app\[locale]\contracts\page.tsx: no table STATUS_KEYZ for t(`contracts.…`)

The screen was then restored from the bytes read before the mutation, and the restore
was verified by sha256 against that digest — so the line above is a mutated run, not a
file left edited. Frontend suite 316 passing / 15 files, unchanged, as a repair to a
failure message must be.

Also removed: `contract-agent`, a 1733-byte file at the repo root holding `git log`
output that an earlier redirect of mine wrote there instead of to the console. It was
untracked and is in no commit.

What this slice does not establish. The repaired report is proven by that one
mutation and by nothing standing: no case in the suite drives the `unresolved` branch,
so a later edit could put the spread back and every gate would stay green. The five
raw-wire composition sites remain unread, exactly as `D10-65` recorded, and copy is
still unchecked in both catalogues.

**Landed as** a `fix(test)` commit plus this `docs(status)` record, on top of
`19ba652`.

### D10-67 — The sentence that was never asked what it was made of

`D10-64` closed by naming a limit and leaving it: the two catalogues can agree with each
other and both still be wrong, because every case in `messages.test.ts` compares them *to
each other* — the same keys, the same ICU arguments, nothing empty — and none of them asks
what a value is made of. So the commonest way an i18n catalogue rots went through the whole
suite untouched: a key is added to both files in one commit, and the `bn.json` half is the
English sentence. It typechecks, it carries the right arguments, it is not empty, and a
Bengali screen renders fluent English.

Three measurements came before the rule, because a rule written on an assumption is the
thing this record keeps having to repair:

    leaf keys                     464
    bn values differing from en   462
    bn values identical             2   "{id}" and "API", both legitimately so
    messages that are prose       452   English half holds a run of 3+ lowercase letters
    prose messages untranslated     0
    Bengali script inside en.json   0

With no exceptions to accommodate, the rule needs none: a prose message must carry at least
one Bengali code point in `bn.json`, and no value in `en.json` may carry Bengali script at
all. The three-letter lowercase run is the whole of the discrimination — `"API"`, `"{id}"`
and `"N/A"` contain none and are skipped, which is what stops the rule flagging the values
that are correctly identical in both locales.

Proven by mutation against the real catalogues, with each restore verified by sha256:

    English sentence into bn.json    exit 1   1 failed | 6 passed
    Bengali sentence into en.json    exit 1   1 failed | 6 passed

Exactly one of seven cases red each time, and the case that failed is the one that names that
direction — the second reports `[ 'app.name' ]`. That pairing is the evidence: a mutation
that reddened both cases would prove only that either one would have caught it. Frontend
suite 318 passing / 15 files, up from 316.

One error of mine, caught by the gate rather than by me: the commit subject read "notice a
Bengali message that is still English", and `commitlint` refused it on `subject-case` — a
capital `B` in the middle of a subject. Reworded to drop the proper noun rather than
lowercasing it into a typo.

What this does not establish is the larger half. Presence is not correctness: a Bengali value
that says the wrong thing passes both cases, and so does a fluent sentence with its subject
and object swapped. This notices a translation that is *missing*, not one that is *wrong*. It
also supersedes the closing clause of `D10-64`'s and `D10-65`'s amendments, which said copy
was not checked at all — the honest replacement is that a missing translation is now checked
and a wrong one is not. And `D10-66`'s standing gap stands: the repaired failure report is
still proven by one mutation rather than by a case, and the five raw-wire composition sites
remain unread by anything.

**Landed as** a `test(web)` commit plus this `docs(status)` record, on top of `133f88a`.

### D10-68 — The CI verdict was readable the whole time; only its log text was not

`D10-51` fixed the `gitleaks` step by reproducing its refusal locally, and recorded two limits: that
`gitleaks` had still never scanned anything, and that the step logs are unreadable from here,
because GitHub answers `403` for job logs to an unauthenticated client and `gh` is not installed.
Both were true when written. Both have now been re-measured. One closed. The other narrowed — and
the narrowing is the larger finding, because the over-broad form of it has been repeated as a
blocked item in every handover since, and it was never blocked.

**What the API does answer without a token.** `GET /repos/touhid9xx/contract-agent/actions/runs` and
`/actions/runs/{id}/jobs` return `200`. The repository is public; the `403` is specific to the *log*
endpoint, not to the run. So the conclusion, the status, every job's name and conclusion, and every
step's name, conclusion and start/finish timestamps are readable from a shell with no credentials.

    runs in the repository              19   18 success, 1 failure
    the one failure                     run #1, `6c02f66` — the `gitleaks` refusal `D10-51` fixed
    HEAD `5130924`, run 42811394561     conclusion success, 7 jobs, 7 success

    frontend (typecheck, eslint, vitest, build)          success
    backend tests (pytest, sqlite)                       success
    demo frontend (typecheck)                            success
    backend integration tests (mysql 8.4)                success
    python static (ruff, mypy strict, bandit)            success
    hygiene + secret scan                                success
    CI gate — "Every required job must have succeeded"  success

**`D10-51`'s first limit closes.** Its own criterion was that run #2 would be `gitleaks`' first real
execution, and that a red run #2 would be a finding rather than a failed fix. Run #2 was green, and
so is every run after it: eighteen in all, the whole history except run #1. On the head run the
`gitleaks` step is listed as `success`, running `11:55:40Z` to `11:55:46Z` — six seconds, which is
not the same-second signature `D10-51` used to prove run #1's red step never executed at all.
`pre-commit` exits 1 when its stage filter finds no matching hook, which is precisely what run #1's
step died of, so a step that succeeds means the hook was found and ran; and `gitleaks` exits
non-zero when it finds anything. The evidence is that exit code and that duration, not the log line:
the one artefact still out of reach is the text that prints the hook's own verdict.

**`D10-51`'s second limit narrows, and this part is my error rather than the file's.** `GET
/actions/jobs/{id}/logs` answers `403` unauthenticated — re-measured today, so the claim stands as
written. What is wrong is where I took it. Every turn of this streak has ended with "CI unreadable
here (no `gh`; `403`)" as an item blocked on the owner, and the item was never blocked: I retried
the one endpoint I had already failed on, and reported its refusal as a property of CI. It is
`D10-59`'s shape one layer out — a tool's failure text is not evidence about the thing you asked —
and `D10-63`'s as well, where the fault was a query that could not have returned the answer it was
trusted for. A `403` on one path is not a `403` on the API.

**What this does not establish.** The `CI gate` job succeeding proves the gate *evaluated* seven
jobs and passed. It still has never been *observed gating* a merge, because required status checks
on `main` are a repository setting that no unauthenticated call can read. Green runs are evidence
about what the runner did with these trees; they are not evidence about branch protection, and not a
claim that the workflow catches what the suites miss.

**Local suites, measured this turn rather than recalled.** `python -m pytest -m "not integration"`
exits 0: 869 tests, 867 passed, 2 skipped, 0 failed — counted twice, from the per-file collection
totals the reporter prints (which sum to 869) and from the progress line's 867 marks plus its two
skips. Frontend `npm test`: 318 passed, 15 files, unchanged from `D10-67`.

Landed as `docs(status)` — two Markdown files. No code, no test, no dependency, no migration.

### D10-69 — One phrase, three measurements, and none of them survives a push

Eleven sentences in `STATUS.md` close a slice with the same figure: the branch stands **N commits
ahead of `origin/main`**, *re-counted rather than carried forward*. The phrase was inherited from
the first of them, the number was re-counted by hand each time, and the drift was admitted in prose
— the `113` written for `D10-46` had "already drifted" by the next slice. That admission is what
made this worth measuring instead of trusting: a figure known to drift, pinned eleven times by
hand, is a figure nobody can check.

**What the early numbers count.** Measured against history rather than read off the sentence:

    claim   commit that carries it      `git rev-list --count`
    113     8d54176  `docs(status)`     113
    117     7198c42  `docs(status)`     117
    121     cc3cba9  `docs(status)`     121
    123     5f05e37  `docs(status)`     123
    124     dd41c04  `test(web)`        124
    125     6c02f66  `docs(status)`     125

Every one equals the *history length* of the commit it sits in, to the commit. That is not
divergence from anything: history length can equal commits-ahead only if the remote holds no
commits at all. So the early figures counted **the branch**, not the distance from `origin/main`,
and the word "ahead" was decoration around a `git rev-list --count HEAD`.

**The fifth row breaks the label rather than the arithmetic.** `STATUS.md` says 124 was counted
"at this `docs(status)` commit", but the commit whose history is 124 is `dd41c04`, a `test(web)`
commit — the third screen test. Its `docs(status)` companion is 125, the figure quoted by the
paragraph *below* it. Either the number was taken one commit early, or the label was copied from
the sentence it sits beside; both cannot hold.

**Then the phrase quietly changed meaning.** The fourth-screen paragraph claims 125 "after this
`docs(status)` commit", but its own commit carries 130, and CI run #1 had already run on `6c02f66`
— commit 125. So at that moment the remote tip *was* 125, and the branch was five ahead
(126–130). 125 is the **remote's** count, not the branch's divergence. After the first push the
same sentence reports 2, 2, 4, 2, 2, 2, and none of those can be anything but divergence: a history
length of 2 does not exist here and a remote tip of 2 does not either. Three quantities wear one
phrase — history length, remote tip, divergence.

**Why that is a defect and not a footnote.** It is `D10-58`'s shape, one fact written in many
places, with the aggravating difference that the places disagree about *which* fact it is; and it
is `D10-62`'s shape, a figure nobody re-measured, except that here somebody did re-measure it, by
hand, eleven times, and the drift arrived anyway. At HEAD the sentence is false under all three
readings at once: `rev-list --left-right --count HEAD...origin/main` prints `0 0`, because a branch
that has just been pushed is never ahead of itself. A divergence written into the document that the
push itself carries has no state in which it can be read.

**What was not done.** The eleven figures are not rewritten. Each was true of the measurement it
actually took, and replacing them with `0` would be inventing a different past to make a sentence
look tidy. What is corrected is the *practice*, in the accompanying amendment: a slice reports
divergence as the pair `rev-list --left-right --count` prints, reports history length under its own
name, and never lets one number stand in for the other.

**Verification.** Counted, not recalled: `STATUS.md` holds eleven `commits ahead of` sentences, the
five pre-push figures were matched to commits with `git rev-list --count`, and the divergence at
HEAD is `0 0`. The first attempt to count them returned zero matches and was wrong — a literal
pattern containing `|` instead of a regex, which is `D10-63`'s lesson in the same costume a third
time. No application code, no test, no dependency, no migration.

Landed as `docs(status)` — two Markdown files, no code.

### D10-70 — A missing-features list is a claim about the tree, and nothing re-measured it

`README.md` opens with a blockquote that names what this project lacks, and every handover
in the last stretch read it as the description of the tree. It carried three claims: no
Kafka broker and no consumer, **no CI**, and **7 of the 9 web screens have no test**. Two
of the three had been false for a long time — not approximately false, or false in tone,
but false against `ls`.

**The CI claim.** `.github/workflows/ci.yml` defines seven jobs — `python-static`,
`backend-tests`, `mysql-integration`, `frontend`, `demo-frontend`, `hygiene` and a `gate`
job that needs all six and runs `if: always()`. The run list answers without a token: 21
runs in this repository, 20 green, the one red being `6c02f66` — the `gitleaks` stage
refusal `D10-51` itself fixed. Run `42817738711`, on `eb21e8e`, is seven jobs and seven
successes. So "no CI — gating runs from `pre-commit` on a developer's machine" described
a repository that stopped existing at `D10-46`, which is the commit that added the file.

**The screen claim, with its interval measured rather than described.** The sentence was
written at `e165ae8`. At that commit `git ls-tree -r --name-only e165ae8 -- frontend/src/app`
lists exactly two `page.test.tsx` files, so 9 − 2 = 7 was **true when it was written**. The
next screen test, `dd41c04`, landed the third, and from that commit the sentence was false.
`git rev-list --count dd41c04..HEAD` prints 37: the claim was correct for the single commit
that made it and wrong for the 37 that followed.

**What was not touched, and why that matters here.** The Kafka clause is true — the compose
file defines no broker, only a comment explaining its absence, and no consumer symbol exists
in `backend/` — so it stays, unstruck. The neighbouring claim that migrations sit at head
`0006` is also true: `0006_refresh_tokens.py` is the highest revision. A correction that
sweeps up true clauses because they sit near false ones is how the previous correction
becomes the next defect.

**One copy.** `D10-61`'s rule is to grep every copy of a sentence before editing one. Both
false clauses exist in exactly one place — `README.md:16` and `README.md:17` — across
`README.md`, `STATUS.md`, `DECISIONS.md` and `DEMO.md`. `STATUS.md`'s own CI table already
said seven jobs and was never wrong; the two files simply disagreed, and the file people
read first was the wrong one.

**The guard, because the correction alone would rot again.** A count in prose has no second
copy to disagree with (`D10-58`), and re-counting by hand is exactly what drifted eleven
times over in `D10-69`. So the corrected claim's machine-checkable half is now a test
rather than a promise: `frontend/src/app/screens.test.ts` requires a `page.test.tsx` beside
every `page.tsx` under `src/app`, exempts one path with its reason stated, and checks that
exemption in both directions — a stale exemption fails as loudly as a missing spec, and an
orphan spec fails too. It asserts the relation, never the count, so adding a tenth screen
cannot make it wrong; it can only make it fail until that screen is tested.

**Verification.** Fresh this turn rather than carried: 21 runs / 20 success from the
actions API, seven of seven jobs on `eb21e8e`, ten `page.tsx` of which nine have a spec,
`0006` confirmed as head, no Kafka service in `docker-compose.yml`, no consumer in
`backend/`. The new spec was mutation-proven in both directions — renaming
`[locale]/users/page.test.tsx` away failed it naming `[locale]/users/page.tsx`, and
creating `[locale]/probe/page.tsx` failed it naming that path — with the tree restored and
`git status` clean between them. Suite after: 322 passed in 16 files.

Landed as `test(web)` + `docs(status)`.

### D10-71 — A key name on screen is not a sentence, and three tables of keys went unchecked

Three screens turn a server refusal into something an operator can act on: the agent
decision screen, the notification queue and the escalation queue. Each maps a `409`'s
machine-readable `code` to a message key — `CONTRACT_CLOSED` → `agent.refuseContractClosed`
— and between them the three tables held 25 keys. Nothing in this repository checked that
any of those keys still existed. Renaming `notifications.refusalContractClosed` in **both**
catalogues was allowed by every check the repo had, and the result would have been the
literal string `notifications.refusalContractClosed` printed where a sentence belongs.

**Three blind spots, each read in source rather than asserted.** `Record<string, string>`
erases the literal type, so the compiler saw `string` and had nothing to compare it with.
Every call site is `useTranslations()` with no namespace (`agent/page.tsx:102`,
`notifications/page.tsx:131`, `escalations/page.tsx:91`), and `t()` accepts any key at all.
And `composed-keys.test.ts` could not see them either: its parser looks for a table indexed
*inside* the call — `t(\`ns.${TABLE[code]}\`)` — while these screens read the table into a
local first (`const key = REFUSAL_KEYS[code]`) and handed the local to `t(\`ns.${key}\`)` a
line later.

**What that costs the standing figures.** `composed-keys.test.ts` resolves 96 keys and the
catalogues hold 464 leaves; these 25 sat in neither set, so both i18n specs could pass while
a refusal went out untranslated. That is `D10-58` again — one fact, and the check attached
to a different copy of it.

**The fix is a move, not a new abstraction.** The three tables left the screens for
`frontend/src/lib/refusals.ts`, annotated `as const satisfies Record<string, string>` — the
shape the status maps already use — for one reason: a node-environment spec can import a
plain table, whereas reaching a `const` inside a `page.tsx` means parsing screen source,
which is the seam that has broken twice here. The lookup helper returns `undefined` rather
than a fallback sentence, because each screen walks its own `forbidden`/`offline`/`failed`
ladder first; and it checks `typeof found === "string"`, because these are object literals,
so `table["constructor"]` reaches `Object.prototype` and a refusal code is attacker-influenced
data on the wire.

**A guard that asserts relations, not key names.** `frontend/src/lib/refusals.test.ts` checks
five things and writes down no sentence and no count: every named key resolves in the English
catalogue *and* in the Bengali one, written as two assertions because a key present in one and
absent from the other is a translation bug rather than the same bug twice; no table repeats a
sentence, since these codes are translated separately precisely because they name different
next actions; every table stays inside its own screen's namespace, derived from the table's own
name; and the other direction — a catalogue key named like a refusal that no table reaches
fails as an orphan. The two floors (`KEY_FLOOR = 24`, `flatEn.size >= 200`) are vacuity guards,
not counts.

**Mutation-proven, four runs, each landing on a different assertion.** Renaming
`notifications.refusalContractClosed` in both catalogues failed *names only sentences that
exist in both catalogues* and also the orphan check. Bengali alone failed at line 83, English
alone at line 82 — the two directions are separate assertions, which is why they are written
apart. Pointing an agent code at `notifications.refusalNoConsent` failed *keeps every table
inside its own screen's namespace*. Tree restored and re-run green after each.

**~~One thing deliberately left unpinned.~~ Pinned on one side and loose on the other, by
`D10-77`.** The *codes* are the server's vocabulary — `REFUSAL_*` constants in
`app/workers/dispatch.py`, `decision.py`, `escalation.py` and
`app/api/v1/notifications.py`. ~~Restating them as a TypeScript union would be a fourth copy
of a fact with nothing to verify it against Python (`D10-63`), so it is not done: a code the
server stops sending leaves an unused entry, a code it starts sending falls through to the
screen's generic sentence, and both are survivable. The sentences are pinned; the codes are
not.~~ The union was refused for the right reason and the wrong one: the copies were never
required, because the assignments can be read out of the server tree the way
`composed-keys.test.ts` already reads `app/db/enums.py`, so a code the server stops sending now
fails a test instead of lingering as an entry nobody can reach. A code the server *starts*
sending still falls through to the screen's generic sentence — that half stays a named trade
(`D10-77`) rather than an unexamined limit.

**Reuse rather than a third copy.** `flatten`, byte-identical in `messages.test.ts` and
`composed-keys.test.ts`, moved to `src/i18n/catalogue.ts`. Its docstring claims each spec
keeps a floor so a broken `flatten` cannot pass three times at once; that claim was read and
checked rather than left as prose — `flatEn.size > 300` and `checked >= 90` are both there.

**Verification.** Suite 327 passed in 17 files, from 322 in 16, with all three screen specs
green before the tables moved and after, so the refactor preserved behaviour. Both catalogues
came out of the mutation runs unchanged (`git diff` empty on each).

Landed as `test(web)` + `docs(status)`.

### D10-72 — Fifteen of fifteen was one word, and the word was the claim

`D10-62` read all fifteen response declarations behind `@/lib/api` against the routes that emit
them and found fourteen agree, with `ErrorEnvelope` declaring four of the five keys
`app.main.error_payload` has always written. It recorded the fifth — `trace_id` — and said one
word would end the disagreement. That word went unwritten until now, which is the lesson rather
than the anecdote: a ledger entry that names its own fix is still only a claim until something
can fail on it.

**The key is not a copy of `request_id`.** The obvious reading — two correlation ids, one
redundant — is wrong, and the code says so: `request_id` is the value echoed back in
`x-request-id`, while `trace_id` is what `app.core.middleware` binds into every log line the
request emits. An operator holding one and searching a log wants the other. The declared type is
`string | null`, and `null` is a real case rather than a placeholder: `error_payload` composes
the envelope from the request contextvars, which are empty outside a request, so a CLI command
or a worker tick produces a failure with nothing bound at all.

**Appended, not inserted.** `ApiError`'s constructor gained `traceId` as its sixth parameter,
because `details` already held position five at roughly sixty hand-built call sites. Inserting
beside `requestId` — the readable place — would have re-bound all of them to the wrong argument
with no type error to catch it, both being nullable. The readable place and the safe place were
not the same place, and the safe one won; the constructor says why in a comment.

**Three assertions, one mutation.** The spec pins each path separately: `traceId: "trace-9"` when
the envelope carries one, `null` when it carries `trace_id: null`, and `null` on the fallback
where the body is not our envelope at all. The middle one matters because the type admits `null`;
the fallback one because a spec that only ever sees the populated path cannot tell "parsed" from
"always null". Rewriting `traceId = body.error.trace_id ?? null` to `traceId = null` turned
exactly the first assertion red and left the other twenty-one green — one mutation, one failure,
and it is the test that names the field.

**A defect found in the doing, and it was mine.** The mutation script's `Path.write_text` writes
in text mode, so on Windows it converted the whole of `api.ts` to CRLF while mutating *and*
restoring it. The `mixed-line-ending` hook that rejected the previous slice's commit would have
caught it, but the fix belongs in the script, which now passes `newline="\n"` on every read and
write. A harness that silently rewrites the file under test is a second change to the code, not
only to a copy of it.

**Verification.** Suite 328 passed in 17 files, from 327, and the worktree came out of the
mutation run at LF with the index unchanged.

Landed as `test(web)` + `docs(status)`.

### D10-73 — The code passed nineteen hooks and the commit did not land

The slice above was finished, measured and green, every hook printed `Passed`, and the commit was
refused. `.husky/pre-commit` runs `pre-commit run --all-files`, which in this repository is
nineteen hook ids in the `pre-commit` stage — eleven hygiene, two `ruff`, `mypy`, `bandit` and
four node checks, counted from `.pre-commit-config.yaml` for this entry rather than carried
forward, with `gitleaks` correctly absent because it is `stages: [manual]`. All nineteen passed.
Then:

    ✖ subject must be sentence-case, lower-case, start-case [subject-case]
    husky - commit-msg script failed (code 1)

**A twentieth hook ran, and it read the one input the other nineteen cannot see.** The commit
message does not exist while `pre-commit` runs. `.husky/commit-msg` is handed the path to it
afterwards and passes it straight to `commitlint --edit`, so *the hooks passed* is a sentence
about the tree, and the commit is a later and separate decision about a string. Both were
reported together, which is why they read as one gate. They are not one gate.

**Why the subject is a syntax error rather than a style nit.** `commitlint.config.mjs:7` sets
`subject-case` to `["sentence-case", "lower-case", "start-case"]`. commitlint reads the subject as
words and asks which shape each one has: the header ended `… on ApiError`, and `ApiError` is
camelCase — not all-lowercase, which is what `lower-case` accepts; not capitalised with the rest
lowercase, which is what `sentence-case` accepts; not Capitalised, which is what `start-case`
accepts. The identifier satisfied none of the three, and the header was rejected whole. The fix
was the one the gate named rather than a suppression: the class name moved into the body, where it
already appeared eleven times, and the header became `carry the envelope's trace id onto the api
error`. A name is welcome in the body and refused in the subject, because only one of the two
becomes history.

**The cheapest gate was the one run last.** Nineteen hooks cost minutes. `commitlint --edit
<path>` is the exact command the hook runs and costs a fraction of a second. Running it before
`git add` inverts the order of discovery, and this is the repository's only gate that can be
applied to its own input before that input exists anywhere else — a message can be linted as a
file, written by hand, with nothing staged at all. It was left until after everything else had
already said yes.

**Nothing was created, so there was nothing to unwind.** `HEAD` was still `de56299` and only
`api.ts` and `api.test.ts` were staged, so the retry was a clean first commit rather than an
`--amend` over something already in history. The finisher threw on the non-zero exit, so the docs
commit, the push and the divergence check never ran: a gate that fires before a commit exists
cannot leave a half-pushed tree. That was read off `HEAD` and the index after the failure rather
than assumed.

**A second defect in the same attempt, and it was mine too.** The finisher's pre-push guard
tested `-match 'api\.test?\.ts|DECISIONS\.md|STATUS\.md'`, where `?` makes only the final `t`
optional: the pattern matches `api.test.ts` and does **not** match `api.ts`, so the guard would
have pushed with the source file still unstaged while reporting four clean paths. It was found by
reading the script, not by a run that failed. Same family as `D10-65`, one step further: a guard
that has never failed has not been tested, and a guard that has never been read has not been
written either. It now asks git about the four paths by name.

**Verification.** `6c9d6ee` (`test(web)`) and `cf6c95a` (`docs(status)`) both landed;
`git rev-list --left-right --count HEAD...origin/main` printed `0	0`; `git status --porcelain
-uall` printed nothing; `git ls-files -- .agent-cache` printed nothing, so this slice's nine
scratch files are gone and not merely ignored. Suite 328 passed in 17 files, re-measured for this
entry through `node scripts/run-frontend-check.mjs test` — the same entry point the hook calls.

Landed as `docs(status)`.

### D10-74 — Eighteen of nineteen, and the nineteenth could never have failed

The two guards owed above are landed: the five table-less compositions `D10-65` measured by hand
are read from `backend/app/db/enums.py` and the `Wire values of app.db.enums.…` docstrings in
`lib/api.ts` and resolved under their prefixes in both catalogues, and `D10-66`'s failure branch is
driven through the same `pair()` the rest of the suite uses. Four mutations, each restored and
verified by sha256 against a digest taken first, and a fifth enum on the server now fails in three
places. The code commit's message holds that table; what this entry records is what the run
corrected in the record itself.

**The number was counted from a config, never from a run.** `D10-73` says
`pre-commit run --all-files` "in this repository is nineteen hook ids … All nineteen passed."
Nineteen is right as a count of ids in the `pre-commit` stage. *All nineteen passed* is not a
sentence this repository can print. The run that landed `419176f` printed eighteen `Passed` lines
and one line that is neither:

    forbid submodules....................................(no files to check)Skipped

**The skip belongs to the tree, not to the change.** husky invokes `pre-commit run --all-files`, so
the candidate file list is the whole repository and every hook's `files:` filter selects from all of
it — nothing about which files this commit touched narrowed anything. `forbid-submodules` is
declared upstream as `types: [directory]`, which in pre-commit's vocabulary means gitlinks, and this
repository has none, so the hook has nothing to inspect on any commit it will ever see. That was
read out of the hook manifest in the pre-commit cache rather than remembered from the failure
message, because the failure message does not say it.

**A skipped hook is not a passed hook, and an id is not a check.** Nineteen ids, eighteen checks,
one of which cannot fail here. The gate is not weaker for it — a repository with no submodules has
nothing for a submodule check to find — but the sentence was weaker for saying it, and the F1
tooling table and the amendment below had both already said it. A green line is a claim about a
check that ran; `Skipped` is a claim about a check that declined to, and the two are counted
together only by someone who is not counting.

**The first amend carried a second wrong reason, and that one was mine.** The message was first
corrected to *"eighteen hooks passed; `forbid submodules` skipped, having no files to check in a
frontend-only change."* The count was right and the cause was invented: `(no files to check)` names
the selection, and I supplied the reason it did not give, then made it specific enough to be false —
a backend-only commit would have skipped the same hook. Reading the upstream definition took one
command. The second amend replaced the clause, and `commitlint --edit` ran on the message file with
nothing staged before git was touched at all: `D10-73`'s cheapest gate, this time used first instead
of last.

**Verification.** `419176f`, one file, +227 −35, amended twice before it was ever pushed — the hooks
re-ran on both, so the tree was re-checked and not merely re-worded. Suite 332 passed in 17 files,
measured twice for this entry: once inside the hook's `frontend tests (vitest run)`, once directly
through `npx vitest run`. `git status --porcelain -uall` printed nothing between the amends.

**Limits.** The four-mutation table was measured in the run that preceded this entry and was not
re-performed for it; the suite figure above was. ~~No CI run was observed for `419176f` — the push
is this entry's next commit's problem, and the `frontend` job's checkout of the whole repository
is the assumption the new spec rests on, stated in the code commit rather than proven by one.~~
`D10-75` supersedes that sentence with a run: `42889593155` is the push's own run, seven jobs,
every one `success`, with `frontend`'s `vitest` and `production build` among them — so the
cross-tree read is proven by a run and not merely asserted by a message. And a skipped hook cannot
be caught by any test in this repository, only by reading the run instead of counting its config.

Landed as `test(web)` (`419176f`) + `docs(status)`.

### D10-75 — The gate job is advisory, because `main` has no protection

`ci.yml` ends with a `gate` job whose comment opened *"One required status check for branch
protection"*, and the file's header said CI is here "to make the existing gate mandatory". Both
describe an enforcement. Neither is a fact about this repository: the aggregator is shaped for a
required check, and no required check exists.

**Measured, not inferred.** `GET /repos/touhid9xx/contract-agent/branches/main` answers
anonymously and reports `"protected": false` with

    "required_status_checks": {"enforcement_level": "off", "contexts": [], "checks": []}

so a direct push to `main` lands whether the run is green or red. The `/protection` sub-resource
answers 401 to an anonymous caller, which is not evidence either way; the boolean on the branch
itself is, and it is unambiguous.

**The evidence was already in this slice's own history.** `9bbea40`, `419176f` and `7b13734` were
each pushed straight to `main` with no pull request and no merge, and nothing refused any of them.
A required check would have stopped the first push in its tracks — a tree whose CI has not yet run
is exactly what it refuses. `D10-46` built the checks; requiring them is a repository setting, and
no line in a YAML file can perform it.

**What changed, and what could not.** The `gate` comment now reads *"one job to name as a required
status check"* and states the measurement instead of implying the wiring; the header sentence
gains the same qualifier. No job, trigger, step, permission or `needs` entry changed. A comment
that described enforcement which does not exist is `D10-59` again — a comment is a claim, and this
one was checkable in a single unauthenticated call that nobody had made.

**Verification.** `check-yaml` passes on the edited workflow, which is the hook CI itself runs,
and every other gate is untouched by a comment. The measurement is one command, reproducible
without a token: the same call printed `protected: False` and `enforcement_level: "off"`. The run
that proves the pipeline green is `42889593155` — seven jobs, all `success`.

**Limits.** Turning protection on needs an admin token this session does not have, so the fix here
is prose plus a record, not a change of behaviour. Read strictly, `enforcement_level: "off"` is
also what a protected-but-unenforced configuration reports, so the safe phrasing is "no
enforcement is observable anonymously" rather than "no rule exists in the settings page". The
consequence is identical either way: nothing has ever refused a push to `main`, and seven green
runs have gated nothing.

Landed as `ci(workflow)` + `docs(status)`.

### D10-76 — The padded id was one contract all along, so the address bar now says so

`D10-55` left exactly one ruling for an owner: `contracts/[id]/page.tsx` accepted
`/^\d+$/` and read `"07"` as contract 7, while `contract-scope.ts` — which every
related link is built with — refused it. One client, two grammars for one id. The
ruling is **admit**, and the reason is not that permissive is friendlier: the
disagreement had a correct side, and which side it was had already been measured.

**The server never refused a padded id.** `D10-59` measured `GET /contracts/01`
answering **200**, and the route says the same thing structurally — `contract_id:
int` in `contracts.py` is resolved by Pydantic, so `'07'` is 7, `'012'` is 12 and
`'0070'` is 70. A client stricter than its own API protects nothing; it turns an
address that works into *"invalid filter"*, which is the same class of lie as
answering a filter it could not parse with the whole tenant.

**Nothing is rescued that should not be.** The shape is `/^0*[1-9][0-9]*$/`: the
old `/^[1-9][0-9]*$/` with a run of zeros in front, and nothing else. `"00"` and
`"000"` are still id zero, because no amount of padding turns zero into a
contract. `"12abc"`, `"1e3"`, `"0x10"`, `" 12"`, `"12 "`, `"1.5"`, `"-1"`,
`"+12"` and `"9007199254740992"` are still refused, by the same digit test and
the same `Number.isSafeInteger` ceiling that refused them before. `readId` still
returns a `number`, so a padded address is canonicalised before anything reaches
the wire.

**The two guards are one rule, and that was measured rather than argued.** The
page tests `/^\d+$/`, then `Number.isSafeInteger`, then `> 0`; the module tests
`/^0*[1-9][0-9]*$/` and then `Number.isSafeInteger`. Over **22,643 generated
strings** — every string of length 0 to 4 over an alphabet of the characters that
matter, plus the boundary integers and the padded spellings — the two predicates
returned the same verdict on **all 22,643**, zero disagreements, and nothing
admitted or refused that the other does not. The same run sizes the ruling: **89**
strings changed hands, every one of them a zero-padded spelling of a real id
(`"001"`, `"0012"`, `"0001"`, ...). The module's half of that probe is **read out
of `contract-scope.ts`** rather than retyped inside the probe, so the measurement
cannot drift from the rule it claims to measure.

**Both ways in agree because there is one way in.** `contractIdFromInput` no
longer holds a second copy of the digits test — it trims and then calls the same
`readId` the address bar calls, so the box and the URL are one function called
twice rather than two tests that happen to agree. `inputNamesAContract` was
already built on `contractIdFromInput`, so the submit button moved with them, and
a typed `"07"` is no longer something the screen calls malformed on its way back
out.

**Two mutations, each restored and proven restored.** Reverting `ID_SHAPE` to
`/^[1-9][0-9]*$/` fails **four** cases — the four padded spellings the tables now
pin as contracts. Making the detail page's guard refuse a padded id fails
**one**, its `"07"` case. Each was restored and compared by sha256 against a
digest taken first, `825ac9d064af` and `dd7cdded78f3`, both byte-identical
afterwards. The restore works in bytes rather than text for a reason the commit
gate then measured for itself. The first version used `Path.read_text`/`write_text`,
which is **not a byte-neutral round trip on this host**: reading in text mode folds
CRLF to `\n` and writing in text mode emits `\r\n`, so a file written that way
comes back holding different bytes than it went in with, and that version reported
a mutation restored while its sha256 had changed. `.gitattributes` sets `* text=auto
eol=lf`, so a checkout is LF and the index expects LF; the CRLF was this slice's own
tooling writing in text mode, not a property of the clone, and that is the opposite
of what this paragraph said first. Two of the seven files were left **mixed**, which
the first version of this entry also got wrong by calling it "at CRLF" — and the
gate said so rather than a reader: `mixed-line-ending` (`--fix=lf`) rewrote both and
refused the commit, so the first attempt at landing the code exited 1 with every
other hook green, and the commit was re-run after the rewrite. A restore that cannot
reproduce the bytes it started from cannot vouch for the code it restored, and a
claim about line endings is a claim like any other: this one was false until a hook
measured it.

**Six false sentences struck in `DECISIONS.md`, three in `STATUS.md`.** The
divergence was asserted live in nine places — this chain's parsing note, the
second half of `D10-55`, then `D10-58`, `D10-59`, `D10-60` and `D10-61`, plus the
three matching `STATUS.md` slices. Each is struck in place and pointed at this
entry rather than deleted, which is the convention the chain already uses: that
someone once believed it is part of the record, and a struck claim is cheaper to
audit than a missing one. Seven of the nine are struck whole; the parsing note is
partly rewritten because its own regex line is the thing that changed.

**Verification.** The suite is **333 passed in 17 files**, up from 332. One
`agent` case flipped from refusal to acceptance and one new `agent` refusal case
was added beside it; the `notifications` and `escalations` refusal cases moved to
`contract=12abc`, which still exercises the damaging path. No screen, key,
namespace, catalogue entry, enum or dependency changed. The module's tables
gained four admitted spellings — `"07"`, `"012"`, `"070"` and `"0007"` — and
one refused one, `"000"`, while `"012"` left `MALFORMED`, where it had been the
wrong answer.

**Limits.** The equivalence is over strings of at most four characters plus a
hand-listed set of longer ones: a very strong sample, not a proof. The argument
covers all lengths; the probe covers the sample. The page's half of the probe is
its guard quoted, because that guard is inline in the component and not exported,
so it is a faithful copy rather than an import. No browser was involved — these
are two predicates, not two rendered screens. And the ruling is a product
decision this slice could not have made on evidence alone: the evidence said which
side was wrong, not which answer an owner wanted.

Landed as `fix(contract-scope)` + `docs(status)`.

### D10-77 — The refusal codes were never unpinnable, they were unread

`refusals.ts` ended its docstring with a limit and a reason for it: the codes are the server's
vocabulary, and pinning them would be "a fourth copy of the same fact **with nothing to verify
it against Python**". The first clause is a judgement this entry does not overturn — a
`TypeScript` union restating `REFUSAL_*` would indeed be a copy. The second clause was simply
false about this repository, and it was load bearing: it was the reason the codes were the one
half of the refusal vocabulary no test looked at, while `D10-71` pinned the sentences.

**Nothing had to be added to the server to verify them.** `i18n/composed-keys.test.ts` already
reaches into `backend/app/db/enums.py` from a node-environment spec, by walking up from the spec
to the repo root. The refusal codes are declared the same way the enums are — module-level
`REFUSAL_* = "CODE"` assignments — so reading them costs one directory walk over `backend/app`
and one regex. The new assertion compares the two sources; it writes down no code of its own, so
the guard cannot rot the way the figure it protects did.

**What the walk found, measured rather than assumed.** 27 declarations across 5 modules, **26
distinct codes**. The three tables carry 25 entries, **21 distinct codes**.

- **Stale entries: 0.** Every code the frontend translates is a constant the server declares.
  The degradation the old comment called survivable had never actually degraded, which is the
  fourth time in this chain a stated limit turned out to be a limit nobody had measured
  (`D10-62`, `D10-63`, `D10-65`).
- **Untranslated server codes: 5** — `NOTIFICATION_EXISTS`, `DECISION_MISMATCH`,
  `TENANT_MISMATCH`, `ALREADY_ESCALATED`, `RESPONSE_RECORDED`. Each is reachable only from a
  route no button on these three screens walks. `api.ts` exposes the escalation queue's
  `queue`, `update` (PATCH) and `recordResponse`, and no `raise`, so `refusal_to_raise`'s three
  codes (`escalations.py:397`) are genuinely unreachable from the UI; `record_response`
  (`:253`) calls no `refusal_to_*` at all.

**One direction is asserted, and that is the design.** Failing on an untranslated *server* code
would contradict the module's own stated choice — a sentence for a click that cannot happen is
copy that never helps, and the screens' generic ladder already covers it. So that direction
stays loose and is now documented as the trade it is, with the five codes named. The direction
that is asserted is the one that can rot silently: a code the server stops sending, or renames,
leaving a table entry nothing can reach and no failure to notice it by.

**Floors, not counts.** The spec asserts 20+ `.py` files walked, 4+ declaring modules and 20+
codes, so that a broken walk or a pattern that stops matching fails loudly instead of comparing
two empty sets and passing — the vacuity guard this chain has used since `D10-71`.

**Mutation-proven on the committed tree.** Renaming `ESCALATION_CLOSED` to
`ESCALATION_RESOLVED` in `refusals.ts` failed *translates only codes the server actually
declares* with `ESCALATION_REFUSALS.ESCALATION_RESOLVED is not a REFUSAL_* constant under
backend/app`, while the six other cases in the file stayed green — the new case isolates this
direction rather than riding on a neighbour. Restored with `git checkout` and the sha256
compared against one taken first, `c53c814e44b8…`, identical afterwards: the restore is done in
bytes for the reason `D10-76` recorded.

**Three false sentences struck in place.** The clause above inside `D10-71`; the `D10-62`
amendment's "no gate holds either side of the comparison"; and the `D10-72` amendment's "the
refusal-code vocabulary … is still unenumerated against the routers that raise it, and no gate
holds either side". Each is struck and pointed here rather than deleted, the convention this
chain already follows.

**Verification.** Suite **335 passed in 17 files**, up from 333. No screen, key, namespace,
catalogue entry, enum, route or dependency changed — the whole slice is one spec, one docstring
and two ledgers. `refusals.test.ts` gained 77 lines.

**Limits.** Only the assignment form is read, so a refusal code written inline at a `raise` site
instead of through its constant is invisible, and so is one assembled at runtime; both would
need the offending file to abandon the vocabulary, which is why the floors count files as well
as codes. The reverse direction is unasserted by design, not by omission. `node` reads the
filesystem, so this proves the frontend's tables against the server's declarations — not that
any route emits a given code on a given input, which remains `D10-63`'s territory.

Landed as `test(web)` + `docs(status)`.

### D10-78 — The correction reached four copies and skipped the one that is read

Gap 15 in `STATUS.md` ended with a limit sentence: no *screen* talks to the real API, sign-in
was the only one, `frontend/src/app/*` still called the mocked demo backend everywhere else, so
the F8, F9 and F10 routes had only ever been called by `TestClient`. Every clause of that is
false. Four other copies of the same sentence had already been struck and corrected in place —
F8's verification paragraph, F9's, the signed GATE 2 item 5 and the F1 inventory — and each
correction was written by the slice that happened to be reading that copy. The copy in the gap
list had not been, and the gap list is the one a reader consults to ask what is still open.

**Measured, not re-argued.** `from "@/lib/api"` appears in eight of the nine data screens under
`frontend/src/app/[locale]`: `contracts`, `contracts/[id]`, `review`, `agent`, `escalations`,
`notifications`, `users` and `login`. The ninth, the home screen, reads `/health` through
`HealthStatus`, which imports `probeHealth` from the same module, so it is a caller too. The
tenth `page.tsx` in the tree is the root redirect and calls nothing. A grep for any import of
the demo client under `frontend/src` returns **no files**: the mock is `demo/backend`, and
`demo/frontend` is its only consumer. `D10-29` had already recorded the consequence — no v1
router is `TestClient`-only — and `D10-33` had recorded the consent editor, so no half of this
sentence had been true for dozens of commits.

**Why it survived is the correction economy, not carelessness.** `D10-19` amended the copy in
F1, `D10-22` the review copy, `D10-23` the escalation one, `D10-24` the agent one and `D10-29`
the signed inventory — each slice fixing the sentence it could see. `D10-61` said to grep every
copy. Nobody had, because a struck sentence reads as handled, and the living list is not where
those slices were looking. This is the same failure mode as `D10-58` one ledger further out: a
fact is not amended until every copy of it is.

**What survives, and it is what survived all along.** No browser has ever rendered any of these
screens. Every one of the screen tests builds its own DOM under `jsdom` and stubs
`next/navigation`; `test_zz`-style probes have driven the *server*, and `smoke_journey.py`
drives it over a real socket, but no human and no browser has clicked through the product. That
limit is what the sentence should have said, and now does.

**Limits.** This is a ledger correction: no code, test, dependency, route, key or schema
changed, and the suite is untouched. The F8 and F9 verification paragraphs are left as written
because they are dated records of the slices that ran those checks; a strike there would claim
those slices had a screen, which they did not. The absence of a browser is not something this
repository can demonstrate — it is an absence, and only a reader can confirm it. Nothing here
asserts `STATUS.md`'s truths either: no gate can fail on a stale claim, which is the whole
reason the living list needs to be read by a human now and then.

**Verification.** `STATUS.md` and `DECISIONS.md` rewritten in bytes, CR **0** in both, single
trailing LF in both, no added line over 100 columns, every anchor asserted to occur exactly
once before either file was opened for writing, and both re-read after the write to confirm
the only changed region is the one intended.

Landed as `docs(status)`.

### D10-79 — A tenant's attention board is a second question, not the operator's narrowed

`D10-39` and `D10-40` both recorded the same next step and both called it **blocked**: an HTTP route
serving the attention report, refused because `dead_letter_count`, `undelivered_counts` and
`fields_for_reseal` read **across tenants** on purpose, and a route answers a *tenant* principal.
Each record then put the same fork: scope those three (contradicting three written decisions) or
serve them unscoped (handing one organisation another's counts and field primary keys). Both
concluded "owner decision, not a patch".

**The fork was a false binary, and that is this record's actual subject.** Those three docstrings
are arguments about the *operator's* board — the reader with no principal, whose job is to describe
every tenant at once. A tenant asking about **their own** rows is a different question, and not a
reader the docstrings ever describe, because it did not exist when they were written. So nothing had
to be narrowed: a **second reader** was added beside the first. `undelivered_counts_for_tenant` and
`fields_for_reseal_for_tenant` are new functions; `undelivered_counts`, `fields_for_reseal` and
`dead_letter_count` are byte-identical, `doctor` still exits on the same numbers, and the watch
still logs the same line. This follows `D10-58` (one fact, many places) in the opposite direction:
two readers, each with a docstring naming the question it answers, beats one function whose
behaviour depends on who called it.

**Two counts, not three.** `outbox_parked` is deliberately absent from the tenant reading. A parked
event is a relay fault attributable to the deployment: the tenant cannot cause it and cannot fix it,
so a count on their board produces a support ticket about somebody else's misconfigured transport.
`D10-39` reached the same conclusion from the other side, explaining parks away under
`EVENT_TRANSPORT=none` rather than alarming on them. The two counts that remain are the two a tenant
can act on from screens they already have — a `failed`/`suppressed` notification is retried from
their queue, and a plaintext field value is sealed from their own data.

**Admin only.** The row-level detail is a list of field primary keys. Those are harmless in
themselves — an admin of that tenant can already open every one of them in the review queue — but
`unsealed_field_values` is a statement about the deployment's encryption hygiene rather than about a
customer, and it is not a viewer's business. The narrowest role that can act on the answer gets it.

**What the route cannot leak.** It takes no tenant from the request: no query parameter, no body
field, no header. `principal.tenant_id` is resolved from the bearer token and a database read in
`app.api.deps.get_current_principal`, so the only tenant a caller can ask about is the one they
authenticated as. `backend/tests/test_attention_route.py` pins that from both sides rather than one:
each organisation holds rows of both kinds, and the assertions include a *busier* other tenant (a
clean board beside a dirty one would pass a route that simply counted the table) and the *reverse*
direction. It also pins that `?tenant_id=` is ignored rather than honoured, which is the difference
between a scoping bug and an IDOR.

**One predicate, three consumers.** The `is_sealed` scan moved into `_unsealed_ids` so the operator
reading and the tenant reading cannot eventually disagree about the same database — the day a column
is added to `PII_COLUMNS`, two copies of that comprehension would be two answers to "is this row
sealed?". `is_sealed` is shape-based, so neither reading needs `ENCRYPTION_KEY`, which is a property
`D10-39` claimed in prose and this record keeps in code.

**The fork was measured, not just argued.** `frozen` + `slots` on the new record, and a
`TenantAttentionReport` that is *not* a field-for-field copy of `AttentionReport`: it has no
`outbox_parked` and no `event_transport`. A shared record with three fields blanked for this reader
would make "this tenant has no parks" and "we do not report parks to tenants" the same value — the
confusion `D10-39` spent a section avoiding on the operator side.

**Gates.** `pytest -q` → **900 tests, 0 failures, 0 errors, 15 skipped**, counted from the JUnit XML
(888 before this slice, +12). The new file is `backend/tests/test_attention_route.py`, 12 tests. It
was mutation-tested three ways on the committed tree, each caught: removing the tenant predicate
from `undelivered_counts_for_tenant` failed three tests; replacing the predicate in
`fields_for_reseal_for_tenant` with a tautology failed three, including a raw-id leak; adding an
`outbox_parked` key to `AttentionView` failed two on the exact-dictionary comparison. The first
draft used `FieldName.COMPANY`, which does not exist, and was corrected to `ADDRESS` before the
first run.

**What this record does not claim.** (1) ~~**No screen calls it.** `frontend/src` contains no
reference to `attention` at all — there is no `api.ts` client function and no UI, so today the route
is reachable by an authenticated `curl` and by nothing else. That is half a product slice and is
stated here rather than implied away by the word "delivered".~~ **Amended by `D10-80`, not
deleted:** the other half was then built — `/[locale]/attention` reads the route through
`attention.read`, is admin-gated in the client as well as the server, and is linked from the home
screen. What survives from this clause is the reason it was written: naming a half-slice is what
made the second one visible. (2) **The park residue is not narrowed for tenants** — it was excluded
by decision, so a tenant still cannot see that their sends are parked, and `D10-39`'s operator
reading remains the only reader of that count. (3) **Gap 25 is untouched and was never blocked on
this ruling.** It is a deployment-default question: `EVENT_TRANSPORT=none` makes the relay park
every event. No route addresses it and no cross-tenant ruling touches it; the earlier gap lists that
grouped it with gap 22 as "blocked on the same decision" were wrong about that, and this record
corrects them rather than repeating the grouping. (4) **Two roles were not surveyed**: the route is
admin-only by argument, not by measurement of what an operator needs.

No schema change, no migration: `alembic` head stays `0006`. The route writes nothing, takes no
lock, and commits nothing.

### D10-80 — A route with no caller is half a slice, and the screen says so on itself

`D10-79` landed `GET /api/v1/attention` and then wrote the limit down instead of smoothing it over:
*no screen calls it*, so the route was reachable by an authenticated `curl` and by nothing else.
That was honest and it was still half a product slice. This record is the other half —
`frontend/src/app/[locale]/attention/page.tsx`, linked from the home screen, reading the route
through a new `attention.read` in `frontend/src/lib/api.ts`.

**The client declines the question the server would refuse.** The server is admin-only and
`backend/tests/test_attention_route.py` earns the 403 from an operator principal. The screen is
gated the same way at a different layer — `enabled: isAdmin` on the query — so an operator's browser
does not ask and is not told no. That is not redundancy for its own sake: the two assertions are
different claims. The server test proves an operator *cannot* read another tenant's board; the
screen test proves a non-admin session makes **zero** API calls, which is the only way to state that
the refusal is never leaned on. The second cannot be written from `TestClient`, because it starts
where the first one ends.

**Read-only, and it says what to do instead.** The board offers no action. Both counts are cleared
on screens that already exist — a `failed` or `suppressed` notification is retried from the
notification queue, and an unsealed field value is re-sealed or revealed from the review queue — so
the page links out and claims no write it does not have. That follows `D10-79`'s argument for why
these two counts and not the third: a number a reader cannot act on is a support ticket, whichever
screen renders it.

**The verdict is read, never recomputed.** `needs_attention` comes off the wire. A draft that
derived it from the three counts would have been a second implementation of the server's rule, free
to disagree with it the first time the rule changed — the failure `frontend/src/lib/refusals.ts`
exists to prevent in the other direction. `field_scan_truncated` is rendered *beside* the count it
qualifies, so a truncated scan cannot read as a small number and a clean board. Both are pinned by
test: a board with three failed notifications and the flag `false` must render calm, and the
truncation marker is asserted present and absent.

**Ids, never values.** The row detail is field primary keys, and the screen renders them as links
into the review queue and nothing more — no value is fetched, decrypted or shown. The route's
`AttentionView` has no `outbox_parked` key and the screen has no park line; the absence is asserted
from both sides rather than left to review — `"outbox_parked" in report === false` in `api.test.ts`,
and the exact seven-key comparison in the route suite. A screen is the easiest place in a stack to
reintroduce a field a decision removed.

**A sentence that contradicted the decision beside it.** The first draft of the operator's refusal
hint read *"Two of its counts are statements about this deployment rather than about a customer."*
The deployment-scoped count — parked events — is precisely the one `D10-79` kept off this board, so
the hint described a board nobody had built. Nothing failed; the catalogue parsed, both locales
agreed, the tests passed. It was caught by reading the copy against the decision and corrected to
name the work an administrator clears. `D10-59` said a comment is a claim; this is the same defect
one layer out, in product copy.

**Gates.** `npx vitest run` → **352 tests in 18 files, 0 failures**, measured on this tree;
`backend/tests/test_attention_route.py` unchanged and the backend suite re-counted from the JUnit
XML at **900 tests, 0 failures, 0 errors, 15 skipped**. No backend file, no schema change, no
migration: `alembic` head stays `0006`. The screen added 14 tests and the client 3.

**What this record does not claim.** (1) **No browser has run it.** Every assertion is a DOM the
suite built; `jsdom` proves the wiring, not the rendering. (2) **The mutation evidence is uneven,
and one gap was admitted rather than closed.** Flipping `needs_attention` to a recomputed `> 0` was
caught by **one** test, not the two predicted, and the reason is a defect in this slice's own
spec: the case titled "renders the catalogue in Bengali" only exercises a clean board, so it cannot
distinguish the two branch conditions it appears to cover. ~~The title was tightened; the coverage
was not added.~~ **Amended by `D10-82`, not deleted:** the coverage was added — that case now hands
the page the one board that separates a wire verdict from a recomputed `> 0` (counts non-zero, flag
`false`), so the same mutation fails there too: **two** tests, measured. Nothing above is deleted,
because the diagnosis was right and being specific in it is what made the fix a one-fixture change.
(3) **One write reported failure and had landed.** Two `EBUSY` responses on the catalogue
files were false — the JSON was correct, verified by reading it back. Earlier in this project the
same error class failed the other way, which is why neither the error nor the success was trusted.
(4) **The home screen's own copy moved with it** — `home.body` said administrators "get a sixth"
link, which this screen made false in both catalogues before either commit.

No source file outside `frontend/` changed.

### D10-81 — The pager took the channel twice, because a rule kept in a session is not a rule

A bare `git log -1 --format=%B 42ff55b` opened `less`, and `less` waits on a stdin nobody is going
to type into. The harness stopped waiting at 120 seconds; the process did not stop. Every command
after it timed out — `git status`, `echo`, `python -c "print(1)"`, and `cmd /c exit 7`, which starts
nothing worth waiting for and prints nothing at all. A redirect whose target file was, afterwards,
not on disk is what made the diagnosis unambiguous rather than a guess: commands were not failing,
they were not *running*. Nothing was at risk — the index was on disk and no partial commit existed —
but the channel was gone until a human killed the pager.

**It was the second time, and the first time was diagnosed correctly.** `STATUS.md` records it, in
the paragraph that begins *"Why this took three sessions, and what the shell actually did."* That
session saw `git status`, `echo shell-alive` and `python -c "print(1)"` all time out with no output,
and the proof was on disk: a root file named `hell-alive` holding 336 lines of `less` help text. The
cause was named then, and the remedy was written down — `--no-pager`, or `GIT_PAGER=cat`. The
practice then survived into at least one later session, which recorded it in passing: *"`--no-pager`
was already in use here."* So the knowledge existed, in three places, as history.

**What history cannot do is arrive in a session that has not read it.** The session that wedged the
channel this time carried no memory of either paragraph; it typed `git log` because a commit body
was wanted, and inherited the entire failure. That asymmetry is the argument for a decision record
rather than another paragraph of narrative: the ledger is what a fresh session inherits, and a
lesson stored as an anecdote is stored where the next session will not look for it. `D10-74` drew
the same distinction about a hook: *a skipped hook is not a passed hook, and an id is not a check.*
A remedy that lives only in the session that found it is not a rule, for the same reason — the
knowledge exists and nobody consults it.

**The rule.** Run git with the pager disabled, on every call whose body reaches stdout:
`git --no-pager log|diff|show …`, or `git -c core.pager=cat …`, or `GIT_PAGER=cat` in the
environment. The class is wider than git, and wider than this failure: anything that can take the
terminal and wait for a keypress — `less`, `more`, `man`, an interactive REPL — holds the same
single channel, and a wedged Python REPL had already done it once in this project. What earns it a
rule is the asymmetry. A wrong answer costs one turn and announces itself in the answer; a wedged
channel costs every subsequent command and *presents as a dead tool*, which is the one symptom this
project has now mis-diagnosed more than once.

**The artifact recurred too, which is why deleting it was not the fix.** A root file named
`contract-agent` — 3,194 bytes of escaped `git diff` output, ANSI codes intact — was in the working
tree when the channel came back, and it carries the same filename the earlier session's record
names. It was read, confirmed to be diff text, and deleted; `git log --all -- contract-agent` is
empty, so it never entered a commit and the tree is clean. But the earlier paragraph reports
deleting that same file, and a later session produced it again. Deleting an artifact removes the
evidence, not the mechanism.

**Gates.** No code, no dependency, no schema, no migration — `alembic` head stays `0006`. This is
written against `83b194e`, whose CI run `43003158157` is green on all seven jobs, the MySQL 8.4
integration job among them. The only gates that apply are the pre-commit hook set and `commitlint`
over two Markdown files, plus the ledger's own checks, which the apply script enforces by refusing
to write: no CR, exactly one trailing LF, `### D10-` headings contiguous `1..81`, no added line over
100 columns, and every removed line covered by an anchor.

**What this record does not claim.** (1) **It is a habit, not a guard.** No hook can hold a
keyboard; nothing in this repository stops the next session from typing a bare `git log`, and the
only evidence the rule works is that every git call in the slice that landed the attention board and
its ledger carried the flag. One session, not a proof. (2) **It covers git only.** `pytest`, a
database client with its own pager, and an editor can take the same channel, and none of them is
addressed here. (3) **The mechanism was not reproduced deliberately.** Proving it would have meant
wedging the channel a third time, which is exactly what the rule exists to prevent; the evidence is
the observed behaviour plus an independent earlier diagnosis that agreed with it. (4) **The stray
file's creation was never witnessed.** Its name, size and contents were measured when it was found;
the redirect that wrote it is inferred from the shape of the file and from the earlier session
recording the same artifact, not from a log.

### D10-82 — The limit named its own missing fixture, and that was the whole fix

`D10-80`'s second limit was unusually specific: flipping `needs_attention` to a recomputed `> 0` was
caught by **one** test where two were predicted, because the case titled "renders the catalogue in
Bengali" only ever exercised a *clean* board. It named the defect and named the fixture, then
stopped. A limit whose remedy is legible in the limit itself is a to-do written where nobody reads
it, so this record closes it.

**A clean board agrees with both readings, which is why that case could not
fail.** `needs_attention` and `sum(counts) > 0` differ in exactly one situation: counts non-zero
with the flag `false`. Hand the page that board and the two readings disagree; hand it zeros and
any recomputation passes. So the fix was not a new test but the right fixture — the Bengali case
now receives `notifications_failed: 3` with `needs_attention: false`, and asserts the *Bengali*
clean heading.

**The test count did not move, which is the point.** Fourteen cases before and after. A fifteenth
asserting the same branch under the same clean fixture would have raised a number and closed
nothing: the count was never the defect, the input was. This is `D10-65`'s distinction — a snapshot
is not a guard — arriving at a branch. Two tests agreeing with the code are not two guards unless
their *inputs* differ.

**Measured in both directions rather than reasoned.** Mutation applied, old body: **1** failure,
`trusts the wire's verdict over the counts it was handed`. Mutation applied, new body: **2**, that
case plus `reads the verdict off the wire in Bengali, not English` — those two being the only
failures, so the change caught the mutation and nothing else. Mutation reverted: **352 tests in 18
files, 0 failures**, unchanged, because no source file was touched. The diff is
`frontend/src/app/[locale]/attention/page.test.tsx`, 13 insertions and 2 deletions.

**The title had to move with the fixture.** `D10-80` records that this case's title was tightened
because it claimed branch coverage it did not have. It carries both halves of what it asks now — the
wire verdict and the second catalogue — so it reads "reads the verdict off the wire in Bengali, not
English". Renaming a case that a previous record renamed is the honest cost of the fixture changing
underneath it; keeping the old title would have re-broken the claim it was fixed for.

**What this record does not claim.** (1) **No browser has run it**, here or in `D10-80`; `jsdom`
proves the wiring, not the rendering. (2) **The finding branch's Bengali copy is still
unexercised.** The new fixture keeps the clean branch deliberately, because it has to be the clean
branch to discriminate, so the Bengali wording of the three count cards is still verified only by
`messages.test.ts`'s catalogue parity and not by a render. (3) **The fixture is a wire state the
server cannot emit.** `needs_attention` is derived from the counts this fixture contradicts, so the
test proves the branch reads the field; it does not prove such a board can be served.

**No `STATUS.md` amendment, and that is a measurement rather than an omission.** A search of
`STATUS.md` for the mutation count, for the Bengali case and for the string `352` returns nothing:
the limit was recorded in `DECISIONS.md` alone, so the only stale sentence was the one struck above.

**Two commands stalled this slice, both `D10-81`'s class.** Piping a *running* process —
`npx vitest | Select-String` — blocked for the full 300-second ceiling, while the same run
redirected to a file and then searched finished in seconds: the pipeline waits on a producer that
writes on exit, which reads exactly like a hung tool. And the shell keeps its working directory
between calls, so a second `cd frontend` failed *from inside* `frontend` and the command after it
ran from the wrong place. Neither was silent, but the redirect habit `D10-81` established is what
made the first one diagnosable instead of mysterious.

**Gates.** 352 tests, 18 files, 0 failures, run on this tree. No backend file, no dependency, no
schema, no migration: `alembic` head stays `0006`. One file outside `.agent-cache/` changed, and it
is a spec.

### D10-83 — The correlation id reached no screen, and the board that needed it shouted least

`D10-62` measured fifteen response shapes and found one key missing; `D10-72` wrote the key down and
surfaced it as `ApiError.traceId`. Neither made it reach a person. A search for `traceId` outside
`api.ts` and its spec returned the constructor, the docstring and nothing else: the value was typed,
documented, deliberately `null`-aware, and read by no screen. That is `D10-77`'s shape exactly — not
unpinnable, unread — and the one thing a person can hand back that names the server's own log line.

**Eleven call sites on nine screens.** Every route-level failure card now renders `<TraceId>` under
the sentence that says what went wrong: agent, attention, contracts, the contract detail's three
independent reads, escalations, login, notifications, review, users. The sentence above it is the
diagnosis; this is the quote.

**A component, because the three wrong renderings are the hard part.** An absent id must print
nothing rather than `Trace id: null`; a failure that is not our envelope — a `TypeError` from
render, a rejected `fetch` — must print nothing, since the prop is typed `unknown` because every
screen's failure slot can hold anything; and it must not be a `role="alert"`, because the failure
is already announced and a second alert makes one failure read twice. A template string gets all
three wrong, so the rules live in one component with a spec that fails on each. The copy is
`common.traceId`, and `bn` is uncast in `i18n/messages.ts`, so a missing translation is a type error
here rather than a key name in production.

**A defect found in the doing, on the screen where it mattered most.** `attention/page.tsx`
rendered its failure sentence as `<p className="text-sm">` — no `role="alert"`, no danger tone —
while every other route-level failure card had both. On the single board whose purpose is "something
here needs a person", a screen reader was told nothing and the sentence did not look like a failure.
That is fixed, and it is what makes `TraceId`'s own docstring true for that page: "the failure is
already announced" was false there, so the comment was a claim and the claim was wrong (`D10-59`).
`attention/page.test.tsx` now asserts the alert stack holds exactly one sentence, which is the check
that would have caught it.

**Login stored the sentence, so the cause died at the boundary.** Its state was `string | null`,
filled by `describeFailure(cause)`: by the time anything rendered, the `ApiError` was gone and the
id with it. It now holds the cause and derives the sentence, which also deletes a second copy of
the wording. The pre-auth failure is the one a person reports with no other context to offer.

**Measured in both directions.** Deleting the null guard — `|| error.traceId === null` — turned
exactly two cases red: the explicit-`null` envelope and the offline `status: 0` one, both printing
`Trace id: ` with an empty id, and left the other four green. Reverted, the suite is **361 tests in
19 files, 0 failures**. Fourteen files changed: twelve modified, two new, two of those a spec.

**What this record does not claim.** (1) ~~**A failed write still loses its id.** Every alert that
reports a failed action — a submit, a toggle, a retry, the home screen's sign-out — keeps a
translated string in state, so the cause is gone by the time it renders; login is the only one
converted. No count is quoted because the shape differs per screen, and citing a number nobody
measured is `D10-62`'s lesson rather than its remedy.~~ **Struck by `D10-84`, not deleted: the
direction was right and the mechanism was wrong.** On four of those eleven sites the cause was
never lost — it sat in the mutation object, in scope the whole render — and the sentence was
printed inline beside it rather than in place of it, so the id was *unread*, which is `D10-77`'s
shape. What genuinely flattened was the six card props typed `error: string | null`. Eleven sites
on seven screens are counted this time rather than declined. (2) **No browser has run this**;
`jsdom` proves the wiring, not the rendering. (3) **It does not prove a route emits `trace_id`** —
that half is `backend/tests/test_health.py`, which pins the envelope's id to the response
header. (4) **Silence is not a fault signal.** A failure produced before our middleware ran — a
proxy's `502`, a refused connection — carries no id and renders nothing, which is correct and also
indistinguishable from an envelope that simply had none.

**No `STATUS.md` amendment, and that is a measurement rather than an omission.** Its trace-id
paragraphs stop at the declaration — `ErrorEnvelope` declares the key, `ApiError` carries it — and
none claims a screen shows it, so nothing there became false. Counts and file totals moved, but they
are timestamped measurements of other slices, not standing claims.

**Gates.** 361 tests, 19 files, 0 failures, on this tree. No backend file, no dependency, no schema,
no migration: `alembic` head stays `0006`.

### D10-84 — A refused write now says which request it was

`D10-83` gave the reads their correlation id and closed by admitting the writes still had none:
eleven mutation paths across seven screens — home, agent, contracts, escalations, notifications,
review, users — rendered each refusal as a sentence built by `describeFailure(cause)` at the
moment the card was constructed. The mutation held the `ApiError`; the card received the
sentence; the id was gone before any element could show it. The fix is one component, not eleven.

**`ErrorNote` takes the cause and the wording, never the sentence.** `describe` is a callback, so
the words an operator reads and the id beneath them are derived from the same value in the same
render. A finished string would have left the id to be found somewhere else and rebuilt the exact
bug the component removes: a card printing one request's words beside another request's id. Each
screen keeps its own vocabulary — `contracts` words an ingest failure and a consent failure
differently, `review` separates a refused decision from a refused reveal — and every one of them
already had a `describeFailure` in the file. Nothing was reworded, which is why no copy test moved.

**A defect in the record, found by doing the work.** `D10-83`'s limit (1) said every failed action
"keeps a translated string in state, so the cause is gone by the time it renders". On four of the
eleven that was false: `contracts`' ingest form, `users`' create form, `escalations`' reply note
and the home screen's sign-out all held the mutation object with the cause intact and printed
`describeFailure(x.error)` inline, so the id was in scope and unread rather than destroyed. What
genuinely flattened was the six card props typed `error: string | null` — the boundary — and
those needed the prop widened to `ReactElement | null`, not a second mechanism. The amendment above
carries the correction; a limit stated from the shape of the code is worth less than one measured.

**The block-element constraint.** `TraceId` renders a `<p>`, and so did every failure box it was
being placed into, so each box became a `<div>` rather than nesting one paragraph in another. That
is also why the existing `queryAllByRole("alert")` assertions kept passing unchanged: the element
carrying the role is still the sentence, and the id is still a sibling below it, not inside it.

**Measured in both directions, on this tree.** Deleting `|| error.traceId === null` from
`trace-id.tsx` turned five cases red across three spec files — `trace-id`, `error-note` and
`users`, the last because a widened prop is only worth widening if the id survives the crossing.
Pointing `ErrorNote`'s `<TraceId>` at the sentence instead of the cause turned four red, all of
them the present-id cases, and left the absence cases green — which is what shows the spec bites
on the pairing rather than on any rendering at all. Reverted, the suite is **370 tests in 20 files,
0 failures**. Ten files changed: eight modified, two new, one of those a spec.

**What this record does not claim.** (1) **No browser has run this**, as in `D10-83`; `jsdom`
proves the wiring, not the rendering. (2) **It still does not prove a route emits `trace_id`** —
that half is `backend/tests/test_health.py`, which pins the envelope's id to the response header.
(3) **The log the id points at is not opened here.** An id on screen names the server's own log
line; nobody has walked from one to the other in this repository, and this record does not.

**No `STATUS.md` amendment, and that is a measurement rather than an omission.** Its trace-id
paragraphs still stop at the declaration — the envelope declares the key, `ApiError` carries
it — and none claims which screens show it, so the widening made none of them false. The counts
it quotes are timestamped measurements of other slices, not standing claims.

**Gates.** 370 tests, 20 files, 0 failures, on this tree. No backend file, no dependency, no
schema, no migration: `alembic` head stays `0006`.

### D10-85 — The reminders struck a count three times and left the reason standing

`GATE 2`'s fifth reminder states the residue of every screen slice in one sentence: no browser has
run any of these screens, "because `frontend/` has no test runner and no e2e suite. The UI is
typechecked and linted, which is weaker than a test." That was true when the record was signed. It
stopped being true at `D10-37` (`df81b8e`), which gave `frontend/` a runner, one spec and a gate
that runs it, and `D10-41` (`8f65fbf`) then taught that runner the `@/*` alias. Between those
commits and this one, four amendments landed directly underneath the sentence — `D10-29`,
`D10-47`, `D10-56`, `D10-57` — and not one of them read it.

**Why four passes missed it.** Each amendment answered the question its own slice asked: which
routers a screen now calls (`D10-22`, `D10-23`, `D10-29`), whether a production build renders a
route (`D10-47`), how many screens are driven in `jsdom` (`D10-56`, `D10-57`). None of those
questions is "is the sentence underneath still true", and each inherited it unchanged. The count
was re-measured twice; the premise never was. That is `D10-56`'s defect one layer down — there a
stale count went unrevisited while new specs landed, here a stale *reason* survived while the
count above it was corrected.

**The sweep, enumerated rather than estimated.** Twenty-eight places in the ledgers say some form
of "no frontend test runner": nineteen in `STATUS.md`, nine in `DECISIONS.md`, none in
`README.md` — `D10-70` had already taken the current-document copy. Twenty-seven of the
twenty-eight are dated records of slices written before `D10-37`, and this document's own rule
leaves them as written (stated in `STATUS.md` gap 15, `:3663`–`:3665`: "those are dated records of
those slices and are left as written"). The twenty-eighth is the live reminder, and it is struck.

**A second stale number in the same entry, found by reading it.** `D10-80` gave
`/[locale]/attention` a screen and a `page.test.tsx`, and reminder 5 was not revisited then, so it
still said "nine of the nine" where the tree holds ten `page.test.tsx` files under `src/app`.
Gap 15 *was* amended for that tenth screen; reminder 5 was not, which is the disagreement `D10-29`
recorded between those same two chains. The count is struck to ten, measured by listing the
directory rather than by counting the sections.

**What the strike keeps, because it never depended on the runner.** `jsdom` is not a browser:
nothing is hydrated, clicked or navigated, no screen has rendered with data behind it, and there
is no e2e suite. That half stands unstruck, so the residue is one clause smaller and not closed.

**What this record does not claim.** (1) **No product behaviour changed** — two Markdown files; no
code, no test, no dependency, no schema, and `alembic` head stays `0006`. (2) **The suite figure is
re-measured, not copied forward:** `npx vitest run` on this tree reports **370 passed in 20
files**, the same number `D10-84` recorded, which is the expected result when only prose moves.
(3) **It does not rule on the reproducibility clauses.** Two records say a key audit can be rerun
only on the machine that ran it; whether the counts those audits produced are now re-derivable
from committed specs is a different question, and this record does not decide it.

**Gates.** 370 tests, 20 files, 0 failures, on this tree. Ledger invariants rechecked: no `CR`, one
trailing `LF`, no added line over 100 columns, balanced strikethrough markers, `D10-` headings
contiguous through `D10-85`.

### D10-86 — Four clauses said "only on my machine", and three specs had landed since

Two `STATUS.md` rows carry the same parenthesis — at `:4127` and `:4244` — and the two decision
entries that copied it carry it again, `D10-28` at `:2868` and `D10-29` at `:2948`. Each says the
key audit is reproducible only on the machine that ran it, because the script lived in
`.agent-cache/`, which is gitignored. The clause was true when it was written: nothing in the tree
parsed those catalogues. It stopped being true on 2026-11-10, in three commits — `9ed8361` pinned
the two catalogues against each other, `19066f4` checked the keys the screens compose, `894161b`
pinned the refusal vocabulary to both catalogues — and `.github/workflows/ci.yml:234` runs
`npm test` inside the `frontend` job, so all three run on every push.

**Which verdicts are now asked, one at a time.** `messages.test.ts:127` compares the key sets in
both directions and writes no key down, which is `0 only-in-en` and `0 only-in-bn`. `:136`
compares the ICU argument shape of every key across the two catalogues, which is
`0 placeholder drift`. `composed-keys.test.ts:276` resolves every key a screen composes from a
table, in both catalogues, which is `0 referenced-but-absent` for those sites. Not asked, and the
strike does not pretend otherwise: a literal `t("…")` key is typed nowhere — `useTranslations()`
takes no namespace and `t()` accepts any string (`D10-71`) — so a renamed literal still reaches a
screen as a raw key; `0 unused` is asked by `lib/refusals.test.ts:117` and only of keys named like
a refusal; and the bare-map-key verdict was a grep at the time and stays one.

**The figures stay, because they describe the commit they name.** `271` describes `07c31ab` and
`337`/`66` describe `bcfce75`; neither row claims those numbers describe today. Measured on this
tree: **492** leaves per locale in each catalogue, **109** under `contracts`, **75** under
`notifications`; `npx vitest run src/i18n` reports **13 passed in two files**.

**Why this is a ruling and not a fourth caveat.** `D10-85` recorded at `:6823`–`:6825` that it did
not decide whether those counts are re-derivable from committed specs — "a different question", it
wrote. This entry is that decision. The failure the clause described was not that the counts were
wrong; it is that a reader had no way to ask the same questions again, and inherited a caveat
instead.

**What this record does not claim.** (1) **No product behaviour changed** — two Markdown files, no
code, no test, no dependency, no schema, and `alembic` head stays `0006`. (2) **The audits'
arithmetic is not re-run.** Nothing here certifies that the 271 or 337 counts were right; it says
only that the same questions are now asked by committed code, which fails the build when broken.
(3) **The four clauses struck are the four that exist** — `README.md` and `DEMO.md` were swept and
hold none.

**Gates.** 370 tests, 20 files, 0 failures on this tree. Ledger invariants rechecked: no `CR`, one
trailing `LF`, every added prose line under 100 columns, an even count of strikethrough markers on
the added lines, `D10-` headings contiguous through `D10-86`. The two amended cells are the
exception and are named rather than rounded away: they were 363 and 381 columns at `HEAD` and are
404 and 422 now, because this slice inserts a strike and a pointer into table cells that were
already wider than the rest of the file keeps.

### D10-87 — The probe was deleted, and the repo could always rebuild it

`D10-30` ended a "what is not proven" paragraph by saying its probe and repeat-driver scripts
"live in `.agent-cache/`, which is gitignored — reproducible on the machine that ran them, not
from the repo". `D10-86` struck four clauses of the same family and deliberately left this one,
recording that the specs it cited did not answer it. This entry answers it, and the answer is
that the clause was never true — not even on the machine that ran it — because both scripts are
**gone**. `Test-Path` on `.agent-cache/probe_staticpool_threads.py` and
`.agent-cache/repeat_test.py` returns `False`, and `.gitignore:25` holds `.agent-cache/`, so
neither file was ever in the repo in the first place.

**The conclusion drawn from that was the mistake.** "Not from the repo" treated the *script* as
the evidence, when the evidence is the *experiment*, and the experiment is fixed by two committed
facts: `backend/tests/conftest.py:140-144` commits the engine recipe (`:memory:`,
`check_same_thread: False`, `StaticPool`) and `:129-141` commits the claim it demonstrates. A
probe rebuilt from those lines alone — no cache directory, no prior file — reproduced both halves.

**What the rebuild measured.** Two simultaneous `engine.connect()` calls hand back the *same*
`sqlite3.Connection`: `is` comparison `True`, and the semantic check agrees, a row committed on
one thread reading back as `1` from another, which is only possible if the connection (and so the
`/memory:` database) is shared. Concurrent use of that one connection faulted **5 times in
15,966 statements — 0.31 per 1,000**, each one `InterfaceError: (sqlite3.InterfaceError) bad
parameter or other API misuse` on `INSERT`. A
thirty-round in-process driver rebuilt around `tests/test_escalation_scanner.py:528` passed
**30/30 rounds, 0 failures**. Both figures come out of the repo; neither needs a cache file.

**A wrong reading of my own, corrected before it reached this entry.** The first identity probe
printed `same=False`, which would have contradicted `D10-30`'s "identical connection object ids".
It was an artifact: `id(conn.connection.dbapi_connection)` was read off a temporary that could be
freed, and the two threads' connects were separated by a `close()`. Holding the references and
asking `is`, then asking the semantic question — is a row written on one thread visible from the
other — gave `True` and `1` on repeated runs. The original claim is corroborated, not overturned;
only the reproducibility clause falls.

**One committed claim was incomplete, so the docstring changed.** `conftest.py` named
`OperationalError`/`SystemError` as the classes concurrent use raises. The class that actually
appeared was `InterfaceError`, so the docstring now lists all three and cites the measured rate.
This is the slice's only code change: a comment, no behaviour, no test.

**What this record does not claim.** (1) **It does not re-derive the A/B control.**
`D10-30`'s other limit — the *old* test body was never run under the driver, so no per-run failure
rate exists for that test — is untouched by anything here. (2) **The rate is not the same
number, and does not contradict the old one.** "Roughly one statement in a thousand" described
the original contention workload; 0.31 per 1,000 describes this one, and both are low enough to
look like flakiness, which is the only property the argument uses. (3) **`D10-30`'s figures
were not re-derived, and the suite has grown since.** 840 collected, 825 passed, 15 skipped
described the tree that wrote them; this tree measures 900 collected, 885 passed, 15 skipped, 0
failures. That is 60 more tests and the same 15 skips, not a disagreement — the figures are cited
as historical, and no claim here rests on either count.

**Gates.** The backend suite runs green over the changed docstring — 900 collected, 885 passed, 15
skipped (the opt-in MySQL integration tests, which skip without `MYSQL_TEST_URL`), 0 failures,
counted from JUnit rather than from a truncated terminal summary. The ledger invariants are
rechecked: no `CR`, one trailing `LF`, every added prose line under 100 columns, an even count of
strikethrough markers on the added lines, `D10-` headings contiguous through `D10-87`.

### D10-88 — The compose header's reason held for Redis and had not for Ollama since F7

`docker-compose.yml`'s header closed its Kafka note with *"No Redis/Ollama service is defined yet
because no code talks to either; they arrive with the milestones that use them."* One reason, two
subjects — and the reason was only ever true for one of them.

**Redis.** Still accurate. `app/core/readiness.py` records that nothing in `backend/app` uses it;
`D2-03` chose a row lease over a Redis lock for the outbox; and the login throttle's counters are
rows in `login_attempts`, not counters in a cache (`D10-16`). No service, and no code that would
reach one.

**Ollama.** False as of F7. `app/workers/extractors.py:282` defines `OllamaExtractor`, which posts
to `/api/generate` with `format: "json"` and `temperature: 0` and raises
`ExtractionUnavailableError` rather than guessing when the model is silent; `build_extractor`
(`:356`) selects it whenever `EXTRACTION_BACKEND=ollama`; `Settings.ollama_base_url` is declared at
`app/core/config.py:121` and `OLLAMA_BASE_URL` is documented at `.env.example:53`. Code talks to
Ollama. And the milestone that uses it — F7 — has already landed, so the header's second clause,
*"they arrive with the milestones that use them"*, names an arrival that never happened for Ollama:
the milestone arrived, the service did not.

**What is actually missing is the service,** which is the distinction `gap 17` draws in `STATUS.md`
and the header erased. `EXTRACTION_BACKEND=pattern` is what runs, not because nothing here can
speak to a model, but because nothing here serves one — and `pattern` stays the default because it
is the backend whose behaviour this repository can prove. The header now says exactly that.

**What this record does not claim.** (1) **No behaviour changed** — one comment in one deployment
file; no code, test, dependency, schema or migration, and `alembic` head stays `0006`. (2) **It does
not close `gap 17`.** Defining an Ollama service is deployment configuration, and a service that no
test exercises and no readiness probe covers would be the unverifiable milestone `D7-03` rejected;
that is the owner's call, not a doc fix's. (3) **It does not claim a model has ever answered.** No
live Ollama has been reached by this tree, before or after this commit.

**Gates.** The F5 table row that recorded the header as "corrected" is struck in place and pointed
at this entry, so the two places that carried the claim now agree — that row is a table line, not
prose, and striking it in place rather than reflowing it keeps the diff to the clause that was
wrong. The corrected compose comment stays under 100 columns per line. The ledger invariants are
rechecked: no `CR`, one trailing `LF`, every added prose line under 100 columns, an even count of
strikethrough markers on the added lines, `D10-` headings contiguous through `D10-89`.

### D10-89 — The channel advisor may re-point a permitted decision, never widen it

**Context.** The agent milestone asks for a model that picks the channel. The policy slice (`D9-05`,
gap 19) deliberately shipped without one: it selects deterministically and sets `needs_review=False`
unconditionally. `agent_backend` named a chooser that no code path asked for an answer from.

**Decision.** `app/workers/advisory.py` adds a *post-policy* advisor. `permitted_channels` runs
first and unchanged; only when it returns a non-empty set is the model asked, and it may choose
only from that set. `app/agents/prompts/` carries the prompt as data — `registry.json` plus
`advisory_channel_v1.txt` — so a version bump is a file edit rather than a deploy. The route asks
for an advisor through `advisor_for`, which returns `None` unless `AGENT_BACKEND=ollama`, so the
default `deterministic` path behaves exactly as before. Transport mirrors the existing
`OllamaExtractor`: `/api/generate`, `format=json`, temperature 0, and the same `ollama_base_url`
setting with its TLS-or-loopback rule.

**Why it is shaped this way.** (1) *The policy stays the authority.* The model can re-point inside
the permitted set and can add `needs_review`; it can never widen the set, and an answer naming an
unpermitted channel keeps the policy channel and flags the row. (2) *Every failure keeps the
decision and flags it.* Transport error, invalid JSON past `agent_repair_max`, a missing prompt, an
open breaker or a full queue all leave the policy channel in place and set `needs_review=True`,
which `dispatch.py:500` already refuses with `NEEDS_REVIEW` — so the safe fallback is "no contact",
never "send email instead". (3) *Nothing identifiable leaves the process.* The payload is six keys
— `age_bucket` (withheld by default), `has_email`, `has_phone`, `contract_value_bucket`,
`days_to_expiry`, `permitted_channels` — wrapped in `<user_input>` tags, and `ChannelAdvisory` is
built `extra="forbid"` so an injected extra key cannot masquerade as a valid answer. (4) *A queued
model call is backpressure, not a failure:* the per-replica semaphore (5 slots, 5s wait) returning
empty keeps the decision, flags review, and deliberately does **not** touch the breaker, so ordinary
traffic cannot open it and suppress advice that would work a second later.

**Blocked alternatives.** A hosted model is unrepresentable rather than discouraged: `AgentBackend`
is a closed literal, so `AGENT_BACKEND=openai` is a startup `ValidationError`. Falling back to email
when the model is unsure is refused for `D10-88`'s reason applied to a new caller. Letting the model
decide eligibility (rather than re-pointing within it) would put consent behind a probabilistic
judgement. A Redis lock was not used for the concurrency cap: a per-process semaphore is enough to
protect the shared model queue and needs no distributed state.

**Limits.** No MLflow logging: the audit trail is the `agent_decisions` row (prompt version, model,
tokens, latency, repair count) and nothing is sent to MLflow. No `PROMPT_AB_SPLIT` A/B; the registry
pins one version per prompt name. No golden-scenario review gate before a model or prompt change.
**No live model has ever answered:** `AGENT_BACKEND` defaults to `deterministic`, no Ollama service
is defined (`D10-88`), and the only advisor this code has met is a test double — gap 17 is
unchanged. The retry prompt carries the schema error text, which can quote the model's own invalid
output; it cannot quote customer data, because none is sent. The semaphore is in-process, so the
real ceiling is 5×N replicas rather than 5.

**Gates.** `backend/tests/test_advisory.py` (19 tests) pins the six-key payload, the bounded repair
loop, the breaker, the unpermitted-channel refusal, the transport-failure fallback, and the
queue-full path's effect on the breaker. `tests/test_config.py` gains cases showing a hosted backend
is not representable and a remote `OLLAMA_BASE_URL` must be TLS. The dispatch refusal for a flagged
decision was already pinned at `tests/test_notifications.py:561-569`.

### D10-90 — Metrics are a dependency-free registry whose labels cannot hold PII

**Context.** P2-J asks for Prometheus metrics. Neither `prometheus_client` nor
`prometheus-fastapi-instrumentator` is installed, and the text exposition format is small enough
that adding a dependency (and a version to pin, audit and keep current) buys little.

**Decision.** `app/core/metrics.py` implements `Counter`, `Gauge` and `Histogram` plus `render()`
for the 0.0.4 text format: no third-party code, no background thread, no network. `GET /metrics`
sits outside `/api/v1` beside `/health` (a scrape path is an infrastructure contract, not a versioned
product API), is unauthenticated like every Prometheus target, and is hidden from OpenAPI.

The label rule is the security decision, not a formatting preference. `LABEL_VALUE_RE` is
`[a-z][a-z0-9_.-]{0,31}`: no `@`, no `+`, no whitespace, and a leading letter is required, so an
e-mail, a phone number, a date or a bare amount cannot be expressed as a label *at all*. A violation
raises `LabelValueError` naming only the label and the rule — never the offending value, because an
exception message is a log line and the value may be the PII the guard exists to stop.

Three families are wired where the work already happens: `extractions_total{status}` and
`extraction_confidence_histogram` in `extract_contract`, `notifications_sent_total{channel,status}`
in `dispatch_notification`.

**What this deliberately does not do.** Only 3 of the spec's ~13 families exist. Declaring the other
ten so they read `0` forever would be the placeholder logic the spec forbids in production paths, so
`escalations_total`, `agent_decisions_total`, `voice_calls_total`, `ollama_requests_total`,
`ollama_latency_seconds`, `agent_schema_repair_total`, `ollama_circuit_breaker_state`,
`kafka_consumer_lag`, `worker_replicas`, `outbox_pending_count` and `redis_connections_active` are
absent rather than hollow. Consequently no alert rule can be written yet, and `extractions_total`
records only `status="ok"` — extractor failures still propagate as exceptions, so the
`HighExtractionFailureRate` rule would never fire. `notifications_sent_total` counts *attempts* by
this attempt's outcome: a quiet-hours deferral is not counted, so `sent + failed` is not "attempts".
The registry is per-process; the `worker_replicas` gauge cannot be derived from inside one replica.
No Prometheus rules, Grafana dashboards, alert routing, or OpenTelemetry/Jaeger traces are added
here either.

**Gates.** `backend/tests/test_metrics.py` (21 tests) pins the text shape, cumulative buckets,
`+Inf`, `_sum`/`_count`, the PII guard (e-mail, dates and bare numbers refused; message value-free),
label-set mismatch, and the endpoint's status/media type/OpenAPI absence. It also feeds every
`Channel` and `NotificationStatus` member through `NOTIFICATIONS_SENT_TOTAL`, so renaming an enum
member to something a label cannot hold fails in CI instead of raising at 03:00 inside a send.
`python -m pytest tests/test_metrics.py` -> 21 passed; the five suites covering the instrumented
files -> 147 passed; `python -m mypy app` -> no issues in 68 files.

### D10-91 — A raised escalation mails the operator, after the commit, and carries no customer data

**Context.** `EscalationScanner` wrote escalations nobody was told about. `app/workers/escalation.py`
says so itself: "This slice writes no background job, so nothing flips
`EscalationStatus.SLA_BREACHED` on its own", and the queue reports `overdue` only to a person who
opens the screen. The spec's P2-G asks for an "escalation email alert" and P2-J records the rule
`EscalationEmailFailed: increase(escalation_email_failed_total[1h]) > 0`. An escalation is raised
precisely when the customer could not be reached, so an alert that reaches nobody is the one failure
mode with no second chance.

**Decision.** `app/workers/escalation_alerts.py` mirrors `attention_alerts.py`: an
`EscalationAlerter` protocol with one `raised(escalations)` call, an `SmtpEscalationAlerter` built on
the `deliver()` seam `SmtpSender` already exposes for operator mail, and `build_escalation_alerter`
returning `None` when `ESCALATION_ALERT_TO` is empty. `EscalationScanner.__init__` takes
`alerter=None`, which means "decide from settings" rather than "never alert", so a deployment that
set the address gets its alert without the entry point passing anything.

Four properties are deliberate:

- **The alert is sent after `session_scope` commits, never inside it.** A slow or refusing relay must
  not hold row locks across a batch it just wrote, and must not be able to roll back an escalation
  that already exists. `_escalate` therefore returns the row it opened (`RaisedEscalation`) instead
  of `True`, so the alert can only name escalations that were actually written.
- **One message per pass, not one per escalation.** A pass may open `ESCALATION_SCAN_BATCH_SIZE`
  rows; fifty mails would bury each other and the operator would act on none. Same reasoning as
  `D10-40`'s "alerts fire on the verdict changing, not on the finding standing".
- **The message carries ids and a deadline, and no customer value of any kind.** No name, phone,
  e-mail, address or DOB. Mail is not a channel this project treats as safe for customer PII, and
  the recipient is the deployment's operator — so no consent, quiet-hours, suppression or template
  gate applies. Those gates govern contacting the *customer*; this is us telling ourselves.
- **A failed alert never raises out of the tick.** The escalations are committed and remain in the
  queue; an exception would make `run_forever` log a failed pass and imply nothing was written when
  everything was. A refusal costs one `ESCALATION_EMAIL_FAILED_TOTAL` increment and one log line.

`ESCALATION_ALERT_TO` is refused at start-up when it is malformed, or when it is set with
`NOTIFICATION_TRANSPORT` that is not `smtp` — the same two failure modes `ATTENTION_ALERT_TO`
already refuses, and for the same reason: an address with no relay behind it reads as "a person is
being told". `ESCALATIONS_TOTAL{status}` is incremented **after** the commit too, so a rolled-back
batch cannot leave escalations in the series that no queue holds.

**What this deliberately does not do.** Nothing alerts on a *breach*. Making that stateless is
impossible — an overdue row stays overdue, so without a "last told" memory every pass would re-alert
forever — and storing one would contradict the recorded decision that `overdue` is derived and never
written. Breach alerting therefore still needs either a state table or an acceptance of repetition,
and is left open rather than guessed at. This also does nothing about the `SLA_BREACHED` enum value,
which remains unreachable; about the inbound direction (a customer's STOP still has no way in, so
`do_not_contact` can only be set by an operator); about retrying a failed alert; about per-tenant
routing or per-tenant alert preferences; and about deduplicating an alert across replicas — two
scanner replicas each build their own alerter and each mail what their own pass opened.

**Gates.** `backend/tests/test_escalation_alerts.py` (14 tests) pins the batch-not-per-row shape, a
quiet pass telling nobody, the refusing-relay case (escalation survives, `run_once` still reports
`opened=1`, counter advances), an unconfigured address building no alerter, the message naming ids
and containing no customer value, the UTC deadline rendering, and both start-up refusals.
`python -m pytest tests/test_escalation_alerts.py tests/test_escalation_scanner.py
tests/test_escalations.py` -> 91 passed; full suite -> 958 passed, 15 skipped; `python -m mypy app`
-> no issues in 69 files.

### D10-92 — SMS gets its own switch, its own adapter, and a refusal in production

**Context.** P2-E asks for SMS and for opt-out. `Notification` has carried an `sms` member since
F10 and both `D10-04` and the SMTP slice refused it on purpose, because there was no gateway — the
`senders.py` docstring said, in as many words, "deliberately **not** here: SMS and voice". Two
things blocked changing that. The first was shape: `MessageSender.available: bool` answers "is a
transport configured", which is the wrong question once a deployment can hold a relay and no
gateway. A `True` there lets a row be attempted and then refused *inside* the adapter, after the
recipient has been decrypted — i.e. the one place a PII read should not happen.

**Decision.** Three parts.

- `MessageSender.channels: frozenset[Channel]` replaces `available`. `dispatch_notification` asks
  whether this **row's** channel is in the set and refuses `SENDER_UNAVAILABLE` *before* the
  contact value is decrypted, so an undeliverable channel cannot even cost a read of customer PII.
- `TextbeeSender` implements `sms` over a fake-able `HttpTransport = Callable[[Request, float],
  bytes]`, following the `urlopen` seam `advisory.py` already uses; no dependency was added. The
  gateway's message id lands in `Notification.provider_message_id`. A body the parser cannot read
  yields `None` and still counts as **sent**, because a retry after a send the gateway accepted is
  a second message to a real person — the same acknowledgement rule as the outbox relay.
- `SMS_TRANSPORT: none | textbee` is a **second switch**, not a third `NotificationTransport`
  member. One enum would have to enumerate the combinations (relay only, gateway only, both,
  neither) that the two independent transports actually take, and `RoutingSender` already composes
  them per channel and reports the union as `channels`.

Four properties are deliberate:

- **No "reply STOP" in the body.** The obvious line was in the first draft and was removed:
  ~~nothing in this build reads an inbound reply~~, so telling a customer they can stop would be a
  promise the system cannot keep. **Amended by `D10-93`:** a reply *can* be read now, but only when
  the deployment is configured to listen, and a static template cannot know that — so the sentence
  would still be unkeepable where `INBOUND_TRANSPORT=none`. The reason narrowed; the wording did not
  change.
- **`SMS_TRANSPORT=textbee` is refused in staging and production** ~~until an inbound route
  exists~~. A message nobody can opt out of is an unsolicited one, not a smaller notification.
  Development stays open so the adapter is exercisable. **Narrowed by `D10-93`:** the refusal now
  names its real precondition — `INBOUND_TRANSPORT != "textbee"` — so a deployment that also listens
  may send. Sending without listening is still refused everywhere outside development.
- **https unless loopback, in every environment.** Stricter than the `OLLAMA_BASE_URL` rule on
  purpose: that one guards a feature vector, while this request body carries the customer's number
  and the header carries the key.
- **No gateway text reaches `last_error`.** An HTTP failure records the status code; a transport
  failure records exception *types*. Gateway prose routinely quotes the rejected recipient.

**A defect this slice introduced, then fixed.** The first version named
`type(getattr(exc, "reason", exc)).__name__`, intending "URLError ⇒ URLError". But `URLError.reason`
is usually a plain **`str`**, so a DNS failure was stored as `"SMS delivery failed: str"` — it named
the wrapper's payload, not the cause, and told an operator nothing. `_failure_name` now names the
raised exception and appends a nested exception's type (`URLError (gaierror)`); only types cross
that boundary. `test_a_nested_cause_is_named_by_type_and_never_by_its_text` pins both halves. The
identical defect existed in `advisory.py`'s unreachable-model path, written in `D10-89`, and is
fixed there too.

**What this deliberately does not do.** No live gateway has ever been contacted: every test drives a
fake transport, exactly as `test_senders.py` drives a fake `smtplib`, so "works against
api.textbee.dev" is untested. ~~There is still **no inbound direction** — no delivery receipt is
parsed, no reply is read, no STOP webhook exists — so `do_not_contact` can still only be set by an
operator or the CSV import.~~
`test_sms_is_refused_outside_development_until_a_reply_can_be_read` is the test to delete when that
route lands, and its refusal is a placeholder rather than a behaviour worth keeping. **`D10-93` adds
the STOP webhook**, so `do_not_contact` is reachable from the customer's own phone where inbound is
configured; delivery receipts, START/HELP and free-text replies are still absent. **That test was
renamed rather than deleted:** the refusal turned out to name a real property — sending to a customer
the deployment cannot hear — so it is kept, narrowed, and paired with a test proving the other half
boots. Also open: one device id for the whole deployment (no per-tenant routing), no retry distinct
from the notification's own backoff, no handling of gateway rate limiting, and `SMS_TIMEOUT_SECONDS`
is the only bound on a call made while holding the row lock.

**Gates.** `backend/tests/test_sms_sender.py` (28 tests) pins the request shape (device route,
API-key header, timeout, URL normalisation), a body carrying no extracted value, the recipient and
key never appearing in an error message or a `repr`, the channel refusal, id parsing (including an
unparseable body still reading as sent), routing composition, and the start-up refusals. Full suite
**986 tests: 971 passed, 15 skipped, 0 failed** (was 958/943 — the +28 is this file);
`mypy app` -> no issues in 69 files; `ruff check` and `ruff format --check` -> clean.

### D10-93 — An opt-out needs a keyed digest, a signed body, and a 202 that says nothing

`D10-04` made consent revocable and `D10-92` shipped a way to send SMS, but `do_not_contact` could
still only be set by an operator or by `consent-import`: the platform could speak and not listen.
P2-E's second half is the missing direction — one internet-facing, account-less route where a
customer's `STOP` arrives. Three things stood in the way, and each one is the reason for a piece of
what follows.

**The stored number could not be looked up.** Contact values live in `raw_value`/`normalized_value`
as Fernet ciphertext under a random IV: the same number encrypts to different bytes every time, so
`WHERE value = :number` cannot exist even in principle. `ExtractedField.blind_index` is a keyed
HMAC-SHA256 over the *normalised* value — the same `normalize_value` output the extractor already
stores — and `contracts_for_contact` is the cross-tenant read that puts the digest, never the
plaintext, into the WHERE clause.

**The index key is its own secret, never derived from `ENCRYPTION_KEY`.** That key is rotated (it
carries a retired-key ring, and `reseal-fields` exists for exactly that), so any derivation would
change every digest the moment it changed, orphaning every stored opt-out in one step with no error
to show for it. `CONTACT_BLIND_INDEX_KEY` therefore has a separate lifetime — which is also why
`backfill-blind-index` fills only `blind_index IS NULL` rows. Recomputing live digests would let a
rotated key rewrite live indexes one batch at a time: the same failure by a slower route.

**A required argument, not a defaulted one.** `extract_contract(..., contact_index=...)` and
`review._apply_correction` take the index as a required parameter. A default would hand every caller
a silent skip path that stores a contact value no `STOP` could ever find; `cli extract` refuses to
run without the key for the same reason, and a corrected number recomputes its digest so a review
edit cannot leave an opt-out both unfindable and mis-suppressing.

The route itself — `POST /api/v1/inbound/sms` — holds five properties:

- **The signature covers the raw bytes.** `X-Signature` is `hex(HMAC-SHA256(secret, body))` over the
  body exactly as read, compared with `hmac.compare_digest`; `valid_signature` fails closed on an
  unset secret, a missing header and a mismatch alike. An unset secret meaning "unsigned is fine" is
  the shape of bug that makes an open endpoint look authenticated. Both sides are compared as
  **bytes**, because a header value is latin-1 decoded and `compare_digest` raises `TypeError` on a
  non-ASCII `str`: comparing the two as text made one high byte in `X-Signature` an unhandled 500 for
  an unauthenticated caller, a cheaper way to fill the log than guessing the digest. Found by reading
  this function back after the gates had already gone green, and pinned by
  `test_a_signature_that_is_not_ascii_is_refused_rather_than_crashing`.
- **Nothing in the request names a tenant.** The payload model carries no `tenant` and no `contract`
  field, and `extra="ignore"` lets the gateway add fields without turning its release into our
  outage. The number is the only handle, it is looked up and never trusted, and the tenant is stamped
  from the matched contract row.
- **Every accepted outcome is the same `202`.** Matched, unmatched, already opted out, not a keyword:
  one status, one body. A route that answers differently for a hit and a miss is a
  number-and-tenant enumeration oracle, and everything the response says the request already knew.
- **Only a bare keyword withdraws consent.** `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`,
  `QUIT`, `OPT OUT`, `OPTOUT` — after collapsing whitespace, stripping trailing punctuation and
  lowercasing, so `"STOP."` (what a phone keyboard produces) is honoured and "stop by the office" is
  a person talking.
- **The write is one transaction over every match.** A number on several contracts (a shared office
  line, or the same person as a customer of two tenants) is one act, and a partial application would
  leave some contracts still mailable after the customer said stop. Each changed row gets its own
  tenant-stamped audit entry (`ACTOR_CUSTOMER`, `ACTION_CONSENT_OPTED_OUT`) carrying ids and flags,
  never the number.

Replay safety is **state-based, not `smsId`-based**: `apply_consent` writes only when a flag actually
changes, so a duplicate delivery is a no-op that writes no second audit row. There is deliberately no
idempotency table because there is deliberately no stored message — no body, no sender, no reply. The
number is used to find a contract and is not persisted by this path.
`sms_inbound_total{outcome}` splits by outcome because the figure an operator needs is `unmatched`
(the opt-outs we did not honour), not the opt-out count.

**What this deliberately does not do.** No live gateway has ever posted here: every test signs its own
body with a fake secret. `blind_index_or_503` resolves as a dependency *before* the handler, so a
deployment that sends SMS without an index key answers `503` where an unconfigured one answers `404`
— pinned by `test_without_a_gateway_the_endpoint_does_not_exist`, whose docstring says so instead of
pretending the two cases are indistinguishable. Only an opt-out keyword is understood: no `START`, no
`HELP`, no free text, no delivery receipts — a customer who texts anything else is answered and
forgotten. Replay is bounded by state and not by count, so nothing limits how many signed requests a
holder of the gateway secret can make, and the 8 KiB body cap is the only size gate. Rotating
`CONTACT_BLIND_INDEX_KEY` has no supported story beyond "backfill fills NULLs": a real rotation needs
a new column or a rewrite pass, and until then the key is permanent-of-record.
`backfill-blind-index` has never run against a deployment's data — only SQLite, in
`test_blind_index_backfill.py`.

**Gates.** New: `test_blind_index.py` (15 — determinism, a pinned golden digest, key separation,
normalisation, contact-only indexing, the four settings refusals), `test_blind_index_backfill.py` (16
— digests that round-trip through the real lookup, dry run writing nothing, re-run examining nothing,
unreadable rows reported rather than guessed, blank key refused, the limit bounding a run, and a
changed key leaving existing digests alone), `test_inbound.py` (16 — a signed STOP suppressing consent
with its audit row, formatting and punctuation tolerance, forged signature, tampered body, unsigned
delivery, a non-ASCII signature refused rather than crashing, malformed payload, a non-keyword
ignored, `202` parity, an unindexed row proving the backfill is load-bearing, cross-tenant
suppression on both tenants, replay under a different `smsId` writing one audit row, `404` with no
gateway, metric deltas). `test_sms_sender.py` grew to 32 tests: the refusal `D10-92` called a
placeholder turned out to name a real property, so it was renamed, narrowed to the pairing it
actually describes, and paired with a test proving that a deployment which both sends and listens
boots outside development. Full suite **1042 tests: 1027 passed, 15 skipped, 0 failed**; `mypy app`
-> no issues in 71 files; `ruff check` and `ruff format --check` -> clean over 130 files.

### D10-94 — Extraction quality gets a number, and the number is a regression net

`EXTRACTION_REVIEW_THRESHOLD` (0.8) is the most consequential constant in the system and had
never been measured: a value at or above it may skip human review and reach a customer, and a
value below it is a hard stop. It was chosen by reading the confidences in `extractors.py` and
reasoning about them. P2-B's acceptance criterion — F1 ≥ 0.85 — was likewise a sentence in the
spec with nothing behind it, so a regex change could make extraction worse and leave every gate
green.

**The corpus.** Eight documents in `tests/golden/extraction/documents/` plus a `manifest.json`
whose `expected` is the value the pipeline should *store*, not every string of that shape.
Utility notice 06 contains `billing@wessexwater.example.test` — the water company's own address
— so its entry states no `email`, and predicting one is a false positive. The README says this
and a test pins it: copying every email-shaped string into `expected` would score a correct
extractor as wrong and reward a greedy regex.

**The score is what would be stored.** `score_corpus` applies `best_per_field` (one candidate
per field, the most confident) and `normalize_value` before comparing, so `24/06/2025` and an
expectation of `2025-06-24` are one value because `normalize_value` says so. That required
making `_best_per_field` public as `best_per_field`: scoring every regex emission rather than
the row the write path would keep measures a pipeline that does not exist. A candidate no
normalizer accepts is counted as `discarded` rather than as a miss, so a corpus of unusable text
is visible instead of reading as zero recall. The headline is a **micro** average over pooled
counts, not the mean of per-field F1s: `email` is stated by six documents and `address` by three,
and a macro average would let the second weigh as much as the first.

**The corpus is refused rather than half-read.** A malformed manifest, an unknown field name, a
blank expectation, a duplicate document name, an empty document, and a `file` that escapes the
corpus directory — including via a symlink, which resolving before the containment check is what
catches — all raise `EvaluationRefusedError` at load, and no message repeats the value. An
expectation that could never match anything would depress the score forever: a number that looks
like a measurement and is not one.

**It found real bugs on its first run.** F1 was **0.841**, under the gate: `start_date` recall
0.625, `end_date` 0.75, `contract_value` 7/8. The cause was label coverage, not logic — `starts
on …` and `commences` were not date label forms, and a lease's `Annual rent:` was not a value
label. Fixed in `extractors.py` with deliberately **anchored** forms (`end(?:s|ing|ed)`, never
`end\w*`, so "endorsement" does not become an end-date). F1 is now **0.919** over 34 matched /
5 missed / 1 wrong, and `unreachable` names `address` (3) and `dob` (1) — gap 17 as a number
instead of a note.

Two results are pinned rather than fixed, and both are deliberate. `03/04/2026 (3 April 2026)`
is legible to a person and ambiguous to a locale-blind regex, so it is dropped and the test says
so; and the water company's email is counted as the false positive it is.

**The gate.** `python -m app.cli evaluate-extraction --golden … --min-f1 …` reads no database,
writes nothing, and exits `0` at or above the floor, `7` below it (`EXIT_BELOW_GATE`, its own
code so a script can tell "release blocked" from "attention needed"), and `1` for a corpus it
cannot use or a backend that cannot answer — "the model is down" must not read as "this
extractor scores 0.4".

**What this deliberately does not do.** The corpus was written by the same hand as the regexes
it scores, so a green gate is **weak evidence about the real world** and strong evidence about
regressions, which is what it is for: 0.919 says extraction did not get worse, never that it is
92 % right on a customer's contracts. No LLM backend has been measured — gap 17 stands, so
`EXTRACTION_BACKEND=pattern` is still the only backend scored and `OllamaExtractor` has met only
canned payloads. Confidence **calibration** is untouched: this measures whether the extractor
finds the right value, not whether a 0.8-confidence value is right 80 % of the time, so the
threshold itself remains unvalidated. Eight documents is small — one document moves F1 by about
0.02 — and `.txt` only: no PDF flows through the scoring path. ~~`.github/workflows/ci.yml` does
**not** run the command, so the gate is one a build chooses to enforce rather than one that
already blocks a merge.~~ **Corrected in place, because that sentence contradicted the next
paragraph's own "a regression reddens the tests as well as the command":** the only thing it got
right is that no workflow *names the command*. The gate itself does run in CI. `ci.yml`'s
`backend tests` job runs `python -m pytest -m "not integration"`, which collects
`test_the_committed_corpus_meets_the_gate`, and that test asserts `measured.meets(DEFAULT_MIN_F1)`
over the committed corpus with `PatternExtractor`. The test and the CLI's `--min-f1` default read
the *same* `DEFAULT_MIN_F1` constant, so the floor cannot drift between the two routes, and a
regex regression fails the suite CI already runs. Its *conclusion* was wrong for a second reason:
"blocks a merge" is a property of branch protection, not of a YAML step. Per `D10-75`, `main`
reported `"protected": false` with required checks `"off"`, so no job in this repository blocks a
merge — extraction gate or otherwise — and editing the workflow would not change that. What
survives as the honest residue: CI never produces the per-field report or exit `7`, so the
readable form of the verdict exists only for whoever runs the command, and only
`EXTRACTION_BACKEND=pattern` is ever scored, because the test constructs `PatternExtractor`
directly rather than `build_extractor(settings)` — `ollama` stays unmeasured (gap 17).

**Gates.** New: `backend/tests/test_extraction_eval.py` (28 — the precision/recall/F1 arithmetic
including a false positive, a never-produced field, and a pooled-vs-averaged case where
`8/9 > 0.8`; every load refusal with no PII echoed; the loop and gate boundaries; and the three
CLI exit codes plus an assertion that the command opens no session). The corpus is scored inside
that suite too, so a regression reddens the tests as well as the command. Full suite **1070
tests: 1055 passed, 15 skipped, 0 failed** (was 1042/1027); `mypy app` -> no issues in 72 files;
`ruff check` and `ruff format --check` -> clean over 133 files.
### D10-95 — CI caught the two gates a green local run does not cover

The five commits pushed as `a8add6c..ed6bd4a` were green locally: 1070 tests (1055 passed, 15
skipped, 0 failed) counted from the JUnit XML rather than the truncated console summary,
`mypy` clean over 72 files, `ruff check` and `ruff format --check` clean over 133 files. The
first CI run on that push (`43216315105`) was **red**: four jobs passed — demo frontend,
frontend, python static, and the MySQL 8.4 integration job — and two failed, `backend tests
(pytest, sqlite)` at the `smoke journey` step and `hygiene + secret scan` at the `gitleaks`
step. The `CI gate` job that failed as well is a rollup, not a third cause.

**The smoke journey was broken by `D10-93`, correctly.** `smoke_journey.py` is a script, not a
test module, so `python -m pytest` neither collects nor proves anything about it — the suite can
be fully green while the thing `docker compose up` is supposed to serve is broken. `cli extract`
now refuses to run without `CONTACT_BLIND_INDEX_KEY` rather than write contact values no inbound
`STOP` could ever find, and the journey's `build_settings` supplied no such key. The refusal was
right; the journey was an operator configuration that no longer exists. Fixed by generating an
index key beside the already-generated Fernet key (`secrets.token_urlsafe(32)`), so no literal
is introduced and nothing has to be stored, and the journey now drives the new path end to end
on a real socket: extraction writes digests, and the review step recomputes one. 108 checks
pass.

**The secret scan had never run locally, and the log that said otherwise was true.** The
`gitleaks` hook is `stages: [manual]`; `pre-commit run --all-files` skips manual-stage hooks, so
the previously recorded "19 hooks: 18 passed, 1 skipped" was accurate and silently incomplete.
Only `--hook-stage manual`, which is what `ci.yml` calls, executes it. Two findings, both in
`e0857f5`, both `generic-api-key` in `backend/tests/test_blind_index.py`: line 35 (a
32-character hex blind-index key) and line 126 (a Fernet key). Neither is a credential — the
tests need *a* key of the documented shape, never that key — but a scanner cannot know that from
entropy, and a scanner taught to ignore a test directory stops being a gate. Fixed the way the
rest of the suite already does it: the blind-index fixture became deliberately low-entropy
descriptive text (the `test_document_text.py` / `test_review.py` convention) and the Fernet key
is generated. The pinned golden digest moved `c4aa08…3245` -> `cc9236…e317` because the *key*
changed, not the construction, and the test says so.

**The values are still in published history, and that was accepted deliberately.** Removing
them from `HEAD` cannot remove them from `e0857f5`, and rewriting a pushed commit would
invalidate every `Landed as` SHA this ledger cites. So the two findings are accepted by
fingerprint in a new `.gitleaksignore`: one commit, one file, one rule, two lines, with the
reason written beside them and an instruction not to add entries to quiet a new finding.
`gitleaks detect` over 197 commits now reports no leaks, and `pre-commit run gitleaks
--all-files --hook-stage manual` passes.

**Why this is worth a record rather than a fix commit.** The design was not wrong; the
definition of "gates green" was. Two of the checks CI runs were absent from the local set — one
because pytest does not collect a script, one because a manual-stage hook is skipped by the
default invocation — and both absences were invisible in a green summary. The correction is a
practice, not a patch: `smoke_journey.py` is part of "the gates" for any change to the CLI, the
settings or the review path, and `gitleaks` is run with `--hook-stage manual` before any commit
that touches keys, fixtures or configuration.

**Gates.** `smoke_journey.py` -> 108 checks passed; `gitleaks` (image `v8.18.4`, the hook's
own) -> no leaks over 197 commits; full suite -> **1070 tests, 1055 passed, 15 skipped, 0
failed**; `mypy app` -> no issues in 72 files; `ruff check` / `ruff format --check` -> clean over
133 files.

**Limits.** The `gitleaks` hook needs Docker, so it cannot run where Docker is not installed —
the same limit `D1-03` records for the hook itself. The `.gitleaksignore` fingerprints name a
line in a commit that will never change, so they cannot rot into a false pass; if they ever stop
matching they are redundant, not evidence of a clean scan. Nothing here revalidated the rest of
CI: the four green jobs were green on the previous commit, and this slice changed no frontend,
no migration and no MySQL-touching code.
