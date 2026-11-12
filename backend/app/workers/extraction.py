"""Field extraction: document text in, reviewable ``ExtractedField`` rows out (F7).

This module is the **policy** half of extraction and it is deliberately free of
any backend: it takes a :class:`DocumentExtractor` and a
:class:`~app.core.crypto.FieldCipher`, so the seams that are hard to test (a local
model, a key) are injected and the rules below can be proven offline. The shape
mirrors the outbox relay (``app/workers/outbox_relay.py`` + ``publishers.py``):
the consumer of a protocol lives here, the concrete transports live next door.

The four rules, each of which a reader should be able to check in the code:

1. **A field is never ``approved`` here.** Extraction may write
   ``auto_accepted`` (at or above the threshold) or ``pending_review``. Approval
   is a human decision and there is no code path in this module that can forge it
   (spec §CUSTOMER CONTACT POLICY).
2. **A reviewer's decision is never overwritten.** Re-extraction skips rows in
   ``approved``/``rejected``/``unreadable`` instead of quietly replacing a value a
   person signed off on. The count comes back as ``skipped_reviewed`` so the skip
   is visible rather than silent.
3. **Raw values are encrypted before they are stored.** ``cipher`` is a required
   argument, not an optional one, which is what makes "there is no plaintext
   mode" true by construction rather than by discipline.
4. **Re-extraction updates in place.** ``uq_extracted_fields_contract_field``
   means one row per ``(contract, field)``; the upsert keeps that invariant so a
   reader can never pick the wrong duplicate.

Nothing here logs a value. Field *names* and counts are safe to log; the text they
came from is PII and stays out of ``structlog`` entirely (spec: no raw PII in logs).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

from sqlalchemy.orm import Session

from app.core.crypto import FieldCipher
from app.core.metrics import EXTRACTION_CONFIDENCE, EXTRACTIONS_TOTAL
from app.db.enums import FieldName, FieldStatus
from app.db.models import Contract, ExtractedField
from app.db.repositories.fields import ExtractedFieldRepository

__all__ = [
    "REVIEWER_OWNED_STATUSES",
    "DocumentExtractor",
    "ExtractionOutcome",
    "ExtractionUnavailableError",
    "FieldCandidate",
    "extract_contract",
    "normalize_value",
]


class ExtractionUnavailableError(RuntimeError):
    """The configured backend could not answer.

    Raised instead of returning an empty list, because "the model is down" and
    "this document contains no fields" need different responses from an operator
    and must not look identical in a log.
    """


@dataclass(frozen=True, slots=True)
class FieldCandidate:
    """One value a backend believes it found, plus how sure it is.

    ``confidence`` is a plain float in ``0.0-1.0`` and is quantized on write to
    the column's ``NUMERIC(4,3)`` precision, so a backend cannot store a score the
    database would silently round.
    """

    field_name: FieldName
    value: str
    confidence: float
    #: 1-based line of the document the value was found on, when known. Kept for
    #: provenance so a reviewer can be shown where a value came from — the Phase-0
    #: review queue already renders ``source_line``.
    source_line: int | None = None
    source_page: int | None = None


class DocumentExtractor(Protocol):
    """The slice of an extraction backend this module needs.

    Narrowed to a protocol so the rules below are tested with a fake backend
    instead of needing a local model — the same reason
    ``app/workers/publishers.py`` narrows ``kafka-python`` to ``Producer``.
    """

    def extract(self, text: str) -> Sequence[FieldCandidate]: ...


#: Statuses a human owns. Re-extraction must not touch a row in one of these:
#: replacing an approved value with a fresh model guess would silently undo a
#: review, and replacing a rejected one would resurrect something a reviewer
#: deliberately threw away.
REVIEWER_OWNED_STATUSES = frozenset(
    {FieldStatus.APPROVED, FieldStatus.REJECTED, FieldStatus.UNREADABLE}
)

#: Fields whose normalized form is an ISO-8601 date. Normalizing dates is what
#: makes ``end_date`` comparable with the daily scan's window.
_DATE_FIELDS = frozenset({FieldName.START_DATE, FieldName.END_DATE, FieldName.DOB})

_WHITESPACE = re.compile(r"\s+")
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_NON_DIGIT = re.compile(r"\D")
_NON_AMOUNT = re.compile(r"[^0-9.\-]")
_MIN_PHONE_DIGITS = 7
_MAX_PHONE_DIGITS = 15


def normalize_value(field_name: FieldName, value: str) -> str:
    """Return the canonical comparison form of ``value``.

    The canonical form is defined here and nowhere else. A value that cannot be
    normalized raises ``ValueError``; the caller discards the candidate rather than
    storing something a later reader would have to guess at.

    The returned string is *plaintext* and is encrypted by the caller before it
    reaches the database (gap 20, D10-11). That is why the result is no longer
    comparable in SQL: ``ExtractedField.normalized_value`` is ciphertext, so
    "find the contract with this email" is not a query the schema offers. Nothing
    in the codebase asked for one; a blind index is what to add if something does.
    """
    text = _WHITESPACE.sub(" ", value).strip()
    if not text:
        raise ValueError("value is blank")

    if field_name is FieldName.EMAIL:
        candidate = text.lower()
        if not _EMAIL_SHAPE.match(candidate):
            raise ValueError("not an email address")
        return candidate

    if field_name is FieldName.PHONE:
        digits = _NON_DIGIT.sub("", text)
        if not _MIN_PHONE_DIGITS <= len(digits) <= _MAX_PHONE_DIGITS:
            raise ValueError("not a phone number")
        # The international prefix is part of the identity of a number, so it is
        # kept when the document had one; everything else is noise from the
        # document's formatting.
        return f"+{digits}" if text.startswith("+") else digits

    if field_name in _DATE_FIELDS:
        try:
            parsed = date.fromisoformat(text)
        except ValueError as exc:
            # Python's own message varies with the input ("Invalid isoformat
            # string" for a slash date, "month must be in 1..12" for a bad
            # month), so it is replaced with one stable phrase. The caller only
            # needs "this could not be normalized", and a parser internal is not
            # an operator-facing message.
            raise ValueError("not an ISO-8601 date") from exc
        return parsed.isoformat()

    if field_name is FieldName.CONTRACT_VALUE:
        cleaned = _NON_AMOUNT.sub("", text)
        try:
            amount = Decimal(cleaned)
        except InvalidOperation as exc:
            raise ValueError("not a numeric amount") from exc
        # ``normalize`` turns Decimal("1500000.00") into "1.5E+6"; formatting with
        # "f" renders it back to plain digits so the stored text is stable.
        return format(amount.normalize(), "f")

    return text


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    """What one extraction run did, in counts an operator can act on."""

    contract_id: int
    candidates: int
    created: int
    updated: int
    skipped_reviewed: int
    discarded: int
    found: tuple[str, ...]

    @property
    def stored(self) -> int:
        """Rows written or rewritten (created plus updated)."""
        return self.created + self.updated

    @property
    def produced_nothing(self) -> bool:
        """True when the document yielded no storable field.

        The CLI turns this into a non-zero exit code: an ingest that produces no
        fields has to be visible, not silent (STATUS.md gap 11).
        """
        return self.stored == 0


def _quantize(confidence: float) -> float:
    """Clamp to ``0.0-1.0`` and round to the column's three decimals."""
    capped = min(max(confidence, 0.0), 1.0)
    return float(Decimal(str(capped)).quantize(Decimal("0.001")))


