"""Health endpoint tests."""

from __future__ import annotations

from starlette.testclient import TestClient


def test_liveness_returns_ok(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "contract-agent"
    assert body["env"] == "test"
    assert "timestamp" in body


def test_readiness_returns_checks(client: TestClient) -> None:
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert "checks" in body
    assert set(body["checks"].keys()) >= {"db", "kafka", "redis", "ollama"}


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


def test_security_headers_present(client: TestClient) -> None:
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_root_endpoint(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["service"] == "contract-agent"


def test_metrics_endpoint(client: TestClient) -> None:
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "http_request" in r.text or "python_info" in r.text


def test_404_returns_consistent_shape(client: TestClient) -> None:
    r = client.get("/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert set(body.keys()) == {"error", "status", "detail", "path", "request_id"}
    assert body["status"] == 404
