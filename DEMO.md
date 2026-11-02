# Phase 0 — Interactive Demo (MOCKED)

A runnable, navigable prototype of the Contract-Expiry Notification Agent.
**Every screen, number, name, phone and email is synthetic fixture data.** No
database, Kafka/StrikeMQ, Ollama, SMTP, SMS gateway or voice provider is
contacted at any point.

This exists for **GATE 1** — your visual + functional approval — before any
real foundation work begins.

---

## 1. Run it

```bash
npm run demo
```

Then open **http://localhost:3000**.

The launcher (`scripts/demo.mjs`):

1. starts the mocked FastAPI backend with the project `venv` interpreter on
   `127.0.0.1:8000`,
2. waits for `GET /health` to return 200,
3. starts the Next.js 16 dev server on port 3000,
4. prints both URLs and stops everything on `Ctrl+C`.

Partial starts, useful while reviewing docs:

```bash
npm run demo:backend     # backend only
npm run demo:frontend    # frontend only (expects the backend already running)
```

Port overrides: `DEMO_BACKEND_PORT`, `DEMO_FRONTEND_PORT`. If the requested port
is already taken (for example by Docker holding `3000`), the launcher picks the
next free port and prints which one it used — the URL in the banner is always
the one to open.

First-time prerequisites:

```bash
python -m venv venv
venv/Scripts/pip install -r demo/backend/requirements.txt
npm --prefix demo/frontend install
```

### Isolated processes (alternative)

```bash
# terminal 1
cd demo/backend
python -m uvicorn main:app --host 127.0.0.1 --port 8000

# terminal 2
cd demo/frontend
npm run dev
```

---

## 2. What is mocked, and how you can tell

| Signal | Meaning |
| --- | --- |
| `MOCKED` badge in the sidebar, every page header and the landing flow | This screen is fixture-driven |
| `X-Mocked: true` HTTP response header on every backend response | The API is the mocked demo service |
| `"mocked": true` key inside each fixture file | Data provenance marker |
| `admin@acme.test`, `Acme Ltd (fictional)`, `+8801******` | Fictional identifiers only |

The mocked backend (`demo/backend/main.py`) mirrors the future `/api/v1`
contract (paths, payload shapes, status codes, optimistic-locking `409`s), so
the UI survives Phase 1 replacement with the real service.

Deliberate design choices in the mock, to exercise real states:

- ~150–450 ms simulated latency per request so loading skeletons are visible.
- **Optimistic locking**: `PATCH /api/v1/review/{field_id}` rejects a stale
  `version` with `409` and returns the current record.
- **Blocked pipeline**: for a contract still awaiting review, the agent re-run
  (`POST /api/v1/agent/decisions/{id}/rerun`) returns `409 PIPELINE_BLOCKED` and
  the button is disabled with an explanatory tooltip — an unresolved field may
  never enter the notification pipeline.
- One notification is in a `failed` state with a retry counter, to show retry UX.

---

## 3. Screens

| # | Screen | Route | Sketch |
| --- | --- | --- | --- |
| 1 | Dashboard overview | `/dashboard` | Sketch 1 |
| 2 | Contracts list (filters, list/kanban, pagination) | `/contracts` | Sketch 2 |
| 3 | Contract detail + timeline (**primary debugging view**) | `/contracts/[id]` | Sketch 3 |
| 4 | Extraction review queue | `/review` | Sketch 4 |
| 5 | Agent decision panel | on `/contracts/[id]` | Sketch 5 |
| 6 | Escalation queue (SLA breached / open / resolved) | `/escalations` | Sketch 6 |
| 7 | Notifications view | `/notifications` | Sketch 7 |
| 8 | Landing + system flow diagram | `/` | Sketch 8 |

Shell behaviours already implemented: responsive sidebar/drawer, light/dark
theme, keyboard shortcuts (`g d`, `g c`, `/`, `?`, `Esc`), focus states, empty
states, skeleton loaders, toasts, and confirmation before destructive actions.

---

## 4. Screen → milestone map (what replaces each mock)

