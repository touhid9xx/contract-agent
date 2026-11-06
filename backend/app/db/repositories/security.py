"""Persistence for the two authentication tables: failures and refresh tokens.

``LoginAttemptRepository`` (gap 9)
    The second deliberate exception to tenant scoping in the data layer, and it
    is worth stating why it is not the same kind of exception
    ``IdentityDirectory`` makes. That class is read-only precisely because an
    unscoped *write* would be a cross-tenant write primitive. This repository
    does write — but only to ``login_attempts``, a table no tenant owns, one row
    at a time, keyed by an opaque hash that carries no tenant reference and no
    address. There is no statement here that can read or modify a tenant-owned
    row, so the "unscoped" property cannot be aimed at another organisation's
    data.

``RefreshTokenRepository`` (gap 6)
    Ordinary in that it writes tenant-owned rows, but reached only by an
    unguessable digest rather than by a principal. It takes no ``tenant_id`` at
    construction because the *lookup* is a capability check, not a query: the row
    found by the hash supplies the user and tenant, which the route then verifies
    against the live rows. No caller may pass a tenant in and expect it to scope
    anything.

Every method takes the caller's session; none commits. The routes decide which
transaction a write belongs to, because the writes here have opposite
requirements: a recorded login failure must survive the exception that ends the
request, while clearing a counter is pure hygiene and must never be able to deny
a session that has already been earned.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.db.base import truncate_to_second
from app.db.models import LoginAttempt, RefreshToken


class LoginAttemptRepository:
    """Read and write the failure log for a single throttle key."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def window(self, key_hash: str, *, since: datetime) -> tuple[int, datetime | None]:
        """Failures for this key since ``since``, and when the oldest one landed.

        One query returns both numbers because the caller needs both: the count
        decides *whether* to refuse, and the oldest timestamp decides *for how
        long*, since the block lifts as attempts age out of the window rather
        than on a separate timer. Splitting it into two round trips would also
        let the two values disagree under concurrent writes.

        ``earliest`` is ``None`` exactly when ``count`` is zero.
        """
        stmt = select(
            func.count(LoginAttempt.id),
            func.min(LoginAttempt.attempted_at),
        ).where(
            LoginAttempt.key_hash == key_hash,
            LoginAttempt.attempted_at >= since,
        )
        row = self._session.execute(stmt).one()
        return int(row[0]), cast("datetime | None", row[1])

    def record(self, key_hash: str, *, now: datetime, prune_before: datetime) -> None:
        """Append one failure, dropping this key's rows that can no longer count.

        The delete is what keeps the table bounded without a sweeper process: an
        installation that never fails a login never writes here at all, and a key
        that is being attacked holds only the attempts inside one window. It is
        scoped by ``key_hash``, so pruning one key's history cannot shorten
        another's block.

        ``now`` is floored to the second here rather than at the call site. The
        model's default does the same, but the login route passes an explicit
        ``utc_now()`` (which carries microseconds), so relying on the default
        would leave the column's precision guarantee true only for callers that
        happened not to supply a time. MySQL rounds a microseconds-bearing value
        *forward*, storing a failure a fraction of a second in the future, and
        the window arithmetic then measures "how long until the oldest failure
        ages out" from an instant that has not arrived yet.
        """
        self._session.execute(
            delete(LoginAttempt).where(
                LoginAttempt.key_hash == key_hash,
                LoginAttempt.attempted_at < prune_before,
            )
        )
        self._session.add(LoginAttempt(key_hash=key_hash, attempted_at=truncate_to_second(now)))

    def clear(self, key_hash: str) -> None:
        """Forget this key's failures.

        Called after a successful login. Without it a user who mistypes their
        password ``login_max_attempts - 1`` times and then gets it right would
        still be carrying those failures, and one more slip in the same window
        would lock them out despite the correct password having just worked.
        """
        self._session.execute(delete(LoginAttempt).where(LoginAttempt.key_hash == key_hash))


