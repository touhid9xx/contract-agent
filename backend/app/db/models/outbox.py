"""Transactional outbox.

The outbox exists so the business write and the event that announces it commit
in the same database transaction. If the process dies after commit, the relay
still publishes; if the publish fails, the business row is still correct and the
event is retried. That is the whole point — an event must never be logged and
dropped (spec §EVENT DELIVERY).

Delivery is **at least once**. Consumers must therefore be idempotent; nothing
in this table tries to promise exactly-once side effects across a provider.

Claiming is done with ``SELECT ... FOR UPDATE SKIP LOCKED`` plus a lease
(``locked_until``), *not* with a Redis lock. A Redis singleton lock would make
throughput depend on one worker process and would turn a Redis outage into a
publishing outage; a row lease is correct with N relay replicas and survives a
crashed worker by expiring. See DECISIONS.md D2-03.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.enums import OutboxStatus, enum_column
from app.db.types import GUID


class OutboxEvent(Base, TimestampMixin):
    """One pending domain event, published to Kafka/StrikeMQ by the relay."""

    __tablename__ = "outbox"
    __table_args__ = (
        # The relay's only hot query: due rows in id order.
        Index("ix_outbox_status_next_attempt_at", "status", "next_attempt_at"),
        # Reclaiming a crashed worker's batch.
        Index("ix_outbox_status_locked_until", "status", "locked_until"),
        Index("ix_outbox_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Stable identity of the event itself, propagated into the message header.
    #: Consumers de-duplicate on this, so it is generated once and never reused.
    event_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), nullable=False, unique=True, default=uuid.uuid4
    )
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)

    #: Tenant is part of the envelope, not just the payload: a consumer must be
    #: able to scope its work before it parses the body.
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)

    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    #: Bumped whenever the payload shape changes. Consumers reject an unknown
    #: version rather than guessing.
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Kafka partition key — the contract id — so all events for one contract
    #: keep their relative order.
    partition_key: Mapped[str] = mapped_column(String(255), nullable=False)

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    status: Mapped[OutboxStatus] = mapped_column(
        enum_column(OutboxStatus, length=20), nullable=False, default=OutboxStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)

    # --- lease -----------------------------------------------------------
    locked_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    #: Truncated to 500 characters and never allowed to contain a payload dump:
    #: provider errors can echo a destination address.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
