"""Customer responses and the escalation queue (gap 15).

Two tables slept through seven slices: ``customer_responses`` and ``escalations``
were created by ``0001`` and nothing ever wrote to them, so
``ContractStatus.RESPONDED`` and ``ContractStatus.ESCALATED`` were states the API
could name and never reach. This file is the proof that both are now reachable,
and that the two clocks they turn on read the same way everywhere.

The file is split the way the slice is:

* **policy** — pure functions in :mod:`app.workers.escalation`, tested without a
  database or a client. These carry the edge cases that are easy to get wrong and
  expensive to get wrong: a deadline that is exactly *now*, a second escalation,
  a reply that arrives after the term ended, a resolved row that someone tries to
  reopen.
* **routes** — the queue and the two mutations, exercised through the real ASGI
  app because the interesting properties live at seams: tenant isolation, the
  audit trail, and the refusal envelope shared with the decision and dispatch
  routers.

The recurring assertion is about *separation*: resolving an escalation by hand
and receiving a customer reply are different facts, and neither may be allowed to
imply the other.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.enums import (
    Channel,
    ContractStatus,
    EscalationStatus,
    ResponseType,
    UserRole,
)
from app.db.models import AuditLog, Contract, CustomerResponse, Escalation, Tenant, User
from app.db.repositories.escalations import (
    OPEN_ESCALATION_STATUSES as REPOSITORY_OPEN_STATUSES,
)
from app.db.session import session_scope
from app.workers import escalation as policy
from app.workers.decision import DecisionRefusedError

LOGIN_PATH = "/api/v1/auth/login"
RESPONSE_PATH = "/api/v1/contracts/{contract_id}/response"
ESCALATE_PATH = "/api/v1/contracts/{contract_id}/escalation"
QUEUE_PATH = "/api/v1/escalations"
ESCALATION_PATH = "/api/v1/escalations/{escalation_id}"

#: Matches ``conftest.DEFAULT_PASSWORD``; protects in-memory databases only.
PASSWORD = "correct-horse-battery-staple"  # nosec B105
EMAIL = "ops@acme.test"
OTHER_EMAIL = "ops@globex.test"
ASSIGNEE_EMAIL = "priya@acme.test"

#: ``DECISIONS.md`` Q2, and the default of ``Settings.escalation_sla_hours``.
SLA_HOURS = 48

#: Refusal codes are API contract, so each is spelled out rather than imported: a
#: renamed constant should fail a test, not silently agree with it.
ALREADY_ESCALATED = "ALREADY_ESCALATED"
ESCALATION_CLOSED = "ESCALATION_CLOSED"
INVALID_TRANSITION = "INVALID_TRANSITION"


# --- helpers --------------------------------------------------------------
def _login(client: TestClient, tenant_slug: str, email: str) -> Response:
    return client.post(
        LOGIN_PATH, json={"tenant": tenant_slug, "email": email, "password": PASSWORD}
    )


def _auth(client: TestClient, tenant_slug: str, email: str) -> dict[str, str]:
    """Log in and return the Authorization header, asserting the login worked."""
    response = _login(client, tenant_slug, email)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _error(response: Response) -> dict[str, Any]:
    payload = response.json()
    assert "error" in payload, payload
    return dict(payload["error"])


def _moment(value: str) -> datetime:
    """Parse an ISO timestamp the API may render with ``Z`` or ``+00:00``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now_naive() -> datetime:
    """The naive UTC the ``DateTime`` columns store, matching ``utc_now``."""
    return datetime.now(UTC).replace(tzinfo=None)


def _audit_rows(
    session_factory: sessionmaker[Session], action: str | None = None
) -> list[AuditLog]:
    """Every audit row in insertion order, optionally filtered to one action."""
    with session_scope(session_factory) as session:
        stmt = select(AuditLog).order_by(AuditLog.id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        return list(session.execute(stmt).scalars())


def _load_contract(session_factory: sessionmaker[Session], contract_id: int) -> Contract:
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract_id)
        assert row is not None
        return row


