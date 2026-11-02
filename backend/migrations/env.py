"""Alembic environment.

Two things differ from the stock template:

* The URL comes from :class:`Settings` (``DATABASE_URL`` wins), so connection
  details live in one place and never in a committed ini file.
* ``compare_type`` is on so autogenerate notices a widened ``VARCHAR`` or a
  changed ``NUMERIC`` precision, which is exactly the class of silent schema
  drift that breaks a deployed service.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ``backend/`` must be importable so ``app.*`` resolves when alembic is invoked
# from the repository root (which is how pre-commit and CI call it).
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402

# Importing the package registers every model on ``Base.metadata``. Without it
# autogenerate would happily propose dropping every table.
import app.db.models  # noqa: E402,F401  isort: skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


#: The inert value committed in ``alembic.ini``. Keep the two in sync: a real
#: URL is only honoured when it differs from this, so the placeholder can never
#: be mistaken for a configured database.
_PLACEHOLDER_URL = "driver://user:pass@localhost/dbname"


def _database_url() -> str:
    """Resolve the URL, escaping ``%`` for configparser interpolation.

    Precedence, most explicit first:

    1. A URL set on the Alembic config object — this is how programmatic
       callers (the MySQL integration test, CI, ``-x`` overrides) point a
       migration run at a specific database.
    2. ``DATABASE_URL`` from the environment.
    3. ``Settings.database_url``, which itself reads ``.env``.

    The ``%`` escaping is for configparser: a URL-encoded password (``p%40ss``)
    would otherwise be read as an interpolation error rather than a credential.
    """
    configured = (config.get_main_option("sqlalchemy.url") or "").strip()
    if configured and configured != _PLACEHOLDER_URL:
        url = configured
    else:
        url = os.getenv("DATABASE_URL") or get_settings().database_url
    return url.replace("%", "%%")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (used to review a migration)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead. It is a no-op on MySQL.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
