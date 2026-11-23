"""Notification dispatch (F10).

``Notification`` was the last model in the schema with no writer: it described
what the system intended to record rather than what it did. This slice gives it a
producer (``POST /notifications``), an attempt path (``POST /…/dispatch``), and the
policy that decides whether either is allowed.

The tests are grouped around four claims:

* **The gates run in the documented order.** A row with several problems must be
  told about the *first* one, so the tests pin which refusal wins rather than just
  that something was refused.
* **Consent is re-read at send time.** Scheduling and sending are separated by an
  arbitrary interval, so the recurring assertion is that a contract which consented
  when planned cannot be contacted after consent is withdrawn.
* **Quiet hours defer, they do not refuse.** A deferral is the system working, and
  is evaluated on the *customer's* wall clock — including across a DST transition,
  which is why those cases are asserted as UTC instants.
* **Nothing readable is PII.** No response and no row carries the contact value.

Two levels are used deliberately. The HTTP tests prove the route wiring and the
refusal envelope; the policy tests call ``dispatch_notification`` with a fake
sender, because the shipped transport is ``none`` — a 200 ``sent`` response is
unreachable through HTTP until a real transport exists, and pretending otherwise
in a route test would only test the fake.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

# The conflict mapping for a lost optimistic-lock race (gap 21) is a module-private
# helper on purpose — it is not part of the route surface. It is tested directly
# because the shared in-memory connection this suite runs on cannot interleave a
# second writer mid-request; see ``test_a_lost_race_is_reported_as_a_conflict``.
from app.api.v1.notifications import _commit_or_conflict
from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.db.base import utc_now
from app.db.enums import (
    Channel,
    ContractStatus,
    FieldName,
    FieldStatus,
    NotificationStatus,
    UserRole,
)
from app.db.models import AgentDecision, Contract, ExtractedField, Notification, Tenant, User
from app.db.repositories import (
    ALL_CHANNELS,
    CONTACTED_STATUSES,
    UNDELIVERED_STATUSES,
    pending_overdue_counts,
)
from app.db.session import session_scope
from app.main import create_app
from app.workers.dispatch import (
    DispatchRefusedError,
    OutgoingMessage,
    SenderError,
    dispatch_notification,
    milestone_for,
    notification_idempotency_key,
    quiet_hours_until,
)

LOGIN_PATH = "/api/v1/auth/login"
NOTIFICATIONS_PATH = "/api/v1/notifications"
DECISIONS_PATH = "/api/v1/agent/decisions"

#: Matches ``conftest.DEFAULT_PASSWORD``; protects in-memory databases only.
PASSWORD = "correct-horse-battery-staple"  # nosec B105
EMAIL = "ops@acme.test"

#: The value no response and no row may disclose. Distinct string so a leak is
#: greppable in a failure message.
CONTACT_EMAIL = "a.rahman@example.test"

#: One key for the whole module, so the value a helper seals is the value the app
#: under test unseals. A per-test key would make every send fail with a decryption
#: error that looks like a policy refusal.
ENCRYPTION_KEY = Fernet.generate_key().decode("ascii")
CIPHER = FieldCipher(ENCRYPTION_KEY)


# --- fixtures and helpers -------------------------------------------------
def utc_today() -> date:
    """The server's UTC calendar day, used to place a contract in a window."""
    return datetime.now(UTC).date()


def make_contract(
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    *,
    end_in_days: int | None = 40,
    timezone_name: str = "UTC",
    status: ContractStatus = ContractStatus.ACTIVE,
    consent_email: bool = True,
    consent_sms: bool = False,
    consent_voice: bool = False,
    do_not_contact: bool = False,
) -> Contract:
    """Create one committed contract, with the contact policy spelled out.

    ``end_in_days=40`` lands the contract inside the loosest milestone only, so the
    due reminder is unambiguously ``T-60``; a default that sat in two windows would
    make the scheduling assertions depend on the cadence order rather than on them.
    """
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=tenant.id,
            customer_ref="A. Rahman",
            end_date=None if end_in_days is None else utc_today() + timedelta(days=end_in_days),
            timezone=timezone_name,
            status=status,
            consent_email=consent_email,
            consent_sms=consent_sms,
            consent_voice=consent_voice,
            do_not_contact=do_not_contact,
        )
        session.add(row)
        session.flush()
        return row


def add_field(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    field_name: FieldName = FieldName.EMAIL,
    value: str | None = CONTACT_EMAIL,
    status: FieldStatus = FieldStatus.APPROVED,
) -> int:
    """Store one extracted field and return its id.

    Sealed with :data:`CIPHER` because the dispatch policy reads
    ``normalized_value`` and decrypts it (gap 20): a row written in the clear is
    refused as unreadable rather than sent.
    """
    with session_scope(session_factory) as session:
        row = ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=field_name,
            raw_value=None,
            normalized_value=CIPHER.encrypt(value) if value is not None else None,
            confidence=0.93,
            status=status,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def make_decision(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    channel: Channel = Channel.EMAIL,
    needs_review: bool = False,
) -> int:
    """Store one decision and return its id.

    Written directly rather than through ``POST /agent/decisions`` because two cases
    below need a decision the F9 policy can never produce (``needs_review=True``).
    """
    with session_scope(session_factory) as session:
        row = AgentDecision(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            channel=channel,
            confidence=0.9,
            reasoning="deterministic channel selection",
            needs_review=needs_review,
            correlation_id=str(uuid.uuid4()),
            prompt_version="policy.channel.selection/v1",
        )
        session.add(row)
        session.flush()
        return int(row.id)


