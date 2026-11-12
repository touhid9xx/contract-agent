"""The reminder planner: the clock the central loop never had.

Every other piece of the reminder path existed before this worker. ``build_notification``
decided which milestone was due, ``POST /notifications`` wrote the row, the scheduler
claimed it and the sender sent it. What was missing was the thing that *asks*: nothing
called ``build_notification`` on a schedule, so the only producer of a notification row
was a client calling the route by hand, once per contract. A deployment could extract,
review, consent and decide a contract and still never remind anyone, because the 60/30/14
promise was an operator's to keep manually.

These tests are built around six claims:

* **The scan is a superset of the policy.** ``claim_reminder_candidates`` over-selects by
  exactly the expiry grace, because the two ends of the window are decided by different
  functions: the query compares end dates, ``milestone_for`` compares a customer's local
  calendar day. Over-selection costs one refused plan; under-selection costs a reminder
  nobody sends. Both directions are pinned.
* **The stored decision is the authority.** A contract an operator has already decided is
  never re-decided here, so the planner cannot quietly undo a correction. A decision is
  written only for a contract that has none, and only once the page is going to commit.
* **A second tick is a no-op, not a duplicate.** The unique slot is the guarantee; the
  planner looks before it inserts so the collision is a quiet ``already_planned`` rather
  than an ``IntegrityError`` that would roll back every other contract in the page.
* **A refusal is the product working.** Blocked, unconsented and decision-pending
  contracts are refused on every tick until a person changes something, so they are
  counted and logged at ``debug`` instead of failing the page.
* **A blocked block cannot starve the queue.** Contracts that are refused forever keep
  matching the claim predicate, so the cursor is what proves progress. Without it a
  batch-sized block would permanently hide every contract behind it while the tick still
  looked healthy.
* **The tenant comes from the row.** The claim is unscoped — one process serves every
  tenant — so the hostile case is a contract belonging to somebody else, and the
  assertion is that each write lands in the tenant that owns the contract.

Clock note: unlike the escalation scanner, this worker takes an injected ``now``
(``run_once(now=...)``), so "a week has passed" is a parameter rather than a back-dated
row. The fixed instant below is deliberate: milestone arithmetic that depended on the
wall clock would change its answer on the day the suite happened to run.
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core import metrics
from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.db.enums import (
    Channel,
    ContractStatus,
    FieldName,
    FieldStatus,
    NotificationStatus,
)
from app.db.models import AgentDecision, AuditLog, Contract, ExtractedField, Notification, Tenant
from app.db.repositories.contracts import REMINDABLE_CONTRACT_STATUSES
from app.db.repositories.escalations import SCANNABLE_CONTRACT_STATUSES
from app.db.session import session_scope
from app.workers.reminder_planner import PlanResult, ReminderPlanner

#: A fixed naive UTC instant, the same kind ``utc_now`` returns. Monday 2026-06-01.
NOW = datetime(2026, 6, 1, 12, 0)
TODAY = NOW.date()

#: The value no row may store in the clear. Distinct string so a leak is greppable.
CONTACT_EMAIL = "a.rahman@example.test"
CONTACT_PHONE = "+15550100"

#: One key for the whole module: the planner only reads presence, but a row written in
#: the clear would be unreadable to every other consumer of the same fixture.
CIPHER = FieldCipher(Fernet.generate_key().decode("ascii"))

#: Spelled out rather than imported, so renaming a constant fails a test instead of
#: silently agreeing with the code it is supposed to pin.
TEMPLATE_LOOSE = "T-60"
TEMPLATE_MIDDLE = "T-30"
TEMPLATE_TIGHT = "T-14"
REFUSAL_NO_CONSENT = "NO_CONSENT"
REFUSAL_NO_CONTACT_VALUE = "NO_CONTACT_VALUE"
REFUSAL_PIPELINE_BLOCKED = "PIPELINE_BLOCKED"
REFUSAL_NOT_IN_WINDOW = "NOT_IN_WINDOW"
REFUSAL_NEEDS_REVIEW = "NEEDS_REVIEW"


# --------------------------------------------------------------------------- #
# fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def clean_registry():
    """Every test starts and ends with empty samples.

    The planner counts after its commit, so the counters are part of the behaviour
    worth asserting — and an absolute assertion needs a known starting point.
    """
    metrics.reset()
    yield
    metrics.reset()


def make_contract(
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    *,
    end_in_days: int | None = 20,
    timezone_name: str = "UTC",
    status: ContractStatus = ContractStatus.ACTIVE,
    consent_email: bool = True,
    consent_sms: bool = False,
    do_not_contact: bool = False,
) -> Contract:
    """Create one committed contract, with the contact policy spelled out.

    ``end_in_days=20`` puts the contract in two milestone windows (``T-60`` and
    ``T-30``), which is what makes the "most urgent due milestone wins" assertion mean
    something; a default inside a single window could not tell the two apart.
    """
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=tenant.id,
            customer_ref="A. Rahman",
            end_date=None if end_in_days is None else TODAY + timedelta(days=end_in_days),
            timezone=timezone_name,
            status=status,
            consent_email=consent_email,
            consent_sms=consent_sms,
            consent_voice=False,
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

    Sealed with :data:`CIPHER` for the same reason the extraction worker seals: the
    planner reads presence only, but the field is what the *dispatch* path would later
    contact, and a plaintext row would be refused there as unreadable.
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


def add_decision(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    channel: Channel = Channel.EMAIL,
    needs_review: bool = False,
) -> int:
    """Store one decision directly and return its id.

    Written to the table rather than through ``POST /agent/decisions`` because one case
    below needs a decision the F9 policy can never produce (``needs_review=True``), and
    because "a stored decision outranks a fresh derivation" is a fact about rows.
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


