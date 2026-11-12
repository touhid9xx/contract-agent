"""Agent decisions over HTTP (F9), including operator corrections (gap 19).

Four routes, and one rule that shapes the first two:

* ``POST /agent/decisions`` — run the channel-selection policy for one contract and
  store what it decided. This is the first caller of an ``approved`` field outside
  the review screens, and therefore the first thing in the repo that could put a
  customer back in front of the pipeline. Re-running it is how a contract gets a
  new recommendation: it **appends**, it never edits.

  The policy runs first and alone decides eligibility. Only when it has already
  produced a permitted channel — and only on a deployment with ``AGENT_BACKEND=ollama``
  — does the local model get to re-point that choice, and it may only pick from the
  channels the policy offered (``app.workers.advisory``, D10-88).
* ``GET /agent/decisions`` — read the stored decisions, newest first.
* ``POST /agent/decisions/{contract_id}/override`` — a person replaces the channel
  the policy chose, choosing another channel those same rules permit.
* ``POST /agent/decisions/{contract_id}/flag-review`` — a person marks the current
  recommendation as needing a human look, which is not decorative:
  ``dispatch`` refuses a flagged decision (``NEEDS_REVIEW``).

The last two are **corrections**, so unlike a re-run they edit the newest row in
place, under the ``version`` lock ``0003`` added and ``0004`` gave something to
lock. They are the only two writes in this module that were made by a human, and
therefore the only two that write ``AuditLog`` — a created decision already *is*
an append-only record of what the policy said, while an override changes a row
that already existed and needs a separate account of who changed it.

Both are addressed by **contract**, not by decision id, matching the Phase-0 demo
(``demo/backend/main.py``) and the demo client that already calls these paths. An
operator correcting "the recommendation for this contract" should not have to hold
a row id, and the current recommendation is exactly ``latest_for_contract`` —
history stays readable and is never rewritten.

What this layer does **not** do, on purpose: it contains no policy. It loads the
contract and its fields, hands them to :mod:`app.workers.decision`, and turns a
refusal into a response. The split is the one F7 established for extraction —
policy in ``app.workers``, transport here — so the rule that decides who gets
contacted can be tested without a database or an HTTP client. The override route
reuses :func:`~app.workers.decision.permitted_channels` rather than re-deriving
who may be contacted, because a second copy of that rule is how an operator path
and a policy path start disagreeing about consent.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api.deps import app_settings, get_request_session, require_roles
from app.core.config import Settings
from app.core.middleware import correlation_id_or_new
from app.core.principal import Principal
from app.db.base import as_utc
from app.db.enums import Channel, UserRole
from app.db.models import AgentDecision, Contract
from app.db.repositories import (
    AgentDecisionRepository,
    AuditLogRepository,
    ContractRepository,
    ExtractedFieldRepository,
)
from app.db.repositories.agent import (
    DEFAULT_DECISION_HISTORY_LIMIT,
    MAX_DECISION_HISTORY_LIMIT,
)
from app.db.repositories.audit import ACTOR_USER, AUDIT_ENTITY_DECISION
from app.workers.advisory import (
    ChannelAdvisor,
    CircuitBreaker,
    advise_decision,
    build_advisor,
    default_breaker,
)
from app.workers.decision import (
    REFUSAL_CHANNEL_NOT_PERMITTED,
    DecisionRefusedError,
    build_decision,
    permitted_channels,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])


class AgentDecisionView(BaseModel):
    """One stored decision, as an operator reads it.

    Every field here is safe to show a viewer: the reasoning is a fixed sentence
    built from channel names, and no extracted value appears anywhere in the row.
    That is why reading decisions needs no reveal step, while reading the value a
    decision rests on does (``POST /review/{id}/reveal``).
    """

    id: int
    contract_id: int
    channel: Channel
    confidence: float
    reasoning: str
    needs_review: bool
    #: Which ruleset produced this row, and whether a model advised on it. Named so a
    #: decision made under an older rule set can be told apart from one made today.
    prompt_version: str
    #: ``deterministic`` unless ``AGENT_BACKEND=ollama``, in which case the grounded
    #: decision was re-pointed by that local model and the model name is recorded
    #: here. Never a hosted model: config refuses any other backend, and the model is
    #: advisory only — it cannot widen the channels consent permits (D9-02, D10-88).
    llm_model: str
    correlation_id: str
    created_at: datetime
    #: Optimistic-lock generation: send it back to override or flag-review, and a
    #: stale value means someone else corrected this row first (409).
    version: int
    #: The channel the *policy* chose, once a person has overridden it. ``None``
    #: means nobody has, which is deliberately not the same as "equals ``channel``".
    #: Filled in once and never rewritten, so a second override still reports the
    #: policy's original answer rather than the previous operator's.
    original_channel: Channel | None
    #: Who last replaced the channel. ``None`` alongside a set ``original_channel``
    #: would mean a row edited before this column existed, not a machine override.
    overridden_by: uuid.UUID | None
    #: How many recommendations already existed for this contract when this row was
    #: appended: ``0`` for the first, ``1`` for the first re-run. Lets a reader tell
    #: a considered decision from a repeatedly re-rolled one.
    rerun_count: int


class AgentDecisionListResponse(BaseModel):
    """A bounded page of decisions, newest first."""

    items: list[AgentDecisionView]
    count: int
    #: Echoed so a caller reading a contract's history knows the list is complete;
    #: ``False`` means older decisions exist and the limit was reached.
    contract_id: int | None


class AgentDecisionRequest(BaseModel):
    """Which contract to decide about.

    A body rather than a path segment: the decision is appended, not addressed, and
    the same contract legitimately accumulates several over its life.
    """

    contract_id: int = Field(gt=0)


class DecisionOverrideRequest(BaseModel):
    """A person replaces the channel the policy chose.

    Separate from :class:`FlagReviewRequest` rather than one model with an ``action``
    discriminator: the two corrections need different fields, and two models make
    "flag-review carrying a channel" unrepresentable instead of merely rejected.
    """

    #: Must be among the channels :func:`~app.workers.decision.permitted_channels`
    #: allows for this contract *now*. An override narrows or redirects a permitted
    #: choice; it can never create permission the customer did not give.
    channel: Channel
    #: The ``version`` last read. Optimistic concurrency: two operators overriding
    #: from stale views must not silently overwrite each other.
    version: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=500)


class FlagReviewRequest(BaseModel):
    """A person marks the current recommendation as needing review.

    ``needs_review`` is read by ``dispatch``, which refuses to send a flagged
    decision, so this is the supported way to take a recommendation out of the
    send path without deleting anything.
    """

    version: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=500)


def _view(decision: AgentDecision) -> AgentDecisionView:
    """Serialize one row.

    ``created_at`` is re-attached to UTC at this boundary rather than stored aware —
    MySQL's ``DATETIME`` discards the offset, so ``as_utc`` is the single place the
    timezone is put back (D2-01).
    """
    return AgentDecisionView(
        id=decision.id,
        contract_id=decision.contract_id,
        channel=decision.channel,
        confidence=float(decision.confidence),
        reasoning=decision.reasoning,
        needs_review=decision.needs_review,
        prompt_version=decision.prompt_version,
        llm_model=decision.llm_model,
        correlation_id=decision.correlation_id,
        created_at=as_utc(decision.created_at) or decision.created_at,
        version=decision.version,
        original_channel=decision.original_channel,
        overridden_by=decision.overridden_by,
        rerun_count=decision.rerun_count,
    )


def _decision_state(decision: AgentDecision) -> dict[str, Any]:
    """What an audit row records about one decision, before or after an edit.

    Channel names and flags only. ``reasoning`` is excluded on purpose — it is a
    fixed sentence built from channel names today, but the audit log must not start
    depending on a free-text column that a future policy version might enrich.
    """
    return {
        "channel": decision.channel.value,
        "original_channel": (
            decision.original_channel.value if decision.original_channel is not None else None
        ),
        "needs_review": decision.needs_review,
        "version": decision.version,
    }


def refusal(exc: DecisionRefusedError) -> HTTPException:
    """Turn a policy refusal into a 409 that says *why* in machine-readable form.

    All five refusals share one status code because they are all "this contract
    cannot be decided on right now", but the ``code`` distinguishes them: the
    Phase-0 UI already branches on ``PIPELINE_BLOCKED``, and an operator needs to
    know whether to review a field, record consent, or chase a contact detail.

    Public rather than module-private because the dispatch router maps its own
    refusals through the identical envelope — :class:`~app.workers.dispatch.
    DispatchRefusedError` subclasses the decision error, so one helper covering
    both is what stops the two routers from drifting into two error vocabularies.
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": exc.code, "message": exc.message, "details": exc.details or None},
    )


