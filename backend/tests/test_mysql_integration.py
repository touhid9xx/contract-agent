"""Integration tests that need a real MySQL 8 server.

Everything else in the suite runs on in-memory SQLite, which cannot prove the
four things below that this project depends on:

1. ``SELECT ... FOR UPDATE SKIP LOCKED`` actually isolates concurrent outbox
   claiming. SQLite has no ``FOR UPDATE``; SQLAlchemy silently omits the clause,
   so the SQLite suite would pass even if the clause were removed entirely.
2. Enum columns are real MySQL ``ENUM`` types carrying the expected values, and
   the Alembic baseline migration applies cleanly to MySQL.
3. The notification dispatch lock — gap 23. ``claim_for_dispatch`` takes the row
   under ``SELECT ... FOR UPDATE`` *before* the policy runs, so a second worker
   blocks and then sees ``sent`` instead of sending a second email. Both halves
   of that sentence are only observable with two real connections; on SQLite the
   clause is a silent no-op, which is why the SQLite suite cannot be the proof.
4. ``confidence``'s declared scale. ``NUMERIC(4, 3)`` is enforced by MySQL and
   only *declared* by SQLite, so the quantiser from ``c90423b`` must be a no-op
   here — this file is where that can be shown rather than argued.
5. Refresh-token rotation — gap 6. ``claim_for_rotation`` is a conditional
   ``UPDATE`` whose *affected-row count* is the answer to "did I win the race",
   and whose row must be locked while it runs. SQLite has no row locks (writers
   queue globally) and no ``AUTO_INCREMENT``-style ``rowcount`` subtleties, so
   neither half of that guarantee is observable on it.
6. Foreign keys that actually cascade. SQLite only enforces them when
   ``PRAGMA foreign_keys`` is on; InnoDB enforces them always. Deleting a user
   must take that user's live sessions with it, which is a claim about the
   server, not about our model declarations.

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
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.refresh import mint_refresh_token, refresh_token_hash
from app.db.base import Base, utc_now
from app.db.enums import Channel, FieldName, FieldStatus, NotificationStatus, UserRole
from app.db.models import Contract, Notification, Tenant, User
from app.db.repositories.notifications import NotificationRepository, claim_next_due
from app.db.repositories.outbox import OutboxRepository, enqueue_event
from app.db.repositories.security import LoginAttemptRepository, RefreshTokenRepository

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


#: MySQL's errno for "lock wait timeout exceeded". Seeing it is the *positive*
#: result below: it is the proof that the second worker waited on the first
#: rather than reading the row alongside it.
LOCK_WAIT_TIMEOUT = 1205

#: MySQL waits 50s by default. Shortened so a regression fails fast instead of
#: stalling the suite — the timeout is the assertion, not the delay.
LOCK_WAIT_SECONDS = 1


def _seed_contract(engine: Engine) -> tuple[uuid.UUID, int]:
    """A clean tenant with one contactable contract, and no other rows.

    Emptied first because ``claim_next_due`` is deliberately *unscoped*: a
    pending row left behind by an earlier test would be a legitimate thing for
    it to return, so "which row did the claim pick" would prove nothing.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.execute(text("DELETE FROM extracted_fields"))
        session.execute(text("DELETE FROM notifications"))
        session.execute(text("DELETE FROM agent_decisions"))
        session.execute(text("DELETE FROM contracts"))
        session.execute(text("DELETE FROM tenants"))

        tenant = Tenant(name="Lock Tenant", slug=f"it-lock-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        contract = Contract(
            tenant_id=tenant.id,
            customer_ref="A. Rahman",
            end_date=utc_now().date() + timedelta(days=40),
            timezone="UTC",
        )
        session.add(contract)
        session.flush()
        tenant_id, contract_id = tenant.id, int(contract.id)
        session.commit()
    return tenant_id, contract_id


