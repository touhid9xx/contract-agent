"""End-to-end smoke journey: one real server, one real socket, one real database.

Why this file exists
--------------------
Every other test in this repository talks to the application through
``fastapi.testclient.TestClient``, which is an in-process ASGI call. That is the
right tool for asserting behaviour, and it is *not* proof that the thing
``docker compose up`` starts actually serves those routes: no socket is opened,
no ``uvicorn`` is involved, and nothing between the two processes can disagree.

This script closes exactly that one gap. It boots the ASGI app under a real
``uvicorn`` on a free loopback port, points it at a file-backed SQLite database,
and drives the customer journey over HTTP with ``httpx`` — the same library the
test suite already depends on. The assertions are deliberately about *seams*
that no single test file owns:

- the error envelope the frontend parses (``frontend/src/lib/api.ts``) is the one
  the server sends, on a real 401 and a real 409;
- the bootstrap path an operator runs first (``create-admin`` over stdin) produces
  credentials that the already-running server accepts;
- a contract can be ingested, extracted, reviewed, consented, decided and
  responded to without any step returning 5xx;
- a single-use refresh token is single-use *across the wire*.

What it does not prove
----------------------
It is not a substitute for the suite. It covers one happy path and a handful of
refusals, on SQLite, with ``EVENT_TRANSPORT=none`` and
``NOTIFICATION_TRANSPORT=none``, so no Kafka, no SMTP and no MySQL is involved.
It says "the served API works as wired", not "every branch is correct".

How to run it
-------------
From the repository root::

    venv\\Scripts\\python.exe backend/tests/smoke_journey.py

Exit code 0 means every step passed. Non-zero means a step failed; the failing
step and the response body are printed. Nothing is left behind: the database,
the document store and the encryption key live in a temporary directory that is
removed on success.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# The bootstrap below mirrors backend/tests/conftest.py: the backend is not an
# installed distribution (see the pyproject header), and APP_ENV must be pinned
# before app.core.config is imported so production validation cannot fire.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND_DIR))
os.environ.setdefault("APP_ENV", "test")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from sqlalchemy import Engine, create_engine, event  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.cli import main as cli_main  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import build_session_factory  # noqa: E402
from app.main import create_app  # noqa: E402

TENANT_SLUG = "smoke"
TENANT_NAME = "Smoke Test Ltd"
ADMIN_EMAIL = "smoke-admin@example.test"
#: Not a credential: it protects a database in a temp directory that this script
#: deletes, and it never leaves the process.
ADMIN_PASSWORD = "smoke-journey-password"
META_CUSTOMER_REF = "SMOKE-META-1"
DOC_CUSTOMER_REF = "SMOKE-DOC-1"
CONTACT_EMAIL = "billing-contact@example.test"
CONTACT_PHONE = "+44 20 7946 0958"

#: The document the extraction worker reads. The phone number is deliberately the
#: first line and carries no label, because a labelled phone extracts at 0.95 and
#: is auto-accepted; an unlabelled one extracts at 0.75, which is below the 0.8
#: review threshold. That is what guarantees the review leg below has a row to
#: act on instead of silently skipping.
DOCUMENT = (
    f"{CONTACT_PHONE}\n"
    f"Service agreement for {TENANT_NAME}\n"
    f"Contact email: {CONTACT_EMAIL}\n"
    "Start date: 2024-02-01\n"
    "End date: 2027-02-01\n"
)

_CHECKS = 0


class SmokeJourneyError(AssertionError):
    """A step of the journey did not hold.

    Subclasses ``AssertionError`` because that is what it is: an expectation the
    walk-through made about the served API and did not get. The name carries
    ``Error`` because ruff's ``N818`` requires it of any exception class, and
    conforming is cheaper than an exception to the rule.
    """


def need(condition: bool, message: str) -> None:
    """Assert one step, counting it so the summary is a real number."""
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise SmokeJourneyError(message)


def say(step: str) -> None:
    print(f"  ..  {step}")


# --- harness --------------------------------------------------------------


def build_database(path: Path) -> tuple[Engine, sessionmaker[Session]]:
    """A file-backed SQLite database the server thread and this process share.

    Deliberately a *file* and not ``:memory:``. The in-memory fixture in
    ``conftest.py`` uses ``StaticPool``, which hands every session the same
    DBAPI connection so that one database can be reached from several sessions —
    and which cannot be used from two threads at once. The server runs its sync
    routes in a worker thread, so a shared in-memory database would be a
    flakiness bug in the harness rather than in the code under test.
    """
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        # Same reason as conftest: SQLite ignores foreign keys unless asked.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return engine, build_session_factory(engine)


def build_settings(database_url: str, documents_dir: Path, key: str) -> Settings:
    """A runnable configuration that needs no external service.

    ``argon2_*`` is lowered for the same reason ``conftest.py`` lowers it: the
    code path is identical and only the work is smaller. The transports stay at
    their shipped defaults, so nothing here can reach a broker or a mail server.
    """
    return Settings(
        app_env="test",
        log_level="WARNING",
        log_json=False,
        argon2_time_cost=1,
        argon2_memory_cost=8192,
        argon2_parallelism=1,
        database_url=database_url,
        encryption_key=key,
        document_store_path=str(documents_dir),
        event_transport="none",
        notification_transport="none",
    )


def free_port() -> int:
    with contextlib.closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def start_server(app: Any, port: int, timeout: float = 30.0) -> uvicorn.Server:
    """Run uvicorn on a daemon thread and wait until it reports itself started."""
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, name="smoke-uvicorn", daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.started:
            return server
        time.sleep(0.05)
    raise SmokeJourneyError(f"uvicorn did not start on 127.0.0.1:{port} within {timeout}s")


def run_cli(argv: list[str], settings: Settings, factory: sessionmaker[Session]) -> int:
    """Invoke the operator CLI in this process, feeding stdin where it needs it.

    ``create-admin`` refuses to take a password as an argument (it would land in
    shell history and in ``ps``), so the password arrives on stdin exactly as it
    would from a pipe in production.
    """
    saved = sys.stdin
    sys.stdin = io.StringIO(ADMIN_PASSWORD + "\n")
    try:
        return cli_main(argv, settings=settings, session_factory=factory)
    finally:
        sys.stdin = saved


# --- wire helpers ---------------------------------------------------------


def envelope(response: httpx.Response) -> dict[str, Any]:
    """Return the error envelope, asserting the shape the frontend parses.

    ``frontend/src/lib/api.ts`` reads ``error.code`` and ``error.message`` off
    every non-2xx body. A server that answered 401 with a bare ``{"detail":
    ...}`` would still pass most backend tests and break every screen, which is
    precisely the disagreement this script exists to catch.
    """
    body = response.json()
    need(
        isinstance(body, dict) and isinstance(body.get("error"), dict),
        f"expected an error envelope, got {body!r}",
    )
    error = body["error"]
    need(
        isinstance(error.get("code"), str) and error["code"] != "",
        f"error envelope carries no string code: {error!r}",
    )
    need(
        isinstance(error.get("message"), str) and error["message"] != "",
        f"error envelope carries no string message: {error!r}",
    )
    return error


class Wire:
    """A thin client that fails the journey on any 5xx.

    A 500 anywhere is not a step that "did not apply": it is an unhandled
    exception escaping a route the UI calls, which is the class of defect a
    happy-path walk-through is most useful for finding.
    """

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self.access: str | None = None

    def call(self, method: str, url: str, *, auth: bool = True, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        if auth and self.access is not None:
            headers["Authorization"] = f"Bearer {self.access}"
        response = self._client.request(method, url, headers=headers, **kwargs)
        need(
            response.status_code < 500,
            f"{method} {url} returned {response.status_code}: {response.text[:600]}",
        )
        return response


# --- the journey ----------------------------------------------------------


def journey(
    wire: Wire,
    settings: Settings,
    factory: sessionmaker[Session],
) -> None:
    client = wire._client

    # 1. The probes an orchestrator hits before it sends traffic.
    say("liveness and readiness probes")
    need(client.get("/health").status_code == 200, "/health is not 200")
    # A default install must be readable: every dependency it has is local, and
    # event transport "none" is omitted from the report rather than failed.
    need(client.get("/health/ready").status_code == 200, "/health/ready is not 200")

    # 2. Bootstrap, through the operator CLI, against a server that is already
    #    running. This is the real first-run order and nothing else tests it.
    say("create-admin bootstraps the first tenant and admin")
    need(
        run_cli(
            [
                "create-admin",
                "--tenant-slug",
                TENANT_SLUG,
                "--tenant-name",
                TENANT_NAME,
                "--email",
                ADMIN_EMAIL,
                "--password-stdin",
            ],
            settings,
            factory,
        )
        == 0,
        "create-admin refused",
    )

    # 3. A refused sign-in says nothing about which half was wrong.
    say("a wrong password is a 401 with the shared envelope")
    refused = wire.call(
        "POST",
        "/api/v1/auth/login",
        auth=False,
        json={"tenant": TENANT_SLUG, "email": ADMIN_EMAIL, "password": "definitely-wrong"},
    )
    need(refused.status_code == 401, f"wrong password returned {refused.status_code}")
    error = envelope(refused)
    need(
        ADMIN_EMAIL not in error["message"],
        f"the 401 echoes the address back: {error['message']!r}",
    )

    # 4. Sign in with the password the CLI just stored.
    say("the admin signs in and receives a session")
    signed_in = wire.call(
        "POST",
        "/api/v1/auth/login",
        auth=False,
        json={"tenant": TENANT_SLUG, "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    need(signed_in.status_code == 200, f"login returned {signed_in.status_code}")
    session = signed_in.json()
    for key in ("access_token", "refresh_token"):
        need(
            isinstance(session.get(key), str) and session[key] != "",
            f"login response carries no {key}",
        )
    wire.access = session["access_token"]
    refresh_token = session["refresh_token"]

    me = wire.call("GET", "/api/v1/auth/me")
    need(me.status_code == 200, f"/auth/me returned {me.status_code}")
    principal = me.json()
    need(principal.get("email") == ADMIN_EMAIL, f"/auth/me returned {principal.get('email')!r}")
    need(principal.get("role") == "admin", f"/auth/me role is {principal.get('role')!r}")

    # 5. Ingestion is idempotent: the same payload twice is a 409, not a row.
    say("ingesting the same metadata twice is a conflict")
    metadata = {
        "customer_ref": META_CUSTOMER_REF,
        "start_date": "2024-02-01",
        "end_date": "2027-02-01",
        "timezone": "Asia/Dhaka",
        "source": "api",
    }
    first = wire.call("POST", "/api/v1/contracts", json=metadata)
    need(first.status_code in (200, 201), f"ingest returned {first.status_code}")
    created = first.json()
    need(isinstance(created.get("id"), int), f"ingest returned no id: {created!r}")
    # The producer side of the outbox: a contract write that enqueued nothing
    # would leave the event stream silently empty.
    need(
        isinstance(created.get("outbox_event_id"), str) and created["outbox_event_id"] != "",
        f"ingest returned no outbox_event_id: {created!r}",
    )

    duplicate = wire.call("POST", "/api/v1/contracts", json=metadata)
    need(duplicate.status_code == 409, f"duplicate ingest returned {duplicate.status_code}")
    envelope(duplicate)

    # 6. A document upload is the path that carries the bytes to extraction.
    say("a contract document is uploaded and stored")
    uploaded = wire.call(
        "POST",
        "/api/v1/contracts/document",
        params={
            "customer_ref": DOC_CUSTOMER_REF,
            "start_date": "2024-02-01",
            "end_date": "2027-02-01",
            "timezone": "Asia/Dhaka",
        },
        content=DOCUMENT.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )
    need(uploaded.status_code in (200, 201), f"document upload returned {uploaded.status_code}")
    contract_id = uploaded.json().get("id")
    need(isinstance(contract_id, int), f"document upload returned no id: {uploaded.json()!r}")

    detail = wire.call("GET", f"/api/v1/contracts/{contract_id}")
    need(detail.status_code == 200, f"contract detail returned {detail.status_code}")
    contract = detail.json()
    need(contract.get("has_document") is True, "the uploaded document is not recorded on the row")
    need(
        contract.get("version") == 1,
        f"a new contract starts at version 1, got {contract.get('version')!r}",
    )

    listing = wire.call("GET", "/api/v1/contracts")
    need(listing.status_code == 200, f"contract list returned {listing.status_code}")
    ids = [item.get("id") for item in listing.json().get("items", [])]
    need(contract_id in ids, "the new contract is missing from GET /contracts")

    # 7. Extraction is operator-driven and has no HTTP route, so it is invoked
    #    the way an operator invokes it — and the *server* must then show the
    #    rows it wrote, which is the seam worth proving.
    say("extract reads the stored document into reviewable fields")
    need(
        run_cli(["extract", "--contract-id", str(contract_id)], settings, factory) == 0,
        "extract refused",
    )

    fields = wire.call("GET", f"/api/v1/contracts/{contract_id}/fields")
    need(fields.status_code == 200, f"fields returned {fields.status_code}")
    field_rows = fields.json().get("fields", [])
    need(len(field_rows) > 0, "extraction produced no fields")
    need(
        fields.json().get("blocking", 0) >= 1,
        f"no field blocks the pipeline: {fields.json()!r}",
    )
    for row in field_rows:
        need(
            "value" not in row,
            f"the fields list leaks a value: {sorted(row)}",
        )

    # 8. The review queue: list, reveal, and two decisions — one refused.
    say("the review queue holds the low-confidence field")
    queue = wire.call("GET", "/api/v1/review/queue")
    need(queue.status_code == 200, f"review queue returned {queue.status_code}")
    queue_body = queue.json()
    need(queue_body.get("count", 0) >= 1, f"the review queue is empty: {queue_body!r}")
    pending = [item for item in queue_body["items"] if item.get("contract_id") == contract_id]
    need(len(pending) == 1, f"expected one queued field for the contract, got {pending!r}")
    item = pending[0]
    need(item.get("field_name") == "phone", f"expected the unlabelled phone, got {item!r}")
    need(item.get("status") == "pending_review", f"field status is {item.get('status')!r}")
    need("value" not in item, f"the review queue leaks a value: {sorted(item)}")
    field_id = item["field_id"]

    say("revealing one value is audited and returns plaintext")
    revealed = wire.call("POST", f"/api/v1/review/{field_id}/reveal")
    need(revealed.status_code == 200, f"reveal returned {revealed.status_code}")
    revealed_body = revealed.json()
    need(
        CONTACT_PHONE.replace(" ", "") in revealed_body.get("value", "").replace(" ", ""),
        f"reveal returned {revealed_body.get('value')!r}",
    )

    say("a stale version is refused with a 409")
    stale = wire.call(
        "PATCH",
        f"/api/v1/review/{field_id}",
        json={"action": "accept", "version": int(item["version"]) + 99},
    )
    need(stale.status_code == 409, f"stale decision returned {stale.status_code}")
    envelope(stale)

    say("accepting the field unblocks the pipeline")
    accepted = wire.call(
        "PATCH",
        f"/api/v1/review/{field_id}",
        json={"action": "accept", "version": int(item["version"])},
    )
    need(accepted.status_code == 200, f"accept returned {accepted.status_code}")
    accepted_body = accepted.json()
    need(accepted_body["item"]["status"] == "approved", f"status is {accepted_body['item']!r}")
    need(
        accepted_body["blocking"] < fields.json()["blocking"],
        f"blocking did not fall: {accepted_body['blocking']}",
    )

    # 9. Consent is the gate that makes any contact lawful.
    say("consent is recorded against the contract version")
    consented = wire.call(
        "PUT",
        f"/api/v1/contracts/{contract_id}/consent",
        json={
            "consent_email": True,
            "consent_sms": False,
            "consent_voice": False,
            "do_not_contact": False,
            "version": int(contract["version"]),
        },
    )
    need(consented.status_code == 200, f"consent returned {consented.status_code}")
    consent_body = consented.json()
    need(consent_body.get("may_contact") is True, f"may_contact is {consent_body!r}")
    need(consent_body.get("consent_channels") == ["email"], f"channels: {consent_body!r}")

    # 10. The agent ranks the channels it is allowed to use.
    say("the agent records a channel decision")
    decided = wire.call("POST", "/api/v1/agent/decisions", json={"contract_id": contract_id})
    need(decided.status_code in (200, 201), f"decision returned {decided.status_code}")
    decision = decided.json()
    need(
        decision.get("contract_id") == contract_id, f"decision is for {decision.get('contract_id')}"
    )
    need(decision.get("channel") == "email", f"decision chose {decision.get('channel')!r}")
    need(decision.get("needs_review") is False, f"decision needs review: {decision!r}")

    decisions = wire.call("GET", "/api/v1/agent/decisions", params={"contract_id": contract_id})
    need(decisions.status_code == 200, f"decision list returned {decisions.status_code}")
    need(decisions.json().get("count", 0) == 1, f"decision count: {decisions.json()!r}")

    # 11. Scheduling may legitimately refuse — this contract's end date is years
    #     away, so no reminder window is open. What must not happen is a 5xx, and
    #     a refusal must use the shared envelope. If a row *is* created, dispatch
    #     it and assert the refusal is redacted: the whole point of
    #     NOTIFICATION_TRANSPORT=none is that a wasted attempt cannot leak the
    #     address it was about.
    say("notification scheduling refuses cleanly or produces a row")
    scheduled = wire.call("POST", "/api/v1/notifications", json={"contract_id": contract_id})
    need(
        scheduled.status_code in (200, 201, 409),
        f"schedule returned {scheduled.status_code}: {scheduled.text[:400]}",
    )
    if scheduled.status_code == 409:
        envelope(scheduled)
    else:
        notification_id = scheduled.json()["id"]
        dispatched = wire.call("POST", f"/api/v1/notifications/{notification_id}/dispatch")
        need(dispatched.status_code == 200, f"dispatch returned {dispatched.status_code}")
        dispatch_body = dispatched.json()
        need(dispatch_body.get("sent") is False, f"dispatch claimed a send: {dispatch_body!r}")
        failure = dispatch_body.get("error") or ""
        need(failure != "", f"a failed dispatch carried no reason: {dispatch_body!r}")
        need(
            CONTACT_EMAIL not in failure and CONTACT_PHONE not in failure,
            f"the dispatch error leaks contact data: {failure!r}",
        )

    notifications = wire.call("GET", "/api/v1/notifications", params={"contract_id": contract_id})
    need(
        notifications.status_code == 200, f"notification list returned {notifications.status_code}"
    )

    # 12. Escalate, then answer: the two routes that make RESPONDED reachable.
    say("an escalation is raised and a customer reply closes it")
    raised = wire.call("POST", f"/api/v1/contracts/{contract_id}/escalation")
    need(raised.status_code in (200, 201), f"escalate returned {raised.status_code}")
    need(raised.json().get("status") in {"open", "in_progress"}, f"status: {raised.json()!r}")

    queue_rows = wire.call("GET", "/api/v1/escalations", params={"contract_id": contract_id})
    need(queue_rows.status_code == 200, f"escalation list returned {queue_rows.status_code}")
    need(
        queue_rows.json().get("count", 0) >= 1,
        f"the escalation is not listed: {queue_rows.json()!r}",
    )

    answered = wire.call(
        "POST",
        f"/api/v1/contracts/{contract_id}/response",
        json={"channel": "email", "response_type": "reply"},
    )
    need(answered.status_code in (200, 201), f"response returned {answered.status_code}")
    answered_body = answered.json()
    need(answered_body.get("contract_status") == "responded", f"status: {answered_body!r}")
    need(
        isinstance(answered_body.get("resolved_escalation_id"), int),
        f"the reply did not close the escalation: {answered_body!r}",
    )

    # 13. Refresh rotation, over the wire: the second use of one token must fail.
    say("a refresh token is single-use across the wire")
    rotated = wire.call(
        "POST", "/api/v1/auth/refresh", auth=False, json={"refresh_token": refresh_token}
    )
    need(rotated.status_code == 200, f"refresh returned {rotated.status_code}")
    rotated_body = rotated.json()
    need(
        rotated_body.get("refresh_token") not in (None, refresh_token),
        "rotation returned the same refresh token",
    )

    replayed = wire.call(
        "POST", "/api/v1/auth/refresh", auth=False, json={"refresh_token": refresh_token}
    )
    need(replayed.status_code == 401, f"a replayed refresh token returned {replayed.status_code}")
    envelope(replayed)

    # 14. Log out, and confirm the family is gone rather than merely forgotten.
    say("logging out retires the refresh family")
    logged_out = wire.call(
        "POST",
        "/api/v1/auth/logout",
        auth=False,
        json={"refresh_token": rotated_body["refresh_token"]},
    )
    need(logged_out.status_code in (200, 204), f"logout returned {logged_out.status_code}")

    after_logout = wire.call(
        "POST",
        "/api/v1/auth/refresh",
        auth=False,
        json={"refresh_token": rotated_body["refresh_token"]},
    )
    need(
        after_logout.status_code == 401, f"refresh after logout returned {after_logout.status_code}"
    )
    envelope(after_logout)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="smoke-journey-"))
    engine: Engine | None = None
    server: uvicorn.Server | None = None
    passed = False
    try:
        database_path = workdir / "smoke.sqlite3"
        documents_dir = workdir / "documents"
        documents_dir.mkdir(parents=True, exist_ok=True)

        engine, factory = build_database(database_path)
        settings = build_settings(
            f"sqlite+pysqlite:///{database_path}",
            documents_dir,
            Fernet.generate_key().decode(),
        )

        port = free_port()
        app = create_app(settings, session_factory=factory)
        server = start_server(app, port)
        base_url = f"http://127.0.0.1:{port}"

        print(f"smoke journey against {base_url} (sqlite, transports off)")
        with httpx.Client(base_url=base_url, timeout=20.0) as client:
            journey(Wire(client), settings, factory)

        passed = True
        print(f"\nOK  {_CHECKS} checks passed")
        return 0
    except SmokeJourneyError as failure:
        print(f"\nFAIL  {failure}")
        print(f"      artifacts kept in {workdir}")
        return 1
    finally:
        if server is not None:
            server.should_exit = True
        if engine is not None:
            engine.dispose()
        # Removed only when everything passed; a failure keeps the database so
        # the rows behind a broken step can be inspected.
        if passed:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
