"""Notification dispatch over HTTP (F10).

``Notification`` has been in the schema since F2 and, like ``AgentDecision`` before
F9, had no writer and no route. This module is the route half; the rules are in
:mod:`app.workers.dispatch`, the persistence in
:class:`~app.db.repositories.notifications.NotificationRepository`.

Four routes, and the split between them is deliberate:

* ``GET /notifications`` — read the delivery table (filters, newest slot first).
  Viewer is allowed: no recipient value appears on a row.
* ``POST /notifications`` — turn a stored decision into a pending row for the
  milestone that is due. This is where ``AgentDecision`` becomes ``Notification``.
* ``POST /notifications/{id}/dispatch`` — attempt the send. Returns 200 with a
  ``deferred_until`` when quiet hours moved it (a deferral is not an error), and
  409 with a code when the policy refuses.
* ``POST /notifications/{id}/retry`` — clear a parked row's retry budget so an
  operator can dispatch again. Retrying is *not* implied by dispatching: a spent
  budget is a decision someone should make explicitly, which is why it has its own
  route and its own audit-visible intent.

What this layer does **not** do, for the same reason ``agent.py`` does not: it
holds no policy. Gate order, quiet hours and the refusal vocabulary live in
``app.workers.dispatch``, so the rule that decides whether a customer is contacted
is testable without a database or an HTTP client.

One thing it deliberately does not do at all: render content. ``template`` names the
milestone, and composing the body is the transport's job until there is a template
table (D10-06).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import app_settings, get_request_session, require_roles
from app.api.v1.agent import refusal
from app.core.config import Settings
from app.core.principal import Principal
from app.db.base import as_utc, utc_now
from app.db.enums import Channel, NotificationStatus, UserRole
from app.db.models import AgentDecision, Contract, ExtractedField, Notification
from app.db.repositories import (
    AgentDecisionRepository,
    ContractRepository,
    ExtractedFieldRepository,
    NotificationRepository,
)
from app.db.repositories.notifications import (
    DEFAULT_NOTIFICATION_LIMIT,
    MAX_NOTIFICATION_LIMIT,
)
from app.workers.decision import CHANNEL_CONTACT_FIELDS, USABLE_FIELD_STATUSES
from app.workers.dispatch import (
    DispatchOutcome,
    DispatchRefusedError,
    MessageSender,
    build_notification,
    build_sender,
    dispatch_notification,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

# --- route-level conflict codes ---------------------------------------------
#: A row already occupies this milestone slot. The unique constraint is what makes
#: a duplicate impossible; this code is how the caller learns *which* row owns it.
REFUSAL_NOTIFICATION_EXISTS = "NOTIFICATION_EXISTS"
#: No decision has been recorded for this contract, so there is nothing to dispatch
#: from. The fix is ``POST /agent/decisions``, and the code says so.
REFUSAL_NO_DECISION = "NO_DECISION"
#: The decision exists but belongs to a different contract.
REFUSAL_DECISION_MISMATCH = "DECISION_MISMATCH"
#: ``retry`` was asked for on a row that is not parked ``failed``.
REFUSAL_NOT_FAILED = "NOT_FAILED"


class NotificationView(BaseModel):
    """One notification, as an operator reads it.

    No recipient value appears here, for the same reason no value appears in the
    review queue: the contact value lives in ``extracted_fields`` behind the reveal
    route, and copying it onto the delivery row would put it in an API response any
    viewer can list.
    """

    id: int
    contract_id: int
    channel: Channel
    template: str
    status: NotificationStatus
    scheduled_date: date
    scheduled_at: datetime | None
    sent_at: datetime | None
    attempts: int
    #: Set when a transport failed. Provider text, truncated by the policy at 500
    #: characters — the transport is responsible for not putting a recipient in it.
    last_error: str | None
    provider_message_id: str | None
    idempotency_key: str


class NotificationListResponse(BaseModel):
    """A bounded page of notifications, newest slot first."""

    items: list[NotificationView]
    count: int
    #: Echoed so a caller knows whether it asked for one contract's deliveries or
    #: the whole tenant's.
    contract_id: int | None


class ScheduleNotificationRequest(BaseModel):
    """Which contract to schedule for, and optionally which decision to use.

    ``decision_id`` is optional and defaults to the contract's newest decision. It is
    offered because a decision is history: after a re-run, an operator may still
    want to schedule from the decision that was reviewed, and silently substituting
    the newest one would make that impossible to express.
    """

    contract_id: int = Field(gt=0)
    decision_id: int | None = Field(default=None, gt=0)


class DispatchResponse(BaseModel):
    """What one dispatch attempt did, plus the row as it now stands.

    ``sent`` is returned rather than inferred from ``status`` because a deferral and
    a failure both leave the row ``pending``, and a caller should not have to read
    ``last_error`` prose to tell them apart.
    """

    notification: NotificationView
    sent: bool
    #: Set when quiet hours moved the attempt to the window opening.
    deferred_until: datetime | None
    #: Set when a transport was reached and failed.
    error: str | None


def _view(row: Notification) -> NotificationView:
    """Serialize one row, re-attaching UTC to the naive datetimes D2-01 stores."""
    return NotificationView(
        id=row.id,
        contract_id=row.contract_id,
        channel=row.channel,
        template=row.template,
        status=row.status,
        scheduled_date=row.scheduled_date,
        scheduled_at=as_utc(row.scheduled_at),
        sent_at=as_utc(row.sent_at),
        attempts=row.attempts,
        last_error=row.last_error,
        provider_message_id=row.provider_message_id,
        idempotency_key=row.idempotency_key,
    )


def _recipient_for(channel: Channel, fields: Sequence[ExtractedField]) -> str:
    """The approved value this channel would contact, or raise.

    Deliberately the same rule F9 used to choose the channel (``USABLE_FIELD_STATUSES``
    plus a present ``normalized_value``): if this function could accept a field the
    decision rejected, the send would go somewhere the decision never approved.

    Raises rather than returning ``None`` so the caller in
    :func:`~app.workers.dispatch.dispatch_notification` has one failure to map onto
    ``RECIPIENT_UNAVAILABLE``, and so a missing value cannot be mistaken for an empty
    string address.
    """
    wanted = CHANNEL_CONTACT_FIELDS[channel]
    for field in fields:
        if (
            field.field_name is wanted
            and field.status in USABLE_FIELD_STATUSES
            and field.normalized_value
        ):
            return field.normalized_value
    raise LookupError(f"no usable {wanted.value} value for channel {channel.value}")


def _load_notification(
    session: Session, principal: Principal, notification_id: int
) -> Notification:
    """One row, tenant-scoped; a row from another organisation is a 404.

    404 rather than 403 for the reason every other route here uses: a 403 confirms
    the id exists, which is the enumeration oracle IDOR/BOLA tests look for.
    """
    row = NotificationRepository(session, principal.tenant_id).get(Notification, notification_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    return row


def _load_contract(session: Session, principal: Principal, contract_id: int) -> Contract:
    """The contract a notification belongs to, tenant-scoped."""
    contract = ContractRepository(session, principal.tenant_id).get(Contract, contract_id)
    if contract is None:
        # Unreachable through the API — the row's own FK and the tenant predicate
        # already agree — but a missing contract must not become an AttributeError
        # on ``contract.timezone``.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contract not found")
    return contract


@router.get(
    "",
    response_model=NotificationListResponse,
    summary="List notifications",
)
def list_notifications(
    contract_id: int | None = Query(default=None, gt=0),
    notification_status: NotificationStatus | None = Query(default=None, alias="status"),
    channel: Channel | None = Query(default=None),
    limit: int = Query(default=DEFAULT_NOTIFICATION_LIMIT, ge=1, le=MAX_NOTIFICATION_LIMIT),
    principal: Principal = Depends(
        require_roles(UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER)
    ),
    session: Session = Depends(get_request_session),
) -> NotificationListResponse:
    """This tenant's notifications, newest slot first.

    Viewer is allowed because a notification row carries no recipient value: what it
    records is *that* a contact was attempted, on which channel, for which milestone,
    and how it ended. Reading who it went to is the review reveal route's job, which
    is audited precisely because it is the exception.

    The filters are applied after the tenant predicate and never replace it, so
    ``?contract_id=`` from another organisation returns an empty list rather than
    somebody else's outbound messages.
    """
    repository = NotificationRepository(session, principal.tenant_id)
    rows = (
        repository.for_contract(contract_id, limit=limit)
        if contract_id is not None
        else repository.recent(limit=limit, status=notification_status, channel=channel)
    )
    # ``status``/``channel`` are ignored when a contract is named: the
    # ``for_contract`` path is the one the contract detail screen uses, and quietly
    # narrowing it by a stale query parameter would hide rows it should show.
    items = [_view(row) for row in rows]
    return NotificationListResponse(items=items, count=len(items), contract_id=contract_id)


@router.post(
    "",
    response_model=NotificationView,
    status_code=status.HTTP_201_CREATED,
    summary="Schedule the due reminder for one contract",
    responses={409: {"description": "Policy refused, or the slot already exists"}},
)
def schedule_notification(
    payload: ScheduleNotificationRequest,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
    settings: Settings = Depends(app_settings),
) -> NotificationView:
    """Create the pending notification for the milestone that is due right now.

    201 rather than 200: a row is created. Scheduling is idempotent *per slot* — a
    second call for the same milestone is a 409 naming the existing row, not a
    second row, because the unique ``(contract_id, template, scheduled_date)``
    constraint is the guard and this route only translates its verdict.

    Viewer is refused. Scheduling is the first action that decides a customer will
    be contacted; it sends nothing, but it commits the system to sending, which is
    the operator-only line F6/D6-01 drew.
    """
    contract = _load_contract(session, principal, payload.contract_id)
    decisions = AgentDecisionRepository(session, principal.tenant_id)

    if payload.decision_id is not None:
        decision = decisions.get(AgentDecision, payload.decision_id)
        if decision is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Agent decision not found"
            )
        if decision.contract_id != contract.id:
            # A 409 rather than a 422: both ids are valid, the pairing is not. Saying
            # so is what stops a caller from silently scheduling contract A's
            # decision against contract B.
            raise refusal(
                DispatchRefusedError(
                    REFUSAL_DECISION_MISMATCH,
                    "That agent decision belongs to a different contract",
                    details={
                        "contract_id": contract.id,
                        "decision_contract_id": decision.contract_id,
                    },
                )
            )
    else:
        decision = decisions.latest_for_contract(contract.id)
        if decision is None:
            raise refusal(
                DispatchRefusedError(
                    REFUSAL_NO_DECISION,
                    "This contract has no agent decision yet, so there is nothing to dispatch from",
                    details={"contract_id": contract.id},
                )
            )

    fields = ExtractedFieldRepository(session, principal.tenant_id).for_contract(contract.id)
    try:
        row = build_notification(
            contract, decision, now=utc_now(), settings=settings, fields=fields
        )
    except DispatchRefusedError as exc:
        logger.info(
            "notification_schedule_refused",
            contract_id=contract.id,
            decision_id=decision.id,
            reason=exc.code,
            **exc.details,
        )
        raise refusal(exc) from None

    repository = NotificationRepository(session, principal.tenant_id)
    existing = repository.find_slot(
        contract_id=contract.id, template=row.template, scheduled_date=row.scheduled_date
    )
    if existing is not None:
        raise refusal(_slot_taken(existing))

    try:
        stored = repository.add(row)
        session.commit()
    except IntegrityError:
        # The read above and this insert are not one atomic step, so two concurrent
        # schedulers can both pass the check. The constraint is what actually
        # prevents the duplicate; this branch turns losing that race into the same
        # 409 the loser would have got had it arrived second.
        session.rollback()
        raced = repository.find_slot(
            contract_id=contract.id, template=row.template, scheduled_date=row.scheduled_date
        )
        raise refusal(_slot_taken(raced)) from None

    session.refresh(stored)
    logger.info(
        "notification_scheduled",
        notification_id=stored.id,
        contract_id=stored.contract_id,
        channel=stored.channel.value,
        template=stored.template,
        scheduled_date=stored.scheduled_date.isoformat(),
        deferred=stored.scheduled_at is not None,
    )
    return _view(stored)


def _slot_taken(existing: Notification | None) -> DispatchRefusedError:
    """The 409 for a milestone slot that is already occupied.

    ``existing`` may be ``None`` in the lost-race branch, when the winning row is
    not visible to this transaction's snapshot yet. The refusal is the same either
    way — the caller must not retry blindly — so it does not depend on the row.
    """
    details: dict[str, Any] = {}
    if existing is not None:
        details = {"notification_id": existing.id, "status": existing.status.value}
    return DispatchRefusedError(
        REFUSAL_NOTIFICATION_EXISTS,
        "A notification already exists for this contract and milestone",
        details=details,
    )


@router.post(
    "/{notification_id}/dispatch",
    response_model=DispatchResponse,
    summary="Attempt to send one pending notification",
    responses={409: {"description": "Policy refused: the message may not be sent"}},
)
def dispatch(
    notification_id: int,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
    settings: Settings = Depends(app_settings),
) -> DispatchResponse:
    """Attempt the send and report what happened.

    Three outcomes, and the status code distinguishes only two of them:

    * **Sent, or a transport failure** — 200 with ``sent`` true or false. A
      transport failure is a *recorded attempt*, not a client error: the row now
      carries ``attempts``/``last_error`` and retrying is a separate decision.
    * **Deferred** — 200 with ``deferred_until`` set. Quiet hours are the system
      working, so answering 409 would train a caller to retry through the night.
    * **Refused** — 409 with a machine-readable code. Consent withdrawn, an opt-out,
      a rejected field, a spent retry budget, or no transport configured.

    The row is committed on the refusal path too, because the policy can change it
    before refusing: a spent retry budget parks the row ``failed`` and that
    transition must survive the 409.
    """
    notification = _load_notification(session, principal, notification_id)
    contract = _load_contract(session, principal, notification.contract_id)
    fields = ExtractedFieldRepository(session, principal.tenant_id).for_contract(contract.id)
    sender: MessageSender = build_sender(settings)

    def resolve_recipient() -> str:
        """Read the approved value late — only once the gates have all passed."""
        return _recipient_for(notification.channel, fields)

    try:
        outcome = dispatch_notification(
            notification,
            contract,
            sender,
            resolve_recipient,
            now=utc_now(),
            settings=settings,
            fields=fields,
        )
    except DispatchRefusedError as exc:
        session.commit()
        logger.info(
            "notification_dispatch_refused",
            notification_id=notification.id,
            contract_id=contract.id,
            reason=exc.code,
            **exc.details,
        )
        raise refusal(exc) from None

    session.commit()
    session.refresh(notification)
    logger.info(
        "notification_dispatched",
        notification_id=notification.id,
        contract_id=notification.contract_id,
        channel=notification.channel.value,
        sent=outcome.sent,
        deferred=outcome.deferred_until is not None,
        attempts=notification.attempts,
        # The failure text only. Never the recipient: this is a log sink, and the
        # row's own provider id is enough to chase a delivery with the provider.
        error=outcome.error,
    )
    return _dispatch_response(notification, outcome)


def _dispatch_response(notification: Notification, outcome: DispatchOutcome) -> DispatchResponse:
    """Build the response, re-attaching UTC to the deferral instant."""
    return DispatchResponse(
        notification=_view(notification),
        sent=outcome.sent,
        deferred_until=as_utc(outcome.deferred_until),
        error=outcome.error,
    )


@router.post(
    "/{notification_id}/retry",
    response_model=NotificationView,
    summary="Clear a parked notification's retry budget",
    responses={409: {"description": "The row is not in a retryable state"}},
)
def retry(
    notification_id: int,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
) -> NotificationView:
    """Reset a ``failed`` row to ``pending`` with a fresh attempt budget.

    Separate from ``/dispatch`` on purpose: dispatching a parked row is refused by
    the policy (``ATTEMPTS_EXHAUSTED``), and lifting that refusal is an operator
    decision. Folding it into dispatch would give every transient network blip an
    unlimited retry loop and no way to tell a stuck row from a retried one.

    ``sent_at``/``provider_message_id`` are left alone — a row that reached a
    transport once should keep that evidence even after it is requeued for a second
    attempt, which is the same choice ``outbox`` replay makes for ``last_error``.
    """
    notification = _load_notification(session, principal, notification_id)
    if notification.status is not NotificationStatus.FAILED:
        raise refusal(
            DispatchRefusedError(
                REFUSAL_NOT_FAILED,
                f"This notification is {notification.status.value}; only a failed "
                "notification is retried",
                details={"status": notification.status.value},
            )
        )

    notification.status = NotificationStatus.PENDING
    notification.attempts = 0
    notification.last_error = None
    session.commit()
    session.refresh(notification)
    logger.info(
        "notification_requeued",
        notification_id=notification.id,
        contract_id=notification.contract_id,
        channel=notification.channel.value,
        template=notification.template,
    )
    return _view(notification)