def _load_escalation(session_factory: sessionmaker[Session], escalation_id: int) -> Escalation:
    with session_scope(session_factory) as session:
        row = session.get(Escalation, escalation_id)
        assert row is not None
        return row


def _responses_for(
    session_factory: sessionmaker[Session], contract_id: int
) -> list[CustomerResponse]:
    with session_scope(session_factory) as session:
        stmt = (
            select(CustomerResponse)
            .where(CustomerResponse.contract_id == contract_id)
            .order_by(CustomerResponse.id)
        )
        return list(session.execute(stmt).scalars())


def _refusal(call: Callable[[], None]) -> str:
    """Run a policy call that must refuse, and return the code it refused with."""
    with pytest.raises(policy.EscalationRefusedError) as caught:
        call()
    return caught.value.code


# --- fixtures -------------------------------------------------------------
@pytest.fixture
def headers(
    client: TestClient, tenant: Tenant, make_user: Callable[..., User]
) -> Callable[..., Any]:
    """Mint an Authorization header for a freshly created user of ``tenant``."""

    def _make(*, email: str = EMAIL, role: UserRole = UserRole.ADMIN) -> dict[str, str]:
        make_user(tenant, email=email, role=role)
        return _auth(client, tenant.slug, email)

    return _make


@pytest.fixture
def make_contract(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> Callable[..., Contract]:
    """Create one committed contract, ``active`` unless a test says otherwise."""

    def _create(
        *,
        status: ContractStatus = ContractStatus.ACTIVE,
        owner: Tenant | None = None,
    ) -> Contract:
        with session_scope(session_factory) as session:
            row = Contract(
                tenant_id=(owner or tenant).id,
                customer_ref="A. Rahman",
                end_date=date(2026, 1, 1),
                status=status,
            )
            session.add(row)
            session.flush()
            return row

    return _create


@pytest.fixture
def make_escalation(session_factory: sessionmaker[Session]) -> Callable[..., Escalation]:
    """Seed an escalation row directly, for queue ordering and isolation tests."""

    def _create(
        contract: Contract,
        *,
        deadline: datetime,
        status: EscalationStatus = EscalationStatus.OPEN,
    ) -> Escalation:
        with session_scope(session_factory) as session:
            row = Escalation(
                tenant_id=contract.tenant_id,
                contract_id=contract.id,
                correlation_id=str(uuid.uuid4()),
                sla_deadline=deadline,
                status=status,
            )
            session.add(row)
            session.flush()
            return row

    return _create


# --- policy: the two clocks ----------------------------------------------
def _transient_contract(status: ContractStatus) -> Contract:
    """An unsaved contract; the policy reads ``status`` and nothing else."""
    return Contract(tenant_id=uuid.uuid4(), customer_ref="A. Rahman", status=status)


def _transient_escalation(status: EscalationStatus, deadline: datetime) -> Escalation:
    """An unsaved escalation, which is enough for every policy function here."""
    return Escalation(
        tenant_id=uuid.uuid4(),
        contract_id=1,
        correlation_id="test",
        sla_deadline=deadline,
        status=status,
    )


def test_the_queue_and_the_policy_agree_on_which_statuses_are_still_open() -> None:
    """The repository duplicates the tuple to avoid importing a worker.

    That duplication is only safe while a test notices when the two drift, so the
    copy is asserted equal here rather than trusted.
    """
    assert REPOSITORY_OPEN_STATUSES == policy.OPEN_ESCALATION_STATUSES


def test_the_deadline_is_the_configured_span_after_the_escalation_is_raised() -> None:
    raised = datetime(2026, 3, 1, 9, 30)
    assert policy.sla_deadline(raised, hours=SLA_HOURS) == datetime(2026, 3, 3, 9, 30)


def test_a_deadline_that_is_exactly_now_is_not_yet_late() -> None:
    """Half-open, like every other window in this app: ``<``, never ``<=``."""
    deadline = datetime(2026, 3, 3, 9, 30)
    row = _transient_escalation(EscalationStatus.OPEN, deadline)

    assert policy.is_overdue(row, deadline) is False
    assert policy.is_overdue(row, deadline + timedelta(seconds=1)) is True


def test_a_resolved_escalation_is_never_late_however_long_it_sat() -> None:
    row = _transient_escalation(EscalationStatus.RESOLVED, datetime(2026, 1, 1))

    assert policy.is_overdue(row, datetime(2030, 1, 1)) is False


def test_an_escalation_in_progress_can_still_go_late() -> None:
    """Someone working the row does not stop the clock."""
    row = _transient_escalation(EscalationStatus.IN_PROGRESS, datetime(2026, 1, 1))

    assert policy.is_overdue(row, datetime(2026, 1, 2)) is True


# --- policy: what may be escalated ---------------------------------------
def test_an_open_escalation_is_reported_before_the_contract_state() -> None:
    """A caller sees one refusal, so the most actionable one has to win."""
    contract = _transient_contract(ContractStatus.EXPIRED)
    open_row = _transient_escalation(EscalationStatus.OPEN, datetime(2026, 1, 1))

    code = _refusal(lambda: policy.refusal_to_raise(contract, open_row))

    assert code == ALREADY_ESCALATED


def test_a_contract_the_customer_answered_cannot_be_escalated() -> None:
    contract = _transient_contract(ContractStatus.RESPONDED)

    assert _refusal(lambda: policy.refusal_to_raise(contract, None)) == (
        policy.REFUSAL_RESPONSE_RECORDED
    )


@pytest.mark.parametrize("status", [ContractStatus.RENEWED, ContractStatus.EXPIRED])
def test_a_contract_whose_term_ended_cannot_be_escalated(status: ContractStatus) -> None:
    contract = _transient_contract(status)

    assert _refusal(lambda: policy.refusal_to_raise(contract, None)) == (
        policy.REFUSAL_CONTRACT_CLOSED
    )


def test_an_active_contract_may_be_escalated() -> None:
    policy.refusal_to_raise(_transient_contract(ContractStatus.ACTIVE), None)


# --- policy: what a reply does to the contract ---------------------------
@pytest.mark.parametrize(
    "status",
    [ContractStatus.ACTIVE, ContractStatus.REMINDER_SENT, ContractStatus.ESCALATED],
)
def test_a_reply_moves_a_live_contract_to_responded(status: ContractStatus) -> None:
    assert policy.status_after_response(_transient_contract(status)) is ContractStatus.RESPONDED


@pytest.mark.parametrize("status", [ContractStatus.RENEWED, ContractStatus.EXPIRED])
def test_a_reply_never_rewrites_a_term_that_already_ended(status: ContractStatus) -> None:
    """The reply is still recorded; "this expired" is not erased by it arriving."""
    assert policy.status_after_response(_transient_contract(status)) is None


# --- policy: the queue state machine -------------------------------------
def test_sla_breached_is_derived_and_cannot_be_set_by_hand() -> None:
    row = _transient_escalation(EscalationStatus.OPEN, datetime(2026, 1, 1))

    code = _refusal(lambda: policy.refusal_to_transition(row, EscalationStatus.SLA_BREACHED))

    assert code == INVALID_TRANSITION


def test_a_resolved_escalation_cannot_be_reopened() -> None:
    row = _transient_escalation(EscalationStatus.RESOLVED, datetime(2026, 1, 1))

    code = _refusal(lambda: policy.refusal_to_transition(row, EscalationStatus.OPEN))

    assert code == ESCALATION_CLOSED


def test_resolving_twice_is_refused_rather_than_treated_as_a_no_op() -> None:
    """A second resolve would rewrite who closed it and why."""
    row = _transient_escalation(EscalationStatus.RESOLVED, datetime(2026, 1, 1))

    assert _refusal(lambda: policy.refusal_to_resolve(row)) == ESCALATION_CLOSED


def test_a_resolved_escalation_cannot_be_assigned_again() -> None:
    row = _transient_escalation(EscalationStatus.RESOLVED, datetime(2026, 1, 1))

    assert _refusal(lambda: policy.refusal_to_assign(row)) == ESCALATION_CLOSED


def test_an_open_escalation_may_be_claimed_and_resolved() -> None:
    row = _transient_escalation(EscalationStatus.OPEN, datetime(2026, 1, 1))

    policy.refusal_to_transition(row, EscalationStatus.IN_PROGRESS)
    policy.refusal_to_resolve(row)
    policy.refusal_to_assign(row)


def test_a_refusal_is_the_same_shape_as_every_other_policy_refusal() -> None:
    """One refusal vocabulary across decision, dispatch and escalation."""
    assert issubclass(policy.EscalationRefusedError, DecisionRefusedError)


# --- routes: raising an escalation ---------------------------------------
def test_raising_an_escalation_opens_the_clock_and_marks_the_contract(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    contract = make_contract()

    response = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=headers())

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == EscalationStatus.OPEN.value
    assert body["overdue"] is False
    assert body["assigned_to"] is None
    assert body["version"] == 1
    # The deadline is 48 h from *now*, not from a request field: an SLA an
    # operator can choose per row is not an SLA.
    expected = datetime.now(UTC) + timedelta(hours=SLA_HOURS)
    assert abs((_moment(body["sla_deadline"]) - expected).total_seconds()) < 120

    assert _load_contract(session_factory, contract.id).status is ContractStatus.ESCALATED

    rows = _audit_rows(session_factory)
    assert [row.action for row in rows] == ["escalation.opened"]
    after = json.loads(rows[0].after_state or "{}")
    assert after["contract_status"] == ContractStatus.ESCALATED.value
    assert rows[0].reason == f"no response within {SLA_HOURS}h"


def test_a_second_escalation_on_one_contract_is_refused(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
) -> None:
    """Two open clocks would make "the oldest one" ambiguous."""
    contract = make_contract()
    auth = headers()
    path = ESCALATE_PATH.format(contract_id=contract.id)
    assert client.post(path, headers=auth).status_code == 201

    second = client.post(path, headers=auth)

    assert second.status_code == 409
    assert _error(second)["code"] == ALREADY_ESCALATED


def test_a_viewer_cannot_raise_an_escalation(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
) -> None:
    contract = make_contract()

    response = client.post(
        ESCALATE_PATH.format(contract_id=contract.id),
        headers=headers(email="viewer@acme.test", role=UserRole.VIEWER),
    )

    assert response.status_code == 403


def test_raising_an_escalation_needs_a_token(
    client: TestClient, make_contract: Callable[..., Contract]
) -> None:
    contract = make_contract()

    assert client.post(ESCALATE_PATH.format(contract_id=contract.id)).status_code == 401


def test_another_tenants_contract_is_a_404_rather_than_a_403(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    other_tenant: Tenant,
) -> None:
    """A 403 would confirm the row exists; the id must stay unprobeable."""
    contract = make_contract(owner=other_tenant)

    response = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=headers())

    assert response.status_code == 404


