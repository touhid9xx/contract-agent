"""SQLAlchemy async engine + session factory + tenant filter."""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, with_loader_criteria

from contract_agent.config import Settings
from contract_agent.db.base import TenantMixin
from contract_agent.logging_config import get_logger, tenant_id_var

logger = get_logger(__name__)


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the async engine."""
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the async session factory."""
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )


# ============================================================
# TENANT FILTER
# ============================================================
@event.listens_for(Session, "do_orm_execute")
def _tenant_filter(state) -> None:  # type: ignore[no-untyped-def]
    """Auto-inject `WHERE tenant_id = <current>` on every SELECT.

    Bypass with: .execution_options(include_all_tenants=True)
    Disabled when tenant_id_var is unset (health checks, migrations).
    """
    if not state.is_select:
        return
    if state.execution_options.get("include_all_tenants"):
        return

    tenant = tenant_id_var.get()
    if not tenant:
        return

    state.statement = state.statement.options(
        with_loader_criteria(
            TenantMixin,
            lambda cls: cls.tenant_id == tenant,
            include_aliases=True,
        )
    )
    logger.debug("tenant_filter_applied", tenant_id=tenant)
