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

from datetime import date, datetime

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
