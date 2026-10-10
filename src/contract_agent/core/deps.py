"""FastAPI dependencies — current user, RBAC guards.

Why deps?
    - Auth check in one place, reused everywhere
    - RBAC as `Depends` — impossible to forget
    - Sets tenant_id_var → activates M4 tenant filter

"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contract_agent.config import Settings, get_settings
from contract_agent.core.security import decode_token, is_jti_blacklisted
from contract_agent.db.base import get_db
from contract_agent.exceptions import AuthError, ForbiddenError
from contract_agent.logging_config import bind_request_context, get_logger
from contract_agent.models.user import User, UserRole

logger = get_logger(__name__)


# ============================================================
# BEARER EXTRACTION
# ============================================================
async def _extract_bearer(request: Request) -> str:
    """Pull the raw token out of the `Authorization: Bearer <token>` header.

    Kept private — callers should use `get_current_user`, not this directly.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise AuthError("Missing bearer token")
    return auth[7:].strip()


# ============================================================
# CURRENT USER
# ============================================================
async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """Decode JWT, verify not blacklisted, load user. Sets tenant context."""
    token = await _extract_bearer(request)

    try:
        payload = decode_token(settings, token)
    except Exception as exc:
        raise AuthError("Invalid or expired token") from exc

    if payload.type != "access":
        raise AuthError("Not an access token")

    # Check blacklist (Redis may be None in tests)
    redis = getattr(request.app.state, "redis", None)
    if redis is not None and await is_jti_blacklisted(redis, payload.jti):
        raise AuthError("Token revoked")

    # Load user
    result = await db.execute(select(User).where(User.id == payload.sub))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError("User not found or inactive")

    # Bind correlation context for logs
    bind_request_context(user_id=user.id, tenant_id=user.tenant_id)

    # Attach payload for logout (JTI + exp needed for blacklisting)
    request.state.jwt_payload = payload
    return user


# Reusable type alias — the resolved current user.
# Use as `user: CurrentUser` inside route signatures.
CurrentUser = Annotated[User, Depends(get_current_user)]


# ============================================================
# RBAC GUARDS
# ============================================================
# A FastAPI dependency callable that takes a CurrentUser and returns it.
# Callers use this as `Depends(require_admin)`.
#
# Why CurrentUser (not plain User)? FastAPI inspects the *default value* of
# each parameter to build the DI graph. `CurrentUser` is
# `Annotated[User, Depends(get_current_user)]`, so a guard taking
# `user: CurrentUser` correctly chains the auth dependency. Using plain
# `User` here would make FastAPI treat it as a query/body param.
RoleGuard = Callable[[CurrentUser], Awaitable[User]]


def require_roles(*allowed: UserRole) -> RoleGuard:
    """Factory: returns a dependency that requires one of the given roles.

    Usage:
        @router.get("/admin", dependencies=[Depends(require_admin)])
        async def admin_endpoint() -> ...: ...

        # or inject the user directly:
        @router.get("/ops")
        async def ops_endpoint(user: OperatorUser) -> ...: ...
    """

    async def _guard(user: CurrentUser) -> User:
        if user.role not in allowed:
            logger.warning(
                "rbac_denied",
                user_id=user.id,
                user_role=user.role.value,
                required=[r.value for r in allowed],
            )
            raise ForbiddenError(f"Requires role: {', '.join(r.value for r in allowed)}")
        return user

    return _guard


# Pre-built guards — these are the ones routers will use.
require_admin = require_roles(UserRole.ADMIN)
require_operator = require_roles(UserRole.ADMIN, UserRole.OPERATOR)


# Injectable variants — use these when a route needs the *user object*.
# e.g. `async def endpoint(user: AdminUser) -> ...:`
AdminUser = Annotated[User, Depends(require_admin)]
OperatorUser = Annotated[User, Depends(require_operator)]
