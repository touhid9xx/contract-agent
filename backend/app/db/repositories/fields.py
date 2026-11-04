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

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import ColumnElement, and_, or_

from app.db.enums import FieldName, FieldStatus
from app.db.models import Contract, ExtractedField
from app.db.repositories.base import TenantScopedRepository

#: How many awaiting-review fields the queue returns when the caller does not say.
#: Bounded at the query, not in the route, so no caller can ask for the whole table.
DEFAULT_REVIEW_QUEUE_LIMIT = 100
MAX_REVIEW_QUEUE_LIMIT = 500

#: A cursor is a page marker, not a capability: it is opaque so callers do not
#: start parsing it, but it carries no secret and needs no signature. Everything
#: it names (confidence, creation time, field id) is already visible in the page
#: it came from, and the tenant predicate is applied by the query regardless, so a
#: hand-edited cursor can only move within the caller's own queue.
_CURSOR_VERSION = 1


@dataclass(frozen=True, slots=True)
class ReviewCursor:
    """The sort key of the last row a review-queue page returned.

    Kept as the triple the query orders by rather than as an offset: an operator
    deciding fields between two requests shifts every row *after* their decision,
    so ``OFFSET`` would skip or repeat work. Re-reading the key is stable under
    those writes.

    ``confidence`` is a :class:`~decimal.Decimal`, not a float: the column is
    ``NUMERIC(4, 3)`` and the page boundary compares it for equality, so the value
    has to survive the round trip exactly.
    """

    confidence: Decimal
    created_at: datetime
    field_id: int


def encode_review_cursor(cursor: ReviewCursor) -> str:
    """Render a page marker as an opaque, URL-safe token."""
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "c": str(cursor.confidence),
            "t": cursor.created_at.isoformat(),
            "i": cursor.field_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_review_cursor(token: str) -> ReviewCursor:
    """Parse a token produced by :func:`encode_review_cursor`.

    Raises :class:`ValueError` for anything else — a truncated token, a token from
    a future cursor version, or a hand-edited one. The caller turns that into a
    422; treating it as "no cursor" would silently re-serve page one and a client
    that pages through a queue would loop forever.
    """
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise ValueError("cursor is not URL-safe base64") from None
    try:
        payload: Any = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("cursor is not JSON") from None
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise ValueError("cursor version is not recognised")

    confidence = payload.get("c")
    created_at = payload.get("t")
    field_id = payload.get("i")
    if not isinstance(confidence, str) or not isinstance(created_at, str):
        raise ValueError("cursor is missing its sort key")
    if not isinstance(field_id, int) or isinstance(field_id, bool) or field_id < 1:
        raise ValueError("cursor has no field id")
    try:
        score = Decimal(confidence)
    except InvalidOperation:
        raise ValueError("cursor confidence is not a number") from None
    if not score.is_finite():
        raise ValueError("cursor confidence is not a number")
    try:
        moment = datetime.fromisoformat(created_at)
    except ValueError:
        raise ValueError("cursor timestamp is not a datetime") from None
    # Stored timestamps are naive UTC (D2-01). An aware value is accepted only by
    # converting it, so a hand-written cursor cannot compare against local time.
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return ReviewCursor(confidence=score, created_at=moment, field_id=field_id)


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
        self,
        *,
        limit: int = DEFAULT_REVIEW_QUEUE_LIMIT,
        after: ReviewCursor | None = None,
    ) -> list[tuple[ExtractedField, Contract]]:
        """Fields waiting for a person, each with the contract it belongs to.

        Lowest confidence first: the least trustworthy extractions are the ones a
        reviewer should meet first. ``created_at``/``id`` break ties so the order
        is stable across calls, which matters for a queue someone works through.

        ``after`` resumes the queue past the last row of the previous page, which
        is what closes gap 16: a queue longer than the cap used to be unreachable
        past its first ``limit`` items. It resumes by sort key rather than by
        ``OFFSET`` because the queue is written to while it is read — a reviewer
        deciding a field removes it, which shifts every later row by one and makes
        an offset skip an item nobody ever saw.

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
        )
        if after is not None:
            stmt = stmt.where(_after(after))
        stmt = stmt.order_by(
            ExtractedField.confidence.asc(),
            ExtractedField.created_at.asc(),
            ExtractedField.id.asc(),
        ).limit(limit)
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


def _after(cursor: ReviewCursor) -> ColumnElement[bool]:
    """The keyset predicate: strictly after the cursor, in queue order.

    Spelled out as three ``OR`` terms instead of a row-value comparison
    (``(a, b, c) > (?, ?, ?)``) so it reads the same on SQLite and MySQL, where
    row-constructor support differs by version. The terms mirror the ``ORDER BY``
    exactly — a predicate that disagrees with the sort order silently drops or
    repeats rows at a page boundary.
    """
    confidence = ExtractedField.confidence
    created_at = ExtractedField.created_at
    return or_(
        confidence > cursor.confidence,
        and_(confidence == cursor.confidence, created_at > cursor.created_at),
        and_(
            confidence == cursor.confidence,
            created_at == cursor.created_at,
            ExtractedField.id > cursor.field_id,
        ),
    )
