"""The escalation scanner: the trigger gap 15 left out.

Gap 15 made ``ContractStatus.ESCALATED`` and ``CustomerStatus.RESPONDED``
reachable, but only through a route a human called. Nothing noticed that a
contract had gone quiet, so the escalation queue stayed empty in a running system
no matter how long a customer ignored the reminders. These tests are built around
four claims:

* **Silence is measured from the last accepted contact.** Not from the contract's
  status — ``reminder_sent`` exists in the enum and nothing writes it — and not
  from the reminder that was *scheduled*. A row that never reached anybody is not
  contact, and a newer send restarts the window instead of leaving the contract
  permanently overdue.
* **The scanner does not undo a human's decision.** ``DECISIONS.md`` D10-20 makes
  hand-resolving an escalation leave the contract ``escalated``; a scanner that
  read that state as "still silent, raise another" would fight the operator every
  five minutes. Both the resolved-row case and the closed-contract cases are
  pinned.
* **Two clocks stay two clocks.** ``escalation_no_response_days`` decides *whether*
  to escalate; ``escalation_sla_hours`` starts once it is raised. The deadline test
  asserts the second is counted from the scan, not from the last contact.
* **The tenant comes from the row.** The claim is an unscoped query — it has to be,
  since one process serves every tenant — so the hostile case is a contract in
  another tenant, and the assertion is that each escalation lands in the tenant
  that owns the contract.

Clock note: like the notification scheduler, :class:`EscalationScanner` calls
``utc_now()`` itself rather than taking an injected instant, so "two weeks have
passed" is simulated by back-dating ``notifications.sent_at``. That is the state
the next pass would genuinely observe, and it keeps a test-only parameter out of
the production signature.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.enums import (
    Channel,
    ContractStatus,
    EscalationStatus,
    NotificationStatus,
    ResponseType,
)
from app.db.models import AuditLog, Contract, CustomerResponse, Escalation, Notification, Tenant
from app.db.repositories.escalations import EscalationRepository
from app.db.session import session_scope
from app.workers.dispatch import notification_idempotency_key
from app.workers.escalation_scanner import EscalationScanner, ScanResult

#: ``DECISIONS.md`` Q2, and the default of ``Settings.escalation_sla_hours``.
SLA_HOURS = 48
#: Matches the default of ``Settings.escalation_no_response_days``.
NO_RESPONSE_DAYS = 14

#: Spelled out rather than imported, so a renamed constant fails a test instead of
#: silently agreeing with the code it is supposed to pin.
ACTION_ESCALATION_OPENED = "escalation.opened"
ACTOR_WORKER = "worker"
ENTITY_ESCALATION = "escalations"

#: A value no audit snapshot may ever contain.
CONTACT_EMAIL = "a.rahman@example.test"
CUSTOMER_NAME = "A. Rahman"


# --- helpers --------------------------------------------------------------
def _naive_now() -> datetime:
    """The naive UTC the ``DateTime`` columns store, matching ``utc_now``."""
    return datetime.now(UTC).replace(tzinfo=None)


def make_contract(
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    *,
    status: ContractStatus = ContractStatus.ACTIVE,
) -> Contract:
    """One committed contract, ``active`` unless a test says otherwise."""
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=tenant.id,
            customer_ref=CUSTOMER_NAME,
            end_date=date(2026, 1, 1),
            status=status,
        )
        session.add(row)
        session.flush()
        return row


def add_contact(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    days_ago: int = NO_RESPONSE_DAYS + 1,
    status: NotificationStatus = NotificationStatus.SENT,
    template: str = "T-60",
) -> int:
    """Record one notification the transport accepted ``days_ago`` days ago.

    ``sent_at`` is the column the dispatch worker actually writes and the only
    evidence the scanner reads, so it is the one this helper moves. ``status`` is
    overridable to build the rows that *look* like contact but are not.
    """
    sent_at = _naive_now() - timedelta(days=days_ago)
    slot = sent_at.date()
    with session_scope(session_factory) as session:
        row = Notification(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            channel=Channel.EMAIL,
            template=template,
            status=status,
            scheduled_date=slot,
            scheduled_at=sent_at,
            sent_at=sent_at,
            idempotency_key=notification_idempotency_key(contract.id, template, slot),
            attempts=1,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def add_response(session_factory: sessionmaker[Session], contract: Contract) -> int:
    """Record that the customer answered — the reason not to escalate."""
    with session_scope(session_factory) as session:
        row = CustomerResponse(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            correlation_id=str(uuid.uuid4()),
            channel=Channel.EMAIL,
            response_type=ResponseType.REPLY,
            received_at=_naive_now() - timedelta(days=2),
        )
        session.add(row)
        session.flush()
        return int(row.id)


def add_escalation(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    status: EscalationStatus = EscalationStatus.OPEN,
    deadline_in_days: int = 2,
) -> int:
    """Seed one escalation row directly, for the "already escalated" cases."""
    with session_scope(session_factory) as session:
        row = Escalation(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            correlation_id=str(uuid.uuid4()),
            sla_deadline=_naive_now() + timedelta(days=deadline_in_days),
            status=status,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def scanner(
    session_factory: sessionmaker[Session],
    *,
    no_response_days: int = NO_RESPONSE_DAYS,
    batch_size: int = 50,
) -> EscalationScanner:
    """A scanner over the test database.

    The settings object is built rather than mutated so pydantic validates the
    bounds, which is also how the production code path receives them.
    """
    settings = Settings(
        app_env="test",
        log_json=False,
        escalation_no_response_days=no_response_days,
        escalation_scan_batch_size=batch_size,
        escalation_sla_hours=SLA_HOURS,
        escalation_scan_poll_interval_seconds=0.01,
    )
    return EscalationScanner(session_factory, settings=settings)


def escalations_for(session_factory: sessionmaker[Session], contract_id: int) -> list[Escalation]:
    with session_scope(session_factory) as session:
        stmt = (
            select(Escalation).where(Escalation.contract_id == contract_id).order_by(Escalation.id)
        )
        return list(session.execute(stmt).scalars())


def audit_rows(session_factory: sessionmaker[Session], action: str | None = None) -> list[AuditLog]:
    with session_scope(session_factory) as session:
        stmt = select(AuditLog).order_by(AuditLog.id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        return list(session.execute(stmt).scalars())


def load_contract(session_factory: sessionmaker[Session], contract_id: int) -> Contract:
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract_id)
        assert row is not None
        return row


# --- the trigger ----------------------------------------------------------
def test_a_contract_contacted_fifteen_days_ago_is_escalated(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The whole feature in one test: silent long enough, so a human is told."""
    contract = make_contract(session_factory, tenant)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 1)

    result = scanner(session_factory).run_once()

    assert result.candidates == 1
    assert result.opened == 1
    assert result.refused == ()
    assert result.worked is True

    raised = escalations_for(session_factory, contract.id)
    assert len(raised) == 1
    assert raised[0].status is EscalationStatus.OPEN
    assert raised[0].tenant_id == tenant.id
    assert load_contract(session_factory, contract.id).status is ContractStatus.ESCALATED


