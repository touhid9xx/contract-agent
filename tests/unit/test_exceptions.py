"""Exception hierarchy + handler shape tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from contract_agent.exceptions import (
    AppError,
    AuthError,
    ComplianceError,
    ConflictError,
    EscalationError,
    ExtractionError,
    ForbiddenError,
    IdempotencyError,
    NotFoundError,
    NotificationError,
    RateLimitError,
    ValidationError,
    register_exception_handlers,
)


# ------------------------------------------------------------
# Hierarchy
# ------------------------------------------------------------
def test_all_errors_subclass_app_error() -> None:
    for cls in [
        NotFoundError,
        ValidationError,
        AuthError,
        ForbiddenError,
        ConflictError,
        IdempotencyError,
        RateLimitError,
        ExtractionError,
        NotificationError,
        EscalationError,
        ComplianceError,
    ]:
        assert issubclass(cls, AppError), f"{cls.__name__} must subclass AppError"


def test_default_status_codes() -> None:
    assert NotFoundError().status_code == 404
    assert ValidationError().status_code == 422
    assert AuthError().status_code == 401
    assert ForbiddenError().status_code == 403
    assert ConflictError().status_code == 409
    assert IdempotencyError().status_code == 409
    assert RateLimitError().status_code == 429
    assert NotificationError().status_code == 502


def test_custom_message_overrides_default() -> None:
    err = NotFoundError("Contract not found")
    assert str(err) == "Contract not found"
    assert err.status_code == 404


def test_error_code_default_and_override() -> None:
    assert NotFoundError().code == "not_found"
    err = NotFoundError(code="contract_missing")
    assert err.code == "contract_missing"


# ------------------------------------------------------------
# Handler shapes
# ------------------------------------------------------------
def _make_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/raise-notfound")
    async def _nfe() -> None:
        raise NotFoundError("Contract xyz missing")

    @app.get("/raise-auth")
    async def _auth() -> None:
        raise AuthError()

    @app.get("/raise-compliance")
    async def _comp() -> None:
        raise ComplianceError(detail={"reason": "GDPR block"})

    return app


def test_notfound_handler_shape() -> None:
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        r = client.get("/raise-notfound")
    assert r.status_code == 404
    body = r.json()
    assert set(body.keys()) == {"error", "status", "detail", "path", "request_id"}
    assert body["error"] == "not_found"
    assert body["status"] == 404
    assert body["detail"] == "Contract xyz missing"
    assert body["path"] == "/raise-notfound"


def test_auth_handler_shape() -> None:
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        r = client.get("/raise-auth")
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "auth_error"
    assert body["detail"] == "Authentication failed"


def test_compliance_handler_with_custom_detail() -> None:
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        r = client.get("/raise-compliance")
    assert r.status_code == 451
    body = r.json()
    assert body["error"] == "compliance_blocked"
    assert body["detail"] == {"reason": "GDPR block"}
