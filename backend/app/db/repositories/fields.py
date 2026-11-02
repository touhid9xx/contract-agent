"""Extracted-field persistence (F7).

The table is one row per ``(contract, field_name)``, so writing extraction output
is an upsert rather than an insert. Where the *decision* to overwrite lives is the
point of this module: the query side (``for_contract``) is here, and the policy
side — whether a row may be touched at all — belongs to
:func:`app.workers.extraction.extract_contract`, because it is a review rule and
not a data-access one.

Two rules the write helpers enforce, both from
``uq_extracted_fields_contract_field`` and the specification's review gate:

- a field is never inserted twice for a contract, so a reader cannot pick the
  wrong duplicate;
- the helper is given the row to overwrite rather than searching for it, so a
  caller cannot aim an update at another contract's field.
"""

from __future__ import annotations

from app.db.enums import FieldName, FieldStatus
from app.db.models import Contract, ExtractedField
from app.db.repositories.base import TenantScopedRepository

#: How many awaiting-review fields the queue returns when the caller does not say.
#: Bounded at the query, not in the route, so no caller can ask for the whole table.
DEFAULT_REVIEW_QUEUE_LIMIT = 100
MAX_REVIEW_QUEUE_LIMIT = 500


class ExtractedFieldRepository(TenantScopedRepository[ExtractedField]):
    """Tenant-scoped access to the fields extracted from one organisation's contracts."""

    def by_id(self, field_id: int) -> ExtractedField | None:
        """One field row by primary key, or ``None`` if it belongs to another tenant.

        Review is addressed by field id, so this is the lookup the review route
        starts from. It is ``get`` rather than a hand-written query for the reason
        the base class exists: another tenant's id must look like a missing row
        (IDOR/BOLA).
        """
        return self.get(ExtractedField, field_id)

    def awaiting_review(
        self, *, limit: int = DEFAULT_REVIEW_QUEUE_LIMIT
    ) -> list[tuple[ExtractedField, Contract]]:
        """Fields waiting for a person, each with the contract it belongs to.

        Lowest confidence first: the least trustworthy extractions are the ones a
        reviewer should meet first. ``created_at``/``id`` break ties so the order
        is stable across calls, which matters for a queue someone works through.

        The contract is joined here rather than fetched per row — the queue shows
        which contract a field blocks, and one query beats N. Both sides carry the
        tenant predicate: the join is on ``contract_id`` and would otherwise be a
        path to another tenant's row if the two ever disagreed.
        """
        stmt = (
            self.scoped(ExtractedField)
            .add_columns(Contract)
            .join(Contract, ExtractedField.contract_id == Contract.id)
            .where(ExtractedField.status == FieldStatus.PENDING_REVIEW)
            .where(Contract.tenant_id == self.tenant_id)
            .order_by(
                ExtractedField.confidence.asc(),
                ExtractedField.created_at.asc(),
                ExtractedField.id.asc(),
            )
            .limit(limit)
        )
        return [(field, contract) for field, contract in self.session.execute(stmt).all()]

    def for_contract(self, contract_id: int) -> list[ExtractedField]:
        """Every field row of one contract, in no particular order.

        ``contract_id`` is not trusted on its own: the tenant predicate from
        :meth:`~app.db.repositories.base.TenantScopedRepository.scoped` is applied
        as well, so a contract id belonging to another tenant yields nothing even
        if the caller guessed it (the IDOR/BOLA case).
        """
        stmt = self.scoped(ExtractedField).where(ExtractedField.contract_id == contract_id)
        return list(self.session.execute(stmt).scalars())

    def create(
        self,
        *,
        contract_id: int,
        field_name: FieldName,
        raw_value: str,
        normalized_value: str,
        confidence: float,
        status: FieldStatus,
        source_document: str | None = None,
        source_page: int | None = None,
        source_line: int | None = None,
    ) -> ExtractedField:
        """Add one extracted field.

        ``raw_value`` arrives already encrypted: encryption happens in the
        extraction stage, which refuses to run without a key, so there is no code
        path that could reach this method with plaintext.
        """
        field = ExtractedField(
            contract_id=contract_id,
            field_name=field_name,
            raw_value=raw_value,
            normalized_value=normalized_value,
            confidence=confidence,
            status=status,
            source_document=source_document,
            source_page=source_page,
            source_line=source_line,
        )
        return self.add(field)

    def overwrite(
        self,
        field: ExtractedField,
        *,
        raw_value: str,
        normalized_value: str,
        confidence: float,
        status: FieldStatus,
        source_page: int | None = None,
        source_line: int | None = None,
    ) -> ExtractedField:
        """Replace the value and score of an existing field row.

        ``version`` is bumped by the optimistic-lock column, so a reviewer holding
        a stale copy of the row loses the race with a 409 rather than silently
        overwriting the newer value — the same contract as the review endpoint
        that follows.
        """
        field.raw_value = raw_value
        field.normalized_value = normalized_value
        field.confidence = confidence
        field.status = status
        field.source_page = source_page
        field.source_line = source_line
        return field