def test_the_deadline_is_the_sla_counted_from_the_scan_not_the_contact(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Two clocks: fourteen days decides *whether*, forty-eight hours decides *when*.

    A deadline derived from the contact would already be thirteen days overdue the
    moment it was raised, which is how an SLA queue stops meaning anything.
    """
    contract = make_contract(session_factory, tenant)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 10)

    before = _naive_now()
    scanner(session_factory).run_once()
    after = _naive_now()

    deadline = escalations_for(session_factory, contract.id)[0].sla_deadline
    # One second of slack on the lower bound: the deadline is floored to the second
    # (``truncate_to_second``), so it can sit a fraction below ``before``.
    assert (
        before + timedelta(hours=SLA_HOURS) - timedelta(seconds=1)
        <= deadline
        <= after + timedelta(hours=SLA_HOURS)
    )
    # Stored to the second, like every other row this app dates (truncate_to_second).
    assert deadline.microsecond == 0


def test_the_audit_row_records_that_a_worker_raised_it(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """``actor_type`` is ``worker``: nobody was watching when this was decided.

    Distinct from the route's ``user`` for the same action, so an auditor can tell
    a contract a person escalated from one a clock did.
    """
    contract = make_contract(session_factory, tenant)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 1)

    scanner(session_factory).run_once()

    rows = audit_rows(session_factory, ACTION_ESCALATION_OPENED)
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_type == ACTOR_WORKER
    assert row.actor_id is None
    assert row.entity == ENTITY_ESCALATION
    assert row.entity_id == str(escalations_for(session_factory, contract.id)[0].id)
    assert row.tenant_id == tenant.id
    assert row.before_state is not None
    assert "active" in row.before_state
    assert row.after_state is not None
    assert "escalated" in row.after_state


def test_the_audit_snapshot_holds_no_customer_data(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The scanner writes statuses and a deadline — never a name or an address."""
    contract = make_contract(session_factory, tenant)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 1)

    scanner(session_factory).run_once()

    row = audit_rows(session_factory, ACTION_ESCALATION_OPENED)[0]
    rendered = f"{row.before_state}{row.after_state}{row.reason}"
    assert CONTACT_EMAIL not in rendered
    assert CUSTOMER_NAME not in rendered


