"""Persistence for the failed-login log used by the login throttle (gap 9).

This is the second deliberate exception to tenant scoping in the data layer, and
it is worth stating why it is not the same kind of exception
``IdentityDirectory`` makes. That class is read-only precisely because an
unscoped *write* would be a cross-tenant write primitive. This repository does
write — but only to ``login_attempts``, a table no tenant owns, one row at a
time, keyed by an opaque hash that carries no tenant reference and no address.
There is no statement here that can read or modify a tenant-owned row, so the
"unscoped" property cannot be aimed at another organisation's data.

Every method takes the caller's session; none commits. The login route decides
which transaction a write belongs to, because the two writes here have opposite
requirements: a recorded failure must survive the exception that ends the
request, while clearing a counter is pure hygiene and must never be able to deny
a session that has already been earned.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.base import truncate_to_second
from app.db.models import LoginAttempt


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
