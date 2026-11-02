# Status

Last updated: Phase 1 (real foundation) — **F1 committed (`f0e1eef`), F2 committed
(`331a7b4`) plus one follow-up fix to the MySQL test harness (`a79d893`), F3
shipping in the commit this file is part of**.

## Phase history

| Phase | State |
| --- | --- |
| Phase 0 — mocked demo | ✅ built, verified |
| GATE 1 — human visual approval | ✅ **approved** ("I'm happy with demo functions and look") |
| Phase 1 — real foundation | 🔄 in progress (F1 ✅, F2 ✅, F3 ✅) |
| GATE 2 — foundation verification | ⏳ pending |

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
4. **`/health/ready` does not exist yet** — `/api/v1/health` is liveness only.
5. **The Kafka transport is not implemented.** The relay ships with
   `UnavailablePublisher`, which raises, so rows retry with backoff and then park
   as failed for an operator. Nothing is silently dropped, but nothing is
   delivered either — the outbox is wired end-to-end except the wire itself.
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

## Next permitted step

**F3 is complete and F4 is still undefined in this repository.** The F3…F7
breakdown lives in the task specification, not in the repo — `README.md`,
`DECISIONS.md`, `STATUS.md` and `DEMO.md` name F1/F2 only. F3 was built because
its scope (authentication + tenant binding) was confirmed explicitly in-conversation
rather than guessed; the same confirmation is needed for F4 before more code is
written.

The repository does contain indirect hints about the remaining milestones, enough
to propose a scope but **not** enough to treat as a specification:

- `docker-compose.yml` — "Redis/Kafka/Ollama arrive with F4/F5".
- `app/api/v1/health.py` — readiness "deliberately omits … pending F5".
- `app/workers/outbox_relay.py` — `UnavailablePublisher` is explicitly the
  stand-in "until a real transport lands in F4" (D2-04). Every published event
  currently retries and then parks as failed, so **no domain event has ever been
  delivered end-to-end.**
- `app/db/models/` — the schema is ahead of the code: `Contract`,
  `ExtractedField`, `AgentDecision`, `Notification`, `CustomerResponse`,
  `Escalation` and `AuditLog` all exist with no route, service or worker that
  writes them.

**Proposed F4, pending confirmation:** the ingestion → extraction → review
pipeline behind the F3 principal (`POST /contracts`, PDF/text ingestion into
`Contract`, extraction into `ExtractedField`, the review endpoints the Phase-0
demo mocks, and a real `EventPublisher` so the outbox finally delivers). This is
the smallest slice that makes the schema stop being dead weight and reuses the
`blocks_pipeline` rule already modelled in F2.

## Non-negotiable reminders

- No low-confidence field may reach the notification pipeline.
- No cloud LLM; Ollama local only.
- Daily-scan + date compare; never a delayed 60-day message.
- Outbox + idempotency for every side effect; DLQ with replay, never log-and-drop.
- No channel switch that bypasses consent, opt-out or quiet hours.
- No raw PII in logs, traces, Ollama payloads or MLflow.
- Tenant identity comes from the authenticated principal, never from the client.
