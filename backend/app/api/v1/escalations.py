"""Customer responses and the escalation queue (gap 15).

Before this module the pipeline ran as far as "a reminder was sent" and stopped:
``customer_responses`` and ``escalations`` existed in the schema since ``0001``
and nothing ever wrote to them, so nothing recorded that a customer answered and
nothing opened the clock that says a human is late.

The split follows the rest of the repo: **policy lives in
:mod:`app.workers.escalation`** — whether a second escalation may be raised,
whether a resolved one may be reassigned, when a deadline passes, what a reply
does to the contract's status — and this file is transport. That is what lets the
rules be tested without a database or an HTTP client, and it is why the refusals
here are raised by the worker and merely *mapped* by :func:`app.api.v1.agent.refusal`
into the same 409 envelope the decision and dispatch routers already use.

Two things this deliberately does **not** do:

* It never stores what the customer said. ``customer_responses`` records
  *that* they replied, on which channel and when; the reply body is not accepted
  by the request model at all, because every other table in this app goes out of
  its way to avoid holding customer PII in plaintext and a message body would be
  the largest such hole. ``response_metadata`` stays available for a channel
  adapter that needs a provider id.
* It does not close a contract when an operator resolves an escalation by hand.
  A hand-resolved escalation is a closed *queue item*; the contract is still one
  nobody has heard from. Only a recorded customer response moves the contract to
  ``responded``, which keeps the two facts — "someone worked this" and "the
  customer answered" — from collapsing into one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api.deps import app_settings, get_request_session, require_roles
from app.api.v1.agent import refusal
from app.core.config import Settings
from app.core.middleware import correlation_id_or_new
from app.core.principal import Principal
from app.db.base import as_utc, truncate_to_second, utc_now
from app.db.enums import Channel, ContractStatus, EscalationStatus, ResponseType, UserRole
from app.db.models import Contract, CustomerResponse, Escalation
from app.db.repositories import (
    DEFAULT_ESCALATION_QUEUE_LIMIT,
    MAX_ESCALATION_QUEUE_LIMIT,
    AuditLogRepository,
    ContractRepository,
    CustomerResponseRepository,
    EscalationRepository,
    UserRepository,
)

#: The audit vocabulary is imported from its own module rather than the repository
#: facade, matching every other router that writes audit rows (``contracts.py``,
#: ``agent.py``, ``review.py``): the facade re-exports *repositories*, and the
#: action/entity strings belong to the log they describe.
from app.db.repositories.audit import (
    ACTION_ESCALATION_ASSIGNED,
    ACTION_ESCALATION_CLAIMED,
    ACTION_ESCALATION_OPENED,
    ACTION_ESCALATION_RESOLVED,
    ACTION_RESPONSE_RECORDED,
    ACTOR_USER,
    AUDIT_ENTITY_ESCALATION,
    AUDIT_ENTITY_RESPONSE,
)
from app.workers.escalation import (
    EscalationRefusedError,
    is_overdue,
    refusal_to_assign,
    refusal_to_raise,
    refusal_to_resolve,
    refusal_to_transition,
    sla_deadline,
    status_after_response,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["escalations"])

#: Bounds on the free-text note an operator may leave when closing an escalation.
#: Long enough for "called back, will sign next week", short enough that the row
#: stays a note rather than a document. It is *not* where a customer's words go.
MAX_RESOLUTION_NOTE_LENGTH = 500


class EscalationView(BaseModel):
    """One escalation as the queue renders it.

    ``overdue`` is computed from ``sla_deadline`` and the clock rather than read
    from a column, so a queue fetched at 09:00 shows a deadline that passed at
    08:00 as late without anything having to write to the row.
    """

    id: int
    contract_id: int
    status: EscalationStatus
    assigned_to: uuid.UUID | None
    sla_deadline: datetime
    resolved_at: datetime | None
    resolution_note: str | None
    overdue: bool
    version: int


class EscalationListResponse(BaseModel):
    """A page of the queue plus the count a dashboard headline needs."""

    items: list[EscalationView]
    count: int
    #: How many of ``items`` have passed their deadline. Not a separate query:
    #: counting the page is the number an operator acts on.
    overdue: int
    #: Echoed so the UI can say "48 h" without hard-coding the configured value.
    sla_hours: int


class CustomerResponseView(BaseModel):
    """That a customer answered — not what they said (see the module docstring)."""

    id: int
    contract_id: int
    channel: Channel
    response_type: ResponseType
    received_at: datetime


class RecordResponseRequest(BaseModel):
    """The reply's metadata only; the reply itself is never sent to this API."""

    channel: Channel
    response_type: ResponseType = ResponseType.REPLY
    #: When the customer replied, if that is not "now" — an operator transcribing
    #: a reply from a call log should record the call's time, not their own.
    received_at: datetime | None = None


