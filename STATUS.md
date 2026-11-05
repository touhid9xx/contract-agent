# Status

Last updated: Phase 1 (real foundation), GATE 2 signed 2026-11-04 — **F1
committed (`f0e1eef`), F2 committed
(`331a7b4`) plus one follow-up fix to the MySQL test harness (`a79d893`), F3
committed (`8e6b927`) plus the bootstrap-path doc (`908f50d`), F4 committed
(`b425ea7`), F5 committed (`21daffc`), F6 committed (`0c2d734`), F7
committed (`90fd0e5`), F8 committed (`8c5e09f`), F9 committed (`50dd0cd`), and F10
committed (`b344c17`), and `chore(hooks)` committed (`4a9e8e9`) — all six steps of the
pipeline slice landed (ingestion, the first real outbox producer, field extraction,
human review, channel selection, notification dispatch). The hooks commit closes gap
13: root `node_modules` was installed, `prepare` → `husky` set `core.hooksPath`, and
`4a9e8e9` is **the first commit in this project that git itself gated** — every
earlier one was gated by a hand-run `pre-commit run --all-files` and nothing more.
F10 closes the last structural gap in the arrow chain but, as committed,
**did not contact anybody**: it shipped a row, a state machine and a contact policy,
and no transport. **That is no longer true of the tree.** An SMTP adapter now
delivers when `NOTIFICATION_TRANSPORT=smtp` is configured (D10-09), so `sent: true` is
reachable through the API for the first time — and still unreachable in the shipped
default, where the stand-in refuses. Since `b344c17` a run of gap-closing slices has
landed. `0f5da36`
(`fix(f10): lock agent decisions and notifications against lost writes`, migration
`0003`) closed gaps 19 and 21 by giving `AgentDecision` and `Notification` a
`version` column and taking the notification row under `SELECT ... FOR UPDATE`
*before* the dispatch policy runs. `4e7dd5c`
(`feat(f10): schedule due and deferred notifications`) closed gap 22: a
`NotificationScheduler` now claims and dispatches due rows — including ones a
quiet-hours deferral wrote into the future — so `scheduled_at` is finally read by
something. `c90423b` (`feat(f8): page the review queue by cursor`) closed gap 16:
the review queue is now paged by keyset, so a backlog larger than the 500-row cap
is reachable rather than truncated — and building it exposed a `NUMERIC(4, 3)`
precision defect that had to be fixed for the page boundary to be honest (D8-06).
The MySQL slice (`c99c792`) then **ran the half that had never run**: the
`integration` suite, `alembic check` and `0003` against a real MySQL 8.4 server, and
two concurrency tests that close gap 23 by proving the dispatch lock rather than
arguing for it (D10-08).
The pipeline is now fully locked, self-draining and pageable, and it **sends** —
`6f4cb0b` implements the transport decision this slice ended on, and the socket suite
that followed proves the client speaks SMTP to a real server rather than only to a
fake one. The shipped default still refuses (`NOTIFICATION_TRANSPORT=none`), which is
the fail-closed choice rather than an unfinished one. The slice after the sign-off is
`feat(g19)`, migration `0004`, and it closes gap 19: **operator override** and
**flag-review** routes now exist on `/agent/decisions`, addressed by contract id,
taking `version` and writing an `AuditLog` row for every correction (D9-06). An
override re-runs the consent and opt-out gates against the contract's state *now*
and chooses only *among* permitted channels, so it redirects a channel and never
creates permission. **GATE 2 was signed on 2026-11-04 at `5c3e077`** — see *GATE 2
— signed* below. That sign-off is unchanged by the later slice; what moved is the
migration head, from `0003` to `0004`.

## Phase history

| Phase | State |
| --- | --- |
| Phase 0 — mocked demo | ✅ built, verified |
| GATE 1 — human visual approval | ✅ **approved** ("I'm happy with demo functions and look") |
| Phase 1 — real foundation | ✅ complete (F1 ✅, F2 ✅, F3 ✅, F4 ✅, F5 ✅, F6 ✅, F7 ✅, F8 ✅, F9 ✅, F10 ✅) |
| GATE 2 — foundation verification | ✅ **signed 2026-11-04 at `5c3e077`**; report below, with 9 items carried forward as open rather than signed |

## F1 — Repo & infra ✅

| Area | Detail |
| --- | --- |
| Python tooling | `pyproject.toml` — ruff (lint + format), `mypy --strict`, pytest + coverage, bandit |
| Pre-commit | `.pre-commit-config.yaml` — hygiene, ruff, ruff-format, mypy, bandit, frontend typecheck, frontend eslint; gitleaks bound to `manual` (D1-03) |
| Commit gate | husky `pre-commit` + `commit-msg`, commitlint (Conventional Commits), lint-staged — **written in F1 but not actually wired until `4a9e8e9`** (gap 13): the `prepare` hook never ran because root `node_modules` was absent |
| Backend | `app/core/config.py` fail-fast settings, `app/core/logging.py` structlog + PII redaction, `app/core/middleware.py` request/trace IDs, `app/main.py` factory + error envelope, `/api/v1/health` |
| Frontend | Next.js 16 + Tailwind 4 + TanStack Query + Zustand + next-themes + next-intl (en, bn), ESLint flat config, Prettier, `[locale]` routing |
| Scripts | `scripts/bootstrap.mjs` (one-command setup), `scripts/run-frontend-check.mjs` |
| Env | `.env.example` documents every setting read by `Settings` |

## F2 — MySQL 8 + SQLAlchemy 2.0 + Alembic + outbox relay ✅

| Area | Detail |
| --- | --- |
| Schema | `backend/app/db/base.py` (declarative base, deterministic constraint names, `TimestampMixin`, `TenantMixin`), `types.py` (portable `GUID` → `CHAR(36)`), `enums.py`, `session.py` (`build_engine` / `build_session_factory` / `session_scope`, pool args skipped for SQLite) |
| Migrations | `backend/alembic.ini` (no hard-coded URL), `backend/migrations/env.py` (explicit config URL → `DATABASE_URL` → `Settings`, `compare_type` on, batch mode for SQLite), `versions/0001_initial_schema.py` — baseline for all ten tables |
| Models | `tenant.py` (`Tenant`, `User`), `contract.py` (`Contract`, `ExtractedField`), `delivery.py` (`AgentDecision`, `Notification`, `CustomerResponse`), `escalation.py` (`Escalation`, `AuditLog`), `outbox.py` (`OutboxEvent`) |
| Repositories | `TenantScopedRepository` — refuses construction without a tenant; `get()` returns `None` for another tenant's id (D2-02). `OutboxRepository` — lease-fenced `claim_due` / `record_outcome`, bounded exponential backoff with ±20 % jitter, error truncation |
| Relay | `backend/app/workers/outbox_relay.py` — transport-agnostic `EventPublisher`; `UnavailablePublisher` **raises** rather than no-ops so nothing is silently log-and-dropped; outcome recorded in its own transaction (at-least-once); SIGINT/SIGTERM graceful stop |
| Compose | `docker-compose.yml` — MySQL 8.4 (loopback-bound), `api`, and a separate `relay` service on the same image so publishing survives API restarts |

## F3 — Authentication & tenant binding ✅

Closes the F2 gap where tenant scoping was "a well-tested contract without a
producer". `tenant_id` now originates in exactly one place: a verified bearer
token confirmed against the database.

| Area | Detail |
| --- | --- |
| Security primitives | `app/core/security.py` — Argon2id hash/verify/rehash (parameters read from the *stored* hash, so raising the cost never locks anyone out), HS256 issue/verify with a one-algorithm allow-list, `TokenExpiredError` / `InvalidTokenError`, and `spend_password_hash_time` so login is not an enumeration oracle (D3-01, D3-06) |
| Principal | `app/core/principal.py` — frozen `Principal`; the only validated carrier of `tenant_id` downstream |
| Dependencies | `app/api/deps.py` — request-scoped session that does **not** commit implicitly; `get_current_principal` with five ordered checks (token → signature/expiry → row exists → tenant matches → active → `token_epoch`); `require_roles` returning 401 vs 403 correctly (D3-03) |
| Repositories | `app/db/repositories/users.py` — `UserRepository` (tenant-scoped) and `IdentityDirectory`, the single audited read-only exception for pre-authentication lookups (D3-04) |
| Routes | `app/api/v1/auth.py` — `POST /auth/login`, `GET /auth/me`, `GET\|POST /auth/users` (admin-only, tenant from principal, 409 on duplicate). Mounted under `settings.api_v1_prefix` so `/api/v1` — the prefix `frontend/src/lib/api.ts` already assumes — stays true |
| Config | `app/core/config.py` — `argon2_*` bounds and `access_token_expire_minutes` (15, bounded 1–120); app factory takes an injectable `session_factory` so tests share one in-memory DB |

### Verification actually run (F3)

| Check | Command | Result |
| --- | --- | --- |
| Full suite, no MySQL | `pytest backend/tests` with `MYSQL_TEST_URL` unset | **137 passed, 3 skipped** |
| Full suite, real MySQL 8.4 | `pytest backend/tests` with `MYSQL_TEST_URL` on `:3307` | **140 passed** |
| Type check | `mypy app` (strict) | **clean, 30 source files** |
| Lint / format | `ruff check backend` | **All checks passed** |
| Security lint | `bandit -c pyproject.toml -r backend/app` | **0 issues** |

The one warning in the run is `starlette/testclient.py` deprecating an
`anyio.abc.BlockingPortal` alias — third-party, not ours.

#### Two defects found by the tests, not by reasoning

1. **A corrupted hash produced a 500 naming the account.** `verify_password`
   caught only `Argon2Error`, but `argon2.exceptions.InvalidHashError` subclasses
   `ValueError`. Any truncated or corrupted hash therefore raised instead of
   reading as a wrong password — the exact leak the module promised to prevent.
   Both types are now caught, in `verify_password` and `password_needs_rehash`.
2. **A startup guard that could never fire.** The
   `argon2_memory_cost < 8 * parallelism` check was unreachable, because the
   memory floor (8192) dwarfs `8 × 16`. Dead validation is worse than none: it
   reads as protection. Removed, and replaced with a test asserting every
   *permitted* shape satisfies the arithmetic — so a future bound widening fails
   in CI instead of silently.

#### Creating the first account (manual testing)

There is **no bootstrap endpoint or CLI yet** — see gap 10. `POST /auth/users`
requires an admin token, which requires a user, so a fresh database cannot be
logged into at all without one manual step. Until a seed command exists, create
the first tenant and admin directly:

```powershell
venv\Scripts\python.exe -c @"
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.enums import UserRole
from app.db.models import Tenant, User
from app.db.session import build_engine, build_session_factory

settings = get_settings()
session = build_session_factory(build_engine(settings))()
tenant = Tenant(name='Acme', slug='acme')
session.add(tenant)
session.flush()
session.add(User(tenant_id=tenant.id, email='admin@acme.test', role=UserRole.ADMIN,
                 password_hash=hash_password('correct-horse-battery', settings)))
session.commit()
print('created tenant', tenant.slug)
"@
```

(Run from `backend/` with `DATABASE_URL` set, after `alembic upgrade head`. This
exact snippet was executed against a real MySQL 8.4 schema before being written
here, not transcribed from memory. `Tenant`/`User` take the keyword arguments
shown — verified against `backend/app/db/models/tenant.py`.)

## F4 — Event transport ✅

Closes D2-04: a real transport now exists, and the relay still does not know its
name. **Scope was taken from the repository, not invented** — `DECISIONS.md`
D2-04 and the `outbox_relay.py` docstring both name F4 as "the real
transport/publisher" milestone, and `docker-compose.yml` said Kafka would arrive
with F4. That comment has since been corrected: the transport exists, but the
stack still defines **no broker service**, so the relay keeps the `none`
transport unless `.env` says otherwise. The ingestion → extraction → review
pipeline is therefore **not** part of this slice.

| Area | Detail |
| --- | --- |
| Publisher | `app/workers/publishers.py` — `KafkaPublisher` calls `send()` and then **blocks on the broker ack**; errors are not caught, so "returned normally" really does mean "delivered" (D4-01) |
| Wire format | `encode_envelope` (sorted keys, compact separators, `default=str`, `occurred_at` through the existing `as_utc`) and `encode_headers` (bytes). Identity travels in body **and** headers; partition key stays the contract id (D4-02) |
| Producer | `create_kafka_producer` — `acks="all"`, `retries` from settings, `linger_ms=0`. `kafka` imported **inside** the function, so `EVENT_TRANSPORT=none` and every offline test never import it (D4-04) |
| Selection | `build_publisher(settings)` — `kafka` → `KafkaPublisher`, anything else → the existing refusing `UnavailablePublisher` (D4-03) |
| Relay | `main()` in `outbox_relay.py` now builds a transport (function-local import, avoids the cycle) and logs `outbox_relay_transport` at start-up so an operator can see whether a tick delivers or parks rows |
| Config | `event_transport` (`none`\|`kafka`, default `none`), `kafka_client_id`, `kafka_publish_timeout_seconds` (10), `kafka_produce_retries` (3). Two **reachable** guards: empty `KAFKA_BOOTSTRAP_SERVERS` with `kafka`, and publish timeout ≥ lease, which would let a second relay republish mid-ack (D4-05) |
| Deps | `kafka-python==2.2.15` — pure Python, resolves on CPython 3.14; `pyproject.toml` gives `kafka`/`kafka.*` an `ignore_missing_imports` override because it ships no `py.typed` |
| Tests | `backend/tests/test_publishers.py` — encoding determinism, UTC-aware envelope, byte headers, partition key, ack wait, error propagation, factory selection, both config guards, and relay-behaviour (marked published on ack; left pending on broker outage). One opt-in `live` test needs `KAFKA_LIVE=1` |

### Verification state (F4) — read this before GATE 2

| Check | Command | Result |
| --- | --- | --- |
| Full suite, real MySQL | `pytest backend/tests` with `MYSQL_TEST_URL` set | **154 passed, 1 skipped**, exit 0 |
| Full suite, no MySQL | `pytest backend/tests` with `MYSQL_TEST_URL` unset | **151 passed, 4 skipped**, exit 0 |
| MySQL integration only | `pytest backend/tests -m integration` | **9 passed** (6 harness + 3 real MySQL) |
| Kafka transport only | `pytest backend/tests/test_publishers.py` | **14 passed, 1 skipped** (the `live` broker test) |
| Pre-commit gate | `pre-commit run --all-files` | **all 16 hooks passed** — hygiene, ruff, ruff format, mypy strict, bandit, frontend `tsc --noEmit`, frontend eslint |
| Commit | `git commit -F .agent-cache/f4-commit-msg.txt` | **`b425ea7`**, no `--no-verify` — but the husky hooks did **not** run (gap 13; corrected, closed in `4a9e8e9`) |

Every count above is read from a JUnit report rather than from the console tail,
which pytest does not reliably flush to a redirected stream here. Both suite runs
report `tests="155" failures="0" errors="0"`; only `skipped` differs, and the
difference is exactly the three MySQL integration tests:

