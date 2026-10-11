"""Integration — change password flow with real MySQL."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def _register(
    api: AsyncClient,
    email: str,
    password: str = "Sup3rSecret!Pass",
) -> dict:
    r = await api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Tester"},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_change_password_success(api: AsyncClient) -> None:
    tokens = await _register(api, "chp-success@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    r = await api.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": "Sup3rSecret!Pass",
            "new_password": "BrandNew!Pass99",
        },
        headers=headers,
    )
    assert r.status_code == 204, r.text

    # Old password no longer works
    r = await api.post(
        "/api/v1/auth/login",
        json={"email": "chp-success@example.com", "password": "Sup3rSecret!Pass"},
    )
    assert r.status_code == 401, r.text

    # New password works
    r = await api.post(
        "/api/v1/auth/login",
        json={"email": "chp-success@example.com", "password": "BrandNew!Pass99"},
    )
    assert r.status_code == 200, r.text


async def test_change_password_wrong_current(api: AsyncClient) -> None:
    tokens = await _register(api, "chp-wrong@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    r = await api.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": "WrongCurrent!1",
            "new_password": "BrandNew!Pass99",
        },
        headers=headers,
    )
    assert r.status_code == 401, r.text


async def test_change_password_same_as_current_rejected(api: AsyncClient) -> None:
    tokens = await _register(api, "chp-same@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    r = await api.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": "Sup3rSecret!Pass",
            "new_password": "Sup3rSecret!Pass",
        },
        headers=headers,
    )
    assert r.status_code == 409, r.text


async def test_change_password_requires_auth(api: AsyncClient) -> None:
    r = await api.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": "Sup3rSecret!Pass",
            "new_password": "BrandNew!Pass99",
        },
    )
    assert r.status_code == 401, r.text


async def test_change_password_revokes_current_session(api: AsyncClient) -> None:
    """After change, the old access token should be blacklisted."""
    tokens = await _register(api, "chp-revoke@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    r = await api.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": "Sup3rSecret!Pass",
            "new_password": "BrandNew!Pass99",
        },
        headers=headers,
    )
    assert r.status_code == 204, r.text

    # Old token now blacklisted → 401
    r = await api.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 401, r.text
