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
from app.core.middleware import current_correlation
from app.core.principal import Principal
from app.db.enums import ContractSource, ContractStatus, RiskLevel, UserRole
from app.db.models import Contract
from app.db.repositories.contracts import ContractRepository
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


def _correlation_id() -> str:
    """The request's correlation id, or a fresh one outside a request context.

    ``OutboxEvent.correlation_id`` is non-nullable, so an empty value would make
    ingestion fail on a column constraint; a standalone call (a script, a worker)
    has no bound context and must not be able to trigger that.
    """
    bound = current_correlation().get("request_id")
    return bound if isinstance(bound, str) and bound else str(uuid.uuid4())


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
            correlation_id=_correlation_id(),
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
