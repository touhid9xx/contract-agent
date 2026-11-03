"""Agent decisions over HTTP (F9).

Two routes, and one rule that shapes both:

* ``POST /agent/decisions`` — run the channel-selection policy for one contract and
  store what it decided. This is the first caller of an ``approved`` field outside
  the review screens, and therefore the first thing in the repo that could put a
  customer back in front of the pipeline.
* ``GET /agent/decisions`` — read the stored decisions, newest first.

What this layer does **not** do, on purpose: it contains no policy. It loads the
contract and its fields, hands them to :mod:`app.workers.decision`, and turns a
refusal into a response. The split is the one F7 established for extraction —
policy in ``app.workers``, transport here — so the rule that decides who gets
contacted can be tested without a database or an HTTP client.

Decisions are not audited through ``AuditLog``. An ``AgentDecision`` row is itself
append-only, carries ``correlation_id``, and states its ``prompt_version``; a
second audit row saying "a decision was created" would duplicate the record it
narrates. ``AuditLog`` stays for human edits (F8), which change rows that were
already there.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_request_session, require_roles
from app.core.middleware import correlation_id_or_new
from app.core.principal import Principal
from app.db.base import as_utc
from app.db.enums import Channel, UserRole
from app.db.models import AgentDecision, Contract
from app.db.repositories import (
    AgentDecisionRepository,
    ContractRepository,
    ExtractedFieldRepository,
)
from app.db.repositories.agent import (
    DEFAULT_DECISION_HISTORY_LIMIT,
    MAX_DECISION_HISTORY_LIMIT,
)
from app.workers.decision import DecisionRefusedError, build_decision

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
    #: Which policy produced this row. Named so a decision made under an older rule
    #: set can be told apart from one made today.
    prompt_version: str
    #: ``deterministic`` for this slice: no model was consulted (D9-02).
    llm_model: str
    correlation_id: str
    created_at: datetime


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
    )


def _refusal(exc: DecisionRefusedError) -> HTTPException:
    """Turn a policy refusal into a 409 that says *why* in machine-readable form.

    All five refusals share one status code because they are all "this contract
    cannot be decided on right now", but the ``code`` distinguishes them: the
    Phase-0 UI already branches on ``PIPELINE_BLOCKED``, and an operator needs to
    know whether to review a field, record consent, or chase a contact detail.
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
        raise _refusal(exc) from None

    stored = AgentDecisionRepository(session, principal.tenant_id).add(decision)
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
    )
    return _view(stored)
