"""Integration test fixtures — use the docker-compose MySQL.

Why NOT testcontainers?
    - testcontainers starts its own MySQL container, ignoring docker-compose.
    - On Windows Docker Desktop the first-boot (`mysqld --initialize`) takes
      longer than the default 120s wait strategy → TimeoutError.
    - testcontainers also runs a Reaper sidecar → extra container you didn't
      ask for.

Design:
    - Same MySQL server as docker-compose (port 3307).
    - Separate *database*: `contract_agent_test` (isolated from dev data).
    - DB setup is SYNCHRONOUS (pymysql) — avoids event-loop scope headaches.
    - App fixture wires `app.state.session_factory` AND `app.state.redis`
      directly, because httpx's ASGITransport does NOT run the ASGI lifespan
      protocol (main.lifespan would normally set these).

Prerequisites:
    docker compose up -d mysql
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pymysql
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from contract_agent.config import Settings
from contract_agent.db.base import get_db
from contract_agent.main import create_app

TEST_DB_NAME = "contract_agent_test"
_ROOT_USER = "root"
_ROOT_PASSWORD = "rootpassword"


# ============================================================
# URL BUILDERS
# ============================================================
def _test_db_url_app(settings: Settings) -> str:
    """App-user URL pointing at the test DB — used by tests."""
    return (
        f"mysql+aiomysql://{settings.mysql_user}:{settings.mysql_password}"
        f"@{settings.mysql_host}:{settings.mysql_port}/{TEST_DB_NAME}"
    )


# ============================================================
# SESSION-SCOPED (SYNC): create test DB + schema ONCE
# ============================================================
@pytest.fixture(scope="session")
def _test_database_url(settings: Settings) -> Iterator[str]:
    """Create `contract_agent_test` DB + tables via pymysql (sync)."""
    # --- 1. CREATE DATABASE + GRANT (root) ---
    conn = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=_ROOT_USER,
        password=_ROOT_PASSWORD,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{TEST_DB_NAME}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cur.execute(
                f"GRANT ALL PRIVILEGES ON `{TEST_DB_NAME}`.* TO '{settings.mysql_user}'@'%'"
            )
            cur.execute("FLUSH PRIVILEGES")
    finally:
        conn.close()

    # --- 2. CREATE TABLES via sync engine ---
    from sqlalchemy import create_engine as create_sync_engine

    import contract_agent.models.user  # noqa: F401 — register model
    from contract_agent.db.base import Base

    sync_url = (
        f"mysql+pymysql://{_ROOT_USER}:{_ROOT_PASSWORD}"
        f"@{settings.mysql_host}:{settings.mysql_port}/{TEST_DB_NAME}"
    )
    sync_engine = create_sync_engine(sync_url, echo=False)
    try:
        Base.metadata.drop_all(sync_engine)
        Base.metadata.create_all(sync_engine)
    finally:
        sync_engine.dispose()

    yield _test_db_url_app(settings)

    # --- 3. DROP DATABASE ---
    conn = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=_ROOT_USER,
        password=_ROOT_PASSWORD,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB_NAME}`")
    finally:
        conn.close()


# ============================================================
# FUNCTION-SCOPED: async engine + session, rolled back per test
# ============================================================
@pytest.fixture()
async def db_engine(_test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(_test_database_url, echo=False)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture()
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Session wrapped in an outer transaction — rolled back at teardown."""
    connection = await db_engine.connect()
    transaction = await connection.begin()

    session_factory = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    session = session_factory()

    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


# ============================================================
# IN-MEMORY REDIS MOCK
# ============================================================
@pytest.fixture()
def fake_redis() -> MagicMock:
    """Dict-backed fake async Redis — enough for auth flows."""
    store: dict[str, Any] = {}

    redis = MagicMock()

    async def _get(k: str) -> Any:
        return store.get(k)

    async def _set(k: str, v: Any, ex: int | None = None, **_kw: Any) -> bool:
        store[k] = v
        return True

    async def _delete(*keys: str) -> int:
        return sum(store.pop(k, None) is not None for k in keys)

    async def _exists(k: str) -> int:
        return int(k in store)

    async def _aclose() -> None:
        store.clear()

    redis.get = AsyncMock(side_effect=_get)
    redis.set = AsyncMock(side_effect=_set)
    redis.delete = AsyncMock(side_effect=_delete)
    redis.exists = AsyncMock(side_effect=_exists)
    redis.aclose = AsyncMock(side_effect=_aclose)
    redis._store = store
    return redis


# ============================================================
# SHARED: app + httpx AsyncClient
# ============================================================
@pytest.fixture()
async def api(
    db_session: AsyncSession,
    db_engine: AsyncEngine,
    fake_redis: MagicMock,
    settings: Settings,
) -> AsyncIterator[AsyncClient]:
    """App bound to the test DB + fake Redis, exposed as an httpx client.

    Why we bypass ASGI lifespan:
        httpx's ASGITransport does not run the ASGI lifespan protocol,
        so `app.state.session_factory` and `app.state.redis` (normally
        set in main.lifespan) would be missing. We inject them explicitly.
    """
    app = create_app()

    # 1. Override get_db → uses the rollback-per-test session
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db

    # 2. Session factory for /health/db reachability
    app.state.session_factory = async_sessionmaker(
        bind=db_engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    # 3. Fake Redis for /auth/refresh, /auth/logout
    app.state.redis = fake_redis

    # 4. Mock Kafka producer for future endpoints
    kafka_producer = MagicMock()
    kafka_producer.start = AsyncMock(return_value=None)
    kafka_producer.stop = AsyncMock(return_value=None)
    kafka_producer.publish = AsyncMock(return_value=True)
    app.state.kafka_producer = kafka_producer

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
