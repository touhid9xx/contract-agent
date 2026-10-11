"""Integration — admin user listing + RBAC."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def _register(api: AsyncClient, email: str) -> dict:
    r = await api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Sup3rSecret!Pass"},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_admin_can_list_users(api: AsyncClient) -> None:
    # First user → admin
    tokens = await _register(api, "admin-list@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # Register 2 more (viewer role)
    await _register(api, "user1@example.com")
    await _register(api, "user2@example.com")

    r = await api.get("/api/v1/auth/users", headers=headers)
    assert r.status_code == 200, r.text
    users = r.json()
    assert len(users) >= 3
    emails = {u["email"] for u in users}
    assert "admin-list@example.com" in emails
    assert "user1@example.com" in emails


async def test_viewer_cannot_list_users(api: AsyncClient) -> None:
    # First user is admin
    await _register(api, "admin-bootstrap@example.com")

    # Second user is viewer
    tokens = await _register(api, "viewer-user@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    r = await api.get("/api/v1/auth/users", headers=headers)
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error"] == "forbidden"


async def test_list_users_requires_auth(api: AsyncClient) -> None:
    r = await api.get("/api/v1/auth/users")
    assert r.status_code == 401


async def test_list_users_scoped_to_tenant(api: AsyncClient) -> None:
    """Only users in the current tenant are returned."""
    tokens = await _register(api, "tenant-scope@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    r = await api.get("/api/v1/auth/users", headers=headers)
    assert r.status_code == 200, r.text

    me_r = await api.get("/api/v1/auth/me", headers=headers)
    me = me_r.json()

    for user in r.json():
        assert user["tenant_id"] == me["tenant_id"]
