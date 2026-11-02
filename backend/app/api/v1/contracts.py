"""Contract ingestion.

This is the first route in the system that writes a business row **and** an outbox
event in the same transaction, which is why it is short: ingestion is not a batch
pipeline yet, it is one atomic write. The four guarantees it enforces, each taken
from a rule the repo already states elsewhere:

1. **The tenant comes from the principal.** There is no ``tenant_id`` in the
   request body, and a ``tenant_id`` a caller adds anyway is ignored by the model
   — the repository stamps the row from the token (F3, D2-02).
2. **Ingestion is idempotent per tenant.** The content fingerprint is the
   ``sha256`` column and its uniqueness is ``(tenant_id, sha256)``, so the same
   contract ingested twice is one row — and two tenants may ingest the same
   document independently.
3. **The event commits with the write or not at all.** ``enqueue_event`` is
   called on the request session inside the same transaction: a crash after
   commit still publishes, and a rollback discards the event with the row. That
   atomicity is the entire reason the outbox exists (D2-04).
4. **Nothing below the review threshold moves.** Ingestion produces no
   ``ExtractedField`` and therefore cannot notify anyone: extraction and review
   are the next milestone, and a contract with no approved contact field stays
   inert.

The route deliberately answers 409 rather than silently returning the existing
row. A retried POST is a *conflict*, not a success it never performed; the
response names the id of the row that already holds that content so the caller
can read it, and no second outbox event is created.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_request_session, require_roles
from app.core.middleware import correlation_id_or_new
from app.core.principal import Principal
from app.db.enums import ContractSource, ContractStatus, FieldName, FieldStatus, RiskLevel, UserRole
from app.db.models import Contract, ExtractedField
from app.db.repositories.contracts import ContractRepository
from app.db.repositories.fields import ExtractedFieldRepository
from app.db.repositories.outbox import enqueue_event

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/contracts", tags=["contracts"])

#: Domain event announcing an ingested contract. Named past-tense because it is a
#: fact that already happened, and versioned on the row rather than in the name so
#: a consumer keeps its subscription when the payload grows.
CONTRACT_INGESTED_EVENT = "contract.ingested"
#: One topic per aggregate rather than per event type: a consumer subscribes once
#: and routes on the ``event_type`` header, which avoids a new subscription for
#: every later contract event (see DECISIONS.md D6-02).
CONTRACT_EVENTS_TOPIC = "contract.events"

#: IANA-shaped zone names, validated by **shape only**.
#:
#: ``zoneinfo.ZoneInfo`` is the obvious validator and is unusable here: the
#: runtime has no ``tzdata`` package, so even ``ZoneInfo("UTC")`` raises
#: ``ZoneInfoNotFoundError`` on a standard Windows install (see DECISIONS.md
#: D6-03). A shape check still rejects injection-shaped input and the empty
#: string, and the value is not consumed until the deterministic quiet-hours
#: check — which is where the tz database has to arrive anyway.
_TIMEZONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*$")

#: Column width of ``contracts.timezone``.
MAX_TIMEZONE_LENGTH = 64


class ContractCreateRequest(BaseModel):
    """The content needed to register one contract.

    ``extra`` stays at Pydantic's default (ignore) on purpose, so a client that
    sends ``tenant_id`` is not rewarded with an error that confirms the field
    would otherwise matter — it is simply never read.
    """

    #: Pseudonymous label for lists ("A. Rahman"). Full identity belongs to the
    #: extracted, encrypted fields, not to the contract row.
    customer_ref: str = Field(min_length=1, max_length=200)
    start_date: date | None = None
    end_date: date | None = None
    timezone: str = Field(default="UTC", max_length=MAX_TIMEZONE_LENGTH)
    source: ContractSource = ContractSource.API

    @field_validator("customer_ref")
    @classmethod
    def _strip_ref(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        candidate = value.strip()
        if not _TIMEZONE_RE.match(candidate):
            raise ValueError("must be an IANA time zone name such as 'Europe/London'")
        return candidate

    @model_validator(mode="after")
    def _check_date_order(self) -> ContractCreateRequest:
        """Reject an interval that runs backwards.

        Both dates stay optional: a contract ingested before its dates are known
        is legitimate, and the daily scan simply skips it until ``end_date``
        exists. An *inverted* pair is never legitimate — it would silently never
        match any reminder window, which is worse than a 422.
        """
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class ContractSummary(BaseModel):
    """What ingestion returns: the persisted row plus the event it announced."""

    id: int
    customer_ref: str
    status: ContractStatus
    source: ContractSource
    risk: RiskLevel
    start_date: date | None
    end_date: date | None
    timezone: str
    sha256: str | None
    version: int
    created_at: datetime
    #: Identity of the outbox row written in the same transaction. Exposed so a
    #: caller (or a test) can follow the event without reading the database.
    outbox_event_id: uuid.UUID


def _fingerprint(payload: ContractCreateRequest) -> str:
    """The per-tenant dedupe key for one ingestion request.

    A stable SHA-256 over the *content* fields, canonicalised by a fixed key set
    and sorted keys, so two requests describing the same contract hash identically
    regardless of JSON key order or field defaults being explicit.

    When PDF upload lands this column will hold the hash of the file bytes
    instead. The two schemes cannot collide in any meaningful way — both are
    SHA-256 of different preimages — so mixing them in one column still yields the
    right answer: the same document is one row, and the same metadata is one row.
    """
    canonical = {
        "customer_ref": payload.customer_ref,
        "end_date": payload.end_date.isoformat() if payload.end_date else None,
        "source": payload.source.value,
        "start_date": payload.start_date.isoformat() if payload.start_date else None,
        "timezone": payload.timezone,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _summary(contract: Contract, *, outbox_event_id: uuid.UUID) -> ContractSummary:
    return ContractSummary(
        id=contract.id,
        customer_ref=contract.customer_ref,
        status=contract.status,
        source=contract.source,
        risk=contract.risk,
        start_date=contract.start_date,
        end_date=contract.end_date,
        timezone=contract.timezone,
        sha256=contract.sha256,
        version=contract.version,
        created_at=contract.created_at,
        outbox_event_id=outbox_event_id,
    )


@router.post(
    "",
    response_model=ContractSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a contract and announce it on the outbox",
)
def ingest_contract(
    payload: ContractCreateRequest,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
) -> ContractSummary:
    """Register one contract for the caller's tenant.

    Viewer is excluded: ingestion is a write that starts a reminder clock, so it
    is an operator action, not a read-only one.
    """
    repository = ContractRepository(session, principal.tenant_id)
    fingerprint = _fingerprint(payload)

    existing = repository.by_sha256(fingerprint)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This contract has already been ingested as contract {existing.id}",
        )

    try:
        contract = repository.create(
            customer_ref=payload.customer_ref,
            sha256=fingerprint,
            source=payload.source,
            start_date=payload.start_date,
            end_date=payload.end_date,
            timezone=payload.timezone,
            created_by=principal.user_id,
        )
        # Flush, not commit: the event's partition key is the contract id, so the
        # id has to exist while the transaction is still open. A rollback below
        # discards both rows together, which is the property being bought.
        session.flush()

        event = enqueue_event(
            session,
            tenant_id=principal.tenant_id,
            correlation_id=correlation_id_or_new(),
            event_type=CONTRACT_INGESTED_EVENT,
            topic=CONTRACT_EVENTS_TOPIC,
            # All events for one contract share a partition, so a consumer sees
            # them in order.
            partition_key=str(contract.id),
            payload={
                "contract_id": contract.id,
                "end_date": contract.end_date.isoformat() if contract.end_date else None,
                "sha256": contract.sha256,
                "source": contract.source.value,
                "status": contract.status.value,
            },
        )
        summary = _summary(contract, outbox_event_id=event.event_id)
        session.commit()
    except IntegrityError:
        # The uniqueness check above is advisory: two concurrent requests can both
        # pass it and the constraint is what actually decides. Reporting that as
        # 409 keeps the outcome stable instead of surfacing a 500 the caller
        # cannot act on — and the rollback means the losing request leaves no
        # outbox event behind.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This contract has already been ingested",
        ) from None

    log.info(
        "contract_ingested",
        contract_id=contract.id,
        outbox_event_id=str(summary.outbox_event_id),
        source=payload.source.value,
    )
    return summary


class ExtractedFieldSummary(BaseModel):
    """One extracted field's review state — deliberately **without its value**.

    The value is PII. The Phase-0 review queue renders a confidence bar and a set
    of actions; revealing what was extracted is a review *detail* action that has
    to be audited, which makes it the review endpoint's job rather than something
    a list route any viewer can poll (DECISIONS.md D7-05).
    """

    field_name: FieldName
    status: FieldStatus
    confidence: float
    version: int
    source_page: int | None
    source_line: int | None
    reviewed_at: datetime | None
    review_reason: str | None
    #: Taken from the model, not recomputed here: one definition of "this field
    #: stops the pipeline", or the two will drift (``ExtractedField``).
    blocks_pipeline: bool


class ContractFieldsResponse(BaseModel):
    """What extraction produced for one contract."""

    contract_id: int
    fields: list[ExtractedFieldSummary]
    #: How many fields still hold the notification pipeline. This is the number an
    #: operator acts on, and it is why a zero-field contract is visible rather
    #: than looking like a clean one.
    blocking: int


def field_summary(field: ExtractedField) -> ExtractedFieldSummary:
    """The value-free view of one extracted field.

    Public because the review queue (``app/api/v1/review.py``) serves the same
    shape with contract context added around it: one definition of "a field
    without its PII" is what keeps a value from leaking through whichever route
    is written next.
    """
    return ExtractedFieldSummary(
        field_name=field.field_name,
        status=field.status,
        # ``Numeric(4,3)`` comes back as ``Decimal``; the API has always spoken
        # floats for confidence (the Phase-0 fixtures do too).
        confidence=float(field.confidence),
        version=field.version,
        source_page=field.source_page,
        source_line=field.source_line,
        reviewed_at=field.reviewed_at,
        review_reason=field.review_reason,
        blocks_pipeline=field.blocks_pipeline,
    )


@router.get(
    "/{contract_id}/fields",
    response_model=ContractFieldsResponse,
    summary="List the fields extracted from one contract",
)
def list_contract_fields(
    contract_id: int,
    principal: Principal = Depends(
        require_roles(UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER)
    ),
    session: Session = Depends(get_request_session),
) -> ContractFieldsResponse:
    """Read one contract's extraction state.

    Viewer is allowed: this is a read, and it is what the review queue lists.

    A contract belonging to another tenant is answered 404, not 403 — the
    repository's ``get`` returns ``None`` for a row outside this tenant, so the
    route cannot be used to learn which contract ids exist (IDOR/BOLA).
    """
    contract = ContractRepository(session, principal.tenant_id).get(Contract, contract_id)
    if contract is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contract not found")

    rows = ExtractedFieldRepository(session, principal.tenant_id).for_contract(contract.id)
    summaries = [field_summary(row) for row in sorted(rows, key=lambda row: row.field_name.value)]
    return ContractFieldsResponse(
        contract_id=contract.id,
        fields=summaries,
        blocking=sum(1 for summary in summaries if summary.blocks_pipeline),
    )