- **No `MYSQL_TEST_URL`** → `skipped="4"`: three integration tests plus the
  opt-in `live` broker test.
- **`MYSQL_TEST_URL` pointing at the loopback MySQL 8.4 container** →
  `skipped="1"`: the `live` broker test only, because no Kafka broker exists to
  talk to (see the gap below). The remaining skip is the deliberate one.

So the suite is 155 tests; the steady state is "everything runs except the one
test that would need a broker".

For the record, earlier drafts of this section said the gate could not be run at
all: the agent's shell tool was timing out on every command, so F4 was left
uncommitted rather than pushed through with `--no-verify`. That block proved
intermittent — it cleared mid-session, and the results above were then produced
normally. Nothing here is claimed as verified that was not actually executed.

### Static cross-checks made before the first run

The likeliest way the F4 tests would fail on their first run is a call that does
not match the signature it calls. Each one was therefore cross-checked against
its definition by reading the code:

| Call made by `test_publishers.py` | What it must match | Verified at |
| --- | --- | --- |
| `OutboxRelay(factory, publisher, *, batch_size, lease_seconds, max_attempts, poll_interval_seconds, worker_id)` | keyword-only, exactly these names | `outbox_relay.py:82-91` |
| `relay.run_once()` → `int` | returns the count actually published | `outbox_relay.py:120-121` |
| `Settings.kafka_servers` | property → `list[str]`, blank entries dropped | `config.py:210-212` |
| `event_transport`, `kafka_client_id`, `kafka_publish_timeout_seconds`, `kafka_produce_retries` | fields exist, defaults `none` / relay id / `10.0` / `3` | `config.py:83-89` |
| `as_utc(created_at)` → aware UTC | naive → `replace(tzinfo=UTC)`, so `isoformat()` ends `+00:00` | `base.py:55-61` |
| `enqueue_event()` defaults | `event_type`/`topic` `contract.ingested`, key `1042`, payload `{"contract_id": 1042}` | `conftest.py:222-234` |
| `row.status is OutboxStatus.PUBLISHED` | `Enum` column, so member identity holds | `models/outbox.py:76-78` |
| `assert published == 1` | `db_engine` is a **fresh** in-memory DB per test | `conftest.py:126-147` |

The last row is the one that would silently break those assertions: on a shared
database, rows enqueued by another test would be claimed by the same tick. They
are not shared, so the count assertions are sound.

These were static checks, made while execution was unavailable. When the gate
finally ran, every cross-check above held on the first attempt — no signature
mismatch, no shared-state assertion failure. That is a good argument for doing
the same on F5, but it was never a substitute for running the suite.

### Open at GATE 2 — three caveats to state, not bury

`docker-compose.yml` has **no Kafka service**, and the `live` test is opt-in, so
the wire path has only ever met a fake producer. What the tests prove is the
*contract* — right key, right headers, right bytes, ack waited on, errors
propagated — not that a real Kafka 4.x broker accepts it. Three things are
therefore **not** covered by a green board, and each must be said out loud when
GATE 2 is signed:

1. **No broker has ever been on the other end.** `KafkaPublisher` has only ever
   been exercised against `FakeProducer` in `test_publishers.py`. Adding a broker
   service to compose and running `KAFKA_LIVE=1` is the first thing that would
   turn this from a contract test into an end-to-end one.
2. **No consumer exists.** F4 owns producing only; the downstream side of
   at-least-once delivery has no implementation in this repository yet.
3. **No DLQ replay tooling.** Rows that exhaust `max_attempts` are parked in
   dead-letter with their error text, and nothing un-parks them. The relay
   deliberately never marks them published.

None of the three is a defect: each is a slice boundary of F4, and each is
visible in the code and in the relay's own logs rather than hidden. The caveats
are about what has *not been built*, not about anything that is built and broken.

> **Status after F5:** caveat 3 is now closed — `python -m app.cli outbox-replay`
> requeues parked rows (D5-03). Caveats 1 and 2 still stand: still no broker
> service, still no consumer.

## F5 — Readiness + dead-letter replay ✅

Closes gaps 4 and 5. Neither is a new feature so much as making two things that
already existed observable and recoverable: a process that could not be asked
whether it could serve, and parked rows nothing could un-park.

| Area | Detail |
| --- | --- |
| Readiness core | `app/core/readiness.py` — `check_database` (`SELECT 1` via the real session factory), `create_broker_probe` (lazy `KafkaAdminClient.list_topics`, bounded by `request_timeout_ms` so an abandoned thread cannot accumulate per scrape), `check_event_transport`, `readiness_report`. Coarse reasons only; driver text goes to the redacting log (D5-02) |
| Endpoint | `app/api/v1/health.py` — `GET /health/ready` → 200 `{"status": "ready", "checks": {...}}` or 503 `{"status": "not_ready", ...}`. `/health` untouched: liveness still touches no dependency (D5-01) |
| Operator CLI | `app/cli.py` — `python -m app.cli outbox-replay [--list \| --dry-run \| --ids N,N]`, exit `EXIT_OK` / `EXIT_NO_MATCH`, structlog (redacted) output. Injectable settings/session factory mirroring `create_app` |
| Replay repo | `app/db/repositories/outbox.py` — `dead_lettered`, `replay_dead_lettered`, `DeadLetterReplay`, `DEFAULT_REPLAY_LIMIT`. Module-level (not `OutboxRepository` methods): replay has no worker identity and no lease. Only `failed` rows are touched; `attempts` resets; `next_attempt_at` floored to the stored second (the D2-06 lesson) |
| Config | `app/core/config.py` — `readiness_timeout_seconds` (`gt=0, le=30`, default 5); `.env.example` documents it |
| Compose | `docker-compose.yml` — header comment corrected (F5 landed, Redis/Ollama still unused). The `api` healthcheck **stays on `/health`** with a comment saying why: it drives `restart: unless-stopped`, so readiness there would restart a healthy API whenever MySQL blips |
| Tests | `backend/tests/test_readiness.py` (15 cases), `backend/tests/test_outbox_replay.py` (13 cases) |

### Verification actually run (F5)

| Check | Command | Result |
| --- | --- | --- |
| Full suite, no MySQL | `pytest` with `MYSQL_TEST_URL` unset | **179 passed, 4 skipped** (JUnit: `tests=183 failures=0 errors=0 skipped=4`) |
| Full suite, real MySQL 8.4 | `pytest` with `MYSQL_TEST_URL` on `:3307` | **182 passed, 1 skipped** (JUnit: `tests=183 failures=0 errors=0 skipped=1`) |
| Type check | `mypy app` (strict) | **clean, 33 source files** |
| Lint | `ruff check backend` | **All checks passed** |
| Format | `ruff format backend` | **stable** (2 files reformatted, then clean) |
| Security lint | `bandit -c pyproject.toml -r backend/app` | **0 issues** |

The single skip in the MySQL run is the opt-in `live` Kafka test (`KAFKA_LIVE=1`),
unchanged from F4. The four skips without MySQL are the three real-MySQL tests
plus that same `live` test.

#### Two gate failures caught before the commit, not by reasoning

1. **`ruff ASYNC109` on `_guarded`.** Naming a parameter `timeout` on an `async`
   function is flagged because a timeout is easy to forget to enforce. Renamed to
   `limit_seconds`; both call sites pass positionally, so nothing else moved.
2. **`mypy` was invoked against the wrong paths.** `mypy app agents tools migrations`
   aborted with `Duplicate module named "__main__"` (both `agents` and `tools`
   carry one). The project's gate is `mypy app` — that passes; the error was in
   the command, not the code.

#### What the readiness tests do and do not prove

`test_readiness.py` injects an `UnreachableDatabase` and a fake broker probe, so
it proves the *verdict logic*: fail-closed on a dead dependency, `timeout` under a
wedged one, the 503 body shape, `not_configured` omission, and that no driver text
reaches the response. It does **not** prove a real broker or a real outage behaves
the same — same caveat as F4's, for the same reason (no broker service).

## F6 — Contract ingestion + first real outbox producer ✅

Steps 1–2 of the pipeline slice. Nothing in Phase 1 had ever enqueued an event
from a business action before this: every prior `enqueue_event` call lived in a
test fixture. A contract row and its `contract.ingested` event now commit in the
same transaction, so the relay has a producer whose events are real by
construction.

| Area | Detail |
| --- | --- |
| Endpoint | `app/api/v1/contracts.py` — `POST /api/v1/contracts`, admin/operator only (`viewer` → 403 and nothing written). `tenant_id` comes from the principal; a `tenant_id` in the body is impossible because the schema has no such field (D6-01) |
| Repository | `app/db/repositories/contracts.py` — `ContractRepository(TenantScopedRepository[Contract])` with `by_sha256` (per-tenant idempotency lookup) and `create` (adds without flushing; transaction control stays with the route). Deliberately does not accept `status` or `risk`, so ingestion cannot pre-set agent-computed state |
| Atomic write | `enqueue_event(..., event_type="contract.ingested", topic="contract.events", partition_key=str(contract.id))` runs on the request session, then `flush()` → `commit()`. Row and event land together or not at all; `IntegrityError` → rollback → 409, so a lost race leaves no orphan event (D6-02, D6-04) |
| Fingerprint | SHA-256 over the canonicalised content fields, unique per tenant via `uq_contracts_tenant_sha256`. Key order does not change the digest; the same content in another tenant is a separate contract (D6-04) |
| Validation | `customer_ref` must be non-blank; timezones checked by IANA *shape*, not `ZoneInfo` — `tzdata` is absent on this runtime and even `ZoneInfo("UTC")` raises (D6-03). Inverted `start_date`/`end_date` → 422 |
| Correlation | `X-Request-ID` is carried into the outbox row's `correlation_id`, so an ingested contract and its event are traceable to one request |
| Wire | `app/main.py` mounts the router under `api_v1_prefix`; `ContractRepository` exported from `app/db/repositories/__init__.py` |
| Tests | `backend/tests/test_contracts.py` (20 cases) — persistence, defaults (`active`/v1), outbox row pending with 0 attempts, `next_attempt_at` flooring, dedupe incl. key-order invariance, the forced-race 409, cross-tenant same-content allowed, body `tenant_id` ignored, 401/403, inverted dates, blank ref, five implausible timezones |

### Verification actually run (F6)

| Check | Command | Result |
| --- | --- | --- |
| Full suite, no MySQL | `pytest tests` (SQLite) | **199 passed, 4 skipped** (JUnit: `tests=203 failures=0 errors=0 skipped=4`) |
| Full suite, real MySQL 8.4 | `pytest tests` with `MYSQL_TEST_URL` on `:3307` | **202 passed, 1 skipped** (JUnit: `tests=203 failures=0 errors=0 skipped=1`) |
| Type check | `mypy app` (strict) | **clean, 35 source files** |
| Lint | `ruff check backend/app backend/tests` | **All checks passed** |
| Format | `ruff format --check` | **51 files already formatted** |
| Security lint | `bandit -c pyproject.toml -r backend/app` | **0 issues** |

The single skip in the MySQL run is the opt-in `live` Kafka test; the extra three
skips without MySQL are the real-MySQL integration tests, unchanged from F5.
Both runs report identical `tests`/`failures`/`errors`; only `skipped` differs,
and the difference is exactly those three tests.

#### What the ingestion tests prove and what they do not

They prove the *transaction boundary*: the contract row and the outbox event
share one commit, and the losing side of a dedupe race leaves no event behind.
They do **not** prove the event ever gets published — that still needs a broker
(gap 5). An ingested contract is also deliberately inert at this milestone: the
route runs no extraction, so nothing reaches a notification from it (F7 adds the
operator-run extractor, and still does not trigger it on ingest).

## F7 — Field extraction + encryption at rest ✅

Step 3 of the pipeline slice, and the milestone where "no low-confidence field
reaches a notification" stops being an intention and becomes code. A contract's
document text now becomes `ExtractedField` rows that carry a status, a confidence
and provenance; the only two statuses extraction can write are `auto_accepted`
and `pending_review`.

| Area | Detail |
| --- | --- |
| Policy module | `app/workers/extraction.py` — `extract_contract(session, contract, text, *, extractor, cipher, review_threshold)`. `cipher` and `extractor` are **required keyword arguments**, so there is no branch that stores a value in the clear and no branch that silently skips encryption. Tenant comes from `contract.tenant_id`, not from a parameter, so a caller cannot write one tenant's fields onto another's contract (D7-01, D7-04) |
| Review gate | `_status_for` maps confidence ≥ `EXTRACTION_REVIEW_THRESHOLD` to `auto_accepted` and everything below to `pending_review`; `approved` is unreachable from this module. Re-runs skip rows in `approved`/`rejected`/`unreadable` and report the count as `skipped_reviewed` instead of overwriting a person's decision (D7-04) |
| Backends | `app/workers/extractors.py` — `PatternExtractor` (default: deterministic offline regexes for 5 of 7 field types; an ambiguous `03/04/2025` is dropped rather than guessed) and `OllamaExtractor` (opt-in: `/api/generate`, `format:"json"`, `temperature:0`, errors never echo the document or the endpoint). `build_extractor` is total over the literal, and the two backends are alternatives, never merged (D7-02, D7-03) |
| Config | `ExtractionBackend = Literal["pattern", "ollama"]` — a cloud backend is not a member, so `EXTRACTION_BACKEND=openai` is a start-up `ValidationError` rather than a code path (D7-02). `EXTRACTION_MODEL`, `_TIMEOUT_SECONDS`, `_REVIEW_THRESHOLD` (0.8, matching what the Phase-0 review UI renders) and `_MAX_DOCUMENT_CHARS` are bounded; a **present but malformed** `ENCRYPTION_KEY` fails at boot, while an empty one stays legal because the API process never stores field values |
| Crypto | `app/core/crypto.py` — `FieldCipher` over Fernet (AES-CBC + HMAC-SHA256, so tampering fails loudly); `build_cipher` raises `MissingEncryptionKeyError` on an empty key; `DecryptionError` never carries the token. `cryptography` is already a runtime dependency (PyMySQL needs it), so the import is unconditional (D7-01) |
| Repository | `app/db/repositories/fields.py` — `ExtractedFieldRepository.for_contract/create/overwrite`; the upsert keeps `uq_extracted_fields_contract_field` intact, and the overwrite *policy* stays in the worker, not here |
| Operator write path | `python -m app.cli extract --contract-id N --text-file P`. Exit codes are a contract: `0` stored something, `1` no such contract, `2` **ran fine and extracted nothing** (`contract_extraction_empty` — gap 11), `3` refused or failed (no key, empty or over-long document, backend down). An over-long document is refused, not truncated, because missing fields are indistinguishable from fields the contract never stated |
| Read route | `GET /api/v1/contracts/{id}/fields` — `ExtractedFieldSummary` carries name/status/confidence/`version`/provenance/review state plus a `blocking` count and **never** `raw_value` or `normalized_value`. Viewer may read; a cross-tenant id is `404`, not `403` (D7-05) |
| Tests | `test_crypto.py` (10), `test_extraction.py` (41, parametrised normalisation included), `test_contract_fields.py` (9). Normalisation per field, per-backend confidences, the review gate (including *extraction cannot write `approved`*), encryption at rest, in-place re-run bumping `version` to 2, reviewer rows never overwritten, `ExtractionBackend` having no cloud member, a malformed `ENCRYPTION_KEY` refused at boot, and the route's non-disclosure. `test_config.py` gained the malformed-key case (17 now) |

