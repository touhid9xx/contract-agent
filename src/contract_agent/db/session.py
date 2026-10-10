"""SQLAlchemy async engine + session factory + tenant filter.

Why session-scoped tenant filter?
    - `do_orm_execute` is a Session-level event in SQLAlchemy 2.0
    - Attaching on Base (DeclarativeBase) raises InvalidRequestError
    - Sessions are created per request → filter runs on every query

"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, with_loader_criteria

from contract_agent.config import Settings, get_settings
from contract_agent.db.base import get_tenant_models
from contract_agent.logging_config import get_logger

logger = get_logger(__name__)


# ============================================================
# ENGINE
# ============================================================
def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create an async SQLAlchemy engine from Settings.

    URL uses `mysql+aiomysql` for async support. The Settings DB URL
    is built for sync (`mysql+pymysql`); we swap the driver here.
    """
    settings = settings or get_settings()
    url = settings.database_url.replace("mysql+pymysql://", "mysql+aiomysql://")

    engine = create_async_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        pool_size=settings.mysql_pool_size,
        pool_recycle=settings.mysql_pool_recycle,
        future=True,
    )
    logger.info("db_engine_created", host=settings.mysql_host, db=settings.mysql_db)
    return engine


# ============================================================
# SESSION FACTORY
# ============================================================
def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build session factory + register tenant filter listener."""
    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    # Register the tenant filter on EVERY session this factory creates.
    # `do_orm_execute` is a Session-level event — must attach to the
    # session class, not to the declarative Base.
    event.listen(Session, "do_orm_execute", _add_tenant_filter)

    return factory


# ============================================================
# TENANT FILTER — inject WHERE tenant_id = current on every SELECT
# ============================================================
def _add_tenant_filter(execute_state: Any) -> None:
    """Inject `WHERE tenant_id = :current` on every SELECT.

    Skip when:
        - Not a SELECT (INSERT/UPDATE/DELETE handled by app code)
        - `include_all_tenants=True` execution option set (admin override)
        - No tenant in contextvar (health checks, migrations)

    Why `with_loader_criteria`?
        - Works with joins, eager loads, subqueries
        - Preserves the query shape (not a raw string hack)
    """
    if not execute_state.is_select:
        return
    if execute_state.execution_options.get("include_all_tenants", False):
        return

    # Local import avoids circular dependency at module load
    from contract_agent.logging_config import tenant_id_var

    tenant_id = tenant_id_var.get()
    if not tenant_id:
        # No tenant in context — let it through (health checks, migrations)
        return

    tenant_models = get_tenant_models()
    if not tenant_models:
        # No models registered yet (M4). Skip silently.
        return

    for model in tenant_models:
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                model,
                lambda cls: cls.tenant_id == tenant_id,
                include_aliases=True,
            )
        )
