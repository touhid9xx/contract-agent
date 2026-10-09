"""Kafka event schemas — one model per topic.

Why typed events?
    - Producers/consumers share the same Pydantic model
    - Schema drift caught by tests
    - Serialized as JSON (M18 adds Avro if needed)

Design note on `event_type`:
    Python's type system does NOT allow narrowing a mutable instance field
    from `str` to `Literal[...]` in a subclass (Liskov violation).
    The idiomatic fix: declare `event_type` only in subclasses with
    `ClassVar` + `Literal`, and add a Pydantic `model_config` flag so it's
    still serialized. This keeps Pylance + mypy happy WITHOUT `# type: ignore`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class BaseEvent(BaseModel):
    """Common envelope for all Kafka events.

    Subclasses MUST define `event_type: ClassVar[Literal[...]]` themselves.
    The base does not declare it, so no variance conflict arises.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    correlation_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    payload: dict[str, Any]


class ContractIngestedEvent(BaseEvent):
    # ClassVar → Pylance treats it as class-level, no LSP conflict
    event_type: Literal["contract.ingested"] = "contract.ingested"


class FieldsExtractedEvent(BaseEvent):
    event_type: Literal["fields.extracted"] = "fields.extracted"


class NotificationSentEvent(BaseEvent):
    event_type: Literal["notification.sent"] = "notification.sent"


class CustomerRespondedEvent(BaseEvent):
    event_type: Literal["customer.responded"] = "customer.responded"


class EscalationTriggeredEvent(BaseEvent):
    event_type: Literal["escalation.triggered"] = "escalation.triggered"