@router.get(
    "/decisions",
    response_model=AgentDecisionListResponse,
    summary="List stored agent decisions",
)
def list_decisions(
    contract_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=DEFAULT_DECISION_HISTORY_LIMIT, ge=1, le=MAX_DECISION_HISTORY_LIMIT),
    principal: Principal = Depends(
        require_roles(UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER)
    ),
    session: Session = Depends(get_request_session),
) -> AgentDecisionListResponse:
    """Decision history for one contract, or the tenant's most recent decisions.

    Viewer is allowed: nothing here is a customer value. A ``contract_id`` from
    another tenant returns an empty list rather than a 404, because the query is
    tenant-scoped before the id is applied — which is also why it cannot be used to
    probe which contract ids exist.
    """
    repository = AgentDecisionRepository(session, principal.tenant_id)
    rows = (
        repository.for_contract(contract_id, limit=limit)
        if contract_id is not None
        else repository.recent(limit=limit)
    )
    items = [_view(row) for row in rows]
    return AgentDecisionListResponse(items=items, count=len(items), contract_id=contract_id)


def advisor_for(settings: Settings = Depends(app_settings)) -> ChannelAdvisor | None:
    """The model client, or ``None`` when this deployment has no model.

    ``None`` is the whole of the deterministic default's safety: with no advisor there
    is no call to make, so ``AGENT_BACKEND=deterministic`` has no path to a model even
    by accident. ``app.core.config`` already refuses any backend other than a local
    one, so a hosted model is unrepresentable rather than merely discouraged.
    """
    if settings.agent_backend != "ollama":
        return None
    return build_advisor(settings)


