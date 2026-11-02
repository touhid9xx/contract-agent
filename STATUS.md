# Status

Last updated: Phase 1 (real foundation) — **F1 committed (`f0e1eef`), F2 committed
(`331a7b4`) plus one follow-up fix to the MySQL test harness (`a79d893`), F3
committed (`8e6b927`) plus the bootstrap-path doc (`908f50d`), F4 committed
(`b425ea7`), F5 (readiness + DLQ replay) with every gate executed and green.
GATE 2 now awaits human sign-off.**

## Phase history

| Phase | State |
| --- | --- |
| Phase 0 — mocked demo | ✅ built, verified |
| GATE 1 — human visual approval | ✅ **approved** ("I'm happy with demo functions and look") |
| Phase 1 — real foundation | 🔄 in progress (F1 ✅, F2 ✅, F3 ✅, F4 ✅, F5 ✅) |
| GATE 2 — foundation verification | ⏳ pending human sign-off; every automated check is green |

## F1 — Repo & infra ✅

| Area | Detail |
| --- | --- |
| Python tooling | `pyproject.toml` — ruff (lint + format), `mypy --strict`, pytest + coverage, bandit |
| Pre-commit | `.pre-commit-config.yaml` — hygiene, ruff, ruff-format, mypy, bandit, frontend typecheck, frontend eslint; gitleaks bound to `manual` (D1-03) |
| Commit gate | husky `pre-commit` + `commit-msg`, commitlint (Conventional Commits), lint-staged |
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
| Commit | `git commit -F .agent-cache/f4-commit-msg.txt` | **`b425ea7`**, husky hooks active, no `--no-verify` |

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
> (`fix(f2): report the real cause when the mysql tests skip`), so running the
> same `-m integration` command today reports **9 passed** — the F4 row above —
> with the three real-MySQL tests unchanged.

```powershell
docker compose up -d mysql
# compose has NO default MYSQL_PASSWORD, so this must match the .env it was started with.
$env:MYSQL_TEST_URL = "mysql+pymysql://contract_agent:<MYSQL_PASSWORD>@127.0.0.1:3306/contract_agent_test"
venv\Scripts\python.exe -m pytest -m integration backend/tests
```

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

## Next permitted step

**F5 is complete and gate-green; GATE 2 awaits human sign-off.** F1–F5 are all
committed. The remaining work inside Phase 1 is the pipeline slice below, which
the repo names unambiguously — this is no longer a guess.

- `app/db/models/` — **the largest remaining gap.** `Contract`, `ExtractedField`,
  `AgentDecision`, `Notification`, `CustomerResponse`, `Escalation` and
  `AuditLog` all exist with **no route, service or worker that writes them**.
  F5 made the outbox recoverable, but nothing enqueues an event from a business
  action yet, so no domain event has ever travelled end-to-end.
- `app/api/v1/` — only `auth` and `health` routers exist. There is no
  `/contracts` route, no review route, and no notification route, while
  `frontend/src/lib/api.ts` and the Phase-0 fixtures already assume all three
  exist. **The UI contract is ahead of the backend by roughly a milestone.**
- Redis (rate limiting, gap 9) and Ollama (extraction, the non-negotiable
  "local LLM only" rule) are still unwired, and each belongs to the slice that
  first needs it.

**Next slice (F6), in the order the dependencies demand:**

1. `POST /contracts` + ingestion into `Contract` behind the F3 principal —
   tenant stamped from the principal, `sha256` dedup per tenant, optimistic
   `version` respected.
2. The first real `enqueue_event` from inside that same transaction, so the outbox
   finally has a producer and the relay has a genuine event to publish.
3. Extraction into `ExtractedField` — Ollama-backed, confidence scored, with the
   non-negotiable that nothing below the review threshold reaches a notification.
4. Review endpoints behind `require_roles`, version-checked (409 on stale write),
   matching what the Phase-0 review queue already mocks.

Steps 1–2 are the smallest slice that makes the outbox end-to-end real, so if the
scope needs trimming, start there and treat 3–4 as the following milestone.

**Open at GATE 2 — say these out loud, do not bury them:** no Kafka broker has
ever been on the other end of a publish (no compose service; `live` test opt-in);
no consumer exists; DLQ replay is operator-initiated with no alerting.

## Non-negotiable reminders

- No low-confidence field may reach the notification pipeline.
- No cloud LLM; Ollama local only.
- Daily-scan + date compare; never a delayed 60-day message.
- Outbox + idempotency for every side effect; DLQ with replay, never log-and-drop.
- No channel switch that bypasses consent, opt-out or quiet hours.
- No raw PII in logs, traces, Ollama payloads or MLflow.
- Tenant identity comes from the authenticated principal, never from the client.
