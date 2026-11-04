"""Human review of extracted fields (F8).

F7 extracted fields and deliberately refused to approve them: extraction writes
``auto_accepted`` or ``pending_review`` and nothing else. This module is the other
half of that decision — the only path by which a field becomes ``approved``, and
therefore the only path by which it becomes eligible to send a customer a
notification.

Three routes, and each one exists to answer a question the F7 work left open:

* ``GET /review/queue`` — what needs a person, worst first. Gap 11 closed "silently
  empty extraction"; this closes "extracted into a queue nobody could see".
* ``PATCH /review/{field_id}`` — record the decision, or correct the value. Gap 14's
  first half.
* ``POST /review/{field_id}/reveal`` — read one stored value *and log that you
  did*. A reviewer cannot judge a phone number they are not allowed to see, and an
  organisation cannot audit a reveal that leaves no trace. Those two requirements
  are why the value is not in the queue listing and why revealing is an action
  rather than a read.

Deliberate limits, recorded in ``DECISIONS.md`` (D8-01…D8-05):

* Review does not emit an outbox event. It writes ``AuditLog`` instead, which is
  what the Phase-0 timeline mocked; the event belongs to the slice that first
  consumes it, not this one.
* A corrected value keeps the extractor's confidence, because the score describes
  the extraction and not the correction.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api.deps import app_settings, get_request_session, require_roles
from app.api.v1.contracts import ExtractedFieldSummary, field_summary
from app.core.config import Settings
from app.core.crypto import (
    DecryptionError,
    FieldCipher,
    MissingEncryptionKeyError,
    build_cipher,
)
from app.core.middleware import correlation_id_or_new
from app.core.principal import Principal
from app.db.base import utc_now
from app.db.enums import ContractStatus, FieldName, FieldStatus, UserRole
from app.db.models import Contract, ExtractedField
from app.db.repositories import (
    AuditLogRepository,
    ContractRepository,
    ExtractedFieldRepository,
    ReviewCursor,
    decode_review_cursor,
    encode_review_cursor,
)
from app.db.repositories.audit import ACTOR_USER, AUDIT_ENTITY_FIELD
from app.db.repositories.fields import DEFAULT_REVIEW_QUEUE_LIMIT, MAX_REVIEW_QUEUE_LIMIT
from app.workers.extraction import decrypt_for_review, normalize_value

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/review", tags=["review"])

#: The action names the Phase-0 review queue already speaks, kept as a literal so
#: an unknown action is a 422 from the schema rather than a silent no-op.
ReviewAction = Literal["accept", "reject", "mark_unreadable", "edit"]

#: What each decision stores on the row. ``edit`` is an approval: a person read the
#: document, saw the extraction was wrong, and supplied the right value.
_DECISION_STATUS: dict[ReviewAction, FieldStatus] = {
    "accept": FieldStatus.APPROVED,
    "edit": FieldStatus.APPROVED,
    "reject": FieldStatus.REJECTED,
    "mark_unreadable": FieldStatus.UNREADABLE,
}

#: ``review_reason`` defaults, so a decision is never stored without a reason.
_DECISION_REASON: dict[ReviewAction, str] = {
    "accept": "value accepted by reviewer",
    "edit": "value corrected by reviewer",
    "reject": "value rejected by reviewer",
    "mark_unreadable": "document marked unreadable by reviewer",
}

REVIEWED_ACTION = "field.reviewed"
REVEALED_ACTION = "field.value_revealed"


class ReviewItem(ExtractedFieldSummary):
    """One field awaiting review, with the contract it is holding up.

    Extends the value-free ``ExtractedFieldSummary`` that the contract fields route
    returns, so there is exactly one definition of a field-without-its-value and a
    new route cannot invent a looser one. ``field_id`` is added here because the
    queue has to address rows across contracts; it is not named ``id`` to keep from
    shadowing the builtin in the response model.
    """

    field_id: int
    contract_id: int
    customer_ref: str
    contract_status: ContractStatus
    end_date: date | None
    #: Negative once the contract has already expired — that is information, not an
    #: error, so it is reported rather than clamped.
    days_to_expiry: int | None
    age_minutes: int
    #: The confidence threshold this field fell below, so a reviewer can see *why*
    #: it is in the queue without knowing the deployment's configuration.
    threshold: float


class ReviewQueueResponse(BaseModel):
    """Everything waiting for a person, lowest confidence first.

    One page of it, anyway: the queue is paged by sort key, so a portfolio whose
    backlog exceeds ``MAX_REVIEW_QUEUE_LIMIT`` is reachable rather than truncated
    (gap 16).
    """

    items: list[ReviewItem]
    #: How many items this page carries, not how many the queue holds.
    count: int
    #: Age of the oldest item *on this page*. Anything above zero means the queue is
    #: not draining — but only the first page is a trustworthy reading, because a
    #: later page starts after the oldest items by construction.
    oldest_minutes: int
    threshold: float
    #: Pass back as ``?cursor=`` to read the next page. ``None`` means this is the
    #: last page. Opaque by design: a client must not parse it.
    next_cursor: str | None = None


class ReviewDecisionRequest(BaseModel):
    """One reviewer decision, tied to the version the reviewer was looking at."""

    action: ReviewAction
    #: The version from the queue item. A mismatch is a 409: someone else got there
    #: first, and silently applying both would let two reviewers overwrite each other.
    version: int = Field(ge=1)
    #: Only meaningful for ``edit``; the replacement text for this field.
    value: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _check_value_usage(self) -> ReviewDecisionRequest:
        if self.action == "edit":
            if self.value is None or not self.value.strip():
                raise ValueError("value is required when action is 'edit'")
        elif self.value is not None:
            # Rejecting it would be worse than saying so: a caller that sends a value
            # with `accept` believes it is being stored.
            raise ValueError("value is only accepted when action is 'edit'")
        return self


class ReviewDecisionResponse(BaseModel):
    """The decided field, plus how much it still blocks."""

    item: ReviewItem
    #: Blocking fields left on the contract. Zero means the pipeline may proceed.
    blocking: int


class FieldValueResponse(BaseModel):
    """One revealed value. Returned by ``POST /reveal`` and nowhere else."""

    field_id: int
    field_name: FieldName
    value: str
    version: int


def _cipher_or_503(settings: Settings) -> FieldCipher:
    """The configured cipher, or a 503 explaining what is missing.

    ``ENCRYPTION_KEY`` is optional at start-up (an installation that only ingests
    does not need it), so a missing key is a deployment state rather than a bug.
    Failing closed here means an unconfigured installation cannot decrypt, and also
    cannot quietly store a correction in plaintext.
    """
    try:
        return build_cipher(settings)
    except MissingEncryptionKeyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Field values cannot be read: ENCRYPTION_KEY is not configured",
        ) from None


def _state(field: ExtractedField) -> dict[str, Any]:
    """The audit snapshot for one field — deliberately never the value.

    The audit log is plain text that any operator can read (``AuditLog`` has no
    column encryption), so putting a phone number here would undo the encryption F7
    applies to ``raw_value``. Status, version and confidence are enough to answer
    "who changed what, and when".
    """
    return {
        "field_name": field.field_name.value,
        "status": field.status.value,
        "version": field.version,
        "confidence": float(field.confidence),
    }


def _item(
    field: ExtractedField,
    contract: Contract,
    *,
    threshold: float,
    now: datetime | None = None,
) -> ReviewItem:
    """Build the queue view of one field."""
    moment = now or utc_now()
    return ReviewItem(
        **field_summary(field).model_dump(),
        field_id=field.id,
        contract_id=contract.id,
        customer_ref=contract.customer_ref,
        contract_status=contract.status,
        end_date=contract.end_date,
        days_to_expiry=(contract.end_date - moment.date()).days if contract.end_date else None,
        # Floored at zero: clock skew between a writer and this reader must not show
        # an operator a negative age for a row that was just created.
        age_minutes=max(int((moment - field.created_at).total_seconds() // 60), 0),
        threshold=threshold,
    )


def _blocking(session: Session, principal: Principal, contract_id: int) -> int:
    """How many fields still hold this contract's pipeline."""
    rows = ExtractedFieldRepository(session, principal.tenant_id).for_contract(contract_id)
    return sum(1 for row in rows if row.blocks_pipeline)


