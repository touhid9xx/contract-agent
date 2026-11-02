"""Contract persistence for the ingestion path.

Only what F6 step 1 needs lives here: the per-tenant ``sha256`` lookup that makes
ingestion idempotent, and a create that cannot aim a row at another organisation
(``TenantScopedRepository.add`` stamps ``tenant_id`` from the repository, so there
is no argument here that could point the write elsewhere).

Two model fields are deliberately **not** settable from this path:

``status``
    A contract always enters the system ``active``. Letting a caller post
    ``expired`` or ``responded`` would let it skip the reminder pipeline and the
    response-tracking state machine that are the point of the product.

``risk``
    Risk is an output of the agent decision, not an input. It stays at the model
    default until something computes it, and a client cannot pre-set its own
    urgency.
"""

from __future__ import annotations

import uuid
from datetime import date

from app.db.enums import ContractSource
from app.db.models import Contract
from app.db.repositories.base import TenantScopedRepository


class ContractRepository(TenantScopedRepository[Contract]):
    """Tenant-scoped access to the contracts of one organisation."""

    def by_sha256(self, sha256: str) -> Contract | None:
        """The contract with this content fingerprint **in this tenant**, or ``None``.

        Unique per tenant, not globally: the same file ingested by two tenants is
        two independent contracts (``uq_contracts_tenant_sha256``).
        """
        stmt = self.scoped(Contract).where(Contract.sha256 == sha256)
        return self.session.execute(stmt).scalar_one_or_none()

    def create(
        self,
        *,
        customer_ref: str,
        sha256: str,
        source: ContractSource,
        start_date: date | None,
        end_date: date | None,
        timezone: str,
        created_by: uuid.UUID | None,
    ) -> Contract:
        """Add one ingested contract to this tenant.

        The row is **not flushed** here: the caller owns the transaction and
        decides when the primary key is needed (the ingestion route flushes so the
        outbox event can carry the contract id as its partition key). Flushing in
        the repository would hide that ordering from the caller, and the ordering
        is the whole point of writing the event in the same transaction.
        """
        contract = Contract(
            customer_ref=customer_ref,
            sha256=sha256,
            source=source,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone,
            created_by=created_by,
        )
        return self.add(contract)
