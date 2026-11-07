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
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.db.enums import ContractSource, ContractStatus
from app.db.models import Contract
from app.db.repositories.base import TenantScopedRepository

#: The four consent columns, named once.
#:
#: Every path that reads or writes consent iterates this tuple: the update route,
#: the CLI import, and the audit snapshot that records what either of them did.
#: Adding a fourth channel flag then reaches all three at once, instead of
#: silently applying to two of them and leaving the audit trail describing an
#: older shape of the row.
CONSENT_FLAG_COLUMNS = (
    "consent_email",
    "consent_sms",
    "consent_voice",
    "do_not_contact",
)

#: How many contracts a listing returns when the caller does not say, and the
#: ceiling it is clamped to. Same shape as ``DEFAULT_DECISION_HISTORY_LIMIT`` and
#: for the same reason: the bound lives at the query, so no caller can ask for a
#: whole tenant's contract table in one response.
DEFAULT_CONTRACT_PAGE_LIMIT = 50
MAX_CONTRACT_PAGE_LIMIT = 200


class ContractRepository(TenantScopedRepository[Contract]):
    """Tenant-scoped access to the contracts of one organisation."""

    def by_sha256(self, sha256: str) -> Contract | None:
        """The contract with this content fingerprint **in this tenant**, or ``None``.

        Unique per tenant, not globally: the same file ingested by two tenants is
        two independent contracts (``uq_contracts_tenant_sha256``).
        """
        stmt = self.scoped(Contract).where(Contract.sha256 == sha256)
        return self.session.execute(stmt).scalar_one_or_none()

    def recent(
        self,
        *,
        limit: int = DEFAULT_CONTRACT_PAGE_LIMIT,
        status: ContractStatus | None = None,
        after: int | None = None,
    ) -> list[Contract]:
        """The tenant's contracts, newest first, as one keyset page.

        ``id`` is both the sort key and the cursor, which is not a shortcut: the
        column is autoincrement, so ``ORDER BY id DESC`` *is* insertion order and
        therefore newest-first. Using it instead of ``created_at`` avoids the tie
        that ``AgentDecisionRepository.recent`` has to break with a second column —
        ``created_at`` is a ``DATETIME`` without fractional seconds, so two
        contracts ingested in the same second store the same timestamp and
        "newest" would stop being a total order.

        ``after`` is the id of the last row of the previous page, so paging is
        ``WHERE id < after``: a scroll through the list touches each contract once
        and cannot skip or repeat a row when other rows are being ingested
        underneath it. That property is what ``OFFSET`` cannot give, and it is the
        same reason the review queue pages by sort key (D8-06).

        The cursor is a **position, not an authorisation**: it is only ever
        combined with the tenant predicate from :meth:`scoped`, so a guessed id
        from another organisation narrows this tenant's own rows rather than
        disclosing someone else's.
        """
        stmt = self.scoped(Contract)
        if status is not None:
            stmt = stmt.where(Contract.status == status)
        if after is not None:
            stmt = stmt.where(Contract.id < after)
        stmt = stmt.order_by(Contract.id.desc()).limit(limit)
        return list(self.session.execute(stmt).scalars())

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
        storage_path: str | None = None,
    ) -> Contract:
        """Add one ingested contract to this tenant.

        The row is **not flushed** here: the caller owns the transaction and
        decides when the primary key is needed (the ingestion route flushes so the
        outbox event can carry the contract id as its partition key). Flushing in
        the repository would hide that ordering from the caller, and the ordering
        is the whole point of writing the event in the same transaction.

        ``storage_path`` is set at create rather than in a follow-up update
        because a document upload produces both facts at once — the bytes have
        already been sealed to disk by the time this is called, and a second
        ``UPDATE`` would only add a window in which the row names no document it
        was created with. It stays optional so metadata-only ingestion
        (``POST /contracts``) is unchanged: a contract whose text never arrived is
        legitimate, and ``None`` is the honest way to say so.
        """
        contract = Contract(
            customer_ref=customer_ref,
            sha256=sha256,
            source=source,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone,
            storage_path=storage_path,
            created_by=created_by,
        )
        return self.add(contract)


def contract_for_operator(session: Session, contract_id: int) -> Contract | None:
    """Fetch a contract by id with **no** tenant predicate.

    This is the second audited exception to tenant scoping, and it exists for the
    same reason ``dead_lettered`` does: an operator running
    ``python -m app.cli extract`` or ``python -m app.cli consent-import`` has a
    database connection, not a principal, so there is no tenant for them to be
    scoped to. Keeping the bypass in one named function — rather than an unscoped
    ``select`` at the call site — is what makes it reviewable, and one function
    means the exceptions are countable.

    The contract's own ``tenant_id`` is then what scopes the *write*: extraction
    builds its repository from ``contract.tenant_id``, and the consent import
    writes its audit row through one, so this lookup can widen which contract an
    operator can see, but never which tenant a row lands in.
    """
    return session.execute(select(Contract).where(Contract.id == contract_id)).scalar_one_or_none()


def consent_state(contract: Contract) -> Mapping[str, Any]:
    """The consent picture of one contract, as a plain mapping.

    Used as the *before* and *after* image written to ``AuditLog`` and as the
    "did anything actually change?" comparison. Deliberately free of stored
    contact values: consent is a set of booleans and a version, so this mapping
    is safe to log and to persist verbatim.

    ``version`` rides along because the interesting question an auditor asks is
    not only what the flags were but which revision of the row said so.
    """
    state: dict[str, Any] = {column: getattr(contract, column) for column in CONSENT_FLAG_COLUMNS}
    state["version"] = contract.version
    return state


def consent_flags(contract: Contract) -> Mapping[str, bool]:
    """Only the four booleans, without ``version``.

    Separate from ``consent_state`` because the two answer different questions:
    ``consent_state`` records a revision, while equality here is what makes a
    re-submitted consent sheet a no-op rather than a write. Comparing the
    version too would turn every re-run into a "change".
    """
    return {column: bool(getattr(contract, column)) for column in CONSENT_FLAG_COLUMNS}


def apply_consent(
    contract: Contract,
    *,
    consent_email: bool,
    consent_sms: bool,
    consent_voice: bool,
    do_not_contact: bool,
    now: datetime | None = None,
) -> bool:
    """Set all four consent flags; report whether that changed anything.

    All four are required, for the same reason ``ConsentUpdateRequest`` requires
    them: a partial application would silently default the flags the caller did
    not mention, and for consent a silent default is a decision nobody made.

    Returns ``False`` when the flags already held these values, in which case
    ``consent_updated_at`` is left alone — the row's stated consent date stays
    the date consent last actually changed, not the date somebody re-sent it.
    """
    before = consent_flags(contract)
    contract.consent_email = consent_email
    contract.consent_sms = consent_sms
    contract.consent_voice = consent_voice
    contract.do_not_contact = do_not_contact
    if consent_flags(contract) == before:
        return False
    contract.consent_updated_at = now or utc_now()
    return True