def make_notification(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    channel: Channel = Channel.EMAIL,
    template: str = "T-60",
    scheduled_date: date | None = None,
    scheduled_at: datetime | None = None,
    status: NotificationStatus = NotificationStatus.PENDING,
    attempts: int = 0,
    last_error: str | None = None,
    provider_message_id: str | None = None,
    sent_at: datetime | None = None,
    created_at: datetime | None = None,
) -> int:
    """Insert one notification row directly and return its id.

    Used where the *route* is not the subject — a row parked after its budget is
    spent, or one whose milestone slot would already be taken.

    ``created_at`` is settable because some rows are aged by the moment they were
    written rather than by a due time: a row with ``scheduled_at`` NULL is due the
    instant it exists, and the only way to make it *overdue* in a test is to move
    its birth into the past.
    """
    slot = scheduled_date if scheduled_date is not None else utc_today() - timedelta(days=20)
    with session_scope(session_factory) as session:
        row = Notification(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            channel=channel,
            template=template,
            status=status,
            scheduled_date=slot,
            scheduled_at=scheduled_at,
            created_at=created_at if created_at is not None else utc_now(),
            sent_at=sent_at,
            idempotency_key=notification_idempotency_key(contract.id, template, slot),
            attempts=attempts,
            last_error=last_error,
            provider_message_id=provider_message_id,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def load_notification(session_factory: sessionmaker[Session], notification_id: int) -> Notification:
    """Re-read a row through a fresh session, so assertions see committed state."""
    with session_scope(session_factory) as session:
        row = session.get(Notification, notification_id)
        assert row is not None
        return row


def load_contract(session_factory: sessionmaker[Session], contract_id: int) -> Contract:
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract_id)
        assert row is not None
        return row


def notifications(session_factory: sessionmaker[Session]) -> list[Notification]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(Notification).order_by(Notification.id)).scalars())