@router.post(
    "/decisions",
    response_model=AgentDecisionView,
    status_code=status.HTTP_201_CREATED,
    summary="Decide the contact channel for one contract",
    responses={409: {"description": "Policy refused: the contract cannot be decided on"}},
)
def decide_channel(
    payload: AgentDecisionRequest,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
    settings: Settings = Depends(app_settings),
    advisor: ChannelAdvisor | None = Depends(advisor_for),
    breaker: CircuitBreaker = Depends(default_breaker),
) -> AgentDecisionView:
    """Run the channel policy and append the decision.

    201 rather than 200: a row was created, and it is never an update — a re-run
    appends a second decision so the history stays readable.

    Viewer is refused. The decision itself sends nothing, but it is the row a
    dispatcher acts on, and the operator-only rule from F6/D6-01 covers every write
    that moves a contract toward a customer.

    A contract belonging to another tenant is a 404, not a 403 (IDOR/BOLA), for the
    same reason as the review routes.
    """
    contract = ContractRepository(session, principal.tenant_id).get(Contract, payload.contract_id)
    if contract is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contract not found")

    fields = ExtractedFieldRepository(session, principal.tenant_id).for_contract(contract.id)
    try:
        decision = build_decision(contract, fields, correlation_id=correlation_id_or_new())
    except DecisionRefusedError as exc:
        # Not an error to log at warning level: every closed contract and every
        # opted-out customer takes this path, and the refusal is the policy working.
        logger.info(
            "agent_decision_refused",
            contract_id=contract.id,
            reason=exc.code,
            **exc.details,
        )
        raise refusal(exc) from None

    if advisor is not None:
        # Advisory only, and strictly after the policy: ``build_decision`` has already
        # proved this contract is contactable and chosen a permitted channel. The model
        # may re-point that choice among the channels consent already allows. It cannot
        # widen the set, and every failure path leaves ``needs_review`` set so
        # ``dispatch`` refuses the row (D9-02, spec §7).
        decision = advise_decision(
            decision,
            contract,
            fields,
            settings=settings,
            advisor=advisor,
            breaker=breaker,
        )

    repository = AgentDecisionRepository(session, principal.tenant_id)
    # Counted before the row is added, so the first decision records 0 and a re-run
    # records the history it is being appended to. This is what makes "re-rolled
    # four times" visible from the row itself rather than only from the history list.
    decision.rerun_count = repository.count_for_contract(contract.id)
    stored = repository.add(decision)
    session.commit()
    # Read after commit so the response reports the stored row, including the id and
    # the timestamp the database assigned.
    session.refresh(stored)
    logger.info(
        "agent_decision_recorded",
        contract_id=stored.contract_id,
        decision_id=stored.id,
        channel=stored.channel.value,
        confidence=float(stored.confidence),
        policy=stored.prompt_version,
        model=stored.llm_model,
        needs_review=stored.needs_review,
        rerun_count=stored.rerun_count,
    )
    return _view(stored)


