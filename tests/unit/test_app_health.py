"""Integration — full app startup + health flow.

Why cast()?
    `TestClient.app` is declared as `ASGIApp` (a Union that includes
    `Callable` and `_WrapASGI2`). Neither of these declares `.state`.
    At runtime, `client.app` is a `FastAPI` instance with `.state`.
    `cast(FastAPI, ...)` narrows the type for Pylance — safe because
    the app factory always returns a `FastAPI` instance.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI
from starlette.testclient import TestClient


def test_app_lifespan_starts_and_stops(client: TestClient) -> None:
    """Lifespan wires kafka_producer + rate limiter into app.state."""
    app = cast(FastAPI, client.app)

    # After lifespan startup, these are set in main.py
    assert app.state.kafka_producer is not None
    assert app.state.limiter is not None


def test_health_flow_end_to_end(client: TestClient) -> None:
    """Full request flow: /health → /health/ready → /metrics → /."""
    # 1. Liveness
    live = client.get("/health")
    assert live.status_code == 200
    rid = live.headers["X-Request-ID"]

    # 2. Readiness — X-Request-ID must be echoed
    ready = client.get("/health/ready", headers={"X-Request-ID": rid})
    assert ready.status_code == 200
    assert ready.headers["X-Request-ID"] == rid

    # 3. Metrics
    metrics = client.get("/metrics")
    assert metrics.status_code == 200

    # 4. Root endpoint
    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["service"] == "contract-agent"


def test_app_state_has_expected_attributes(client: TestClient) -> None:
    """FastAPI app.state exposes kafka_producer + limiter after startup."""
    app = cast(FastAPI, client.app)

    # Access via getattr to be type-safe regardless of attribute presence
    kafka_producer: Any = app.state.kafka_producer
    limiter: Any = app.state.limiter

    assert kafka_producer is not None
    assert limiter is not None


def test_app_startup_idempotent(client: TestClient) -> None:
    """Multiple sequential requests don't break app state."""
    for _ in range(3):
        r = client.get("/health")
        assert r.status_code == 200

    # State still intact
    app = cast(FastAPI, client.app)
    assert app.state.kafka_producer is not None