# --- what must be left alone ---------------------------------------------
def test_recent_contact_is_left_alone(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Day thirteen of a fourteen-day window is not silence."""
    contract = make_contract(session_factory, tenant)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS - 1)

    result = scanner(session_factory).run_once()

    assert result.opened == 0
    assert result.worked is False
    assert escalations_for(session_factory, contract.id) == []


def test_a_newer_contact_restarts_the_window(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """An old send does not make a contract overdue while reminders are still going.

    Without this, the second reminder of a sequence would escalate the contract the
    moment the *first* one aged out.
    """
    contract = make_contract(session_factory, tenant)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 20, template="T-60")
    add_contact(session_factory, contract, days_ago=3, template="T-14")

    assert scanner(session_factory).run_once().opened == 0
    assert escalations_for(session_factory, contract.id) == []


def test_a_contract_that_replied_is_left_alone(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """A recorded response is the answer; there is nothing to chase."""
    contract = make_contract(session_factory, tenant)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 1)
    add_response(session_factory, contract)

    assert scanner(session_factory).run_once().opened == 0
    assert escalations_for(session_factory, contract.id) == []


def test_a_contract_already_marked_responded_is_skipped(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The status alone is enough even when the response row is missing.

    Belt and braces: a contract can be marked ``responded`` from the escalation
    route, and the scanner must not depend on the two facts arriving together.
    """
    contract = make_contract(session_factory, tenant, status=ContractStatus.RESPONDED)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 1)

    assert scanner(session_factory).run_once().opened == 0
    assert escalations_for(session_factory, contract.id) == []


@pytest.mark.parametrize(
    "status",
    [EscalationStatus.OPEN, EscalationStatus.IN_PROGRESS, EscalationStatus.SLA_BREACHED],
)
def test_a_contract_with_an_escalation_in_flight_gets_no_second_one(
    session_factory: sessionmaker[Session], tenant: Tenant, status: EscalationStatus
) -> None:
    """One open escalation per contract — the route refuses a second, so must this."""
    contract = make_contract(session_factory, tenant, status=ContractStatus.ESCALATED)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 30)
    existing = add_escalation(session_factory, contract, status=status)

    assert scanner(session_factory).run_once().opened == 0
    assert [row.id for row in escalations_for(session_factory, contract.id)] == [existing]