def _pending(
    engine: Engine, tenant_id: uuid.UUID, contract_id: int, *, template: str = "T-60"
) -> int:
    """One committed ``pending`` row that is already due (no ``scheduled_at``)."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        row = Notification(
            tenant_id=tenant_id,
            contract_id=contract_id,
            channel=Channel.EMAIL,
            template=template,
            status=NotificationStatus.PENDING,
            scheduled_date=utc_now().date() - timedelta(days=20),
            scheduled_at=None,
            idempotency_key=f"it-lock-{uuid.uuid4().hex}",
            attempts=0,
        )
        session.add(row)
        session.flush()
        notification_id = int(row.id)
        session.commit()
    return notification_id


def test_the_dispatch_lock_blocks_a_second_worker(migrated: Engine) -> None:
    """Gap 23: the half of the double-send guard SQLite cannot exercise.

    ``claim_for_dispatch`` takes the row under ``SELECT ... FOR UPDATE`` *before*
    the policy runs. Two claims are what prove it: the second worker must
    **block** (not read the row concurrently), and once the first commits it must
    see ``sent`` — which is the state the policy refuses with ``NOT_PENDING``
    instead of reaching the transport a second time.
    """
    tenant_id, contract_id = _seed_contract(migrated)
    notification_id = _pending(migrated, tenant_id, contract_id)
    factory = sessionmaker(bind=migrated, expire_on_commit=False)
    worker_a = factory()
    worker_b = factory()
    try:
        claimed = NotificationRepository(worker_a, tenant_id).claim_for_dispatch(notification_id)
        assert claimed is not None, "worker A should hold the row"
        assert claimed.status == NotificationStatus.PENDING

        worker_b.execute(text(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_SECONDS}"))
        with pytest.raises(OperationalError) as blocked:
            NotificationRepository(worker_b, tenant_id).claim_for_dispatch(notification_id)
        assert _driver_error(blocked.value).startswith(f"MySQL error {LOCK_WAIT_TIMEOUT}"), (
            "worker B must wait on worker A's lock; without FOR UPDATE it would have "
            f"read the row alongside it instead ({_driver_error(blocked.value)})"
        )
        worker_b.rollback()

        claimed.status = NotificationStatus.SENT
        claimed.sent_at = utc_now()
        worker_a.commit()

        seen = NotificationRepository(worker_b, tenant_id).claim_for_dispatch(notification_id)
        assert seen is not None
        assert seen.status == NotificationStatus.SENT, (
            "once the lock is released the second worker must see the send that already "
            "happened, so the policy refuses it rather than sending again"
        )
        worker_b.rollback()
    finally:
        worker_a.rollback()
        worker_b.rollback()
        worker_a.close()
        worker_b.close()


def test_the_scheduler_claim_skips_a_row_another_worker_holds(migrated: Engine) -> None:
    """``4e7dd5c`` added a *second* caller of the lock; it must skip, not block.

    ``claim_next_due`` asks for ``SKIP LOCKED``, so N schedulers share the table
    without queueing behind each other. The assertion is the row *identity*: B
    must be handed the other due row, which is only possible if A's locked row was
    skipped. A blocked or duplicated claim both fail here.
    """
    tenant_id, contract_id = _seed_contract(migrated)
    seeded = {
        _pending(migrated, tenant_id, contract_id, template="T-60"),
        _pending(migrated, tenant_id, contract_id, template="T-30"),
    }

    factory = sessionmaker(bind=migrated, expire_on_commit=False)
    worker_a = factory()
    worker_b = factory()
    try:
        claimed_a = claim_next_due(worker_a)
        assert claimed_a is not None, "the scheduler should claim a due row"
        claimed_b = claim_next_due(worker_b)
        assert claimed_b is not None, "worker B must not block on worker A's locked row"
        assert claimed_a.id != claimed_b.id, "the same row was handed to two schedulers"
        assert {int(claimed_a.id), int(claimed_b.id)} == seeded
    finally:
        worker_a.rollback()
        worker_b.rollback()
        worker_a.close()
        worker_b.close()


def test_the_login_attempt_index_serves_the_window_query(migrated: Engine) -> None:
    """Migration ``0005`` must ship the exact index the throttle's query needs.

    ``LoginAttemptRepository.window`` filters on ``key_hash`` (equality) and then
    ranges over ``attempted_at``, so the index has to lead with the first and
    carry the second. Getting that wrong would still return *correct* rows, only
    slowly, and nothing else in the suite would notice — which is why the column
    order is read back from ``information_schema`` rather than inferred from the
    model, whose ``Index`` declaration is not what MySQL actually built.
    """
    assert "login_attempts" in set(inspect(migrated).get_table_names())

    with migrated.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT COLUMN_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'login_attempts' "
                "AND INDEX_NAME = 'ix_login_attempts_key_hash_attempted_at' "
                "ORDER BY SEQ_IN_INDEX"
            )
        ).all()

    assert [row[0] for row in rows] == ["key_hash", "attempted_at"], (
        "the composite index must lead with the equality column and then the range "
        f"column; MySQL built {[row[0] for row in rows]!r}"
    )

    # Exact-width digest and second precision, both load-bearing: a wider column
    # would accept a malformed key, and a fractional-second column would store
    # the value ``record`` floors and undo the fix asserted below.
    assert _column_type(migrated, "login_attempts", "key_hash") == "varchar(64)"
    assert _column_type(migrated, "login_attempts", "attempted_at") == "datetime"


def _throttle_key() -> str:
    """A 64-character hex digest, unique per call, so tests cannot collide."""
    return uuid.uuid4().hex + uuid.uuid4().hex


def _with_microseconds(microsecond: int) -> datetime:
    """A "now" carrying a fraction MySQL would round — never the real clock."""
    return utc_now().replace(microsecond=microsecond)


def test_a_recorded_attempt_is_floored_rather_than_rounded_forward(migrated: Engine) -> None:
    """Why ``LoginAttemptRepository.record`` floors its own timestamp.

    MySQL stores ``DATETIME`` with no fractional seconds and *rounds* on write,
    so a failure entered with 0.7s is stored a moment in the future. The window
    then measures "how long until the oldest failure ages out" from an instant
    that has not happened yet, and ``Retry-After`` outlives the configured
    window. The model's default already floors, but the login route passes an
    explicit ``utc_now()``, so the repository is the only enforcement point that
    covers every caller.

    Both halves are asserted, because either alone proves nothing: that raw
    insertion really does round *forward* on this server (so the floor is not
    guarding against an imaginary hazard), and that ``record`` does not.
    """
    fractional = _with_microseconds(700_000)
    floored = fractional.replace(microsecond=0)

    raw_key = _throttle_key()
    with migrated.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO login_attempts (id, key_hash, attempted_at) "
                "VALUES (:id, :key_hash, :attempted_at)"
            ),
            {"id": str(uuid.uuid4()), "key_hash": raw_key, "attempted_at": fractional},
        )
        raw_stored = connection.execute(
            text("SELECT attempted_at FROM login_attempts WHERE key_hash = :key_hash"),
            {"key_hash": raw_key},
        ).scalar_one()

    assert raw_stored == floored + timedelta(seconds=1), (
        "this test is only meaningful if MySQL rounds a .7s fraction up; it stored "
        f"{raw_stored!r} for {fractional!r}"
    )

    recorded_key = _throttle_key()
    factory = sessionmaker(bind=migrated, expire_on_commit=False)
    with factory() as session:
        LoginAttemptRepository(session).record(
            recorded_key, now=fractional, prune_before=fractional - timedelta(seconds=900)
        )
        session.commit()

    with factory() as session:
        stored = session.execute(
            text("SELECT attempted_at FROM login_attempts WHERE key_hash = :key_hash"),
            {"key_hash": recorded_key},
        ).scalar_one()
        assert stored == floored, (
            f"record() must floor {fractional!r} to {floored!r}, stored {stored!r}"
        )

        # The stored value has to be usable by the query that consumes it: a
        # failure rounded into the future would fall outside a window that starts
        # at "now", so it would never be counted and never block anything.
        count, earliest = LoginAttemptRepository(session).window(
            recorded_key, since=floored - timedelta(seconds=1)
        )
    assert count == 1
    assert earliest == floored


def test_confidence_enforces_its_declared_scale_without_the_quantiser(migrated: Engine) -> None:
    """``c90423b`` repairs a SQLite defect, so on MySQL it should be a no-op.

    That is a falsifiable claim, and this is the only place it can be tested: the
    value is inserted with **raw SQL**, bypassing :meth:`ExtractedField._quantise_confidence`,
    so what is measured is MySQL's own behaviour rather than the fix. If MySQL
    stored the unrounded float, the fix would be load-bearing here too — and the
    keyset page boundary would need a different answer than "quantise on write".
    """
    assert _column_type(migrated, "extracted_fields", "confidence") == "decimal(4,3)"

    tenant_id, contract_id = _seed_contract(migrated)
    noisy = 0.1 * 3
    assert noisy != 0.3, "the whole point is a value that is not exactly representable"

    with migrated.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO extracted_fields "
                "(tenant_id, contract_id, field_name, confidence, status, version, "
                "created_at, updated_at) "
                "VALUES (:tenant_id, :contract_id, :field_name, :confidence, :status, 1, "
                ":now, :now)"
            ),
            {
                "tenant_id": str(tenant_id),
                "contract_id": contract_id,
                "field_name": FieldName.EMAIL.value,
                "confidence": noisy,
                "status": FieldStatus.APPROVED.value,
                "now": utc_now(),
            },
        )
        stored = connection.execute(
            text("SELECT confidence FROM extracted_fields WHERE contract_id = :contract_id"),
            {"contract_id": contract_id},
        ).scalar_one()

    assert stored == Decimal("0.300"), (
        f"MySQL should round to the declared scale on write, got {stored!r}"
    )


def _seed_user(engine: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    """A committed tenant and one admin user: ``(tenant_id, user_id)``.

    Committed rather than flushed because the refresh tests open *separate*
    sessions on purpose — an uncommitted parent row would be invisible to them,
    and a cascade test that cannot see the child proves nothing.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        tenant = Tenant(name="Refresh Tenant", slug=f"rt-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        user = User(
            tenant_id=tenant.id,
            email=f"rt-{uuid.uuid4().hex[:8]}@example.test",
            password_hash="integration-test-not-a-real-argon2-hash",
            role=UserRole.ADMIN,
            token_epoch=0,
        )
        session.add(user)
        session.commit()
        return tenant.id, user.id


