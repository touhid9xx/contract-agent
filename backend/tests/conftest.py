"""Shared pytest fixtures.

Environment is pinned to ``test`` before the application is imported so no test
can accidentally boot with production validation enabled.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import pytest
import structlog

# Only APP_ENV is pinned here. Individual tests pass ``log_json`` explicitly
# when they care, because an exported LOG_JSON would shadow field defaults and
# silently change the outcome of the production-validation tests.
os.environ.setdefault("APP_ENV", "test")

import uuid
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.core.logging import redact_event_dict
from app.core.security import hash_password
from app.db.base import Base
from app.db.enums import UserRole
from app.db.models import Tenant, User
from app.db.repositories import enqueue_event as enqueue_outbox_event
from app.db.session import build_session_factory, session_scope
from app.main import create_app


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Iterator[None]:
    """Keep the ``lru_cache`` on get_settings from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _detach_structlog_from_capture() -> None:
    """Point structlog at a logger that performs no I/O.

    ``configure_logging`` binds ``sys.stdout`` *at configure time*
    (``PrintLoggerFactory(file=sys.stdout)``), which is right for a process and
    hostile to a test run: pytest swaps ``sys.stdout`` per test and closes the
    previous one, so whichever test logged first leaves a dead stream behind and
    the next test to log dies with "I/O operation on closed file". That surfaces
    as failures that move around when test order changes.

    Rendering still runs (so ``redact_event_dict`` is exercised); only the write
    is removed. ``test_logging`` calls ``configure_logging`` itself when it needs
    to assert on real output.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_event_dict,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )


@pytest.fixture(autouse=True)
def quiet_logging() -> Iterator[None]:
    """Keep every test off pytest's (soon to be closed) capture stream."""
    _detach_structlog_from_capture()
    yield
    _detach_structlog_from_capture()


#: Password used by every fixture-made user. Not a credential: it never leaves
#: the test process and the databases it protects are in-memory.
DEFAULT_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def settings() -> Settings:
    """Test settings, with Argon2 cost lowered on purpose.

    The code path is identical; only the work is smaller. The production
    defaults cost ~80 ms per hash, which would dominate the suite, and a
    verification reads its parameters from the stored hash rather than from
    settings — so lowering the cost here cannot mask a verification bug.
    ``test_security`` asserts the real defaults separately.
    """
    return Settings(
        app_env="test",
        log_json=False,
        argon2_time_cost=1,
        argon2_memory_cost=8192,
        argon2_parallelism=1,
    )


@pytest.fixture
def client(settings: Settings, session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """A TestClient whose app talks to the same in-memory database as the tests."""
    app = create_app(settings, session_factory=session_factory)
    with TestClient(app) as test_client:
        yield test_client


# --- database -------------------------------------------------------------
# Tests run on in-memory SQLite. That is fast and needs no container, and it
# exercises the ORM, repositories and relay logic. It is NOT proof that the
# MySQL-specific pieces work: ``SELECT ... FOR UPDATE SKIP LOCKED`` and native
# ``ENUM`` columns are covered by the opt-in MySQL integration tests
# (``-m integration``), which skip when no MySQL is reachable.


@pytest.fixture
def db_engine() -> Iterator[Engine]:
    """A fresh in-memory database per test, with foreign keys enforced.

    ``StaticPool`` is what makes ``:memory:`` reachable from more than one session
    per test — and also what makes every session hand out the *same* DBAPI
    connection. So a test may use the database from a worker thread, or from the
    test thread, but not from both at once: concurrent use of one ``sqlite3``
    connection raises ``OperationalError``/``SystemError`` at a rate low enough to
    look like flakiness. ``test_escalation_scanner`` is the one test that starts a
    worker thread, and it waits on an event and reads the rows only after joining.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
        # SQLite ignores foreign keys unless asked; without this a broken FK
        # would pass the tests that are supposed to catch it.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def session_factory(db_engine: Engine) -> sessionmaker[Session]:
    return build_session_factory(db_engine)


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def tenant(db_session: Session) -> Tenant:
    owner = Tenant(name="Acme Ltd", slug="acme")
    db_session.add(owner)
    # Committed, not just flushed: other sessions in the same test (the relay
    # uses its own session_scope) must be able to see the tenant.
    db_session.commit()
    return owner


@pytest.fixture
def other_tenant(db_session: Session) -> Tenant:
    """A second tenant, for isolation tests that must fail closed."""
    owner = Tenant(name="Globex Ltd", slug="globex")
    db_session.add(owner)
    db_session.commit()
    return owner


@pytest.fixture
def make_user(session_factory: sessionmaker[Session], settings: Settings) -> Callable[..., User]:
    """Create one committed user inside a tenant and return it.

    Committed rather than flushed because the request under test runs in another
    session and must see the row; returning a detached object is safe here
    because the factory does not expire attributes on commit.
    """

    def _create(
        owner: Tenant,
        *,
        email: str = "ops@acme.test",
        password: str = DEFAULT_PASSWORD,
        role: UserRole = UserRole.ADMIN,
        is_active: bool = True,
        token_epoch: int = 0,
    ) -> User:
        with session_scope(session_factory) as session:
            user = User(
                tenant_id=owner.id,
                email=email.strip().lower(),
                password_hash=hash_password(password, settings),
                role=role,
                is_active=is_active,
                token_epoch=token_epoch,
            )
            session.add(user)
            session.flush()
        return user

    return _create


@pytest.fixture
def enqueue_event(session_factory: sessionmaker[Session], tenant: Tenant) -> Callable[..., int]:
    """Create one pending outbox row in its own transaction; return its id."""

    def _create(**overrides: Any) -> int:
        values: dict[str, Any] = {
            "tenant_id": tenant.id,
            "correlation_id": str(uuid.uuid4()),
            "event_type": "contract.ingested",
            "topic": "contract.ingested",
            "partition_key": "1042",
            "payload": {"contract_id": 1042},
        }
        values.update(overrides)
        with session_scope(session_factory) as session:
            event = enqueue_outbox_event(session, **values)
            return int(event.id)

    return _create