def test_an_escalation_resolved_by_hand_is_not_reopened(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """``D10-20``: resolving by hand leaves the contract ``escalated``, and that stands.

    A scanner that treated "escalated with nothing open" as "silent again" would
    undo the operator's decision on the very next pass, every five minutes, forever.
    """
    contract = make_contract(session_factory, tenant, status=ContractStatus.ESCALATED)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 30)
    resolved = add_escalation(
        session_factory, contract, status=EscalationStatus.RESOLVED, deadline_in_days=-5
    )

    assert scanner(session_factory).run_once().opened == 0
    assert [row.id for row in escalations_for(session_factory, contract.id)] == [resolved]
    assert load_contract(session_factory, contract.id).status is ContractStatus.ESCALATED


@pytest.mark.parametrize("status", [ContractStatus.EXPIRED, ContractStatus.RENEWED])
def test_a_closed_contract_is_never_scanned(
    session_factory: sessionmaker[Session], tenant: Tenant, status: ContractStatus
) -> None:
    """Chasing a customer about a file that is already closed is the wrong call."""
    contract = make_contract(session_factory, tenant, status=status)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 90)

    assert scanner(session_factory).run_once().opened == 0
    assert escalations_for(session_factory, contract.id) == []


@pytest.mark.parametrize(
    "status",
    [NotificationStatus.PENDING, NotificationStatus.FAILED, NotificationStatus.SUPPRESSED],
)
def test_a_notification_that_never_reached_anybody_is_not_contact(
    session_factory: sessionmaker[Session], tenant: Tenant, status: NotificationStatus
) -> None:
    """Contact is "the transport accepted it", not "we tried".

    These rows carry a ``sent_at`` here on purpose: the exclusion must come from
    the status, so a row that was scheduled, or refused, or suppressed can never be
    counted as silence evidence no matter what timestamp it holds.
    """
    contract = make_contract(session_factory, tenant)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 1, status=status)

    assert scanner(session_factory).run_once().opened == 0


