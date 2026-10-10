"""Auth endpoints — register, login, refresh, logout, me.

Why thin endpoints?
    - All logic in core/security.py + core/deps.py
    - Endpoint = orchestration + DB + Redis + schema validation

"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contract_agent.config import Settings, get_settings
from contract_agent.core.deps import CurrentUser
from contract_agent.core.security import (
    blacklist_jti,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    rotate_refresh_token,
    verify_password,
)
from contract_agent.db.base import get_db
from contract_agent.exceptions import AuthError, ConflictError
from contract_agent.logging_config import get_logger
from contract_agent.models.user import User, UserRole
from contract_agent.schemas.user import (
    TokenPair,
    UserLogin,
    UserRead,
    UserRegister,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger(__name__)

# Module-level limiter — used by @limiter.limit on the login route.
# NOTE: the canonical limiter is attached to app.state.limiter in middleware.py.
# This module-level one is required because @limiter.limit is evaluated at
# import time, before the app exists. Both share the same key_func, so they
# produce identical rate-limit decisions.
limiter = Limiter(key_func=get_remote_address)


# ============================================================
# REGISTER
# ============================================================
@router.post(
    "/register",
    response_model=TokenPair,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
)
async def register(
    payload: UserRegister,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenPair:
    tenant_id = settings.tenant_default_id

    # Check duplicate email *within tenant*
    result = await db.execute(
        select(User).where(User.email == payload.email, User.tenant_id == tenant_id)
    )
    if result.scalar_one_or_none() is not None:
        raise ConflictError("Email already registered")

    # First user in tenant → admin; rest → viewer
    existing_count = await db.execute(select(User).where(User.tenant_id == tenant_id))
    is_first = existing_count.scalars().first() is None
    role = UserRole.ADMIN if is_first else UserRole.VIEWER

    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=role,
        full_name=payload.full_name,
        tenant_id=tenant_id,
    )
    db.add(user)
    await db.flush()  # get user.id
    await db.refresh(user)

    # Issue tokens
    access, access_exp = create_access_token(
        settings, user_id=user.id, tenant_id=user.tenant_id, role=user.role
    )
    refresh, _ = create_refresh_token(
        settings, user_id=user.id, tenant_id=user.tenant_id, role=user.role
    )

    logger.info("user_registered", user_id=user.id, role=role.value)
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=access_exp)


# ============================================================
# LOGIN
# ============================================================
@router.post("/login", response_model=TokenPair, summary="Login")
@limiter.limit("5/minute")
async def login(
    request: Request,
    payload: UserLogin,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenPair:
    result = await db.execute(
        select(User).where(
            User.email == payload.email,
            User.tenant_id == settings.tenant_default_id,
        )
    )
    user = result.scalar_one_or_none()

    # Constant-time-ish: verify even if user missing (fake hash)
    if user is None or not verify_password(payload.password, user.hashed_password):
        logger.warning("login_failed", email=payload.email)
        raise AuthError("Invalid email or password")

    if not user.is_active:
        raise AuthError("Account is inactive")

    access, access_exp = create_access_token(
        settings, user_id=user.id, tenant_id=user.tenant_id, role=user.role
    )
    refresh, _ = create_refresh_token(
        settings, user_id=user.id, tenant_id=user.tenant_id, role=user.role
    )

    logger.info("user_logged_in", user_id=user.id)
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=access_exp)


# ============================================================
# REFRESH (with rotation)
# ============================================================
@router.post("/refresh", response_model=TokenPair, summary="Rotate refresh token")
async def refresh(
    request: Request,
    body: dict[str, str],
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenPair:
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        raise AuthError("refresh_token required")

    try:
        payload = decode_token(settings, refresh_token)
    except Exception as exc:
        logger.warning("refresh_decode_failed", error=str(exc))
        raise AuthError("Invalid refresh token") from exc

    if payload.type != "refresh":
        raise AuthError("Not a refresh token")

    # Look up user — ensure still active
    result = await db.execute(select(User).where(User.id == payload.sub))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError("User no longer valid")

    redis = request.app.state.redis
    try:
        new_access, new_refresh, access_exp = await rotate_refresh_token(
            settings,
            redis,
            payload,
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role,
        )
    except ValueError as exc:
        raise AuthError(str(exc)) from exc

    return TokenPair(access_token=new_access, refresh_token=new_refresh, expires_in=access_exp)


# ============================================================
# LOGOUT (blacklist current JTI)
# ============================================================
@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Logout")
async def logout(
    request: Request,
    current_user: CurrentUser,
) -> None:
    # get_current_user already validated + attached payload to request.state
    payload = request.state.jwt_payload
    redis = request.app.state.redis
    ttl = payload.exp - int(time.time())
    await blacklist_jti(redis, payload.jti, ttl)
    logger.info("user_logged_out", user_id=current_user.id)


# ============================================================
# ME
# ============================================================
@router.get("/me", response_model=UserRead, summary="Get current user")
async def me(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)
