"""The notification scheduler (gap 22).

Before this worker existed, nothing read ``notifications.scheduled_at``. The three
claims these tests are built around:

* **A deferral resumes.** Quiet hours move ``scheduled_at`` into the future and
  leave the row ``pending``. That is only useful if something picks it up when the
  time passes — which is exactly what :func:`claim_next_due`'s predicate does, and
  what the first test asserts end to end.
* **A permanent refusal must not block the queue.** Rows are claimed oldest-due
  first, so a row that can never send sits at the head forever. Permanent refusals
  are parked ``suppressed``; transient ones stay ``pending``. Getting this backwards
  either starves the queue or silently discards a send, so both directions are
  pinned.
* **The tenant comes from the row.** The claim is the one unscoped query in the
  codebase, so the test that matters is the hostile one: a row whose contract lives
  in another tenant must not be dispatched.

Clock note: the scheduler calls ``utc_now()`` itself rather than taking an injected
instant, so "time passes" is simulated by moving the row's ``scheduled_at`` into the
past. That is the state the next tick would genuinely observe, and it keeps the
production signature free of a parameter that exists only for tests.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.db.enums import Channel, ContractStatus, FieldName, FieldStatus, NotificationStatus
from app.db.models import Contract, ExtractedField, Notification, Tenant
from app.db.session import session_scope
from app.workers.dispatch import (
    OutgoingMessage,
    SenderError,
    UnavailableSender,
    notification_idempotency_key,
)
from app.workers.notification_scheduler import (
    REFUSAL_TENANT_MISMATCH,
    NotificationScheduler,
)

#: The value no row may ever hold, and the address a send must go to.
CONTACT_EMAIL = "a.rahman@example.test"

#: One key for the whole module, so the value a helper seals is the value the
#: scheduler unseals. Since gap 20 the recipient is Fernet ciphertext, and a
#: scheduler built without a key refuses to tick at all — so every settings copy
#: below carries this key explicitly.
ENCRYPTION_KEY = Fernet.generate_key().decode("ascii")
CIPHER = FieldCipher(ENCRYPTION_KEY)


# --- doubles ---------------------------------------------------------------
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

    def __init__(self) -> None:
        self.calls = 0

    @property
    def channels(self) -> frozenset[Channel]:
        return frozenset({Channel.EMAIL})

    def send(self, message: OutgoingMessage) -> str | None:
        self.calls += 1
        raise SenderError("smtp: 421 try again later")


# --- helpers ---------------------------------------------------------------
def utc_today() -> date:
    return datetime.now(UTC).date()


def make_contract(
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    *,
    end_in_days: int | None = 40,
    timezone_name: str = "UTC",
    status: ContractStatus = ContractStatus.ACTIVE,
    consent_email: bool = True,
    do_not_contact: bool = False,
) -> Contract:
    """One committed contract consenting to email, due in the T-60 window."""
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=tenant.id,
            customer_ref="A. Rahman",
            end_date=None if end_in_days is None else utc_today() + timedelta(days=end_in_days),
            timezone=timezone_name,
            status=status,
            consent_email=consent_email,
            do_not_contact=do_not_contact,
        )
        session.add(row)
        session.flush()
        return row


def add_contact_field(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    value: str | None = CONTACT_EMAIL,
    status: FieldStatus = FieldStatus.APPROVED,
    cipher: FieldCipher = CIPHER,
) -> int:
    """Store the approved email the policy resolves a recipient from.

    Sealed with :data:`CIPHER`, because the scheduler decrypts this column to build
    the recipient (gap 20). A row written in the clear is refused as unreadable
    rather than sent to, so a plaintext fixture would make every send test fail for
    a reason unrelated to what it asserts.

    ``cipher`` is overridable for the wrong-key case: a row sealed under a key this
    process does not hold is the shape of a botched key rotation, and it must refuse
    rather than deliver.
    """
    with session_scope(session_factory) as session:
        row = ExtractedField(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            field_name=FieldName.EMAIL,
            raw_value=None,
            normalized_value=cipher.encrypt(value) if value is not None else None,
            confidence=0.93,
            status=status,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def make_notification(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    tenant_id: uuid.UUID | None = None,
    scheduled_at: datetime | None = None,
    attempts: int = 0,
    template: str = "T-60",
    channel: Channel = Channel.EMAIL,
) -> int:
    """Insert one pending row directly and return its id.

    ``tenant_id`` is overridable so a cross-tenant row can be built: the case the
    scheduler's tenant guard exists for cannot be produced through the API.
    """
    slot = utc_today() - timedelta(days=20)
    with session_scope(session_factory) as session:
        row = Notification(
            tenant_id=contract.tenant_id if tenant_id is None else tenant_id,
            contract_id=contract.id,
            channel=channel,
            template=template,
            status=NotificationStatus.PENDING,
            scheduled_date=slot,
            scheduled_at=scheduled_at,
            idempotency_key=notification_idempotency_key(contract.id, template, slot),
            attempts=attempts,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def load(session_factory: sessionmaker[Session], notification_id: int) -> Notification:
    """Re-read a row through a fresh session, so assertions see committed state."""
    with session_scope(session_factory) as session:
        row = session.get(Notification, notification_id)
        assert row is not None
        return row


def defer_due_time_into_the_past(
    session_factory: sessionmaker[Session], notification_id: int
) -> None:
    """Simulate time passing over a quiet-hours deferral.

    The deferral writes a future ``scheduled_at``; this moves it behind now, which
    is precisely the row the next tick would find once that moment arrived.
    """
    with session_scope(session_factory) as session:
        row = session.get(Notification, notification_id)
        assert row is not None
        row.scheduled_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)


def open_window(settings: Settings) -> Settings:
    """Settings whose quiet window does not contain the current hour.

    Carries :data:`ENCRYPTION_KEY` because every scheduler built from this helper
    decrypts a recipient; without it ``run_once`` returns immediately and every
    send test would assert on a no-op.
    """
    hour = datetime.now(UTC).hour
    return settings.model_copy(
        update={
            "quiet_hours_start": (hour + 2) % 24,
            "quiet_hours_end": (hour + 3) % 24,
            "encryption_key": ENCRYPTION_KEY,
        }
    )


def quiet_now(settings: Settings) -> Settings:
    """Settings whose quiet window contains the current hour."""
    hour = datetime.now(UTC).hour
    return settings.model_copy(
        update={
            "quiet_hours_start": hour,
            "quiet_hours_end": (hour + 1) % 24,
            "encryption_key": ENCRYPTION_KEY,
        }
    )


def scheduler(
    session_factory: sessionmaker[Session],
    settings: Settings,
    sender: Any,
    *,
    batch_size: int = 50,
) -> NotificationScheduler:
    return NotificationScheduler(session_factory, sender, settings=settings, batch_size=batch_size)


@pytest.fixture
def contract(session_factory: sessionmaker[Session], tenant: Tenant) -> Contract:
    return make_contract(session_factory, tenant)


@pytest.fixture
def contact_field(session_factory: sessionmaker[Session], contract: Contract) -> int:
    return add_contact_field(session_factory, contract)


# --- a deferral actually resumes ------------------------------------------
def test_quiet_hours_defer_the_row_without_spending_an_attempt(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
    contact_field: int,
) -> None:
    """The deferral half: nothing is sent, and the row is left untouched but for its time."""
    notification_id = make_notification(session_factory, contract)
    sender = RecordingSender()

    tick = scheduler(session_factory, quiet_now(settings), sender)._tick()

    assert tick.claimed is True
    assert tick.deferred is True
    assert tick.resolved is True
    assert sender.sent == []
    row = load(session_factory, notification_id)
    assert row.status is NotificationStatus.PENDING
    assert row.attempts == 0
    assert row.scheduled_at is not None
    # The deferral is expressed only as a future due time — that is the whole
    # mechanism, so it is asserted directly rather than inferred from "not sent".
    assert row.scheduled_at > datetime.now(UTC).replace(tzinfo=None)


def test_a_deferred_row_is_not_due_until_its_window_opens_then_sends(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
    contact_field: int,
) -> None:
    """The resumption half: the row is skipped until due, then reaches the transport."""
    notification_id = make_notification(session_factory, contract)
    sender = RecordingSender()

    scheduler(session_factory, quiet_now(settings), sender).run_once()
    # Still inside the window and still deferred, so there is nothing to claim.
    assert scheduler(session_factory, quiet_now(settings), sender).run_once() == 0
    assert sender.sent == []

    defer_due_time_into_the_past(session_factory, notification_id)
    assert scheduler(session_factory, open_window(settings), sender).run_once() == 1

    row = load(session_factory, notification_id)
    assert row.status is NotificationStatus.SENT
    assert row.sent_at is not None
    assert row.provider_message_id == "provider-1"
    assert row.attempts == 1
    assert len(sender.sent) == 1
    assert sender.sent[0].recipient == CONTACT_EMAIL
    # The contact value travels to the transport only; it is never stored on the row.
    assert row.last_error is None


# --- permanent vs transient refusals ---------------------------------------
def test_a_permanent_refusal_is_parked_so_it_cannot_starve_the_queue(
    settings: Settings,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """The row at the head cannot send; the row behind it must still be reached."""
    blocked_contract = make_contract(session_factory, tenant, do_not_contact=True)
    add_contact_field(session_factory, blocked_contract)
    healthy_contract = make_contract(session_factory, tenant)
    add_contact_field(session_factory, healthy_contract)

    blocked_id = make_notification(session_factory, blocked_contract)
    healthy_id = make_notification(session_factory, healthy_contract)
    # Claim order is (scheduled_at, id); both have no due time, so the blocked row
    # is first by id and genuinely occupies the head of the queue.
    assert blocked_id < healthy_id

    sender = RecordingSender()
    assert scheduler(session_factory, open_window(settings), sender).run_once() == 1

    blocked = load(session_factory, blocked_id)
    assert blocked.status is NotificationStatus.SUPPRESSED
    assert blocked.attempts == 0
    assert blocked.last_error is not None
    assert blocked.last_error.startswith("CONTACT_REFUSED")
    assert load(session_factory, healthy_id).status is NotificationStatus.SENT
    assert len(sender.sent) == 1

    # A parked row is no longer claimed, so a later tick does not re-litigate it.
    assert scheduler(session_factory, open_window(settings), sender).run_once() == 0


def test_a_spent_retry_budget_parks_the_row_failed_not_suppressed(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
    contact_field: int,
) -> None:
    """``ATTEMPTS_EXHAUSTED`` is already a policy verdict; it must survive the commit.

    The scheduler does not re-raise refusals, so the ``failed`` status the policy
    assigned is committed. This is the case that would silently roll back if the
    worker let the exception escape ``session_scope``.
    """
    notification_id = make_notification(
        session_factory, contract, attempts=settings.notification_retry_max
    )
    sender = RecordingSender()

    tick = scheduler(session_factory, open_window(settings), sender)._tick()

    assert tick.claimed is True
    assert tick.suppressed is False
    assert tick.refusal == "ATTEMPTS_EXHAUSTED"
    assert sender.sent == []
    row = load(session_factory, notification_id)
    assert row.status is NotificationStatus.FAILED
    assert row.attempts == settings.notification_retry_max


def test_a_transport_failure_spends_one_attempt_per_tick(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
    contact_field: int,
) -> None:
    """A failing send must not burn the whole retry budget inside one drain."""
    notification_id = make_notification(session_factory, contract)
    failing = FailingSender()

    # batch_size is far above 1: if the loop continued on an unresolved tick it
    # would re-claim this still-due row repeatedly and hit the transport each time.
    assert scheduler(session_factory, open_window(settings), failing, batch_size=25).run_once() == 0

    assert failing.calls == 1
    row = load(session_factory, notification_id)
    assert row.status is NotificationStatus.PENDING
    assert row.attempts == 1
    assert row.last_error is not None
    assert "421" in row.last_error


# --- the tenant guard -------------------------------------------------------
def test_a_row_whose_contract_is_in_another_tenant_is_parked(
    settings: Settings,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """The claim is unscoped, so this is the guard that makes it safe.

    The row names tenant A while its ``contract_id`` points at a contract owned by
    tenant B. Following that pointer would read another tenant's data; the scoped
    repository returns nothing instead, and the row is parked rather than sent.
    """
    other_tenant = Tenant(slug=f"other-{uuid.uuid4().hex[:8]}", name="Other Ltd")
    with session_scope(session_factory) as session:
        session.add(other_tenant)
        session.flush()

    foreign_contract = make_contract(session_factory, other_tenant)
    add_contact_field(session_factory, foreign_contract)
    # The row itself belongs to ``tenant`` but points across the boundary.
    notification_id = make_notification(session_factory, foreign_contract, tenant_id=tenant.id)

    sender = RecordingSender()
    tick = scheduler(session_factory, open_window(settings), sender)._tick()

    assert tick.claimed is True
    assert tick.suppressed is True
    assert tick.refusal == REFUSAL_TENANT_MISMATCH
    assert sender.sent == []
    row = load(session_factory, notification_id)
    assert row.status is NotificationStatus.SUPPRESSED
    assert row.last_error is not None
    assert REFUSAL_TENANT_MISMATCH in row.last_error


# --- no transport -----------------------------------------------------------
def test_no_transport_claims_nothing_at_all(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
    contact_field: int,
) -> None:
    """The shipped build: rows must stay dispatchable, not be parked by accident.

    Refusing ``SENDER_UNAVAILABLE`` per row would have moved every pending row into
    a state only a human can lift, for a reason an operator fixes with one env var.
    """
    notification_id = make_notification(session_factory, contract)

    assert scheduler(session_factory, open_window(settings), UnavailableSender()).run_once() == 0

    row = load(session_factory, notification_id)
    assert row.status is NotificationStatus.PENDING
    assert row.attempts == 0
    assert row.last_error is None


def test_an_empty_queue_is_a_no_op(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    assert scheduler(session_factory, open_window(settings), RecordingSender()).run_once() == 0


def test_no_encryption_key_claims_nothing_at_all(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
    contact_field: int,
) -> None:
    """Without a key no recipient can be read, so no row is worth claiming.

    Claiming and then refusing would spend an attempt — or suppress the row — for a
    reason an operator fixes by setting one environment variable. The build this
    replaces behaved the same way with no transport configured, for the same reason.
    """
    notification_id = make_notification(session_factory, contract)
    no_key = open_window(settings).model_copy(update={"encryption_key": ""})

    assert scheduler(session_factory, no_key, RecordingSender()).run_once() == 0

    row = load(session_factory, notification_id)
    assert row.status is NotificationStatus.PENDING
    assert row.attempts == 0
    assert row.last_error is None


def test_a_value_sealed_under_another_key_is_suppressed_not_sent(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
) -> None:
    """A botched key rotation must park the row, never mail the ciphertext.

    The refusal is permanent from this process's point of view — retrying with the
    same key cannot succeed — so the row is suppressed and left for
    ``POST /notifications/{id}/retry`` once the key is restored.
    """
    notification_id = make_notification(session_factory, contract)
    rotated = FieldCipher(Fernet.generate_key().decode("ascii"))
    add_contact_field(session_factory, contract, cipher=rotated)

    sender = RecordingSender()
    tick = scheduler(session_factory, open_window(settings), sender)._tick()

    assert tick.suppressed is True
    assert tick.refusal == "RECIPIENT_UNAVAILABLE"
    assert sender.sent == []
    stored = load(session_factory, notification_id)
    assert stored.status is NotificationStatus.SUPPRESSED
    # The row explains itself without repeating the value it could not read.
    assert stored.last_error is not None
    assert CONTACT_EMAIL not in stored.last_error


def test_rows_are_drained_up_to_the_batch_size(
    settings: Settings,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """A drain resolves several rows per tick, not one."""
    ids = []
    for index in range(3):
        contract = make_contract(session_factory, tenant)
        add_contact_field(session_factory, contract)
        ids.append(make_notification(session_factory, contract, template=f"T-6{index}"))

    sender = RecordingSender()
    assert scheduler(session_factory, open_window(settings), sender).run_once() == 3

    statuses = {load(session_factory, notification_id).status for notification_id in ids}
    assert statuses == {NotificationStatus.SENT}
    assert len(sender.sent) == 3


def test_run_forever_stops_when_asked(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop must exit on its stop event rather than run to the test timeout."""
    stop = threading.Event()
    worker = scheduler(session_factory, open_window(settings), RecordingSender())
    ticks: list[int] = []
    original = worker.run_once

    def _tick_then_stop() -> int:
        ticks.append(1)
        # Setting the event here proves the loop honours ``stop`` rather than
        # draining indefinitely; it must not sleep a full poll interval first.
        stop.set()
        return original()

    monkeypatch.setattr(worker, "run_once", _tick_then_stop)
    worker.run_forever(stop)

    assert ticks == [1]


