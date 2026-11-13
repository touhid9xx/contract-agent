# Contract Expiry Notification Agent

Contract-lifecycle automation: contract PDF ingestion, AI field extraction with
per-field confidence, human review of low-confidence fields, channel selection
(email / SMS / voice), T-60 / T-30 / T-14 reminders, two-week response tracking
and escalation to a human operator.

> **Status: Phase 1 complete — GATE 1 and GATE 2 both signed.**
> The real application in `backend/` and `frontend/` runs against MySQL, writes every
> side effect through a transactional outbox, authenticates real users with Argon2id
> and rotating refresh tokens, seals PII with Fernet, extracts contract fields, gates
> them behind human review, and dispatches a reminder over a real authenticated SMTP
> socket. ~~**Phase 2 (P2-A … P2-L) has not started.**~~ **It has: seven of the twelve
> milestones have landed slices** — P2-A, P2-B, P2-C, P2-D, P2-E, P2-G and P2-J — recorded
> part by part in `STATUS.md`. What has no code at all is P2-F (voice and answering-machine
> detection), P2-H (the pull-mode contracts API) and P2-I's dashboard, timeline and kanban
> views; P2-K and P2-L are specified nowhere but the range in this file, so nothing can be
> built against them yet. The struck clause was true when written and became false the
> commit the first `P2` slice landed, which is why it is struck rather than deleted.
>
> What is still missing, stated plainly rather than left to be discovered: there is
> **no Kafka broker and no consumer**. ~~**no CI** — gating runs from `pre-commit` on a
> developer's machine — and **7 of the 9 web screens have no test**.~~ Struck, not
> deleted, because both of those clauses had been false for 37 commits while they stood
> here (`D10-70`): the sentence was written when two specs existed and became false the
> commit a third landed. `.github/workflows/ci.yml` runs **seven jobs** — python static,
> backend pytest, MySQL 8.4 integration, frontend typecheck/eslint/vitest/build, demo
> frontend typecheck, hygiene + `gitleaks`, and a `gate` job that fails when any of them
> does — and **every web screen has a test**; the only `page.tsx` without a spec is the
> root redirect, and `frontend/src/app/screens.test.ts` now fails if a screen is ever
> added without one. What survives of the CI half is narrower: it has been observed
> passing and failing, never *gating* a merge, because required status checks are a
> repository setting no unauthenticated read can see. See [`STATUS.md`](STATUS.md) for
> per-feature detail and [`DECISIONS.md`](DECISIONS.md) for the reasoning behind each
> choice.
>
> Struck, not deleted, because it was true when written and is now false in every
> clause: ~~*Status: Phase 0 — interactive mocked demo, awaiting GATE 1 approval.
> Nothing in this repository talks to a real database, Kafka, Ollama, SMTP, SMS
> gateway or voice provider yet.*~~

---

## Run the application

The real stack is `backend/` + `frontend/` and needs MySQL, an `ENCRYPTION_KEY` and
a `SECRET_KEY` (see `backend/app/core/config.py`, which refuses to start on unsafe
settings rather than degrading). Bring it up with Docker Compose, then run the
frontend against it. Migrations are Alembic, currently at head `0006`.

## Run the demo

The Phase-0 mocked demo is kept for reference — it is not the application and is not
wired to the real API.

```bash
npm run demo
```

Then open the URL printed in the banner (defaults to <http://localhost:3000>,
falling back to the next free port if `3000` is taken).

First-time setup:

```bash
python -m venv venv
venv/Scripts/pip install -r demo/backend/requirements.txt   # Windows
# venv/bin/pip install -r demo/backend/requirements.txt     # macOS / Linux
npm --prefix demo/frontend install
```

Full walkthrough, screen list and the screen → milestone map:
[`DEMO.md`](DEMO.md).

---

## Repository layout

| Path | Purpose | Phase |
| --- | --- | --- |
| `backend/` | Real FastAPI app: auth, contracts, review, agent, notifications, escalations | 1 |
| `frontend/` | Next.js UI (`en`, `bn`), wired to the real API | 1 |
| `demo/backend/` | Mocked FastAPI service serving JSON fixtures, no DB | 0 |
| `demo/fixtures/` | Story-driven synthetic data (contracts, fields, decisions, …) | 0 |
| `demo/frontend/` | Next.js 16 + Tailwind 4 UI (real, pointed at the mocked API) | 0 |
| `scripts/demo.mjs` | One-command launcher (uvicorn + next dev) | 0 |
| `DEMO.md` | Demo walkthrough + screen → milestone map | 0 |
| `DECISIONS.md` | Architecture decisions, assumptions, open conflicts | all |
| `STATUS.md` | What is done, what is next, known issues | all |

The real application **is** `backend/` and `frontend/`: Phase 1 (F1–F10) is committed
and signed off at GATE 2 (`5c3e077`). `demo/` is kept as the Phase-0 artefact it was
— it is no longer how the application is run, and nothing in it is wired to the API
that matters.

---

## Delivery workflow (from the build spec)

| Phase | Content | Gate |
| --- | --- | --- |
| 0 | Interactive mocked demo — every screen, all states, one command | **GATE 1** — ✅ approved |
| 1 | Real foundation F1–F10 (infra, DB/outbox, auth, bus, observability, review, decisions, dispatch, PII encryption) | **GATE 2** — ✅ signed (`5c3e077`) |
| 2 | P2-A … P2-L features, one milestone per commit, human picks the order | **GATE 3** — in progress: P2-A, -B, -C, -D, -E, -G, -J have landed slices; P2-F, P2-H and P2-I's new views none; P2-K/-L unspecified (see `STATUS.md`) |

Rules that are not negotiable anywhere in this repo:

- no notification on low-confidence extraction without human review;
- no cloud LLM — Ollama local only;
- no "delayed 60-day message" scheduling — daily scan + date compare only;
- outbox + idempotency for every side effect, never log-and-drop an event;
- no channel switch may bypass consent, opt-out or quiet hours;
- no raw PII in logs, traces, Ollama payloads or MLflow.

---

## Synthetic data notice

Every name, phone number, email address and contract in this repository is
fictional (`admin@acme.test`, `Acme Ltd (fictional)`, `+8801******`). Phone and
address values are masked at the fixture level. Never replace fixtures with real
customer data.
