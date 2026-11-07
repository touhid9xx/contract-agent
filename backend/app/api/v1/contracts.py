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

``GET /contracts`` is the aggregate's **read** side, and it exists because
without it every per-contract route in v1 took its id on faith: a contract could
be created and its id echoed once, but never found again. It pages by keyset
(``?cursor=``), filters on ``status``, and exposes no customer value and no
filesystem path — see :class:`ContractListItem`.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api.deps import document_store_or_503, get_request_session, require_roles
from app.core.documents import (
    DocumentStore,
    DocumentTooLargeError,
    EmptyDocumentError,
    document_digest,
)
from app.core.middleware import correlation_id_or_new
from app.core.principal import Principal
from app.db.base import as_utc
from app.db.enums import (
    Channel,
    ContractSource,
    ContractStatus,
    FieldName,
    FieldStatus,
    RiskLevel,
    UserRole,
)
from app.db.models import Contract, ExtractedField
from app.db.repositories.audit import (
    ACTION_CONSENT_UPDATED,
    ACTOR_USER,
    AUDIT_ENTITY_CONTRACT,
    AuditLogRepository,
)
from app.db.repositories.contracts import (
    DEFAULT_CONTRACT_PAGE_LIMIT,
    MAX_CONTRACT_PAGE_LIMIT,
    ContractRepository,
    apply_consent,
    consent_state,
)
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

#: Media type of the upload body (``POST /contracts/document``).
#:
#: Declared rather than inferred because FastAPI decides how to read a body from
#: its media type. Anything ``application/json`` is parsed as JSON, so a client
#: that sent a PDF under that header would get a JSON decode error instead of its
#: document stored — a confusing answer to a plausible mistake.
DOCUMENT_MEDIA_TYPE = "application/octet-stream"


def _clean_customer_ref(value: str) -> str:
    """Trim a customer label and reject a blank one.

    Module-level so the metadata route and the document route cannot disagree
    about what a usable label is: two copies of "strip, then refuse empty" is
    exactly the pair that drifts into one path accepting ``"   "``.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("must not be blank")
    return cleaned


def _clean_timezone(value: str) -> str:
    """Validate an IANA-shaped zone name (see ``_TIMEZONE_RE``)."""
    candidate = value.strip()
    if not _TIMEZONE_RE.match(candidate):
        raise ValueError("must be an IANA time zone name such as 'Europe/London'")
    return candidate


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
        return _clean_customer_ref(value)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        return _clean_timezone(value)

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


class ContractListItem(BaseModel):
    """One contract as a list shows it.

    Nothing here is a customer value: ``customer_ref`` is the pseudonymous label
    the model documents, and the identity lives in the encrypted
    ``ExtractedField`` rows, which this route never touches.

    Two columns are deliberately **not** exposed:

    ``storage_path``
        An absolute path inside the API container. A viewer has no use for it and
        it maps the deployment's filesystem for anyone who does. What a caller
        actually needs to know — "is there a document behind this row?" — is
        answered by ``has_document``.

    ``created_by``
        An internal user id. Nothing renders it, and it would let a viewer
        enumerate accounts by scrolling a contract list.
    """

    id: int
    customer_ref: str
    status: ContractStatus
    source: ContractSource
    risk: RiskLevel
    start_date: date | None
    end_date: date | None
    timezone: str
    #: Per-tenant dedupe key — the metadata fingerprint, or the document hash when
    #: the row came from ``POST /contracts/document`` (gap 12). A fingerprint of
    #: content, not the content itself, so it is safe to list.
    sha256: str | None
    #: ``storage_path is not None``. A boolean rather than the path: see above.
    has_document: bool
    consent_email: bool
    consent_sms: bool
    consent_voice: bool
    do_not_contact: bool
    #: Derived from the flags by the model, so this listing cannot disagree with
    #: what the decision policy will refuse.
    consent_channels: list[Channel]
    may_contact: bool
    version: int
    created_at: datetime


class ContractListResponse(BaseModel):
    """One keyset page of the tenant's contracts, newest first."""

    items: list[ContractListItem]
    count: int
    #: Echoed so a caller reading a filtered list knows which filter produced it.
    status: ContractStatus | None
    #: The id to pass back as ``?cursor=`` for the next page, or ``None`` when this
    #: page reached the end. Set only when a row was actually seen beyond the page,
    #: so ``None`` is a real completeness claim rather than "the page looked full".
    next_cursor: int | None


