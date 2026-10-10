"""RBAC guard tests — using a tiny in-memory app."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient

from contract_agent.core.deps import AdminUser, OperatorUser
from contract_agent.exceptions import register_exception_handlers
from contract_agent.models.user import User, UserRole


def _make_user(role: UserRole) -> User:
    return User(
        id="u1",
        tenant_id="t1",
        email="a@b.com",
        hashed_password="x",
        role=role,
        is_active=True,
    )


def _build_app(role: UserRole) -> FastAPI:
    """Build a tiny app whose routes are guarded by RBAC deps."""
    app = FastAPI()
    register_exception_handlers(app)

    async def _fake_current_user() -> User:
        return _make_user(role)

    # Override the dep chain by injecting a stub for get_current_user.
    from contract_agent.core import deps

    app.dependency_overrides[deps.get_current_user] = _fake_current_user

    @app.get("/admin-only")
    async def _admin_only(user: AdminUser) -> dict[str, str]:
        return {"ok": "admin", "user_id": user.id}

    @app.get("/operator-only")
    async def _op_only(user: OperatorUser) -> dict[str, str]:
        return {"ok": "operator", "user_id": user.id}

    return app


def test_admin_can_access_admin_route() -> None:
    app = _build_app(UserRole.ADMIN)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/admin-only")
    assert r.status_code == 200, r.text


def test_viewer_cannot_access_admin_route() -> None:
    app = _build_app(UserRole.VIEWER)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/admin-only")
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error"] == "forbidden"


def test_operator_can_access_operator_route() -> None:
    app = _build_app(UserRole.OPERATOR)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/operator-only")
    assert r.status_code == 200, r.text


def test_viewer_cannot_access_operator_route() -> None:
    app = _build_app(UserRole.VIEWER)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/operator-only")
    assert r.status_code == 403, r.text
