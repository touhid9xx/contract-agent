"""User schemas — request/response shapes.

Why password policy in Pydantic validator?
    - Fail at API boundary, not deep in business logic
    - Same policy enforced on register AND password change
    - Frontend Zod mirrors these rules via M3 pipeline (M6)

"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from contract_agent.models.user import UserRole

# ============================================================
# PASSWORD POLICY
# ============================================================
PASSWORD_MIN_LENGTH = 12
PASSWORD_RULES = {
    "uppercase": re.compile(r"[A-Z]"),
    "lowercase": re.compile(r"[a-z]"),
    "digit": re.compile(r"\d"),
    "symbol": re.compile(r"[^A-Za-z0-9]"),
}


def validate_password_strength(value: str) -> str:
    """Enforce 12+ chars + 1 upper + 1 lower + 1 digit + 1 symbol."""
    errors: list[str] = []
    if len(value) < PASSWORD_MIN_LENGTH:
        errors.append(f"at least {PASSWORD_MIN_LENGTH} characters")
    for label, pattern in PASSWORD_RULES.items():
        if not pattern.search(value):
            errors.append(f"one {label} character")

    if errors:
        raise ValueError("Password must contain: " + ", ".join(errors))
    return value


PasswordStr = Annotated[str, Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)]


# ============================================================
# REQUESTS
# ============================================================
class UserRegister(BaseModel):
    """POST /auth/register."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: PasswordStr
    full_name: str | None = Field(default=None, max_length=255)

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        return validate_password_strength(v)


class UserLogin(BaseModel):
    """POST /auth/login."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str  # No policy on login — only on register/change


# ============================================================
# RESPONSES
# ============================================================
class UserRead(BaseModel):
    """User representation — never includes password."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    email: EmailStr
    role: UserRole
    is_active: bool
    full_name: str | None
    created_at: datetime
    updated_at: datetime


class TokenPair(BaseModel):
    """Returned by /register, /login, /refresh."""

    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # seconds until access_token expires


class TokenPayload(BaseModel):
    """Decoded JWT claims."""

    model_config = ConfigDict(extra="forbid")

    sub: str  # user id
    tenant_id: str
    role: UserRole
    jti: str  # JWT ID — for blacklist
    type: Literal["access", "refresh"]
    exp: int  # expiry epoch
    iat: int  # issued at epoch
