"""Agent decision, notification and customer-response models."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.enums import (
    Channel,
    NotificationStatus,
    ResponseType,
    enum_column,
)
from app.db.types import GUID


class AgentDecision(Base, TimestampMixin):
    """One LLM recommendation, stored redacted.

    The row intentionally contains **no** free-form model output beyond the
    capped ``reasoning`` field: raw completions can echo injected document text
    or PII, and the spec forbids persisting them. ``reasoning`` is the model's
    own short justification, validated by Pydantic before it is written.
    """

    __tablename__ = "agent_decisions"
    __table_args__ = (
        Index("ix_agent_decisions_contract_created", "contract_id", "created_at"),
        Index("ix_agent_decisions_correlation", "correlation_id"),
        Index("ix_agent_decisions_tenant_channel", "tenant_id", "channel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    channel: Mapped[Channel] = mapped_column(enum_column(Channel, length=10), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    #: Set when validation failed, the model was unavailable, or policy flagged
    #: the decision. A review item is created; nothing is dispatched.
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- provenance (never prompt content) -------------------------------
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    llm_model: Mapped[str] = mapped_column(String(50), nullable=False, default="qwen2:7b")
    temperature: Mapped[float | None] = mapped_column(Numeric(3, 2), nullable=True)
    tokens_input: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_output: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schema_repair_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mlflow_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Optimistic-lock column, matching ``Contract``, ``ExtractedField`` and
    #: ``Escalation``. It was added by ``0003`` while still inert, because a table
    #: that cannot be locked is the structural reason no override route could be
    #: written at all; the override and flag-review routes in
    #: ``app/api/v1/agent.py`` are what finally use it.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- operator correction (gap 19) ------------------------------------
    #: The channel the policy chose, preserved the first time a person overrides
    #: this decision. NULL means "no override has happened", which is why it is not
    #: backfilled with ``channel``: the two are equal until a person disagrees, and
    #: a NULL says *nobody has* rather than *somebody agreed*.
    original_channel: Mapped[Channel | None] = mapped_column(
        enum_column(Channel, length=10), nullable=True
    )
    #: The authenticated operator who overrode the channel. A plain GUID rather than
    #: a foreign key, matching ``ExtractedField.reviewed_by``: the audit log keeps
    #: the durable record of who did what, and a deleted user must not cascade a
    #: decision back to a channel it no longer has.
    overridden_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: How many decisions already existed for this contract when this row was
    #: written, so "re-run for the third time" is answerable from the row itself.
    #: A re-run still *appends* — this is a counter of history, not a mutable
    #: identity, and the previous rows remain readable (D9-05).
    rerun_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: See :class:`~app.db.models.contract.Contract` — a declarative directive, not
    #: shared mutable state.
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class Notification(Base, TimestampMixin):
    """One attempted contact on one channel for one reminder milestone.

    ``idempotency_key`` is the uniqueness that makes duplicate dispatch
    impossible: a replayed event carrying the same key collides instead of
    sending a second email. It must be supplied by the caller, never derived at
    insert time from a timestamp.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("contract_id", "template", "scheduled_date", name="uq_notification_slot"),
        UniqueConstraint("idempotency_key", name="uq_notifications_idempotency_key"),
        Index("ix_notifications_status_scheduled_at", "status", "scheduled_at"),
        Index("ix_notifications_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    channel: Mapped[Channel] = mapped_column(enum_column(Channel, length=10), nullable=False)
    #: Reminder milestone: ``T-60``, ``T-30`` or ``T-14``.
    template: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        enum_column(NotificationStatus, length=20),
        nullable=False,
        default=NotificationStatus.PENDING,
    )

    scheduled_date: Mapped[date] = mapped_column(Date, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Optimistic-lock column (gap 21). ``idempotency_key`` above stops a duplicate
    #: row being *created*; it says nothing about two workers mutating the same row
    #: at once. Without this column the second dispatch's commit was a plain
    #: last-writer-wins UPDATE, so a worker could overwrite another's ``attempts``
    #: increment or revert a ``SENT`` transition. With it, the loser's write matches
    #: zero rows and SQLAlchemy raises :class:`~sqlalchemy.orm.exc.StaleDataError`
    #: instead of silently discarding the winner's state.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: See :class:`~app.db.models.contract.Contract` — a declarative directive, not
    #: shared mutable state.
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class CustomerResponse(Base, TimestampMixin):
    """Evidence that the customer actually responded.

    A voicemail-machine detection or an unanswered ring is *not* a response and
    must not be recorded here — the spec forbids treating "call was answered"
    as resolution on its own. ``CALL_ANSWERED`` means a human spoke.
    """

    __tablename__ = "customer_responses"
    __table_args__ = (
        Index("ix_customer_responses_contract_received", "contract_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    channel: Mapped[Channel] = mapped_column(enum_column(Channel, length=10), nullable=False)
    response_type: Mapped[ResponseType] = mapped_column(
        enum_column(ResponseType, length=20), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    #: Provider payload, already stripped of PII by the caller.
    response_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
