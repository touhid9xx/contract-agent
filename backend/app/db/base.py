"""Declarative base, naming conventions and shared column mixes.

Two decisions worth knowing before reading a model:

* **Timestamps are naive UTC.** The spec requires UTC for stored values and an
  explicit tenant timezone for presentation. MySQL's ``DATETIME`` discards
  ``tzinfo``, so persisting aware datetimes would silently come back naive and
  any ``aware - naive`` arithmetic would raise. ``utc_now()`` therefore returns
  a naive UTC value, and ``as_utc()`` is the single place that re-attaches the
  timezone when serialising. See DECISIONS.md D2-01.
* **Every tenant-owned table repeats ``tenant_id``** rather than relying on a
  join chain. Redundant on paper, but it is what makes scoping enforceable in
  the repository layer with a single predicate — see DECISIONS.md D2-02.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import GUID

#: Deterministic constraint names so Alembic migrations are reviewable and
#: MySQL's 64-character identifier limit is never hit by an auto-generated name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    """Current time as a naive UTC datetime (see module docstring)."""
    return datetime.now(UTC).replace(tzinfo=None)


def truncate_to_second(value: datetime) -> datetime:
    """Drop sub-second precision so a value cannot be *rounded forward*.

    MySQL's ``DATETIME`` without a fractional-seconds specifier rounds a value
    carrying microseconds on insert. A timestamp taken "now" can therefore end up
    stored up to half a second in the **future**, which makes an eligibility
    comparison such as ``next_attempt_at <= now`` false for a row that was just
    enqueued — the relay misses it on its first tick. Flooring the value before
    writing keeps "due now" meaning due now. See DECISIONS.md D2-06.
    """
    return value.replace(microsecond=0)


def as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a stored naive datetime for serialisation."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class Base(DeclarativeBase):
    """Shared declarative base."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """``created_at`` / ``updated_at`` maintained by the application clock."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class TenantMixin:
    """Adds the tenant discriminator required on every tenant-owned table."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