def test_a_blocking_field_refusal_is_parked_not_retried(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
) -> None:
    """``PIPELINE_BLOCKED`` needs a reviewer, so an unattended tick must stop asking."""
    notification_id = make_notification(session_factory, contract)
    add_contact_field(session_factory, contract, status=FieldStatus.PENDING_REVIEW)

    sender = RecordingSender()
    tick = scheduler(session_factory, open_window(settings), sender)._tick()

    assert tick.suppressed is True
    assert tick.refusal == "PIPELINE_BLOCKED"
    assert sender.sent == []
    row = load(session_factory, notification_id)
    assert row.status is NotificationStatus.SUPPRESSED
    assert row.last_error is not None
    assert row.last_error.startswith("PIPELINE_BLOCKED")


def test_the_scheduler_sends_nothing_when_no_contact_value_is_approved(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
) -> None:
    """An approved-but-empty contact value is a permanent refusal, not a retry."""
    notification_id = make_notification(session_factory, contract)
    add_contact_field(session_factory, contract, value=None)

    sender = RecordingSender()
    tick = scheduler(session_factory, open_window(settings), sender)._tick()

    assert tick.suppressed is True
    assert tick.refusal == "RECIPIENT_UNAVAILABLE"
    assert sender.sent == []
    assert load(session_factory, notification_id).status is NotificationStatus.SUPPRESSED