def _issue_token(
    engine: Engine,
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    family_id: uuid.UUID | None = None,
) -> tuple[str, str]:
    """Mint and persist one refresh token. Returns ``(raw, digest)``.

    Goes through the real repository rather than raw SQL so these tests exercise
    the same write path the route uses, including its timestamp flooring.
    """
    raw = mint_refresh_token()
    digest = refresh_token_hash(raw)
    issued_at = utc_now()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        RefreshTokenRepository(session).create(
            user_id=user_id,
            tenant_id=tenant_id,
            family_id=family_id or uuid.uuid4(),
            token_hash=digest,
            token_epoch=0,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(days=7),
        )
        session.commit()
    return raw, digest


def test_the_refresh_token_table_stores_digests_under_a_unique_index(migrated: Engine) -> None:
    """Migration ``0006``: three schema facts the redemption path depends on.

    **The digest is unique.** The lookup is ``WHERE token_hash = ?`` and treats
    the single row it finds as authority over which user and tenant the session
    belongs to. Without the constraint, a duplicate digest would make
    ``scalar_one_or_none`` raise rather than refuse — a 500 instead of a 401 —
    and, worse, two logins could resolve to whichever row the planner returned
    first.

    **It is exactly 64 characters.** A hex SHA-256 and nothing wider, so a
    truncated or over-long digest is a bug the column rejects instead of data.

    **The timestamps carry no fractional seconds.** ``expires_at`` is the
    deadline; a value rounded *forward* on write would let a token outlive it.
    The same trap ``LoginAttemptRepository.record`` floors against, and only the
    server's own ``COLUMN_TYPE`` shows whether the column can even hold a
    fraction.
    """
    with migrated.connect() as connection:
        uniques = connection.execute(
            text(
                "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME "
                "FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'refresh_tokens' "
                "AND COLUMN_NAME = 'token_hash' "
                "ORDER BY INDEX_NAME"
            )
        ).all()

    assert [(name, int(non_unique), column) for name, non_unique, column in uniques] == [
        ("uq_refresh_tokens_token_hash", 0, "token_hash")
    ], f"token_hash must carry exactly one single-column UNIQUE index, MySQL built {uniques!r}"

    assert _column_type(migrated, "refresh_tokens", "token_hash") == "varchar(64)"
    assert _column_type(migrated, "refresh_tokens", "issued_at") == "datetime"
    assert _column_type(migrated, "refresh_tokens", "expires_at") == "datetime"


