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
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.orm import Session

from app.core.blind_index import CONTACT_FIELD_NAMES
from app.db.enums import FieldName, FieldStatus
from app.db.models import Contract, ExtractedField
from app.db.repositories.base import TenantScopedRepository

#: How many awaiting-review fields the queue returns when the caller does not say.
#: Bounded at the query, not in the route, so no caller can ask for the whole table.
DEFAULT_REVIEW_QUEUE_LIMIT = 100
MAX_REVIEW_QUEUE_LIMIT = 500

#: How many field rows one ``reseal-fields`` run rewrites. Bounded like every other
#: operator batch, so a mistake cannot rewrite an unbounded table in one transaction.
DEFAULT_RESEAL_LIMIT = 500

#: How many contact rows one ``backfill-blind-index`` run repairs. Bounded for the
#: same reason: the column it writes is derived from ciphertext, so a wrong key or a
#: wrong normalisation would rewrite every digest in the table in one transaction.
DEFAULT_BLIND_INDEX_BACKFILL_LIMIT = 500

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
        blind_index: str | None = None,
    ) -> ExtractedField:
        """Add one extracted field.

        ``raw_value`` arrives already encrypted: encryption happens in the
        extraction stage, which refuses to run without a key, so there is no code
        path that could reach this method with plaintext.

        ``blind_index`` is the same value's keyed digest, for contact fields only
        (:func:`app.core.blind_index.contact_digest` decides which). It is optional
        here rather than required because a non-contact field legitimately has none —
        but a caller that forgets it for an email or a phone produces a row the
        opt-out path cannot find, which is why the two callers that exist both compute
        it from the field name rather than deciding for themselves.
        """
        field = ExtractedField(
            contract_id=contract_id,
            field_name=field_name,
            raw_value=raw_value,
            normalized_value=normalized_value,
            blind_index=blind_index,
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
        blind_index: str | None = None,
    ) -> ExtractedField:
        """Replace the value and score of an existing field row.

        ``version`` is bumped by the optimistic-lock column, so a reviewer holding
        a stale copy of the row loses the race with a 409 rather than silently
        overwriting the newer value — the same contract as the review endpoint
        that follows.

        ``blind_index`` is rewritten alongside ``normalized_value`` and never left
        alone: an index that still points at the *previous* number is worse than no
        index, because a lookup would match the old value with full confidence. The
        two travel together in this method so a caller cannot update one and skip the
        other by forgetting an argument.
        """
        field.raw_value = raw_value
        field.normalized_value = normalized_value
        field.blind_index = blind_index
        field.confidence = confidence
        field.status = status
        field.source_page = source_page
        field.source_line = source_line
        return field


def fields_for_reseal(
    session: Session,
    *,
    field_ids: Sequence[int] | None = None,
    limit: int = DEFAULT_RESEAL_LIMIT,
) -> list[ExtractedField]:
    """Fetch field rows for ``python -m app.cli reseal-fields``, **unscoped**.

    This is the third audited exception to tenant scoping, alongside
    ``dead_lettered`` and ``contract_for_operator``. An operator running the CLI has
    a database connection, not a principal, so there is no tenant to be scoped to —
    and scoping it to one tenant would be actively wrong: every tenant's plaintext
    has to be sealed, and a per-tenant run leaves the others behind while looking
    like it succeeded.

    Ordered by id so ``--limit`` takes a stable prefix and a second run continues
    where the first stopped instead of re-reading the same rows.
    """
    stmt = select(ExtractedField).order_by(ExtractedField.id.asc()).limit(limit)
    if field_ids is not None:
        stmt = stmt.where(ExtractedField.id.in_(list(field_ids)))
    return list(session.execute(stmt).scalars())


def fields_for_reseal_for_tenant(
    session: Session,
    tenant_id: uuid.UUID,
    *,
    field_ids: Sequence[int] | None = None,
    limit: int = DEFAULT_RESEAL_LIMIT,
) -> list[ExtractedField]:
    """Field rows belonging to **one organisation**, for the tenant attention read.

    The tenant-scoped twin of :func:`fields_for_reseal`, and like it in every respect
    except the ``tenant_id`` predicate. Written beside it rather than replacing it:
    ``reseal-fields`` runs from a shell with a database connection and no principal,
    and it must seal *every* tenant's plaintext in one pass — scoping that run to one
    organisation would leave the others in the clear while reporting success. The
    sibling's docstring is an argument about that operator command, not about a
    request-scoped read, so neither reading narrows the other.

    This one exists for the opposite situation: an authenticated request where the
    tenant came from the token. Handing it another organisation's field ids would
    both leak and be useless, since ``reseal-fields --ids`` is the only thing that
    can act on them and it is not reachable over HTTP.

    Ordered by id for the same reason as the sibling: a bounded scan takes a stable
    prefix, and a full page means the count is a **floor**, which the caller reports
    as ``field_scan_truncated`` rather than rounding away.
    ``ix_extracted_fields_tenant_status`` leads on ``tenant_id``.
    """
    stmt = (
        select(ExtractedField)
        .where(ExtractedField.tenant_id == tenant_id)
        .order_by(ExtractedField.id.asc())
        .limit(limit)
    )
    if field_ids is not None:
        stmt = stmt.where(ExtractedField.id.in_(list(field_ids)))
    return list(session.execute(stmt).scalars())