def _current_decision(session: Session, principal: Principal, contract_id: int) -> AgentDecision:
    """The recommendation an operator is correcting.

    409 with ``NO_DECISION`` rather than 404: the contract exists (the caller checked),
    there is simply nothing to correct yet, and the fix is to run the decision first.
    """
    decision = AgentDecisionRepository(session, principal.tenant_id).latest_for_contract(
        contract_id
    )
    if decision is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "NO_DECISION",
                "message": "No decision has been made for this contract yet",
                "details": None,
            },
        )
    return decision


def _require_version(decision: AgentDecision, version: int) -> None:
    """Reject a correction built from a stale read.

    The same envelope the consent route uses, including the current row in
    ``details`` so a client that lost the race can re-render instead of guessing
    what changed underneath it.
    """
    if decision.version != version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "VERSION_CONFLICT",
                "message": "This decision changed since you read it; reload and try again",
                "details": {"current": _view(decision).model_dump(mode="json")},
            },
        )


def _commit_correction(
    session: Session,
    principal: Principal,
    decision: AgentDecision,
    *,
    action: str,
    before: dict[str, Any],
    reason: str | None,
) -> None:
    """Flush the edit, audit it, and commit — translating a lost race into a 409.

    The ``version`` predicate is only checked when the ``UPDATE`` is emitted, so a
    competing writer is detected here at flush time rather than by ``_require_version``.
    ``version_id_col`` turns that into ``StaleDataError``, which is the honest answer
    to "someone else got there first" — not a 500.
    """
    try:
        # Flush before auditing: the audit row records the ``after`` state, and the
        # version bump only exists in the database after the UPDATE is emitted — so
        # ``after`` is read here, not captured by the caller before the flush.
        session.flush()
        after = _decision_state(decision)
        AuditLogRepository(session, principal.tenant_id).record(
            correlation_id=correlation_id_or_new(),
            actor_type=ACTOR_USER,
            actor_id=principal.user_id,
            action=action,
            entity=AUDIT_ENTITY_DECISION,
            entity_id=str(decision.id),
            before=before,
            after=after,
            reason=reason,
        )
        session.commit()
    except StaleDataError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "VERSION_CONFLICT",
                "message": "This decision changed since you read it; reload and try again",
                "details": None,
            },
        ) from None
    session.refresh(decision)


def _require_contract(session: Session, principal: Principal, contract_id: int) -> Contract:
    """The caller's contract, or a 404.

    Both corrections are addressed by contract, so both need this — and both need
    the *same* answer for someone else's id. A 404 rather than a 403, for the reason
    the review routes give: a 403 would confirm the id exists (IDOR/BOLA).
    """
    contract = ContractRepository(session, principal.tenant_id).get(Contract, contract_id)
    if contract is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contract not found")
    return contract


