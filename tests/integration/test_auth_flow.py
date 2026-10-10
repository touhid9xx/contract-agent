"""Integration — full auth flow against docker-compose MySQL.

Prerequisites:
    docker compose up -d mysql

The `api` fixture is provided by tests/integration/conftest.py — do NOT
define a local one here (fixture shadowing will bypass the fake Redis wiring).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_register_login_me_flow(api: AsyncClient) -> None:
    # --- Register ---
    r = await api.post(
        "/api/v1/auth/register",
        json={
            "email": "alice@example.com",
            "password": "Sup3rSecret!Pass",
            "full_name": "Alice",
        },
    )
    assert r.status_code == 201, r.text
    tokens = r.json()
    assert "access_token" in tokens
    assert "refresh_token" in tokens

    # --- Me ---
    r = await api.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["email"] == "alice@example.com"
    assert me["role"] == "admin"

    # --- Login ---
    r = await api.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "Sup3rSecret!Pass"},
    )
    assert r.status_code == 200, r.text
    tokens2 = r.json()
    assert tokens2["access_token"] != tokens["access_token"]


async def test_duplicate_email_rejected(api: AsyncClient) -> None:
    payload = {"email": "dup@example.com", "password": "Sup3rSecret!Pass"}
    r1 = await api.post("/api/v1/auth/register", json=payload)
    assert r1.status_code == 201, r1.text

    r2 = await api.post("/api/v1/auth/register", json=payload)
    assert r2.status_code == 409, r2.text


async def test_wrong_password_rejected(api: AsyncClient) -> None:
    await api.post(
        "/api/v1/auth/register",
        json={"email": "bob@example.com", "password": "Sup3rSecret!Pass"},
    )
    r = await api.post(
        "/api/v1/auth/login",
        json={"email": "bob@example.com", "password": "WrongPassword!1"},
    )
    assert r.status_code == 401, r.text


async def test_refresh_rotation(api: AsyncClient) -> None:
    r = await api.post(
        "/api/v1/auth/register",
        json={"email": "carol@example.com", "password": "Sup3rSecret!Pass"},
    )
    assert r.status_code == 201, r.text
    tokens = r.json()

    # --- First refresh — success ---
    r = await api.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert r.status_code == 200, r.text
    new_tokens = r.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    # --- Replay old refresh → rejected ---
    r = await api.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert r.status_code == 401, r.text


async def test_logout_blacklists_access_token(api: AsyncClient) -> None:
    """Logout should blacklist the JTI so the access token is unusable."""
    # --- Register ---
    r = await api.post(
        "/api/v1/auth/register",
        json={"email": "logout@example.com", "password": "Sup3rSecret!Pass"},
    )
    assert r.status_code == 201, r.text
    tokens = r.json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    # --- Access works before logout ---
    r = await api.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 200, r.text

    # --- Logout ---
    r = await api.post("/api/v1/auth/logout", headers=headers)
    assert r.status_code == 204, r.text

    # --- Access rejected after logout ---
    r = await api.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 401, r.text