def add_notification(
    session_factory: sessionmaker[Session],
    contract: Contract,
    *,
    template: str,
    scheduled_date: date,
) -> int:
    """Seed the slot the planner would otherwise claim, as another producer would."""
    with session_scope(session_factory) as session:
        row = Notification(
            tenant_id=contract.tenant_id,
            contract_id=contract.id,
            channel=Channel.EMAIL,
            template=template,
            status=NotificationStatus.PENDING,
            scheduled_date=scheduled_date,
            scheduled_at=NOW,
            idempotency_key=f"{contract.id}:{template}:{scheduled_date.isoformat()}",
            attempts=0,
        )
        session.add(row)
        session.flush()
        return int(row.id)


def planner(
    session_factory: sessionmaker[Session],
    *,
    batch_size: int = 50,
    poll_interval_seconds: float = 0.01,
) -> ReminderPlanner:
    """A planner over the test database with an uninjectable clock.

    The settings object is built rather than mutated so pydantic validates the bounds,
    which is also how the production process receives them.
    """
    settings = Settings(
        app_env="test",
        log_json=False,
        reminder_scan_batch_size=batch_size,
        reminder_scan_poll_interval_seconds=poll_interval_seconds,
    )
    return ReminderPlanner(session_factory, settings=settings)


def notifications_for(
    session_factory: sessionmaker[Session], contract_id: int
) -> list[Notification]:
    with session_scope(session_factory) as session:
        stmt = (
            select(Notification)
            .where(Notification.contract_id == contract_id)
            .order_by(Notification.id)
        )
        return list(session.execute(stmt).scalars())


def decisions_for(session_factory: sessionmaker[Session], contract_id: int) -> list[AgentDecision]:
    with session_scope(session_factory) as session:
        stmt = (
            select(AgentDecision)
            .where(AgentDecision.contract_id == contract_id)
            .order_by(AgentDecision.id)
        )
        return list(session.execute(stmt).scalars())


def audit_rows(session_factory: sessionmaker[Session]) -> list[AuditLog]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(AuditLog)).scalars())


def load_contract(session_factory: sessionmaker[Session], contract_id: int) -> Contract:
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract_id)
        assert row is not None
        return row


# --------------------------------------------------------------------------- #
# the duplicated status set
# --------------------------------------------------------------------------- #


