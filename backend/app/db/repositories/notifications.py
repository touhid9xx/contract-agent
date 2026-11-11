"""Notification persistence (F10).

``Notification`` has been in the schema since F2 and, like ``AgentDecision`` before
F9 and ``AuditLog`` before F8, had **no writer**: the table described what the
system intended to store rather than what it did. This module is the writer.

Two shapes worth stating, both inherited from :mod:`app.db.repositories.agent`:

* **Notifications are state, not just history.** Unlike a decision row, a
  notification is updated in place — ``attempts``, ``last_error``, ``status``,
  ``sent_at`` — because a send is one attempt against one slot, not an append-only
  record of reasoning. The append-only history lives in ``AuditLog`` and in the
  transport's own provider log; what a reader needs from this table is *where the
  slot currently stands*.
* **Reads are tenant-scoped, writes are tenant-stamped.** Both come from
  :class:`~app.db.repositories.base.TenantScopedRepository`, so a contract id
  guessed from another organisation returns an empty list rather than somebody
  else's outbound messages.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.enums import Channel, NotificationStatus
from app.db.models import Notification
from app.db.repositories.base import TenantScopedRepository

#: How many notifications a listing returns when the caller does not say. Bounded
#: at the query so no caller can ask for a whole tenant's delivery table.
DEFAULT_NOTIFICATION_LIMIT = 50
MAX_NOTIFICATION_LIMIT = 200


class NotificationRepository(TenantScopedRepository[Notification]):
    """Tenant-scoped access to one organisation's outbound notifications."""

    def for_contract(
        self, contract_id: int, *, limit: int = DEFAULT_NOTIFICATION_LIMIT
    ) -> list[Notification]:
        """One contract's notifications, newest scheduled first.

        ``contract_id`` is not trusted on its own: the tenant predicate applies as
        well, so a guessed id from another tenant yields an empty list (IDOR/BOLA).

        The ordering is ``(scheduled_date, id)`` descending rather than
        ``created_at``: what a reader wants from this table is "the most recent
        slot", and two rows created in the same second must still come back in a
        deterministic order — the same tiebreak argument F9 made for decisions.
        """
        stmt = (
            self.scoped(Notification)
            .where(Notification.contract_id == contract_id)
            .order_by(Notification.scheduled_date.desc(), Notification.id.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars())

    def recent(
        self,
        *,
        limit: int = DEFAULT_NOTIFICATION_LIMIT,
        status: NotificationStatus | None = None,
        channel: Channel | None = None,
    ) -> list[Notification]:
        """The newest notifications across this tenant, with optional filters.

        ``status`` and ``channel`` filter *after* the tenant predicate and never
        replace it: the filters are caller-supplied query parameters, and adding one
        must not be a way to widen the scope.
        """
        stmt = self.scoped(Notification)
        if status is not None:
            stmt = stmt.where(Notification.status == status)
        if channel is not None:
            stmt = stmt.where(Notification.channel == channel)
        stmt = stmt.order_by(
            Notification.scheduled_date.desc(),
            Notification.id.desc(),
        ).limit(limit)
        return list(self.session.execute(stmt).scalars())

    def find_slot(
        self, *, contract_id: int, template: str, scheduled_date: date
    ) -> Notification | None:
        """The row occupying one milestone slot, if any.

        This is the read side of the idempotency guard. The unique constraint on
        ``(contract_id, template, scheduled_date)`` is what actually prevents a
        duplicate; this lookup exists so the route can answer 409 with the *existing*
        row's id instead of a driver-level integrity error.
        """
        stmt = self.scoped(Notification).where(
            Notification.contract_id == contract_id,
            Notification.template == template,
            Notification.scheduled_date == scheduled_date,
        )
        return self.session.execute(stmt).scalars().first()

    def claim_for_dispatch(self, notification_id: int) -> Notification | None:
        """Read one row *under a write lock*, for the dispatch path.

        Optimistic locking alone is not enough here, and the distinction matters:
        :attr:`~app.db.models.delivery.Notification.version` stops two workers
        clobbering each other's *row state*, but both would still have reached the
        transport before either committed. The customer would get two emails and the
        database would report one send.

        Taking the lock before the policy runs closes that. On MySQL this is
        ``SELECT ... FOR UPDATE``: the second worker blocks until the first commits,
        then its locking read returns the *updated* row — now ``sent`` — so the
        policy refuses it with ``NOT_PENDING`` instead of sending again.

        ``with_for_update`` degrades to a no-op on SQLite, which is correct rather
        than merely convenient: SQLite serialises writers at the file level, so
        there is no concurrent writer to lock out.

        Still tenant-scoped: ``notification_id`` is a caller-supplied path parameter,
        so the lock must not become a way to reach another tenant's row.
        """
        stmt = self.scoped(Notification).where(Notification.id == notification_id).with_for_update()
        return self.session.execute(stmt).scalar_one_or_none()

    def create(
        self,
        *,
        contract_id: int,
        channel: Channel,
        template: str,
        scheduled_date: date,
        scheduled_at: datetime | None,
        idempotency_key: str,
    ) -> Notification:
        """Insert one pending notification.

        ``status`` is not an argument: a freshly scheduled row is always ``pending``
        and letting a caller choose would allow inserting a row that claims to have
        been sent. ``attempts`` starts at zero for the same reason.
        """
        notification = Notification(
            contract_id=contract_id,
            channel=channel,
            template=template,
            status=NotificationStatus.PENDING,
            scheduled_date=scheduled_date,
            scheduled_at=scheduled_at,
            idempotency_key=idempotency_key,
            attempts=0,
        )
        return self.add(notification)


def claim_next_due(session: Session, *, now: datetime | None = None) -> Notification | None:
    """The next row the scheduler may dispatch, **read under a write lock**.

    The one query in this module that is *not* tenant-scoped, and the reason is
    structural rather than convenient: a scheduler is one process serving every
    tenant, so it cannot know the tenant before it has picked the row. The tenant is
    therefore taken **from the row** — ``dispatch_notification`` is then handed that
    row's own ``tenant_id`` for every follow-up read, and the contract's tenant is
    checked against it before the policy runs (see
    :mod:`app.workers.notification_scheduler`). :func:`~app.db.repositories.outbox.
    claim_due` is unscoped for exactly the same reason.

    "Due" is ``pending`` and either no due time or one that has passed. A row
    deferred by quiet hours carries a future ``scheduled_at``, so this predicate is
    also what makes a deferral *resume* — nothing else has to remember it.

    ``SKIP LOCKED`` lets N schedulers share the table without blocking. SQLite has no
    ``FOR UPDATE``; SQLAlchemy omits the clause there, which is acceptable because the
    suite is single-writer and the real locking is covered by the MySQL integration
    test — the same trade-off ``claim_for_dispatch`` documents.
    """
    due = and_(
        Notification.status == NotificationStatus.PENDING,
        or_(Notification.scheduled_at.is_(None), Notification.scheduled_at <= (now or utc_now())),
    )
    stmt = (
        select(Notification)
        .where(due)
        # NULLs sort first on both MySQL and SQLite, so "no due time" (send now) is
        # preferred over every dated row — which is the order an operator expects.
        .order_by(Notification.scheduled_at, Notification.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return session.execute(stmt).scalars().first()


#: The statuses that mean "this row stopped without reaching the customer".
#: ``failed`` exhausted its retry budget; ``suppressed`` was refused by a policy
#: gate. Both need a human, and both currently sit silent — that is gap 22's
#: residue, and naming the set once here is what makes the attention check and
#: any future alarm agree about which rows count as undelivered.
UNDELIVERED_STATUSES: tuple[NotificationStatus, ...] = (
    NotificationStatus.FAILED,
    NotificationStatus.SUPPRESSED,
)


def undelivered_counts(session: Session) -> dict[NotificationStatus, int]:
    """Count rows that stopped without being delivered, **unscoped**.

    The second unscoped query in this module, for the reason :func:`claim_next_due`
    already gives: an attention check is run by an operator or a cron entry, serves
    every tenant at once, and has no principal to be scoped to. Scoping it to one
    tenant would be actively misleading — it would report a clean board while
    another organisation's sends sat parked.

    Both statuses are **always** present in the result, zero-filled. A missing key
    would force every caller to distinguish "no such row" from "no rows of that
    status", and the natural slip — ``.get(status, 0)`` in one place and a loop
    over ``.items()`` in another — is how one status quietly stops being reported.

    No row content is read: a count is all an alarm needs, and a count cannot leak
    a recipient.
    """
    stmt = (
        select(Notification.status, func.count())
        .where(Notification.status.in_(UNDELIVERED_STATUSES))
        .group_by(Notification.status)
    )
    counts: dict[NotificationStatus, int] = dict.fromkeys(UNDELIVERED_STATUSES, 0)
    for status, total in session.execute(stmt).all():
        counts[status] = int(total)
    return counts


def undelivered_counts_for_tenant(
    session: Session, tenant_id: uuid.UUID
) -> dict[NotificationStatus, int]:
    """Count **one organisation's** undelivered rows.

    The tenant-scoped twin of :func:`undelivered_counts`, and it is written beside
    it rather than replacing it because the two answer different questions:

    * :func:`undelivered_counts` backs ``python -m app.cli doctor`` and the
      scheduled attention watch. Neither runs behind a request, so neither has a
      principal, and both must see the whole board.
    * this one runs inside an authenticated request, where the tenant is known from
      the token. Reading another organisation's count there would be a cross-tenant
      disclosure, and the caller could do nothing with it anyway.

    **It does not contradict the sibling's docstring.** That sentence — scoping a
    count "would be actively misleading ... it would report a clean board while
    another organisation's sends sat parked" — is an argument about the *operator's*
    board, whose whole job is to describe every tenant at once. It stays true of
    that board. A tenant asking about their own queue is not the reader it is
    about, so the two readings coexist without either being narrowed.

    ``ix_notifications_tenant_status`` leads on ``tenant_id``, so this is an index
    scan over one organisation's rows rather than a filter over the table.

    Both statuses are always present and zero-filled, for the reason the sibling
    gives; no row content is read, so no count can leak a recipient.
    """
    stmt = (
        select(Notification.status, func.count())
        .where(
            Notification.tenant_id == tenant_id,
            Notification.status.in_(UNDELIVERED_STATUSES),
        )
        .group_by(Notification.status)
    )
    counts: dict[NotificationStatus, int] = dict.fromkeys(UNDELIVERED_STATUSES, 0)
    for status, total in session.execute(stmt).all():
        counts[status] = int(total)
    return counts
