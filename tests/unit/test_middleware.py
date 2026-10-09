"""Middleware tests — request context, security headers, CORS.

Why these tests?
    - Verify every response carries X-Request-ID + X-Process-Time-Ms
    - Verify OWASP security headers are attached
    - Verify CORS preflight (OPTIONS) works for allowed origins
    - Verify CORS expose_headers appear on ACTUAL responses (not preflight)

Bangla: এই টেস্টগুলো M0 middleware-এর contract verify করে — প্রতিটা request-এ
request_id propagate, timing header, security headers, এবং CORS behavior।
"""

from __future__ import annotations

from starlette.testclient import TestClient


# ============================================================
# REQUEST CONTEXT
# ============================================================
def test_request_id_header_present(client: TestClient) -> None:
    """Every response must include X-Request-ID."""
    r = client.get("/health")
    assert "X-Request-ID" in r.headers
    assert len(r.headers["X-Request-ID"]) > 0


def test_request_id_echoed_when_supplied(client: TestClient) -> None:
    """Inbound X-Request-ID must be reused (distributed tracing)."""
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"


def test_process_time_header_present(client: TestClient) -> None:
    """Every response must include X-Process-Time-Ms as a float string."""
    r = client.get("/health")
    assert "X-Process-Time-Ms" in r.headers
    float(r.headers["X-Process-Time-Ms"])  # must parse


# ============================================================
# SECURITY HEADERS
# ============================================================
def test_security_headers_present(client: TestClient) -> None:
    """OWASP-recommended security headers must be attached."""
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"


# ============================================================
# CORS — PREFLIGHT (OPTIONS)
# ============================================================
def test_cors_preflight_allowed(client: TestClient) -> None:
    """Preflight request from allowed origin returns CORS allow headers."""
    r = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


# ============================================================
# CORS — ACTUAL REQUEST (GET)
# ============================================================
def test_cors_exposes_custom_headers_on_actual_request(client: TestClient) -> None:
    """expose_headers appear on ACTUAL responses, not preflight.

    Why? CORS spec:
        - Preflight (OPTIONS) advertises what the browser MAY send.
        - Actual response (GET/POST) exposes what JS MAY read.
    """
    r = client.get(
        "/health",
        headers={"Origin": "http://localhost:3000"},
    )
    assert r.status_code == 200
    expose = r.headers.get("access-control-expose-headers", "").lower()
    assert "x-request-id" in expose
    assert "x-process-time-ms" in expose
