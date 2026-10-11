"""Alembic environment configuration.

Why this file exists:
    Alembic reads this at every `alembic` command. It wires:
        1. Your app Settings (so `alembic.ini`'s placeholder URL is replaced)
        2. Your ORM models' metadata (so autogenerate can diff)
        3. Sync DB connection (Alembic is synchronous; use pymysql, not aiomysql)
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from contract_agent.config import get_settings
from contract_agent.db.base import Base

# ============================================================
# IMPORT ALL MODELS — REQUIRED FOR AUTOGENERATE
# ============================================================
# Alembic's autogenerate diffs `Base.metadata` against the live DB.
# Models are only visible to `Base.metadata` if they've been imported.
# Importing them here is a **side-effect import** — Pylance can't tell,
# so we import inside a function whose name documents the purpose.

def _register_models() -> None:
    """Import all ORM models so Base.metadata contains their tables.

    Without this, `alembic revision --autogenerate` produces migrations
    with empty upgrade() bodies — target_metadata has no tables.
    """
    import contract_agent.models.user  # noqa: F401


_register_models()


# ============================================================
# ALEMBIC CONFIG OBJECT
# ============================================================
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# ============================================================
# WIRE SETTINGS → SQLALCHEMY URL
# ============================================================
# Alembic is synchronous; use the sync pymysql driver even though the
# app's runtime uses aiomysql. Swap the driver prefix so the URL works
# for both `alembic upgrade` (sync) and the app (async).
_settings = get_settings()
_sync_url = _settings.database_url.replace("+aiomysql", "+pymysql")
config.set_main_option("sqlalchemy.url", _sync_url)


# The metadata that autogenerate compares against.
target_metadata = Base.metadata


# ============================================================
# OFFLINE MODE (emit SQL without connecting)
# ============================================================
def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Emits SQL to stdout / file without a DB connection.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ============================================================
# ONLINE MODE (connect + run)
# ============================================================
def run_migrations_online() -> None:
    """Run migrations in 'online' mode (typical)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
