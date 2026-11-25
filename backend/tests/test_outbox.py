"""Outbox claim / retry / dead-letter behaviour.

The properties under test are the ones that make at-least-once delivery safe:
a row is leased before it is published, a lost lease is not overwritten, a
failure is retried with backoff, and a poison row is parked rather than dropped.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import utc_now
from app.db.enums import OutboxStatus
from app.db.models import OutboxEvent
from app.db.repositories import OutboxRepository
from app.db.repositories.outbox import (
    BACKOFF_CAP_SECONDS,
    backoff_seconds,
    backoff_with_jitter,
    outbox_pending_overdue_count,
    prune_terminal_before,
)
from app.db.session import session_scope

LEASE_SECONDS = 30
MAX_ATTEMPTS = 3
WORKER = "relay-a"

#: The grace the attention watch passes: the outbox lease, ``D10-134``'s one statement
#: of how long the relay may hold a claim before the row counts as nobody's.
GRACE = timedelta(seconds=LEASE_SECONDS)


def repository(session: Session, worker_id: str = WORKER) -> OutboxRepository:
    return OutboxRepository(
        session,
        worker_id=worker_id,
        lease_seconds=LEASE_SECONDS,
        max_attempts=MAX_ATTEMPTS,
    )


def force_due(session: Session, event_id: int) -> None:
    """Pretend the retry timer has elapsed, without sleeping."""
    session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == event_id)
        .values(next_attempt_at=utc_now())
        .execution_options(synchronize_session=False)
    )


def expire_lease(session: Session, event_id: int) -> None:
    """Pretend the owning relay died mid-publish."""
    session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == event_id)
        .values(locked_until=utc_now() - timedelta(seconds=1))
        .execution_options(synchronize_session=False)
    )


def test_enqueue_starts_pending_and_due(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()

    with session_scope(session_factory) as session:
        row = session.get(OutboxEvent, event_id)

    assert row is not None
    assert row.status is OutboxStatus.PENDING
    assert row.attempts == 0
    assert row.locked_by is None
    assert row.next_attempt_at <= utc_now()


def test_enqueued_timestamp_is_floored_to_stored_precision(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """Regression for D2-06 — the MySQL integration test caught this one.

    MySQL's ``DATETIME`` *rounds* microseconds on insert, so a row enqueued at
    ``12:00:00.7`` is stored as ``12:00:01`` and is not yet due at
    ``12:00:00.9``. The relay would then miss an event it had just written.
    Flooring the value before the write keeps "due now" meaning due now.
    """
    available_at = utc_now().replace(microsecond=987_654)
    event_id = enqueue_event(available_at=available_at)

    with session_scope(session_factory) as session:
        row = session.get(OutboxEvent, event_id)

    assert row is not None
    assert row.next_attempt_at.microsecond == 0
    assert row.next_attempt_at <= utc_now()


def test_claim_takes_a_lease(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()

    with session_scope(session_factory) as session:
        claimed = repository(session).claim_due(10)

    assert [event.id for event in claimed] == [event_id]
    assert claimed[0].status is OutboxStatus.PUBLISHING
    assert claimed[0].attempts == 1
    assert claimed[0].locked_by == WORKER
    assert claimed[0].locked_until is not None


def test_a_leased_row_is_not_handed_to_another_tick(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    enqueue_event()

    with session_scope(session_factory) as session:
        assert len(repository(session).claim_due(10)) == 1
    with session_scope(session_factory) as session:
        assert repository(session).claim_due(10) == []


def test_claim_respects_the_batch_limit(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    for _ in range(5):
        enqueue_event()

    with session_scope(session_factory) as session:
        assert len(repository(session).claim_due(2)) == 2


def test_record_success_marks_published(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()

    with session_scope(session_factory) as session:
        repository(session).claim_due(10)
    with session_scope(session_factory) as session:
        updated = repository(session).record_outcome(event_id, error=None)

    assert updated is not None
    assert updated.status is OutboxStatus.PUBLISHED
    assert updated.published_at is not None
    assert updated.locked_by is None
    assert updated.locked_until is None


def test_a_stale_worker_cannot_record_an_outcome(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()

    with session_scope(session_factory) as session:
        repository(session, worker_id="relay-a").claim_due(10)
    with session_scope(session_factory) as session:
        assert repository(session, worker_id="relay-b").record_outcome(event_id, error=None) is None

    with session_scope(session_factory) as session:
        row = session.get(OutboxEvent, event_id)

    # The row is untouched: relay-a still owns it. Overwriting here would let a
    # stalled worker clobber a newer attempt.
    assert row is not None
    assert row.status is OutboxStatus.PUBLISHING
    assert row.locked_by == "relay-a"


def test_failure_schedules_a_retry_rather_than_dropping_the_row(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()

    with session_scope(session_factory) as session:
        repository(session).claim_due(10)
    with session_scope(session_factory) as session:
        updated = repository(session).record_outcome(event_id, error="RuntimeError: broker down")

    assert updated is not None
    assert updated.status is OutboxStatus.PENDING
    assert updated.last_error == "RuntimeError: broker down"
    assert updated.next_attempt_at > utc_now()
    assert updated.locked_by is None


def test_a_row_is_dead_lettered_after_max_attempts(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()

    for _ in range(MAX_ATTEMPTS):
        with session_scope(session_factory) as session:
            claimed = repository(session).claim_due(10)
        assert [event.id for event in claimed] == [event_id]
        with session_scope(session_factory) as session:
            repository(session).record_outcome(event_id, error="RuntimeError: broker down")
            force_due(session, event_id)

    with session_scope(session_factory) as session:
        row = session.get(OutboxEvent, event_id)

    assert row is not None
    assert row.status is OutboxStatus.FAILED
    assert row.attempts == MAX_ATTEMPTS
    assert row.last_error == "RuntimeError: broker down"


def test_an_expired_lease_is_reclaimed(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()

    with session_scope(session_factory) as session:
        repository(session, worker_id="relay-a").claim_due(10)
    with session_scope(session_factory) as session:
        expire_lease(session, event_id)

    with session_scope(session_factory) as session:
        claimed = repository(session, worker_id="relay-b").claim_due(10)

    assert [event.id for event in claimed] == [event_id]
    assert claimed[0].locked_by == "relay-b"
    assert claimed[0].attempts == 2


def test_error_text_is_truncated(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()

    with session_scope(session_factory) as session:
        repository(session).claim_due(10)
    with session_scope(session_factory) as session:
        updated = repository(session).record_outcome(event_id, error="x" * 5000)

    assert updated is not None
    assert len(updated.last_error or "") == 500


def test_pending_count_sees_unpublished_rows(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    with session_scope(session_factory) as session:
        assert repository(session).pending_count() == 0

    first = enqueue_event()
    enqueue_event()

    with session_scope(session_factory) as session:
        assert repository(session).pending_count() == 2

    with session_scope(session_factory) as session:
        repository(session).claim_due(10)
        repository(session).record_outcome(first, error=None)

    # One published, one still leased and unpublished.
    with session_scope(session_factory) as session:
        assert repository(session).pending_count() == 1


# --- the backlog nothing has claimed (D10-134) -----------------------------
# ``pending_count`` above answers "how much work is in the table"; these answer the
# different question the published gauge asks: "is anything not moving?". A row
# inside its retry backoff is unpublished and healthy, so the two numbers are
# deliberately not the same number.


def test_an_empty_outbox_has_nothing_overdue(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        assert outbox_pending_overdue_count(session, grace=GRACE) == 0


def test_a_row_inside_the_grace_is_not_yet_overdue(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """A row written this instant is overdue by nobody's definition.

    The grace is what stops the gauge reading non-zero on the half second between a
    route committing an event and the relay's next poll — a number that is briefly
    true about every deployment and therefore says nothing about any of them.
    """
    enqueue_event()

    with session_scope(session_factory) as session:
        assert outbox_pending_overdue_count(session, grace=GRACE, now=utc_now()) == 0


def test_a_row_that_has_waited_longer_than_the_grace_is_counted(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """The same row, the same grace, one lease later: now it is evidence.

    Only the clock moved. That is the property that makes the reading a statement
    about the relay rather than about the writer.
    """
    enqueue_event()
    later = utc_now() + GRACE + timedelta(seconds=60)

    with session_scope(session_factory) as session:
        assert outbox_pending_overdue_count(session, grace=GRACE, now=later) == 1


def test_a_row_serving_out_a_retry_backoff_is_not_overdue(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """The reason it is a timestamp query and not ``pending_count``.

    A broker hiccup leaves rows unpublished *and* correctly scheduled, so counting
    them would make the gauge non-zero during exactly the event a relay is handling
    — the alarm-nobody-reads shape ``D10-39`` warns about. The exclusion expires
    when the retry does, which is asserted here rather than assumed.
    """
    event_id = enqueue_event()

    with session_scope(session_factory) as session:
        repository(session).claim_due(10)
    with session_scope(session_factory) as session:
        repository(session).record_outcome(event_id, error="RuntimeError: broker down")

    with session_scope(session_factory) as session:
        row = session.get(OutboxEvent, event_id)
        assert row is not None
        retry_at = row.next_attempt_at
        # One second of grace, and a clock on either side of the retry timer: before
        # it the row is backing off, at it the row is due again and nothing claimed it.
        backing_off = outbox_pending_overdue_count(
            session, grace=timedelta(seconds=1), now=retry_at - timedelta(seconds=1)
        )
        now_overdue = outbox_pending_overdue_count(
            session, grace=timedelta(seconds=1), now=retry_at + timedelta(seconds=1)
        )

    assert backing_off == 0
    assert now_overdue == 1


def test_a_published_row_is_never_overdue(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """Delivered is not pending, however old the row — and the row is not deleted."""
    event_id = enqueue_event()

    with session_scope(session_factory) as session:
        repository(session).claim_due(10)
    with session_scope(session_factory) as session:
        repository(session).record_outcome(event_id, error=None)

    with session_scope(session_factory) as session:
        assert (
            outbox_pending_overdue_count(session, grace=GRACE, now=utc_now() + timedelta(days=30))
            == 0
        )


def test_a_parked_row_belongs_to_the_park_gauge_and_not_this_one(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """Two families, two populations, no row counted twice.

    A parked row is ``failed`` and flagged by ``outbox_parked_events``; counting it
    here as well would double the report an operator reads and make a dead-letter
    backlog look like a stalled relay — two different jobs.
    """
    event_id = enqueue_event()

    for _ in range(MAX_ATTEMPTS):
        with session_scope(session_factory) as session:
            repository(session).claim_due(10)
        with session_scope(session_factory) as session:
            repository(session).record_outcome(event_id, error="RuntimeError: broker down")
            force_due(session, event_id)

    with session_scope(session_factory) as session:
        assert (
            outbox_pending_overdue_count(session, grace=GRACE, now=utc_now() + timedelta(days=1))
            == 0
        )


def test_a_lease_that_still_holds_the_row_is_not_overdue(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """A ``publishing`` row is a relay doing its job until its lease runs out.

    Only after the lease may another replica take the row, so only after the lease
    can the row be called unclaimed — which is why the query reads ``locked_until``
    rather than the status alone.
    """
    enqueue_event()

    with session_scope(session_factory) as session:
        repository(session).claim_due(10)

    with session_scope(session_factory) as session:
        inside = outbox_pending_overdue_count(session, grace=GRACE, now=utc_now())
        after = outbox_pending_overdue_count(
            session, grace=GRACE, now=utc_now() + GRACE + timedelta(seconds=1)
        )

    assert inside == 0
    assert after == 1


def test_backoff_grows_exponentially_then_caps() -> None:
    assert backoff_seconds(1) == 2
    assert backoff_seconds(2) == 4
    assert backoff_seconds(3) == 8
    assert backoff_seconds(4) == 16
    assert backoff_seconds(99) == BACKOFF_CAP_SECONDS


def test_jitter_stays_within_bounds() -> None:
    for attempts in range(1, 12):
        base = backoff_seconds(attempts)
        for _ in range(50):
            assert base * 0.8 <= backoff_with_jitter(attempts) <= base * 1.2


# --- retention (outbox-prune) ---------------------------------------------
# The outbox is a delivery record, not a history: a published row has done its job
# and a parked row is a human's to-do. Pruning therefore has to be surgical, and
# every test below is a way it could be wrong. What must never happen is a
# ``pending`` or ``publishing`` row disappearing, because that loses an event --
# the one thing the outbox exists to make impossible.

#: How far back the helpers below push a row's timestamps.
AGED = timedelta(days=90)

#: A cutoff every aged row is older than, and no freshly written row is.
CUTOFF = utc_now() - timedelta(days=30)


def publish(session_factory: sessionmaker[Session], event_id: int) -> None:
    """Drive one row to ``published`` the way the relay does."""
    with session_scope(session_factory) as session:
        repository(session).claim_due(10)
    with session_scope(session_factory) as session:
        repository(session).record_outcome(event_id, error=None)


def park(session_factory: sessionmaker[Session], event_id: int) -> None:
    """Drive one row to ``failed`` the way the relay does, without sleeping."""
    for _ in range(MAX_ATTEMPTS):
        with session_scope(session_factory) as session:
            repository(session).claim_due(10)
        with session_scope(session_factory) as session:
            repository(session).record_outcome(event_id, error="RuntimeError: broker down")
            force_due(session, event_id)


def age_row(session_factory: sessionmaker[Session], event_id: int, **values: object) -> None:
    """Move a row's timestamps into the past without waiting for one.

    Explicit ``values`` beat the model's ``onupdate`` hook, which is what lets a
    test age a ``failed`` row's ``updated_at`` and pin the second branch of the
    prune predicate.
    """
    with session_scope(session_factory) as session:
        session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )


def exists(session_factory: sessionmaker[Session], event_id: int) -> bool:
    with session_scope(session_factory) as session:
        return session.get(OutboxEvent, event_id) is not None


def test_a_published_row_past_the_cutoff_is_deleted_and_named(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """The report is built before the ``DELETE``, so it can name what it removed."""
    event_id = enqueue_event()
    publish(session_factory, event_id)
    age_row(session_factory, event_id, published_at=utc_now() - AGED)

    with session_scope(session_factory) as session:
        pruned = prune_terminal_before(session, before=CUTOFF)

    assert [row.id for row in pruned] == [event_id]
    assert pruned[0].status == OutboxStatus.PUBLISHED.value
    assert pruned[0].event_type == "contract.ingested"
    # Asked because "how much of which tenant's history did I just delete" is the
    # only question this report exists to answer.
    assert pruned[0].tenant_id
    assert not exists(session_factory, event_id)


def test_a_published_row_inside_the_cutoff_survives(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """Publishing is what dates a row, not enqueueing: ``published_at`` is the cutoff."""
    event_id = enqueue_event()
    publish(session_factory, event_id)
    age_row(session_factory, event_id, created_at=utc_now() - AGED)

    with session_scope(session_factory) as session:
        assert prune_terminal_before(session, before=CUTOFF) == []

    # Old enough to enqueue is not old enough to delete: it finished its job today.
    assert exists(session_factory, event_id)


def test_a_pending_row_is_never_deleted_however_old(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """The one failure this command must not have: deleting an undelivered event."""
    event_id = enqueue_event()
    age_row(
        session_factory,
        event_id,
        created_at=utc_now() - AGED,
        next_attempt_at=utc_now() - AGED,
    )

    with session_scope(session_factory) as session:
        assert prune_terminal_before(session, before=utc_now(), include_failed=True) == []

    assert exists(session_factory, event_id)


def test_an_in_flight_row_is_never_deleted_however_old(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """``publishing`` means a relay holds the row. Its status is not settled, so nor is it."""
    event_id = enqueue_event()
    with session_scope(session_factory) as session:
        repository(session).claim_due(10)
    age_row(session_factory, event_id, updated_at=utc_now() - AGED)

    with session_scope(session_factory) as session:
        assert prune_terminal_before(session, before=utc_now(), include_failed=True) == []

    assert exists(session_factory, event_id)


def test_a_parked_row_survives_by_default(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """``D7-01``: a park is a row a human was meant to look at, so it is not housekeeping."""
    event_id = enqueue_event()
    park(session_factory, event_id)
    age_row(session_factory, event_id, updated_at=utc_now() - AGED)

    with session_scope(session_factory) as session:
        assert prune_terminal_before(session, before=CUTOFF) == []

    assert exists(session_factory, event_id)


def test_a_parked_row_is_deleted_once_the_operator_says_so(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    event_id = enqueue_event()
    park(session_factory, event_id)
    age_row(session_factory, event_id, updated_at=utc_now() - AGED)

    with session_scope(session_factory) as session:
        pruned = prune_terminal_before(session, before=CUTOFF, include_failed=True)

    assert [row.id for row in pruned] == [event_id]
    assert pruned[0].status == OutboxStatus.FAILED.value
    assert not exists(session_factory, event_id)


def test_a_recently_parked_row_survives_an_old_cutoff(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """A parked row has no terminal timestamp, so ``updated_at`` stands in for one.

    Measuring it from ``created_at`` instead would delete a row parked a minute ago
    because the event it carries was enqueued months ago.
    """
    event_id = enqueue_event()
    age_row(session_factory, event_id, created_at=utc_now() - AGED)
    park(session_factory, event_id)

    with session_scope(session_factory) as session:
        assert prune_terminal_before(session, before=CUTOFF, include_failed=True) == []

    assert exists(session_factory, event_id)


def test_the_limit_bounds_one_run_and_a_second_run_finishes_the_job(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    ids = [enqueue_event() for _ in range(3)]
    for event_id in ids:
        publish(session_factory, event_id)
        age_row(session_factory, event_id, published_at=utc_now() - AGED)

    with session_scope(session_factory) as session:
        first = prune_terminal_before(session, before=CUTOFF, limit=2)
    with session_scope(session_factory) as session:
        second = prune_terminal_before(session, before=CUTOFF, limit=2)

    assert [row.id for row in first] == ids[:2]
    assert [row.id for row in second] == ids[2:]
    assert all(not exists(session_factory, event_id) for event_id in ids)


def test_a_limit_that_is_not_positive_deletes_nothing(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """A negative ``LIMIT`` is *unbounded* in SQL, so the cap must never reach the query.

    Measured before this guard existed: pruning with ``--limit -1`` removed every
    settled row in the table, parked ones included, while the command reported a cap
    of minus one. Zero is refused from the other side -- it reads as "do nothing" but
    a caller only passes it by mistyping the number they meant, and neither value may
    turn a bounded run into an unbounded one. Both branches of the predicate are
    exercised so the parked population is covered too, since that is the set
    ``attention_report`` counts.
    """
    published = [enqueue_event() for _ in range(2)]
    for event_id in published:
        publish(session_factory, event_id)
        age_row(session_factory, event_id, published_at=utc_now() - AGED)

    parked = enqueue_event()
    park(session_factory, parked)
    age_row(session_factory, parked, updated_at=utc_now() - AGED)

    for bad_limit in (0, -1, -1000):
        with session_scope(session_factory) as session:
            assert (
                prune_terminal_before(session, before=CUTOFF, include_failed=True, limit=bad_limit)
                == []
            )

    assert all(exists(session_factory, event_id) for event_id in [*published, parked])


def test_a_dry_run_reports_the_rows_and_deletes_nothing(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """Counting before committing is the point: the same rows, read twice."""
    event_id = enqueue_event()
    publish(session_factory, event_id)
    age_row(session_factory, event_id, published_at=utc_now() - AGED)

    with session_scope(session_factory) as session:
        dry = prune_terminal_before(session, before=CUTOFF, dry_run=True)

    assert [row.id for row in dry] == [event_id]
    assert exists(session_factory, event_id)

    with session_scope(session_factory) as session:
        real = prune_terminal_before(session, before=CUTOFF)

    assert real == dry
    assert not exists(session_factory, event_id)


def test_an_empty_table_is_not_an_error(
    session_factory: sessionmaker[Session], enqueue_event: Callable[..., int]
) -> None:
    """The steady state of a pruned table. Deleting nothing is the success case."""
    with session_scope(session_factory) as session:
        assert prune_terminal_before(session, before=CUTOFF) == []

    enqueue_event()
    with session_scope(session_factory) as session:
        assert prune_terminal_before(session, before=CUTOFF) == []
