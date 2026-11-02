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
)
from app.db.session import session_scope

LEASE_SECONDS = 30
MAX_ATTEMPTS = 3
WORKER = "relay-a"


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
