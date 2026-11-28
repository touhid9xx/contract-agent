# Contract Expiry Notification Agent

Contract-lifecycle automation: contract PDF ingestion, AI field extraction with
per-field confidence, human review of low-confidence fields, channel selection
(email / SMS / voice), T-60 / T-30 / T-14 reminders, two-week response tracking
and escalation to a human operator.

> **Status: Phase 1 complete — GATE 1 and GATE 2 both signed; GATE 3 signed for ten of
> Phase 2's twelve milestones, with `P2-K` and `P2-L` not signed.**
> The real application in `backend/` and `frontend/` runs against MySQL, writes every
> side effect through a transactional outbox, authenticates real users with Argon2id
> and rotating refresh tokens, seals PII with Fernet, extracts contract fields, gates
> them behind human review, and dispatches a reminder over a real authenticated SMTP
> socket. ~~**Phase 2 (P2-A … P2-L) has not started.**~~ **It has: ten of the twelve
> milestones have landed slices** — P2-A, P2-B, P2-C, P2-D, P2-E, P2-F, P2-G, P2-H,
> P2-I and P2-J — recorded part by part in `STATUS.md`. What has no code at all is
> nothing. P2-I is complete: its timeline, board and dashboard landed as `D10-135`,
> `D10-136` and `D10-137`, P2-H's pull-mode contracts API as `D10-143`, and P2-F's ARI
> dialler as `D10-144` — a dialler, not an answering-machine detector, which is a Stasis
> application this build does not contain. P2-K and P2-L are specified nowhere but the
> range in this file, so nothing can be built against them yet. The struck clause was
> true when written and became false the commit the first `P2` slice landed, which is
> why it is struck rather than deleted.
>
> What is still missing, stated plainly rather than left to be discovered: there is
> **no Kafka broker and no consumer**. ~~**no CI** — gating runs from `pre-commit` on a
> developer's machine — and **7 of the 9 web screens have no test**.~~ Struck, not
> deleted, because both of those clauses had been false for 37 commits while they stood
> here (`D10-70`): the sentence was written when two specs existed and became false the
> commit a third landed. `.github/workflows/ci.yml` runs **ten jobs** — python static,
> backend pytest, MySQL 8.4 integration, frontend typecheck/eslint/vitest/build, demo
> frontend typecheck, hygiene + `gitleaks`, an `observability` job that validates the
> Prometheus config and its alert rule, an `image` job that builds the container and runs
> every entry point, a `browser` job that boots the real stack and drives Chromium through
> every screen and the signed-in journey, and a `gate` job that fails when any of them does
> — and **every web screen has a test**; the only `page.tsx` without a spec is the root
> redirect, and `frontend/src/app/screens.test.ts` now fails if a screen is ever added
> without one.
> What survives of the CI half is narrower: it has been observed passing and failing,
> never *gating* a merge, because required status checks are a repository setting no
> unauthenticated read can see. See [`STATUS.md`](STATUS.md) for per-feature detail and
> [`DECISIONS.md`](DECISIONS.md) for the reasoning behind each choice.
>
> Struck, not deleted, because it was true when written and is now false in every
> clause: ~~*Status: Phase 0 — interactive mocked demo, awaiting GATE 1 approval.
> Nothing in this repository talks to a real database, Kafka, Ollama, SMTP, SMS
> gateway or voice provider yet.*~~

---

## Run the application

The real stack is `backend/` + `frontend/` and needs MySQL, an `ENCRYPTION_KEY` and a
`SECRET_KEY` (see `backend/app/core/config.py`, which refuses to start on unsafe
settings rather than degrading). `docker compose up` brings up both tiers — the API and
its workers, and the Next.js frontend in its own container on <http://localhost:3000>
(`FRONTEND_PORT` moves it), proxying `/api` to the API service so the browser and its
token share one origin (`D10-128`). Migrations are Alembic, currently at head `0008`,
and they ship inside the image: the one-shot `migrate` service applies
`alembic upgrade head` before the API or any worker starts (`D10-114`). To apply them by
hand instead, the same image does it without a checkout —
`docker compose run --rm migrate`.

A `.env` file is not optional: Compose reads it through `env_file` and refuses to start
while a `${…:?}` variable is unset. `cp .env.example .env` produces one. A `.env`
written before the observability tier landed is missing `GRAFANA_PORT` and
`GRAFANA_ADMIN_PASSWORD`, and the whole stack then fails to start with an error that
names the second one — adding those two lines is the fix.

`.env` is a default, not an authority: a variable of the same name exported in your shell
wins over the file silently, with no warning. So if the API reports `Access denied for
user`, ask a container what it was actually given before touching a volume —

```bash
docker compose run --rm --no-deps --entrypoint python migrate \
  -c "import os; print(os.environ['DATABASE_URL'])"
```

— and note that `docker compose down -v` answers the same error by deleting the database.

## The first account

The stack boots with no users, deliberately: there is no seeded password to change, and
`login` cannot succeed for a tenant that has none. `create-admin` mints the first tenant
and its one administrator; every account after that is made from the users screen by an
administrator. It reads the password from stdin, so it never lands in your shell's
history, and it refuses a slug that already exists rather than handing a second first
administrator to a live tenant.

```bash
docker compose run --rm --no-deps --entrypoint python migrate \
  -m app.cli create-admin --tenant-slug acme --email admin@acme.test \
  --tenant-name "Acme Ltd" --password-stdin
```

The browser then signs in at <http://localhost:3000> with the tenant slug, that address
and that password. Nothing else in the stack creates a user: there is no fixture, no seed
and no default.

## Watch the stack

The application is a background system: its workers tick quietly and most of what they
do leaves no screen behind. All of it is nonetheless visible on loopback, and none of it
is required to run the stack. `make logs` (or `docker compose logs -f api`) follows the
processes' own lines; the rest is below. Every port is loopback-only and can be moved
with the matching `*_PORT` variable from `.env.example`.

| Address | What it answers |
| --- | --- |
| <http://localhost:3000> | The application: contracts, review queue, decisions, queues. |
| <http://127.0.0.1:3001> | Grafana. The dashboard under `ops/grafana/dashboards/` plots every family `app/core/metrics.py` declares. Sign in as `admin` with `GRAFANA_ADMIN_PASSWORD`; anonymous access is off. |
| <http://127.0.0.1:9090> | Prometheus. `/targets` is the honest answer to "is anything being scraped" — the API and all eight workers are targets, and `up{job="workers"}` is a question a boot cannot answer for itself. |
| <http://127.0.0.1:9093> | Alertmanager. `/alerts` lists what fired; silences and the notification log live here too, and die with the container. |
| <http://127.0.0.1:8025> | Mailpit. Everything the stack mails — escalation alerts, attention alerts, and any notification sent with `NOTIFICATION_TRANSPORT=smtp` — arrives here and can be *read* rather than inferred from a log line saying it was sent. |

The panels are provisioned from files, so nothing edited in the Grafana UI survives a
restart and nothing forgotten there is lost. The JSON is guarded by
`backend/tests/test_grafana_provisioning.py`, which fails if a panel queries a series the
code does not declare.

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
| 2 | P2-A … P2-L features, one milestone per commit, human picks the order | **GATE 3** — ✅ signed for ten of twelve at `22f82a3`: P2-A, -B, -C, -D, -E, -F, -G, -H, -I and -J have landed slices; nothing has no code at all; P2-K/-L unspecified and **not signed**, so the gate stays open for the two of them (see `STATUS.md`) |

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
