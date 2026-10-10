"""Consolidated health + middleware + error shape tests.

Consolidates what was previously spread across:
    - test_health.py
    - test_health_extended.py
    - test_app_health.py
    - parts of test_middleware.py

Why one file?
    - Health endpoints share fixtures (client, mock_kafka_producer)
    - All checks against /health, /health/ready, /metrics, /, 404
    - Easier to maintain one source of truth
"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI
from starlette.testclient import TestClient


# ============================================================
# LIVENESS
# ============================================================
def test_liveness_returns_ok(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "contract-agent"
    assert body["env"] == "test"
    assert "timestamp" in body


# ============================================================
# READINESS
# ============================================================
def test_readiness_returns_checks(client: TestClient) -> None:
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert "checks" in body
    assert set(body["checks"].keys()) >= {"db", "kafka", "redis", "ollama"}


def test_readiness_kafka_ok_when_producer_attached(client: TestClient, mock_kafka_producer) -> None:
    """Attach mock kafka producer → /health/ready reports kafka ok."""
    app = cast(FastAPI, client.app)
    app.state.kafka_producer = mock_kafka_producer

    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["checks"]["kafka"]["status"] in ("ok", "disabled")


# ============================================================
# REQUEST CONTEXT (middleware)
# ============================================================
def test_request_id_header_present(client: TestClient) -> None:
    r = client.get("/health")
    assert "X-Request-ID" in r.headers
    assert len(r.headers["X-Request-ID"]) > 0


def test_request_id_echoed_when_supplied(client: TestClient) -> None:
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"


def test_process_time_header_present(client: TestClient) -> None:
    r = client.get("/health")
    assert "X-Process-Time-Ms" in r.headers
    float(r.headers["X-Process-Time-Ms"])  # parses


# ============================================================
# SECURITY HEADERS
# ============================================================
def test_security_headers_present(client: TestClient) -> None:
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"


# ============================================================
# CORS
# ============================================================
def test_cors_preflight_allowed(client: TestClient) -> None:
    r = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_exposes_custom_headers_on_actual_request(client: TestClient) -> None:
    r = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 200
    expose = r.headers.get("access-control-expose-headers", "").lower()
    assert "x-request-id" in expose
    assert "x-process-time-ms" in expose


# ============================================================
# ROOT
# ============================================================
def test_root_returns_service_info(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "contract-agent"
    assert body["version"] == "0.1.0"
    assert body["docs"] == "/docs"


# ============================================================
# METRICS
# ============================================================
def test_metrics_endpoint_returns_prometheus_format(client: TestClient) -> None:
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    assert "# HELP" in body or "# TYPE" in body


# ============================================================
# 404 CONSISTENCY
# ============================================================
def test_404_returns_consistent_error_shape(client: TestClient) -> None:
    r = client.get("/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert set(body.keys()) == {"error", "status", "detail", "path", "request_id"}
    assert body["status"] == 404
    assert body["path"] == "/does-not-exist"


# ============================================================
# APP LIFESPAN / STATE
# ============================================================
def test_app_lifespan_state_wired(client: TestClient) -> None:
    """After lifespan startup, app.state has expected attributes."""
    app = cast(FastAPI, client.app)
    assert hasattr(app.state, "kafka_producer")
    assert hasattr(app.state, "limiter")


def test_app_startup_idempotent(client: TestClient) -> None:
    """Multiple sequential requests don't break app state."""
    for _ in range(3):
        r = client.get("/health")
        assert r.status_code == 200

    app = cast(FastAPI, client.app)
    assert hasattr(app.state, "kafka_producer")
