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

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

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

#: Default number of settled rows one prune removes. Larger than the replay bound
#: because a prune is doing housekeeping rather than asking a human to read every
#: row: a deployment that has never pruned has thousands of settled rows, and a
#: hundred-at-a-time tool would need a loop to be useful. Still bounded, for the
#: same reason — one statement must not try to delete a table.
DEFAULT_PRUNE_LIMIT = 1000


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
        """Unpublished rows, whether or not they are due yet.

        Not the backlog gauge's query — see :func:`outbox_pending_overdue_count`, which
        excludes rows still serving out a retry backoff. This one answers "how many
        rows are unpublished", which a test wants and a threshold does not: a row
        waiting four minutes of backoff is unpublished and healthy.
        """
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


def outbox_pending_overdue_count(
    session: Session, *, grace: timedelta, now: datetime | None = None
) -> int:
    """Rows that have been claimable for longer than ``grace`` and are still here.

    This is the one number that says "the relay is not draining the outbox", and
    nothing in the deployment currently says it: the relay's own listener answers
    "did this process start?" (``D10-117``), and a relay that is not running writes
    no family at all — which is indistinguishable, from Prometheus' side, from a
    target that was never scraped. An outside observer reading the table is the
    only reading that survives the observer's failure, so the attention watcher
    publishes this rather than the relay.

    **Why a grace, and why the lease.** ``claim_due`` takes a row within one poll
    interval (0.5 s by default), so a row that has sat claimable for minutes is
    evidence rather than jitter. Backoff is the reason a plain count is wrong:
    ``_mark_failed`` pushes ``next_attempt_at`` into the future, so a row mid-retry
    is unpublished *and healthy*, and counting it would make the gauge non-zero
    during any broker hiccup — the alarm-nobody-reads shape ``D10-39`` warns about.
    Filtering on the timestamp excludes that population without a second setting,
    and reusing ``OUTBOX_LEASE_SECONDS`` as the grace means the deployment has one
    statement of "how long the relay may take" instead of two that could be
    configured into the wrong order. A ``publishing`` row is counted once its
    **lease** has run out and no grace on top, because the lease is already that
    statement: ``claim_due`` would have reclaimed the row by then. Charging both
    would hold a stalled claim quiet for two leases (four minutes at the shipped
    defaults) while saying "one lease" in one place and "two" in another.

    Unscoped like :func:`dead_letter_count`: a stalled relay is not a tenant's.
    """
    moment = utc_now() if now is None else now
    claimable = moment - grace
    overdue = or_(
        and_(
            OutboxEvent.status == OutboxStatus.PENDING,
            OutboxEvent.next_attempt_at <= claimable,
        ),
        and_(
            OutboxEvent.status == OutboxStatus.PUBLISHING,
            OutboxEvent.locked_until.is_not(None),
            OutboxEvent.locked_until <= moment,
        ),
    )
    stmt = select(func.count()).select_from(OutboxEvent).where(overdue)
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


# --- retention (operator operations) --------------------------------------
# The outbox has been append-only for its whole life, which is right for a
# delivery record and wrong for a table: every published row and every parked row
# stays forever. ``D10-131`` chose to *park* events enqueued while the bus is off
# rather than discard them at write time, so an installation running the default
# ``none`` transport accumulates one parked row per business event and has no way
# to give any of them back. That is the retention half of gap 25, and it is what
# this section answers. Like replay it is a module-level function: the relay's
# claim/fence machinery is not what a retention pass is, and a prune has no worker
# identity or lease to supply.


@dataclass(frozen=True)
class PrunedEvent:
    """One settled row being removed, described before it stops existing.

    The same reasoning as :class:`DeadLetterReplay`: once the ``DELETE`` runs there
    is nothing left to report, so the operator's log has to be fed from the rows as
    they were read. ``tenant_id`` and ``event_type`` are here because "how much of
    which tenant's history did I just delete" is the only question this report
    exists to answer.
    """

    id: int
    tenant_id: str
    event_type: str
    status: str


