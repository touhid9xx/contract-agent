"""Authentication routes.

Four endpoints, and the shape of each is a decision:

``POST /auth/login``
    Takes a tenant slug plus credentials and returns a short-lived access token.
    The tenant is part of the request because login happens *before* a principal
    exists — nothing else in the system is allowed to take a tenant from the
    client. The token that comes back carries the id of the tenant that owned
    the matched user row, so a mis-targeted slug cannot mint a token for the
    wrong organisation.

``GET /auth/me``
    Echoes the resolved principal. Useful to a UI, and the smallest possible
    demonstration that the dependency chain works end to end.

``POST /auth/users`` and ``GET /auth/users``
    Admin-only, and tenant-scoped by construction: the repository is built from
    ``principal.tenant_id``, so there is no request field that could aim the
    write at another organisation.

Every failure path is written to fail closed. There is no "if we cannot tell,
allow" branch anywhere in this module.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import app_settings, get_current_principal, get_request_session, require_roles
from app.core.principal import Principal
from app.core.security import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    hash_password,
    issue_access_token,
    password_needs_rehash,
    spend_password_hash_time,
    verify_password,
)
from app.db.enums import UserRole
from app.db.models import User
from app.db.repositories.users import (
    IdentityDirectory,
    UserRepository,
    is_plausible_email,
    normalize_email,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

#: Login answers with one message for every cause — unknown tenant, unknown
#: user, wrong password, deactivated account. Anything more specific turns the
#: endpoint into an oracle for which addresses and organisations exist.
_INVALID_CREDENTIALS = "Invalid tenant, email or password"


class LoginRequest(BaseModel):
    """Credentials plus the tenant the caller intends to log in to."""

    tenant: str = Field(min_length=1, max_length=64, description="Tenant slug")
    email: str = Field(min_length=3, max_length=320)
    #: Bounded so a login attempt cannot hand a megabyte of text to the KDF.
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class CreateUserRequest(BaseModel):
    """An admin creating a colleague inside their own tenant."""

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    role: UserRole = UserRole.VIEWER

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        if not is_plausible_email(value):
            raise ValueError("must be a valid email address")
        return normalize_email(value)


class UserSummary(BaseModel):
    """The user fields that are safe to return. Never the password hash."""

    id: uuid.UUID
    email: str
    role: UserRole
    tenant_id: uuid.UUID
    tenant_slug: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  # a scheme name, not a secret
    expires_in: int
    expires_at: datetime
    user: UserSummary


def _user_summary(user: User, *, tenant_slug: str) -> UserSummary:
    return UserSummary(
        id=user.id,
        email=user.email,
        role=user.role,
        tenant_id=user.tenant_id,
        tenant_slug=tenant_slug,
    )


def _principal_summary(principal: Principal) -> UserSummary:
    return UserSummary(
        id=principal.user_id,
        email=principal.email,
        role=principal.role,
        tenant_id=principal.tenant_id,
        tenant_slug=principal.tenant_slug,
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Exchange credentials for an access token",
)
def login(
    payload: LoginRequest,
    request: Request,
    session: Session = Depends(get_request_session),
) -> LoginResponse:
    """Authenticate a user of one tenant and mint an access token.

    Every rejection below returns the same 401 with the same message, after
    spending the same Argon2id work, so neither the body nor the response time
    reveals whether the tenant or the address exists.
    """
    settings = app_settings(request)
    directory = IdentityDirectory(session)

    tenant = directory.tenant_by_slug(payload.tenant)
    user = None
    if tenant is not None and tenant.is_active:
        user = UserRepository(session, tenant.id).by_email(payload.email)

    if user is None or tenant is None:
        spend_password_hash_time(settings)
        log.info("login_failed", reason="no_matching_account", tenant=payload.tenant)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_CREDENTIALS)

    # Always verify, even when the account is disabled: short-circuiting would
    # make "deactivated" measurably faster than "wrong password".
    password_ok = verify_password(payload.password, user.password_hash, settings)
    if not (password_ok and user.is_active):
        log.info("login_failed", reason="bad_password_or_inactive_account")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_CREDENTIALS)

    # A successful login is the only moment the plaintext exists, so it is the
    # only place an old hash can be upgraded to the current cost. Best-effort: a
    # failure here must not deny a valid user their session.
    if password_needs_rehash(user.password_hash, settings):
        try:
            user.password_hash = hash_password(payload.password, settings)
            session.commit()
            log.info("password_rehashed", user_id=str(user.id))
        except SQLAlchemyError:
            session.rollback()
            log.warning("password_rehash_skipped", user_id=str(user.id))

    access = issue_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
        token_epoch=user.token_epoch,
        settings=settings,
    )
    log.info("login_succeeded", user_id=str(user.id), tenant=tenant.slug)
    return LoginResponse(
        access_token=access.token,
        expires_in=access.expires_in,
        expires_at=access.expires_at,
        user=_user_summary(user, tenant_slug=tenant.slug),
    )


@router.get("/me", response_model=UserSummary, summary="The authenticated principal")
def read_current_user(principal: Principal = Depends(get_current_principal)) -> UserSummary:
    """Return who the caller is, as resolved from their token and the database."""
    return _principal_summary(principal)


@router.get(
    "/users",
    response_model=list[UserSummary],
    summary="List the users of the caller's tenant",
)
def list_users(
    principal: Principal = Depends(require_roles(UserRole.ADMIN)),
    session: Session = Depends(get_request_session),
) -> list[UserSummary]:
    """Admin-only. The repository is scoped to the caller's tenant, so this can
    only ever return colleagues — never another organisation's users."""
    users = UserRepository(session, principal.tenant_id).list_all()
    return [_user_summary(user, tenant_slug=principal.tenant_slug) for user in users]


@router.post(
    "/users",
    response_model=UserSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user in the caller's tenant",
)
def create_user(
    payload: CreateUserRequest,
    request: Request,
    principal: Principal = Depends(require_roles(UserRole.ADMIN)),
    session: Session = Depends(get_request_session),
) -> UserSummary:
    """Admin-only. ``tenant_id`` comes from the principal, never from the body."""
    settings = app_settings(request)
    repository = UserRepository(session, principal.tenant_id)

    if repository.by_email(payload.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists in this tenant",
        )

    user = repository.create(
        email=payload.email,
        password_hash=hash_password(payload.password, settings),
        role=payload.role,
    )
    try:
        session.commit()
    except SQLAlchemyError:
        # The pre-check above is advisory: two concurrent requests can both pass
        # it, and the unique constraint is what actually decides. Reporting the
        # constraint violation as 409 keeps the outcome stable instead of
        # surfacing a 500 the caller cannot act on.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists in this tenant",
        ) from None

    log.info("user_created", user_id=str(user.id), role=user.role.value)
    return _user_summary(user, tenant_slug=principal.tenant_slug)