# --- routes: the queue ----------------------------------------------------
def test_the_queue_lists_the_most_urgent_deadline_first_and_counts_the_late_ones(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    make_escalation: Callable[..., Escalation],
) -> None:
    now = _now_naive()
    late = make_escalation(make_contract(), deadline=now - timedelta(hours=2))
    soon = make_escalation(make_contract(), deadline=now + timedelta(hours=5))

    response = client.get(QUEUE_PATH, headers=headers())

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [late.id, soon.id]
    assert body["count"] == 2
    assert body["overdue"] == 1
    assert body["sla_hours"] == SLA_HOURS
    assert body["items"][0]["overdue"] is True
    assert body["items"][1]["overdue"] is False


def test_a_viewer_may_read_the_queue(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    make_escalation: Callable[..., Escalation],
) -> None:
    make_escalation(make_contract(), deadline=_now_naive() + timedelta(hours=1))

    response = client.get(
        QUEUE_PATH, headers=headers(email="viewer@acme.test", role=UserRole.VIEWER)
    )

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_the_queue_filters_by_status(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    make_escalation: Callable[..., Escalation],
) -> None:
    now = _now_naive()
    open_row = make_escalation(make_contract(), deadline=now + timedelta(hours=1))
    make_escalation(
        make_contract(),
        deadline=now - timedelta(hours=1),
        status=EscalationStatus.RESOLVED,
    )

    response = client.get(
        QUEUE_PATH, params=[("status", EscalationStatus.OPEN.value)], headers=headers()
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [open_row.id]


def test_the_queue_never_shows_another_tenants_rows(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    make_escalation: Callable[..., Escalation],
    other_tenant: Tenant,
) -> None:
    make_escalation(make_contract(owner=other_tenant), deadline=_now_naive() + timedelta(hours=1))

    response = client.get(QUEUE_PATH, headers=headers())

    assert response.status_code == 200
    assert response.json()["items"] == []


# --- routes: recording a customer response -------------------------------
def test_recording_a_response_moves_the_contract_and_closes_its_escalation(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    contract = make_contract()
    auth = headers()
    raised = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth)
    escalation_id = raised.json()["id"]

    response = client.post(
        RESPONSE_PATH.format(contract_id=contract.id),
        headers=auth,
        json={"channel": Channel.EMAIL.value},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["contract_id"] == contract.id
    assert body["contract_status"] == ContractStatus.RESPONDED.value
    assert body["resolved_escalation_id"] == escalation_id
    assert body["response"]["channel"] == Channel.EMAIL.value
    assert body["response"]["response_type"] == ResponseType.REPLY.value

    assert _load_contract(session_factory, contract.id).status is ContractStatus.RESPONDED
    closed = _load_escalation(session_factory, escalation_id)
    assert closed.status is EscalationStatus.RESOLVED
    assert closed.resolution_note == "Customer responded"
    assert closed.resolved_at is not None


def test_recording_a_reply_to_an_escalated_contract_writes_both_audit_rows(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    """One row for the reply, one for what the reply closed, and no third."""
    contract = make_contract()
    auth = headers()
    client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth)

    client.post(
        RESPONSE_PATH.format(contract_id=contract.id),
        headers=auth,
        json={"channel": Channel.SMS.value},
    )

    assert {row.action for row in _audit_rows(session_factory)} == {
        "escalation.opened",
        "response.recorded",
        "escalation.resolved",
    }
    resolved = _audit_rows(session_factory, "escalation.resolved")[0]
    assert resolved.reason == "customer_response"


def test_a_reply_to_a_contract_that_already_ended_is_recorded_without_reopening_it(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    contract = make_contract(status=ContractStatus.EXPIRED)

    response = client.post(
        RESPONSE_PATH.format(contract_id=contract.id),
        headers=headers(),
        json={"channel": Channel.VOICE.value, "response_type": ResponseType.MANUAL.value},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["contract_status"] == ContractStatus.EXPIRED.value
    assert body["resolved_escalation_id"] is None
    assert len(_responses_for(session_factory, contract.id)) == 1


def test_the_reply_body_is_never_stored(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    """The request model has no field for it, and nothing keeps the extra one.

    ``customer_responses`` records *that* the customer answered. A message body
    would be the largest unencrypted PII column in the schema, so a caller that
    sends one gets it ignored rather than persisted — and no audit snapshot may
    echo it either.
    """
    contract = make_contract()
    said = "we will sign next week, call me"

    response = client.post(
        RESPONSE_PATH.format(contract_id=contract.id),
        headers=headers(),
        json={"channel": Channel.EMAIL.value, "message": said},
    )

    assert response.status_code == 201, response.text
    stored = _responses_for(session_factory, contract.id)
    assert len(stored) == 1
    assert stored[0].response_metadata is None
    assert all(
        said not in (row.after_state or "") and said not in (row.before_state or "")
        for row in _audit_rows(session_factory)
    )


def test_a_transcribed_reply_keeps_the_time_it_arrived(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
) -> None:
    """An operator typing up a call log must record the call's clock, not theirs."""
    contract = make_contract()

    response = client.post(
        RESPONSE_PATH.format(contract_id=contract.id),
        headers=headers(),
        json={
            "channel": Channel.VOICE.value,
            "response_type": ResponseType.CALL_ANSWERED.value,
            "received_at": "2026-02-01T10:15:00+06:00",
        },
    )

    assert response.status_code == 201, response.text
    received = _moment(response.json()["response"]["received_at"])
    assert received == datetime(2026, 2, 1, 4, 15, tzinfo=UTC)


def test_a_viewer_cannot_record_a_response(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
) -> None:
    contract = make_contract()

    response = client.post(
        RESPONSE_PATH.format(contract_id=contract.id),
        headers=headers(email="viewer@acme.test", role=UserRole.VIEWER),
        json={"channel": Channel.EMAIL.value},
    )

    assert response.status_code == 403


# --- routes: assigning, claiming, resolving ------------------------------
def test_assigning_claims_the_row_and_names_the_assignee(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    make_user: Callable[..., User],
    tenant: Tenant,
    session_factory: sessionmaker[Session],
) -> None:
    contract = make_contract()
    auth = headers()
    escalation_id = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth).json()[
        "id"
    ]
    assignee = make_user(tenant, email=ASSIGNEE_EMAIL, role=UserRole.OPERATOR)

    response = client.patch(
        ESCALATION_PATH.format(escalation_id=escalation_id),
        headers=auth,
        json={"assigned_to": str(assignee.id)},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assigned_to"] == str(assignee.id)
    # Assigning is claiming: a row in hand must not still read "open".
    assert body["status"] == EscalationStatus.IN_PROGRESS.value

    assigned = _audit_rows(session_factory, "escalation.assigned")[0]
    assert json.loads(assigned.after_state or "{}")["assigned_to"] == str(assignee.id)


def test_assigning_to_a_colleague_in_another_tenant_is_a_404(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    make_user: Callable[..., User],
    other_tenant: Tenant,
) -> None:
    contract = make_contract()
    auth = headers()
    escalation_id = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth).json()[
        "id"
    ]
    outsider = make_user(other_tenant, email=OTHER_EMAIL)

    response = client.patch(
        ESCALATION_PATH.format(escalation_id=escalation_id),
        headers=auth,
        json={"assigned_to": str(outsider.id)},
    )

    assert response.status_code == 404


def test_claiming_without_assigning_records_a_claim_not_an_assignment(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    """Claiming a row and reassigning it are different claims on the work."""
    contract = make_contract()
    auth = headers()
    escalation_id = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth).json()[
        "id"
    ]

    response = client.patch(
        ESCALATION_PATH.format(escalation_id=escalation_id),
        headers=auth,
        json={"status": EscalationStatus.IN_PROGRESS.value},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == EscalationStatus.IN_PROGRESS.value
    assert response.json()["assigned_to"] is None
    assert _audit_rows(session_factory, "escalation.claimed") != []
    assert _audit_rows(session_factory, "escalation.assigned") == []


def test_resolving_by_hand_closes_the_queue_item_and_leaves_the_contract_escalated(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    """Two different facts: someone worked this, and the customer answered.

    A hand-resolved escalation must not claim the customer replied, so the
    contract keeps ``escalated`` and the reminder pipeline still knows it is quiet.
    """
    contract = make_contract()
    auth = headers()
    escalation_id = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth).json()[
        "id"
    ]

    response = client.patch(
        ESCALATION_PATH.format(escalation_id=escalation_id),
        headers=auth,
        json={
            "status": EscalationStatus.RESOLVED.value,
            "resolution_note": "called back, will sign next week",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == EscalationStatus.RESOLVED.value
    assert body["resolution_note"] == "called back, will sign next week"
    assert body["resolved_at"] is not None
    assert _load_contract(session_factory, contract.id).status is ContractStatus.ESCALATED


def test_resolving_twice_is_refused(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
) -> None:
    contract = make_contract()
    auth = headers()
    escalation_id = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth).json()[
        "id"
    ]
    path = ESCALATION_PATH.format(escalation_id=escalation_id)
    resolved = {"status": EscalationStatus.RESOLVED.value, "resolution_note": "done"}
    assert client.patch(path, headers=auth, json=resolved).status_code == 200

    second = client.patch(path, headers=auth, json=resolved)

    assert second.status_code == 409
    assert _error(second)["code"] == ESCALATION_CLOSED


def test_the_route_refuses_to_reopen_a_resolved_escalation(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
) -> None:
    contract = make_contract()
    auth = headers()
    escalation_id = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth).json()[
        "id"
    ]
    path = ESCALATION_PATH.format(escalation_id=escalation_id)
    client.patch(path, headers=auth, json={"status": EscalationStatus.RESOLVED.value})

    reopened = client.patch(path, headers=auth, json={"status": EscalationStatus.OPEN.value})

    assert reopened.status_code == 409
    assert _error(reopened)["code"] == ESCALATION_CLOSED


def test_sla_breached_cannot_be_set_by_hand(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
) -> None:
    """A hand-set ``sla_breached`` would contradict the row's own deadline."""
    contract = make_contract()
    auth = headers()
    escalation_id = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth).json()[
        "id"
    ]

    response = client.patch(
        ESCALATION_PATH.format(escalation_id=escalation_id),
        headers=auth,
        json={"status": EscalationStatus.SLA_BREACHED.value},
    )

    assert response.status_code == 409
    assert _error(response)["code"] == INVALID_TRANSITION


def test_patching_the_status_a_row_already_holds_writes_nothing(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    session_factory: sessionmaker[Session],
) -> None:
    """An audit row claiming an edit that did not happen is worse than no row."""
    contract = make_contract()
    auth = headers()
    escalation_id = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth).json()[
        "id"
    ]
    path = ESCALATION_PATH.format(escalation_id=escalation_id)
    claimed = {"status": EscalationStatus.IN_PROGRESS.value}
    assert client.patch(path, headers=auth, json=claimed).status_code == 200

    again = client.patch(path, headers=auth, json=claimed)

    assert again.status_code == 200
    assert again.json()["status"] == EscalationStatus.IN_PROGRESS.value
    assert len(_audit_rows(session_factory, "escalation.claimed")) == 1


def test_a_stale_version_is_refused(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
) -> None:
    """Supplying the version turns an edit into a checked claim."""
    contract = make_contract()
    auth = headers()
    escalation_id = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth).json()[
        "id"
    ]

    response = client.patch(
        ESCALATION_PATH.format(escalation_id=escalation_id),
        headers=auth,
        json={"status": EscalationStatus.IN_PROGRESS.value, "version": 99},
    )

    assert response.status_code == 409


def test_the_version_it_holds_is_accepted(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
) -> None:
    contract = make_contract()
    auth = headers()
    raised = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth)
    escalation_id = raised.json()["id"]

    response = client.patch(
        ESCALATION_PATH.format(escalation_id=escalation_id),
        headers=auth,
        json={
            "status": EscalationStatus.IN_PROGRESS.value,
            "version": raised.json()["version"],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["version"] > raised.json()["version"]


def test_another_tenants_escalation_is_a_404(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
    make_escalation: Callable[..., Escalation],
    other_tenant: Tenant,
) -> None:
    row = make_escalation(
        make_contract(owner=other_tenant), deadline=_now_naive() + timedelta(hours=1)
    )

    response = client.patch(
        ESCALATION_PATH.format(escalation_id=row.id),
        headers=headers(),
        json={"status": EscalationStatus.IN_PROGRESS.value},
    )

    assert response.status_code == 404


# --- request-shape refusals ----------------------------------------------
def test_an_empty_patch_body_is_refused(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
) -> None:
    contract = make_contract()
    auth = headers()
    escalation_id = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth).json()[
        "id"
    ]

    response = client.patch(
        ESCALATION_PATH.format(escalation_id=escalation_id), headers=auth, json={}
    )

    assert response.status_code == 422


def test_a_resolution_note_without_a_resolve_is_refused(
    client: TestClient,
    headers: Callable[..., Any],
    make_contract: Callable[..., Contract],
) -> None:
    """The note would land on an open row where nothing reads it."""
    contract = make_contract()
    auth = headers()
    escalation_id = client.post(ESCALATE_PATH.format(contract_id=contract.id), headers=auth).json()[
        "id"
    ]

    response = client.patch(
        ESCALATION_PATH.format(escalation_id=escalation_id),
        headers=auth,
        json={"resolution_note": "will chase tomorrow"},
    )

    assert response.status_code == 422
