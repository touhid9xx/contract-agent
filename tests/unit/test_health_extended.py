"""Extended health endpoint tests — 404, metrics, root."""

from __future__ import annotations

from starlette.testclient import TestClient


def test_root_returns_service_info(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "contract-agent"
    assert body["version"] == "0.1.0"
    assert body["docs"] == "/docs"


def test_metrics_endpoint_returns_prometheus_format(client: TestClient) -> None:
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    # Standard Prometheus exposition markers
    body = r.text
    assert "# HELP" in body or "# TYPE" in body


def test_404_returns_consistent_error_shape(client: TestClient) -> None:
    r = client.get("/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert set(body.keys()) == {"error", "status", "detail", "path", "request_id"}
    assert body["status"] == 404
    assert body["path"] == "/does-not-exist"


def test_health_ready_reports_checks(client: TestClient) -> None:
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert "checks" in body
    assert set(body["checks"].keys()) >= {"db", "kafka", "redis", "ollama"}


def test_health_ready_kafka_ok_when_producer_attached(
    client: TestClient, mock_kafka_producer
) -> None:
    client.app.state.kafka_producer = mock_kafka_producer  # type: ignore[attr-defined]
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["checks"]["kafka"] == "ok"
