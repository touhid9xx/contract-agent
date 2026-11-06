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

from app.db.enums import Channel, EscalationStatus, ResponseType
from app.db.models import CustomerResponse, Escalation
from app.db.repositories.base import TenantScopedRepository

__all__ = [
    "DEFAULT_ESCALATION_QUEUE_LIMIT",
    "DEFAULT_RESPONSE_HISTORY_LIMIT",
    "MAX_ESCALATION_QUEUE_LIMIT",
    "MAX_RESPONSE_HISTORY_LIMIT",
    "OPEN_ESCALATION_STATUSES",
    "CustomerResponseRepository",
    "EscalationRepository",
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
    ) -> list[Escalation]:
        """The oldest deadline first, which is also the most urgent row first.

        Ordering by ``sla_deadline`` rather than ``created_at`` is deliberate: an
        operator works the queue in the order the SLA will punish them, and two
        escalations raised minutes apart can already have different deadlines if
        the SLA is changed between them.
        """
        stmt = self.scoped(Escalation)
        if statuses is not None:
            stmt = stmt.where(Escalation.status.in_(list(statuses)))
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
