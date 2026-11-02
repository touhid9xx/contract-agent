"""End-to-end tests for contract ingestion (F6, steps 1-2).

Ingestion is the first write path in the system that produces a domain event, so
these tests go through the real ASGI app against a real (in-memory) database —
the guarantees under test are cross-cutting:

* the tenant is taken from the token, and a ``tenant_id`` in the body changes
  nothing;
* the same content ingested twice is one contract **and** one event;
* the outbox row and the business row are written or discarded together, which is
  the property the whole outbox exists to provide;
* a role that may not write is refused before any row exists.

A test that called the router function directly could not observe the last one,
because the transaction boundary belongs to the request session.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.enums import ContractSource, ContractStatus, OutboxStatus, RiskLevel, UserRole
from app.db.models import Contract, OutboxEvent, Tenant
from app.db.repositories.contracts import ContractRepository
from app.db.session import session_scope

CONTRACTS_PATH = "/api/v1/contracts"
LOGIN_PATH = "/api/v1/auth/login"

#: Matches ``conftest.DEFAULT_PASSWORD``. Not a credential: it protects in-memory
#: databases that only exist for the duration of a test.
PASSWORD = "correct-horse-battery-staple"  # nosec B105

#: The content of the contract every test starts from. Dates are fixed so the
#: dedupe key is stable across runs.
BASE_PAYLOAD: dict[str, Any] = {
    "customer_ref": "A. Rahman",
    "start_date": "2024-01-01",
    "end_date": "2026-01-01",
    "timezone": "Europe/London",
    "source": "api",
}


# --- helpers --------------------------------------------------------------
def _token(client: TestClient, tenant: Tenant, user_email: str) -> str:
    response = client.post(
        LOGIN_PATH,
        json={"tenant": tenant.slug, "email": user_email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _ingest(
    client: TestClient,
    token: str,
    *,
    headers: dict[str, str] | None = None,
    **overrides: Any,
) -> Response:
    body = {**BASE_PAYLOAD, **overrides}
    return client.post(CONTRACTS_PATH, json=body, headers={**_auth(token), **(headers or {})})


def _events(session_factory: sessionmaker[Session]) -> list[OutboxEvent]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(OutboxEvent).order_by(OutboxEvent.id)).scalars())


def _contract_rows(session_factory: sessionmaker[Session]) -> int:
    with session_scope(session_factory) as session:
        return int(session.execute(select(func.count()).select_from(Contract)).scalar_one())


@pytest.fixture
def token(client: TestClient, tenant: Tenant, make_user: Callable[..., Any]) -> str:
    """An operator of the ``tenant`` fixture, already logged in."""
    make_user(tenant, email="ops@acme.test", role=UserRole.OPERATOR)
    return _token(client, tenant, "ops@acme.test")


# --- the happy path -------------------------------------------------------
def test_ingesting_a_contract_persists_it_for_the_callers_tenant(
    client: TestClient,
    token: str,
    tenant: Tenant,
    session_factory: sessionmaker[Session],
) -> None:
    response = _ingest(client, token)
    assert response.status_code == 201, response.text

    body = response.json()
    with session_scope(session_factory) as session:
        contract = session.get(Contract, body["id"])

    assert contract is not None
    assert contract.tenant_id == tenant.id
    assert contract.customer_ref == "A. Rahman"
    assert contract.start_date is not None
    assert contract.end_date is not None
    assert contract.start_date.isoformat() == "2024-01-01"
    assert contract.end_date.isoformat() == "2026-01-01"
    assert contract.timezone == "Europe/London"
    assert contract.source is ContractSource.API


def test_a_new_contract_starts_active_at_version_one(client: TestClient, token: str) -> None:
    response = _ingest(client, token)
    assert response.status_code == 201, response.text

    body = response.json()
    # A caller cannot post itself into ``expired`` or pre-set its own risk: both
    # come from the pipeline, so ingestion leaves the model defaults in place.
    assert body["status"] == ContractStatus.ACTIVE.value
    assert body["risk"] == RiskLevel.LOW.value
    assert body["version"] == 1
    assert body["sha256"] is not None
    assert len(body["sha256"]) == 64


# --- the outbox producer --------------------------------------------------
def test_ingestion_writes_one_pending_event_for_the_new_contract(
    client: TestClient,
    token: str,
    session_factory: sessionmaker[Session],
) -> None:
    response = _ingest(client, token, headers={"X-Request-ID": "ingest-correlation-1"})
    assert response.status_code == 201, response.text
    contract_id = response.json()["id"]

    events = _events(session_factory)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "contract.ingested"
    assert event.topic == "contract.events"
    # The partition key is the contract id, so every event about one contract
    # keeps its relative order for consumers.
    assert event.partition_key == str(contract_id)
    assert event.payload["contract_id"] == contract_id
    assert event.payload["status"] == ContractStatus.ACTIVE.value
    assert event.status is OutboxStatus.PENDING
    assert event.attempts == 0
    # The event inherits the request's correlation id, which is what lets an
    # operator join the HTTP log line to the publish log line.
    assert event.correlation_id == "ingest-correlation-1"
    # And the response hands back the identity the consumer will de-duplicate on.
    assert response.json()["outbox_event_id"] == str(event.event_id)


def test_the_event_is_ready_for_the_relay_to_claim_immediately(
    client: TestClient,
    token: str,
    session_factory: sessionmaker[Session],
) -> None:
    """The D2-06 lesson, observed from the producer side.

    A row enqueued "now" whose ``next_attempt_at`` rounded *forward* would be
    invisible to the relay on its next tick, so the floor applied in
    ``enqueue_event`` is part of what makes the producer real.
    """
    response = _ingest(client, token)
    assert response.status_code == 201, response.text

    event = _events(session_factory)[0]
    assert event.next_attempt_at.microsecond == 0


# --- idempotency ----------------------------------------------------------
def test_repeating_the_ingestion_is_a_conflict_that_writes_nothing_new(
    client: TestClient,
    token: str,
    session_factory: sessionmaker[Session],
) -> None:
    first = _ingest(client, token)
    assert first.status_code == 201, first.text

    second = _ingest(client, token)
    assert second.status_code == 409
    assert str(first.json()["id"]) in second.json()["error"]["message"]

    # One contract, one event: a retried POST must not double the reminder clock
    # or announce the contract twice.
    assert _contract_rows(session_factory) == 1
    assert len(_events(session_factory)) == 1


def test_field_order_and_explicit_defaults_do_not_defeat_the_dedupe(
    client: TestClient,
    token: str,
    session_factory: sessionmaker[Session],
) -> None:
    """The fingerprint canonicalises, so JSON key order is not part of identity."""
    first = _ingest(client, token)
    assert first.status_code == 201, first.text

    reordered = {
        "source": "api",
        "timezone": "Europe/London",
        "end_date": "2026-01-01",
        "start_date": "2024-01-01",
        "customer_ref": "A. Rahman",
    }
    second = client.post(CONTRACTS_PATH, json=reordered, headers=_auth(token))
    assert second.status_code == 409
    assert _contract_rows(session_factory) == 1


def test_different_content_is_a_different_contract(client: TestClient, token: str) -> None:
    first = _ingest(client, token)
    second = _ingest(client, token, customer_ref="B. Chowdhury")
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["sha256"] != second.json()["sha256"]


def test_a_losing_race_leaves_neither_a_row_nor_an_event(
    client: TestClient,
    token: str,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The uniqueness check is advisory; the constraint is what decides.

    The pre-check is disabled deliberately to model the concurrent case, where both
    requests pass it and the second one loses at the database. What matters is the
    outcome: a 409 the caller can act on, and — because both rows are written in
    one transaction — no orphaned event announcing a contract that was not stored.
    """
    first = _ingest(client, token)
    assert first.status_code == 201, first.text

    monkeypatch.setattr(ContractRepository, "by_sha256", lambda self, sha256: None)

    second = _ingest(client, token)
    assert second.status_code == 409, second.text

    assert _contract_rows(session_factory) == 1
    assert len(_events(session_factory)) == 1


