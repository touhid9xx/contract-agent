"""Escalation policy: raising a late contract, and reading how late it is (gap 15).

``customer_responses`` and ``escalations`` have existed since the F2 schema and
had no writer until now, which is why ``ContractStatus.RESPONDED`` and
``ContractStatus.ESCALATED`` were unreachable states: nothing recorded that a
customer had answered, and nothing opened the clock that says a human is late.

Two clocks, and this module owns both readings of them.

* **The escalation clock.** ``DECISIONS.md`` Q2 fixes the SLA at **48 hours after
  the escalation is raised** — not 48 hours after the reminder went out. The
  Phase-0 demo splits the same way (a two-week no-response window raises the
  escalation, then ``sla_hours: 48``). :func:`sla_deadline` is that arithmetic,
  kept in one place so the route that raises an escalation and the queue that
  renders it cannot disagree about when a row is late.
* **Whether a row is late.** :func:`is_overdue` compares the stored deadline
  against a caller-supplied ``now`` and is **derived, never stored**. This slice
  writes no background job, so nothing flips ``EscalationStatus.SLA_BREACHED``
  on its own; the queue reports ``overdue`` and an operator sees it. That limit
  is recorded in ``STATUS.md`` rather than papered over with a write-on-read.

The refusals live here rather than in the route for the reason F7 established
for extraction: a rule that decides when a contract may be escalated is then
testable without a database or an HTTP client. Nothing in this module imports a
session, and nothing here reads the clock — every function takes ``now``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.db.enums import ContractStatus, EscalationStatus
from app.db.models import Contract, Escalation
from app.workers.decision import DecisionRefusedError

__all__ = [
    "DEFAULT_ESCALATION_SLA_HOURS",
    "OPEN_ESCALATION_STATUSES",
    "REFUSAL_ALREADY_ESCALATED",
    "REFUSAL_CONTRACT_CLOSED",
    "REFUSAL_ESCALATION_CLOSED",
    "REFUSAL_INVALID_TRANSITION",
    "REFUSAL_RESPONSE_RECORDED",
    "RESPONDABLE_CONTRACT_STATUSES",
    "EscalationRefusedError",
    "is_overdue",
    "refusal_to_assign",
    "refusal_to_raise",
    "refusal_to_resolve",
    "refusal_to_transition",
    "sla_deadline",
    "status_after_response",
]

#: ``DECISIONS.md`` Q2: 48 hours after the escalation is raised.
DEFAULT_ESCALATION_SLA_HOURS = 48

#: The statuses that mean "this escalation is still someone's job". ``resolved``
#: is the only state that takes a row out of the queue, which is why it is
#: defined by exclusion here rather than by listing the three open states twice.
OPEN_ESCALATION_STATUSES: tuple[EscalationStatus, ...] = (
    EscalationStatus.OPEN,
    EscalationStatus.IN_PROGRESS,
    EscalationStatus.SLA_BREACHED,
)

#: Contract states from which no further escalation is possible: the term ran
#: out or was renewed, so a human chasing a reply would be chasing a closed file.
CLOSED_CONTRACT_STATUSES: tuple[ContractStatus, ...] = (
    ContractStatus.RENEWED,
    ContractStatus.EXPIRED,
)

#: Contract states a customer reply may still move to ``responded``. A contract
#: that has already been renewed or has expired keeps that state: the reply is
#: still recorded, but "they answered after the term ended" must not erase the
#: fact that the term ended. Defined here rather than in the route so the rule is
#: readable next to the state machine it belongs to.
RESPONDABLE_CONTRACT_STATUSES: tuple[ContractStatus, ...] = (
    ContractStatus.ACTIVE,
    ContractStatus.REMINDER_SENT,
    ContractStatus.ESCALATED,
)

REFUSAL_ALREADY_ESCALATED = "ALREADY_ESCALATED"
REFUSAL_CONTRACT_CLOSED = "CONTRACT_CLOSED"
REFUSAL_RESPONSE_RECORDED = "RESPONSE_RECORDED"
REFUSAL_ESCALATION_CLOSED = "ESCALATION_CLOSED"
#: A caller asked to move an escalation into a state the queue does not store.
REFUSAL_INVALID_TRANSITION = "INVALID_TRANSITION"


class EscalationRefusedError(DecisionRefusedError):
    """A rule said no. Carries the machine-readable code the route returns.

    Subclasses :class:`~app.workers.decision.DecisionRefusedError` for the reason
    that class gives: a refusal is one shape across the policy surface — a stable
    ``code``, a prose message, and ``details`` that never contain a field value —
    so ``app.api.v1.agent.refusal`` maps it to a 409 without this module growing a
    third error vocabulary. ``details`` is inherited unchanged.
    """


def sla_deadline(raised_at: datetime, *, hours: int) -> datetime:
    """When an escalation raised at ``raised_at`` becomes late.

    Naive in, naive out: every datetime this app stores is naive UTC
    (``app.db.base.utc_now``), and mixing an aware value into a ``DateTime``
    column would compare against the database's idea of "now" unpredictably.
    """
    return raised_at + timedelta(hours=hours)


def is_overdue(escalation: Escalation, now: datetime) -> bool:
    """Whether ``escalation`` has passed its deadline without being resolved.

    Strictly ``<``: a row whose deadline is exactly ``now`` is not late yet. The
    same half-open reading ``quiet_hours_until`` uses, for the same reason — an
    instant belongs to one side of the boundary, never both.
    """
    if escalation.status not in OPEN_ESCALATION_STATUSES:
        return False
    return escalation.sla_deadline < now


def refusal_to_raise(contract: Contract, open_escalation: Escalation | None) -> None:
    """Raise :class:`EscalationRefusedError` if this contract may not be escalated.

    Order matters for the message a caller sees. An open escalation is answered
    first because it is the most actionable ("work the one you have"), then a
    contract that already replied, then one that has closed. A caller can only
    ever see one of them, and the first true one is the most useful.
    """
    if open_escalation is not None:
        raise EscalationRefusedError(
            REFUSAL_ALREADY_ESCALATED,
            "This contract already has an open escalation",
        )
    if contract.status is ContractStatus.RESPONDED:
        raise EscalationRefusedError(
            REFUSAL_RESPONSE_RECORDED,
            "The customer has already responded to this contract",
        )
    if contract.status in CLOSED_CONTRACT_STATUSES:
        raise EscalationRefusedError(
            REFUSAL_CONTRACT_CLOSED,
            f"This contract is {contract.status.value} and cannot be escalated",
        )


def refusal_to_assign(escalation: Escalation) -> None:
    """A resolved escalation is a closed file: it cannot be assigned again."""
    if escalation.status is EscalationStatus.RESOLVED:
        raise EscalationRefusedError(
            REFUSAL_ESCALATION_CLOSED,
            "This escalation is resolved; reopen the contract instead",
        )


def refusal_to_resolve(escalation: Escalation) -> None:
    """Resolving twice is refused rather than treated as a no-op.

    A second resolve would move ``resolved_at`` and overwrite the note the first
    operator left, quietly rewriting the record of who closed it and why. The
    409 is the honest answer; a no-op would make the caller believe it acted.
    """
    if escalation.status is EscalationStatus.RESOLVED:
        raise EscalationRefusedError(
            REFUSAL_ESCALATION_CLOSED,
            "This escalation is already resolved",
        )


def refusal_to_transition(escalation: Escalation, target: EscalationStatus) -> None:
    """Refuse a move the queue state machine does not have.

    Two separate rules, both about keeping the row self-consistent:

    * ``sla_breached`` is *derived* — :func:`is_overdue` answers it from the
      deadline — so letting an operator set it would create a row that claims to
      be late while its own ``sla_deadline`` says otherwise.
    * ``resolved`` is terminal. Reopening would leave ``resolved_at`` and
      ``resolution_note`` populated on a row that is open again, i.e. a queue item
      whose own columns disagree about whether it is closed. The action an operator
      actually wants here — chase this contract again — is to raise a *new*
      escalation, which the route allows and which records its own deadline.
    """
    if target is EscalationStatus.SLA_BREACHED:
        raise EscalationRefusedError(
            REFUSAL_INVALID_TRANSITION,
            "sla_breached is derived from sla_deadline and cannot be set by hand",
            details={"status": target.value},
        )
    if escalation.status is EscalationStatus.RESOLVED and target is not EscalationStatus.RESOLVED:
        raise EscalationRefusedError(
            REFUSAL_ESCALATION_CLOSED,
            "This escalation is resolved; raise a new one instead of reopening it",
            details={"status": target.value},
        )


def status_after_response(contract: Contract) -> ContractStatus | None:
    """The status a customer reply moves ``contract`` to, or ``None`` to leave it.

    Returned rather than applied so this module stays free of side effects, and so
    the route can audit the transition it actually made.
    """
    if contract.status in RESPONDABLE_CONTRACT_STATUSES:
        return ContractStatus.RESPONDED
    return None
