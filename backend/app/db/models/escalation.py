"""Escalation queue and audit log."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.enums import EscalationStatus, enum_column
from app.db.types import GUID


class Escalation(Base, TimestampMixin):
    """A contract with no meaningful customer response inside the window.

    ``sla_deadline`` is a business clock, not a contact permission: the default
    of 48 h (``DECISIONS.md`` Q2) decides when a human is late, never whether we
    may contact the customer again.
    """

    __tablename__ = "escalations"
    __table_args__ = (
        Index("ix_escalations_status_sla_deadline", "status", "sla_deadline"),
        Index("ix_escalations_assigned_to", "assigned_to", "status"),
        Index("ix_escalations_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    assigned_to: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    sla_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    status: Mapped[EscalationStatus] = mapped_column(
        enum_column(EscalationStatus, length=20), nullable=False, default=EscalationStatus.OPEN
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Optimistic lock so two operators cannot both claim / resolve silently.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: Declarative directive, not shared mutable state — SQLAlchemy copies it per class.
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class AuditLog(Base, TimestampMixin):
    """Append-only record of who changed what.

    ``before``/``after`` hold JSON snapshots with sensitive values already
    redacted by the caller — the audit log is readable by operators, so storing
    a decrypted email here would defeat the PII encryption applied in F7.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_entity_entity_id", "entity", "entity_id"),
        Index("ix_audit_log_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_log_correlation", "correlation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)

    #: ``user`` | ``worker`` | ``system`` — an escalation or a scheduler tick
    #: has no human actor, and pretending otherwise would falsify the trail.
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    action: Mapped[str] = mapped_column(String(100), nullable=False)
    #: Table name plus a stringified primary key; kept generic on purpose so the
    #: log survives schema changes to any one entity.
    entity: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    before_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
