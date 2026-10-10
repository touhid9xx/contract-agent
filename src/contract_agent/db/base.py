"""SQLAlchemy DeclarativeBase + tenant mixin.

Tenant filter event listener lives in `db/session.py` because
`do_orm_execute` is a Session-level event (not a model/Base event).

Why tenant filter as an event listener?
    - Every query MUST be scoped to the current tenant
    - Manual WHERE clauses = easy to forget = data leak
    - Event listener = automatic, impossible to bypass

Why not global tenant in contextvar?
    - Contextvars don't propagate across threads/connections reliably
    - We read tenant from request.state (set by auth dep in M5)
    - Fallback to default tenant for non-authenticated contexts (health checks)

"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from contract_agent.config import get_settings
from contract_agent.logging_config import get_logger

logger = get_logger(__name__)


# ============================================================
# BASE
# ============================================================
class Base(DeclarativeBase):
    """Root declarative base for all ORM models."""

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


# ============================================================
# TENANT MIXIN
# ============================================================
class TenantMixin:
    """Adds `tenant_id` column to any model that mixes it in."""

    tenant_id: Mapped[str] = mapped_column(
        index=True,
        nullable=False,
        default=lambda: get_settings().tenant_default_id,
    )


# ============================================================
# TENANT MODEL REGISTRY
# ============================================================
# The set of models that carry tenant_id. Populated lazily.
_TENANT_MODELS: set[type[Any]] = set()


def register_tenant_model(model: type[Any]) -> None:
    """Register a model as tenant-scoped. Called by each model in M7."""
    _TENANT_MODELS.add(model)


def get_tenant_models() -> frozenset[type[Any]]:
    """Read-only view of registered tenant models."""
    return frozenset(_TENANT_MODELS)


# ============================================================
# FASTAPI DEPENDENCY
# ============================================================
async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — yields a session from app.state.session_factory.

    Usage:
        @app.get("/x")
        async def x(db: AsyncSession = Depends(get_db)):
            ...
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbDep = Depends(get_db)
