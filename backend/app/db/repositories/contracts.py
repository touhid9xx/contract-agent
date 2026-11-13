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

#: Contract states the reminder planner may plan a send *from*.
#:
#: Deliberately identical to :data:`app.db.repositories.escalations.
#: SCANNABLE_CONTRACT_STATUSES`, and duplicated rather than imported for the reason
#: that module already gives: a repository must not import another repository's
#: business set, because the two would then always move together even on the day one
#: of them should not. They answer different questions — "may this contract receive a
#: reminder?" versus "may this contract be escalated?" — and today's answer happens to
#: be the same pair. ``test_reminder_planner.py`` asserts the two tuples are equal, so
#: a divergence is a failing test rather than a silent one.
#:
#: ``escalated`` is absent for the reason D10-20 records: an operator resolving an
#: escalation by hand deliberately leaves the contract in that state, and a clock
#: that kept sending reminders afterwards would undo a person's decision.
REMINDABLE_CONTRACT_STATUSES: tuple[ContractStatus, ...] = (
    ContractStatus.ACTIVE,
    ContractStatus.REMINDER_SENT,
)

#: Contract states whose term has run out without anything recording it.
#:
#: The same pair as :data:`REMINDABLE_CONTRACT_STATUSES`, and a separate name for the
#: same reason that one is separate from the scanner's set: "may this contract
#: receive a reminder?" and "may a passing end date close this contract?" are
#: different questions that happen to share an answer today.
#:
#: ``responded`` is absent on purpose, even though its term still ends. A customer
#: answering is a person engaging with the file, and replacing that with a
#: clock-driven state would erase the reply while telling nobody anything the
#: response itself did not already say. ``escalated`` is absent for the reason
#: D10-20 records: the contract sits in that state because an operator resolved it by
#: hand, and a clock that overwrote it would undo a person's decision.
EXPIRABLE_CONTRACT_STATUSES: tuple[ContractStatus, ...] = (
    ContractStatus.ACTIVE,
    ContractStatus.REMINDER_SENT,
)


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


def claim_reminder_candidates(
    session: Session,
    *,
    earliest_end_date: date,
    latest_end_date: date,
    after_id: int = 0,
    limit: int,
) -> list[Contract]:
    """Lease the contracts whose expiry could be reminded today, **unscoped**.

    The tenant predicate is absent on purpose. The reminder planner is the system
    actor — it has a database connection and a clock, not a principal — so a
    tenant-scoped query could not see the contracts it exists to find. Keeping the
    bypass in this one named function, next to ``contract_for_operator``, is what
    keeps the exceptions to tenant scoping countable; the *write* it enables is
    still tenant-scoped, because the planner builds its repositories from each
    row's own ``tenant_id``.

    The predicate is ``status IN (...) AND end_date BETWEEN ? AND ?`` — exactly the
    shape ``ix_contracts_scan`` was declared for (see the comment on the model),
    which is why the window is expressed as bounds rather than as the date
    arithmetic that decides a milestone. The policy stays in
    :func:`app.workers.dispatch.milestone_for`; this query only has to be a
    **superset** of what that function would schedule, because a row this query
    over-selects costs one refused plan, while a row it under-selects is a reminder
    nobody ever sends.

    Two deliberate filters:

    ``do_not_contact``
        Removed here rather than in the worker. It cannot change with the clock —
        only a person or an inbound STOP can set it — so leaving it in would make
        the same rows occupy the batch on every tick forever. The remaining consent
        questions are *not* filtered: they are re-answered each tick by the policy,
        because approving a field or recording consent is what makes a blocked
        contract plannable.

    ``end_date IS NOT NULL``
        A contract with no end date has no milestone. ``build_notification`` would
        refuse it as ``MISSING_END_DATE``, but it would do so on every tick, so the
        scan does not ask.

    ``FOR UPDATE SKIP LOCKED`` (a no-op on SQLite) is what makes two planner
    replicas safe: they lease disjoint rows rather than both planning the same
    contract. It is a lease over the *contract*, not over the notification, so the
    unique constraint on the slot remains the last line of defence rather than the
    first.

    ``after_id`` exists because the reminder planner must be able to prove progress.
    A contract that is *refused* every tick — pipeline blocked, no consent — keeps
    matching this predicate, so ordering by urgency alone would let a batch-sized
    block of permanently-blocked contracts starve every contract behind them
    forever, and the tick would still look healthy. See
    :class:`app.workers.reminder_planner.ReminderPlanner` for how the cursor is
    advanced.
    """
    if limit <= 0:
        return []
    statement = (
        select(Contract)
        .where(
            Contract.status.in_(REMINDABLE_CONTRACT_STATUSES),
            Contract.do_not_contact.is_(False),
            Contract.end_date.is_not(None),
            Contract.end_date >= earliest_end_date,
            Contract.end_date <= latest_end_date,
            Contract.id > after_id,
        )
        .order_by(Contract.id)
        .limit(limit)
        .with_for_update(skip_locked=True, of=Contract)
    )
    return list(session.execute(statement).scalars())


def claim_expired_candidates(
    session: Session,
    *,
    on_or_before: date,
    after_id: int = 0,
    limit: int,
) -> list[Contract]:
    """Lease the contracts whose end date may already have passed, **unscoped**.

    The tenant predicate is absent for the reason :func:`claim_reminder_candidates`
    gives: the expiry sweep is a system actor with a database connection and a
    clock, not a principal, so a tenant-scoped query could not see the contracts it
    exists to close. The *write* it enables stays tenant-scoped — the sweep builds
    its audit repository from each row's own ``tenant_id``.

    ``on_or_before`` is a calendar date in **UTC**, and the predicate is deliberately
    a superset. "Has the term ended for this customer?" cannot be answered from one
    date for a book spanning every zone, so the sweep takes everything at or before
    today in UTC and lets :func:`app.workers.dispatch.contract_today` decide per row.
    A row this over-selects costs one do-nothing tick; a row it under-selects is a
    contract that stays ``active`` after its term ended, which is the defect this
    query exists to remove. The predicate is ``status IN (...) AND end_date <= ?`` —
    the shape ``ix_contracts_scan`` indexes, so the query costs one index range.

    Two deliberate absences:

    ``do_not_contact``
        **Not** filtered, unlike the reminder scan. A customer who asked us to stop
        writing asked about *messages*; their contract still ends, and an
        ``active`` contract nobody may contact is precisely the row that must not
        outlive its end date. Filtering it here would leave every opted-out
        contract permanently ``active`` — a worse lie than a stale status.

    ``end_date IS NOT NULL``
        Kept, because a contract with no end date cannot have outlived one. The
        planner treats a dateless contract as unplannable; the sweep treats it as
        unexpirable. Both leave it to review to supply the date (see
        ``app.api.v1.review``).

    ``FOR UPDATE SKIP LOCKED`` (a no-op on SQLite) so two sweep replicas lease
    disjoint rows rather than both writing a status and an audit row for one
    contract. ``after_id`` lets :meth:`app.workers.expiry.ExpirySweep.drain` prove
    progress within a single tick: a row that is not yet expired *in its own zone*
    keeps matching this predicate and would otherwise occupy the front of every
    batch.
    """
    if limit <= 0:
        return []
    statement = (
        select(Contract)
        .where(
            Contract.status.in_(EXPIRABLE_CONTRACT_STATUSES),
            Contract.end_date.is_not(None),
            Contract.end_date <= on_or_before,
            Contract.id > after_id,
        )
        .order_by(Contract.id)
        .limit(limit)
        .with_for_update(skip_locked=True, of=Contract)
    )
    return list(session.execute(statement).scalars())
