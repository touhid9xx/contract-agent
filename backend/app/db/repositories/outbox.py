"""Outbox persistence: enqueue and the claim/publish race.

Claiming uses a row lease rather than a Redis singleton lock, so N relay
replicas can run safely and a Redis outage cannot stop publishing
(DECISIONS.md D2-03).

The lease is also the **fencing token**. A relay that stalled past its lease may
still be alive; when it comes back and tries to record an outcome it discovers
the row is no longer owned by it and leaves it alone. Without that check, a
slow worker could mark a row published after another worker had already
re-published it — or worse, overwrite a newer attempt's error state.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db.base import truncate_to_second, utc_now
from app.db.enums import OutboxStatus
from app.db.models.outbox import OutboxEvent

#: Exponential backoff bounds for a failed publish.
BACKOFF_BASE_SECONDS = 2
BACKOFF_CAP_SECONDS = 300
#: ±20 % jitter so a batch that failed together does not retry in lock-step.
BACKOFF_JITTER = 0.2

#: Truncation limit for ``last_error``. Provider errors can echo a destination
#: address, so the stored text is short and the caller is expected to have
#: redacted it already.
MAX_ERROR_LENGTH = 500

#: Default number of parked rows one inspection or replay touches. Bounded so an
#: operator command cannot load an unbounded table into memory.
DEFAULT_REPLAY_LIMIT = 100


def backoff_seconds(attempts: int) -> int:
    """Delay before retrying a row that has already failed ``attempts`` times."""
    exponent = max(attempts - 1, 0)
    return int(min(BACKOFF_BASE_SECONDS * (2**exponent), BACKOFF_CAP_SECONDS))


def backoff_with_jitter(attempts: int, *, rng: random.Random | None = None) -> float:
    """``backoff_seconds`` plus bounded jitter, never negative."""
    generator = rng or random  # jitter, not cryptography
    base = backoff_seconds(attempts)
    spread = base * BACKOFF_JITTER
    return max(0.0, base + generator.uniform(-spread, spread))


def _truncate_error(error: str) -> str:
    return error[:MAX_ERROR_LENGTH]


def enqueue_event(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    correlation_id: str,
    event_type: str,
    topic: str,
    partition_key: str,
    payload: dict[str, Any],
    schema_version: int = 1,
    available_at: datetime | None = None,
) -> OutboxEvent:
    """Add an event to the outbox **inside the caller's transaction**.

    This must be called in the same session/transaction as the business write it
    announces; that atomicity is the entire reason the outbox exists. Calling it
    in a separate transaction reintroduces the dual-write problem.

    The row is flushed (not committed) before returning so the caller gets a
    populated primary key back. Flushing stays inside the caller's transaction,
    so a later rollback still discards the event along with the business write.
    """
    event = OutboxEvent(
        event_id=uuid.uuid4(),
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        event_type=event_type,
        schema_version=schema_version,
        topic=topic,
        partition_key=partition_key,
        payload=payload,
        status=OutboxStatus.PENDING,
        attempts=0,
        # Floored, not truncated by the DB: see ``truncate_to_second`` and
        # DECISIONS.md D2-06. Without this a row enqueued "now" is sometimes not
        # yet due on the relay's next tick.
        next_attempt_at=truncate_to_second(available_at or utc_now()),
    )
    session.add(event)
    session.flush()
    return event


class OutboxRepository:
    """Claim-and-record operations for the relay.

    Deliberately **not** tenant-scoped: the relay is a system actor that
    publishes for every tenant. ``tenant_id`` travels inside each envelope so
    consumers can scope their work. Every privileged access of this kind must be
    audited — see the relay's structured logs.
    """

    def __init__(
        self,
        session: Session,
        *,
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
    ) -> None:
        self._session = session
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    # --- claiming --------------------------------------------------------
    def claim_due(self, limit: int) -> list[OutboxEvent]:
        """Lease up to ``limit`` due rows for this worker.

        A row is due when it is ``pending`` and its retry time has passed, or
        when it is ``publishing`` and its lease has expired (the previous worker
        crashed mid-publish). ``SKIP LOCKED`` lets other replicas take the rest
        without blocking. SQLite has no ``FOR UPDATE`` clause; SQLAlchemy omits
        it there, which is acceptable because the test suite is single-writer —
        the real locking behaviour is covered by the MySQL integration test.
        """
        now = utc_now()
        due = or_(
            and_(
                OutboxEvent.status == OutboxStatus.PENDING,
                OutboxEvent.next_attempt_at <= now,
            ),
            and_(
                OutboxEvent.status == OutboxStatus.PUBLISHING,
                OutboxEvent.locked_until.is_not(None),
                OutboxEvent.locked_until <= now,
            ),
        )

        stmt = (
            select(OutboxEvent)
            .where(due)
            .order_by(OutboxEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

        claimed: list[OutboxEvent] = []
        for event in self._session.execute(stmt).scalars():
            event.attempts += 1
            if event.attempts > self._max_attempts:
                # Poison row: stop retrying and park it for an operator rather
                # than looping forever. Never silently dropped — the row stays
                # visible in the failed state with its last error.
                event.status = OutboxStatus.FAILED
                event.locked_by = None
                event.locked_until = None
                event.last_error = "max_attempts_exceeded"
                continue

            event.status = OutboxStatus.PUBLISHING
            event.locked_by = self._worker_id
            event.locked_until = now + timedelta(seconds=self._lease_seconds)
            claimed.append(event)

        self._session.flush()
        return claimed

    # --- recording -------------------------------------------------------
    def record_outcome(
        self,
        event_id: int,
        *,
        error: str | None,
    ) -> OutboxEvent | None:
        """Record a publish result, but only if this worker still owns the row.

        Returns the updated row, or ``None`` when the lease was lost — in which
        case nothing is written and the caller must log the fact rather than
        pretend the publish succeeded.
        """
        stmt = select(OutboxEvent).where(
            OutboxEvent.id == event_id,
            OutboxEvent.locked_by == self._worker_id,
            OutboxEvent.status == OutboxStatus.PUBLISHING,
        )
        event = self._session.execute(stmt).scalar_one_or_none()
        if event is None:
            return None

        if error is None:
            self._mark_published(event)
        else:
            self._mark_failed(event, error)
        self._session.flush()
        return event

    def _mark_published(self, event: OutboxEvent) -> None:
        event.status = OutboxStatus.PUBLISHED
        event.published_at = utc_now()
        event.locked_by = None
        event.locked_until = None
        event.last_error = None

    def _mark_failed(self, event: OutboxEvent, error: str) -> None:
        event.last_error = _truncate_error(error)
        event.locked_by = None
        event.locked_until = None

        if event.attempts >= self._max_attempts:
            event.status = OutboxStatus.FAILED
            return

        event.status = OutboxStatus.PENDING
        event.next_attempt_at = utc_now() + timedelta(seconds=backoff_with_jitter(event.attempts))

    # --- reporting -------------------------------------------------------
    def pending_count(self) -> int:
        """Unpublished rows, for the outbox backlog gauge and alert."""
        stmt = (
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status.in_([OutboxStatus.PENDING, OutboxStatus.PUBLISHING]))
        )
        return int(self._session.execute(stmt).scalar_one())


# --- dead letter (operator operations) ------------------------------------
# Module-level functions rather than OutboxRepository methods on purpose: that
# class is the relay's claim/fence machinery and its constructor demands a
# worker identity plus a lease, neither of which a replay has. These take the
# caller's session for the same reason ``enqueue_event`` does -- the caller owns
# the transaction boundary and commits once.


@dataclass(frozen=True)
class DeadLetterReplay:
    """What one replay did to one parked row.

    Carries the state *before* the reset, so the operator's log keeps the reason
    the row was parked even though the row itself no longer shows it.
    """

    id: int
    event_type: str
    tenant_id: str
    attempts: int
    last_error: str | None


def dead_lettered(session: Session, *, limit: int = DEFAULT_REPLAY_LIMIT) -> list[OutboxEvent]:
    """Rows parked after exhausting ``max_attempts``, oldest first.

    ``last_error`` is returned as stored: that text is the reason an operator is
    looking at these rows in the first place.
    """
    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.FAILED)
        .order_by(OutboxEvent.id)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def dead_letter_count(session: Session) -> int:
    """How many rows are parked in the dead-letter state, table-wide.

    :func:`dead_lettered` returns a bounded *page*, because replay is bounded — an
    operator requeues a batch, not the whole table. A count is the different
    question an attention check asks ("is anything parked at all, and how much"),
    and a page cannot answer it: once the backlog exceeds the page size the page
    stops moving while the number an alerting script needs keeps growing.

    Unscoped, for the fourth time and the same reason as :func:`dead_lettered`:
    parked events belong to every tenant, and a relay problem is not a tenant's.
    """
    stmt = (
        select(func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.FAILED)
    )
    return int(session.execute(stmt).scalar_one())


def replay_dead_lettered(
    session: Session,
    *,
    event_ids: Sequence[int] | None = None,
    limit: int = DEFAULT_REPLAY_LIMIT,
    dry_run: bool = False,
) -> list[DeadLetterReplay]:
    """Requeue parked rows for another delivery attempt; return what was touched.

    Replay is the missing half of "dead letter with replay, never log-and-drop".
    The relay parks a row precisely so a human can decide later; without this,
    "later" meant editing the table by hand.

    Semantics, each deliberate:

    * **Only ``failed`` rows are touched.** Replaying a ``published`` row would
      republish an event the business write never asked to send twice, and
      re-marking a ``pending`` or ``publishing`` row would collide with a live
      relay. The status predicate is the guard, not a caller's good intentions.
    * **``attempts`` resets to zero.** The retry budget exists to stop a live
      loop hammering a broken transport, not to cap how many times an operator
      may recover one row over its lifetime.
    * **``last_error`` clears** so the next failure is reported as itself rather
      than as stale text. The pre-replay error is handed back in
      :class:`DeadLetterReplay` so the reason is preserved in the invocation log.
    * **``next_attempt_at`` is floored, not truncated by the database** — the
      D2-06 lesson: MySQL's second-precision ``DATETIME`` rounds microseconds, so
      a row requeued at ``12:00:00.7`` could be stored as not-yet-due and be
      skipped by the very relay this was meant to feed.

    No ``SELECT ... FOR UPDATE``: the relay never claims a ``failed`` row, so
    there is no concurrent writer to fence off. Two simultaneous replays would
    both set ``pending``, which is idempotent.
    """
    stmt = select(OutboxEvent).where(OutboxEvent.status == OutboxStatus.FAILED)
    if event_ids is not None:
        stmt = stmt.where(OutboxEvent.id.in_(list(event_ids)))

    rows = list(session.execute(stmt.order_by(OutboxEvent.id).limit(limit)).scalars())

    replays = [
        DeadLetterReplay(
            id=event.id,
            event_type=event.event_type,
            tenant_id=str(event.tenant_id),
            attempts=event.attempts,
            last_error=event.last_error,
        )
        for event in rows
    ]

    if dry_run or not rows:
        return replays

    now = truncate_to_second(utc_now())
    for event in rows:
        event.status = OutboxStatus.PENDING
        event.attempts = 0
        event.next_attempt_at = now
        event.locked_by = None
        event.locked_until = None
        event.last_error = None
    session.flush()
    return replays
