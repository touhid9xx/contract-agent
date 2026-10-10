"""Pytest fixtures — isolated settings, test client, mocks.

Fixtures hierarchy:
    env vars (session) → settings (function) → app (function) → client (function)
                                       ↘ mocks (kafka, llm, secrets)

Why isolation?
    - Tests must NOT touch real DB / Kafka / Redis / Ollama
    - Override env vars BEFORE importing app modules
    - Cache-clear Settings per session
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

if TYPE_CHECKING:
    from starlette.testclient import TestClient


# ============================================================
# FORCE TEST ENV *BEFORE* IMPORTING APP MODULES
# ============================================================
os.environ.update(
    {
        "APP_ENV": "test",
        "APP_DEBUG": "false",
        "APP_LOG_LEVEL": "WARNING",  # quieter tests
        "JWT_SECRET_KEY": "x" * 64,
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(b"0" * 32).decode(),
        # --- MySQL (docker-compose MySQL is on host port 3307) ---
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_PORT": "3307",
        "MYSQL_USER": "contract",
        "MYSQL_PASSWORD": "contract",
        "MYSQL_DB": "contract_agent",
        # --- Redis (docker-compose Redis on 6379; use DB 1 for tests) ---
        "REDIS_URL": "redis://localhost:6379/1",
        # --- Feature flags for tests ---
        "KAFKA_ENABLED": "false",
        "KAFKA_BOOTSTRAP_SERVERS": "localhost:9092",
        "LLM_MOCK": "true",
        "SMS_MOCK": "true",
        "EMAIL_MOCK": "true",
        "VOICE_MOCK": "true",
        "OTEL_ENABLED": "false",
        "PROMETHEUS_ENABLED": "true",
        "SCHEDULE_ENABLED": "false",
        "SKIP_VIRUS_SCAN": "true",
        "SECRETS_PROVIDER": "env",
        "TENANT_ENFORCE": "false",  # M7 turns this on
    }
)


# ============================================================
# SESSION-LEVEL: clear Settings LRU cache once per session
# ============================================================
@pytest.fixture(scope="session", autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Ensure Settings is re-read after env overrides."""
    from contract_agent.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()  # type: ignore[attr-defined]


# ============================================================
# SETTINGS
# ============================================================
@pytest.fixture(scope="session")
def settings():
    """Fresh Settings instance — reflects test env vars.

    Session-scoped so session-scoped integration fixtures (e.g., test DB
    creation) can depend on it. get_settings() is itself an LRU-cached
    singleton, so session vs. function scope is behaviorally identical.
    """
    from contract_agent.config import get_settings

    return get_settings()


# ============================================================
# APP + CLIENT
# ============================================================
@pytest.fixture()
def app(settings):
    """FastAPI app instance — fresh per test."""
    from contract_agent.main import create_app

    return create_app()


@pytest.fixture()
def client(app, mock_session_factory) -> Iterator[TestClient]:
    from starlette.testclient import TestClient

    # Inject mock session factory if the app tried to create a real one
    # (app.state.session_factory may be None if DB unreachable in test env)
    if getattr(app.state, "session_factory", None) is None:
        app.state.session_factory = mock_session_factory

    with TestClient(app) as c:
        yield c


# ============================================================
# MOCK: KAFKA PRODUCER
# ============================================================
@pytest.fixture()
def mock_kafka_producer() -> MagicMock:
    """Mock KafkaProducer with async publish/start/stop."""
    producer = MagicMock()
    producer.start = AsyncMock(return_value=None)
    producer.stop = AsyncMock(return_value=None)
    producer.publish = AsyncMock(return_value=True)
    return producer


# ============================================================
# MOCK: KAFKA CONSUMER
# ============================================================
@pytest.fixture()
def mock_kafka_consumer() -> MagicMock:
    consumer = MagicMock()
    consumer.start = AsyncMock(return_value=None)
    consumer.stop = AsyncMock(return_value=None)
    consumer.run = AsyncMock(return_value=None)
    return consumer


# ============================================================
# MOCK: LLM CLIENT (Ollama)
# ============================================================
@pytest.fixture()
def mock_llm_response() -> dict[str, Any]:
    """Canned LLM decision — used by agent tests in M25."""
    return {
        "channel": "email",
        "reasoning": "Customer has email and opted in",
        "confidence": 0.9,
    }


@pytest.fixture()
def mock_llm_client(mock_llm_response: dict[str, Any]) -> MagicMock:
    """Mock Ollama client with deterministic response."""
    client = MagicMock()
    client.generate = AsyncMock(return_value=mock_llm_response)
    client.chat = AsyncMock(return_value=mock_llm_response)
    return client


# ============================================================
# MOCK: SECRETS PROVIDER
# ============================================================
@pytest.fixture()
def mock_secrets_provider() -> MagicMock:
    provider = MagicMock()
    provider.get = MagicMock(side_effect=lambda key, default=None: default)
    return provider


# ============================================================
# MOCK: REDIS
# ============================================================
@pytest.fixture()
def mock_redis() -> MagicMock:
    """In-memory Redis mock — dict-backed."""
    store: dict[str, Any] = {}

    redis = MagicMock()
    redis.get = AsyncMock(side_effect=lambda k: store.get(k))
    redis.set = AsyncMock(side_effect=lambda k, v, **_: store.__setitem__(k, v) or True)
    redis.delete = AsyncMock(
        side_effect=lambda *keys: sum(store.pop(k, None) is not None for k in keys)
    )
    redis.exists = AsyncMock(side_effect=lambda k: int(k in store))
    redis._store = store  # escape hatch for assertions
    return redis


# ============================================================
# HELPERS
# ============================================================
@pytest.fixture()
def fixed_uuid() -> str:
    """Deterministic UUID for assertions."""
    return "00000000-0000-0000-0000-000000000abc"


@pytest.fixture()
def default_tenant_id() -> str:
    """Matches TENANT_DEFAULT_ID from .env.example."""
    return "00000000-0000-0000-0000-000000000001"


# ============================================================
# MOCK: SESSION FACTORY (for tests without Docker)
# ============================================================
@pytest.fixture()
def mock_session_factory() -> MagicMock:
    """Mock async session factory — returns a mock AsyncSession."""
    from sqlalchemy.ext.asyncio import AsyncSession

    session = MagicMock(spec=AsyncSession)
    session.commit = AsyncMock(return_value=None)
    session.rollback = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=1)))
    session.close = AsyncMock(return_value=None)

    # Context manager protocol
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=session)
    return factory


# ============================================================
# AUTH FIXTURES
# ============================================================
@pytest.fixture()
def db_session_mock() -> MagicMock:
    """Mock AsyncSession that supports execute + add + flush + refresh."""
    from sqlalchemy.ext.asyncio import AsyncSession

    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


@pytest.fixture()
def valid_password() -> str:
    """Meets 12-char + 4-class policy."""
    return "Sup3rSecret!Pass"


@pytest.fixture()
def valid_user_payload(valid_password: str) -> dict[str, str]:
    return {
        "email": "alice@example.com",
        "password": valid_password,
        "full_name": "Alice",
    }