@router.get(
    "/queue",
    response_model=ReviewQueueResponse,
    summary="List fields awaiting human review",
)
def review_queue(
    limit: int = Query(default=DEFAULT_REVIEW_QUEUE_LIMIT, ge=1, le=MAX_REVIEW_QUEUE_LIMIT),
    cursor: str | None = Query(
        default=None,
        max_length=512,
        description="The ``next_cursor`` of the previous page. Omit for the first page.",
    ),
    principal: Principal = Depends(
        require_roles(UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER)
    ),
    session: Session = Depends(get_request_session),
    settings: Settings = Depends(app_settings),
) -> ReviewQueueResponse:
    """Every field below the confidence threshold that a person has not decided yet.

    Viewer is allowed: this lists review *state* and never a value, exactly like the
    contract fields route. Revealing a value is a separate, audited, role-gated call.
    """
    threshold = settings.extraction_review_threshold
    after = _cursor_or_422(cursor)
    # One row beyond the page: whether a next page exists is answered by the same
    # query rather than by a second ``COUNT`` that could disagree with it.
    rows = ExtractedFieldRepository(session, principal.tenant_id).awaiting_review(
        limit=limit + 1, after=after
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    now = utc_now()
    items = [_item(field, contract, threshold=threshold, now=now) for field, contract in page]
    return ReviewQueueResponse(
        items=items,
        count=len(items),
        oldest_minutes=max((item.age_minutes for item in items), default=0),
        threshold=threshold,
        # Only when a further row is known to exist: handing back a cursor that
        # leads to an empty page makes every client do one pointless round trip.
        next_cursor=_cursor_of(page[-1][0]) if has_more and page else None,
    )


def _cursor_or_422(token: str | None) -> ReviewCursor | None:
    """Decode a caller's page marker, or refuse it loudly.

    A malformed cursor is a ``422`` rather than a silent first page: a client that
    pages by cursors would otherwise loop over page one forever, which looks like a
    server bug from the outside.
    """
    if token is None:
        return None
    try:
        return decode_review_cursor(token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The review cursor is not recognised; restart from the first page",
        ) from None


def _cursor_of(field: ExtractedField) -> str:
    """The page marker that resumes the queue after ``field``."""
    return encode_review_cursor(
        ReviewCursor(
            confidence=Decimal(field.confidence),
            created_at=field.created_at,
            field_id=field.id,
        )
    )


@router.patch(
    "/{field_id}",
    response_model=ReviewDecisionResponse,
    summary="Record a reviewer's decision about one field",
)
def review_field(
    field_id: int,
    payload: ReviewDecisionRequest,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
    settings: Settings = Depends(app_settings),
) -> ReviewDecisionResponse:
    """Accept, reject, mark unreadable, or correct one extracted field.

    Viewer is refused: this is the write that lets a contract's fields reach a
    customer, and the operator-only rule from F6/D6-01 applies to it for the same
    reason.

    A field belonging to another tenant is answered 404, not 403 — the repository
    returns ``None`` outside this tenant, so the route cannot confirm which field
    ids exist (IDOR/BOLA).
    """
    threshold = settings.extraction_review_threshold
    repository = ExtractedFieldRepository(session, principal.tenant_id)
    field = repository.by_id(field_id)
    if field is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Field not found")

    contract = ContractRepository(session, principal.tenant_id).get(Contract, field.contract_id)
    if contract is None:  # pragma: no cover - the row cannot exist without its contract
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Field not found")

    if field.version != payload.version:
        # The 409 carries the winning row so the reviewer can see what changed
        # instead of guessing, rather than being told only that they lost.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "This field changed since you loaded it; reload and try again",
                "details": {
                    "current": _item(field, contract, threshold=threshold).model_dump(mode="json")
                },
            },
        )

    before = _state(field)
    try:
        if payload.action == "edit":
            _apply_correction(field, payload, _cipher_or_503(settings))
        field.status = _DECISION_STATUS[payload.action]
        field.reviewed_by = principal.user_id
        field.reviewed_at = utc_now()
        field.review_reason = payload.reason or _DECISION_REASON[payload.action]
        # Flush before the snapshot so ``after`` records the version the write
        # produced, not the one it replaced.
        session.flush()
        after = _state(field)
        AuditLogRepository(session, principal.tenant_id).record(
            correlation_id=correlation_id_or_new(),
            actor_type=ACTOR_USER,
            actor_id=principal.user_id,
            action=REVIEWED_ACTION,
            entity=AUDIT_ENTITY_FIELD,
            entity_id=str(field.id),
            before=before,
            after=after,
            reason=field.review_reason,
        )
        session.commit()
    except StaleDataError:
        # The version check above is the common path; this is the race it cannot
        # close on its own (another writer committed between the read and the flush).
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This field changed since you loaded it; reload and try again",
        ) from None

    # Read after commit: the item must report the stored state, including the new
    # version, and computing it before the commit would report the old one.
    session.refresh(field)
    decided = _item(field, contract, threshold=threshold)
    remaining = _blocking(session, principal, contract.id)
    logger.info(
        "review_decided",
        field_id=field.id,
        action=payload.action,
        status=decided.status.value,
        blocking=remaining,
    )
    return ReviewDecisionResponse(item=decided, blocking=remaining)


