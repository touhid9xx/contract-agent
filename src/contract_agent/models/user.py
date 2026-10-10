"""User model — authentication, RBAC, multi-tenancy.

Why UUID primary key?
    - No enumeration (guessable IDs from URLs)
    - Distributed-friendly (future sharding)
    - Tenant-safe (never collides across tenants)

Why role as Enum?
    - Pydantic v2 + SQLAlchemy both support Enum cleanly
    - DB constraint catches typos
    - Frontend can generate Zod enum from JSON Schema (M3 pipeline)

"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import ClassVar

from sqlalchemy import Boolean, DateTime, Enum, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from contract_agent.db.base import Base, TenantMixin, register_tenant_model


class UserRole(enum.StrEnum):
    """RBAC roles.

    - admin:    full access, can manage users
    - operator: can review fields, handle escalations, view everything
    - viewer:   read-only
    """

    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class User(Base, TenantMixin):
    """User account."""

    __tablename__ = "users"
    __table_args__ = (
        # Email unique *per tenant*, not globally
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"),
        nullable=False,
        default=UserRole.VIEWER,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Optimistic locking — bump on every update, reject stale writes
    version: Mapped[int] = mapped_column(nullable=False, default=1)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ClassVar tells mypy/ruff this is a class-level config dict, not an
    # instance attribute. SQLAlchemy reads it at mapper-configuration time.
    __mapper_args__: ClassVar[dict[str, object]] = {"version_id_col": version}  # type: ignore

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} role={self.role}>"


# Register with tenant filter (M4 db/base.py)
register_tenant_model(User)