class RefreshTokenRepository:
    """Issue, find, spend and revoke refresh tokens (gap 6).

    Deliberately not tenant-scoped at construction: the row is located by the
    digest of a token the caller presented, and *that row* names the user. A
    ``tenant_id`` passed in here would have to be trusted before the token was
    verified, which is exactly backwards.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def create(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        family_id: uuid.UUID,
        token_hash: str,
        token_epoch: int,
        issued_at: datetime,
        expires_at: datetime,
    ) -> RefreshToken:
        """Add one token row, dropping this user's rows that can no longer work.

        The prune keeps the table bounded without a sweeper, the same trade
        ``LoginAttemptRepository.record`` makes: an installation whose sessions
        all end by expiry never needs one, and the delete is scoped to ``user_id``
        so housekeeping for one account cannot disturb another's session.

        Only rows that are *expired* are removed. A used-but-unexpired row is
        deliberately kept — it is the evidence that makes replay detectable, so
        deleting it would turn a detected reuse into an unexplained 401.
        """
        self._session.execute(
            delete(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.expires_at <= truncate_to_second(issued_at),
            )
        )
        token = RefreshToken(
            user_id=user_id,
            tenant_id=tenant_id,
            family_id=family_id,
            token_hash=token_hash,
            token_epoch=token_epoch,
            # Floored for the reason ``truncate_to_second`` documents: MySQL
            # rounds a microseconds-bearing value *forward*, which would make a
            # just-issued token's ``issued_at`` slightly in the future and
            # ``expires_at`` a rounding artefact rather than a deadline.
            issued_at=truncate_to_second(issued_at),
            expires_at=truncate_to_second(expires_at),
        )
        self._session.add(token)
        return token

    def by_hash(self, token_hash: str) -> RefreshToken | None:
        """The row for this digest, or ``None``. No row means "not ours"."""
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        return self._session.execute(stmt).scalar_one_or_none()

    def claim_for_rotation(self, token_hash: str, *, now: datetime) -> bool:
        """Mark the token spent, returning whether *this* call was the one.

        A single conditional ``UPDATE`` rather than read-then-write. Two requests
        racing with the same token would both pass a Python-side ``if
        token.used_at is None`` check and both mint a successor, which is two
        live sessions from one token — the exact outcome rotation exists to
        prevent. The database decides instead: the second update matches no row
        and reports ``False``.

        ``revoked_at IS NULL`` is part of the predicate, not a courtesy: a token
        whose family was already killed must not be spendable even if a stale
        caller still holds the raw value.
        """
        # A bulk UPDATE bypasses the ORM's unit of work, and this session is built
        # with autoflush=False, so a row added earlier in the same transaction is
        # still pending and the predicate below cannot see it. Flushing first keeps
        # the method correct regardless of how the caller ordered its writes.
        self._session.flush()
        stmt = (
            update(RefreshToken)
            .where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.used_at.is_(None),
                RefreshToken.revoked_at.is_(None),
            )
            .values(used_at=truncate_to_second(now))
        )
        result = cast("CursorResult[Any]", self._session.execute(stmt))
        return result.rowcount == 1

    def revoke_family(self, family_id: uuid.UUID, *, now: datetime) -> int:
        """Revoke every token descended from one login. Returns rows affected.

        Called on logout, and on reuse detection — where the point is precisely
        that it kills the successor too. Refusing only the replayed token would
        leave the attacker holding whichever copy they had already rotated in.
        """
        # See claim_for_rotation: a pending insert is invisible to a bulk UPDATE.
        self._session.flush()
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=truncate_to_second(now))
        )
        result = cast("CursorResult[Any]", self._session.execute(stmt))
        return result.rowcount

    def revoke_all_for_user(self, user_id: uuid.UUID, *, now: datetime) -> int:
        """Revoke every live session of one account. Returns rows affected.

        This is the repository half of "sign out everywhere" (gap 8) and of every
        act that must invalidate a session immediately: a password change, a
        password reset, a deactivation, and the caller's own ``/auth/logout-all``.

        Keyed on ``user_id`` alone, with no tenant predicate, because
        ``RefreshTokenRepository`` is not tenant-scoped (see the class docstring)
        and does not need to be: a user id identifies exactly one row in exactly
        one tenant, so there is no second tenant's session this could reach.
        ``tenant_id`` is repeated on the table for the *other* query the model
        anticipates — revoking a whole organisation — not for this one.

        Already-revoked rows are filtered out rather than re-stamped, so the
        return value counts the sessions this call actually ended. That is what a
        route can honestly report, and re-stamping would overwrite the original
        revocation time that reuse detection may need to reason about.
        """
        # See claim_for_rotation: a pending insert is invisible to a bulk UPDATE.
        self._session.flush()
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=truncate_to_second(now))
        )
        result = cast("CursorResult[Any]", self._session.execute(stmt))
        return result.rowcount
