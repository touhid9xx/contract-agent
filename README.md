# Contract Expiry Notification Agent

Contract-lifecycle automation: contract PDF ingestion, AI field extraction with
per-field confidence, human review of low-confidence fields, channel selection
(email / SMS / voice), T-60 / T-30 / T-14 reminders, two-week response tracking
and escalation to a human operator.

> **Status: Phase 0 — interactive mocked demo, awaiting GATE 1 approval.**
> Nothing in this repository talks to a real database, Kafka, Ollama, SMTP, SMS
> gateway or voice provider yet. See [`STATUS.md`](STATUS.md).

---

## Run the demo

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
| `demo/backend/` | Mocked FastAPI service serving JSON fixtures, no DB | 0 |
| `demo/fixtures/` | Story-driven synthetic data (contracts, fields, decisions, …) | 0 |
| `demo/frontend/` | Next.js 16 + Tailwind 4 UI (real, pointed at the mocked API) | 0 |
| `scripts/demo.mjs` | One-command launcher (uvicorn + next dev) | 0 |
| `DEMO.md` | Demo walkthrough + screen → milestone map | 0 |
| `DECISIONS.md` | Architecture decisions, assumptions, open conflicts | all |
| `STATUS.md` | What is done, what is next, known issues | all |

The real application (`app/`, `agent/`, `workers/`, `web/`, `infra/`) does not
exist yet — it is built in Phase 1 (F1–F7) **only after GATE 1 is approved**.

---

## Delivery workflow (from the build spec)

| Phase | Content | Gate |
| --- | --- | --- |
| 0 | Interactive mocked demo — every screen, all states, one command | **GATE 1** — human visual + functional approval |
| 1 | Real foundation F1–F7 (infra, DB/outbox, auth, bus, observability, web shell, PII encryption) | **GATE 2** — foundation verification + feature menu |
| 2 | P2-A … P2-L features, one milestone per commit, human picks the order | per-milestone approval |

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