def _list_item(contract: Contract) -> ContractListItem:
    """Serialize one contract for the listing.

    ``created_at`` is re-attached to UTC here for the same reason the decision view
    does it: MySQL's ``DATETIME`` discards the offset, so ``as_utc`` is the single
    place the timezone is put back (D2-01).
    """
    return ContractListItem(
        id=contract.id,
        customer_ref=contract.customer_ref,
        status=contract.status,
        source=contract.source,
        risk=contract.risk,
        start_date=contract.start_date,
        end_date=contract.end_date,
        timezone=contract.timezone,
        sha256=contract.sha256,
        has_document=contract.storage_path is not None,
        consent_email=contract.consent_email,
        consent_sms=contract.consent_sms,
        consent_voice=contract.consent_voice,
        do_not_contact=contract.do_not_contact,
        consent_channels=sorted(contract.consent_channels, key=lambda channel: channel.value),
        may_contact=contract.may_contact,
        version=contract.version,
        created_at=as_utc(contract.created_at) or contract.created_at,
    )


@router.get(
    "",
    response_model=ContractListResponse,
    summary="List the caller's contracts, newest first",
)
def list_contracts(
    contract_status: Annotated[ContractStatus | None, Query(alias="status")] = None,
    cursor: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_CONTRACT_PAGE_LIMIT)] = DEFAULT_CONTRACT_PAGE_LIMIT,
    principal: Principal = Depends(
        require_roles(UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER)
    ),
    session: Session = Depends(get_request_session),
) -> ContractListResponse:
    """Read the contracts this tenant holds.

    Until now the aggregate root had a write path and no read path: a contract
    could be ingested, and every later route took its id on faith. This is the
    listing an operator needs to find that id — it is what makes the other
    per-contract routes reachable without reading one out of an ingest response.

    Viewer is allowed, because nothing here is a customer value.

    The parameter is named ``contract_status`` and aliased to ``status``: the
    module already imports ``fastapi.status`` and a parameter shadowing it would
    turn the next ``status.HTTP_...`` in this function into an ``AttributeError``
    at the worst possible moment.

    ``limit + 1`` rows are fetched to decide whether a next page exists. Asking
    for one row more is what lets ``next_cursor`` be a fact about the database
    rather than an inference from "the page looked full".
    """
    repository = ContractRepository(session, principal.tenant_id)
    rows = repository.recent(limit=limit + 1, status=contract_status, after=cursor)

    items = [_list_item(row) for row in rows[:limit]]
    return ContractListResponse(
        items=items,
        count=len(items),
        status=contract_status,
        next_cursor=items[-1].id if len(rows) > limit else None,
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


@router.post(
    "/document",
    response_model=ContractSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a contract from an uploaded document",
)
def ingest_contract_document(
    customer_ref: Annotated[str, Query(min_length=1, max_length=200)],
    payload: Annotated[bytes, Body(media_type=DOCUMENT_MEDIA_TYPE)],
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    timezone: Annotated[str, Query(max_length=MAX_TIMEZONE_LENGTH)] = "UTC",
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
    store: DocumentStore = Depends(document_store_or_503),
) -> ContractSummary:
    """Register a contract **and** keep the document it was read from (gap 12).

    This is the route that gives ``Contract.storage_path`` its first writer, and
    therefore the first route after which ``extract_contract`` has text to work
    with. It is deliberately a sibling of ``POST /contracts`` rather than an
    extension of it: metadata ingestion is a JSON body and a hash of the request,
    while this is opaque bytes and a hash of the file, and folding the two into
    one endpoint would mean one schema with a mutually exclusive body.

    The bytes are **not** parsed here. Storing and reading a document are separate
    concerns, and a route that also tried to extract text would put an unbounded
    CPU cost (PDF parsing) inside a request that is otherwise a write. What this
    route guarantees is narrower and testable: a document that arrives is sealed,
    addressed by its own hash, and named by a committed row.
    """
    try:
        clean_ref = _clean_customer_ref(customer_ref)
        clean_timezone = _clean_timezone(timezone)
    except ValueError as exc:
        # The same rules ``ContractCreateRequest`` applies to a JSON body, applied
        # to query parameters, and answered with the status FastAPI would have
        # used for the body: a caller should not have to know which half of the
        # request a bad value was in.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="end_date must not be before start_date",
        )

    # Fingerprint first, store second: a duplicate is refused without sealing a
    # document that no row would ever name. The hash is over the plaintext, so it
    # is stable across sealings and usable as the per-tenant idempotency key.
    digest = document_digest(payload)
    repository = ContractRepository(session, principal.tenant_id)
    existing = repository.by_sha256(digest)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This document has already been ingested as contract {existing.id}",
        )

    try:
        stored = store.save(payload)
    except DocumentTooLargeError as exc:
        # 413 rather than 422: the request is well-formed and the client's only
        # problem is the size, which is what 413 exists to say.
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "code": "DOCUMENT_TOO_LARGE",
                "message": str(exc),
                "details": {"size": exc.size, "limit": exc.limit},
            },
        ) from None
    except EmptyDocumentError:
        # Unreachable through this route (FastAPI rejects a zero-length body as a
        # missing field), kept because the store is also used from the CLI, where
        # there is no request parser to catch it first.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The request body must contain the document bytes",
        ) from None

    try:
        contract = repository.create(
            customer_ref=clean_ref,
            sha256=stored.sha256,
            source=ContractSource.PDF,
            start_date=start_date,
            end_date=end_date,
            timezone=clean_timezone,
            created_by=principal.user_id,
            # Written before the commit, on purpose: a crash between the two
            # leaves an unreferenced blob (harmless, deduplicated, reused on
            # re-upload), where the opposite order would leave a row pointing at
            # a document that does not exist.
            storage_path=stored.key,
        )
        session.flush()

        event = enqueue_event(
            session,
            tenant_id=principal.tenant_id,
            correlation_id=correlation_id_or_new(),
            event_type=CONTRACT_INGESTED_EVENT,
            topic=CONTRACT_EVENTS_TOPIC,
            partition_key=str(contract.id),
            # The same five keys the metadata route publishes. A consumer must not
            # be able to tell which route produced the fact; what differs is
            # ``source`` (``pdf`` here, ``api`` there), which is a field it already
            # reads.
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
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This document has already been ingested",
        ) from None

    log.info(
        "contract_document_ingested",
        contract_id=contract.id,
        outbox_event_id=str(summary.outbox_event_id),
        # Size and key only: the digest is a fingerprint of a document that
        # contains PII, and the log's job here is to make a stored file findable,
        # not to record its contents.
        document_bytes=stored.size,
        storage_key=stored.key,
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


class ConsentUpdateRequest(BaseModel):
    """The full consent state for one contract.

    All four flags are required on every call. This is a replacement of the whole
    consent picture, not a patch of one flag, because the dangerous mistake here is
    a partial write: a caller that sends only ``consent_email: true`` and leaves the
    rest unset must not silently revoke SMS consent it never mentioned. Omitting a
    field is a 422 rather than an implicit ``false``.
    """

    consent_email: bool
    consent_sms: bool
    consent_voice: bool
    #: A veto over every channel. Auditable separately from the grants because the
    #: two answer different questions: what did they allow, and did they say stop.
    do_not_contact: bool
    #: The version the operator was looking at, as ``GET /contracts/{id}/fields`` and
    #: the decision listing report it. A mismatch is a 409 so two operators cannot
    #: overwrite each other's consent decision.
    version: int = Field(ge=1)


class ConsentView(BaseModel):
    """A contract's stored consent state, plus what it resolves to."""

    contract_id: int
    consent_email: bool
    consent_sms: bool
    consent_voice: bool
    do_not_contact: bool
    #: Derived from the flags, not stored twice: this is the same answer the
    #: decision policy reads, so a caller can see *why* a decision will be refused.
    consent_channels: list[Channel]
    may_contact: bool
    consent_updated_at: datetime | None
    version: int


def _consent_view(contract: Contract) -> ConsentView:
    return ConsentView(
        contract_id=contract.id,
        consent_email=contract.consent_email,
        consent_sms=contract.consent_sms,
        consent_voice=contract.consent_voice,
        do_not_contact=contract.do_not_contact,
        consent_channels=sorted(contract.consent_channels, key=lambda channel: channel.value),
        may_contact=contract.may_contact,
        consent_updated_at=contract.consent_updated_at,
        version=contract.version,
    )


@router.put(
    "/{contract_id}/consent",
    response_model=ConsentView,
    summary="Record a contract's channel consent and opt-out state",
)
def update_consent(
    contract_id: int,
    payload: ConsentUpdateRequest,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
) -> ConsentView:
    """Replace the consent state for one contract.

    This route exists because the columns default to ``false``: without a write path
    every contract would be un-contactable, which is the correct fail-closed default
    but not a usable system. Consent is recorded by a person who has it, so this is
    an operator action, and it is audited for the same reason a review decision is —
    "who said we could call this customer" has to be answerable later.

    A re-submission identical to what is stored writes nothing: no version bump, no
    audit row. Otherwise an idempotent retry would look like a second consent
    decision and could lose an operator's concurrent change for no reason.

    ``PUT`` rather than ``PATCH`` because the body carries the complete state, and
    because ``PATCH`` already means "one reviewer decision" elsewhere in v1.
    """
    contract = ContractRepository(session, principal.tenant_id).get(Contract, contract_id)
    if contract is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contract not found")

    if contract.version != payload.version:
        # Same shape as the review 409: the caller is told what the row is now, so a
        # lost update does not have to be diagnosed by re-reading everything.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "VERSION_CONFLICT",
                "message": "Consent changed since you loaded it; reload and try again",
                "details": {"current": _consent_view(contract).model_dump(mode="json")},
            },
        )

    # The snapshot and the write itself come from the repository module, shared with
    # ``python -m app.cli consent-import``: two copies of "what consent looks like"
    # would drift the first time a channel was added.
    before = consent_state(contract)
    changed = apply_consent(
        contract,
        consent_email=payload.consent_email,
        consent_sms=payload.consent_sms,
        consent_voice=payload.consent_voice,
        do_not_contact=payload.do_not_contact,
    )
    if not changed:
        return _consent_view(contract)

    try:
        # Flush before the snapshot so ``after`` records the version the write
        # produced, not the one it replaced.
        session.flush()
        after = consent_state(contract)
        AuditLogRepository(session, principal.tenant_id).record(
            correlation_id=correlation_id_or_new(),
            actor_type=ACTOR_USER,
            actor_id=principal.user_id,
            action=ACTION_CONSENT_UPDATED,
            entity=AUDIT_ENTITY_CONTRACT,
            entity_id=str(contract.id),
            before=before,
            after=after,
            reason="consent recorded through the API",
        )
        session.commit()
    except StaleDataError:
        # The version check above is the common path; this is the race it cannot
        # close on its own (another writer committed between the read and the flush).
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "VERSION_CONFLICT",
                "message": "Consent changed since you loaded it; reload and try again",
            },
        ) from None

    session.refresh(contract)
    log.info(
        "contract_consent_updated",
        contract_id=contract.id,
        channels=[channel.value for channel in contract.consent_channels],
        do_not_contact=contract.do_not_contact,
        version=contract.version,
    )
    return _consent_view(contract)
