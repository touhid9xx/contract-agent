"""Request-scoped dependencies: a database session and the authenticated principal.

This module is the seam the specification cares most about. Everything below it
that touches tenant-owned data demands a ``tenant_id``; this is the only place
that produces one, and it produces it from a verified token plus a database read
— never from anything the client sent as data.

Verification order, cheapest first:

1. Is there a bearer token at all?
2. Is its signature valid, is it unexpired, and is it ours?
3. Does the user row still exist, and is it in the tenant the token claims?
4. Is the user — and the tenant — still active?
5. Does the token's epoch match the user's current ``token_epoch``?

Steps 3-5 are why a token is not a capability: revoking a user, deactivating an
organisation, or bumping ``token_epoch`` takes effect on the very next request,
without a denylist and without waiting for expiry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import cast

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.crypto import FieldCipher, MissingEncryptionKeyError, build_cipher
from app.core.principal import Principal
from app.core.security import InvalidTokenError, TokenExpiredError, decode_access_token
from app.db.enums import UserRole
from app.db.repositories.users import IdentityDirectory

#: ``auto_error=False`` so a missing or non-Bearer header arrives as ``None``
#: here and becomes a 401 with a ``WWW-Authenticate`` challenge. The default
#: (``auto_error=True``) answers 403, which tells a client "you are authenticated
#: but not allowed" when in fact it is not authenticated at all.
bearer_scheme = HTTPBearer(auto_error=False, description="Access token from POST /auth/login")

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}

#: Every rejection reason collapses to this one message for the client. The
#: distinction between "expired" and "invalid" is kept only in the branch that
#: produced it, so an attacker cannot use the wording to probe token validity.
_UNAUTHENTICATED = "Not authenticated"


def app_settings(request: Request) -> Settings:
    """The settings the running app was built with (not a fresh singleton)."""
    return cast(Settings, request.app.state.settings)


def cipher_or_503(settings: Settings) -> FieldCipher:
    """The configured field cipher, or a 503 explaining what is missing.

    ``ENCRYPTION_KEY`` is optional at start-up (an installation that only ingests
    does not need it), so a missing key is a deployment state rather than a bug.
    Failing closed here means an unconfigured installation cannot decrypt — and,
    on the write side, cannot quietly store a value in plaintext.

    It lives here rather than in a router because two do the same thing: ``review``
    reveals and stores field values, and ``notifications`` must decrypt one to
    address a message. Two copies of this mapping is how one route starts
    answering 500 for a missing key while the other answers 503.
    """
    try:
        return build_cipher(settings)
    except MissingEncryptionKeyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Field values cannot be read: ENCRYPTION_KEY is not configured",
        ) from None


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_UNAUTHENTICATED,
        headers=_UNAUTHORIZED_HEADERS,
    )


def app_session_factory(request: Request) -> sessionmaker[Session]:
    """The session factory the running app was built with.

    Separate from :func:`get_request_session` for callers that must open their
    own session rather than share the request's — the readiness probe runs its
    blocking check in a worker thread, where a session bound to the request
    thread would be the wrong object to hand out.
    """
    return cast("sessionmaker[Session]", request.app.state.session_factory)


def get_request_session(request: Request) -> Iterator[Session]:
    """Yield a session bound to this request, closing it afterwards.

    Deliberately does **not** commit: a read endpoint must not write, and a write
    endpoint commits once, explicitly, when it has finished its work. An
    implicit commit here would turn any accidental write into a durable one.
    """
    session = app_session_factory(request)()
    try:
        yield session
    finally:
        session.close()


def get_current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_request_session),
) -> Principal:
    """Resolve the caller from their bearer token, or raise 401."""
    if credentials is None or not credentials.credentials:
        raise _unauthorized()

    settings = app_settings(request)
    try:
        claims = decode_access_token(credentials.credentials, settings)
    except TokenExpiredError:
        raise _unauthorized() from None
    except InvalidTokenError:
        raise _unauthorized() from None

    user = IdentityDirectory(session).user_by_id(claims.user_id)
    if user is None:
        # The signing key is fine but the account is gone: a deleted user's
        # unexpired token must stop working immediately.
        raise _unauthorized()

    # The token carries a tenant so a request can be scoped without a lookup, but
    # the row wins. A mismatch means the token was minted for a different
    # tenant than the user now belongs to -- reject rather than pick a side.
    if user.tenant_id != claims.tenant_id:
        raise _unauthorized()

    if not user.is_active or not user.tenant.is_active:
        raise _unauthorized()

    if user.token_epoch != claims.token_epoch:
        # Logout-everywhere, password change, or an administrative revoke.
        raise _unauthorized()

    return Principal(
        user_id=user.id,
        tenant_id=user.tenant_id,
        tenant_slug=user.tenant.slug,
        email=user.email,
        role=user.role,
        token_epoch=user.token_epoch,
    )


def require_roles(*roles: UserRole) -> Callable[..., Principal]:
    """Build a dependency that admits only the given roles.

    Returns the principal, so a route can depend on ``require_roles(...)``
    alone and still have everything ``get_current_principal`` provides. A
    principal who is authenticated but lacks the role gets 403, which is the
    correct distinction from the 401 above.
    """
    allowed = frozenset(roles)

    def dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This operation requires a higher role",
            )
        return principal

    return dependency