def test_the_remindable_statuses_are_exactly_the_scannable_ones() -> None:
    """The duplication the repository layer forced, made safe by this assertion.

    ``claim_reminder_candidates`` deliberately lives in ``contracts.py`` and does not
    import ``escalations.py``; a repository may not depend on another repository's
    business set, so the tuple is written twice. Drift between the two would mean a
    contract the scanner tracks and the planner ignores, or the reverse — a silent
    gap visible only as a reminder that never arrives.
    """
    assert REMINDABLE_CONTRACT_STATUSES == SCANNABLE_CONTRACT_STATUSES
    assert REMINDABLE_CONTRACT_STATUSES == (
        ContractStatus.ACTIVE,
        ContractStatus.REMINDER_SENT,
    )


# --------------------------------------------------------------------------- #
# what counts as a candidate
# --------------------------------------------------------------------------- #


def test_a_contract_inside_the_window_is_claimed_and_planned(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    contract = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, contract)

    result = planner(session_factory).run_once(now=NOW)

    assert result.candidates == 1
    assert result.planned == 1
    assert result.refused == ()
    assert result.worked is True


@pytest.mark.parametrize(
    ("end_in_days", "candidates", "planned", "refused"),
    [
        # The loosest milestone, due exactly today.
        (60, 1, 1, ()),
        # Inside the query's grace but one day past the policy: the over-selection the
        # docstring admits to, and the price it says it is willing to pay.
        (61, 1, 0, (REFUSAL_NOT_IN_WINDOW,)),
        # Beyond the window entirely: never claimed, so never refused.
        (62, 0, 0, ()),
        # Expired in UTC but possibly still "today" in a zone ahead of it.
        (-1, 1, 1, ()),
        # Expired and past the grace: a reminder that says "approaching expiry" about a
        # contract that already expired would be the one false statement in the message.
        (-2, 0, 0, ()),
    ],
)
def test_the_edges_of_the_window_are_where_the_policy_says_they_are(
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    end_in_days: int,
    candidates: int,
    planned: int,
    refused: tuple[str, ...],
) -> None:
    contract = make_contract(session_factory, tenant, end_in_days=end_in_days)
    add_field(session_factory, contract)

    result = planner(session_factory).run_once(now=NOW)

    assert result.candidates == candidates
    assert result.planned == planned
    assert result.refused == refused