| Demo screen / element | Replaced by | Real backing |
| --- | --- | --- |
| Landing flow diagram | P2-J Observability (+ F1/F4 architecture) | Real metrics/topology docs |
| Dashboard stat cards, reminder windows, channel mix | P2-I Frontend Features | Aggregations over `contracts`, `notifications`, `escalations` |
| Contracts list, filters, kanban, pagination | P2-I Frontend Features | `GET /api/v1/contracts` |
| Contract detail fields + agent panel | P2-I (+ P2-D) | `agent_decisions`, `extracted_fields` |
| Timeline events | P2-I | `audit_log`, `notifications`, `agent_decisions`, `customer_responses`, `escalations` |
| Review queue accept/reject/diff/409 | **P2-C Human Review** | `SELECT … FOR UPDATE SKIP LOCKED`, optimistic locking |
| Upload control (currently disabled) | **P2-A PDF Upload** | magic-byte + ClamAV + sha256 dedup |
| Extracted field values & confidences | **P2-B Extraction** | pdfplumber + regex/spaCy, per-field confidence |
| Agent decision panel (channel/confidence/reasoning) | **P2-D AI Agent** | smolagents + local Ollama, Pydantic validation |
| Notification rows, retries, idempotency keys | **P2-E Notifications** | SMTP/textbee, daily-scan scheduler |
| Voice + AMD rows | **P2-F Voice** | Asterisk ARI + Whisper AMD |
| Escalation queue, SLA, assign-to-me | **P2-G Response & Escalation** | `escalations`, 2-week worker |
| "Pull from company API" ingestion | **P2-H Contracts API** | pull-mode ingestion → outbox |
| Everything (auth, tenant, correlation IDs) | Phase 1 F1–F7 | real stack |

---

## 5. Story-driven walkthrough (Appendix D)

The fixtures tell one continuous lifecycle. Click in this order:

1. **`/`** — system flow diagram: PDF → 202 + correlation_id → extraction →
   low-confidence review → outbox → Kafka/StrikeMQ → agent → notification →
   response tracking → escalation.
2. **`/dashboard`** — four stat cards (1,284 / 47 / 6 / 3), reminder-window
   bars (T-60 / T-30 / T-14), recent activity, channel mix (62 / 21 / 17).
3. **`/contracts`** — filter by status/channel/risk; switch list ↔ kanban;
   paginate. Note the `review !` and `escalated !` rows.
4. **`/contracts/1042`** — A. Rahman. Five extracted fields (one at 0.54),
   agent decision `email 0.92` with provenance (prompt v3, qwen2:7b, t=0.2,
   latency, schema repairs) and the PII-free input signals. Timeline grouped by
   day; click any event to expand its JSON.
5. **`/review`** — the low-confidence address for #1039: accept / reject / mark
   unreadable, or edit inline. A stale `version` returns a 409 conflict banner.
   The "cannot enter notification pipeline" note is always visible.
6. **`/contracts/1037`** — voice + AMD (`machine` → TTS message left), then the
   2-week no-response escalation and SLA breach highlight.
7. **`/notifications`** — email/SMS/voice rows with idempotency keys; one
   `failed` row with `retry 2/3` and a retry action.
8. **`/escalations`** — breached vs open vs resolved, assign-to-me and resolve.
9. Toggle dark mode; press `/` to focus search; press `?` for shortcuts.

Try the error paths too: submit a review edit twice (stale `409`), and press
"Re-run agent" on #1039 (blocked while a field is unresolved).

---

## 6. Fixtures

`demo/fixtures/` — `contracts.json`, `extracted_fields.json`,
`agent_decisions.json`, `notifications.json`, `escalations.json`,
`timeline.json`, `dashboard.json`, `users.json`.

The six story contracts, in lifecycle order:

| Contract | Story |
| --- | --- |
| `#1042` | Fresh → extracted → agent chose **email** (0.92) → sent → timeline |
| `#1039` | Low-confidence field → **blocked awaiting human review** (`409` on re-run) |
| `#1041` | Agent chose **SMS**; open escalation, no reply yet |
| `#1040` | **Voice** call answered; agent choice voice |
| `#1038` | Voice call hit a **machine** (AMD) → TTS message left |
| `#1037` | No response after 2 weeks → **escalated**, SLA breached |

All names, addresses, DOBs and phone numbers are fictional; phone numbers are
partially masked (`+8801******`).

---

## 7. Assumptions carried into the demo (Appendix E)

- **Q1 — run command**: `npm run demo`; the launcher spawns uvicorn and Next.
- **Q2 — escalation SLA**: 48 h after the escalation is raised.
- **Q3 — reminder semantics**: T-60/T-30/T-14 are calendar-day boundaries
  compared during the daily scan; displayed times are illustrative.
- **Q4 — voice depth**: AMD result and TTS script are mocked text events; no
  audio is produced in Phase 0.
- **Q5 — channel mix**: fixed 62/21/17 fixture percentages until P2-J provides
  real metrics.
- Upload is intentionally **disabled** (visible, with tooltip) because PDF
  ingestion is P2-A; nothing in this demo accepts a real file.
- The demo runs single-tenant (`acme`) and single user (`admin@acme.test`);
  multi-tenant isolation is enforced for real in Phase 1.

Anything else unspecified (spacing, colour, microcopy) was chosen as a sensible
default; say the word at GATE 1 and I'll change it.

---

## 8. What this demo deliberately does NOT do

No real PDF parsing, no LLM calls (no Ollama), no email/SMS/voice dispatch, no
database, no auth, no consent/opt-out engine, no encryption. Those are Phase 1
foundation (F1–F7) and Phase 2 features (P2-A…P2-L), each of which replaces a
labelled part of this demo.