class RecordResponseResponse(BaseModel):
    """The stored reply, plus the two side effects it caused."""

    response: CustomerResponseView
    contract_id: int
    #: The status the contract now holds. Equal to the old one when the contract
    #: had already closed: a late reply is still recorded, but it does not rewrite
    #: "this expired".
    contract_status: ContractStatus
    #: Set when this reply closed the contract's open escalation.
    resolved_escalation_id: int | None


class EscalationUpdateRequest(BaseModel):
    """The two changes an operator makes from the queue, in one body.

    Both are optional but at least one is required. ``version`` is the row's
    version as the operator loaded it; supplying it turns "I edited the row I was
    looking at" into a checked claim instead of a last-write-wins overwrite.
    """

    assigned_to: uuid.UUID | None = None
    status: EscalationStatus | None = None
    resolution_note: str | None = Field(default=None, max_length=MAX_RESOLUTION_NOTE_LENGTH)
    version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _require_a_change(self) -> EscalationUpdateRequest:
        if self.assigned_to is None and self.status is None and self.resolution_note is None:
            raise ValueError("Provide assigned_to, status, or resolution_note")
        if self.resolution_note is not None and self.status is not EscalationStatus.RESOLVED:
            # A note without the transition would be attached to a row that is
            # still open, where nothing reads it and the next resolve overwrites it.
            raise ValueError("resolution_note is only accepted when status is resolved")
        return self


def _wire(value: datetime) -> datetime:
    """Re-attach UTC to a stored naive datetime for the response body."""
    return as_utc(value) or value


def _stored(value: datetime) -> datetime:
    """Convert a caller's timestamp to the naive UTC a ``DateTime`` column holds.

    The inverse of :func:`_wire`, and floored to the second for the reason
    ``truncate_to_second`` documents: MySQL rounds a value carrying microseconds,
    so an unfloored ``received_at`` can come back half a second later than the
    value that was compared against it.
    """
    naive = value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
    return truncate_to_second(naive)


def _view(row: Escalation, now: datetime) -> EscalationView:
    """Serialize one row; ``overdue`` is decided here, never stored."""
    return EscalationView(
        id=row.id,
        contract_id=row.contract_id,
        status=row.status,
        assigned_to=row.assigned_to,
        sla_deadline=_wire(row.sla_deadline),
        resolved_at=as_utc(row.resolved_at),
        resolution_note=row.resolution_note,
        overdue=is_overdue(row, now),
        version=row.version,
    )


def _response_view(row: CustomerResponse) -> CustomerResponseView:
    return CustomerResponseView(
        id=row.id,
        contract_id=row.contract_id,
        channel=row.channel,
        response_type=row.response_type,
        received_at=_wire(row.received_at),
    )


def _load_contract(session: Session, principal: Principal, contract_id: int) -> Contract:
    """One contract, tenant-scoped; another tenant's id is a 404, not a 403."""
    contract = ContractRepository(session, principal.tenant_id).get(Contract, contract_id)
    if contract is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contract not found")
    return contract