def test_a_contract_without_an_end_date_is_never_claimed(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """``MISSING_END_DATE`` is a real refusal, but the scan does not ask the question.

    A document-ingested contract has no end date until review promotes one (D10-96).
    Refusing it every tick would fill the refusal count with a row that says nothing
    about the pipeline; the query simply excludes it and the count stays honest.
    """
    contract = make_contract(session_factory, tenant, end_in_days=None)
    add_field(session_factory, contract)

    result = planner(session_factory).run_once(now=NOW)

    assert result == PlanResult()


def test_a_do_not_contact_contract_never_occupies_the_batch(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Removed in the query, not refused in the worker.

    An opt-out cannot change with the clock — only a person or an inbound STOP sets it
    — so leaving it in the page would let the same rows occupy every batch forever.
    The remaining consent questions *are* left in, because approving a field or
    recording consent is what makes a blocked contract plannable.
    """
    contract = make_contract(session_factory, tenant, do_not_contact=True)
    add_field(session_factory, contract)

    result = planner(session_factory).run_once(now=NOW)

    assert result == PlanResult()


@pytest.mark.parametrize("status", [ContractStatus.EXPIRED, ContractStatus.RENEWED])
def test_a_closed_contract_is_not_scanned(
    session_factory: sessionmaker[Session], tenant: Tenant, status: ContractStatus
) -> None:
    contract = make_contract(session_factory, tenant, status=status)
    add_field(session_factory, contract)

    result = planner(session_factory).run_once(now=NOW)

    assert result == PlanResult()


# --------------------------------------------------------------------------- #
# which milestone, and what the row looks like
# --------------------------------------------------------------------------- #


def test_the_most_urgent_due_milestone_is_the_one_planned(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """``T-60`` and ``T-30`` are both due for a contract 20 days out; ``T-30`` wins.

    The contract is 20 days from expiry, so ``T-60`` and ``T-30`` have both come due
    and ``T-14`` has not. Planning the loosest one would send "60 days" about a
    contract 20 days away — true, useless, and the reason ``milestone_for`` returns the
    last due entry rather than the first.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, contract)

    planner(session_factory).run_once(now=NOW)

    [row] = notifications_for(session_factory, contract.id)
    assert row.template == TEMPLATE_MIDDLE
    assert row.scheduled_date == TODAY + timedelta(days=20 - 30)
    assert row.status is NotificationStatus.PENDING
    assert row.channel is Channel.EMAIL
    assert row.attempts == 0
    assert row.tenant_id == tenant.id
    assert row.idempotency_key == f"{contract.id}:{TEMPLATE_MIDDLE}:{row.scheduled_date}"


def test_a_second_tick_plans_nothing_and_says_so(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The steady state of a healthy deployment: ``already_planned`` and nothing else.

    ``already_planned`` is not a failure and must not be counted as one, or every
    deployment's graph shows a wall of errors for a queue that is exactly right.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, contract)
    worker = planner(session_factory)

    first = worker.run_once(now=NOW)
    second = worker.run_once(now=NOW)

    assert (first.planned, first.already_planned) == (1, 0)
    assert (second.planned, second.already_planned) == (0, 1)
    assert second.worked is False
    assert len(notifications_for(session_factory, contract.id)) == 1


def test_a_slot_another_producer_already_wrote_is_respected(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """``POST /notifications`` and this worker share one unique constraint.

    A row the route wrote for the same ``(contract, template, scheduled_date)`` means
    the reminder is already queued. The planner reads the slot before inserting so the
    collision is a quiet ``already_planned`` rather than an ``IntegrityError`` at
    commit that would roll back every other contract in the same page.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, contract)
    slot = TODAY + timedelta(days=20 - 30)
    add_notification(session_factory, contract, template=TEMPLATE_MIDDLE, scheduled_date=slot)

    result = planner(session_factory).run_once(now=NOW)

    assert (result.planned, result.already_planned) == (0, 1)
    assert len(notifications_for(session_factory, contract.id)) == 1


def test_the_next_milestone_is_planned_as_the_contract_approaches(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """``T-30`` today, ``T-14`` a week later — two rows, not one updated row.

    The milestone is a function of today and the end date alone, so the queue advances
    by planning the next slot when it comes due. Anything that rewrote the existing row
    would lose the record of what the customer had already been sent.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, contract)
    worker = planner(session_factory)

    worker.run_once(now=NOW)
    later = worker.run_once(now=NOW + timedelta(days=7))

    assert later.planned == 1
    assert [row.template for row in notifications_for(session_factory, contract.id)] == [
        TEMPLATE_MIDDLE,
        TEMPLATE_TIGHT,
    ]


# --------------------------------------------------------------------------- #
# the decision the plan is built from
# --------------------------------------------------------------------------- #


def test_a_contract_with_no_decision_gets_one_written_for_it(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The planner is a producer of decisions too, or nothing would plan at all.

    A contract that has never been decided cannot be planned, and a client is not
    going to call ``POST /agent/decisions`` for every contract in the table.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, contract)

    planner(session_factory).run_once(now=NOW)

    [decision] = decisions_for(session_factory, contract.id)
    assert decision.channel is Channel.EMAIL
    assert decision.needs_review is False
    assert decision.tenant_id == tenant.id


def test_a_later_milestone_reuses_the_decision_instead_of_deriving_another(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Reuse has to be observed on a tick that actually reaches the write.

    The first version of this test took two ticks at the same instant and asserted the
    decision count was one. It passed for the wrong reason: the second tick stops at
    ``find_slot`` because that milestone's slot is already taken, so it returns before
    ``decisions.add`` and the count would have stayed at one even if the planner had
    re-derived and thrown the fresh row away. Eleven days later a tighter milestone is
    due, its slot is empty, and a re-derivation appends a second decision row here.

    Unconditional derivation would also silently discard an operator's correction on
    the next pass, which ``test_a_stored_decision_outranks_a_fresh_derivation`` pins
    on the first-substitution path.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, contract)
    worker = planner(session_factory)

    worker.run_once(now=NOW)
    later = worker.run_once(now=NOW + timedelta(days=11))

    assert later.planned == 1
    assert len(notifications_for(session_factory, contract.id)) == 2
    assert len(decisions_for(session_factory, contract.id)) == 1


def test_a_stored_decision_outranks_a_fresh_derivation(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """An operator's channel choice survives the unattended tick.

    Both an email and a phone number are approved and both channels are consented, so
    the deterministic policy would choose ``email``. The stored decision says ``sms``,
    and the planned row must say ``sms``: an agent that re-decided here would undo a
    human correction every five minutes.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20, consent_sms=True)
    add_field(session_factory, contract, field_name=FieldName.EMAIL)
    add_field(session_factory, contract, field_name=FieldName.PHONE, value=CONTACT_PHONE)
    add_decision(session_factory, contract, channel=Channel.SMS)

    planner(session_factory).run_once(now=NOW)

    [row] = notifications_for(session_factory, contract.id)
    assert row.channel is Channel.SMS
    assert len(decisions_for(session_factory, contract.id)) == 1


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #


def test_a_contract_without_consent_is_refused_and_leaves_no_decision(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Refused every tick, and deliberately not persisted.

    A decision row written before the refusal would be re-read as a stored
    recommendation forever, so the only record of this contract is the refusal code in
    the tick summary — which is the right amount: the contract has not consented, and
    nothing about that is a fact worth storing.
    """
    contract = make_contract(
        session_factory, tenant, end_in_days=20, consent_email=False, consent_sms=False
    )
    add_field(session_factory, contract)

    result = planner(session_factory).run_once(now=NOW)

    assert result.refused == (REFUSAL_NO_CONSENT,)
    assert result.planned == 0
    assert decisions_for(session_factory, contract.id) == []
    assert notifications_for(session_factory, contract.id) == []


def test_an_unapproved_field_blocks_the_page(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """``pending_review`` is a veto that outranks consent, and it names the field.

    The refusal carries field *names* (the same labels the review queue shows any
    viewer) and never values, so this can be counted and logged.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, contract, status=FieldStatus.PENDING_REVIEW)

    result = planner(session_factory).run_once(now=NOW)

    assert result.refused == (REFUSAL_PIPELINE_BLOCKED,)
    assert decisions_for(session_factory, contract.id) == []


def test_a_contract_becomes_plannable_once_the_field_is_approved(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The refusal must not poison the contract for later ticks.

    This is the whole reason consent and review are *not* filtered in the claim query
    while ``do_not_contact`` is: a person approving the field is the event that turns a
    refused contract into a plannable one, and the very next tick has to notice.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20)
    field_id = add_field(session_factory, contract, status=FieldStatus.PENDING_REVIEW)
    worker = planner(session_factory)

    before = worker.run_once(now=NOW)
    with session_scope(session_factory) as session:
        row = session.get(ExtractedField, field_id)
        assert row is not None
        row.status = FieldStatus.APPROVED
    after = worker.run_once(now=NOW)

    assert before.refused == (REFUSAL_PIPELINE_BLOCKED,)
    assert (after.planned, after.refused) == (1, ())


def test_a_contract_with_no_usable_value_is_refused(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Consent granted but nothing to contact: the customer agreed, extraction failed.

    The decision *is* derived here (a channel can be chosen from consent alone), which
    makes this the second place where the deferred write earns its keep: the refusal
    comes from ``build_notification``, after derivation, so a decision written at
    derivation time would survive a contract that is never going to be contacted.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20)

    result = planner(session_factory).run_once(now=NOW)

    assert result.refused == (REFUSAL_NO_CONTACT_VALUE,)
    assert decisions_for(session_factory, contract.id) == []


def test_a_decision_flagged_for_review_plans_nothing(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The review flag is checked before anything else in ``build_notification``.

    A decision holding a recommendation a person has not confirmed must not become a
    queued message, however healthy the contract looks.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, contract)
    add_decision(session_factory, contract, needs_review=True)

    result = planner(session_factory).run_once(now=NOW)

    assert result.refused == (REFUSAL_NEEDS_REVIEW,)
    assert notifications_for(session_factory, contract.id) == []


def test_one_refused_contract_does_not_stop_the_page(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """A refusal is the expected answer, not an exception that abandons the batch.

    On a real deployment most contracts are refused on most ticks — waiting for review,
    waiting for consent — so a worker that stopped at the first one would plan almost
    nothing and look like it was running.
    """
    blocked = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, blocked, status=FieldStatus.PENDING_REVIEW)
    healthy = make_contract(session_factory, tenant, end_in_days=18)
    add_field(session_factory, healthy)

    result = planner(session_factory).run_once(now=NOW)

    assert result.candidates == 2
    assert result.planned == 1
    assert result.refused == (REFUSAL_PIPELINE_BLOCKED,)
    assert len(notifications_for(session_factory, healthy.id)) == 1


# --------------------------------------------------------------------------- #
# fairness: the cursor
# --------------------------------------------------------------------------- #


def test_a_block_of_blocked_contracts_cannot_starve_the_queue(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The claim this worker's cursor exists to make true.

    ``batch_size`` is 2 and the two contracts that can never be planned have the lowest
    ids, so an urgency-ordered or id-less scan would return them on every tick and
    return them first, forever. The assertion is that the *third* contract is reached
    on the next page without any of the blocked two changing.
    """
    for _ in range(2):
        blocked = make_contract(session_factory, tenant, end_in_days=20)
        add_field(session_factory, blocked, status=FieldStatus.PENDING_REVIEW)
    healthy = make_contract(session_factory, tenant, end_in_days=10)
    add_field(session_factory, healthy)
    worker = planner(session_factory, batch_size=2)

    first = worker.run_once(now=NOW)
    second = worker.run_once(now=NOW)

    assert (first.candidates, first.planned, len(first.refused)) == (2, 0, 2)
    assert (second.candidates, second.planned) == (1, 1)
    assert len(notifications_for(session_factory, healthy.id)) == 1


def test_drain_covers_a_table_larger_than_one_page(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """``drain`` is for a cron-style ``--once``, where one page per run would starve.

    A deployment that runs this from cron must not need three runs to see three
    contracts, so ``drain`` loops until a short page and the cursor returns to zero.
    """
    ids = []
    for end_in_days in (20, 19, 18):
        contract = make_contract(session_factory, tenant, end_in_days=end_in_days)
        add_field(session_factory, contract)
        ids.append(contract.id)
    worker = planner(session_factory, batch_size=2)

    result = worker.drain(now=NOW)

    assert (result.candidates, result.planned) == (3, 3)
    assert all(notifications_for(session_factory, contract_id) for contract_id in ids)
    # The cursor is back at zero, so the next drain re-reads from the start and finds
    # everything already planned rather than re-planning a tail.
    assert worker.drain(now=NOW) == PlanResult(candidates=3, already_planned=3)


def test_an_empty_database_is_a_quiet_tick(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    result = planner(session_factory).run_once(now=NOW)

    assert result == PlanResult()
    assert result.worked is False


# --------------------------------------------------------------------------- #
# tenant attribution
# --------------------------------------------------------------------------- #


def test_each_reminder_lands_in_the_tenant_that_owns_the_contract(
    session_factory: sessionmaker[Session], tenant: Tenant, other_tenant: Tenant
) -> None:
    """The claim is unscoped; the writes are not.

    One process serves every tenant, so the only way this worker can be safe is that
    every repository it builds comes from the row's own ``tenant_id``. The hostile case
    is two tenants in one page, and the assertion is per-row attribution rather than a
    total, which a mis-scoped write could still satisfy.
    """
    mine = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, mine)
    theirs = make_contract(session_factory, other_tenant, end_in_days=20)
    add_field(session_factory, theirs)

    result = planner(session_factory).run_once(now=NOW)

    assert result.planned == 2
    [my_notification] = notifications_for(session_factory, mine.id)
    [their_notification] = notifications_for(session_factory, theirs.id)
    assert my_notification.tenant_id == tenant.id
    assert their_notification.tenant_id == other_tenant.id
    assert decisions_for(session_factory, mine.id)[0].tenant_id == tenant.id
    assert decisions_for(session_factory, theirs.id)[0].tenant_id == other_tenant.id


# --------------------------------------------------------------------------- #
# nothing visible changes
# --------------------------------------------------------------------------- #


def test_planning_writes_no_audit_row_and_leaves_the_contract_alone(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The escalation scanner records an audit row because it changes a status.

    This worker changes nothing a person can see except that a reminder is now
    scheduled, and that fact *is* the row: ``notifications`` already answers who was
    contacted, when, on which channel, from which decision. An audit entry per planned
    reminder would duplicate a queryable table and would have to be kept in step with
    it.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, contract)

    planner(session_factory).run_once(now=NOW)

    assert audit_rows(session_factory) == []
    reloaded = load_contract(session_factory, contract.id)
    assert reloaded.status is ContractStatus.ACTIVE
    assert reloaded.version == contract.version


# --------------------------------------------------------------------------- #
# the series an operator reads
# --------------------------------------------------------------------------- #


def test_the_tick_counts_each_outcome_it_produced(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """A planner running and finding nothing looks identical to one that is not running.

    The counter is the only thing that tells those apart, so it is counted *after* the
    page commits: a page that rolled back must not leave reminders in the series that
    no queue holds.
    """
    contract = make_contract(session_factory, tenant, end_in_days=20)
    add_field(session_factory, contract)
    worker = planner(session_factory)

    worker.run_once(now=NOW)
    worker.run_once(now=NOW)

    assert metrics.REMINDER_PLANS_TOTAL.value(outcome="planned") == 1.0
    assert metrics.REMINDER_PLANS_TOTAL.value(outcome="already_planned") == 1.0


def test_a_refusal_is_counted_without_being_planned(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    contract = make_contract(
        session_factory, tenant, end_in_days=20, consent_email=False, consent_sms=False
    )
    add_field(session_factory, contract)

    planner(session_factory).run_once(now=NOW)

    assert metrics.REMINDER_PLANS_TOTAL.value(outcome="refused") == 1.0
    assert metrics.REMINDER_PLANS_TOTAL.value(outcome="planned") == 0.0


# --------------------------------------------------------------------------- #
# the process loop
# --------------------------------------------------------------------------- #


def test_run_forever_stops_when_the_event_is_set(
    session_factory: sessionmaker[Session], tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop request is honoured between ticks, not in the middle of a page.

    A page is one transaction, so "finish" means "commit or roll back". The tick is
    wrapped here only to raise the flag from inside it — the same thing the SIGTERM
    handler does — which makes the shutdown assertion deterministic instead of a sleep.
    """
    make_contract(session_factory, tenant, end_in_days=20)
    worker = planner(session_factory)
    stop = threading.Event()
    ticks: list[int] = []
    real = worker.run_once

    def counting(*, now: datetime | None = None) -> PlanResult:
        result = real(now=NOW)
        ticks.append(1)
        stop.set()
        return result

    monkeypatch.setattr(worker, "run_once", counting)
    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    thread.join(10)

    assert not thread.is_alive()
    assert len(ticks) == 1


def test_a_failed_tick_is_survived_rather_than_fatal(
    session_factory: sessionmaker[Session], tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient database fault must not end the process.

    Nothing was committed for the failed page and the cursor only advances after a
    commit, so the next tick re-reads the same candidates. That is the whole recovery
    story: no retry state, no dead-letter, just the next tick.
    """
    worker = planner(session_factory)
    stop = threading.Event()
    calls: list[int] = []

    def flaky(*, now: datetime | None = None) -> PlanResult:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("connection reset")
        stop.set()
        return PlanResult()

    monkeypatch.setattr(worker, "run_once", flaky)
    thread = threading.Thread(target=worker.run_forever, args=(stop,), daemon=True)
    thread.start()
    thread.join(10)

    assert not thread.is_alive()
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# the settings the deployment supplies
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("batch_size", [0, 1001])
def test_the_batch_size_is_bounded(batch_size: int) -> None:
    """Zero plans nothing forever; a thousand is a page nobody sized on purpose.

    Rejected at import time because the alternative is discovering the first case as a
    tick that reports ``candidates: 0`` for a week.
    """
    with pytest.raises(ValidationError):
        Settings(
            app_env="test",
            log_json=False,
            reminder_scan_batch_size=batch_size,
        )


@pytest.mark.parametrize("interval", [0.0, 3601.0])
def test_the_poll_interval_is_bounded(interval: float) -> None:
    """Zero spins over a table that has not changed; an hour is a different product.

    The upper bound exists because the loosest milestone is 60 days out: an interval
    longer than that would miss the window it was configured for.
    """
    with pytest.raises(ValidationError):
        Settings(
            app_env="test",
            log_json=False,
            reminder_scan_poll_interval_seconds=interval,
        )
