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
