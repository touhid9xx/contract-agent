"""Escalation queue and customer-response persistence (gap 15).

Both tables were created by ``0001`` and stayed empty until this module: the
flow ran as far as "a reminder was sent" and stopped, because nothing recorded
a reply and nothing opened the clock that says a human is late.

Two rules, both inherited from :class:`~app.db.repositories.base.TenantScopedRepository`
and worth stating because they are the point of writing this as a repository
rather than inline queries:

1. **Tenant-scoped.** Every query here goes through ``scoped()``, so another
   tenant's escalation answers 404 instead of leaking its existence.
2. **No commits.** Like the refresh-token and login-attempt repositories, these
   take the caller's session and never commit — the route owns the transaction,
   because raising an escalation also moves the *contract's* status and writes an
   audit row, and those have to be one transaction or none of them.

``create`` flushes on purpose. The session factory is built with
``autoflush=False`` (see ``app.db.session``), a lesson learned the hard way when
a refresh-token row had no id to audit against; the audit row needs
``escalation.id``, so the flush belongs here rather than being left to whichever
caller remembers.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Exists, and_, exists, select
from sqlalchemy.orm import Session

from app.db.enums import Channel, ContractStatus, EscalationStatus, NotificationStatus, ResponseType
from app.db.models import Contract, CustomerResponse, Escalation, Notification
from app.db.repositories.base import TenantScopedRepository

__all__ = [
    "CONTACTED_STATUSES",
    "DEFAULT_ESCALATION_QUEUE_LIMIT",
    "DEFAULT_RESPONSE_HISTORY_LIMIT",
    "MAX_ESCALATION_QUEUE_LIMIT",
    "MAX_RESPONSE_HISTORY_LIMIT",
    "OPEN_ESCALATION_STATUSES",
    "SCANNABLE_CONTRACT_STATUSES",
    "CustomerResponseRepository",
    "EscalationRepository",
    "claim_silent_contracts",
]

#: Rows one queue page returns. Bounded like every other list in the repo, so an
#: operator with a backlog cannot turn a page load into an unbounded scan.
DEFAULT_ESCALATION_QUEUE_LIMIT = 50
MAX_ESCALATION_QUEUE_LIMIT = 200

#: Responses shown per contract. Short on purpose: this is "did they answer?", not
#: a mailbox. The full history stays in the table for an auditor.
DEFAULT_RESPONSE_HISTORY_LIMIT = 20
MAX_RESPONSE_HISTORY_LIMIT = 100

#: Mirrors :data:`app.workers.escalation.OPEN_ESCALATION_STATUSES` for the query
#: layer. Duplicated rather than imported because importing a worker into a
#: repository would invert the dependency direction the repo keeps (routes and
#: workers depend on repositories, never the reverse); ``test_escalations.py``
#: asserts the two tuples are equal so the copy cannot drift silently.
OPEN_ESCALATION_STATUSES: tuple[EscalationStatus, ...] = (
    EscalationStatus.OPEN,
    EscalationStatus.IN_PROGRESS,
    EscalationStatus.SLA_BREACHED,
)

#: ``Notification.status`` values that mean the transport accepted the row.
#:
#: Expressed by exclusion — everything past ``pending`` that is not one of the two
#: failure states — because those are the only two ways a row stops without
#: reaching anybody. ``machine`` counts as contact on purpose: a voicemail reached
#: a mailbox even though the spec is explicit that it is *not* a response, which is
#: precisely the situation an escalation exists for.
CONTACTED_STATUSES: tuple[NotificationStatus, ...] = (
    NotificationStatus.SENT,
    NotificationStatus.DELIVERED,
    NotificationStatus.ANSWERED,
    NotificationStatus.MACHINE,
)

#: Contract states the scanner may escalate *from*.
#:
#: ``responded`` is excluded because the customer answered. ``renewed``/``expired``
#: are excluded because the file is closed
#: (:data:`app.workers.escalation.CLOSED_CONTRACT_STATUSES`). ``escalated`` is
#: excluded for a less obvious reason: ``DECISIONS.md`` D10-20 makes an operator
#: resolving an escalation by hand leave the contract in that state, so re-opening
#: one automatically would undo a decision a person deliberately made. A contract
#: that has to be chased again gets a new escalation from a person, not from a clock.
SCANNABLE_CONTRACT_STATUSES: tuple[ContractStatus, ...] = (
    ContractStatus.ACTIVE,
    ContractStatus.REMINDER_SENT,
)


class EscalationRepository(TenantScopedRepository[Escalation]):
    """Tenant-scoped reads and the single writer for ``escalations``."""

    def by_id(self, escalation_id: int) -> Escalation | None:
        """One escalation, or ``None`` when it belongs to another tenant."""
        return self.get(Escalation, escalation_id)

    def open_for_contract(self, contract_id: int) -> Escalation | None:
        """The escalation still in flight for ``contract_id``, newest first.

        Newest-first because the row that matters is the one most recently
        raised; a contract cannot have two open escalations (the route refuses
        the second), so the ordering is belt-and-braces against a row a previous
        version left behind rather than a routine tie-break.
        """
        stmt = (
            self.scoped(Escalation)
            .where(Escalation.contract_id == contract_id)
            .where(Escalation.status.in_(list(OPEN_ESCALATION_STATUSES)))
            .order_by(Escalation.id.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def queue(
        self,
        *,
        limit: int,
        statuses: Sequence[EscalationStatus] | None = None,
        contract_id: int | None = None,
    ) -> list[Escalation]:
        """The oldest deadline first, which is also the most urgent row first.

        Ordering by ``sla_deadline`` rather than ``created_at`` is deliberate: an
        operator works the queue in the order the SLA will punish them, and two
        escalations raised minutes apart can already have different deadlines if
        the SLA is changed between them. The same order reads correctly when
        ``contract_id`` narrows the result to one contract's history, because a
        contract's escalations are raised one after another: oldest deadline first
        is oldest escalation first.

        ``contract_id`` narrows the queue, it does not replace the tenant
        predicate — the filter is applied *after* ``scoped()``, so another tenant's
        id yields an empty list rather than their rows, and cannot be used to probe
        which ids exist. It lives here rather than in a second ``for_contract``
        method so "most urgent first" is decided in exactly one place; a copy would
        be free to drift from this one.
        """
        stmt = self.scoped(Escalation)
        if statuses is not None:
            stmt = stmt.where(Escalation.status.in_(list(statuses)))
        if contract_id is not None:
            stmt = stmt.where(Escalation.contract_id == contract_id)
        stmt = stmt.order_by(Escalation.sla_deadline.asc(), Escalation.id.asc()).limit(limit)
        return list(self.session.execute(stmt).scalars())

    def create(
        self,
        *,
        contract_id: int,
        correlation_id: str,
        deadline: datetime,
        status: EscalationStatus = EscalationStatus.OPEN,
    ) -> Escalation:
        """Open an escalation for ``contract_id`` and flush so it has an id."""
        row = Escalation(
            tenant_id=self.tenant_id,
            contract_id=contract_id,
            correlation_id=correlation_id,
            sla_deadline=deadline,
            status=status,
        )
        self.add(row)
        self.session.flush()
        return row


class CustomerResponseRepository(TenantScopedRepository[CustomerResponse]):
    """Tenant-scoped reads and the single writer for ``customer_responses``."""

    def for_contract(
        self,
        contract_id: int,
        *,
        limit: int = DEFAULT_RESPONSE_HISTORY_LIMIT,
    ) -> list[CustomerResponse]:
        """Responses for one contract, newest first."""
        stmt = (
            self.scoped(CustomerResponse)
            .where(CustomerResponse.contract_id == contract_id)
            .order_by(CustomerResponse.received_at.desc(), CustomerResponse.id.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars())

    def latest_for_contract(self, contract_id: int) -> CustomerResponse | None:
        """The most recent response, or ``None`` if the customer never answered."""
        stmt = (
            self.scoped(CustomerResponse)
            .where(CustomerResponse.contract_id == contract_id)
            .order_by(CustomerResponse.received_at.desc(), CustomerResponse.id.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def create(
        self,
        *,
        contract_id: int,
        correlation_id: str,
        channel: Channel,
        response_type: ResponseType,
        received_at: datetime,
        metadata: dict[str, Any] | None = None,
    ) -> CustomerResponse:
        """Record one customer response and flush so it has an id.

        ``received_at`` is the *customer's* clock (when they replied), not the
        row's ``created_at`` (when an operator typed it in). Keeping both is what
        lets a later slice measure response time without guessing.
        """
        row = CustomerResponse(
            tenant_id=self.tenant_id,
            contract_id=contract_id,
            correlation_id=correlation_id,
            channel=channel,
            response_type=response_type,
            received_at=received_at,
            response_metadata=json.dumps(metadata, sort_keys=True) if metadata else None,
        )
        self.add(row)
        self.session.flush()
        return row


def claim_silent_contracts(
    session: Session,
    *,
    cutoff: datetime,
    limit: int,
) -> list[Contract]:
    """Contracts that have gone quiet since ``cutoff``, **read under a write lock**.

    This is the third unscoped query in the codebase, and it is unscoped for the
    same structural reason as :func:`~app.db.repositories.notifications.
    claim_next_due` and :func:`~app.db.repositories.outbox.claim_due`: the scanner
    is one process serving every tenant, so it cannot name a tenant before it has
    picked a row. It also returns the rows themselves rather than bare ids, so the
    caller can read each row's ``tenant_id`` and scope every follow-up query by it.

    "Silent" is four conditions, and each one is a distinct reason a contract must
    not be escalated:

    * it was **contacted** — at least one notification the transport accepted — and
      the last such contact is older than ``cutoff``;
    * nothing has been accepted **since** ``cutoff``, so a contract whose reminders
      are still going out is not escalated halfway through its own sequence;
    * it has **not replied** — any row in ``customer_responses`` takes it out;
    * it has **no open escalation**, so a contract already in the queue is not
      entered twice.

    ``sla_breached`` is in :data:`OPEN_ESCALATION_STATUSES` on purpose: a row
    already marked late is still somebody's job, and re-opening it would reset a
    deadline that has demonstrably already been missed.

    The two "contacted" clauses are written as a pair of ``EXISTS`` rather than one
    ``MAX(sent_at) < cutoff`` subquery in the select list. They mean the same thing
    and read the same way, but only this shape keeps the statement to a single
    table in ``FOR UPDATE``, which is what makes ``SKIP LOCKED`` safe to reason
    about: the lock is taken on the contract rows the caller is about to mutate.
    """

    def _contacted(instant: ColumnElement[bool]) -> Exists:
        """One accepted contact for this contract, at ``instant``.

        Shared by both clauses so the pair cannot drift into disagreeing about
        which notification statuses count — the failure that would let a contract
        be escalated while its own reminder sequence was still running.
        """
        return exists().where(
            and_(
                Notification.contract_id == Contract.id,
                Notification.status.in_(list(CONTACTED_STATUSES)),
                Notification.sent_at.is_not(None),
                instant,
            )
        )

    contacted_before_cutoff = _contacted(Notification.sent_at < cutoff)
    contacted_since_cutoff = _contacted(Notification.sent_at >= cutoff)
    replied = exists().where(CustomerResponse.contract_id == Contract.id)
    already_open = exists().where(
        and_(
            Escalation.contract_id == Contract.id,
            Escalation.status.in_(list(OPEN_ESCALATION_STATUSES)),
        )
    )

    stmt = (
        select(Contract)
        .where(
            Contract.status.in_(list(SCANNABLE_CONTRACT_STATUSES)),
            contacted_before_cutoff,
            ~contacted_since_cutoff,
            ~replied,
            ~already_open,
        )
        # Ordered by id rather than by how long the contract has been silent. The
        # batch drains in a single transaction, and a contract that was escalated
        # stops matching, so no row is starved; a stable order is worth more here
        # than an urgency order nobody reads, because it is what makes "the same
        # tick sees the same rows" true in a test.
        .order_by(Contract.id)
        .limit(limit)
        .with_for_update(skip_locked=True, of=Contract)
    )
    return list(session.execute(stmt).scalars())