def _load_escalation(session: Session, principal: Principal, escalation_id: int) -> Escalation:
    """One escalation, tenant-scoped, for the same 404-not-403 reason."""
    escalation = EscalationRepository(session, principal.tenant_id).by_id(escalation_id)
    if escalation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Escalation not found")
    return escalation


@router.post(
    "/contracts/{contract_id}/response",
    response_model=RecordResponseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a customer's reply to a contract",
)
def record_response(
    contract_id: int,
    payload: RecordResponseRequest,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
) -> RecordResponseResponse:
    """Store that the customer answered, and close what that closes.

    Recording a reply resolves the contract's open escalation if there is one — a
    customer who answers is exactly what the SLA clock was waiting for, so leaving
    the escalation open would keep an operator chasing a reply already on file.
    The two writes share one transaction for that reason.

    Viewer is refused: this changes a contract's status and closes a queue item.

    A reply against an already closed contract (``renewed`` / ``expired``) is
    accepted and recorded, but does not move the status — see
    :func:`app.workers.escalation.status_after_response`.
    """
    contract = _load_contract(session, principal, contract_id)
    correlation_id = correlation_id_or_new()
    received_at = (
        _stored(payload.received_at)
        if payload.received_at is not None
        else truncate_to_second(utc_now())
    )

    responses = CustomerResponseRepository(session, principal.tenant_id)
    escalations = EscalationRepository(session, principal.tenant_id)
    audit = AuditLogRepository(session, principal.tenant_id)

    try:
        response = responses.create(
            contract_id=contract.id,
            correlation_id=correlation_id,
            channel=payload.channel,
            response_type=payload.response_type,
            received_at=received_at,
        )
        audit.record(
            correlation_id=correlation_id,
            actor_type=ACTOR_USER,
            actor_id=principal.user_id,
            action=ACTION_RESPONSE_RECORDED,
            entity=AUDIT_ENTITY_RESPONSE,
            entity_id=str(response.id),
            after={
                "contract_id": contract.id,
                "channel": payload.channel.value,
                "response_type": payload.response_type.value,
                "received_at": received_at.isoformat(),
            },
        )

        # The status change is decided by the worker and applied here, so the
        # route audits what actually moved rather than what it assumed would.
        next_status = status_after_response(contract)
        previous_status = contract.status
        if next_status is not None:
            contract.status = next_status
            session.flush()

        open_escalation = escalations.open_for_contract(contract.id)
        resolved_id: int | None = None
        if open_escalation is not None:
            before = {"status": open_escalation.status.value}
            open_escalation.status = EscalationStatus.RESOLVED
            open_escalation.resolved_at = received_at
            open_escalation.resolution_note = "Customer responded"
            session.flush()
            resolved_id = open_escalation.id
            audit.record(
                correlation_id=correlation_id,
                actor_type=ACTOR_USER,
                actor_id=principal.user_id,
                action=ACTION_ESCALATION_RESOLVED,
                entity=AUDIT_ENTITY_ESCALATION,
                entity_id=str(open_escalation.id),
                before=before,
                after={"status": EscalationStatus.RESOLVED.value, "reason": "customer_response"},
                reason="customer_response",
            )

        session.commit()
    except StaleDataError:
        # Two callers raced to change the same escalation or contract. The reply
        # itself is not the loser here; asking for a retry keeps the audit trail
        # consistent with the row it describes.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This contract changed while the response was being recorded; try again",
        ) from None

    session.refresh(response)
    if resolved_id is not None:
        logger.info(
            "customer_response_resolved_escalation",
            contract_id=contract.id,
            escalation_id=resolved_id,
            channel=payload.channel.value,
        )
    else:
        logger.info(
            "customer_response_recorded",
            contract_id=contract.id,
            channel=payload.channel.value,
            status=previous_status.value,
        )
    return RecordResponseResponse(
        response=_response_view(response),
        contract_id=contract.id,
        contract_status=contract.status,
        resolved_escalation_id=resolved_id,
    )