def login(client: TestClient, tenant: Tenant, email: str = EMAIL) -> dict[str, str]:
    response = client.post(
        LOGIN_PATH,
        json={"tenant": tenant.slug, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def auth(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    *,
    role: UserRole = UserRole.OPERATOR,
    email: str = EMAIL,
) -> dict[str, str]:
    make_user(tenant, email=email, role=role)
    return login(client, tenant, email)


@contextmanager
def _client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    quiet_start: int,
    quiet_end: int,
) -> Iterator[TestClient]:
    """A TestClient whose quiet window is stated in absolute hours.

    The window has to be moved per test rather than trusted to the default, because
    a suite that ran at 03:00 UTC would otherwise fail the "a message is sent" cases
    for a reason that has nothing to do with the code under test.

    The encryption key is pinned to :data:`ENCRYPTION_KEY` so the routes unseal what
    :func:`add_field` sealed; without it every dispatch is a ``503``.
    """
    tuned = settings.model_copy(
        update={
            "quiet_hours_start": quiet_start,
            "quiet_hours_end": quiet_end,
            "encryption_key": ENCRYPTION_KEY,
        }
    )
    with TestClient(create_app(tuned, session_factory=session_factory)) as test_client:
        yield test_client


@pytest.fixture
def contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    """A live contract consenting to email, expiring inside the T-60 window only."""
    return make_contract(session_factory, tenant)


@pytest.fixture
def contact_field(session_factory: sessionmaker[Session], contract: Contract) -> int:
    """The approved email value any send would go to."""
    return add_field(session_factory, contract)


@pytest.fixture
def active_client(
    settings: Settings, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    """A client whose quiet window starts three hours from now — i.e. open now."""
    hour = datetime.now(UTC).hour
    with _client(
        settings, session_factory, quiet_start=(hour + 3) % 24, quiet_end=(hour + 4) % 24
    ) as test_client:
        yield test_client


@pytest.fixture
def quiet_window(settings: Settings) -> tuple[int, int]:
    """The quiet window ``quiet_client`` runs with: ``[now-1h, now+2h)`` local.

    Exposed as its own fixture because the assertions need the *tuned* window, not
    ``settings.quiet_hours_end``: that attribute is the default (8) and the client's
    copy is what the policy actually reads. Three hours wide, so a test that starts
    near an hour boundary cannot fall out of the window between fixture creation and
    the request.
    """
    hour = datetime.now(UTC).hour
    return (hour - 1) % 24, (hour + 2) % 24


@pytest.fixture
def quiet_client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    quiet_window: tuple[int, int],
) -> Iterator[TestClient]:
    """A client inside a quiet window: ``[now-1h, now+2h)`` local."""
    quiet_start, quiet_end = quiet_window
    with _client(
        settings, session_factory, quiet_start=quiet_start, quiet_end=quiet_end
    ) as test_client:
        yield test_client


# --- scheduling ------------------------------------------------------------
def test_scheduling_writes_a_pending_row_for_the_due_milestone(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    make_decision(session_factory, contract)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": contract.id}, headers=headers
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["channel"] == "email"
    assert body["template"] == "T-60"
    assert body["attempts"] == 0
    assert body["sent_at"] is None
    # Derived only from the slot: a key containing "when the row was written"
    # could never collide, and so could not be an idempotency guard.
    assert body["idempotency_key"] == notification_idempotency_key(
        contract.id, "T-60", utc_today() - timedelta(days=20)
    )
    assert len(notifications(session_factory)) == 1


def test_a_second_schedule_for_the_same_slot_names_the_row_that_won(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    make_decision(session_factory, contract)
    headers = auth(active_client, tenant, make_user)

    first = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": contract.id}, headers=headers
    )
    assert first.status_code == 201, first.text

    second = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": contract.id}, headers=headers
    )

    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "NOTIFICATION_EXISTS"
    assert error["details"]["notification_id"] == first.json()["id"]
    assert len(notifications(session_factory)) == 1


def test_scheduling_without_a_decision_names_the_missing_step(
    active_client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": contract.id}, headers=headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NO_DECISION"


def test_scheduling_from_another_contracts_decision_is_refused(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    other = make_contract(session_factory, tenant)
    foreign_decision = make_decision(session_factory, other)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        NOTIFICATIONS_PATH,
        json={"contract_id": contract.id, "decision_id": foreign_decision},
        headers=headers,
    )

    # 409, not 422: both ids exist, the pairing does not. Silently scheduling
    # contract A's decision against contract B is what this refuses.
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "DECISION_MISMATCH"
    assert error["details"]["decision_contract_id"] == other.id


def test_an_explicit_decision_id_is_honoured(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    decision_id = make_decision(session_factory, contract)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        NOTIFICATIONS_PATH,
        json={"contract_id": contract.id, "decision_id": decision_id},
        headers=headers,
    )

    assert response.status_code == 201, response.text


def test_an_unknown_decision_is_not_found(
    active_client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        NOTIFICATIONS_PATH,
        json={"contract_id": contract.id, "decision_id": 9999},
        headers=headers,
    )

    assert response.status_code == 404


def test_a_contract_without_consent_cannot_be_scheduled(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    unconsented = make_contract(session_factory, tenant, consent_email=False)
    add_field(session_factory, unconsented)
    make_decision(session_factory, unconsented)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": unconsented.id}, headers=headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NO_CONSENT"
    assert notifications(session_factory) == []


def test_an_opted_out_customer_cannot_be_scheduled(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    opted_out = make_contract(session_factory, tenant, do_not_contact=True)
    add_field(session_factory, opted_out)
    make_decision(session_factory, opted_out)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": opted_out.id}, headers=headers
    )

    # The opt-out outranks the grant: consent_email is still True on this row.
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONTACT_REFUSED"


def test_a_rejected_field_blocks_scheduling(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    add_field(session_factory, contract, field_name=FieldName.PHONE, status=FieldStatus.REJECTED)
    make_decision(session_factory, contract)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": contract.id}, headers=headers
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PIPELINE_BLOCKED"
    assert error["details"]["fields"] == ["phone"]


def test_a_decision_flagged_for_review_schedules_nothing(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    make_decision(session_factory, contract, needs_review=True)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": contract.id}, headers=headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NEEDS_REVIEW"


def test_a_contract_outside_every_window_is_refused(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    distant = make_contract(session_factory, tenant, end_in_days=200)
    add_field(session_factory, distant)
    make_decision(session_factory, distant)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": distant.id}, headers=headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NOT_IN_WINDOW"


def test_a_contract_without_an_end_date_is_refused(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    undated = make_contract(session_factory, tenant, end_in_days=None)
    add_field(session_factory, undated)
    make_decision(session_factory, undated)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": undated.id}, headers=headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MISSING_END_DATE"


def test_a_viewer_may_not_schedule(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    make_decision(session_factory, contract)
    headers = auth(active_client, tenant, make_user, role=UserRole.VIEWER)

    response = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": contract.id}, headers=headers
    )

    assert response.status_code == 403
    assert notifications(session_factory) == []


def test_scheduling_requires_a_token(
    active_client: TestClient, contract: Contract, contact_field: int
) -> None:
    response = active_client.post(NOTIFICATIONS_PATH, json={"contract_id": contract.id})

    assert response.status_code == 401


def test_scheduling_for_another_tenants_contract_is_not_found(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    other_tenant: Tenant,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    foreign = make_contract(session_factory, other_tenant)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": foreign.id}, headers=headers
    )

    assert response.status_code == 404


def test_scheduling_under_quiet_hours_stores_the_window_opening(
    quiet_client: TestClient,
    quiet_window: tuple[int, int],
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    make_decision(session_factory, contract)
    headers = auth(quiet_client, tenant, make_user)

    response = quiet_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": contract.id}, headers=headers
    )

    # The row is still created — scheduling is a plan, not a contact — but it is
    # stamped for the moment the window opens rather than for now. The contract is
    # in UTC, so the stored instant's UTC hour is the local one.
    _, quiet_end = quiet_window
    assert response.status_code == 201, response.text
    scheduled_at = datetime.fromisoformat(response.json()["scheduled_at"])
    local = scheduled_at.astimezone(ZoneInfo("UTC"))
    assert local.hour == quiet_end
    assert local > datetime.now(UTC)


# --- reading ---------------------------------------------------------------
def test_the_list_is_tenant_scoped(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    other_tenant: Tenant,
    make_user: Callable[..., User],
    tenant: Tenant,
    contract: Contract,
) -> None:
    make_notification(session_factory, contract)
    foreign_contract = make_contract(session_factory, other_tenant)
    make_notification(session_factory, foreign_contract)
    headers = auth(active_client, tenant, make_user)

    response = active_client.get(NOTIFICATIONS_PATH, headers=headers)

    assert response.status_code == 200
    # A guessed contract id from another organisation yields an empty list, never
    # somebody else's outbound messages.
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["contract_id"] == contract.id


def test_the_list_filters_by_status_and_channel(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
) -> None:
    make_notification(session_factory, contract, template="T-60", status=NotificationStatus.SENT)
    make_notification(session_factory, contract, template="T-30", status=NotificationStatus.PENDING)
    make_notification(
        session_factory, contract, template="T-14", channel=Channel.SMS, scheduled_date=utc_today()
    )
    headers = auth(active_client, tenant, make_user)

    sent = active_client.get(f"{NOTIFICATIONS_PATH}?status=sent", headers=headers)
    sms = active_client.get(f"{NOTIFICATIONS_PATH}?channel=sms", headers=headers)

    assert [row["template"] for row in sent.json()["items"]] == ["T-60"]
    assert [row["template"] for row in sms.json()["items"]] == ["T-14"]


def test_the_list_is_bounded(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
) -> None:
    for index, template in enumerate(("T-60", "T-30", "T-14")):
        make_notification(
            session_factory,
            contract,
            template=template,
            scheduled_date=utc_today() - timedelta(days=index),
        )
    headers = auth(active_client, tenant, make_user)

    bounded = active_client.get(f"{NOTIFICATIONS_PATH}?limit=2", headers=headers)
    over_max = active_client.get(f"{NOTIFICATIONS_PATH}?limit=500", headers=headers)
    zero = active_client.get(f"{NOTIFICATIONS_PATH}?limit=0", headers=headers)

    assert bounded.json()["count"] == 2
    # No caller can ask for a whole tenant's delivery table.
    assert over_max.status_code == 422
    assert zero.status_code == 422


def test_a_viewer_may_read_the_list(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
) -> None:
    make_notification(session_factory, contract)
    headers = auth(active_client, tenant, make_user, role=UserRole.VIEWER)

    response = active_client.get(NOTIFICATIONS_PATH, headers=headers)

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_the_list_never_contains_a_recipient_value(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    make_notification(session_factory, contract)
    headers = auth(active_client, tenant, make_user)

    response = active_client.get(NOTIFICATIONS_PATH, headers=headers)

    assert response.status_code == 200
    # Reading *who* it went to is the audited reveal route's job, not a listing any
    # viewer can page through.
    assert CONTACT_EMAIL not in response.text


def test_the_list_names_the_contract_it_was_filtered_to(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
) -> None:
    make_notification(session_factory, contract)
    headers = auth(active_client, tenant, make_user)

    response = active_client.get(f"{NOTIFICATIONS_PATH}?contract_id={contract.id}", headers=headers)

    assert response.json()["contract_id"] == contract.id
    assert response.json()["count"] == 1


# --- dispatching: refusals ------------------------------------------------
def test_dispatch_without_a_transport_refuses(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    notification_id = make_notification(session_factory, contract)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        f"{NOTIFICATIONS_PATH}/{notification_id}/dispatch", headers=headers
    )

    assert response.status_code == 409
    # The shipped transport is ``none``, and it refuses rather than pretending: a
    # silent no-op would mark the row ``sent`` with nothing delivered.
    assert response.json()["error"]["code"] == "SENDER_UNAVAILABLE"
    assert load_notification(session_factory, notification_id).status is NotificationStatus.PENDING


def test_a_spent_retry_budget_parks_the_row_and_the_409_survives(
    active_client: TestClient,
    settings: Settings,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    notification_id = make_notification(
        session_factory, contract, attempts=settings.notification_retry_max
    )
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        f"{NOTIFICATIONS_PATH}/{notification_id}/dispatch", headers=headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ATTEMPTS_EXHAUSTED"
    # The policy mutated the row before refusing; that transition has to be
    # committed on the error path too, or the row stays pending forever.
    assert load_notification(session_factory, notification_id).status is NotificationStatus.FAILED


def test_consent_withdrawn_after_scheduling_is_refused_at_send_time(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    # The contract must consent to a *second* channel, or withdrawing email leaves
    # it consenting to nothing and the earlier "no consent at all" gate fires
    # instead. This test is about the narrower refusal — consent exists, just not
    # for the channel the decision chose.
    contract = make_contract(session_factory, tenant, consent_sms=True)
    add_field(session_factory, contract)
    make_decision(session_factory, contract)
    headers = auth(active_client, tenant, make_user)
    scheduled = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": contract.id}, headers=headers
    )
    assert scheduled.status_code == 201, scheduled.text

    with session_scope(session_factory) as session:
        row = session.get(Contract, contract.id)
        assert row is not None
        row.consent_email = False

    response = active_client.post(
        f"{NOTIFICATIONS_PATH}/{scheduled.json()['id']}/dispatch", headers=headers
    )

    assert response.status_code == 409
    # The decision still says email; the contract no longer consents. Send-time
    # consent is re-read rather than trusted from the decision row.
    assert response.json()["error"]["code"] == "CHANNEL_NOT_CONSENTED"


def test_a_field_rejected_after_scheduling_blocks_the_send(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    make_decision(session_factory, contract)
    headers = auth(active_client, tenant, make_user)
    scheduled = active_client.post(
        NOTIFICATIONS_PATH, json={"contract_id": contract.id}, headers=headers
    )
    assert scheduled.status_code == 201, scheduled.text

    add_field(
        session_factory, contract, field_name=FieldName.PHONE, status=FieldStatus.PENDING_REVIEW
    )

    response = active_client.post(
        f"{NOTIFICATIONS_PATH}/{scheduled.json()['id']}/dispatch", headers=headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PIPELINE_BLOCKED"


def test_a_sent_row_cannot_be_dispatched_again(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    notification_id = make_notification(
        session_factory, contract, status=NotificationStatus.SENT, attempts=1
    )
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        f"{NOTIFICATIONS_PATH}/{notification_id}/dispatch", headers=headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NOT_PENDING"


def test_a_row_scheduled_for_later_is_not_yet_due(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    # ``scheduled_at`` is stored naive-UTC (``app.db.base``), so the future instant
    # is built with the same helper the models use rather than an aware datetime
    # the column cannot round-trip.
    notification_id = make_notification(
        session_factory, contract, scheduled_at=utc_now() + timedelta(hours=6)
    )
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        f"{NOTIFICATIONS_PATH}/{notification_id}/dispatch", headers=headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NOT_YET_DUE"


def test_an_unresolvable_timezone_refuses_rather_than_assuming_utc(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    notification_id = make_notification(session_factory, contract)
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract.id)
        assert row is not None
        row.timezone = "Mars/Olympus_Mons"
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        f"{NOTIFICATIONS_PATH}/{notification_id}/dispatch", headers=headers
    )

    assert response.status_code == 409
    # Assuming UTC would shift the quiet window by up to fourteen hours and, on the
    # wrong edge, authorise a 03:00 call.
    assert response.json()["error"]["code"] == "UNKNOWN_TIMEZONE"


def test_dispatch_of_another_tenants_row_is_not_found(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    other_tenant: Tenant,
    make_user: Callable[..., User],
    tenant: Tenant,
) -> None:
    foreign_contract = make_contract(session_factory, other_tenant)
    notification_id = make_notification(session_factory, foreign_contract)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(
        f"{NOTIFICATIONS_PATH}/{notification_id}/dispatch", headers=headers
    )

    assert response.status_code == 404


def test_a_viewer_may_not_dispatch(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    notification_id = make_notification(session_factory, contract)
    headers = auth(active_client, tenant, make_user, role=UserRole.VIEWER)

    response = active_client.post(
        f"{NOTIFICATIONS_PATH}/{notification_id}/dispatch", headers=headers
    )

    assert response.status_code == 403


def test_quiet_hours_defer_instead_of_refusing(
    quiet_client: TestClient,
    quiet_window: tuple[int, int],
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
    contact_field: int,
) -> None:
    notification_id = make_notification(session_factory, contract, scheduled_at=None)
    headers = auth(quiet_client, tenant, make_user)

    response = quiet_client.post(
        f"{NOTIFICATIONS_PATH}/{notification_id}/dispatch", headers=headers
    )

    # 200, not 409: quiet hours are the system working. A 409 would train a caller
    # to retry through the night.
    _, quiet_end = quiet_window
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sent"] is False
    assert body["error"] is None
    deferred_until = datetime.fromisoformat(body["deferred_until"])
    assert deferred_until.astimezone(ZoneInfo("UTC")).hour == quiet_end

    stored = load_notification(session_factory, notification_id)
    assert stored.status is NotificationStatus.PENDING
    # The transport was never reached: the deferral happens before the recipient is
    # read, so no attempt is consumed.
    assert stored.attempts == 0


# --- retrying -------------------------------------------------------------
def test_retry_requeues_a_parked_row(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
) -> None:
    notification_id = make_notification(
        session_factory,
        contract,
        status=NotificationStatus.FAILED,
        attempts=3,
        last_error="smtp: connection refused",
        provider_message_id="provider-1",
        sent_at=datetime(2026, 1, 2, 3, 4, 5),
    )
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(f"{NOTIFICATIONS_PATH}/{notification_id}/retry", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["attempts"] == 0
    assert body["last_error"] is None
    # Evidence a transport was once reached is kept, the same way outbox replay
    # keeps the error it replaced.
    assert body["provider_message_id"] == "provider-1"
    assert body["sent_at"] is not None
    requeued = load_notification(session_factory, notification_id)
    assert requeued.status is NotificationStatus.PENDING
    # Gap 21: the requeue UPDATE carries a version predicate, so a competing write
    # now matches zero rows instead of silently overwriting this one.
    assert requeued.version == 2


def test_retry_refuses_a_row_that_is_not_failed(
    active_client: TestClient,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
    contract: Contract,
) -> None:
    notification_id = make_notification(session_factory, contract)
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(f"{NOTIFICATIONS_PATH}/{notification_id}/retry", headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NOT_FAILED"


def test_retry_of_an_unknown_row_is_not_found(
    active_client: TestClient, tenant: Tenant, make_user: Callable[..., User]
) -> None:
    headers = auth(active_client, tenant, make_user)

    response = active_client.post(f"{NOTIFICATIONS_PATH}/9999/retry", headers=headers)

    assert response.status_code == 404


def test_a_lost_race_is_reported_as_a_conflict(
    session_factory: sessionmaker[Session], contract: Contract
) -> None:
    """Gap 21: a stale commit is a 409, not a 500.

    Two workers dispatch the same row; the loser's ``UPDATE`` matches zero rows
    because the winner already committed version 2. SQLAlchemy raises
    ``StaleDataError`` for that, and left unhandled it would be an unhandled
    exception — a 500 for a request whose only fault was arriving second.

    Driven through the helper rather than over HTTP because every session in this
    suite shares one in-memory connection (``StaticPool``), so a competing writer
    cannot be committed behind an in-flight request. The race below is real: the
    out-of-band ``UPDATE`` bumps the stored version, and the dirty row's commit is
    what SQLAlchemy rejects.
    """
    notification_id = make_notification(session_factory, contract)
    session = session_factory()
    try:
        notification = session.get(Notification, notification_id)
        assert notification is not None
        session.execute(
            update(Notification)
            .where(Notification.id == notification_id)
            .values(version=notification.version + 1)
            .execution_options(synchronize_session=False)
        )
        notification.attempts += 1

        with pytest.raises(HTTPException) as refused:
            _commit_or_conflict(session, notification_id)
    finally:
        session.close()

    assert refused.value.status_code == 409
    # ``HTTPException.detail`` is annotated ``str`` in starlette even though FastAPI
    # accepts a JSON-able payload, so the shape has to be asserted, not assumed.
    detail = cast(dict[str, Any], refused.value.detail)
    assert detail["code"] == "CONCURRENT_UPDATE"
    assert detail["details"] == {"notification_id": notification_id}


# --- the policy itself: a configured transport -----------------------------
class RecordingSender:
    """A transport that accepts every message and remembers what it was given."""

    def __init__(self, provider_id: str | None = "provider-1") -> None:
        self.provider_id = provider_id
        self.sent: list[OutgoingMessage] = []

    @property
    def channels(self) -> frozenset[Channel]:
        return frozenset({Channel.EMAIL})

    def send(self, message: OutgoingMessage) -> str | None:
        self.sent.append(message)
        return self.provider_id


class FailingSender:
    """A transport that was reached and refused the message."""

    @property
    def channels(self) -> frozenset[Channel]:
        return frozenset({Channel.EMAIL})

    def send(self, message: OutgoingMessage) -> str | None:
        raise SenderError("smtp: 421 try again later")


def _open_window(settings: Settings) -> Settings:
    """Settings whose quiet window is not open now, for direct policy calls."""
    hour = datetime.now(UTC).hour
    return settings.model_copy(
        update={"quiet_hours_start": (hour + 3) % 24, "quiet_hours_end": (hour + 4) % 24}
    )


def test_a_configured_transport_sends_and_records_the_provider_id(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
    contact_field: int,
) -> None:
    notification_id = make_notification(session_factory, contract)
    notification = load_notification(session_factory, notification_id)
    sender = RecordingSender()

    outcome = dispatch_notification(
        notification,
        load_contract(session_factory, contract.id),
        sender,
        lambda: CONTACT_EMAIL,
        now=datetime.now(UTC),
        settings=_open_window(settings),
    )

    assert outcome.sent is True
    assert outcome.error is None
    assert notification.status is NotificationStatus.SENT
    assert notification.provider_message_id == "provider-1"
    assert notification.attempts == 1
    assert notification.sent_at is not None
    assert len(sender.sent) == 1
    # The milestone, not a rendered body: composing content is the transport's job.
    assert sender.sent[0].template == "T-60"
    assert sender.sent[0].channel is Channel.EMAIL


def test_the_recipient_never_appears_in_a_message_repr(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
    contact_field: int,
) -> None:
    message = OutgoingMessage(
        channel=Channel.EMAIL,
        template="T-60",
        contract_id=contract.id,
        idempotency_key="1:T-60:2026-01-01",
        recipient=CONTACT_EMAIL,
    )

    # A traceback, a debug log or a pytest failure that echoes the dataclass must
    # not print an address into a log sink.
    assert CONTACT_EMAIL not in repr(message)
    assert CONTACT_EMAIL not in str(message)


def test_a_transport_failure_is_recorded_not_raised(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
    contact_field: int,
) -> None:
    notification = load_notification(session_factory, make_notification(session_factory, contract))

    outcome = dispatch_notification(
        notification,
        load_contract(session_factory, contract.id),
        FailingSender(),
        lambda: CONTACT_EMAIL,
        now=datetime.now(UTC),
        settings=_open_window(settings),
    )

    # A recorded attempt, not an exception: the row now carries the evidence and
    # retrying is a separate decision.
    assert outcome.sent is False
    assert outcome.error == "smtp: 421 try again later"
    assert notification.status is NotificationStatus.PENDING
    assert notification.attempts == 1
    assert notification.last_error == "smtp: 421 try again later"


def test_repeated_failures_park_the_row(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
    contact_field: int,
) -> None:
    notification = load_notification(session_factory, make_notification(session_factory, contract))
    contract_row = load_contract(session_factory, contract.id)
    tuned = _open_window(settings)

    for _ in range(tuned.notification_retry_max):
        dispatch_notification(
            notification,
            contract_row,
            FailingSender(),
            lambda: CONTACT_EMAIL,
            now=datetime.now(UTC),
            settings=tuned,
        )

    assert notification.attempts == tuned.notification_retry_max
    assert notification.status is NotificationStatus.FAILED

    with pytest.raises(DispatchRefusedError) as raised:
        dispatch_notification(
            notification,
            contract_row,
            FailingSender(),
            lambda: CONTACT_EMAIL,
            now=datetime.now(UTC),
            settings=tuned,
        )
    assert raised.value.code == "NOT_PENDING"


def test_a_row_whose_recipient_disappeared_is_refused(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
) -> None:
    notification = load_notification(session_factory, make_notification(session_factory, contract))

    def _missing() -> str:
        raise LookupError("no usable value")

    with pytest.raises(DispatchRefusedError) as raised:
        dispatch_notification(
            notification,
            load_contract(session_factory, contract.id),
            RecordingSender(),
            _missing,
            now=datetime.now(UTC),
            settings=_open_window(settings),
        )

    assert raised.value.code == "RECIPIENT_UNAVAILABLE"
    # Refused before the attempt counter moved: nothing was sent anywhere.
    assert notification.attempts == 0


# --- quiet hours and milestones: pure arithmetic --------------------------
def _contract_in(timezone_name: str) -> Any:
    """A stand-in contract: ``quiet_hours_until`` reads only ``timezone``.

    Constructing an ORM row would drag a session and a tenant into a test that is
    about arithmetic, and would invite the next reader to think these functions
    touch the database.
    """
    return SimpleNamespace(timezone=timezone_name)


def test_quiet_hours_wrap_midnight() -> None:
    contract = _contract_in("UTC")
    now = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)

    opens = quiet_hours_until(contract, now, start=20, end=8)

    assert opens == datetime(2026, 1, 16, 8, 0)


def test_quiet_hours_are_evaluated_on_the_customers_clock() -> None:
    now = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)

    # 03:00 UTC is 03:00 in London — the customer's night — so it defers.
    assert quiet_hours_until(_contract_in("Europe/London"), now, start=20, end=8) is not None
    # The same instant is 09:00 in Dhaka, which is not quiet at all.
    assert quiet_hours_until(_contract_in("Asia/Dhaka"), now, start=20, end=8) is None


def test_a_dst_transition_moves_the_instant_not_the_hour() -> None:
    contract = _contract_in("Europe/London")
    # Last Sunday of March 2026 is the 29th; the clocks go forward at 01:00 GMT.
    before = datetime(2026, 3, 28, 23, 30, tzinfo=UTC)  # GMT, 23:30 local
    after = datetime(2026, 7, 1, 22, 30, tzinfo=UTC)  # BST, 23:30 local

    # Both open at 08:00 *where the customer is*; only the UTC instant differs,
    # because 08:00 BST is 07:00 UTC. Adding eight hours to the clock instead
    # would have called them at 07:00 local for half the year.
    assert quiet_hours_until(contract, before, start=20, end=8) == datetime(2026, 3, 29, 7, 0)
    assert quiet_hours_until(contract, after, start=20, end=8) == datetime(2026, 7, 2, 7, 0)


def test_an_open_window_returns_nothing_to_wait_for() -> None:
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

    assert quiet_hours_until(_contract_in("UTC"), now, start=20, end=8) is None


def test_the_innermost_due_milestone_wins() -> None:
    end_date = date(2026, 7, 1)

    # Ten days out, T-60 and T-30 are both "due", but a customer inside their final
    # fortnight should not receive a late T-60.
    template, slot = milestone_for(end_date=end_date, today=date(2026, 6, 21))
    assert template == "T-14"
    assert slot == date(2026, 6, 17)

    assert milestone_for(end_date=end_date, today=date(2026, 6, 10))[0] == "T-30"
    # A month and a half out only T-60 is due. It has to be strictly *before*
    # 2026-06-01 (end_date - 30): the window is inclusive at its edge, so that day
    # already belongs to T-30.
    assert milestone_for(end_date=end_date, today=date(2026, 5, 15)) == ("T-60", date(2026, 5, 2))


def test_a_contract_further_out_than_every_milestone_is_refused() -> None:
    with pytest.raises(DispatchRefusedError) as raised:
        milestone_for(end_date=date(2027, 1, 1), today=date(2026, 1, 1))

    assert raised.value.code == "NOT_IN_WINDOW"


# --- the pending-overdue reader (D10-129) ---------------------------------
#
# A ``pending`` row is the third way a notification can fail to reach anybody, and
# the only one the database recorded nowhere: ``failed``/``suppressed`` are counted
# by ``undelivered_counts``, but a row nothing ever claimed simply sits. These cases
# pin the *predicate* at the repository, so the scheduler and the attention check
# cannot drift apart without one of them failing here first.
#
# The reader is exercised through its real signature rather than a wrapper that
# forgets ``now``: every case passes an explicit instant, because "overdue" is
# only meaningful relative to a clock and a test that read the wall clock twice
# could straddle the grace boundary.
GRACE = timedelta(minutes=15)

#: The result for a board with nothing overdue: every channel, zero-filled.
NOTHING_OVERDUE: dict[Channel, int] = dict.fromkeys(ALL_CHANNELS, 0)


def overdue(
    session_factory: sessionmaker[Session],
    *,
    grace: timedelta = GRACE,
    now: datetime | None = None,
) -> dict[Channel, int]:
    """Run ``doctor``'s and the attention watch's reader in one short call."""
    with session_scope(session_factory) as session:
        return pending_overdue_counts(session, grace=grace, now=now)


def test_an_empty_queue_reports_zero_for_every_channel(
    session_factory: sessionmaker[Session],
) -> None:
    # Zero-filled rather than empty: a missing channel would read, from the
    # publisher's side, as a channel that is never stuck rather than one nobody
    # counted.
    assert overdue(session_factory) == NOTHING_OVERDUE
    assert set(overdue(session_factory)) == set(Channel)


def test_a_row_inside_the_grace_is_not_yet_a_finding(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    now = utc_now()
    make_notification(
        session_factory,
        make_contract(session_factory, tenant),
        scheduled_at=now - GRACE + timedelta(seconds=30),
    )

    # Waiting for the next five-second scheduler tick is not a stuck queue.
    assert overdue(session_factory, now=now) == NOTHING_OVERDUE


def test_a_row_becomes_overdue_once_the_grace_has_elapsed(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    now = utc_now()
    make_notification(
        session_factory,
        make_contract(session_factory, tenant),
        scheduled_at=now - GRACE - timedelta(seconds=1),
    )

    assert overdue(session_factory, now=now) == {**NOTHING_OVERDUE, Channel.EMAIL: 1}


def test_a_row_with_no_due_time_is_aged_from_its_own_creation(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The branch a ``scheduled_at``-only predicate would silently drop.

    ``reminder_planner`` writes ``scheduled_at=None`` for immediate send, and
    ``claim_next_due`` treats NULL as due now — so an age test that compared
    ``scheduled_at`` alone would compare NULL, match nothing, and report a clean
    board over exactly the rows the producer wrote for right now.
    """
    now = utc_now()
    contract = make_contract(session_factory, tenant)
    # Immediate send, written an hour ago: due then, still pending now.
    make_notification(
        session_factory,
        contract,
        template="T-60",
        scheduled_at=None,
        created_at=now - timedelta(hours=1),
    )

    assert overdue(session_factory, now=now)[Channel.EMAIL] == 1

    # The other direction: an immediate-send row written a moment ago is simply
    # the next thing to go out, and NULL must not be read as "infinitely old".
    make_notification(
        session_factory,
        contract,
        template="T-30",
        scheduled_at=None,
        created_at=now,
    )

    assert overdue(session_factory, now=now)[Channel.EMAIL] == 1


def test_a_quiet_hours_deferral_is_never_counted(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Deferral is the system working, and it must not page anybody.

    A deferred row carries a *future* ``scheduled_at`` and an old ``created_at``,
    which is precisely the shape a reader keyed on creation time would mistake for
    a stuck queue — every deployment would alert at two in the morning.
    """
    now = utc_now()
    make_notification(
        session_factory,
        make_contract(session_factory, tenant),
        scheduled_at=now + timedelta(hours=6),
        created_at=now - timedelta(days=2),
    )

    assert overdue(session_factory, now=now) == NOTHING_OVERDUE


@pytest.mark.parametrize(
    "status",
    [NotificationStatus.SENT, NotificationStatus.FAILED, NotificationStatus.SUPPRESSED],
)
def test_a_row_that_left_pending_is_not_counted_however_old(
    session_factory: sessionmaker[Session], tenant: Tenant, status: NotificationStatus
) -> None:
    now = utc_now()
    make_notification(
        session_factory,
        make_contract(session_factory, tenant),
        status=status,
        scheduled_at=now - timedelta(days=3),
    )

    # ``failed`` and ``suppressed`` are the sibling reader's population. Counting
    # them here as well would report one broken send twice.
    assert overdue(session_factory, now=now) == NOTHING_OVERDUE


def test_the_counts_are_per_channel_and_not_one_total(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    now = utc_now()
    contract = make_contract(session_factory, tenant)
    # Same contract, distinct milestone slots: the unique constraint is
    # ``(contract, template, scheduled_date)``, so the template is what keeps two
    # email rows apart here — not the channel.
    for template, channel in (
        ("T-60", Channel.EMAIL),
        ("T-30", Channel.EMAIL),
        ("T-14", Channel.SMS),
    ):
        make_notification(
            session_factory,
            contract,
            template=template,
            channel=channel,
            scheduled_at=now - timedelta(hours=1),
        )

    assert overdue(session_factory, now=now) == {
        Channel.EMAIL: 2,
        Channel.SMS: 1,
        Channel.VOICE: 0,
    }


# --- the status sets themselves --------------------------------------------
def test_the_three_status_sets_partition_the_status_enum() -> None:
    """Every ``NotificationStatus`` is pending, contacted, or undelivered — once.

    Three readers slice the same column: the stuck-row count takes ``pending``, the
    attention check takes ``undelivered``, and the dashboard's channel mix takes
    ``contacted``. That is only safe while the three are a *partition*.
    ``CONTACTED_STATUSES`` is written by inclusion ("everything that reached
    somebody") and ``UNDELIVERED_STATUSES`` by name, so they can drift the moment a
    status is added: a sixth status in neither set would simply never be counted,
    and the mix would under-report contact with no error raised anywhere.

    Asserting the partition makes that drift fail here instead — in the cheapest
    place it can, and on the commit that introduces the status rather than on the
    day somebody notices a number looks low.

    ``pending`` is the third cell and is deliberately not a named constant: it is
    exactly one status, and a one-element tuple would invite a second "pending-ish"
    state rather than a decision about which of the two real sets it belongs to.
    """
    contacted = set(CONTACTED_STATUSES)
    undelivered = set(UNDELIVERED_STATUSES)

    assert contacted.isdisjoint(undelivered)
    # Equality, not a subset check: this also fails if either set names a status
    # the enum no longer has, which is what a rename would leave behind.
    assert contacted | undelivered | {NotificationStatus.PENDING} == set(NotificationStatus)