def _settled_before(before: datetime, *, include_failed: bool) -> ColumnElement[bool]:
    """The predicate for "this row's work finished before ``before``".

    Two branches, because the two settled states record when they settled
    differently, and both are honest rather than exact:

    * **published** has ``published_at`` — the column that literally means "this
      row's job was done at". Exact, and it is why the branch can be trusted to
      delete: a row published moments ago is not old however long ago it was
      enqueued, which is why ``created_at`` would be the wrong column here.
    * **failed** has no terminal timestamp, so the cutoff uses ``updated_at``,
      which the ``onupdate`` hook moves on the write that parked the row. That is
      the parking instant for a row parked where it stood; for a row parked by
      ``claim_due`` on its way past the attempt ceiling it is also the last write.
      Either way it is the closest thing the schema has, and the schema is not
      being extended for a housekeeping command.

    ``pending`` and ``publishing`` are absent on purpose and are absent **by
    predicate**, not by a caller remembering to filter: a retention pass that could
    delete an unpublished row would lose an event, which is the one thing the
    outbox exists to make impossible.
    """
    branches = [
        and_(
            OutboxEvent.status == OutboxStatus.PUBLISHED,
            OutboxEvent.published_at.is_not(None),
            OutboxEvent.published_at <= before,
        )
    ]
    if include_failed:
        branches.append(
            and_(
                OutboxEvent.status == OutboxStatus.FAILED,
                OutboxEvent.updated_at <= before,
            )
        )
    return or_(*branches)


def prune_terminal_before(
    session: Session,
    *,
    before: datetime,
    limit: int = DEFAULT_PRUNE_LIMIT,
    include_failed: bool = False,
    dry_run: bool = False,
) -> list[PrunedEvent]:
    """Delete settled rows older than ``before``; return what was removed.

    ``before`` is mandatory and has no default. That is the whole design: "how long
    do we keep published events" is a retention **policy**, and a policy chosen by
    this function's default would be a decision made by whichever engineer wrote
    last. An operator states the cutoff per invocation instead, exactly as
    ``--ids`` lets an operator state which parked rows to replay, and the deployment
    inherits no invented promise about how much history exists.

    ``include_failed`` is off by default so the destructive half is a second,
    deliberate keystroke. A parked row is a row a human was *supposed* to look at
    (``D7-01``: dead letter with replay, never log-and-drop), and pruning it ends
    that possibility permanently. Passed, it means the operator has decided the
    parks are explained and past keeping — which for a ``none``-transport
    deployment is exactly the case ``D10-131`` left open.

    ``dry_run`` reads the same rows and returns them without deleting, so an
    operator can count before committing. The ``DELETE`` re-states the status
    predicate as well as the ids: a row cannot be published and re-parked between
    the two statements, but re-asserting the guard inside the write costs nothing
    and means the destructive statement is safe to read on its own.

    No ``SELECT ... FOR UPDATE`` and no lease: settled rows are never claimed by the
    relay again (``claim_due`` reads only ``pending`` and ``publishing``), so there
    is no concurrent writer to fence off. Two simultaneous prunes would simply
    delete disjoint id sets, or one would find its rows already gone.

    A ``limit`` of zero or less returns nothing instead of being handed to the
    database: a negative ``LIMIT`` means *unbounded* to SQLite and MySQL, so passing
    one straight through would turn the caller's cap into the opposite of a cap.
    The same guard ``claim_reminder_candidates`` uses. The CLI refuses a non-positive
    ``--limit`` before it gets here; this is for every other caller.
    """
    if limit <= 0:
        return []

    stmt = (
        select(OutboxEvent)
        .where(_settled_before(before, include_failed=include_failed))
        .order_by(OutboxEvent.id)
        .limit(limit)
    )
    rows = list(session.execute(stmt).scalars())

    pruned = [
        PrunedEvent(
            id=row.id,
            tenant_id=str(row.tenant_id),
            event_type=row.event_type,
            status=str(row.status),
        )
        for row in rows
    ]

    if dry_run or not rows:
        return pruned

    session.execute(
        delete(OutboxEvent).where(
            OutboxEvent.id.in_([row.id for row in rows]),
            _settled_before(before, include_failed=include_failed),
        )
    )
    session.flush()
    return pruned