@router.post(
    "/contracts/{contract_id}/escalation",
    response_model=EscalationView,
    status_code=status.HTTP_201_CREATED,
    summary="Raise an escalation for a contract that has gone quiet",
)
def raise_escalation(
    contract_id: int,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
    settings: Settings = Depends(app_settings),
) -> EscalationView:
    """Open the SLA clock on a contract, and mark the contract ``escalated``.

    The deadline comes from ``settings.escalation_sla_hours`` (``DECISIONS.md``
    Q2: 48 h) rather than a request field: an SLA an operator can choose per row
    is not an SLA. It is floored to the second, and computed from "now" — this
    route raises the escalation, it does not backdate one.

    A second escalation on the same contract is refused (409 ``ALREADY_ESCALATED``)
    instead of being allowed to open a parallel clock, which would make "the
    oldest open escalation" ambiguous and let a queue show one contract twice.
    """
    contract = _load_contract(session, principal, contract_id)
    escalations = EscalationRepository(session, principal.tenant_id)

    try:
        refusal_to_raise(contract, escalations.open_for_contract(contract.id))
    except EscalationRefusedError as exc:
        raise refusal(exc) from None

    correlation_id = correlation_id_or_new()
    deadline = truncate_to_second(sla_deadline(utc_now(), hours=settings.escalation_sla_hours))
    previous_status = contract.status

    try:
        escalation = escalations.create(
            contract_id=contract.id,
            correlation_id=correlation_id,
            deadline=deadline,
        )
        contract.status = ContractStatus.ESCALATED
        session.flush()
        AuditLogRepository(session, principal.tenant_id).record(
            correlation_id=correlation_id,
            actor_type=ACTOR_USER,
            actor_id=principal.user_id,
            action=ACTION_ESCALATION_OPENED,
            entity=AUDIT_ENTITY_ESCALATION,
            entity_id=str(escalation.id),
            before={"contract_status": previous_status.value},
            after={
                "status": escalation.status.value,
                "sla_deadline": deadline.isoformat(),
                "contract_status": contract.status.value,
            },
            reason=f"no response within {settings.escalation_sla_hours}h",
        )
        session.commit()
    except StaleDataError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This contract changed while the escalation was being raised; try again",
        ) from None

    session.refresh(escalation)
    logger.info(
        "escalation_raised",
        contract_id=contract.id,
        escalation_id=escalation.id,
        sla_hours=settings.escalation_sla_hours,
    )
    return _view(escalation, utc_now())


@router.get(
    "/escalations",
    response_model=EscalationListResponse,
    summary="List the escalation queue, most urgent first",
)
def list_escalations(
    statuses: list[EscalationStatus] | None = Query(default=None, alias="status"),
    limit: int = Query(default=DEFAULT_ESCALATION_QUEUE_LIMIT, ge=1, le=MAX_ESCALATION_QUEUE_LIMIT),
    principal: Principal = Depends(
        require_roles(UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER)
    ),
    session: Session = Depends(get_request_session),
    settings: Settings = Depends(app_settings),
) -> EscalationListResponse:
    """The queue, ordered by deadline — the row the SLA punishes first is first.

    A viewer may read it: knowing what is late is not a write. ``status`` may be
    repeated (``?status=open&status=in_progress``); omitting it returns every
    state, including resolved ones, which is what an auditor wants and what a
    working queue filters out.
    """
    rows = EscalationRepository(session, principal.tenant_id).queue(limit=limit, statuses=statuses)
    now = utc_now()
    items = [_view(row, now) for row in rows]
    return EscalationListResponse(
        items=items,
        count=len(items),
        overdue=sum(1 for item in items if item.overdue),
        sla_hours=settings.escalation_sla_hours,
    )