### Verification actually run (F7)

| Check | Command | Result |
| --- | --- | --- |
| Full suite, no MySQL | `pytest tests` (SQLite) | **260 passed, 4 skipped** (JUnit: `tests=264 failures=0 errors=0 skipped=4`) |
| Full suite, real MySQL 8.4 | `pytest tests` with `MYSQL_TEST_URL` on `:3307` | **263 passed, 1 skipped** (JUnit: `tests=264 … skipped=1`) |
| Type check | `mypy app` (strict) | **clean, 39 source files** |
| Lint | `ruff check backend/app backend/tests` | **All checks passed** |
| Format | `ruff format --check` | **58 files already formatted** |
| Security lint | `bandit -c pyproject.toml -r backend/app` | **0 issues** |
| Commit gate | `pre-commit run --all-files` | **16/16 hooks passed** |

Counts are up from F6's 199/202 because every `test_extraction.py` parametrisation
counts as a case. The F6→F7 deltas are +61 tests on SQLite and +61 on MySQL; the
skips are unchanged (the three real-MySQL integration tests when no server is
configured; the opt-in `live` Kafka test always).

#### What the extraction tests prove and what they do not

They prove the **policy**: with a fake `DocumentExtractor` and a real key, the
worker cannot write a plaintext value, cannot approve a field, cannot overwrite a
reviewer's decision, and cannot cross a tenant. They do **not** prove a real
Ollama answers anything — `docker-compose.yml` has no Ollama service, so the
`ollama` backend has never been exercised against a live model, and its parsing is
tested against canned payloads. The `pattern` backend, which *is* what runs by
default, is proven end-to-end. No test feeds a real PDF through the pipeline:
there is still no upload path, so "document text" means a text file the operator
supplies.

## F8 — Human review + audited reveal ✅

Step 4, and the milestone F7 was explicitly waiting for. Extraction could write
only `auto_accepted` or `pending_review` (D7-04), which made `pending_review` a
place rows went to stop. This slice adds the other half: the three routes that
turn a pending field into a decision, and the first code in the repository to
write `AuditLog` — a table that had existed since F2 with no writer.