#: How many contracts one inbound reply may suppress in a single pass.
#:
#: Bounded because the caller is an internet-facing webhook and an unbounded
#: ``UPDATE`` behind it is a denial-of-service primitive, not because 100 is a
#: meaningful customer number. The bound is also a *safety* claim, and the honest one
#: is narrow: a number shared by more contracts than this is a switchboard, where a
#: bare STOP is ambiguous anyway (whose employee is it?), so stopping at the cap and
#: reporting it is better than guessing on the hundred-and-first. Exceeding it is
#: logged as ``opt_out_truncated`` rather than silently finishing.
MAX_OPT_OUT_CONTRACTS = 100


def fields_needing_blind_index(
    session: Session,
    *,
    field_ids: Sequence[int] | None = None,
    limit: int = DEFAULT_BLIND_INDEX_BACKFILL_LIMIT,
) -> list[ExtractedField]:
    """Contact rows whose ``blind_index`` is still ``NULL``, **unscoped**.

    The repair set for ``python -m app.cli backfill-blind-index``, which is the step
    migration ``0007`` promises. Rows written before the column existed carry no
    digest, and until one is computed an inbound STOP cannot find them — so this is
    not tidying, it is the difference between honouring an opt-out and not.

    The predicate is ``IS NULL`` rather than "recompute every contact row" so the
    command converges: a second run over a repaired database examines nothing, which
    is what makes it safe to run from a cron or to re-run after an interruption.
    Recomputing rows that already have a digest would also mean a rotated key
    silently rewrites live digests one batch at a time, which is exactly the failure
    the separate key exists to avoid.

    Unscoped for the reason :func:`fields_for_reseal` gives: this runs from a shell
    with a connection and no principal, and scoping it to one organisation would
    report success while leaving the others unindexed. Contact names only, so the
    permanent ``NULL`` on a date column is not counted as a defect.

    Ordered by id, so a bounded run takes a stable prefix and the next run resumes
    where this one stopped.
    """
    stmt = (
        select(ExtractedField)
        .where(
            ExtractedField.field_name.in_(tuple(CONTACT_FIELD_NAMES)),
            ExtractedField.blind_index.is_(None),
        )
        .order_by(ExtractedField.id.asc())
        .limit(limit)
    )
    if field_ids is not None:
        stmt = stmt.where(ExtractedField.id.in_(list(field_ids)))
    return list(session.execute(stmt).scalars())


def contracts_for_contact(
    session: Session,
    *,
    field_name: FieldName,
    blind_index: str,
    limit: int = MAX_OPT_OUT_CONTRACTS,
) -> list[Contract]:
    """Find every contract whose contact field has this digest, **across tenants**.

    This is the third audited exception to tenant scoping, and the only one reachable
    from an unauthenticated request. ``dead_lettered`` and ``contract_for_operator``
    exist because a CLI has no principal; this one exists because the *customer* has
    no principal either. Someone replying STOP from a phone has no tenant to name and
    cannot be asked for one — the whole point of an opt-out is that it must work
    without cooperation. So the lookup is deliberately global and the tenant comes
    from whatever rows it finds.

    Widening the *read* does not widen the *write*: each contract returned carries its
    own ``tenant_id``, which is what the caller builds its repositories and its audit
    rows from. The same argument as ``contract_for_operator``, and the reason this is
    one named function instead of an unscoped ``select`` at the call site — the
    exceptions have to stay countable.

    **Status is not part of the predicate, deliberately.** A pending-review or even
    rejected contact field still matches. The reasoning is one-sided: the data quality
    of our extraction is our problem, while suppression is the fail-safe direction. If
    a low-confidence number matches a STOP, honouring it can only *withhold* a message
    we might have sent; ignoring it would keep texting someone who asked us to stop.
    Between a false suppression and an unwanted text, the false suppression wins.

    Ordered by id so the cap takes a stable prefix, and returning a list rather than a
    lazy result so the caller sees a snapshot it can reason about after the commit.
    """
    stmt = (
        select(Contract)
        .join(ExtractedField, ExtractedField.contract_id == Contract.id)
        .where(
            ExtractedField.field_name == field_name,
            ExtractedField.blind_index == blind_index,
        )
        .order_by(Contract.id.asc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


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