def test_the_same_digest_cannot_be_stored_twice(migrated: Engine) -> None:
    """The constraint above, exercised rather than merely inspected."""
    tenant_id, user_id = _seed_user(migrated)
    _, digest = _issue_token(migrated, user_id=user_id, tenant_id=tenant_id)

    occurred_at = utc_now()
    factory = sessionmaker(bind=migrated, expire_on_commit=False)
    with factory() as session:
        RefreshTokenRepository(session).create(
            user_id=user_id,
            tenant_id=tenant_id,
            family_id=uuid.uuid4(),
            token_hash=digest,
            token_epoch=0,
            issued_at=occurred_at,
            expires_at=occurred_at + timedelta(days=7),
        )
        with pytest.raises(IntegrityError) as duplicate:
            session.commit()

    assert _driver_error(duplicate.value).startswith("MySQL error 1062"), (
        "a second row for one digest must be refused by the server, not silently "
        f"accepted ({_driver_error(duplicate.value)})"
    )


def test_deleting_the_user_takes_its_live_sessions_with_it(migrated: Engine) -> None:
    """The ``ON DELETE CASCADE`` on ``user_id``, proven by InnoDB.

    A refresh token outlives the access token it was issued beside, so a deleted
    user whose session survived would be an identity that can still mint access
    tokens. SQLite only enforces this because the test harness turns
    ``PRAGMA foreign_keys`` on; MySQL enforces it unconditionally, which is the
    configuration production actually runs.
    """
    tenant_id, user_id = _seed_user(migrated)
    survivor_tenant_id, survivor_user_id = _seed_user(migrated)
    _issue_token(migrated, user_id=user_id, tenant_id=tenant_id)
    _issue_token(migrated, user_id=user_id, tenant_id=tenant_id)
    _issue_token(migrated, user_id=survivor_user_id, tenant_id=survivor_tenant_id)

    with migrated.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": str(user_id)})
        gone = connection.execute(
            text("SELECT COUNT(*) FROM refresh_tokens WHERE user_id = :id"),
            {"id": str(user_id)},
        ).scalar_one()
        kept = connection.execute(
            text("SELECT COUNT(*) FROM refresh_tokens WHERE user_id = :id"),
            {"id": str(survivor_user_id)},
        ).scalar_one()

    assert gone == 0, "deleting a user must not leave a redeemable session behind"
    assert kept == 1, "the cascade must be scoped to the deleted user, not the whole table"


