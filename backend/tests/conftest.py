"""Shared pytest fixtures.

Environment is pinned to ``test`` before the application is imported so no test
can accidentally boot with production validation enabled.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Only APP_ENV is pinned here. Individual tests pass ``log_json`` explicitly
# when they care, because an exported LOG_JSON would shadow field defaults and
# silently change the outcome of the production-validation tests.
os.environ.setdefault("APP_ENV", "test")

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Iterator[None]:
    """Keep the ``lru_cache`` on get_settings from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return Settings(app_env="test", log_json=False)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