def test_suppressing_a_row_records_why_on_the_row(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
) -> None:
    """The verdict must be readable later without the log."""
    notification_id = make_notification(session_factory, contract)
    worker = scheduler(session_factory, open_window(settings), RecordingSender())

    with session_scope(session_factory) as session:
        row = session.get(Notification, notification_id)
        assert row is not None
        worker._suppress(row, "X" * 900)

    stored = load(session_factory, notification_id)
    assert stored.status is NotificationStatus.SUPPRESSED
    assert stored.last_error is not None
    assert len(stored.last_error) == 500


def test_the_claimed_row_is_always_pending(
    settings: Settings,
    session_factory: sessionmaker[Session],
    contract: Contract,
    contact_field: int,
) -> None:
    """A sent row is never re-claimed: the due predicate and the policy agree."""
    notification_id = make_notification(session_factory, contract)
    sender = RecordingSender()

    assert scheduler(session_factory, open_window(settings), sender).run_once() == 1
    assert scheduler(session_factory, open_window(settings), sender).run_once() == 0

    assert load(session_factory, notification_id).status is NotificationStatus.SENT
    assert len(sender.sent) == 1


def test_every_pending_row_is_reachable_exactly_once(
    settings: Settings,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
) -> None:
    """Draining returns the number delivered, and nothing is delivered twice."""
    for index in range(4):
        contract = make_contract(session_factory, tenant)
        add_contact_field(session_factory, contract)
        make_notification(session_factory, contract, template=f"T-7{index}")

    sender = RecordingSender()
    assert scheduler(session_factory, open_window(settings), sender).run_once() == 4
    assert scheduler(session_factory, open_window(settings), sender).run_once() == 0

    with session_scope(session_factory) as session:
        rows = list(session.execute(select(Notification)).scalars())
    assert len(rows) == 4
    assert {row.status for row in rows} == {NotificationStatus.SENT}
    assert len(sender.sent) == 4
    # Each message carries its own row's idempotency key, which is what a real
    # provider dedupes on when a crash forces a re-send.
    assert len({message.idempotency_key for message in sender.sent}) == 4
