"""Tenant and user models — the root of the isolation boundary."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.enums import UserRole, enum_column
from app.db.types import GUID


class Tenant(Base, TimestampMixin):
    """A customer organisation. Every other tenant-owned row hangs off this."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Stable human-readable handle used in the UI and in logs (never an id).
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    users: Mapped[list[User]] = relationship(back_populates="tenant")


class User(Base, TimestampMixin):
    """An operator of one tenant.

    ``tenant_id`` is part of the uniqueness constraint on ``email``: the same
    person may legitimately hold accounts in two tenants, and a global unique
    email would leak the fact that an address exists elsewhere.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    #: Argon2id hash. Never a plaintext or reversible value.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole, length=20), nullable=False, default=UserRole.VIEWER
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Bumped on logout-all / password change; a token carrying an older value
    #: is rejected even if it has not expired yet (F3).
    token_epoch: Mapped[int] = mapped_column(nullable=False, default=0)

    tenant: Mapped[Tenant] = relationship(back_populates="users")