def _apply_correction(
    field: ExtractedField, payload: ReviewDecisionRequest, cipher: FieldCipher
) -> None:
    """Store a reviewer-supplied value, normalized and encrypted like an extracted one.

    The value is normalized for the field it belongs to, not stored as typed: a
    correction that ends up as an unparseable phone number would pass review and then
    fail at send time, which is the failure this whole gate exists to prevent.
    """
    raw = (payload.value or "").strip()
    try:
        normalized = normalize_value(field.field_name, raw)
    except ValueError:
        # Deliberately does not echo the value back: an error is logged and
        # sometimes rendered, and the value is customer PII.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The value is not a valid {field.field_name.value}",
        ) from None
    field.raw_value = cipher.encrypt(raw)
    field.normalized_value = normalized


@router.post(
    "/{field_id}/reveal",
    response_model=FieldValueResponse,
    summary="Reveal one field's stored value, audited",
)
def reveal_field_value(
    field_id: int,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
    settings: Settings = Depends(app_settings),
) -> FieldValueResponse:
    """Decrypt one field's value and record who looked at it.

    ``POST`` rather than ``GET`` because this writes an audit row. A read with a
    side effect is not cacheable, not safely prefetchable, and not something a
    proxy should repeat — and a reveal that leaves no trace is unauditable, which
    is the whole reason the queue withholds values.

    Viewer is refused: reading a customer's contact details is a PII access event,
    not a view of review state.
    """
    field = ExtractedFieldRepository(session, principal.tenant_id).by_id(field_id)
    if field is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Field not found")

    cipher = _cipher_or_503(settings)
    try:
        value = decrypt_for_review(field, cipher)
    except DecryptionError:
        # Never echo the token or the exception text: one is ciphertext, the other
        # can name the key material's shape.
        logger.warning("field_value_undecryptable", field_id=field.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The stored value cannot be decrypted with the configured key",
        ) from None

    if value is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This field has no stored value to reveal",
        )

    response = FieldValueResponse(
        field_id=field.id,
        field_name=field.field_name,
        value=value,
        version=field.version,
    )
    AuditLogRepository(session, principal.tenant_id).record(
        correlation_id=correlation_id_or_new(),
        actor_type=ACTOR_USER,
        actor_id=principal.user_id,
        action=REVEALED_ACTION,
        entity=AUDIT_ENTITY_FIELD,
        entity_id=str(field.id),
        before=None,
        after=None,
        reason=None,
    )
    session.commit()
    # The value itself is never logged — only the fact that someone looked.
    logger.info("field_value_revealed", field_id=field.id, field_name=field.field_name.value)
    return response
