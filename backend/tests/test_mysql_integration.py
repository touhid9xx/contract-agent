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
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base, utc_now
from app.db.models import Tenant
from app.db.repositories.outbox import OutboxRepository, enqueue_event

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[1]

#: Best-effort fallback matching ``docker-compose.yml``'s default *user* and
#: *database* — with a ``_test`` suffix so a stray local ``contract_agent``
#: database is never dropped.
#:
#: It is only a guess: compose has **no** default ``MYSQL_PASSWORD`` (the file
#: requires ``MYSQL_PASSWORD`` to be set in ``.env``), so whenever the password
#: is anything other than ``contract_agent`` — which is the normal case — these
#: tests skip with "cannot connect ... as 'contract_agent'" and the operator must
#: pass ``MYSQL_TEST_URL``. That is the documented path in ``STATUS.md``.
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


class _FakeDBAPIError(Exception):
    """Stands in for a SQLAlchemy ``DBAPIError``, which carries ``.orig``."""

    def __init__(self, orig: BaseException) -> None:
        super().__init__("(wrapped by SQLAlchemy)")
        self.orig = orig


def test_driver_error_surfaces_the_errno_and_message() -> None:
    """A skip reason must name the real cause, not the wrapper's class name.

    Regression: an auth failure (1045) was reported as "cannot reach or create
    the test database", which sends the operator hunting for a database that is
    in fact running fine.
    """
    error = _FakeDBAPIError(OSError(1045, "Access denied for user 'x'"))
    assert _driver_error(error) == "MySQL error 1045: Access denied for user 'x'"


def test_driver_error_falls_back_to_the_type_name() -> None:
    """Non-driver errors (DNS, socket, malformed URL) have no errno to show."""
    assert _driver_error(OSError("network unreachable")) == "OSError"
    assert _driver_error(_FakeDBAPIError(ValueError("no errno here"))) == "_FakeDBAPIError"


#: The test database name is interpolated into DDL, so it must not be clever.
_SAFE_DATABASE_NAME = re.compile(r"\w+")


def _driver_error(exc: Exception) -> str:
    """The driver's own words, e.g. ``MySQL error 1045: Access denied ...``.

    ``str(exc)`` on a SQLAlchemy error wraps the driver error in a multi-line
    message plus a docs URL, which is noise in a skip reason. PyMySQL puts
    ``(errno, message)`` in ``exc.orig.args``, so surface that directly.
    """
    args = getattr(getattr(exc, "orig", None), "args", ())
    if args and isinstance(args[0], int):
        detail = args[1] if len(args) > 1 else ""
        return f"MySQL error {args[0]}: {detail}".strip()
    return type(exc).__name__


def _ensure_database(url: str) -> None:
    """Create the test database if it is missing, or skip with the reason *why*.

    ``docker compose up -d mysql`` only creates ``contract_agent``, so out of the
    box the test database does not exist. Connecting straight to it raises
    "Unknown database", which the fixture would report as a *skip* — reading
    exactly like "no MySQL here" while the tests silently never run.

    The two ways this can fail need different fixes, so they get different
    messages. Collapsing them into one is actively misleading: a wrong password
    (``Access denied``, error 1045) used to be reported as "cannot reach or
    create the test database", pointing the operator at ``docker compose up``
    and a ``CREATE DATABASE`` when the real fix is to correct the credentials in
    ``MYSQL_TEST_URL``.
    """
    parsed = make_url(url)
    name = parsed.database or ""
    if not _SAFE_DATABASE_NAME.fullmatch(name):
        pytest.skip(f"MYSQL_TEST_URL must name a simple database, got {name!r}")

    try:
        engine = create_engine(parsed.set(database=None), pool_pre_ping=True, future=True)
    except Exception as exc:
        pytest.skip(f"cannot build a MySQL engine from MYSQL_TEST_URL ({type(exc).__name__})")

    # ``render_as_string`` omits the password, so this is safe to put in output.
    where = f"{parsed.host}:{parsed.port} as {parsed.username!r}"
    try:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:
            pytest.skip(
                f"cannot connect to MySQL at {where} ({_driver_error(exc)}). Check that the "
                f"server is up (`docker compose up -d mysql`) and that MYSQL_TEST_URL uses the "
                f"same MYSQL_USER/MYSQL_PASSWORD credentials compose was started with."
            )

        try:
            with engine.begin() as connection:
                connection.execute(
                    text(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4")
                )
        except Exception as exc:
            pytest.skip(
                f"connected to MySQL at {where} but cannot create the test database {name!r} "
                f"({_driver_error(exc)}). Create it once as an admin: docker compose exec mysql "
                f'mysql -uroot -p -e "CREATE DATABASE IF NOT EXISTS {name}"'
            )
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def mysql_engine() -> Iterator[Engine]:
    """A connected engine, or a skip when no MySQL is reachable."""
    url = _mysql_url()
    _ensure_database(url)
    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
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
    config.set_main_option("sqlalchemy.url", mysql_engine.url.render_as_string(hide_password=False))
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
