"""Pytest fixtures — isolated settings, test client, mocks.

Fixtures hierarchy:
    env vars (session) → settings (function) → app (function) → client (function)
                                       ↘ mocks (kafka, llm, secrets)

Why isolation?
    - Tests must NOT touch real DB / Kafka / Redis / Ollama
    - Override env vars BEFORE importing app modules
    - Cache-clear Settings per session

Bangla: প্রতিটি test-এ আলাদা Settings + TestClient। KAFKA_ENABLED=false,
LLM_MOCK=true — কোনো external service লাগবে না। এটা M1-এর সবচেয়ে গুরুত্বপূর্ণ file।
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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
@pytest.fixture()
def settings():
    """Fresh Settings instance — reflects test env vars."""
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
def client(app):
    """TestClient with lifespan events triggered."""
    from starlette.testclient import TestClient

    with TestClient(app) as c:
        yield c


# ============================================================
# MOCK: KAFKA PRODUCER
# ============================================================
@pytest.fixture()
def mock_kafka_producer() -> MagicMock:
    """Mock KafkaProducer with async publish/start/stop.

    Usage:
        def test_foo(client, mock_kafka_producer):
            client.app.state.kafka_producer = mock_kafka_producer
            ...
            mock_kafka_producer.publish.assert_awaited_once()
    """
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
