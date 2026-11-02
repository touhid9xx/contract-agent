# Status

Last updated: Phase 1 (real foundation) — **F1 committed, F2 in progress**.

## Phase history

| Phase | State |
| --- | --- |
| Phase 0 — mocked demo | ✅ built, verified |
| GATE 1 — human visual approval | ✅ **approved** ("I'm happy with demo functions and look") |
| Phase 1 — real foundation (F1–F7) | 🔄 in progress |
| GATE 2 — foundation verification | ⏳ pending |

## F1 — Repo & infra ✅

| Area | Detail |
| --- | --- |
| Python tooling | `pyproject.toml` — ruff (lint + format), `mypy --strict`, pytest + coverage, bandit |
| Pre-commit | `.pre-commit-config.yaml` — hygiene, ruff, ruff-format, mypy, bandit, frontend typecheck; gitleaks bound to `manual` (D1-03) |
| Commit gate | husky `pre-commit` + `commit-msg`, commitlint (Conventional Commits), lint-staged |
| Backend | `app/core/config.py` fail-fast settings, `app/core/logging.py` structlog + PII redaction, `app/core/middleware.py` request/trace IDs, `app/main.py` factory + error envelope, `/api/v1/health` |
| Containers | `Dockerfile.backend` (non-root, tini, read-only rootfs), `docker-compose.yml` (api only — DB/Redis/Kafka land in F2/F4) |
| Frontend | Next.js 16 + Tailwind 4 + TanStack Query + Zustand + next-themes + next-intl (en, bn), ESLint flat config, Prettier, `[locale]` routing |
| Scripts | `scripts/bootstrap.mjs` (one-command setup), `scripts/run-frontend-check.mjs` |
| Env | `.env.example` documents every setting read by `Settings` |

## Verification actually run (F1)

| Check | Command | Result |
| --- | --- | --- |
| Backend unit/integration tests | `venv\Scripts\python.exe -m pytest backend/tests -q` | **33 passed** |
| Lint | `ruff check backend` | pass |
| Format | `ruff format --check backend` | 14 files already formatted |
| Types | `mypy backend/app` (strict) | Success: no issues in 9 source files |
| Security lint | `bandit -c pyproject.toml -r backend/app` | 0 issues (all severities) |
| Frontend typecheck | `npm run typecheck` | pass |
| Frontend lint | `npm run lint` | pass |

## Known gaps / issues

1. **`gitleaks` needs Docker** and is manual-stage only (D1-03). Run it with
   `pre-commit run gitleaks --all-files` before pushing, or rely on CI.
2. **`make` is optional** — not installed on the primary Windows host; `npm`
   scripts are the contract (D1-09).
3. **Docker Compose currently has one service** (`api`). MySQL, Redis, Kafka and
   Ollama are added in F2/F4 as the code that uses them lands — shipping idle
   containers now would misrepresent readiness.
4. **`/health/ready` does not exist yet** — it arrives in F5 once there are
   dependencies to check. `/api/v1/health` is liveness only.

## Next permitted step

F2 — MySQL 8 + SQLAlchemy 2.0 + Alembic, tenant-scoped core tables, outbox
table and the relay worker that publishes to Kafka/StrikeMQ.

## Non-negotiable reminders

- No low-confidence field may reach the notification pipeline.
- No cloud LLM; Ollama local only.
- Daily-scan + date compare; never a delayed 60-day message.
- Outbox + idempotency for every side effect; DLQ with replay, never log-and-drop.
- No channel switch that bypasses consent, opt-out or quiet hours.
- No raw PII in logs, traces, Ollama payloads or MLflow.
- Tenant identity comes from the authenticated principal, never from the client.
