# Status

Last updated: Phase 0 (mock demo) — **waiting for GATE 1**.

## Current phase

**Phase 0 — Interactive Demo.** Built, runnable and verified locally.
**Not yet approved.** Phase 1 (F1–F7) must not start until the human says
_"I'm happy with demo functions and look"_ (GATE 1).

## Completed

| Area | Detail |
| --- | --- |
| Repo hygiene | `.gitignore`, root `package.json`, `README.md`, `DEMO.md`, `DECISIONS.md`, `STATUS.md` |
| Mocked backend | `demo/backend/main.py` — FastAPI, in-memory state, `/api/v1/*`, `X-Mocked: true` on every response, deterministic latency, optimistic-locking 409, blocked-pipeline 409 |
| Fixtures | `demo/fixtures/` — contracts (11 rows + totals), extracted fields, agent decisions, notifications, escalations, timeline, dashboard, users |
| Frontend | Next.js 16 + Tailwind 4 + TanStack Query + Zustand, 8 routes, dark mode, keyboard shortcuts, toasts, skeletons, empty/error states, `MOCKED` badges |
| Launcher | `scripts/demo.mjs` — `npm run demo`, port auto-selection, health wait, clean shutdown |
| Docs | `DEMO.md` screen → milestone map, walkthrough, mocked-signal table |

## Verification actually run

| Check | Command | Result |
| --- | --- | --- |
| TypeScript | `npx tsc --noEmit` (in `demo/frontend`) | pass, exit 0 |
| Production build | `npm run build` (in `demo/frontend`) | pass, 8/8 routes, exit 0 |
| Backend smoke | `GET /health`, `GET /api/v1/contracts` @ `:8000` | `200`, `200` |
| Contract detail / review / notifications / escalations | `GET /api/v1/*` | `200` each |
| Blocked pipeline | `POST /api/v1/agent/decisions/{id}/rerun` on an unreviewed contract | `409` as designed |

## Known gaps / issues

1. **`pre-commit` not runnable yet** — `.pre-commit-config.yaml` is an F1
   deliverable, and Phase 1 may not start before GATE 1. See `DECISIONS.md`
   D0-08 for the proposed resolution.
2. **No backend tests yet** — Phase 0 ships no pytest suite; the mocked service
   is validated by the route smoke tests above. The spec's coverage targets
   (backend ≥ 85 %, etc.) apply from Phase 1.
3. **Port 3000 may be busy** on a machine running Docker; the launcher now walks
   to the next free port and prints the URL to use (`DECISIONS.md` D0-07).
4. **Upload / voice audio are intentionally inert** — upload button disabled
   with a tooltip (`P2-A`); voice events are text fixtures (`P2-F`).

## Next permitted step

Human reviews the demo at GATE 1 and replies **"I'm happy with demo functions
and look"**. Only then: Phase 1, F1 — repo & infra (ruff, pre-commit, husky,
docker, settings, structlog, `/health`, frontend skeleton).

## Non-negotiable reminders for the next phase

- No low-confidence field may reach the notification pipeline.
- No cloud LLM; Ollama local only.
- Daily-scan + date compare; never a delayed 60-day message.
- Outbox + idempotency for every side effect; DLQ with replay, never log-and-drop.
- No channel switch that bypasses consent, opt-out or quiet hours.
- No raw PII in logs, traces, Ollama payloads or MLflow.