@router.patch(
    "/escalations/{escalation_id}",
    response_model=EscalationView,
    summary="Assign, claim, or resolve an escalation",
)
def update_escalation(
    escalation_id: int,
    payload: EscalationUpdateRequest,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
) -> EscalationView:
    """The queue's two mutations behind one route.

    Assigning writes an ``escalation.assigned`` audit row and moving to
    ``in_progress`` records that someone picked it up; resolving writes
    ``escalation.resolved`` with the note. Both may be done in one call, in which
    case both rows are written — an audit trail that drops the intermediate step
    cannot answer "who picked this up", which is the question it exists for.

    ``assigned_to`` must be a colleague: a user id from another tenant is a 404
    from :meth:`UserRepository.by_id`, the same opacity every other foreign id
    gets. An assignment points a queue item at a person; it grants them nothing.

    Resolving here closes the queue item **only**. The contract keeps
    ``escalated`` — nobody has heard from the customer, and the module docstring
    says why those two facts stay separate.
    """
    escalation = _load_escalation(session, principal, escalation_id)

    if payload.version is not None and payload.version != escalation.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This escalation changed since you loaded it; reload and try again",
        )

    correlation_id = correlation_id_or_new()
    audit = AuditLogRepository(session, principal.tenant_id)
    mutated = False

    try:
        if payload.assigned_to is not None:
            try:
                refusal_to_assign(escalation)
            except EscalationRefusedError as exc:
                raise refusal(exc) from None
            assignee = UserRepository(session, principal.tenant_id).by_id(payload.assigned_to)
            if assignee is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            # Assigning a row to whoever already holds it changes nothing unless
            # the status also moves, and an audit row that says "before == after"
            # is a false record of work. Same rule as the status branch below.
            if escalation.assigned_to != assignee.id or escalation.status is EscalationStatus.OPEN:
                before: dict[str, Any] = {
                    "assigned_to": str(escalation.assigned_to) if escalation.assigned_to else None,
                    "status": escalation.status.value,
                }
                escalation.assigned_to = assignee.id
                # Claiming is implied: assigning to someone who is not shown as
                # working it leaves the queue reading "open" for a row in hand.
                if escalation.status is EscalationStatus.OPEN:
                    escalation.status = EscalationStatus.IN_PROGRESS
                session.flush()
                mutated = True
                audit.record(
                    correlation_id=correlation_id,
                    actor_type=ACTOR_USER,
                    actor_id=principal.user_id,
                    action=ACTION_ESCALATION_ASSIGNED,
                    entity=AUDIT_ENTITY_ESCALATION,
                    entity_id=str(escalation.id),
                    before=before,
                    after={
                        "assigned_to": str(assignee.id),
                        "status": escalation.status.value,
                    },
                )

        if payload.status is not None:
            try:
                refusal_to_transition(escalation, payload.status)
                if payload.status is EscalationStatus.RESOLVED:
                    refusal_to_resolve(escalation)
            except EscalationRefusedError as exc:
                raise refusal(exc) from None
            if payload.status is not escalation.status:
                before_status = {"status": escalation.status.value}
                escalation.status = payload.status
                if payload.status is EscalationStatus.RESOLVED:
                    escalation.resolved_at = truncate_to_second(utc_now())
                escalation.resolution_note = payload.resolution_note
                session.flush()
                mutated = True
                audit.record(
                    correlation_id=correlation_id,
                    actor_type=ACTOR_USER,
                    actor_id=principal.user_id,
                    action=ACTION_ESCALATION_RESOLVED
                    if payload.status is EscalationStatus.RESOLVED
                    else ACTION_ESCALATION_CLAIMED,
                    entity=AUDIT_ENTITY_ESCALATION,
                    entity_id=str(escalation.id),
                    before=before_status,
                    after={"status": escalation.status.value},
                    reason=payload.resolution_note,
                )

        if not mutated:
            # Every supplied field already held that value. Nothing changed, so
            # nothing is written: an audit row claiming an edit that did not
            # happen is worse than no row.
            return _view(escalation, utc_now())

        session.commit()
    except StaleDataError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This escalation changed since you loaded it; reload and try again",
        ) from None

    session.refresh(escalation)
    logger.info(
        "escalation_updated",
        escalation_id=escalation.id,
        status=escalation.status.value,
        assigned=escalation.assigned_to is not None,
    )
    return _view(escalation, utc_now())
