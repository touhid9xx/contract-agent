"""Argon2 + JWT + rotation tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from contract_agent.core.security import (
    blacklist_jti,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    is_jti_blacklisted,
    rotate_refresh_token,
    verify_password,
)
from contract_agent.models.user import UserRole


def test_argon2_hash_and_verify(settings) -> None:
    h = hash_password("Sup3rSecret!Pass")
    assert h.startswith("$argon2id$")
    assert verify_password("Sup3rSecret!Pass", h)
    assert not verify_password("wrong", h)


def test_hash_is_unique_per_call(settings) -> None:
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2  # salt differs


def test_access_token_roundtrip(settings) -> None:
    token, exp = create_access_token(settings, user_id="u1", tenant_id="t1", role=UserRole.ADMIN)
    assert exp == settings.jwt_access_token_expire_minutes * 60
    payload = decode_token(settings, token)
    assert payload.sub == "u1"
    assert payload.tenant_id == "t1"
    assert payload.role == UserRole.ADMIN
    assert payload.type == "access"


def test_refresh_token_roundtrip(settings) -> None:
    token, exp = create_refresh_token(settings, user_id="u1", tenant_id="t1", role=UserRole.VIEWER)
    assert exp == settings.jwt_refresh_token_expire_days * 24 * 3600
    payload = decode_token(settings, token)
    assert payload.type == "refresh"


def test_decode_rejects_wrong_secret(settings) -> None:
    import jwt as pyjwt

    token, _ = create_access_token(settings, user_id="u1", tenant_id="t1", role=UserRole.ADMIN)
    with pytest.raises(pyjwt.InvalidSignatureError):
        pyjwt.decode(token, "wrong-secret", algorithms=["HS256"])


@pytest.mark.asyncio
async def test_blacklist_roundtrip() -> None:
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)

    await blacklist_jti(redis, "jti-123", ttl_seconds=60)
    redis.set.assert_awaited_once()
    assert not await is_jti_blacklisted(redis, "jti-123")

    redis.get = AsyncMock(return_value="1")
    assert await is_jti_blacklisted(redis, "jti-123")


@pytest.mark.asyncio
async def test_blacklist_skips_zero_or_negative_ttl() -> None:
    redis = MagicMock()
    redis.set = AsyncMock()
    await blacklist_jti(redis, "jti-x", ttl_seconds=0)
    redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotate_refresh_issues_new_pair_and_blacklists_old(settings) -> None:
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)

    refresh_token, _ = create_refresh_token(
        settings, user_id="u1", tenant_id="t1", role=UserRole.OPERATOR
    )
    payload = decode_token(settings, refresh_token)

    # Only `new_refresh` is asserted below; prefix the others with `_`
    # to signal intent to Ruff (RUF059) and future readers.
    _new_access, new_refresh, _access_exp = await rotate_refresh_token(
        settings,
        redis,
        payload,
        user_id="u1",
        tenant_id="t1",
        role=UserRole.OPERATOR,
    )

    assert new_refresh != refresh_token
    redis.set.assert_awaited_once()  # old jti blacklisted


@pytest.mark.asyncio
async def test_rotate_refresh_rejects_replay(settings) -> None:
    redis = MagicMock()
    redis.set = AsyncMock()
    redis.get = AsyncMock(return_value="1")  # already blacklisted

    refresh_token, _ = create_refresh_token(
        settings, user_id="u1", tenant_id="t1", role=UserRole.OPERATOR
    )
    payload = decode_token(settings, refresh_token)

    with pytest.raises(ValueError, match="replay"):
        await rotate_refresh_token(
            settings,
            redis,
            payload,
            user_id="u1",
            tenant_id="t1",
            role=UserRole.OPERATOR,
        )
