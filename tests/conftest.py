"""Pytest fixtures — isolated settings, test client, mocks.

Why isolated settings?
    - Tests must NOT touch real DB/Kafka/Redis
    - Override env vars before importing app
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from starlette.testclient import TestClient

# ------------------------------------------------------------
# Force test env BEFORE importing app modules
# ------------------------------------------------------------
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_DEBUG", "false")
os.environ.setdefault("JWT_SECRET_KEY", "x" * 64)
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("KAFKA_ENABLED", "false")
os.environ.setdefault("LLM_MOCK", "true")
os.environ.setdefault("SMS_MOCK", "true")
os.environ.setdefault("EMAIL_MOCK", "true")
os.environ.setdefault("VOICE_MOCK", "true")
os.environ.setdefault("OTEL_ENABLED", "false")
os.environ.setdefault("PROMETHEUS_ENABLED", "true")
os.environ.setdefault("SCHEDULE_ENABLED", "false")
os.environ.setdefault("SKIP_VIRUS_SCAN", "true")


@pytest.fixture(scope="session", autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Ensure Settings is re-read after env overrides."""
    from contract_agent.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.fixture()
def settings() -> object:
    """Fresh Settings instance (cached per test session)."""
    from contract_agent.config import get_settings

    return get_settings()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """FastAPI TestClient with app factory — no real external services."""
    from fastapi.testclient import TestClient

    from contract_agent.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
