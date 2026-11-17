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
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

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


#: Every channel a ``pending`` row can be stuck on, in the enum's declaration order.
#:
#: Named once because two things fill their zeros from it — the two counts below and
#: the ``notifications_pending`` gauge in :mod:`app.core.metrics` — and a channel
#: missing from one of them would read, from Prometheus' side, as a channel that is
#: never stuck rather than one nobody counted.
PENDING_CHANNELS: tuple[Channel, ...] = tuple(Channel)


def _overdue_pending(grace: timedelta, now: datetime | None) -> ColumnElement[bool]:
    """The predicate for "due, still ``pending``, and due for longer than ``grace``".

    Deliberately built from the same two columns as :func:`claim_next_due` and with
    the same reading of them, so the reader that counts stuck rows and the scheduler
    that sends them cannot disagree about which rows are due: a row is due once
    ``scheduled_at`` has passed, and a row with no ``scheduled_at`` is due the moment
    it exists.

    The grace is where the two differ, and it is not padding. ``claim_next_due`` asks
    "may I send this *now*"; this asks "should somebody already have sent this", and
    between those two questions sits every row that is merely waiting for the next
    five-second tick. A ``scheduled_at`` in the future — the quiet-hours deferral
    (``dispatch.quiet_hours_until``) — is not due at all and so never reaches here:
    deferral is correct behaviour, and a check that alerted on it would page every
    deployment at two in the morning.

    For a row with no due time the age that matters is its own, so ``created_at``
    stands in. Reading ``scheduled_at`` alone there would compare NULL, match nothing,
    and under-report exactly the rows the producer created for immediate send.
    """
    cutoff = (utc_now() if now is None else now) - grace
    return and_(
        Notification.status == NotificationStatus.PENDING,
        or_(
            and_(
                Notification.scheduled_at.is_not(None),
                Notification.scheduled_at <= cutoff,
            ),
            and_(
                Notification.scheduled_at.is_(None),
                Notification.created_at <= cutoff,
            ),
        ),
    )


def pending_overdue_counts(
    session: Session, *, grace: timedelta, now: datetime | None = None
) -> dict[Channel, int]:
    """Count rows that are due and still ``pending`` past ``grace``, **unscoped**.

    The population this table had no reading for. A row leaves ``pending`` in one of
    three ways and only two of them are visible anywhere else: it is sent, or it parks
    as ``failed``/``suppressed`` — both counted by :func:`undelivered_counts`. The
    third is that nothing ever claims it, and on the shipped configuration that is
    *every* reminder: with ``NOTIFICATION_TRANSPORT=none`` the transport raises
    ``SENDER_UNAVAILABLE`` before ``attempts`` is incremented, so the row cannot reach
    the retry cap, never turns ``failed``, and stays ``pending`` for ever. The
    attention check therefore reported a clean board while a queue of customer
    reminders sat behind it, unsendable and uncounted.

    Unscoped for the reason :func:`undelivered_counts` gives: this backs an operator
    check with no principal, and scoping it would report a clean board while another
    organisation's queue sat stuck.

    Every channel is **always** present in the result, zero-filled, for the same
    reason both statuses are in the sibling: a missing key would force every caller to
    tell "no such row" from "no rows of that channel", and the natural slip — ``.get``
    in one place, a loop over ``.items()`` in another — is how a channel quietly stops
    being reported.

    It returns the count **per channel and nothing else**. Whether a stuck row on that
    channel is explained by configuration or is a fault is a question about the
    deployment's transports, not about the table, and it is answered at the one place
    that holds the settings: :func:`app.core.attention.attention_report`.
    """
    stmt = (
        select(Notification.channel, func.count())
        .where(_overdue_pending(grace, now))
        .group_by(Notification.channel)
    )
    counts: dict[Channel, int] = dict.fromkeys(PENDING_CHANNELS, 0)
    for channel, total in session.execute(stmt).all():
        counts[channel] = int(total)
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


#: Deliberately **no** tenant-scoped twin of :func:`pending_overdue_counts`.
#:
#: Its two siblings have one each because an operator reading a count and a tenant
#: reading the same count answer different questions. This one does not: all three
#: buckets it feeds are statements about what the *deployment* can send, so there is
#: nothing a tenant could do with their own version of the number — and the population
#: is not hidden from them anyway, because the notification queue screen lists their
#: ``pending`` rows by name. An unread query written "for symmetry" is how a module
#: grows a second answer that nobody checks; see ``D10-129`` and
#: :class:`app.core.attention.TenantAttentionReport`.
