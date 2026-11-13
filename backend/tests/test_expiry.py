"""The expiry sweep: the other half of the same clock.

``ContractStatus.EXPIRED`` has existed since the first migration with no writer
anywhere. A contract whose term ran out therefore stayed ``active`` for ever: counted
as live on the dashboard, still eligible for an escalation chase, and still reading to
``assert_may_contact`` as a file no human had closed.

The consequence that mattered was worse than a stale count. ``build_notification``
computes "today" in the customer's zone and ``milestone_for`` returns the *tightest*
milestone already due — for a past end date that is every one of them. So asking for a
reminder on an expired contract **produced one**: a ``T-14`` row dated before the term
ended, claimed on the next scheduler tick. The message itself was the false statement.

These tests are built around five claims:

* **The verdict is the customer's calendar day.** A contract ending today is still live
  today; it is over when the next day starts where the customer is, which can be up to
  fourteen hours before or after it starts in UTC. Both directions are pinned with two
  contracts that share an end date and differ only in zone.
* **The lease over-selects, the verdict decides.** The claim compares end dates with a
  one-day grace, because the row's zone is not in the index. A row that turns out to be
  still current is counted, not closed, and not an error.
* **A row that cannot be judged is counted, not guessed at.** An unresolvable zone
  leaves the contract alone and one bad row does not stop the rest of the page.
* **Closing is one audited state change.** Exactly one row, before and after, the date
  and the zone in the reason, and no contact value anywhere in the trail.
* **The status set is the planner's, verbatim.** The repository layer forced a second
  copy of the tuple; drift between them is a contract the planner still plans for and
  the sweep never closes, or the reverse.

Clock note: ``run_once``/``drain`` take an injected ``now``, so "the term ended" is a
parameter rather than a back-dated row. The fixed instant below is deliberate: expiry
arithmetic that depended on the wall clock would change its answer on the day the suite
happened to run.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core import metrics
from app.core.config import Settings
from app.core.crypto import FieldCipher
from app.db.enums import Channel, ContractStatus, FieldName, FieldStatus
from app.db.models import AgentDecision, AuditLog, Contract, ExtractedField, Tenant
from app.db.repositories.contracts import (
    EXPIRABLE_CONTRACT_STATUSES,
    REMINDABLE_CONTRACT_STATUSES,
)
from app.db.session import session_scope
from app.workers.dispatch import DispatchRefusedError, build_notification
from app.workers.expiry import ExpirySweep, is_expired

#: A fixed naive UTC instant, the same kind ``utc_now`` returns. Monday 2026-06-01,
#: mid-afternoon in UTC so that "the same instant in another zone" is a different day
#: for zones more than twelve hours away in either direction.
NOW = datetime(2026, 6, 1, 12, 0)
TODAY = NOW.date()

#: Early morning UTC, when a zone eleven hours behind is still on the previous day.
EARLY = datetime(2026, 6, 1, 2, 0)

#: UTC+14 and UTC-11: the two extremes, so a same-instant difference is a whole day.
AHEAD_OF_UTC = "Pacific/Kiritimati"
BEHIND_UTC = "Pacific/Midway"

#: Resolvable by nothing, and refused rather than silently treated as UTC.
UNRESOLVABLE_ZONE = "Mars/Olympus"

#: The value no row may store in the clear. Distinct string so a leak is greppable.
CONTACT_EMAIL = "a.rahman@example.test"

#: One key for the whole module: this worker never reads a field, but the fixture holds
#: one so a leak into the audit trail is greppable, and a plaintext row would be
#: unreadable to every other consumer of the same database.
CIPHER = FieldCipher(Fernet.generate_key().decode("ascii"))

#: Spelled out rather than imported, so renaming a constant fails a test instead of
#: silently agreeing with the code it is supposed to pin.
ACTION_EXPIRED = "contract.expired"
ENTITY_CONTRACT = "contracts"
ACTOR_WORKER = "worker"
REFUSAL_CONTRACT_CLOSED = "CONTRACT_CLOSED"
REFUSAL_UNKNOWN_TIMEZONE = "UNKNOWN_TIMEZONE"
TEMPLATE_TIGHT = "T-14"


# --------------------------------------------------------------------------- #
# fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def clean_registry():
    """Every test starts and ends with empty samples.

    The sweep counts after its commit, so the counters are part of the behaviour worth
    asserting — and an absolute assertion needs a known starting point.
    """
    metrics.reset()
    yield
    metrics.reset()


def make_contract(
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    *,
    end_in_days: int | None = -1,
    timezone_name: str = "UTC",
    status: ContractStatus = ContractStatus.ACTIVE,
    consent_email: bool = True,
    do_not_contact: bool = False,
) -> Contract:
    """Create one committed contract whose end date is relative to :data:`TODAY`.

    ``end_in_days=-1`` is "the term ended yesterday", which is the whole subject of
    this module; ``0`` is the boundary case that must stay live; ``None`` is a document
    whose date a reviewer has not supplied yet.
    """
    with session_scope(session_factory) as session:
        row = Contract(
            tenant_id=tenant.id,
            customer_ref="A. Rahman",
            end_date=None if end_in_days is None else TODAY + timedelta(days=end_in_days),
            timezone=timezone_name,
            status=status,
            consent_email=consent_email,
            consent_sms=False,
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
    status: FieldStatus = FieldStatus.APPROVED,
) -> None:
    """Give the contract an approved email, sealed, exactly as extraction would."""
    with session_scope(session_factory) as session:
        session.add(
            ExtractedField(
                tenant_id=contract.tenant_id,
                contract_id=contract.id,
                field_name=FieldName.EMAIL,
                raw_value=CIPHER.encrypt(CONTACT_EMAIL),
                normalized_value=CIPHER.encrypt(CONTACT_EMAIL),
                confidence=0.93,
                status=status,
            )
        )


def sweep(
    session_factory: sessionmaker[Session],
    *,
    batch_size: int = 50,
    poll_interval_seconds: float = 0.01,
) -> ExpirySweep:
    """A sweep over the test database with an uninjectable clock.

    The settings object is built rather than mutated so pydantic validates the bounds,
    which is also how the production process receives them.
    """
    settings = Settings(
        app_env="test",
        log_json=False,
        expiry_sweep_batch_size=batch_size,
        expiry_sweep_poll_interval_seconds=poll_interval_seconds,
    )
    return ExpirySweep(session_factory, settings=settings)


def status_of(session_factory: sessionmaker[Session], contract_id: int) -> ContractStatus:
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract_id)
        assert row is not None
        return row.status


def version_of(session_factory: sessionmaker[Session], contract_id: int) -> int:
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract_id)
        assert row is not None
        return int(row.version)


def audit_rows(session_factory: sessionmaker[Session]) -> list[AuditLog]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(AuditLog).order_by(AuditLog.id)).scalars())


def decision_for(contract: Contract) -> AgentDecision:
    """An unsaved decision row, which is all ``build_notification`` reads.

    ``build_notification`` is typed ``Any`` for exactly this reason and says so: the
    policy reads ``channel`` and ``needs_review`` and nothing else. The row is never
    persisted, so it needs no tenant-stamped write and cannot be mistaken for a
    decision this worker made.
    """
    return AgentDecision(
        tenant_id=contract.tenant_id,
        contract_id=contract.id,
        channel=Channel.EMAIL,
        confidence=0.9,
        reasoning="deterministic channel selection",
        needs_review=False,
    )


def samples(outcome: str) -> float:
    """The value of one ``contracts_expired_total`` series in the current registry."""
    total = 0.0
    for line in metrics.render().splitlines():
        if line.startswith("contracts_expired_total{") and f'outcome="{outcome}"' in line:
            total += float(line.rsplit(" ", 1)[1])
    return total


# --------------------------------------------------------------------------- #
# the duplicated status set
# --------------------------------------------------------------------------- #


def test_the_expirable_statuses_are_exactly_the_remindable_ones() -> None:
    """The change the reminder planner's window was narrowed for, made consistent.

    ``claim_expired_candidates`` lives in ``contracts.py`` and does not import the
    planner, so the tuple is written twice — the same duplication
    ``REMINDABLE_CONTRACT_STATUSES`` already carries and for the same reason. Drift
    would be silent in both directions: a status the planner still plans for and the
    sweep never closes (the defect this worker exists to fix), or one it closes while
    the planner keeps trying to remind.
    """
    assert EXPIRABLE_CONTRACT_STATUSES == REMINDABLE_CONTRACT_STATUSES
    assert EXPIRABLE_CONTRACT_STATUSES == (
        ContractStatus.ACTIVE,
        ContractStatus.REMINDER_SENT,
    )


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #


def test_a_contract_ending_today_is_still_live_today(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The boundary the whole rule turns on.

    Exceptional because it is the one direction that cannot be corrected afterwards: a
    contract closed a day early has a reminder silenced that a person was going to
    read this morning.
    """
    contract = make_contract(session_factory, tenant, end_in_days=0)

    assert is_expired(contract, now=NOW) is False


