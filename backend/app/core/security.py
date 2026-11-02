"""Password hashing and access-token signing.

Two primitives live together here because they answer the same question — *is
this caller who they claim to be?* — and both fail in ways that are silent
unless the failure modes are enumerated explicitly.

Password storage
    Argon2id, via ``argon2-cffi``. The cost parameters come from settings so a
    deployment can raise them, and ``password_needs_rehash`` lets a login
    transparently upgrade a hash minted under weaker parameters. Verification
    reads the parameters *from the stored hash*, so lowering the configured cost
    never breaks existing passwords.

Token signing
    HS256 only, with the algorithm hard-coded into a one-element allow-list.
    The ``algorithms`` argument is the defence against algorithm-confusion
    attacks — a token whose header says ``alg: none`` is rejected because
    ``none`` is not in the list, regardless of what the caller sends.

Nothing in this module logs, returns, or embeds a plaintext password. Callers
that need to record a failure log the *reason*, never the credential.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError

from app.core.config import Settings
from app.db.enums import UserRole

#: The only signature algorithm this service issues or accepts.
JWT_ALGORITHM = "HS256"

#: Access tokens are distinguishable from the refresh tokens a later milestone
#: will add, so one can never be replayed in place of the other.
TOKEN_TYPE_ACCESS = "access"  # noqa: S105  # nosec B105  # a claim value, not a credential

#: Claims that must be present. ``exp`` keeps PyJWT verifying expiry by default;
#: the rest are asserted here so a token missing its tenant cannot be "repaired"
#: by a caller falling back to a default.
REQUIRED_CLAIMS = ("exp", "iat", "iss", "sub", "tid", "typ")

#: Anything shorter is rejected at account creation. Deliberately generous: a
#: minimum length beats a composition rule, which mostly produces "Passw0rd!".
MIN_PASSWORD_LENGTH = 12
#: Upper bound so a login request cannot submit a megabyte of text to a KDF.
MAX_PASSWORD_LENGTH = 256

#: Not a credential: it is stored nowhere and only ever hashed to burn the same
#: CPU as a real verification, so that "unknown user" and "wrong password" take
#: indistinguishable time. See ``spend_password_hash_time``.
DUMMY_PASSWORD = "no-such-user-timing-equaliser"  # noqa: S105  # nosec B105


class TokenError(Exception):
    """Base class for every reason a bearer token is rejected."""


class InvalidTokenError(TokenError):
    """The token is malformed, tampered with, or not ours."""


class TokenExpiredError(TokenError):
    """The token is well-formed but its ``exp`` has passed."""


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The parts of a verified token this service acts on.

    Everything here is still *unverified against the database*: a token can be
    perfectly signed and still belong to a revoked or deactivated user. The
    dependency layer re-reads the user row before trusting any of it.
    """

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: UserRole
    token_epoch: int


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A freshly minted token plus the metadata a client needs to cache it."""

    token: str
    expires_at: datetime
    expires_in: int


# --- password hashing -----------------------------------------------------
@lru_cache(maxsize=8)
def _password_hasher(time_cost: int, memory_cost: int, parallelism: int) -> PasswordHasher:
    """One hasher per distinct cost configuration; they are expensive to build."""
    return PasswordHasher(
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
    )


def password_hasher(settings: Settings) -> PasswordHasher:
    return _password_hasher(
        settings.argon2_time_cost,
        settings.argon2_memory_cost,
        settings.argon2_parallelism,
    )


def hash_password(plain: str, settings: Settings) -> str:
    """Return an Argon2id hash (salt and parameters embedded in the string)."""
    return password_hasher(settings).hash(plain)


def verify_password(plain: str, encoded: str, settings: Settings) -> bool:
    """Constant-time-ish check of ``plain`` against a stored hash.

    Every ``Argon2Error`` collapses to ``False``: a corrupted or truncated hash
    in the database must read as "wrong password", never as a 500 that tells an
    attacker which accounts have damaged rows.
    """
    try:
        return password_hasher(settings).verify(encoded, plain)
    except (Argon2Error, InvalidHashError):
        # ``InvalidHashError`` is a ``ValueError``, *not* an ``Argon2Error`` -- an
        # easy trap, and the one that matters most: a truncated or otherwise
        # unparseable hash is exactly the "damaged row" case, so catching only
        # ``Argon2Error`` turns it into a 500 that names the affected account.
        return False


def password_needs_rehash(encoded: str, settings: Settings) -> bool:
    """Whether a stored hash predates the currently configured cost."""
    try:
        return password_hasher(settings).check_needs_rehash(encoded)
    except (Argon2Error, InvalidHashError):
        # An unparseable hash cannot be upgraded; it can only be replaced, which
        # requires the user to log in successfully first.
        return False


def dummy_password_hash(settings: Settings) -> str:
    """A real Argon2id hash of a value nobody can authenticate with.

    Verification against this burns the same CPU as a genuine check, which is
    the entire point: see ``spend_password_hash_time``. Cached per cost
    configuration, so it costs one hash per process rather than one per login.
    """
    return _dummy_hash(
        settings.argon2_time_cost,
        settings.argon2_memory_cost,
        settings.argon2_parallelism,
    )


@lru_cache(maxsize=8)
def _dummy_hash(time_cost: int, memory_cost: int, parallelism: int) -> str:
    hasher = _password_hasher(time_cost, memory_cost, parallelism)
    return hasher.hash(DUMMY_PASSWORD)


def spend_password_hash_time(settings: Settings) -> None:
    """Do one throwaway verification, discarding the result.

    Login must answer "no such tenant" and "no such user" as slowly as "wrong
    password". Returning early on a missing row makes the response measurably
    faster and turns login into an oracle for which addresses are registered —
    a user-enumeration leak, and a PII leak by the specification's definition.
    """
    verify_password(DUMMY_PASSWORD, dummy_password_hash(settings), settings)


# --- tokens ---------------------------------------------------------------
def issue_access_token(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role: UserRole,
    token_epoch: int,
    settings: Settings,
    now: datetime | None = None,
) -> AccessToken:
    """Mint a signed access token for an already-authenticated user.

    ``tenant_id`` is the tenant of the *user row that was just verified*, never
    a value supplied by the caller. It is carried in the token so a request can
    be rejected without a lookup, but the dependency layer still treats the
    database row as authoritative.
    """
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.access_token_expire_minutes)

    claims = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role.value,
        "epoch": token_epoch,
        "typ": TOKEN_TYPE_ACCESS,
        "iss": settings.app_name,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        # Unique per mint: gives logs and a future denylist something to key on
        # without ever needing to store the token itself.
        "jti": uuid.uuid4().hex,
    }
    encoded = jwt.encode(claims, settings.secret_key, algorithm=JWT_ALGORITHM)
    return AccessToken(
        token=encoded,
        expires_at=expires_at,
        expires_in=settings.access_token_expire_minutes * 60,
    )


def decode_access_token(token: str, settings: Settings) -> TokenClaims:
    """Verify a token's signature, issuer and expiry, then extract its claims.

    Raises :class:`TokenExpiredError` or :class:`InvalidTokenError`; the caller
    turns both into a 401 and must not distinguish them to an unauthenticated
    client in more detail than "log in again".
    """
    try:
        payload: dict[str, object] = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[JWT_ALGORITHM],
            issuer=settings.app_name,
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidTokenError(str(exc)) from exc

    if payload.get("typ") != TOKEN_TYPE_ACCESS:
        raise InvalidTokenError("not an access token")

    try:
        user_id = uuid.UUID(str(payload["sub"]))
        tenant_id = uuid.UUID(str(payload["tid"]))
        role = UserRole(str(payload["role"]))
        token_epoch = int(str(payload["epoch"]))
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError("token claims are malformed") from exc

    return TokenClaims(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        token_epoch=token_epoch,
    )