def test_a_call_answered_by_a_machine_counts_as_contact(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """A voicemail reached a mailbox — and the spec says that is *not* a response.

    That combination is exactly what an escalation exists for, so the row must
    count as contact (silence is measured from it) without counting as a reply.
    """
    contract = make_contract(session_factory, tenant)
    add_contact(
        session_factory, contract, days_ago=NO_RESPONSE_DAYS + 1, status=NotificationStatus.MACHINE
    )

    assert scanner(session_factory).run_once().opened == 1
    assert load_contract(session_factory, contract.id).status is ContractStatus.ESCALATED


# --- batching, idempotency, tenancy --------------------------------------
def test_the_batch_size_bounds_one_pass(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """A bounded transaction: the batch is what one pass may lock and write."""
    contracts = [make_contract(session_factory, tenant) for _ in range(3)]
    for contract in contracts:
        add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 1)

    worker = scanner(session_factory, batch_size=2)
    first = worker.run_once()
    second = worker.run_once()

    assert (first.candidates, first.opened) == (2, 2)
    assert (second.candidates, second.opened) == (1, 1)
    opened = sum(len(escalations_for(session_factory, row.id)) for row in contracts)
    assert opened == 3


def test_a_second_pass_finds_nothing_left_to_do(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Idempotent: the contract it just escalated is not a candidate again."""
    contract = make_contract(session_factory, tenant)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 1)

    worker = scanner(session_factory)
    assert worker.run_once().opened == 1
    again = worker.run_once()

    assert (again.candidates, again.opened) == (0, 0)
    assert len(escalations_for(session_factory, contract.id)) == 1


def test_each_tenant_gets_an_escalation_of_its_own(
    session_factory: sessionmaker[Session], tenant: Tenant, other_tenant: Tenant
) -> None:
    """The claim is unscoped; the writes are not.

    One process serves every tenant, so the tenant must come from the row it
    claimed. The failure this pins is an escalation landing in the wrong tenant —
    which would be a cross-tenant write, not merely a wrong field.
    """
    mine = make_contract(session_factory, tenant)
    theirs = make_contract(session_factory, other_tenant)
    add_contact(session_factory, mine, days_ago=NO_RESPONSE_DAYS + 1)
    add_contact(session_factory, theirs, days_ago=NO_RESPONSE_DAYS + 1)

    assert scanner(session_factory).run_once().opened == 2

    assert escalations_for(session_factory, mine.id)[0].tenant_id == tenant.id
    assert escalations_for(session_factory, theirs.id)[0].tenant_id == other_tenant.id
    assert {row.tenant_id for row in audit_rows(session_factory)} == {
        tenant.id,
        other_tenant.id,
    }


def test_the_configured_window_is_the_one_that_is_used(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Three days of silence escalates under a three-day window, not fourteen."""
    old = make_contract(session_factory, tenant)
    fresh = make_contract(session_factory, tenant)
    add_contact(session_factory, old, days_ago=5)
    add_contact(session_factory, fresh, days_ago=2, template="T-60")

    assert scanner(session_factory, no_response_days=3).run_once().opened == 1
    assert len(escalations_for(session_factory, old.id)) == 1
    assert escalations_for(session_factory, fresh.id) == []


def test_an_idle_pass_reports_no_work(session_factory: sessionmaker[Session]) -> None:
    """An empty database is a quiet tick, not an error."""
    result: ScanResult = scanner(session_factory).run_once()

    assert result == ScanResult(candidates=0, opened=0, refused=())
    assert result.worked is False


def test_run_forever_stops_when_the_event_is_set(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The loop is stoppable, and it does the work before it is asked to stop."""
    contract = make_contract(session_factory, tenant)
    add_contact(session_factory, contract, days_ago=NO_RESPONSE_DAYS + 1)

    worker = scanner(session_factory)
    stop = threading.Event()
    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    try:
        # Wait for the first pass rather than sleeping a fixed time, so the test
        # fails on the behaviour and not on a slow machine.
        deadline = _naive_now() + timedelta(seconds=10)
        while not escalations_for(session_factory, contract.id) and _naive_now() < deadline:
            thread.join(0.02)
        assert escalations_for(session_factory, contract.id), "the scanner never opened one"
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not thread.is_alive()


def test_a_refusal_does_not_abort_the_rest_of_the_batch(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """A refusal writes nothing, so it must not roll back its neighbours.

    The claim query is meant to make a refusal unreachable, so this drives the
    branch directly: the contract is escalated *between* the claim and the policy
    check, which is the only way the two can disagree. The point is that the other
    contract in the same batch still gets its escalation, and the pass reports the
    code instead of hiding it.
    """
    refused = make_contract(session_factory, tenant)
    allowed = make_contract(session_factory, tenant)

    class RacingScanner(EscalationScanner):
        """Opens an escalation behind the scanner's back, once.

        The row is written into the scanner's *own* session so it is visible to the
        policy check that follows, which is the only faithful way to reproduce "a
        second writer got there between the claim and the decision".
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._raced = False

        def _escalate(
            self,
            session: Session,
            contract: Contract,
            *,
            deadline: datetime,
            refused: list[str],
        ) -> bool:
            if contract.id == refused_contract.id and not self._raced:
                self._raced = True
                EscalationRepository(session, refused_contract.tenant_id).create(
                    contract_id=refused_contract.id,
                    correlation_id=str(uuid.uuid4()),
                    deadline=deadline,
                )
            return super()._escalate(session, contract, deadline=deadline, refused=refused)

    refused_contract = refused
    add_contact(session_factory, refused, days_ago=NO_RESPONSE_DAYS + 1)
    add_contact(session_factory, allowed, days_ago=NO_RESPONSE_DAYS + 1)

    result = RacingScanner(
        session_factory,
        settings=Settings(
            app_env="test",
            log_json=False,
            escalation_scan_batch_size=6,
        ),
    ).run_once()

    assert result.refused == ("ALREADY_ESCALATED",)
    assert result.opened == 1
    # The contract that was allowed to escalate did, despite the refusal beside it.
    assert load_contract(session_factory, allowed.id).status is ContractStatus.ESCALATED
