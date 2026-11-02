"""Enumerations shared by models, schemas and workers.

Values are stored as their lowercase strings, not as Python member names, so a
renamed member cannot silently change what is already in the database. The
``enum_column`` helper centralises that mapping.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import Enum as SAEnum


class UserRole(StrEnum):
    """Spec: RBAC with three roles, enforced at route level (F3)."""

    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class ContractStatus(StrEnum):
    """Contract lifecycle states from the specification."""

    ACTIVE = "active"
    REMINDER_SENT = "reminder_sent"
    RESPONDED = "responded"
    RENEWED = "renewed"
    EXPIRED = "expired"
    ESCALATED = "escalated"


class ContractSource(StrEnum):
    """How the contract entered the system (P2-A upload vs P2-H pull)."""

    PDF = "pdf"
    API = "api"


class RiskLevel(StrEnum):
    """Coarse risk bucket. Deliberately coarse: it is sent to the LLM."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FieldName(StrEnum):
    """Fields the extraction pipeline is allowed to produce."""

    EMAIL = "email"
    PHONE = "phone"
    ADDRESS = "address"
    DOB = "dob"
    START_DATE = "start_date"
    END_DATE = "end_date"
    CONTRACT_VALUE = "contract_value"


class FieldStatus(StrEnum):
    """Review state of one extracted field.

    ``pending_review`` is a hard gate: no field in this state may reach the
    notification pipeline (spec §CUSTOMER CONTACT POLICY).
    """

    AUTO_ACCEPTED = "auto_accepted"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    UNREADABLE = "unreadable"


class Channel(StrEnum):
    """Contact channels. The LLM may only rank among these."""

    EMAIL = "email"
    SMS = "sms"
    VOICE = "voice"


class NotificationStatus(StrEnum):
    """Dispatch state of a notification row."""

    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    ANSWERED = "answered"
    MACHINE = "machine"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class ResponseType(StrEnum):
    """What counts as a customer response."""

    REPLY = "reply"
    CLICK = "click"
    CALL_ANSWERED = "call_answered"
    MANUAL = "manual"


class EscalationStatus(StrEnum):
    """Escalation queue states."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    SLA_BREACHED = "sla_breached"


class OutboxStatus(StrEnum):
    """Outbox row state.

    ``PUBLISHING`` exists so a crashed relay leaves a reclaimable claim rather
    than a row that looks pending while another worker is mid-publish.
    """

    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


def enum_column(enum_type: type[StrEnum], *, length: int) -> SAEnum:
    """Return an ``Enum`` column that stores values, works on MySQL and SQLite.

    ``native_enum`` is left to the dialect: MySQL builds a real ``ENUM``, SQLite
    falls back to ``VARCHAR`` + ``CHECK``. Either way the stored text is the
    member's ``value``, which is what the API and the DDL in the spec show.
    """

    return SAEnum(
        enum_type,
        values_callable=lambda members: [member.value for member in members],
        native_enum=True,
        length=length,
        validate_strings=True,
    )


def enum_values(enum_type: type[StrEnum]) -> list[Any]:
    """All stored values, for CHECK constraints and API validation."""
    return [member.value for member in enum_type]