def test_a_contract_that_ended_yesterday_has_expired(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    contract = make_contract(session_factory, tenant, end_in_days=-1)

    assert is_expired(contract, now=NOW) is True


@pytest.mark.parametrize("hour", [0, 6, 12, 23])
def test_the_verdict_does_not_depend_on_the_time_of_day(
    session_factory: sessionmaker[Session], tenant: Tenant, hour: int
) -> None:
    """A UTC contract's day is the UTC day, whatever hour it is read at.

    Pinned because the neighbouring rule — the customer's zone — is easy to over-apply
    into "the day is not over until midnight somewhere", which would close nothing on
    the tick an operator was watching.
    """
    ended = make_contract(session_factory, tenant, end_in_days=-1)
    current = make_contract(session_factory, tenant, end_in_days=0)
    moment = NOW.replace(hour=hour)

    assert is_expired(ended, now=moment) is True
    assert is_expired(current, now=moment) is False


def test_a_contract_with_no_end_date_never_expires(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """A document whose date a reviewer has not supplied is not a contract that ended.

    It is a contract nobody can schedule, which the planner already refuses with
    ``MISSING_END_DATE``; closing it here would hide that from the review queue.
    """
    contract = make_contract(session_factory, tenant, end_in_days=None)

    assert is_expired(contract, now=NOW) is False


def test_a_zone_behind_utc_keeps_the_contract_live_for_its_own_last_day(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Same end date, same instant, different answer: the zone is what decides.

    At 02:00 UTC it is already 2026-06-01 in UTC and still 2025-05-31 in a zone eleven
    hours behind, so a contract ending 2026-05-31 is over for the UTC one and not over
    for the other. Server-date arithmetic would close the second one while its
    customer's last day was still running.
    """
    behind = make_contract(session_factory, tenant, timezone_name=BEHIND_UTC)
    utc = make_contract(session_factory, tenant)

    assert is_expired(utc, now=EARLY) is True
    assert is_expired(behind, now=EARLY) is False


def test_a_zone_ahead_of_utc_closes_the_contract_a_day_before_utc_would(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The mirror case, and the reason the grace in the lease is a day either way.

    At 12:00 UTC it is already the next day in a zone fourteen hours ahead, so a
    contract ending today is over there and still live here. A sweep that compared
    against the server's date would leave it open for another twenty-four hours.
    """
    ahead = make_contract(session_factory, tenant, end_in_days=0, timezone_name=AHEAD_OF_UTC)
    utc = make_contract(session_factory, tenant, end_in_days=0)

    assert is_expired(ahead, now=NOW) is True
    assert is_expired(utc, now=NOW) is False


def test_an_unresolvable_zone_is_refused_rather_than_guessed(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Fail closed: assuming UTC would close the contract a day early or a day late.

    A day either side of a term end is a message somebody reads, so the sweep counts
    these instead of inventing an answer — see the page-level test below.
    """
    contract = make_contract(session_factory, tenant, timezone_name=UNRESOLVABLE_ZONE)

    with pytest.raises(DispatchRefusedError) as caught:
        is_expired(contract, now=NOW)

    assert caught.value.code == REFUSAL_UNKNOWN_TIMEZONE


# --------------------------------------------------------------------------- #
# what the page closes, and what it leaves alone
# --------------------------------------------------------------------------- #


def test_a_contract_whose_term_ended_is_closed(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    contract = make_contract(session_factory, tenant, end_in_days=-1)

    result = sweep(session_factory).run_once(now=NOW)

    assert result.candidates == 1
    assert result.expired == (str(contract.id),)
    assert result.worked is True
    assert status_of(session_factory, contract.id) is ContractStatus.EXPIRED


def test_a_contract_ending_today_is_counted_and_left_active(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Over-selection is the design, not a miss: the lease is a day generous.

    ``still_current`` is the steady-state reading of a UTC-morning tick, so the result
    must not describe it as work — a quiet pass that reported ``worked`` would make
    every deployment's log and dashboard claim a change no row holds.
    """
    contract = make_contract(session_factory, tenant, end_in_days=0)

    result = sweep(session_factory).run_once(now=NOW)

    assert result.candidates == 1
    assert result.expired == ()
    assert result.still_current == 1
    assert result.worked is False
    assert status_of(session_factory, contract.id) is ContractStatus.ACTIVE


def test_a_second_tick_finds_nothing_even_from_a_fresh_instance(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Idempotence comes from the status, not from the cursor.

    A fresh instance starts at cursor zero, so this is the assertion that the closed
    contract has genuinely left the predicate — and that no second audit row appears.
    """
    contract = make_contract(session_factory, tenant, end_in_days=-1)
    sweep(session_factory).run_once(now=NOW)

    again = sweep(session_factory).run_once(now=NOW)

    assert again.candidates == 0
    assert again.expired == ()
    assert len(audit_rows(session_factory)) == 1
    assert status_of(session_factory, contract.id) is ContractStatus.EXPIRED


def test_a_contract_with_no_end_date_is_never_leased(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    make_contract(session_factory, tenant, end_in_days=None)

    result = sweep(session_factory).run_once(now=NOW)

    assert result.candidates == 0


def test_a_future_contract_is_not_leased(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    make_contract(session_factory, tenant, end_in_days=30)

    result = sweep(session_factory).run_once(now=NOW)

    assert result.candidates == 0


def test_a_do_not_contact_contract_still_expires(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Opting out is a statement about messages, not about the term.

    ``do_not_contact`` silences the dispatch gate; it does not mean the contract is
    still running. Skipping it here would leave an opted-out file in ``active`` for
    ever, which is exactly the stale-state defect this worker exists to fix.
    """
    contract = make_contract(
        session_factory, tenant, end_in_days=-1, consent_email=False, do_not_contact=True
    )

    sweep(session_factory).run_once(now=NOW)

    assert status_of(session_factory, contract.id) is ContractStatus.EXPIRED


@pytest.mark.parametrize(
    "status",
    [ContractStatus.RESPONDED, ContractStatus.ESCALATED, ContractStatus.EXPIRED],
)
def test_a_contract_a_person_has_already_accounted_for_is_never_closed(
    session_factory: sessionmaker[Session], tenant: Tenant, status: ContractStatus
) -> None:
    """``responded`` and ``escalated`` are facts a human established.

    A reply after the term ended is still a reply, and overwriting ``responded`` with
    ``expired`` would erase the more informative of the two; ``escalated`` is a queue
    entry somebody is working. Both stay as they are, and ``expired`` going round again
    is what makes a repeated tick free.
    """
    contract = make_contract(session_factory, tenant, end_in_days=-1, status=status)

    result = sweep(session_factory).run_once(now=NOW)

    assert result.candidates == 0
    assert audit_rows(session_factory) == []
    assert status_of(session_factory, contract.id) is status


# --------------------------------------------------------------------------- #
# the audit trail
# --------------------------------------------------------------------------- #


def test_closing_writes_exactly_one_audit_row(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    contract = make_contract(session_factory, tenant, end_in_days=-1)

    sweep(session_factory).run_once(now=NOW)

    rows = audit_rows(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.action == ACTION_EXPIRED
    assert row.entity == ENTITY_CONTRACT
    assert row.entity_id == str(contract.id)
    assert row.actor_type == ACTOR_WORKER
    # No user did this. An actor id here would name somebody who could be asked why.
    assert row.actor_id is None


def test_the_audit_row_records_the_status_it_replaced(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Before *and* after, because "expired" alone does not say from where.

    ``reminder_sent`` and ``active`` are both expirable, and an operator reading the
    trail needs to know whether the customer had already been chased.
    """
    make_contract(session_factory, tenant, end_in_days=-1, status=ContractStatus.REMINDER_SENT)
    ended_on = (TODAY - timedelta(days=1)).isoformat()

    sweep(session_factory).run_once(now=NOW)

    row = audit_rows(session_factory)[0]
    # The snapshots are ``Text`` columns holding JSON, so they are parsed rather than
    # compared as strings: a reordered key must not read as a behaviour change.
    before = json.loads(row.before_state or "null")
    after = json.loads(row.after_state or "null")
    assert set(before) == {"status", "end_date"}
    assert set(after) == {"status", "end_date"}
    assert before["status"] == ContractStatus.REMINDER_SENT.value
    assert after["status"] == ContractStatus.EXPIRED.value
    assert before["end_date"] == after["end_date"] == ended_on


def test_the_audit_row_names_the_date_and_the_zone_that_closed_it(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The reason has to be enough to argue with.

    "Expired" answers nothing when a customer disputes it; the date and the zone it was
    judged in are what a person re-checks.
    """
    make_contract(session_factory, tenant, end_in_days=-1, timezone_name=BEHIND_UTC)

    sweep(session_factory).run_once(now=NOW)

    reason = audit_rows(session_factory)[0].reason
    assert reason is not None
    assert (TODAY - timedelta(days=1)).isoformat() in reason
    assert BEHIND_UTC in reason


def test_the_audit_trail_never_contains_a_contact_value(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The one row this worker writes is about a date, so no contact value belongs.

    Asserted over the whole trail rather than the row, so a future field added to the
    snapshot shows up here as a leak instead of passing quietly.
    """
    contract = make_contract(session_factory, tenant, end_in_days=-1)
    add_field(session_factory, contract)

    sweep(session_factory).run_once(now=NOW)

    for row in audit_rows(session_factory):
        trail = " ".join(
            [
                row.before_state or "",
                row.after_state or "",
                row.reason or "",
                row.action,
                row.entity,
                row.entity_id,
            ]
        )
        assert CONTACT_EMAIL not in trail


def test_closing_bumps_the_optimistic_lock(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """What stops this write from overwriting a concurrent operator edit.

    ``Contract`` carries ``version_id_col``, so assigning ``status`` is enough — but
    only if the column really moves, which is what makes the second writer's commit
    raise ``StaleDataError`` instead of silently reverting a correction.
    """
    contract = make_contract(session_factory, tenant, end_in_days=-1)
    before = version_of(session_factory, contract.id)

    sweep(session_factory).run_once(now=NOW)

    assert version_of(session_factory, contract.id) == before + 1


# --------------------------------------------------------------------------- #
# tenants, and a row that cannot be judged
# --------------------------------------------------------------------------- #


def test_one_tick_closes_contracts_from_different_tenants_and_stamps_each_row(
    session_factory: sessionmaker[Session], tenant: Tenant, other_tenant: Tenant
) -> None:
    """The claim is unscoped — one process serves every tenant.

    So the hostile case is a contract belonging to somebody else, and the assertion is
    that each audit row lands in the tenant that owns the contract rather than in the
    tenant of whichever row the page happened to read first.
    """
    mine = make_contract(session_factory, tenant, end_in_days=-1)
    theirs = make_contract(session_factory, other_tenant, end_in_days=-1)

    result = sweep(session_factory).run_once(now=NOW)

    assert result.expired == (str(mine.id), str(theirs.id))
    by_entity = {row.entity_id: row for row in audit_rows(session_factory)}
    assert by_entity[str(mine.id)].tenant_id == tenant.id
    assert by_entity[str(theirs.id)].tenant_id == other_tenant.id


def test_an_unresolvable_zone_is_counted_and_does_not_stop_the_page(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """One poisoned row costs one contract, not the batch.

    The sweep has no single transaction for the page to lose: the row is skipped with a
    warning and the contracts behind it are still closed. A worker that raised here
    would be blocked for ever by whichever file recorded its zone badly.
    """
    broken = make_contract(session_factory, tenant, timezone_name=UNRESOLVABLE_ZONE)
    fine = make_contract(session_factory, tenant, end_in_days=-1)

    result = sweep(session_factory).run_once(now=NOW)

    assert result.candidates == 2
    assert result.unknown_timezone == (str(broken.id),)
    assert result.expired == (str(fine.id),)
    assert status_of(session_factory, broken.id) is ContractStatus.ACTIVE
    assert status_of(session_factory, fine.id) is ContractStatus.EXPIRED


# --------------------------------------------------------------------------- #
# the metric
# --------------------------------------------------------------------------- #


def test_the_three_outcomes_are_counted_separately(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """Three different operator questions, so three series rather than one total.

    A single ``expired_total`` would make a deployment that cannot resolve one zone
    indistinguishable from one that has simply nothing due.
    """
    make_contract(session_factory, tenant, end_in_days=-1)
    make_contract(session_factory, tenant, end_in_days=0)
    make_contract(session_factory, tenant, timezone_name=UNRESOLVABLE_ZONE)

    sweep(session_factory).run_once(now=NOW)

    assert samples("expired") == 1.0
    assert samples("still_current") == 1.0
    assert samples("unknown_timezone") == 1.0


def test_a_tick_with_nothing_to_lease_counts_nothing(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """No samples at all, rather than a zero series nobody asked for.

    The scrape then shows the family only once this deployment has actually judged
    something, which is what makes the graph's first point meaningful.
    """
    make_contract(session_factory, tenant, end_in_days=30)

    sweep(session_factory).run_once(now=NOW)

    assert samples("expired") == 0.0
    assert samples("still_current") == 0.0
    assert samples("unknown_timezone") == 0.0


# --------------------------------------------------------------------------- #
# the cursor, and the loop
# --------------------------------------------------------------------------- #


def test_a_still_current_contract_does_not_starve_the_row_behind_it(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The cursor's whole purpose, because over-selection keeps rows in the predicate.

    A contract ending today stays a candidate for every tick until tomorrow, so with a
    batch of one a re-read from zero would find it for ever and never reach the expired
    contract behind it. Without the cursor this test hangs at ``expired == ()`` while
    the tick still reports candidates and looks healthy.
    """
    current = make_contract(session_factory, tenant, end_in_days=0)
    ended = make_contract(session_factory, tenant, end_in_days=-1)

    worker = sweep(session_factory, batch_size=1)
    first = worker.run_once(now=NOW)
    second = worker.run_once(now=NOW)

    assert first.candidates == 1
    assert first.expired == ()
    assert first.still_current == 1
    assert second.candidates == 1
    assert second.expired == (str(ended.id),)
    assert status_of(session_factory, current.id) is ContractStatus.ACTIVE
    assert status_of(session_factory, ended.id) is ContractStatus.EXPIRED


def test_drain_covers_a_table_larger_than_one_page(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """``--once`` on a first deployment is the whole backlog, not one page of it.

    A cron-style run that cleared a page per invocation would leave old files looking
    live for as many hours as there were pages, so ``drain`` loops until a short page.
    """
    ids = [
        make_contract(session_factory, tenant, end_in_days=-1).id,
        make_contract(session_factory, tenant, end_in_days=-1).id,
        make_contract(session_factory, tenant, end_in_days=-1).id,
    ]

    result = sweep(session_factory, batch_size=1).drain(now=NOW)

    assert result.expired == tuple(str(contract_id) for contract_id in ids)
    assert result.candidates == 3


def test_a_zero_sized_page_leases_nothing(
    session_factory: sessionmaker[Session], tenant: Tenant
) -> None:
    """The guard on ``limit``, reachable only through the constructor override.

    Settings forbid a zero batch, so this cannot happen in a deployment; the assertion
    is that the repository returns an empty page rather than issuing ``LIMIT 0`` and
    looping a caller that expects progress.
    """
    make_contract(session_factory, tenant, end_in_days=-1)
    settings = Settings(app_env="test", log_json=False)
    worker = ExpirySweep(session_factory, settings=settings, batch_size=0)

    result = worker.drain(now=NOW)

    assert result.candidates == 0
    assert result.expired == ()
    # ``drain`` terminating at all is the claim: a zero page is short, so it stops.
    assert result.worked is False


def test_run_forever_returns_as_soon_as_stop_is_set(
    session_factory: sessionmaker[Session],
) -> None:
    stop = threading.Event()
    stop.set()

    sweep(session_factory).run_forever(stop)


def test_a_failing_tick_does_not_kill_the_worker(
    session_factory: sessionmaker[Session],
) -> None:
    """A transient database fault must cost one tick, not the process.

    Nothing was committed for the failed page, so the next tick re-reads it. The event
    is set inside the failing tick so the loop exits deterministically instead of
    polling until the test's own timeout.
    """
    stop = threading.Event()
    worker = sweep(session_factory)

    def boom(*, now: datetime | None = None):
        stop.set()
        raise RuntimeError("the database went away")

    worker.run_once = boom  # type: ignore[method-assign]
    worker.run_forever(stop)

    assert stop.is_set()


# --------------------------------------------------------------------------- #
# end to end: the reminder the sweep silences
# --------------------------------------------------------------------------- #


def test_the_sweep_stops_a_back_dated_reminder_being_planned(
    session_factory: sessionmaker[Session], tenant: Tenant, settings: Settings
) -> None:
    """The defect, and then the fix, in one test.

    Before the sweep, the ordinary planning call on an expired contract returns a row:
    ``milestone_for`` takes the tightest milestone already due, which for a past end
    date is ``T-14``, and the only thing that row says to a customer is that a date has
    already passed. After the sweep the same call refuses, because the contract is
    closed — which is why the planner's window could be narrowed without this worker
    and why it cannot be reopened without it.
    """
    contract = make_contract(session_factory, tenant, end_in_days=-1)
    ended_on = contract.end_date
    assert ended_on is not None

    with session_scope(session_factory) as session:
        row = session.get(Contract, contract.id)
        assert row is not None
        premature = build_notification(row, decision_for(row), now=NOW, settings=settings)

    assert premature.template == TEMPLATE_TIGHT
    assert premature.scheduled_date < ended_on

    sweep(session_factory).run_once(now=NOW)

    with session_scope(session_factory) as session:
        row = session.get(Contract, contract.id)
        assert row is not None
        with pytest.raises(DispatchRefusedError) as caught:
            build_notification(row, decision_for(row), now=NOW, settings=settings)

    assert caught.value.code == REFUSAL_CONTRACT_CLOSED