def test_claim_for_rotation_is_decided_by_the_database(migrated: Engine) -> None:
    """The rotation race, on a server that really has row locks.

    ``claim_for_rotation`` is a conditional ``UPDATE`` whose affected-row count is
    the answer to "did *I* spend this token", because a Python-side
    ``if token.used_at is None`` would let two racing requests both rotate one
    token into two live sessions. Two halves, and neither is observable on
    SQLite:

    * Worker B must **wait** on worker A's in-flight ``UPDATE`` and give up after
      ``innodb_lock_wait_timeout``. A read-no-lock implementation would return
      immediately instead.
    * Once A commits, B's identical statement must report ``rowcount == 0``. That
      is the value the route turns into "replay detected, revoke the family", so
      it has to come from the server rather than from a driver heuristic.
    """
    tenant_id, user_id = _seed_user(migrated)
    _, digest = _issue_token(migrated, user_id=user_id, tenant_id=tenant_id)

    factory = sessionmaker(bind=migrated, expire_on_commit=False)
    worker_a = factory()
    worker_b = factory()
    try:
        assert RefreshTokenRepository(worker_a).claim_for_rotation(digest, now=utc_now()) is True

        worker_b.execute(text(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_SECONDS}"))
        with pytest.raises(OperationalError) as blocked:
            RefreshTokenRepository(worker_b).claim_for_rotation(digest, now=utc_now())
        assert _driver_error(blocked.value).startswith(f"MySQL error {LOCK_WAIT_TIMEOUT}"), (
            "the claim must take an exclusive lock on the row, so a concurrent claimer "
            f"waits instead of spending the same token twice ({_driver_error(blocked.value)})"
        )
        worker_b.rollback()

        worker_a.commit()

        assert (
            RefreshTokenRepository(worker_b).claim_for_rotation(digest, now=utc_now()) is False
        ), (
            "a token already spent must report zero affected rows; that count is how the "
            "route detects a replay and revokes the family"
        )
        worker_b.rollback()
    finally:
        worker_a.rollback()
        worker_b.rollback()
        worker_a.close()
        worker_b.close()


def test_revoke_family_reports_the_rows_it_killed(migrated: Engine) -> None:
    """``revoke_family`` returns MySQL's affected count, so the number matters.

    Three tokens in one family and one in another: a count of "everything the
    user owns" would log the victim out of a second browser, and a count of "the
    row I found" would leave the successor alive — which is precisely the copy an
    attacker who already rotated holds.

    The second revoke must report 0. MySQL reports *changed* rows rather than
    matched ones, and the ``revoked_at IS NULL`` predicate is what keeps that
    honest: re-revoking cannot reset the original revocation time, so the
    evidence of when a session was killed is not overwritten by a later logout.
    """
    tenant_id, user_id = _seed_user(migrated)
    family_id = uuid.uuid4()
    for _ in range(3):
        _issue_token(migrated, user_id=user_id, tenant_id=tenant_id, family_id=family_id)
    _, other_digest = _issue_token(migrated, user_id=user_id, tenant_id=tenant_id)

    factory = sessionmaker(bind=migrated, expire_on_commit=False)
    with factory() as session:
        assert RefreshTokenRepository(session).revoke_family(family_id, now=utc_now()) == 3
        session.commit()

    with factory() as session:
        assert RefreshTokenRepository(session).revoke_family(family_id, now=utc_now()) == 0

        survivor = RefreshTokenRepository(session).by_hash(other_digest)
        assert survivor is not None, "the other family's token must still exist"
        assert survivor.revoked_at is None, "revoking one session must not end the other"
        session.rollback()
