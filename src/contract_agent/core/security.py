"""Password hashing + JWT creation/validation + refresh rotation.

Why Argon2id?
    - Winner of Password Hashing Competition (2015)
    - Resistant to GPU/ASIC attacks
    - Modern default for new apps (OWASP recommends)

Why JWT rotation?
    - Refresh tokens are long-lived (7d) — high value if stolen
    - On each refresh, we issue a NEW refresh token AND blacklist the old one
    - If attacker replays the old one → blacklisted → session killed
    - Redis stores JTI (JWT ID) → TTL = remaining lifetime
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from passlib.context import CryptContext
from redis.asyncio import Redis

from contract_agent.config import Settings
from contract_agent.logging_config import get_logger
from contract_agent.models.user import UserRole
from contract_agent.schemas.user import TokenPayload

logger = get_logger(__name__)


# ============================================================
# PASSWORD HASHING
# ============================================================
# Argon2id parameters — tuned for ~50ms hash time on modern hardware
pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__type="ID",  # Argon2id
    argon2__memory_cost=65536,  # 64 MiB
    argon2__time_cost=3,  # 3 iterations
    argon2__parallelism=4,  # 4 threads
)


def hash_password(plain: str) -> str:
    """Hash a password with Argon2id."""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against a hash. Constant-time."""
    try:
        return pwd_context.verify(plain, hashed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("password_verify_error", error=str(exc))
        return False


def needs_rehash(hashed: str) -> bool:
    """True if hash was made with outdated parameters."""
    return pwd_context.needs_update(hashed)


# ============================================================
# JWT
# ============================================================
def _now() -> datetime:
    return datetime.now(tz=UTC)


def create_access_token(
    settings: Settings,
    *,
    user_id: str,
    tenant_id: str,
    role: UserRole,
) -> tuple[str, int]:
    """Create an access token. Returns (token, expires_in_seconds)."""
    now = _now()
    expires_in = settings.jwt_access_token_expire_minutes * 60
    payload: dict[str, Any] = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "role": role.value,
        "jti": str(uuid.uuid4()),
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, expires_in


def create_refresh_token(
    settings: Settings,
    *,
    user_id: str,
    tenant_id: str,
    role: UserRole,
) -> tuple[str, int]:
    """Create a refresh token. Returns (token, expires_in_seconds)."""
    now = _now()
    expires_in = settings.jwt_refresh_token_expire_days * 24 * 3600
    payload: dict[str, Any] = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "role": role.value,
        "jti": str(uuid.uuid4()),
        "type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, expires_in


def decode_token(settings: Settings, token: str) -> TokenPayload:
    """Decode + validate a JWT. Raises jwt exceptions on failure."""
    raw = jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "iat", "sub", "jti", "type"]},
    )
    return TokenPayload(**raw)


# ============================================================
# REFRESH TOKEN BLACKLIST (Redis)
# ============================================================
_BLACKLIST_PREFIX = "jwt:blacklist:"


async def blacklist_jti(redis: Redis[str], jti: str, ttl_seconds: int) -> None:
    """Add a JTI to the blacklist with TTL = remaining token lifetime."""
    if ttl_seconds <= 0:
        return
    await redis.set(f"{_BLACKLIST_PREFIX}{jti}", "1", ex=ttl_seconds)
    logger.info("jti_blacklisted", jti=jti, ttl=ttl_seconds)


async def is_jti_blacklisted(redis: Redis[str] | None, jti: str) -> bool:
    """Check if a JTI is blacklisted."""
    if redis is None:
        return False
    val = await redis.get(f"{_BLACKLIST_PREFIX}{jti}")
    return val is not None


# ============================================================
# REFRESH ROTATION
# ============================================================
async def rotate_refresh_token(
    settings: Settings,
    redis: Redis[str],
    old_payload: TokenPayload,
    *,
    user_id: str,
    tenant_id: str,
    role: UserRole,
) -> tuple[str, str, int]:
    """Rotate refresh: blacklist old JTI, issue new access+refresh.

    Returns: (new_access, new_refresh, access_expires_in_seconds)

    Raises:
        ValueError if old refresh is already blacklisted (replay attack)
    """
    if await is_jti_blacklisted(redis, old_payload.jti):
        logger.warning(
            "refresh_replay_attempt",
            jti=old_payload.jti,
            user_id=user_id,
        )
        raise ValueError("Refresh token already used (replay detected)")

    ttl = old_payload.exp - int(_now().timestamp())
    await blacklist_jti(redis, old_payload.jti, ttl)

    new_access, access_exp = create_access_token(
        settings, user_id=user_id, tenant_id=tenant_id, role=role
    )
    new_refresh, _ = create_refresh_token(settings, user_id=user_id, tenant_id=tenant_id, role=role)
    logger.info("refresh_rotated", user_id=user_id)
    return new_access, new_refresh, access_exp