def _status_for(confidence: float, threshold: float) -> FieldStatus:
    """The only two states extraction is allowed to produce.

    At or above the threshold the field may skip review; below it the field is a
    hard stop for the pipeline until a person looks at it
    (``ExtractedField.blocks_pipeline``). ``approved`` is deliberately unreachable.
    """
    return FieldStatus.AUTO_ACCEPTED if confidence >= threshold else FieldStatus.PENDING_REVIEW


def _best_per_field(candidates: Sequence[FieldCandidate]) -> list[FieldCandidate]:
    """One candidate per field: the most confident, ties keeping the first.

    A backend that finds two email addresses would otherwise have the row
    overwritten by whichever came last, which is a coin flip. The result is
    ordered by ``FieldName`` so a run is byte-for-byte reproducible.
    """
    best: dict[FieldName, FieldCandidate] = {}
    for candidate in candidates:
        current = best.get(candidate.field_name)
        if current is None or candidate.confidence > current.confidence:
            best[candidate.field_name] = candidate
    return [best[name] for name in FieldName if name in best]


def extract_contract(
    session: Session,
    contract: Contract,
    document_text: str,
    *,
    extractor: DocumentExtractor,
    cipher: FieldCipher,
    review_threshold: float,
) -> ExtractionOutcome:
    """Extract fields from ``document_text`` into ``contract``'s rows.

    The caller owns the transaction (as with ``ContractRepository.create``), so
    the CLI decides when to commit. The tenant is taken from the *contract*, not
    from an argument: there is no parameter through which a caller could write
    one tenant's fields onto another tenant's contract.
    """
    repository = ExtractedFieldRepository(session, contract.tenant_id)
    chosen = _best_per_field(extractor.extract(document_text))
    existing = {row.field_name: row for row in repository.for_contract(contract.id)}

    created = 0
    updated = 0
    skipped = 0
    discarded = 0
    found: list[str] = []

    for candidate in chosen:
        row = existing.get(candidate.field_name)
        if row is not None and row.status in REVIEWER_OWNED_STATUSES:
            skipped += 1
            continue

        try:
            normalized = normalize_value(candidate.field_name, candidate.value)
        except ValueError:
            # A backend that returns an unusable value is a data problem, not a
            # crash: drop the candidate, count it, and keep the rest of the run.
            discarded += 1
            continue

        confidence = _quantize(candidate.confidence)
        status = _status_for(confidence, review_threshold)
        # Both value columns are encrypted, and both are encrypted here rather than
        # in the repository: this is the stage that already refuses to run without a
        # key, so no write path can reach storage with plaintext in either column.
        raw_value = cipher.encrypt(candidate.value.strip())
        sealed_normalized = cipher.encrypt(normalized)

        if row is None:
            existing[candidate.field_name] = repository.create(
                contract_id=contract.id,
                field_name=candidate.field_name,
                raw_value=raw_value,
                normalized_value=sealed_normalized,
                confidence=confidence,
                status=status,
                source_page=candidate.source_page,
                source_line=candidate.source_line,
            )
            created += 1
        else:
            repository.overwrite(
                row,
                raw_value=raw_value,
                normalized_value=sealed_normalized,
                confidence=confidence,
                status=status,
                source_page=candidate.source_page,
                source_line=candidate.source_line,
            )
            updated += 1
        found.append(candidate.field_name.value)

    EXTRACTIONS_TOTAL.inc(status="ok")
    for candidate in chosen:
        EXTRACTION_CONFIDENCE.observe(float(candidate.confidence))

    return ExtractionOutcome(
        contract_id=contract.id,
        candidates=len(chosen),
        created=created,
        updated=updated,
        skipped_reviewed=skipped,
        discarded=discarded,
        found=tuple(found),
    )


def decrypt_for_review(field: ExtractedField, cipher: FieldCipher) -> str | None:
    """Reveal one field's raw value to a reviewer, or ``None`` if it has none.

    Kept here rather than in the read route so the decision to decrypt stays in
    one place: the review endpoint is the only caller that should ever reveal a
    raw value, and it must record that it did (audit log, next milestone).
    """
    if not field.raw_value:
        return None
    return cipher.decrypt(field.raw_value)