# --- tenant isolation -----------------------------------------------------
def test_the_same_content_may_be_ingested_by_two_tenants(
    client: TestClient,
    token: str,
    tenant: Tenant,
    other_tenant: Tenant,
    make_user: Callable[..., Any],
    session_factory: sessionmaker[Session],
) -> None:
    other_user = make_user(other_tenant, email="ops@globex.test", role=UserRole.OPERATOR)
    other_token = _token(client, other_tenant, other_user.email)

    mine = _ingest(client, token)
    theirs = _ingest(client, other_token)

    assert mine.status_code == 201, mine.text
    assert theirs.status_code == 201, theirs.text
    assert mine.json()["sha256"] == theirs.json()["sha256"]
    assert mine.json()["id"] != theirs.json()["id"]

    with session_scope(session_factory) as session:
        stored = [
            session.get(Contract, mine.json()["id"]),
            session.get(Contract, theirs.json()["id"]),
        ]
    owners = {row.tenant_id for row in stored if row is not None}
    assert len(stored) == 2
    assert owners == {tenant.id, other_tenant.id}


def test_a_tenant_id_in_the_body_is_ignored(
    client: TestClient,
    token: str,
    tenant: Tenant,
    other_tenant: Tenant,
    session_factory: sessionmaker[Session],
) -> None:
    """The one field a client might try in order to write into another tenant."""
    response = _ingest(client, token, tenant_id=str(other_tenant.id))
    assert response.status_code == 201, response.text

    with session_scope(session_factory) as session:
        contract = session.get(Contract, response.json()["id"])

    assert contract is not None
    assert contract.tenant_id == tenant.id
    assert contract.tenant_id != other_tenant.id


# --- authorization --------------------------------------------------------
def test_ingestion_requires_a_token(client: TestClient) -> None:
    response = client.post(CONTRACTS_PATH, json=BASE_PAYLOAD)
    assert response.status_code == 401


def test_a_viewer_may_not_ingest(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., Any],
    session_factory: sessionmaker[Session],
) -> None:
    make_user(tenant, email="viewer@acme.test", role=UserRole.VIEWER)
    viewer_token = _token(client, tenant, "viewer@acme.test")

    response = _ingest(client, viewer_token)
    assert response.status_code == 403
    assert _contract_rows(session_factory) == 0
    assert _events(session_factory) == []


# --- validation -----------------------------------------------------------
def test_an_inverted_date_range_is_rejected(client: TestClient, token: str) -> None:
    response = _ingest(client, token, start_date="2026-01-01", end_date="2024-01-01")
    assert response.status_code == 422


def test_a_missing_end_date_is_allowed(client: TestClient, token: str) -> None:
    """Dates may be unknown at ingestion; the daily scan skips the contract until
    ``end_date`` exists, which is better than refusing the row."""
    response = _ingest(client, token, end_date=None)
    assert response.status_code == 201, response.text
    assert response.json()["end_date"] is None


@pytest.mark.parametrize(
    "timezone",
    ["", "   ", "Europe/London; DROP TABLE contracts", "../../etc/passwd", "Europe//London"],
)
def test_an_implausible_timezone_is_rejected(client: TestClient, token: str, timezone: str) -> None:
    response = _ingest(client, token, timezone=timezone)
    assert response.status_code == 422


def test_a_blank_customer_reference_is_rejected(client: TestClient, token: str) -> None:
    response = _ingest(client, token, customer_ref="   ")
    assert response.status_code == 422
