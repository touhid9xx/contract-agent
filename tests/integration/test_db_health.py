"""Integration — MySQL via testcontainers + health endpoint.

Requires Docker. Skipped if Docker is unavailable.

Why the `integration` marker?
    The default pytest run uses `-m "not integration"` to skip these
    (they spawn testcontainers/Docker), keeping the fast unit-test loop
    clean. Run explicitly with:  pytest -m integration
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from contract_agent.config import get_settings
from contract_agent.db.session import create_engine, create_session_factory
from contract_agent.main import create_app


def _docker_available() -> bool:
    try:
        import docker  # type: ignore[import-untyped]

        client = docker.from_env()
        client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


# Both marks: integration (deselected by default) + skipif (no Docker).
# `pytestmark` as a list applies ALL marks to every test in this module.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _docker_available(),
        reason="Docker not available",
    ),
]


@pytest.fixture(scope="module")
def mysql_container() -> Iterator[object]:
    """Start a MySQL 8 testcontainer for the module."""
    from testcontainers.community.mysql import MySqlContainer  # type: ignore[import-untyped]

    container = MySqlContainer("mysql:8.0")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture()
def app_with_db(mysql_container, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """App with real MySQL wired in."""
    # Point Settings at the container
    monkeypatch.setenv("MYSQL_HOST", mysql_container.get_container_host_ip())
    monkeypatch.setenv("MYSQL_PORT", str(mysql_container.get_exposed_port(3306)))
    monkeypatch.setenv("MYSQL_USER", mysql_container.username)
    monkeypatch.setenv("MYSQL_PASSWORD", mysql_container.password)
    monkeypatch.setenv("MYSQL_DB", mysql_container.dbname)

    get_settings.cache_clear()  # type: ignore[attr-defined]

    app = create_app()
    # Enter TestClient context to run lifespan (DB engine + session factory).
    # `client` value is not needed — but the `with` block IS (lifespan lifecycle).
    with TestClient(app):
        yield app
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_health_db_returns_ok(app_with_db: FastAPI) -> None:
    with TestClient(app_with_db) as client:
        r = client.get("/health/db")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok", body


def test_health_ready_includes_db_ok(app_with_db: FastAPI) -> None:
    with TestClient(app_with_db) as client:
        r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["db"]["status"] == "ok"


def test_db_engine_actually_connects(mysql_container) -> None:
    """Direct engine test — no app needed."""
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)

    async def _run() -> None:
        async with factory() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1

    asyncio.run(_run())