@router.post(
    "/decisions/{contract_id}/override",
    response_model=AgentDecisionView,
    summary="Replace the channel the policy chose",
    responses={
        409: {"description": "Stale version, no decision yet, or a channel that is not permitted"}
    },
)
def override_decision(
    contract_id: int,
    payload: DecisionOverrideRequest,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
) -> AgentDecisionView:
    """A person picks a different channel for the current recommendation.

    The requested channel must be one :func:`~app.workers.decision.permitted_channels`
    already allows **now**, re-evaluated at override time rather than trusted from
    the decision row. That matters because the row is a snapshot: consent may have
    been withdrawn, or a field newly un-approved, since the policy ran. Reusing the
    policy's own gate set is therefore not a convenience — an override that created
    permission would be a consent bypass wearing an operator's badge.

    ``original_channel`` is filled in once, on the first override, and left alone
    afterwards, so a decision overridden twice still reports what the *policy* said
    rather than the previous operator's choice. Older decision rows are untouched:
    the correction is a fact about this recommendation, not a new recommendation.
    """
    contract = _require_contract(session, principal, contract_id)
    decision = _current_decision(session, principal, contract.id)
    _require_version(decision, payload.version)

    fields = ExtractedFieldRepository(session, principal.tenant_id).for_contract(contract.id)
    try:
        permitted = permitted_channels(contract, fields)
    except DecisionRefusedError as exc:
        # The contract has become undecidable since the decision was stored. Same
        # envelope as a fresh decision would produce, so a client branching on
        # ``code`` needs no second vocabulary for corrections.
        logger.info(
            "agent_override_refused",
            contract_id=contract.id,
            decision_id=decision.id,
            reason=exc.code,
            **exc.details,
        )
        raise refusal(exc) from None

    if payload.channel not in permitted:
        # Not a 422: the body is well-formed and the channel is real. The *pairing* of
        # this contract and this channel is what the policy forbids, so it is the same
        # 409 shape as every other "may not contact that way" answer.
        # Named ``not_permitted`` rather than ``exc``: the ``except`` binding above is
        # already deleted by the time this runs, and reusing its name makes the two
        # look related when one is a policy refusal and this is a wrong request.
        not_permitted = DecisionRefusedError(
            REFUSAL_CHANNEL_NOT_PERMITTED,
            "That channel is not permitted for this contract; an override chooses among "
            "permitted channels and never creates permission",
            details={"permitted": sorted(member.value for member in permitted)},
        )
        logger.info(
            "agent_override_refused",
            contract_id=contract.id,
            decision_id=decision.id,
            reason=not_permitted.code,
            requested=payload.channel.value,
            **not_permitted.details,
        )
        raise refusal(not_permitted) from None

    # Captured before the edit: the audit row needs the state the operator saw.
    before = _decision_state(decision)
    if decision.original_channel is None:
        decision.original_channel = decision.channel
    decision.channel = payload.channel
    decision.overridden_by = principal.user_id

    _commit_correction(
        session,
        principal,
        decision,
        action="agent_decision.overridden",
        before=before,
        reason=payload.reason,
    )
    logger.info(
        "agent_decision_overridden",
        contract_id=decision.contract_id,
        decision_id=decision.id,
        original_channel=(
            decision.original_channel.value if decision.original_channel is not None else None
        ),
        channel=decision.channel.value,
        version=decision.version,
    )
    return _view(decision)


@router.post(
    "/decisions/{contract_id}/flag-review",
    response_model=AgentDecisionView,
    summary="Flag the current recommendation for a human look",
    responses={409: {"description": "Stale version, or no decision has been made yet"}},
)
def flag_review_decision(
    contract_id: int,
    payload: FlagReviewRequest,
    principal: Principal = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
    session: Session = Depends(get_request_session),
) -> AgentDecisionView:
    """Take the current recommendation out of the send path without deleting it.

    No policy gate runs here, deliberately. Every refusal in
    :func:`~app.workers.decision.permitted_channels` answers "may we contact this
    customer?", and flagging is the opposite request — it *stops* a send. Requiring
    consent in order to express doubt about a send would make the one safe action
    unavailable exactly when the contract looks worst.

    Flagging sets ``needs_review``, which ``dispatch`` already refuses on
    (``NEEDS_REVIEW``), so it needs no new column and no new refusal code: the
    column existed since F2 and was simply unreachable by a person.
    """
    contract = _require_contract(session, principal, contract_id)
    decision = _current_decision(session, principal, contract.id)
    _require_version(decision, payload.version)

    before = _decision_state(decision)
    decision.needs_review = True

    _commit_correction(
        session,
        principal,
        decision,
        action="agent_decision.flagged",
        before=before,
        reason=payload.reason,
    )
    logger.info(
        "agent_decision_flagged",
        contract_id=decision.contract_id,
        decision_id=decision.id,
        needs_review=decision.needs_review,
        version=decision.version,
    )
    return _view(decision)