| Area | Detail |
| --- | --- |
| Policy | `app/api/v1/review.py` — `ReviewAction` is a `Literal["accept","reject","mark_unreadable","edit"]`, so an unknown action is a schema `422` rather than a silent no-op. Each action maps to exactly one `FieldStatus`; `accept` and `edit` are the only paths to `approved`, and both are operator-only and audited (D8-05) |
| Queue | `GET /api/v1/review/queue` — lowest confidence first, ties broken by `created_at` then `id` so the order is stable for someone working through it. Bounded at the query (`DEFAULT_REVIEW_QUEUE_LIMIT` 100, `MAX_REVIEW_QUEUE_LIMIT` 500) rather than in the route, so no caller can ask for the whole table. Reports `count`, `oldest_minutes` (a queue that is not draining shows it — but it is a max over *this page*, so only page one is a trustworthy reading) and the `threshold` that put each item there. Paged by `?cursor=` → `next_cursor` (`c90423b`, D8-06), so the whole backlog is reachable; a cursor that does not decode is a `422`, never a silent page one |
| Decision | `PATCH /api/v1/review/{field_id}` — carries the `version` the reviewer was looking at; a mismatch is a `409` whose body contains the **winning row**, so a reviewer sees what changed instead of only that they lost. `StaleDataError` closes the read-to-flush race the version check cannot. Response reports the decided item plus the contract's remaining `blocking` count |
| Correction | `action="edit"` normalizes the replacement for that field's type, encrypts it, and stores it — the same two rules F7 applies to an extracted value. An unparseable correction is a `422` and **writes nothing**, and the error never echoes the value back. The extractor's `confidence` is deliberately left alone (D8-02) |
| Reveal | `POST /api/v1/review/{field_id}/reveal` — the only route in the API that returns a decrypted value, and the only caller of `decrypt_for_review`, which closes gap 14. `POST` because it writes an audit row, so a refresh, prefetch or cache cannot produce a reveal that left no trace (D8-03). `503` when `ENCRYPTION_KEY` is unset, `409` when the field stores no value, `500` on a rotated key without echoing the token |
| Audit writer | `app/db/repositories/audit.py` — `AuditLogRepository.record(...)`, append-only by construction (no update, no delete), tenant stamped from the repository. Snapshots are built by the caller and carry `field_name`/`status`/`version`/`confidence` — **never a value**, because `AuditLog` has no column encryption (D8-04) |
| Repository | `app/db/repositories/fields.py` — `by_id()` (tenant-scoped `get`, so another tenant's id reads as missing), `awaiting_review()` (paged by the `ReviewCursor` keyset, carrying the tenant predicate on **both** sides of the `Contract` join) and the `encode_review_cursor`/`decode_review_cursor` pair, which is strict about what it did not mint |
| Shared shape | `_field_summary` → public `field_summary` in `api/v1/contracts.py`; `ReviewItem` extends the value-free `ExtractedFieldSummary` instead of re-declaring it, so there is one definition of "a field without its PII" for a new route to inherit. `correlation_id_or_new` moved from a private helper in `contracts.py` to `core/middleware.py` because two routers now need it |
| Error envelope | `main.py`'s HTTP-exception handler now fills the `details` slot `error_payload` has had since F1, when a route raises with a `{"message","details"}` dict. The envelope is byte-identical for every existing route — the F6 `409` from ingestion is covered by its own regression test |
| Tests | `test_review.py` (34 at F8, **43 after `c90423b`**): queue ordering/tenant isolation/bounds/limit validation/viewer read/value absence; accept→`approved` with blocking cleared; reject keeps blocking; `mark_unreadable` unblocks; edit normalized and encrypted at rest; stale version→`409` carrying the winning row; one audit row per decision with snapshots that never contain the value; viewer `403` writing nothing; cross-tenant `404`; reveal round-trip with exactly one `field.value_revealed` row; `503` with no key; `409` with no stored value; `500` on a rotated key without leaking the token. The paging slice adds 9 — every row past the cap reaches a reviewer exactly once, a cursor survives an intervening `PATCH`, tied confidence falls through to `created_at`/`id`, the last page returns `next_cursor: None`, four malformed cursors are `422`, and a forged foreign-tenant cursor returns `[]`. `test_models.py` gains 2 pinning the confidence quantiser |

`main.py` also gained the only change in this milestone that touches existing
behaviour: a route may now raise `HTTPException(detail={"message": ..., "details":
...})` to put structured context in the envelope. Every pre-existing raise uses a
plain string and produces the same body as before.

### Verification actually run (F8)

| Check | Command | Result |
| --- | --- | --- |
| Full suite, no MySQL | `pytest tests` (SQLite, `MYSQL_TEST_URL` cleared) | **294 passed, 4 skipped** (JUnit: `tests=298 failures=0 errors=0 skipped=4`) |
| Full suite, real MySQL 8.4 | `pytest tests` with `MYSQL_TEST_URL` on `:3307` | **297 passed, 1 skipped** (JUnit: `tests=298 … skipped=1`) |
| Type check | `mypy app` (strict) | **clean, 41 source files** |
| Lint | `ruff check app tests` | **All checks passed** |
| Format | `ruff format --check app tests` | **61 files already formatted** |
| Security lint | `bandit -c pyproject.toml -r backend/app` | **0 issues** |
| Commit gate | `pre-commit run --all-files` | **16 hooks passed** (17th, `forbid submodules`, not applicable) |

The F7→F8 delta is +34 tests on both databases — the whole of `test_review.py`,
which is why the two runs differ by exactly the three real-MySQL integration tests
and the opt-in `live` Kafka test, exactly as before.

#### What the review tests prove and what they do not

They prove the **rules**: a viewer cannot decide or reveal; a field in another
tenant is indistinguishable from one that does not exist; a stale `version` cannot
overwrite a decision; a correction is normalized and encrypted rather than stored
as typed; every decision and every reveal writes exactly one audit row; and no
audit snapshot, no queue item and no log line ever contains a stored value. They
do **not** prove the workflow is usable — there is no review screen, so these
routes have only ever been called by `TestClient` (gap 15). Nor is anything yet
*consuming* `approved` fields: a field can now become approved and then nothing
happens, because there is no agent-decision or notification slice yet.

## F9 — Agent channel decision + consent ✅

Step 5, and the first arrow after review. F8 made `approved` reachable and then
nothing happened (gap 15): the decision that turns an approved contact value into
a channel did not exist. This slice adds it, and with it the first writer of
`AgentDecision` — a table that had existed since F2 with no route, worker or
service touching it.

It also had to add a schema before it could add a rule. The non-negotiable "no
channel switch that bypasses consent, opt-out or quiet hours" had **nothing in the
database to enforce it against**: no consent flag, no opt-out, no quiet-hours
window. Migration `0002` and the five `contracts` columns exist because of that,
not as a preference.

| Area | Detail |
| --- | --- |
| Consent schema | `Contract.do_not_contact`, `consent_email`, `consent_sms`, `consent_voice`, `consent_updated_at`; `consent_channels` and `may_contact` properties. **All three flags default to `false` in the model and as the migration's `server_default`** — a `true` backfill would grant consent nobody gave (D9-01). Opt-out is a separate column because it is a veto, not a channel choice |
| Migration | `0002_contract_consent.py`, hand-written like `0001`. It is the first revision added to the chain since the baseline, and the integration fixture's `downgrade base` → `upgrade head` now exercises it in both directions on real MySQL |
| Consent route | `PUT /api/v1/contracts/{id}/consent` — full-state replacement (a partial `PATCH` cannot express a withdrawal), carries the `version` the caller read, `409` whose body holds the **current** consent state, one `AuditLog` row when something changed, and a no-op when nothing did. The version check runs **before** the no-op short-circuit, so a stale body is never reported as success |
| Policy | `app/workers/decision.py` — candidate set is `contract.consent_channels` ∩ channels with an approved contact value, then the fixed `CHANNEL_PRIORITY` (email → SMS → voice). `blocks_pipeline` is **read, not re-implemented**: a field awaiting review or rejected is a hard stop, and there is exactly one definition of that rule |
| Refusals | Five stable codes — `CONTRACT_CLOSED`, `PIPELINE_BLOCKED`, `CONTACT_REFUSED`, `NO_CONSENT`, `NO_CONTACT_VALUE` — all `409`, distinguished by `detail.code`, all writing **nothing** and logging at info. Gate order is fixed and tested, because it decides which reason a contract with several problems is told about (D9-04) |
| Provenance | `prompt_version="policy.channel.1"` and `llm_model="deterministic"`: the column set records *what produced the recommendation*, so naming a model for a decision no model made would fabricate the one fact the table exists to carry (D9-02). `needs_review` is always `false` — a deterministic selector cannot be unsure (D9-05) |
| Routes | `POST /api/v1/agent/decisions` (201, appends rather than updates, admin/operator) and `GET /api/v1/agent/decisions` (viewer allowed — no customer value is in the row; cross-tenant `contract_id` returns an empty list rather than confirming an id exists) |
| Repository | `app/db/repositories/agent.py` — `for_contract`, `recent`, `latest_for_contract`, `create`, tenant-stamped from the base class, ordered `created_at DESC, id DESC` so a re-run's history reads newest first |
| Error envelope | `main.py`'s handler now also honours a route-raised `code`, so a policy refusal arrives as `{code, message, details}`. Every pre-existing raise is unchanged — the F8 `details` regression test and the F6 ingestion `409` remain green |
| Tests | `test_agent_decisions.py` (44): priority order; consent subsetting; each refusal path; opt-out beating a full grant; nothing value-shaped in `reasoning` or in `GET`; `409` carries the winning consent state; one audit row per consent change; identical resubmission writes nothing; stale-but-no-change is still a conflict; tenant isolation; viewer cannot decide; unauthenticated rejected |

Two limits are stated rather than implied. **Quiet hours are not enforced
anywhere** (gap 18, D9-03): the window is a timing input checked immediately
before sending, and `Contract.timezone` has no tz database behind it at runtime
(D6-03). And **nothing sends** (gap 15): a decision now names a channel, but there
is no dispatcher, so the pipeline still stops one arrow short of a notification.

### Verification actually run (F9)

| Check | Command | Result |
| --- | --- | --- |
| Full suite, no MySQL | `pytest tests` (SQLite, `MYSQL_TEST_URL` cleared) | **342 tests, 0 failures, 4 skipped** (JUnit: `tests=342 failures=0 errors=0 skipped=4`) |
| Full suite, real MySQL 8.4 | `pytest tests` with `MYSQL_TEST_URL` on `:3307` | **342 tests, 0 failures, 1 skipped** (JUnit: `tests=342 … skipped=1`) |
| Migration, real MySQL | `alembic current` / `alembic check` at head, plus the integration fixture's `downgrade base` → `upgrade head` | **`0002 (head)`**, **"No new upgrade operations detected"**, columns present as `tinyint(1) NOT NULL DEFAULT 0` |
| Type check | `mypy app` (strict) | **clean, 44 source files** |
| Lint | `ruff check app tests` | **All checks passed** |
| Format | `ruff format --check app tests` | **68 files already formatted** |
| Security lint | `bandit -c pyproject.toml -r backend/app` | **0 issues** |
| Commit gate | `pre-commit run --all-files` | **16 hooks passed** (17th, `forbid submodules`, not applicable) |

The F8→F9 delta is **+44 tests on both databases** — the whole of
`test_agent_decisions.py`. The two runs differ by exactly the three real-MySQL
integration tests and the opt-in `live` Kafka test, unchanged from F8.

The migration row is worth reading twice, because it is the gate that does **not**
run by default and the one I had wrongly reported as green. The suite builds its
tables from `Base.metadata`, so the new columns work in tests while `0002` itself
could be broken; `alembic check` against the real server is what proves otherwise,
and the integration fixture only caught it because it runs the migration chain
rather than `create_all`.

#### What the decision tests prove and what they do not

They prove the **rules**: a channel is chosen only from consented channels that
also have an approved value; an opt-out refuses even when every consent flag is
set; a refusal writes no decision row and no audit row; a blocked pipeline is
reported as blocked rather than as a consent gap; reasoning names channels and
never a value; a re-run appends; a viewer cannot decide; another tenant's contract
is `404` and its decisions invisible. They do **not** prove the decision reaches
anyone — there is no dispatcher, no notification row and no screen, so these routes
have only ever been called by `TestClient`.

## F10 — Notification dispatch ✅ (its one unrun gate has since been run)

Step 6, and the arrow F9 left dangling. F9 produced a decision naming a channel;
`Notification` — a table since F2 — still had **no writer at all**. This slice adds
the writer, the state machine and the contact policy in front of it. It also
retires the caveat F9 had to carry: **quiet hours are now enforced**, because
`tzdata` made `Contract.timezone` resolvable and the window is checked immediately
before the transport is reached.

| Area | Detail |
| --- | --- |
| Policy | `app/workers/dispatch.py` — milestone selection (`T-60` / `T-30` / `T-14`, innermost due wins), the idempotency key, quiet hours, and one nine-step gate order. `blocks_pipeline`, `CLOSED_CONTRACT_STATUSES` and the consent refusals are **imported from F9, not restated** — a second copy is how two policies start disagreeing about whether a customer may be contacted |
| Consent, re-read | `assert_may_contact` runs at *plan* time and again at *send* time against the current contract row. An `AgentDecision` is history: a customer who withdrew consent an hour ago must not be emailed by a decision made yesterday (D10-03) |
| Quiet hours | Evaluated as **local wall clock in `Contract.timezone`** via `zoneinfo` + `tzdata` (`[20:00, 08:00)` by default, half-open, wrapping midnight). DST is handled by doing the arithmetic on local time and converting to UTC once, so a transition moves the *instant*, not the hour the customer is called. An unresolvable zone refuses (`UNKNOWN_TIMEZONE`) rather than assuming UTC — a guess can authorise a 03:00 call (D10-01) |
| Deferral, not refusal | Inside the window the row keeps its slot, keeps `pending`, and `scheduled_at` moves to the window opening; the response is `200` with `deferred_until` and the transport is never reached (D10-02). A future `scheduled_at` is the separate `NOT_YET_DUE` `409` |
| Transport seam | `MessageSender` protocol + `UnavailableSender` + `build_sender(settings)`, mirroring `EventPublisher` and `DocumentExtractor`. At the F10 commit `notification_transport` had **exactly one member, `none`**, so the stand-in refused instead of marking a row `sent` with nothing delivered (D10-04). **It now has two:** `smtp` selects `SmtpSender` in `app/workers/senders.py`, which delivers over a real relay (D10-09) |
| Routes | `GET /api/v1/notifications` (filters `status`/`channel`/`contract_id`, bounded 1–200, viewer allowed — the row carries no recipient), `POST /api/v1/notifications` (schedule from the newest decision, or a `decision_id` the caller names; `409 NOTIFICATION_EXISTS` on a taken slot), `POST /{id}/dispatch`, `POST /{id}/retry` (`409 NOT_FAILED`) |
| Repository | `app/db/repositories/notifications.py` — `for_contract`, `recent` (status/channel filters), `find_slot` (the read side of the unique `(contract_id, template, scheduled_date)` slot), `create`. Tenant-stamped from the base class; another tenant's id is `404` |
| Refusals | Fourteen dispatch codes, one shape: the F9 vocabulary (`CONTRACT_CLOSED`, `CONTACT_REFUSED`, `NO_CONSENT`, `PIPELINE_BLOCKED`) plus `NOT_PENDING`, `NOT_YET_DUE`, `NOT_IN_WINDOW`, `MISSING_END_DATE`, `UNKNOWN_TIMEZONE`, `NEEDS_REVIEW`, `CHANNEL_NOT_CONSENTED`, `ATTEMPTS_EXHAUSTED`, `SENDER_UNAVAILABLE`, `RECIPIENT_UNAVAILABLE`. `DispatchRefusedError` subclasses `DecisionRefusedError` so both map through **one** envelope (`refusal()` in `api/v1/agent.py` became public rather than growing a second vocabulary) |
| PII | The recipient is resolved **last**, after every gate, through a callable — so a refusal never puts a value in the frame. `OutgoingMessage.recipient` is `repr=False`, and no test-visible response echoes it. `DISPATCH_POLICY_VERSION` is documentation: `Notification` has no `prompt_version` column to store it in |
| Config | `notification_transport`, `quiet_hours_start` / `quiet_hours_end` (0–23, `start == end` refused at startup because a zero-length window would read as "configured" while switching the rule off), `notification_retry_max` |
| Dependency | `tzdata==2026.6` **added to `backend/requirements.txt`**, pinned and commented. This is a new runtime dependency, not a dev extra: without it `ZoneInfo("Europe/London")` raises on Windows (verified, not assumed) |
| Tests | `test_notifications.py` (55): every refusal code and the gate order; quiet-hours deferral across midnight **and across a DST transition** (summer "08:00 local" asserts `07:00Z`); `do_not_contact` beating a full grant; consent withdrawn between schedule and dispatch; idempotent slot; viewer can list but not dispatch; attempt exhaustion parking the row `failed`; `SENT`/`FAILED`/`PENDING` after a transport failure; no recipient value in any response or stored row |

### What F10 does **not** do — state it, do not bury it

- ~~**Nothing is sent.** `build_sender` returns the refusing stand-in, so
  **`sent: true` is unreachable through the API**: every HTTP dispatch is either
  `409 SENDER_UNAVAILABLE` or a quiet-hours deferral. The send path is exercised
  only by calling `dispatch_notification` with an injected fake in tests. "F10 sends
  notifications" is false until an adapter exists (gap 15).~~ — **closed by the SMTP
  slice (D10-09).** With `NOTIFICATION_TRANSPORT=smtp` and a relay configured, a
  dispatch reaches `sent`, and only after `DATA` was accepted with a 2xx. In the
  shipped default (`none`) the stand-in still refuses exactly as described, so
  "unreachable" was true of *that* configuration, not of the tree. ~~That the send
  path is proven against a fake client and not a socket remains true~~ — **also
  closed:** `test_smtp_socket.py` drives the adapter against a real loopback relay,
  and since D10-10 the handshake *authenticates* that relay rather than merely
  completing. What no test can supply is a real provider — see *The transport,
  built* below.
- ~~**No scheduler.** A deferred row is retried only when something dispatches it
  again — the outbox relay polls *events*, not notifications. Same
  operator-initiated shape as DLQ replay (D5-03, gap 22).~~ — **closed by
  `4e7dd5c`** (gap 22): a poller now claims due rows itself. At the F10 commit
  this was true.
- ~~**No row lock.** `Notification` has no `version` column, so two concurrent
  dispatches are not serialised; the unique `idempotency_key` is passed to the
  transport for provider-side dedupe (D10-05, gap 21).~~ — **closed by `0003`
  (`0f5da36`)**; the concurrency caveat that remained (gap 23) is now closed against
  a real MySQL server by two integration tests (D10-08).
- **No content rendering and no outbox event.** `template` names the milestone; the
  body is the transport's job. A send emits no `contract.events` message, so nothing
  downstream learns that contact happened.

### Verification actually run (F10)

| Check | Command | Result |
| --- | --- | --- |
| Full suite, SQLite | `pytest tests` (`MYSQL_TEST_URL` cleared) | **388 tests, 0 failures, 4 skipped** |
| Lint | `ruff check app tests` | **All checks passed** |
| Format | `ruff format --check app tests` | **69 files already formatted** |
| Type check | `mypy app` (strict) | **clean, 47 source files** |
| Security lint | `bandit -c pyproject.toml -r backend/app` | **0 issues** |
| **Migration, real MySQL** | `alembic check` at head | **NOT RUN — Docker daemon was not running on this host.** F10 itself added no migration, so at the time of the F10 commit `0002` was still head. **Superseded by the delivery-lock slice below:** `0003` now exists, head is `0003`, and this row is *more* unrun than it was — there is now a new revision that has never been checked against MySQL either. Do not cite this row as verified. **That has since been done:** `0003` runs on MySQL 8.4 with *no new upgrade operations* — see *The MySQL half, run* below (`c99c792`) |
| **Full suite, real MySQL** | `pytest` with `MYSQL_TEST_URL` on `:3307` | **NOT RUN — same cause.** Expect `388 … skipped=1`; confirm `skipped="1"`, because a skip is not a pass (this is the mistake F9's first report made). **Since run:** `421 passed, 1 skipped` of 422 collected — see *The MySQL half, run* below |
| Commit gate | `pre-commit run --files` on the 12 changed files | **all hooks passed**; the two frontend hooks (`tsc --noEmit`, `eslint`) reported `(no files to check)` because no frontend file changed in this slice. `--all-files` was not used, so the repo-wide tsc/eslint pass is **not** claimed here |
| Alembic topology | `alembic heads` | Was **`0002 (head)`** at the F10 commit — one head, unchanged from F9. **Now `0003 (head)`** after the delivery-lock slice. This does **not** replace `alembic check` (row above), which needs a live server |

The F9→F10 delta is **+46 SQLite tests** (`388` vs `342`). The two MySQL rows are
left explicitly unrun rather than filled in from expectation: the harness that
proves `SKIP LOCKED` and the real ENUM columns needs a server, and no server was
reachable. **That was true of the F10 commit and is no longer true of the tree** —
a Docker daemon answered in `c99c792`, both MySQL rows were run, and the row that
matters most (`SKIP LOCKED`) now fails if the clause is removed. See *The MySQL
half, run* below.

#### The transport, built (D10-09)

`app/workers/senders.py` implements `MessageSender` over SMTP, so `build_sender`
now returns `SmtpSender` when `NOTIFICATION_TRANSPORT=smtp`. The protocol and the
factory stayed where D10-04 put them; this is the addition that entry predicted.

| Area | Detail |
| --- | --- |
| Adapter | `SmtpClient` protocol (the slice of `smtplib.SMTP` actually used), `SmtpSender`, `open_smtp_client`, `create_smtp_sender`, `build_sender`. `dispatch.py` still never imports `smtplib`, so the consent and quiet-hours tests need no transport at all |
| Envelope | Sender, recipient, a constant `SUBJECT`, `Message-ID` and `X-Idempotency-Key`. The body is a placeholder naming the contract reference and carries **no extracted value** — the counterparty and the dates stay in the database |
| Message-ID | `sha256(idempotency_key)@<sender domain>`, **not** `email.utils.make_msgid`. At-least-once delivery is only survivable if the re-send is byte-identical in the field a provider de-duplicates on; a random id would make the retry a different message |
| Timeout | `SMTP_TIMEOUT_SECONDS` (default 10, `gt=0, le=60`) reaches `smtplib` as the socket timeout, so connect, TLS, auth and `DATA` share one ceiling. It is a correctness bound: the send runs *inside* the transaction holding the row's `FOR UPDATE` lock |
| Refusals | `sms` and `voice` are **refused, not substituted** — a voice reminder must not arrive as an e-mail. The refusal is a `SenderError`, so the row spends its retry budget and parks `failed` where an operator sees it |
| Redaction | Relay error text passes through `redact_text` before it can reach `Notification.last_error`, because `GET /notifications` renders that column to viewers who cannot see contact values elsewhere |
| TLS | `starttls(context=...)` receives a **verifying** context from `build_ssl_context`. `SMTP_CA_BUNDLE` replaces the trust store; no bundle means the system store. `None` resolves to `ssl.create_default_context()`, so no argument ever means "skip checks" — `smtplib`'s own default for `context=None` is `CERT_NONE`, which is what gap 24 was |
| Settings | `SMTP_HOST` required, `SMTP_FROM` shape-checked (a display name is refused), username/password both-or-neither, `SMTP_CA_BUNDLE` must be a file `ssl` can actually load, a bundle with `SMTP_STARTTLS=false` is refused (a CA never consulted would read as "TLS on"), and `SMTP_STARTTLS=false` is refused in staging/production. All seven are boot errors, so a half-configured relay fails at start-up rather than at the first send inside the row lock |
| Tests, unit | `test_senders.py` (35 tests: selection, envelope, Message-ID stability, channel refusal, refusal redaction, TLS-before-auth ordering, the verifying context handed to `starttls` and its system-store default, conversation closing on failure, timeout argument, and the eight settings rules). All of them drive a **fake** client, so they pin what we ask `smtplib` to do, not what it does |
| Tests, socket | `test_smtp_socket.py` (8 tests) against a loopback relay built from `socket` + `threading` — no container, no new dependency. It observes the real `EHLO → MAIL → RCPT → DATA → QUIT` conversation, a body that crosses only after `354`, a genuine `SMTPRecipientsRefused` whose text keeps `550` and drops the address, a re-send byte-identical **on the wire**, the timeout as a real ceiling, a real `STARTTLS` handshake where `AUTH` reaches the relay only over the encrypted socket (the relay refuses auth in the clear with `530`), and — the one that proves peer authentication — **the same relay refused** when its self-signed certificate is not in the configured bundle |

**Verification actually run, this slice:**

| Check | Command | Result |
| --- | --- | --- |
| Full suite, SQLite | `pytest tests` (`MYSQL_TEST_URL` cleared) | **458 passed, 7 skipped, 0 failed** |
| Lint | `ruff check .` | **All checks passed** |
| Format | `ruff format --check .` | **82 files already formatted** |
| Type check | `mypy app` (strict) | **clean, 49 source files** |
| Security lint | `bandit -r app` | **0 issues** (3 expected `nosec B104`) |

The suite grew by exactly the TLS slice: `458 − 451 = 7`, which is `29 → 35` in
`test_senders.py` (+6) and `7 → 8` in `test_smtp_socket.py` (+1). The format count went
`78 → 82`, but that is **scope, not files**: this run measured from the repo root, while
the earlier figure counted `backend/` alone — and `backend/` holds exactly 78 `.py`
files, so the two numbers are not comparable. Every gate was also re-run through
`pre-commit` with its own pinned toolchain (`ruff` 0.17.3, `mypy` 2.4.1, `bandit`
1.9.4) rather than the venv, and all four hooks passed.

**Three claims were falsified before they were believed.** The first two were reverted
and `senders.py` was confirmed byte-identical to `HEAD`; the third was removed from the
working tree after it failed, and the tree since differs from `HEAD` only by this
slice's intended change:

| Falsification | Expected | Observed |
| --- | --- | --- |
| `if self._starttls and False:` (never call `starttls()`) | encrypted-socket test fails | **FAILED** — the relay advertised no AUTH, because auth in the clear is refused with `530` |
| `timeout=None` (drop the socket timeout) | timeout test fails | **FAILED** — the send sat for the relay's full 30 s instead of failing under 2 s |
| remove `context=self._context` from the `starttls` call | the refusal test fails | **FAILED** — and, the point of it, the *encrypted-socket* test still **passed**: a handshake it observed proves nothing about who answered it. Four unit tests failed alongside it |

**What this does not prove.** The relay is ours, so what the socket tests show is that
the client speaks SMTP to something that answers SMTP — not that it is compatible with
a particular provider's quirks. Peer authentication **is** now proven in the direction
that matters: with the relay's self-signed certificate absent from the bundle the send
is refused, and with it present the handshake completes, so the client is genuinely
checking rather than merely calling `starttls()`. What is not proven is the other
direction — a certificate issued by a public CA over a real network, and a hostname
mismatch against a provider — because a loopback test cannot be its own third party.
Everything beyond the relay's own answers — greylisting, rate limits, spam scoring,
`Message-ID` de-duplication in practice — is still unobserved, and the first deployment
will be the first real conversation with a real relay.


#### Five defects the tests caught in my own work

1. **Quiet-hours assertions read the wrong `Settings`.** Two tests compared against
   `settings.quiet_hours_end` (the default `8`) while the fixture ran a tuned window
   whose end was `12`. The policy was right and the assertion was wrong. Fixed with a
   `quiet_window` fixture that owns the tuned pair and feeds both the client and the
   assertions.
2. **A consent-withdrawal test constructed a case it did not mean.** Withdrawing the
   *only* consent flag empties `consent_channels`, so the earlier `NO_CONSENT` code
   fires correctly and `CHANNEL_NOT_CONSENTED` was unreachable — the test now also
   grants SMS, so withdrawal of email leaves a non-empty set and exercises the narrow
   gate.
3. **A DST assertion contradicted its own comment.** The comment said "08:00 BST is
   07:00 UTC"; the assertion said `08:00`. The comment was right.
4. **A milestone boundary off-by-one.** `2026-06-01` is exactly `end_date - 30`, and
   `milestone_for`'s window is inclusive at the edge, so that day is legitimately
   `T-30`. Replaced with a date genuinely outside it, asserted as a full
   `(template, scheduled_date)` tuple so both halves are pinned.
5. **Deprecated `datetime.utcnow()`** in one test (gap 10, carried since F2). Replaced
   with the existing `app.db.base.utc_now()` helper; `utcnow` now appears nowhere in
   `backend/`.

#### One editor warning that came and went — stated, not explained

Mid-slice, Pylance reported ~117 `Mapped`/attribute errors in
`test_notifications.py` while `test_agent_decisions.py`, `test_review.py`,
`test_contracts.py` and `test_contract_fields.py` reported **zero** with identical
imports, `__future__` annotations and `Mapped` usage. A scratch module importing the
same set resolved cleanly, so it was not a poisoned import.

**It does not reproduce now.** Re-querying diagnostics on that file after the test
fixes reports it clean. I never identified the cause, so I am not claiming to have
fixed it: the honest statement is that the warning was real, is gone, and was never
diagnosed. `mypy` — the actual gate, scoped to `backend/app/` in
`.pre-commit-config.yaml` — does not analyse `backend/tests/` and is clean either way.

## Verification actually run (F2)

| Check | Command | Result |
| --- | --- | --- |
| Full suite, real MySQL | `pytest backend/tests` with `MYSQL_TEST_URL` set | **84 passed** |
| Full suite, no MySQL | `pytest backend/tests` with `MYSQL_TEST_URL` unset/blank | **81 passed, 3 skipped** |
| MySQL integration only | `pytest -m integration` | **7 passed** (4 harness + 3 real MySQL) |
| Pre-commit gate | `pre-commit run --all-files` | **all 16 hooks passed** |
| Security lint (gate scope) | `bandit -c pyproject.toml -r backend/app` | 0 issues |
| Security lint (repo-wide) | `bandit -c pyproject.toml -r backend` | 0 Medium/High; 170 Low, **all in `backend/tests`** (165 × B101 `assert_used`, 5 × B106 hard-coded fixture passwords) |

The three integration tests are the only ones that need a database server beyond
SQLite. They are skipped — not failed — when MySQL is unreachable.

> **Historical count, not a contradiction.** This table records what the F2 commit
> actually reported. Two harness tests (the SQLAlchemy driver-error cases in
> `test_mysql_integration.py`) landed afterwards in the follow-up `a79d893`
> (`fix(f2): report the real cause when the mysql tests skip`), so the same
> `-m integration` command **reported 9 passed** at the time of that follow-up —
> the F4 row above — with the three real-MySQL tests unchanged. It reports **12**
> today, because the F10 slice added three more; see *The MySQL half, run* below.

```powershell
docker compose up -d mysql
# compose has NO default MYSQL_PASSWORD, so this must match the .env it was started with.
$env:MYSQL_TEST_URL = "mysql+pymysql://contract_agent:<MYSQL_PASSWORD>@127.0.0.1:3306/contract_agent_test"
venv\Scripts\python.exe -m pytest -m integration backend/tests
```

> **The `:3306` above is wrong for this compose file.** The service publishes
> `127.0.0.1:3307->3306`, so every `:3306` URL in this file — this one included —
> looks plausible and connects to nothing. The run recorded in *The MySQL half, run*
> used `:3307` and read the password from the live container env, because there is
> no `.env` in the repo root to copy it from.

`MYSQL_TEST_URL` points at a **disposable** database — the tests drop and
recreate the schema. A set-but-blank value falls back to the default rather than
erroring (regression-tested).

**Set `MYSQL_TEST_URL`, including the password.** The built-in fallback assumes
the password is literally `contract_agent`; `docker-compose.yml` declares
`MYSQL_PASSWORD` as required with no default, so in any real setup the fallback
is wrong and the tests skip. One extra step is needed the first time: the app
user usually lacks `CREATE DATABASE`, so create the scratch database once as an
admin —

```powershell
docker compose exec mysql mysql -uroot -p -e "CREATE DATABASE IF NOT EXISTS contract_agent_test"
```

Skip reasons name the exact cause (unreachable vs. `Access denied` vs. no
`CREATE` privilege) and never print the password.

### What the MySQL tests prove that SQLite cannot

1. **`SELECT ... FOR UPDATE SKIP LOCKED` works.** SQLite has no `FOR UPDATE`;
   SQLAlchemy silently omits the clause, so the SQLite suite would pass even if
   the clause were deleted. The test runs two workers on separate connections and
   asserts they claim **disjoint** rows.
2. **Enums are native MySQL `ENUM(...)`** columns with the expected values.
3. **The baseline migration applies to real MySQL** and creates every table in
   `Base.metadata` — i.e. the DDL operators will actually ship is valid.

These tests earned their keep immediately: they caught **D2-06**, where MySQL's
second-precision `DATETIME` *rounds* microseconds on insert, so an event
enqueued "now" could be stored as not-yet-due and be skipped by the relay that
just wrote it. Timestamps are now floored before writing.

## Optimistic locking for the delivery tables ✅ (`0f5da36`, migration `0003`)

Gap-closing slice rather than a numbered milestone: `AgentDecision` (gap 19) and
`Notification` (gap 21) were the two tables in the flow with no `version` column and
no `__mapper_args__`, so neither could be optimistic-locked. For `Notification` that
was load-bearing — `POST /{id}/dispatch` and `/{id}/retry` mutated the row as a plain
last-writer-wins UPDATE, so a worker could revert another's `SENT` transition or lose
its `attempts` increment. For `AgentDecision` it was structural: a table that cannot
be locked is why no override or flag-review route could be written at all.

The change is four parts, and the second is the one that is not just schema:

1. `version` + `__mapper_args__` on both models, mirroring `Contract`'s idiom.
2. **`NotificationRepository.claim_for_dispatch`** reads the row under
   `SELECT ... FOR UPDATE` *before* the policy runs. A version column alone
   serialises the commit, not the send: two workers would still both reach the
   transport, then one would lose at commit after the customer already had two
   emails. The lock makes the second request block and then see `sent`, so it
   refuses `NOT_PENDING` instead of sending twice.
3. `_commit_or_conflict` maps `StaleDataError` to a `409 CONCURRENT_UPDATE`,
   deliberately distinct from `NOT_PENDING` — that means the row was already sent
   when we looked, this means it moved while we looked.
4. `0003_delivery_versions.py`, hand-written like `0001`/`0002`, adding both columns
   `NOT NULL` with `server_default=1` so existing rows get the truthful value rather
   than a placeholder.

| Check | Command | Result |
| --- | --- | --- |
| Full suite, SQLite | `pytest tests` | **391 tests, 0 failures, 4 skipped** (JUnit-confirmed; `+3` lock tests) |
| Lint | `ruff check app tests migrations` | **All checks passed** |
| Format | `ruff format --check app tests migrations` | **73 files already formatted** (2 needed reformatting first) |
| Type check | `mypy app` (strict) | **clean, 47 source files** |
| Security lint | `bandit` | **0 issues** (3 expected `nosec B104`) |
| Migration applies | `alembic upgrade head` on a fresh database | **`0001 → 0002 → 0003`, exit 0** |
| `alembic check` at head | SQLite, fresh DB built by the chain | **zero operations for either new `version` column.** The 13 reported diffs are all `modify_type` on enum columns created by `0001` — a pre-existing SQLite-vs-`ENUM` length artefact, not drift this slice introduced |
| Alembic topology | `alembic heads` | **`0003 (head)`** — one head |
| **`alembic check`, real MySQL** | needs a server | **NOT RUN AT THIS COMMIT — no Docker daemon on this host.** Unrun then, not green then — **superseded: since run, see *The MySQL half, run* below**, where `0003` is verified at head against MySQL 8.4 with zero diffs |
| Commit gate | husky pre-commit + commit-msg | **all 17 hooks Passed**, including `tsc --noEmit` and `eslint` |

The three new tests are `test_models.py::test_notification_rejects_a_stale_write`,
`::test_agent_decision_rejects_a_stale_write`, and
`test_notifications.py::test_a_lost_race_is_reported_as_a_conflict`. The last one
drives `_commit_or_conflict` directly rather than over HTTP, because every session in
that suite shares one in-memory connection (`StaticPool`) so a competing commit
cannot land behind an in-flight request; the out-of-band `UPDATE` still bumps the
stored version, so the dirty row's commit is genuinely rejected.

## A scheduler for due and deferred notifications ✅ (`4e7dd5c`)

Gap-closing slice, no migration. Gap 22 was that `scheduled_at` was **written and
never read**: a quiet-hours deferral stores a future timestamp and stops, and the
outbox relay polls *events*, not notifications — so a deferred row moved only when
an operator POSTed `/dispatch` again. The deferral was therefore not a delay, it was
a stop.

`app/workers/notification_scheduler.py` closes it, mirroring `outbox_relay`'s shape
(`run_once()` / `run_forever(stop)` / `main()`, `python -m
app.workers.notification_scheduler`):

1. **`NotificationRepository.claim_next_due`** — `SELECT ... FOR UPDATE SKIP LOCKED
   LIMIT 1` over `status = pending AND (scheduled_at IS NULL OR scheduled_at <=
   now)`. That predicate *is* the gap: "due" is exactly what makes a deferral resume.
   It is deliberately **not** tenant-scoped — the second unscoped read in the
   repositories, after `OutboxRepository.claim_due`, which is unscoped for the same
   reason: a poller is one process serving every tenant, so it cannot know the tenant
   before it has picked the row (D10-07).
2. **The tenant guard is the scoped read itself.** The worker never compares tenant
   ids: it reads the contract through `ContractRepository(session, row.tenant_id)`,
   so a contract belonging to a different tenant is simply *not found* and the row is
   parked `TENANT_MISMATCH` rather than dispatched. That is what keeps the unscoped
   claim safe, and it is tested.
3. **Claim and send share one transaction**, so the `FOR UPDATE` taken by
   `claim_next_due` still spans the send and the commit lands after the transport
   returns. A crash between send and commit re-sends rather than losing the row —
   at-least-once, the same rule the relay uses.
4. **`recipient_for` moved out of the route into `app/workers/dispatch.py`.** The
   poller resolves a recipient with no HTTP request in sight; leaving the rule in
   the router would have forced a second copy of it into the worker, which is the
   duplication that module exists to prevent.
5. **`POST /{id}/retry` now lifts `suppressed`, not just `failed`.** The scheduler
   parks permanent policy refusals as `suppressed`; without this they would have
   been unreachable by the one route meant to recover them.

Three judgements that could have been made silently and wrongly:

- **`SENDER_UNAVAILABLE` is deliberately not suppressible.** The shipped transport
  is `none`, so suppressing it would let one scheduler start move *every* pending
  row into a human-only state, for a cause an operator fixes with a single env var.
  `run_once()` returns early, and `run_forever` warns once, when no sender is
  available.
- **A failed send stops the drain.** Otherwise the loop re-claims the same still-due
  row and burns the whole retry budget inside one tick; `TickResult.resolved`
  enforces "attempts, not attempts per drain".
- **`notification_retry_max` floor moved `ge=0` → `ge=1`.** At `0` the policy parks
  every row before reaching the transport, so sending was impossible by
  configuration.

| Check | Command | Result |
| --- | --- | --- |
| Full suite, SQLite | `pytest tests` | **408 tests, 0 failures, 4 skipped** (JUnit-confirmed; `+15` scheduler, `+2` config) |
| Lint | `ruff check app tests migrations` | **All checks passed** |
| Format | `ruff format --check app tests migrations` | **75 files already formatted** (2 needed reformatting first) |
| Type check | `mypy app` (strict) | **clean, 48 source files** |
| Security lint | `bandit` | **0 issues** (3 expected `nosec B104`) |
| Migration applies | `alembic upgrade head` on a fresh database | **`0001 → 0002 → 0003`, exit 0** |
| `alembic check` at head | SQLite | only the 13 pre-existing enum-length artefacts — **no new migration**, `SUPPRESSED` was already in `0001`'s ENUM definition |
| Alembic topology | `alembic heads` | **`0003 (head)`** — unchanged, this slice added no revision |
| **`alembic check` / full suite, real MySQL** | needs a server | **NOT RUN AT THIS COMMIT.** Unrun then, not green then — **superseded: since run, see “The MySQL half, run” below** |
| Commit gate | husky pre-commit + commit-msg | **all 17 hooks Passed**, including `tsc --noEmit` and `eslint` |

Three ruff findings were fixed rather than suppressed: unsorted `__all__` in
`dispatch.py`, a `field` loop variable shadowing the `dataclasses.field` import, and
a missing `Any` import in the new test file. `mypy tests` reports 12 errors in
`test_crypto.py` / `test_extraction.py` — pre-existing and outside the gate's `app`
scope; this slice did not touch them.

## A cursor for the review queue ✅ (`c90423b`)

Gap 16. `GET /review/queue` was bounded at 500 rows and had no way to ask for the
next 500, so on a backlog larger than the cap the lowest-confidence items were
returned forever and everything past them was invisible. That is the right *order*
and the wrong *reach*.

What lands:

1. **Keyset paging, not `OFFSET`.** `ReviewCursor` names the sort key of the last
   row on the page — `(confidence, created_at, field_id)` — and
   `awaiting_review(after=...)` applies it as a predicate over exactly the triple
   the query orders by. The queue is written while it is read: a reviewer deciding
   a field removes it from the predicate, every later row shifts up by one, and an
   offset would skip an item nobody ever saw — silently, and as the normal case
   rather than a race (D8-06).
2. **An opaque token that is deliberately unsigned.** URL-safe base64 over
   versioned JSON, carrying no secret and no tenant. Everything it names is already
   on the page it came from, and the tenant predicate is applied by the query
   regardless, so a hand-edited cursor can only move within the caller's own queue.
   A token that does not decode is a `422`, never a silent page one — a client
   paging by cursor would otherwise loop over page one and report a server bug.
3. **`limit + 1` answers "is there more".** No second `COUNT`: a count over a
   moving set is a number from a different instant than the page, so it can promise
   a page that is not there.
4. **The keyset predicate is three `OR` terms, not a row-value comparison.**
   Row-constructor support differs between SQLite and MySQL versions and the two
   must read identically; the terms mirror the `ORDER BY` term for term, because a
   predicate that disagrees with the sort order drops or repeats rows exactly at
   the page boundary.

**A real defect, found by the paging test rather than by review.** `confidence` is
`NUMERIC(4, 3)`. MySQL enforces that scale; **SQLite does not**, and stores the
float it is handed. `0.1 * 3` is `0.30000000000000004`, so it was stored in full
and read back rounded to `0.300` — and the boundary term `confidence ==
cursor.confidence` then matched a row whose stored and read values differed, so
page two re-served the last row of page one. The fix is `CONFIDENCE_SCALE` plus a
`@validates("confidence")` quantiser on `ExtractedField`, which makes the declared
scale true on both dialects for the same reason `truncate_to_second` floors
timestamps (D2-06). It is the test being right about the code, not the other way
round; on MySQL it should be a no-op — and it **is**: a raw-SQL insert of `0.1 * 3`
against MySQL 8.4 reads back `Decimal('0.300')` on its own, so the fix is load-bearing
only where the scale is merely declared. Measured, not assumed — see “The MySQL half,
run” below.

| Check | Command | Result |
| --- | --- | --- |
| Full suite, SQLite | `pytest tests` | **415 passed, 0 failures, 4 skipped** — `+11` collected over the post-scheduler 408 (9 review paging, 2 model quantiser) |
| Lint | `ruff check app tests migrations` | **All checks passed** |
| Format | `ruff format --check app tests migrations` | **75 files already formatted** |
| Type check | `mypy app` (strict) | **clean, 48 source files** |
| Security lint | `bandit` | **0 issues** (3 expected `nosec B104`) |
| Migrations | — | **none added.** No model column changed shape, so `alembic heads` stays `0003 (head)` |
| **`alembic check` / full suite, real MySQL** | needs a server | **NOT RUN AT THIS COMMIT.** Unrun then, not green then — **superseded: since run, see “The MySQL half, run” below**; the quantiser was the claim tested |
| Commit gate | husky pre-commit + commit-msg | **all 17 hooks Passed**, including `tsc --noEmit` and `eslint` |

Three ruff findings were fixed rather than suppressed while landing it: an
import-order error in the new test file, and two files that needed reformatting.
`test_review.py` also had one exact-body assertion that `next_cursor` legitimately
broke; the assertion was updated rather than the response.

## The MySQL half, run ✅ (`c99c792` — `test(f10): prove the dispatch lock against a real mysql server`)

Four slices carried the same open item: "the MySQL half is unrun, not green". A
Docker daemon answered this time, so it is run.

| Check | Command | Result |
| --- | --- | --- |
| MySQL integration only | `pytest -m integration` vs. **MySQL 8.4.13** on `127.0.0.1:3307` | **12 passed**, 410 deselected |
| Full suite, real MySQL | `pytest` with `MYSQL_TEST_URL` set | **421 passed, 1 skipped** (the live-Kafka opt-in) |
| Full suite, no MySQL | `pytest` with `MYSQL_TEST_URL` unset | **415 passed, 7 skipped** |
| `alembic current` | MySQL | **`0003 (head)`** |
| `alembic check` | MySQL | **"No new upgrade operations detected."** — zero diffs |
| `alembic check` | SQLite | 13 pre-existing enum-length artefacts, unchanged |
| Lint / format / types / bandit | `ruff`, `mypy app`, `bandit -r app` | clean: **75 files**, **48 source files**, **0 issues** |

**Why the container's address is `:3307`, not `:3306`.** The compose service
publishes MySQL on `127.0.0.1:3307->3306`, and there is no `.env` in the repo root,
so the credentials come from the running container's environment. Every earlier
documented command in this file used `:3306`; that is why they looked plausible and
failed.

**Gap 23 is closed by two tests, not by a better argument.** Both live in
`test_mysql_integration.py` and both need two real connections, which is exactly
what the SQLite suite cannot offer:

1. `test_the_dispatch_lock_blocks_a_second_worker` — worker B is proven to **block**
   on worker A's `SELECT ... FOR UPDATE` (MySQL errno **1205**, with the session's
   lock-wait timeout shortened to 1s so a regression fails fast), and after A commits
   B's locking read returns the row as `sent`, which is the state the policy refuses
   with `NOT_PENDING` instead of reaching the transport twice.
2. `test_the_scheduler_claim_skips_a_row_another_worker_holds` — `4e7dd5c` added a
   *second* caller of the lock, and `claim_next_due` must **skip** rather than
   queue. The assertion is row *identity*: B must be handed the other due row.

`errno 1205` is used as the **positive** assertion here, which is the unusual part:
the test passes *because* a timeout was raised. A blocked claim and a concurrent
read are the two hypotheses, and only the blocking one raises.

**Both were falsified before being trusted.** Removing `.with_for_update()` from
`claim_for_dispatch` makes the first test fail at `test_mysql_integration.py:410`
("DID NOT RAISE"), and removing `skip_locked=True` from `claim_next_due` makes the
second report that the same row went to two schedulers. A test that cannot fail
proves nothing, and this pair was the one claim in the project resting on reasoning.

**The quantiser from `c90423b` is a no-op here — shown, not argued.**
`test_confidence_enforces_its_declared_scale_without_the_quantiser` inserts
`0.1 * 3` with **raw SQL**, bypassing the `@validates` quantiser entirely, and MySQL
stores and returns `Decimal('0.300')` because `NUMERIC(4, 3)` is enforced rather
than merely declared. That is the falsifiable form of "MySQL did not need this fix":
had MySQL stored the unrounded float, the fix would be load-bearing on both dialects
and the keyset page boundary would need a different answer than "quantise on write".

**Docker state is not a gate.** These tests skip, not fail, when MySQL is
unreachable, so the offline suite stays green — but a skip is not evidence, which is
why the rows above record a run rather than an absence.

## Operator overrides and flag-review ✅ (`feat(g19)`, migration `0004`)

Gap 19, the last of the three-gap schema cluster. `0003` made the write *safe*
(`version` + `__mapper_args__`), which is why the routes could not be written
before it; this slice is the feature half.

What lands:

1. **Two routes on the contract, not the decision row:**
   `POST /agent/decisions/{contract_id}/override` and
   `POST /agent/decisions/{contract_id}/flag-review`. Addressed by contract id to
   match the shapes `demo/backend/main.py` already defines, which is also the id
   an operator actually has when they notice the wrong channel was chosen.
2. **Two request models, not one with an optional field.** `DecisionOverrideRequest`
   carries `channel`, `FlagReviewRequest` does not, so "flag-review that also
   switches channel" is unrepresentable rather than merely rejected.
3. **The gates are re-run, not re-implemented.** `permitted_channels()` was
   extracted out of `select_channel()` so the override route evaluates the same
   review/opt-out/consent predicate the policy uses — against the contract's state
   *now*. An override therefore chooses **among** permitted channels and never
   creates permission: a contract that revoked consent after the decision refuses
   with `CHANNEL_NOT_PERMITTED` (`REFUSAL_CHANNEL_NOT_PERMITTED` in
   `app/workers/decision.py`).
4. **`original_channel` is written once and never backfilled.** `NULL` means "no
   human overrode this", so the migration deliberately does not copy `channel`
   into it — doing so would assert that a script's choice had been confirmed by a
   person. `overridden_by` records the actor; `rerun_count` counts appends.
5. **Every correction is audited** (`agent_decision.overridden` /
   `agent_decision.flagged`) with before/after state, written through
   `AuditLogRepository.record`. The row is mutated in place, so the audit row is
   what makes the edit reconstructable.
6. **Stale writes answer `409 VERSION_CONFLICT` with the current row**, a missing
   decision answers `409 NO_DECISION`, and a `StaleDataError` raised at flush is
   mapped to the same 409 rather than a 500.

| Check | Command | Result |
| --- | --- | --- |
| Full suite, **real MySQL 8.4** | `pytest tests` with `MYSQL_TEST_URL` on `:3307` | **482 collected — 481 passed, 1 skipped**, exit 0 (JUnit-confirmed) |
| Full suite, SQLite | `pytest tests`, `MYSQL_TEST_URL` **unset** | **482 collected — 475 passed, 7 skipped** |
| Migration applies | `alembic upgrade head` on a **fresh** MySQL database | **`0001 → 0002 → 0003 → 0004`**, exit 0 |
| `alembic check` | MySQL, fresh DB built by the chain | **"No new upgrade operations detected."** |
| Alembic topology | `alembic current` | **`0004 (head)`** |
| Lint | `ruff check .` (repo root) | All checks passed |
| Format | `ruff format --check .` (repo root) | **83 files** already formatted |
| Types | `mypy app` (strict) | clean, **49 source files** |
| Security lint | `bandit -c pyproject.toml -r app` | **0 issues** (3 expected `nosec B104`) |

The two suites reconcile rather than being two numbers: **482 either way**, which
is GATE 2's 465 plus 17. Offline, 6 MySQL-dependent tests plus the opt-in
`KAFKA_LIVE` test skip (7); online, the same 6 run and only `KAFKA_LIVE` skips (1).
The scratch database was created as root because the app user lacks
`CREATE DATABASE`, and it was dropped afterwards — verified absent, not assumed.

**A real defect the gate caught, not the author.** `mypy` flagged a new `except`
that rebound `exc` over the deleted binding from an earlier handler; left alone, a
stale exception variable and a fresh policy refusal would have been
indistinguishable in the log. It was renamed rather than suppressed. `ruff` also
reformatted the new test module.

Two residues are carried rather than hidden: a re-run is still a `POST` that
**appends**, so `rerun_count` is a snapshot at append time and not a live
aggregate; and ~~`demo/fixtures/agent_decisions.json` still carries field names
(`tokens_in`, `input_signals`, …) the real model does not have~~ — **closed
since.** The fixture was rewritten to the `AgentDecisionView` wire shape, and the
drift was wider than first recorded: five invented fields, four missing ones, and
the same drift in `users.json`, `extracted_fields.json` and `timeline.json`, plus
the demo backend accepting an override without a `version` and mutating a decision
in place where the real route appends. Still open: `llm_model` legitimately reads
`qwen2:7b` in the fixture against `deterministic` in a real row — demo narrative,
not drift.

## Known gaps / issues

1. **`gitleaks` needs Docker** and is manual-stage only (D1-03). Run
   `pre-commit run gitleaks --all-files` before pushing, or rely on CI.
2. **`make` is optional** — not installed on the primary Windows host; `npm`
   scripts are the contract (D1-09).
3. **Bandit's gate scope is `backend/app`.** A repo-wide `bandit -r backend`
   reports 170 Low findings, all of them in test code (`assert_used`,
   hard-coded fixture passwords). This is deliberate: `B101` in tests carries no
   signal, and narrowing the app scope to silence it would be worse than
   documenting it.
4. ~~**`/health/ready` does not exist yet**~~ — **closed by F5.** `GET
   /health/ready` probes MySQL and, when a transport is configured, Kafka, and
   returns 503 with a coarse per-check reason (D5-01, D5-02). It is deliberately
   **not** what the Docker healthcheck calls, because that would restart a
   healthy API on a database blip.
5. **No broker and no consumer; DLQ replay is manual.** F4 delivers a Kafka
   *publisher* and F5 adds `python -m app.cli outbox-replay` for parked rows, but
   `docker-compose.yml` still has no Kafka service, so nothing has been proven
   against a real broker and the `live` test is opt-in (`KAFKA_LIVE=1`). The
   consumer side of at-least-once does not exist. Replay is operator-initiated:
   **no alert fires when rows park and nothing requeues them automatically**
   (D5-03). The default `EVENT_TRANSPORT=none` keeps the refusing stand-in, so a
   fresh deployment retries and parks events rather than losing them (D4-03).
6. **No refresh token.** Access tokens last 15 minutes and cannot be renewed
   without re-entering credentials (D3-05). Revocation works immediately via
   `token_epoch`, so this is a usability limit, not a security one.
7. **The frontend does not use the auth API yet.** F3 is backend-only: there is
   no login screen, no token storage and no `Authorization` header in
   `frontend/src/`. The `frontend/src/lib/api.ts` module and the `demo/` fixtures
   still run unauthenticated. Wiring the UI is part of a later milestone — until
   then, `/api/v1/auth/*` is exercised by tests only.
8. **Only `POST /auth/users` exists for account lifecycle.** There is no
   password reset, no self-service change, no deactivate/delete endpoint. A
   forgotten password is an operator task (bump `token_epoch`, set a new hash).
9. **No rate limiting on `/auth/login`.** Argon2id makes each attempt expensive
   server-side, which is not the same as throttling an attacker. Admission
   control belongs at the edge (proxy/WAF) or with a later Redis-backed limiter.
10. **No bootstrap path for the first user.** `POST /auth/users` requires an
    admin token, which requires an existing user, so a fresh database cannot be
    logged into at all. The workaround is the manual snippet under "Creating the
    first account" (executed and verified above). A seed command
    (`python -m app.cli create-admin`) is the obvious fix and is **not built** —
    it needs a decision on whether seeding is an operator CLI or a migration-safe
    env-var bootstrap, since the latter puts a password in the environment.
11. ~~**Ingested contracts are inert.**~~ — **closed by F7.** `python -m app.cli
    extract` turns a contract's document text into `ExtractedField` rows with a
    status, confidence and provenance, and the run's counts are logged with a
    distinct exit code (`2`) when it stores nothing — so an ingest that produces
    no fields is loud rather than silent. Nothing *automatically* triggers
    extraction on ingest yet: it is an operator command, which is the same
    deliberate shape as `outbox-replay` (D7-01's trade-off).
12. **The ingestion fingerprint is metadata-only.** `sha256` covers the submitted
    content fields, not the document bytes, because there is no upload yet. Two
    genuinely different PDFs describing the same customer and dates would collide
    (D6-04). When PDF upload lands, the digest must become the file hash — until
    then dedupe is only as good as the metadata the caller sends.
13. ~~**The husky commit gate has never actually run.**~~ — **closed by
    `chore(hooks)` (`4a9e8e9`).** The cause was upstream of `core.hooksPath`: root
    `node_modules` did not exist on this host at all (bootstrap had been run with
    `--skip-node`), so the `prepare` → `husky` step in `package.json` had never
    executed and `.husky/_/` — the v9 shim directory that is the thing which sets
    `core.hooksPath` — had never been generated. `npm install` at the root, exactly
    what `scripts/bootstrap.mjs` already intended, was the whole fix. Verified **by
    refusal rather than by inspection**: `git commit --allow-empty -m "nope this is
    not conventional"` ran all 17 hooks green and was then rejected by commitlint
    (`subject-empty`, `type-empty`, exit 1, no commit object created), after which
    `4a9e8e9` was committed with both hooks live. One clause of the original entry
    was **wrong** and is corrected here: `.husky/pre-commit` already probed both
    `venv/bin/pre-commit` and `venv/Scripts/pre-commit.exe`, so the script needed
    no change and was never the obstacle. The root `package-lock.json` is now
    tracked so the gate's own tooling (husky, commitlint, prettier, lint-staged)
    is reproducible, the same reason the two frontend lockfiles are tracked.
14. ~~**There is no way to read an extracted value over HTTP, and no review
    endpoint.**~~ — **closed by F8.** `GET /review/queue`, `PATCH /review/{id}` and
    `POST /review/{id}/reveal` now cover queue → decide → audit, and the reveal
    route is the caller `decrypt_for_review` never had. A `pending_review` row is
    no longer a dead end; it is a row an operator can accept, reject, mark
    unreadable or correct, with a version-checked `409` if two reviewers collide
    and one `AuditLog` row per decision.
15. **Nothing leaves the building — closed by the SMTP transport (D10-09).** F9 gave an
    `approved` field its first consumer and F10 gave the decision its own, so the
    Phase-0 arrow chain is now real end to end *inside the database*:
    `POST /agent/decisions` writes an `AgentDecision`, `POST /notifications` writes
    a `Notification` from it, and `POST /{id}/dispatch` moves that row
    `pending` → `sent`/`failed` with retries that park exhausted rows. What no
    adapter exists for is the send itself. ~~`build_sender` returned the refusing
    stand-in and `notification_transport` had exactly one member, `none`, so
    **`sent: true` was unreachable through the API**~~ — **closed by the SMTP slice
    (D10-09):** with `NOTIFICATION_TRANSPORT=smtp` and a relay configured, a dispatch
    reaches `sent` on the relay's own 2xx, so the arrow chain now leaves the process.
    One limit remains from that report and it has since been narrowed rather than
    closed by assertion: the send path **now speaks SMTP to a real socket** in
    `test_smtp_socket.py` (a loopback relay built from `socket`, no container, no new
    dependency), so the envelope, the `DATA`/`QUIT` conversation, a re-send's
    byte-identical body, a relay refusal, the timeout ceiling and a real `STARTTLS`
    handshake with `AUTH` crossing only the encrypted socket are all observed rather
    than reasoned about.
    What is still not proven is compatibility with a *particular* provider's quirks.
    Relay authentication is **no longer** among the gaps: `starttls()` is now handed a
    verifying context (`build_ssl_context`), so a relay whose certificate the client
    cannot chain to the configured bundle or the system store is refused — proven by
    the socket suite, which fails when the bundle is withheld and passes when it is
    supplied (gap 24, closed by D10-10). Separately,
    `CustomerResponse` and `Escalation` still have **no writer**, so nothing records
    a reply or opens an SLA clock; and no *screen* talks to the real API:
    `frontend/src/app/*` still calls the mocked demo backend, so the F8, F9 and F10
    routes have only ever been called by `TestClient`.
16. ~~**The review queue paginates by `limit` only.**~~ — **closed by `c90423b`.**
    `awaiting_review()` takes an `after=ReviewCursor` keyset and the route accepts
    `?cursor=` / returns `next_cursor`, so a backlog longer than the 500-row cap is
    reachable rather than truncated: lowest-confidence-first order is preserved and
    an operator can now walk the whole queue. Paging by sort key rather than
    `OFFSET` is what makes it correct while reviewers are *deciding* — a decision
    removes a row and shifts every later one (D8-06). Two honest residues: the
    cursor is unsigned, so it must never be treated as authorising anything (it is
    a position, and the tenant predicate on the query is what protects the row), and
    `oldest_minutes` remains a max over the returned page, so only page one is a
    trustworthy "how stale is the backlog" reading. Building it also surfaced and
    fixed a `NUMERIC(4, 3)` precision defect in `confidence` that no test had
    covered before.
17. **The default `pattern` backend reaches 5 of the 7 field types.** Email, phone,
    `start_date`, `end_date` and `contract_value` have regexes; `address` and
    `dob` are reachable only through the opt-in `ollama` backend, which has no
    compose service and has never been run against a live model. So the default
    path — the one the verification above actually exercises — cannot produce a
    reviewable address, and a contract whose only contact detail is a postal
    address yields nothing usable (D7-03).
18. **Consent has no UI and no import path.** F9's flags default to `false`, so
    **every contract that existed before `0002` became un-contactable** —
    fail-closed and correct, but resolvable only through
    `PUT /contracts/{id}/consent`, one contract at a time. There is no consent
    screen and no bulk import, so this is a standing data-entry task somebody has
    to own; it is also the honest answer to "why does the decision route refuse
    everything on a fresh database". Consent and opt-out are enforced in code at
    both plan time and send time. ~~Quiet hours are enforced nowhere~~ — **retired
    by F10.** `Contract.timezone` is read at last, `zoneinfo` + `tzdata` resolve it,
    and the window (`[20:00, 08:00)` by default) is checked immediately before the
    transport is reached, on the customer's wall clock and DST-safely (D10-01,
    D10-02). The non-negotiable's third clause now has code behind it.
19. ~~**An `AgentDecision` cannot be overridden or flagged.**~~ — **closed.** Two
    halves. The structural half was `0003` (`0f5da36`), which added `version` +
    `__mapper_args__` so two operators correcting the same decision collide
    detectably. The feature half is this slice: migration `0004` adds
    `original_channel`, `overridden_by` and `rerun_count`, and the router gains
    `POST /agent/decisions/{contract_id}/override` and
    `.../flag-review` (D9-06). Both are addressed by **contract** id, matching
    `demo/backend/main.py`'s shapes, both require an operator role, both take
    `version` and answer `409 VERSION_CONFLICT` with the current row on a stale
    write and `409 NO_DECISION` when the contract has no decision. An override
    re-runs `permitted_channels` against the contract's state *now* and refuses
    `CHANNEL_NOT_PERMITTED` for anything outside it — so it redirects a permitted
    channel and never creates permission; a contract that revoked consent after
    the decision refuses the override. `original_channel` is written once, from
    what the policy chose, and never rewritten; `overridden_by` records the actor;
    each correction writes an `AuditLog` row (`agent_decision.overridden` /
    `agent_decision.flagged`) carrying the before/after state, so the edit is
    reconstructable even though the row is mutated in place. One thing remains
    open and is named rather than hidden: a re-run is still a new `POST` that
    **appends** — `rerun_count` is stamped at append time and is a snapshot, not a
    live aggregate. ~~And `demo/fixtures/agent_decisions.json` still carries field
    names (`tokens_in`, `input_signals`, …) the real model does not have, which is
    a fixture-fidelity gap rather than an unimplemented route.~~ — **closed
    since.** The fixture is now the `AgentDecisionView` shape field for field, and
    the demo backend, its `AgentDecision` type and both mutation helpers follow the
    real wire contract: `version` is sent and compared (409 `CONCURRENT_UPDATE`), a
    flag-review body cannot carry a channel, `original_channel` is written once,
    and a re-run appends instead of overwriting. The frontend now calls the routes
    that exist instead of narrating fields no route returns.
20. **`normalized_value` is plaintext PII, and it is the column the policy reads.**
    F7 encrypts `raw_value`, and the fields API never carries either column — but
    `ExtractedField.normalized_value` is a plain `Text` column holding the
    *canonical* form of the same data (a lowercased email, an E.164 phone), and
    downstream code depends on it precisely because it is readable:
    `decision.py` requires it present to pick a channel and `dispatch.py` returns
    it as the recipient. So the ciphertext on `raw_value` is shadowed by a
    cleartext copy one column over, and the "no raw PII at rest" non-negotiable is
    contradicted in fact. This is acknowledged in-code rather than discovered here
    (`test_notifications.py` names it as gap 20), but it needs a decision, not a
    patch: encrypt this column too and compare on a hash, or keep it and state the
    exception openly. It is not a migration to slip in — it changes how the decision
    and dispatch policies read fields.
21. ~~**A `Notification` cannot be locked against a concurrent dispatch.**~~ —
    **closed by `0003` (`0f5da36`).** Two mechanisms, because one was not enough:
    `version` + `__mapper_args__` makes the loser's `UPDATE` match zero rows, and
    `NotificationRepository.claim_for_dispatch` takes the row under
    `SELECT ... FOR UPDATE` *before* the policy runs, so the second worker blocks and
    then sees `sent` and refuses `NOT_PENDING` rather than reaching the transport
    twice. A version column alone would only have serialised the commit, after both
    sends had happened. The lost race now surfaces as `409 CONCURRENT_UPDATE`, not a
    500. The concurrency half of that claim is no longer unproven: gap 23 is closed
    by two tests against a real MySQL server (D10-08).
22. ~~**No scheduler polls notifications.**~~ — **closed by `4e7dd5c`**.
    `NotificationScheduler` claims `pending` rows whose `scheduled_at` is null or
    past, so a quiet-hours deferral resumes on its own. What it is **not**: there is
    still no alerting — a row parked `failed` after exhausting
    `notification_retry_max`, or `suppressed` by a policy refusal, sits there until
    an operator looks. Retry stays operator-initiated, like DLQ replay (D5-03); the
    difference is that a *deferred* row now recovers itself. The gap 23 caveat it
    inherited — a claim lock that is a no-op on SQLite — is now **closed**: on MySQL
    two schedulers are proven not to receive the same row (D10-08). On SQLite the
    no-op remains true by construction, so the offline suite still cannot prove it.
    **No alert when rows park** is still open, and it is a real weakness at GATE 2.
23. ~~**The double-send serialisation is proven by reasoning, not by a concurrent
    test.**~~ — **closed against MySQL (D10-08).** The gap was real and correctly
    stated: `0003` (`0f5da36`) makes the second dispatch block on
    `SELECT ... FOR UPDATE` and then refuse `NOT_PENDING`, but `with_for_update()`
    is a **silent no-op on SQLite**, and every session in `test_notifications.py`
    shares one in-memory connection (`StaticPool`) — a competing commit *cannot*
    land behind an in-flight request there, which is why
    `test_a_lost_race_is_reported_as_a_conflict` drives `_commit_or_conflict`
    directly. So the `version` column was proven and the lock was not.
    It is now proven by two tests that open **two real MySQL connections**:
    `test_the_dispatch_lock_blocks_a_second_worker` (B blocks — errno `1205` — then
    sees `sent`, the state that refuses `NOT_PENDING`) and
    `test_the_scheduler_claim_skips_a_row_another_worker_holds` (B is handed the
    *other* due row, proving `SKIP LOCKED` skipped A's). `4e7dd5c` had widened the
    blast radius to two callers; both callers are now covered. **What is still true:**
    on the SQLite the offline suite runs, the clause remains a no-op, so that suite
    can never be the proof — the guarantee is MySQL-specific and the integration
    suite is where it lives.
24. ~~**The SMTP transport does not authenticate the relay.**~~ — **closed by D10-10.**
    `SmtpSender.send` called `client.starttls()` with **no context** (`senders.py:174`),
    so `smtplib` negotiated TLS through its default context:
    `ssl._create_stdlib_context()` with `verify_mode=CERT_NONE` and
    `check_hostname=False` — measured, not assumed. The connection was encrypted and the
    peer was whoever answered, which then received the credentials. Now `starttls` is
    called with a context from `build_ssl_context(settings)`: `SMTP_CA_BUNDLE` replaces
    the trust store, and an unset bundle means `ssl.create_default_context()`, i.e. the
    system store — there is no path to an unchecked handshake, because `None` is
    *resolved* rather than forwarded. Settings refuses a bundle that is not a loadable
    PEM, and a bundle paired with `SMTP_STARTTLS=false` (a CA that is never consulted
    would read as "TLS on"), both at boot rather than inside the row lock where the
    context is built. The socket suite carries the proof: the same loopback relay is
    **refused** without its certificate in the bundle and accepted with it, and removing
    `context=` makes that refusal test fail while the encrypted-socket test still
    passes — which is why the encrypted-socket test alone was never the proof. **What is
    still true:** no test here has verified a public-CA certificate over a real network,
    so provider hostname and chain behaviour remains unobserved.

## Next permitted step

**F10 (step 6) is committed (`b344c17`), three gap-closing slices have landed since
(`0f5da36`, `4e7dd5c`, `c90423b`), the MySQL half that all of them deferred has now
been run (`c99c792`) — including the concurrency proof that closes gap 23 — and the
transport decision the slice ended on has been built and proven against a socket
(`6f4cb0b`, and the socket suite that followed); GATE 2 is signed (`5c3e077`).**
F1–F10 are all committed and the pipeline slice is closed — every arrow in the Phase-0
flow (ingest → extract → review → decide → notify) now has real code behind it, a due
row is reached by a background poller rather than an operator, the review backlog is
walkable end to end rather than truncated at the cap, and with
`NOTIFICATION_TRANSPORT=smtp` plus a relay configured the last arrow leaves the
process. The pipeline now **does** send, and the relay it talks to is now
*authenticated* as well as encrypted (gap 24, D10-10) — so what is left is not a
missing feature and not a hardening pass either, but three decisions (gaps 18, 19
and 20), none of which changes whether the flow runs.

- `app/db/models/` — every table in the flow now has a writer: `Contract`
  (`POST /contracts`), `ExtractedField` (`python -m app.cli extract`,
  `PATCH /review/{id}`), `AuditLog` (review, reveal, consent), `AgentDecision`
  (`POST /agent/decisions`) and `Notification` (`POST /notifications`,
  `POST /{id}/dispatch`, `POST /{id}/retry`, and now the scheduler itself, which
  dispatches without a request at all — `4e7dd5c`). `CustomerResponse` and
  `Escalation` remain writer-less (gap 15), which is why nothing records a reply or
  opens an SLA clock.
- `app/api/v1/` — `auth`, `health`, `contracts`, `agent`, `review` and now
  `notifications`. The backend is no longer *behind* the UI contract by a route; on
  the notification path it is ahead of it. No screen talks to the real API yet:
  `frontend/src/app/*` still calls the mocked demo backend, so the F8, F9 and F10
  routes have only ever been called by `TestClient`.
- Redis (rate limiting, gap 9) and Ollama are still unwired: the `ollama` backend
  is written but has no compose service, so `EXTRACTION_BACKEND=pattern` is what
  runs (gap 17). Each belongs to the slice that first needs it.

**The pipeline slice, in the order the dependencies demanded:**

1. ✅ **Done (F6).** `POST /contracts` + ingestion into `Contract` behind the F3
   principal — tenant stamped from the principal, `sha256` dedup per tenant,
   optimistic `version` respected.
2. ✅ **Done (F6).** The first real `enqueue_event` from inside that same
   transaction, so the outbox has a producer and the relay has a genuine event to
   publish.
3. ✅ **Done (F7).** Extraction into `ExtractedField`, confidence scored, values
   encrypted at rest, with the non-negotiable enforced in code: extraction can
   write only `auto_accepted` or `pending_review`, so nothing below the threshold
   can reach a notification.
4. ✅ **Done (F8).** Review endpoints behind `require_roles`, version-checked (`409`
   on a stale write, carrying the winning row), matching what the Phase-0 review
   queue mocks — plus the audited decrypt route F7 refused to fold into the list
   route. `approved` has a writer for the first time, and every write that can
   reach it leaves an `AuditLog` row.
5. ✅ **Done (F9).** Channel selection into `AgentDecision`, reading
   `blocks_pipeline` rather than re-implementing the rule and selecting only from
   `contract.consent_channels` — so an opt-out beats a grant and a `REJECTED`
   field stops the pipeline before a channel is chosen. Refusals are `409`s with
   stable codes (`PIPELINE_BLOCKED`, `CONTACT_REFUSED`, `NO_CONSENT`,
   `NO_CONTACT_VALUE`, `CONTRACT_CLOSED`), and `reasoning` is assembled from
   channel names only, never a value. Consent itself became real: `0002` adds
   `do_not_contact` + three per-channel flags to `Contract`, set through
   `PUT /contracts/{id}/consent`, version-checked and audited.

6. ✅ **Done (F10).** Notification dispatch behind a transport seam:
   `app/workers/dispatch.py` selects the innermost due milestone (`T-60` / `T-30` /
   `T-14`), enforces quiet hours on the customer's wall clock via `zoneinfo` +
   `tzdata` (DST-safe), re-reads consent at send time, and moves the row
   `pending` → `sent`/`failed` under a unique idempotency key. It imports
   `blocks_pipeline` and the consent refusals from F9 rather than restating them.
   `MessageSender` has two implementations: `UnavailableSender` — the shipped
   default, which refuses — and `SmtpSender` (`D10-09`), which delivers over a real
   relay and reports `sent` only on a 2xx. The SMTP path is proven against a fake
   client **and** against a real loopback socket, certificate verification included
   (`D10-10`); a real provider is the thing still unobserved.

~~**Next, and it is a decision rather than a build order.**~~ — **decided: SMTP
(D10-09).** The pipeline is no longer complete-inside-only. With
`NOTIFICATION_TRANSPORT=smtp` and a relay configured, a due row reaches a customer and
`sent` is recorded only after `DATA` was accepted. The three consequences that the
choice opened are answered rather than pending: a send still emits **no**
`contract.events` message, so nothing downstream learns that contact happened — a
deferral, not an oversight; a provider failure maps onto the existing `failed` /
`attempts` / `notification_retry_max` rules unchanged, because the adapter raises the
`SenderError` the policy already understood; and `sent` is asserted only on the relay's
own 2xx, which is the honest default this paragraph predicted. The scheduler
(`4e7dd5c`) is what made the decision urgent: it reaches a due row with no operator in
the loop, so the moment an adapter was configured the poller would start calling it.
That is now the shipped behaviour whenever SMTP is on.

~~Close the harness gate first (gap 13).~~ — **done** in `4a9e8e9`. `core.hooksPath`
is `.husky/_` and both hooks are live; the wiring was proven by a commit that git
*refused*, not by reading config. Every commit from here on is gated for real.

One alternative remains, and it is gap-sized rather than milestone-sized:

- **Decide what to do about `normalized_value` (gap 20).** This was a three-gap
  schema cluster; gap 21 was closed by `0003` (`0f5da36`) and gap 19 by `0003` plus
  `0004` (the `feat(g19)` slice), so one question is left and it is not a migration
  to slip in. `ExtractedField.raw_value`
  is Fernet ciphertext, but `normalized_value` — the canonical email or E.164 phone
  the decision and dispatch policies actually read — is a plain `Text` column, so
  the "no raw PII at rest" non-negotiable is contradicted one column over. It needs
  a decision (encrypt it and compare on a hash, or keep it and record the exception
  openly), because either answer changes how those two policies read fields.

**Open at GATE 2 — say these out loud, do not bury them:** no Kafka broker has
ever been on the other end of a publish (no compose service; `live` test opt-in);
no consumer exists; DLQ replay is operator-initiated with no alerting, and
notification **retry** is too — the scheduler (`4e7dd5c`) now recovers a *deferred*
row by itself, but a row parked `failed` or `suppressed` still sits until a human
looks, and nothing alerts when it does (the residue of gap 22); no live
Ollama has answered anything; and the `review`, `agent` and `notification` routes
have only ever been called by `TestClient`, because no screen talks to them yet.
**Nothing has ever been sent to a real customer** (gap 15): the shipped default
transport still refuses, and the SMTP path has only ever spoken to a loopback relay
this suite started and certified itself — so `sent: true` over HTTP is proven against
a relay of our own making, never against a provider, and never through a handshake
chaining to a public CA over a real network (D10-10). From F9: **every contract that
predates `0002` is un-contactable** until someone sets consent one row at a time
(gap 18), and ~~an `AgentDecision` still **cannot be overridden or flagged** — the
`version` column that blocked those routes now exists (`0f5da36`), but the routes
themselves still do not (gap 19)~~. **The struck clause is amended, not deleted:**
the `feat(g19)` slice added both routes on top of migration `0004`, so an
`AgentDecision` can now be overridden or flagged under a `version` check with an
audit row behind it. Two narrow residues replace it, both recorded in gap 19 — a
re-run **appends** a new decision rather than editing this one, so `rerun_count` is
a snapshot taken at append time rather than a live aggregate, and
`demo/fixtures/agent_decisions.json` still carries field names the real model does
not have. The review queue pages now (`c90423b`, gap 16), so
the backlog is reachable rather than truncated at 500 — but its `oldest_minutes` is
only trustworthy on page one, and its cursor is a position rather than a capability,
so neither should be presented to an operator as more than it is. Quiet hours — the
caveat F9 had to carry — are now enforced (D10-01/D10-02), so that one is retired
rather than repeated. Two gate reports in this project were wrong the first time and
both are recorded as such: F9's first "MySQL gate green" carried a bad password and
skipped, and F10's two MySQL rows **were unrun at the F10 commit** because no Docker
daemon answered then. That has since changed, and the correction cuts the other way:
the suite builds tables from `Base.metadata`, so `alembic check` is the only thing
that proves a migration matches the models — it was green on SQLite for `0003` and
is now **green on MySQL too** ("No new upgrade operations detected."), so `0003` is
a revision a real database has validated. The `confidence` quantiser from `c90423b`
was the one claim that could only be settled there, and MySQL confirms it is a
**no-op**: the declared scale was already enforced. The dispatch lock is no longer
unproven under concurrency either — two integration tests show the second worker
blocking and then seeing `sent`, and two schedulers being handed different rows. What
remains true is that this is **MySQL-only evidence**: `FOR UPDATE` is still a no-op on
the SQLite the offline suite runs, so that suite can never be the proof, and a skip
there is not a pass.

One caveat carried forward rather than retired: the *number* of sends is now safe on
MySQL, but a row that parks `failed` or `suppressed` still raises no alert (gap 22).
Correctness under concurrency and visibility of failure are different problems, and
only the first one moved.

## GATE 2 — signed

**Signed 2026-11-04 at `5c3e077`**, by the project owner, on the report below.
GATE 2's exit (README) is "foundation verification + feature menu": this section is
the verification, and the menu is Phase 2.

What it added over the earlier per-milestone reports is not a new claim but a
**re-run**: every row below was produced against the tree at `5c3e077` with the
MySQL 8.4 container live on `127.0.0.1:3307`, and each was run again before the
sign-off was committed. The earlier records (`c99c792` and each milestone's own
table) were not copied — a gate report in this project has been wrong before by
being carried forward, and F9's first "MySQL gate green" skipped while claiming to
pass.

| Check | Command | Result |
| --- | --- | --- |
| Full suite, **real MySQL 8.4** | `pytest tests` with `MYSQL_TEST_URL` on `:3307` | **464 passed, 1 skipped**, exit 0 |
| Integration only | `pytest -m integration tests` | **12 passed**, 453 deselected |
| Full suite, SQLite | `pytest tests`, `MYSQL_TEST_URL` **unset** | **458 passed, 7 skipped** |
| Migrations on MySQL | `alembic upgrade head && alembic check` | `0001 → 0002 → 0003`, **`0003 (head)`**, "No new upgrade operations detected." |
| Lint | `ruff check .` (repo root) | All checks passed |
| Format | `ruff format --check .` (repo root) | **82 files** already formatted |
| Types | `mypy app` (strict) | clean, **49 source files** |
| Security lint | `bandit -c pyproject.toml -r backend/app` | **0 issues** |

**Head has moved since this table was produced, and the row above is left as
measured.** Every row records the tree at `5c3e077`, when head was `0003`. The
`feat(g19)` slice added `0004`, so that row is a date, not a description of the
current tree. Re-measured after it, against a **freshly created** MySQL scratch
database: `0001 → 0002 → 0003 → 0004` (exit 0), `alembic check` → "No new upgrade
operations detected.", `alembic current` → **`0004 (head)`**. See *Operator
overrides and flag-review* below for the full table.

The two suites reconcile rather than being two numbers: the total is **465** either
way. Offline, 6 MySQL-dependent tests plus the opt-in `KAFKA_LIVE` test skip (7);
online, the same 6 run and only `KAFKA_LIVE` skips (1). *A skip is not a pass* —
which is exactly why the MySQL column was executed rather than declared, and why the
migration check ran against a **freshly created** scratch database
(`ca_alembic_check`), not the one the integration tests had just rewritten.

Reproduce (PowerShell — there is **no `.env`** in this repo, so the credentials are
read back out of the running container, and the port is read from the publish map
rather than assumed: compose defaults to `:3306`, but this stack was started with
`MYSQL_PORT=3307`, which is why every `:3306` URL written earlier in this file
connects to nothing):

```powershell
$e = docker inspect contract-agent-mysql-1 --format '{{json .Config.Env}}' | ConvertFrom-Json
$u = (($e | ? { $_ -like 'MYSQL_USER=*' }) -replace '^MYSQL_USER=','')
$p = (($e | ? { $_ -like 'MYSQL_PASSWORD=*' }) -replace '^MYSQL_PASSWORD=','')
$port = (docker port contract-agent-mysql-1 3306/tcp) -replace '.*:(\d+)$','$1'
$env:MYSQL_TEST_URL = "mysql+pymysql://${u}:${p}@127.0.0.1:${port}/contract_agent_test?charset=utf8mb4"
venv\Scripts\python.exe -m pytest backend\tests
```

The scratch database must already exist — the app user **lacks** `CREATE DATABASE`
(measured: `ERROR 1044` for `contract_agent`), so `ca_alembic_check` was created as
root and dropped afterwards. Inside the container, pass the password as `MYSQL_PWD`
rather than with `-p`: PowerShell mangles `-p<value>`, and MySQL then answers
`Access denied for user '$u'` — which reads like a bad password but is a quoting bug.

Run the checks from the repository root as `.\venv\Scripts\python.exe -m …`. Writing
`..\venv\Scripts\python.exe` at root resolves to `D:\Projects\venv` — an unrelated
environment that does exist on this machine and carries ruff **0.8.6**, which reports
2 errors and 2 unformatted files that this project's pinned ruff **0.17.3** does not.
The "intermittent lint failure" recorded earlier was this path, not the code: resolve
a green-that-turns-red to its interpreter before believing it either way.

### What is signed, and what is not

Signed: the foundation **works and is proven where it was claimed** — ingestion →
outbox → relay, auth and tenant isolation from the principal, encryption at rest,
review gating, deterministic channel selection, dispatch with consent/quiet-hours,
a scheduler that reaches a due row, and an SMTP transport that authenticates the
relay it hands credentials to. The MySQL-only guarantees (the dispatch lock, the
scheduler claim skipping a held row, real `ENUM`s, the `NUMERIC(4,3)` scale, and a
migration set that matches the models) are proven against a real MySQL 8.4 server.

**Not signed, and carried forward as open — these are the conditions of the
sign-off, not footnotes:**

1. **No Kafka broker has ever been on the other end of a publish**, and **no
   consumer exists**. The wire path is a contract test (`FakeProducer`); `KAFKA_LIVE`
   skips. D4-03's accepted risk stands: a misconfigured production relay parks
   events rather than failing the deploy.
2. **No alert fires when a row parks** (`failed`) or is `suppressed`, and outbox
   replay is operator-initiated (D5-03, gap 22's residue). Correctness under
   concurrency moved at `c99c792`; *visibility of failure did not*.
3. **Nothing has ever been sent to a real customer.** The shipped default transport
   refuses; SMTP has only spoken to a loopback relay this suite started and
   certified itself, so no handshake has chained to a public CA over a real network
   (D10-10). `sent: true` over HTTP is proven against a relay of our own making.
4. **No live Ollama has answered anything** (gap 17); `EXTRACTION_BACKEND=pattern`
   is what runs.
5. **No screen talks to the real API.** `frontend/` still calls the mocked demo
   backend, so the `review`, `agent` and `notification` routes have only ever been
   called by `TestClient`.
6. **`normalized_value` is still plaintext** (gap 20) — the one place a shipped
   column contradicts "no raw PII at rest", and it needs a decision (encrypt +
   compare on a blind index, or keep it and record the exception), not a patch.
7. ~~**Every contract predating `0002` is un-contactable** until consent is set row
   by row (gap 18), and an `AgentDecision` still cannot be overridden or flagged
   (gap 19) even though the `version` column those routes need now exists.~~
   **Amended after the sign-off, not deleted from it:** the gap 18 half stands
   exactly as signed. The gap 19 half is superseded by the `feat(g19)` slice, which
   added override and flag-review on top of migration `0004`. The two residues that
   replace it are narrower and are carried here rather than buried — a re-run
   appends a new decision instead of updating this one, so `rerun_count` is a
   snapshot at append time, and ~~the demo fixture still names fields the real
   model does not have~~ — **that second residue is now closed too**: the demo
   fixture, its `AgentDecision` type, its mutation helpers and
   `demo/backend/main.py` all follow the real wire contract. One residue remains,
   the `rerun_count` snapshot named first, and it is deliberate.
8. **`FOR UPDATE` is a no-op on SQLite.** The concurrency guarantees are
   MySQL-only evidence by construction; the offline suite can never be the proof.
9. **Missing pieces by design, not by defect:** `CustomerResponse` and `Escalation`
   remain writer-less (gap 15) — nothing records a reply or opens an SLA clock.

None of 1–9 is a green check described as complete. Each is visible in the code and
in this file, which is the standard this project has held gates to: a caveat stated
out loud is acceptable; a caveat buried under a green board is not.

## Non-negotiable reminders

- No low-confidence field may reach the notification pipeline.
- No cloud LLM; Ollama local only.
- Daily-scan + date compare; never a delayed 60-day message.
- Outbox + idempotency for every side effect; DLQ with replay, never log-and-drop.
- No channel switch that bypasses consent, opt-out or quiet hours.
- No raw PII in logs, traces, Ollama payloads or MLflow.
- Tenant identity comes from the authenticated principal, never from the client.
