"""Agent-decision persistence (F9).

``AgentDecision`` has existed since the F2 schema and had no writer until the
channel-selection policy landed: the table was a claim about what the system
would store, not a record of anything it stored. This module is the writer, and
it is deliberately thin — the *policy* lives in :mod:`app.workers.decision` and
the route only composes the two, the same split F7 used for extraction.

Two shapes worth stating:

* **Decisions are history, not state.** There is no update path and no unique
  constraint per contract: re-running the decision appends a row, so "why did we
  email them in March?" is answerable after the consent flags have changed. The
  newest row is what a caller acts on, and :meth:`latest_for_contract` is how it
  is found without a second table.
* **Reads are tenant-scoped, writes are tenant-stamped.** Both come free from
  :class:`~app.db.repositories.base.TenantScopedRepository`, which is why a
  contract id belonging to another organisation yields ``None`` rather than a
  decision.
"""

from __future__ import annotations

from app.db.enums import Channel
from app.db.models import AgentDecision
from app.db.repositories.base import TenantScopedRepository

#: How much decision history a listing returns when the caller does not say.
#: Bounded at the query, so no caller can ask for a whole tenant's decision table.
DEFAULT_DECISION_HISTORY_LIMIT = 50
MAX_DECISION_HISTORY_LIMIT = 200


class AgentDecisionRepository(TenantScopedRepository[AgentDecision]):
    """Tenant-scoped access to one organisation's stored agent decisions."""

    def for_contract(
        self, contract_id: int, *, limit: int = DEFAULT_DECISION_HISTORY_LIMIT
    ) -> list[AgentDecision]:
        """One contract's decisions, newest first.

        ``contract_id`` is not trusted on its own: the tenant predicate from
        :meth:`~app.db.repositories.base.TenantScopedRepository.scoped` is applied
        as well, so a guessed id from another tenant returns an empty list rather
        than someone else's decisions (IDOR/BOLA).

        ``id`` breaks the ``created_at`` tie deliberately. ``created_at`` is a
        ``DATETIME`` without fractional seconds, so MySQL rounds it and two
        decisions made in the same second store the *same* timestamp. Without the
        tiebreak they would come back in whatever order the database chose — and
        "the newest decision" has to be one row, not two candidates.
        """
        stmt = (
            self.scoped(AgentDecision)
            .where(AgentDecision.contract_id == contract_id)
            .order_by(AgentDecision.created_at.desc(), AgentDecision.id.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars())

    def recent(self, *, limit: int = DEFAULT_DECISION_HISTORY_LIMIT) -> list[AgentDecision]:
        """The newest decisions across this tenant, newest first.

        The dashboard view needs "what did the agent decide lately" without naming
        a contract. Same ordering and same bound as :meth:`for_contract`, so the two
        listings cannot disagree about which row is newest.
        """
        stmt = (
            self.scoped(AgentDecision)
            .order_by(AgentDecision.created_at.desc(), AgentDecision.id.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars())

    def latest_for_contract(self, contract_id: int) -> AgentDecision | None:
        """The most recent decision for one contract, or ``None`` if there is none."""
        rows = self.for_contract(contract_id, limit=1)
        return rows[0] if rows else None

    def create(
        self,
        *,
        contract_id: int,
        correlation_id: str,
        channel: Channel,
        confidence: float,
        reasoning: str,
        prompt_version: str,
        llm_model: str,
        needs_review: bool = False,
    ) -> AgentDecision:
        """Append one decision row.

        ``reasoning`` arrives already redacted by the caller: the policy builds a
        fixed sentence from channel names and consent, never from document text or
        a field value, because this column is plain text that any operator reads
        (the model docstring explains why the row holds no free-form model output).

        ``tenant_id`` is stamped by ``add`` from the repository, so no argument
        here can aim a decision at another organisation.
        """
        decision = AgentDecision(
            contract_id=contract_id,
            correlation_id=correlation_id,
            channel=channel,
            confidence=confidence,
            reasoning=reasoning,
            needs_review=needs_review,
            prompt_version=prompt_version,
            llm_model=llm_model,
        )
        return self.add(decision)
