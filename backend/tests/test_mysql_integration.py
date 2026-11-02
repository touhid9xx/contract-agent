"""Integration tests that need a real MySQL 8 server.

Everything else in the suite runs on in-memory SQLite, which cannot prove two
things this project depends on:

1. ``SELECT ... FOR UPDATE SKIP LOCKED`` actually isolates concurrent outbox
   claiming. SQLite has no ``FOR UPDATE``; SQLAlchemy silently omits the clause,
   so the SQLite suite would pass even if the clause were removed entirely.
2. Enum columns are real MySQL ``ENUM`` types carrying the expected values, and
   the Alembic baseline migration applies cleanly to MySQL.

These are skipped, not failed, when no MySQL is reachable, so the default
``pytest`` run stays offline. To run them against the compose stack::

    docker compose up -d mysql
    venv\\Scripts\\python.exe -m pytest -m integration backend/tests

Point ``MYSQL_TEST_URL`` at any disposable database to use a different server.
The tests drop and recreate the schema, so never aim it at real data.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base, utc_now
from app.db.models import Tenant
from app.db.repositories.outbox import OutboxRepository, enqueue_event

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[1]

#: Matches the credentials ``docker-compose.yml`` defaults to, but a *test*
#: database name so a stray local ``contract_agent`` database is never dropped.
DEFAULT_URL = "mysql+pymysql://contract_agent:contract_agent@127.0.0.1:3306/contract_agent_test"


def _mysql_url() -> str:
    """The MySQL URL these tests run against.

    A *blank* ``MYSQL_TEST_URL`` must fall back to the default rather than be
    handed to ``create_engine``. ``os.environ.get(key, default)`` returns ``""``
    when the variable is set-but-empty, which is exactly how CI templates and
    ``docker compose`` env blocks express "not configured"; passing that to
    ``create_engine`` raises ``ArgumentError`` during fixture setup, so the
    tests *error* instead of skipping. Strip first, then fall back.
    """
    return os.environ.get("MYSQL_TEST_URL", "").strip() or DEFAULT_URL


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_env_var_falls_back_to_the_default_url(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """Regression: a set-but-blank MYSQL_TEST_URL must not reach create_engine."""
    monkeypatch.setenv("MYSQL_TEST_URL", blank)
    assert _mysql_url() == DEFAULT_URL


def test_an_explicit_url_is_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYSQL_TEST_URL", "mysql+pymysql://u:p@db:3306/scratch")
    assert _mysql_url() == "mysql+pymysql://u:p@db:3306/scratch"


@pytest.fixture(scope="module")
def mysql_engine() -> Iterator[Engine]:
    """A connected engine, or a skip when no MySQL is reachable."""
    url = _mysql_url()
    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any driver/connection error means "skip"
        engine.dispose()
        pytest.skip(f"MySQL not reachable at {url!r} ({type(exc).__name__})")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def migrated(mysql_engine: Engine) -> Engine:
    """Apply the Alembic baseline to a clean schema, once per module.

    Running the real migration (rather than ``metadata.create_all``) is the
    point: it proves the DDL operators will actually ship is valid MySQL.
    """
    config = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", mysql_engine.url.render_as_string(hide_password=False)
    )
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    return mysql_engine


@pytest.fixture
def mysql_session(migrated: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=migrated, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _column_type(engine: Engine, table: str, column: str) -> str:
    with engine.connect() as connection:
        value = connection.execute(
            text(
                "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = :table AND COLUMN_NAME = :column"
            ),
            {"table": table, "column": column},
        ).scalar_one()
    assert isinstance(value, str)
    return value


def test_baseline_migration_applies_and_creates_every_table(migrated: Engine) -> None:
    existing = set(inspect(migrated).get_table_names())
    missing = set(Base.metadata.tables) - existing
    assert not missing, f"migration is missing tables declared in metadata: {sorted(missing)}"


def test_enums_are_native_mysql_enum_types(migrated: Engine) -> None:
    """A VARCHAR fallback would let a typo reach the database unchecked."""
    contract_status = _column_type(migrated, "contracts", "status")
    assert contract_status.startswith("enum("), contract_status
    assert "'active'" in contract_status
    assert "'reminder_sent'" in contract_status

    outbox_status = _column_type(migrated, "outbox", "status")
    assert outbox_status.startswith("enum("), outbox_status
    assert "'pending'" in outbox_status


def test_skip_locked_lets_two_workers_claim_disjoint_rows(
    migrated: Engine, mysql_session: Session
) -> None:
    """The core at-least-once guarantee: concurrent relays never share a row.

    Worker A claims a row inside an open transaction and holds the lock. Worker
    B's claim must *skip* that row instead of blocking, and must get a different
    one. Without ``SKIP LOCKED`` this test either blocks until timeout or hands
    the same row to both workers, which would double-send a notification.
    """
    engine = migrated
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM outbox"))
        connection.execute(text("DELETE FROM tenants"))

    tenant = Tenant(name="Integration Tenant", slug=f"it-{uuid.uuid4().hex[:8]}")
    mysql_session.add(tenant)
    mysql_session.flush()

    for index in range(2):
        enqueue_event(
            mysql_session,
            tenant_id=tenant.id,
            correlation_id=f"it-{index}",
            event_type="contract.expiring",
            topic="contract.events",
            partition_key=str(index),
            payload={"index": index},
            available_at=utc_now(),
        )
    mysql_session.commit()

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    worker_a = factory()
    worker_b = factory()
    try:
        claimed_a = OutboxRepository(
            worker_a, worker_id="worker-a", lease_seconds=60, max_attempts=5
        ).claim_due(1)
        assert len(claimed_a) == 1, "worker A should claim exactly one row"

        claimed_b = OutboxRepository(
            worker_b, worker_id="worker-b", lease_seconds=60, max_attempts=5
        ).claim_due(1)
        assert len(claimed_b) == 1, "worker B must not block on worker A's locked row"

        assert claimed_a[0].id != claimed_b[0].id, "the same row was handed out twice"
        assert claimed_a[0].locked_by == "worker-a"
        assert claimed_b[0].locked_by == "worker-b"
    finally:
        worker_a.rollback()
        worker_b.rollback()
        worker_a.close()
        worker_b.close()
