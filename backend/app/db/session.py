"""Engine and session factory construction.

The engine is built from :class:`Settings` once per process. Nothing here opens
a connection at import time: a process that cannot reach MySQL should still be
able to start and report an unhappy readiness probe (F5) instead of crashing on
import with an opaque traceback.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings

#: Dialects that do not accept MySQL pool arguments.
_SQLITE_PREFIX = "sqlite"


def is_sqlite(url: str) -> bool:
    return url.startswith(_SQLITE_PREFIX)


def build_engine(settings: Settings) -> Engine:
    """Create the engine, applying pool settings only where they are valid.

    SQLite (used by the test suite) rejects ``pool_size``/``max_overflow``
    because it uses a different pool implementation, so passing them
    unconditionally would make every test fail on a TypeError from deep inside
    SQLAlchemy.
    """
    kwargs: dict[str, Any] = {
        "echo": settings.db_echo,
        "pool_pre_ping": True,
        "future": True,
    }
    if is_sqlite(settings.database_url):
        # The test engine is shared across threads by the FastAPI TestClient.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
        kwargs["pool_recycle"] = settings.db_pool_recycle_seconds

    return create_engine(settings.database_url, **kwargs)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, roll back on any exception, always close."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
