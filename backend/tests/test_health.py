"""Liveness endpoint and correlation-id behaviour."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.middleware import SAFE_ID_RE


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "contract-expiry-notification-agent"
    assert body["environment"] == "test"
    assert isinstance(body["uptime_seconds"], float)


def test_health_generates_correlation_headers(client: TestClient) -> None:
    response = client.get("/health")

    request_id = response.headers["x-request-id"]
    trace_id = response.headers["x-trace-id"]
    assert SAFE_ID_RE.match(request_id)
    assert len(trace_id) == 32


def test_health_echoes_safe_inbound_request_id(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "abc-123.req:1"})

    assert response.headers["x-request-id"] == "abc-123.req:1"


def test_health_replaces_unsafe_inbound_request_id(client: TestClient) -> None:
    unsafe = "abc<script>alert(1)</script>"

    response = client.get("/health", headers={"X-Request-ID": unsafe})

    echoed = response.headers["x-request-id"]
    assert echoed != unsafe
    assert SAFE_ID_RE.match(echoed)


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"]
    assert error["request_id"] == response.headers["x-request-id"]
    assert error["trace_id"] == response.headers["x-trace-id"]


def test_root_describes_service(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "contract-expiry-notification-agent"
    assert body["api"] == "/api/v1"


def test_openapi_available_in_non_production(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
