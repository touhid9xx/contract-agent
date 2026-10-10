"""Alembic env — reads DSN from Settings, supports async.

Why read from Settings?
    - Single source of truth (.env)
    - No duplicate DSN in alembic.ini
    - Same config in dev/CI/prod

Why NOT `config.set_main_option("sqlalchemy.url", ...)`?
    Alembic uses `ConfigParser` internally, which treats `%` as an
    interpolation marker (e.g. `%(name)s`). Our DSN contains URL-encoded
    passwords (`%40` for `@`, `%25` for `%`, etc.) — those `%XX` sequences
    are invalid interpolation syntax and ConfigParser raises:
        ValueError: invalid interpolation syntax in 'mysql+aiomysql://...'

    Instead, we pass the DSN as a module-level constant and construct the
    async engine directly via `create_async_engine()`, bypassing
    ConfigParser entirely.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from contract_agent.config import get_settings
from contract_agent.db.base import Base  # noqa: F401 — imports metadata

# Alembic Config object
config = context.config

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# DSN from Settings — built once, reused by both offline & online modes.
# We deliberately avoid `config.set_main_option("sqlalchemy.url", ...)`:
# ConfigParser would choke on `%` in URL-encoded passwords (see module docstring).
# ---------------------------------------------------------------------------
settings = get_settings()
DSN = settings.database_url.replace("mysql+pymysql://", "mysql+aiomysql://")

# Metadata for autogenerate
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline mode — emit SQL to stdout without DB connection."""
    context.configure(
        url=DSN,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Online mode — async engine built directly from DSN.

    Uses `create_async_engine(DSN, ...)` instead of
    `async_engine_from_config(...)` because the latter reads
    `sqlalchemy.url` from ConfigParser — which fails on URL-encoded
    passwords containing `%`.
    """
    connectable = create_async_engine(DSN, poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
