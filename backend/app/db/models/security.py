"""Security tables that are not owned by a business aggregate.

Two tables live here and they make opposite choices, which is why they are worth
reading together:

``login_attempts`` (gap 9)
    Written *before* authentication, when no principal exists — so it has no
    ``tenant_id`` and cannot.

``refresh_tokens`` (gap 6)
    Written *after* authentication, when the user row is known — so it does
    carry ``tenant_id``, and D2-02 applies with no exception to argue.

The rule underneath both: a table repeats the tenant discriminator exactly when
something tenant-scoped could legitimately query it.

Failed-login attempt log — gap 9.

A row here is evidence that *somebody* tried to authenticate against a
``(tenant slug, email)`` pair and failed. It exists so the login route can ask
"how many times has this key failed recently?" and refuse with ``429`` once the
answer passes a threshold, instead of handing an unlimited number of Argon2id
verifications to anyone who asks.

Three things about this table are deliberate, and each is the opposite of the
obvious choice:

**It stores no address.** ``key_hash`` is a SHA-256 of the namespace, the
tenant slug and the normalised email — never the email itself. The throttle only
ever needs *equality* on the key, so storing the address would buy nothing and
turn a security control into a second, unrelated copy of the customer's PII
(the same argument that sealed ``extracted_fields.normalized_value`` in gap 20).
A side effect worth naming: this table cannot be used to enumerate which
addresses exist, because the digest is not reversible and the salt-free hash of
an address is not searchable without guessing the address.

**It has no ``tenant_id``.** Every other table in this schema repeats the tenant
discriminator so the repository layer can enforce scoping with one predicate
(``D2-02``). This one is the exception, and it has to be: a login attempt is
counted *before* a tenant is resolved — the slug is input, not authority — and
the common case of a wrong slug has no tenant row to point at. The table is
therefore a system-level pre-authentication table, exactly like
``IdentityDirectory``, and no tenant-scoped query path may be built over it.

**It is append-only and self-pruning.** Counting a window as rows rather than
keeping a mutable counter means concurrent failures cannot lose an increment to
a lost update: two racing inserts both land, and the count sees both. The cost
is growth, which is bounded by deleting a key's rows that have aged out of the
window at the moment the next failure is written.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, truncate_to_second, utc_now
from app.db.types import GUID


def _attempted_now() -> datetime:
    """The default ``attempted_at``: naive UTC, floored to the second.

    Flooring matches every other stored timestamp in this schema; the reason is
    spelled out on the column below.
    """
    return truncate_to_second(utc_now())


class LoginAttempt(Base):
    """One failed authentication attempt, identified by a hash and nothing else."""

    __tablename__ = "login_attempts"
    #: The count query filters on ``key_hash`` and then on ``attempted_at``, so
    #: the composite index serves it without a sort. The name is left to the
    #: naming convention in ``app.db.base`` so it is deterministic and stays
    #: inside MySQL's 64-character identifier limit.
    __table_args__ = (Index(None, "key_hash", "attempted_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    #: Hex SHA-256 of the throttle key. Fixed width, so ``String(64)`` is exact
    #: rather than generous, and an over-long value is a bug rather than data.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Naive UTC, like every other stored timestamp (``D2-01``). Written with
    #: second precision so MySQL cannot round it forward — the same trap
    #: ``truncate_to_second`` documents for outbox eligibility, which here would
    #: make a just-written failure appear to be from the future and therefore
    #: never count.
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, default=_attempted_now
    )


class RefreshToken(Base):
    """One issued refresh token, stored as a digest and rotatable — gap 6.

    **The token itself is never stored.** ``secrets.token_urlsafe`` gives 256
    bits of entropy, so a plain SHA-256 is enough to make the digest useless as a
    credential while keeping it a fixed 64 characters; there is nothing to
    brute-force. A stolen database dump therefore contains no usable session.

    **Rotation, not reuse.** Redeeming a token marks the row ``used_at`` and
    mints a successor in the same ``family_id``. Presenting a token that is
    already ``used_at`` means either a replay by an attacker or a client that
    raced itself; both are answered by revoking the whole family, which is why
    the column exists at all. A single long-lived token with no row behind it —
    a plain signed JWT — could not do this: it would stay valid until expiry no
    matter what the server learned in the meantime.

    **Rotation does not extend the session.** A successor inherits the
    ``expires_at`` of the token it replaced, so ``REFRESH_TOKEN_EXPIRE_DAYS``
    bounds the whole session rather than sliding forever. One rule, no "how long
    can this actually last" arithmetic to get wrong.

    ``token_epoch`` is copied from the user row at mint time and re-checked on
    every redemption, so the epoch bump that already revokes access tokens
    (F3) revokes refresh tokens in the same instant instead of after a week.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        # Explicitly named to match the ``uq`` convention in ``app.db.base``.
        UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        # Revoking a family is the one multi-row write here, and it filters on
        # this column alone.
        Index(None, "family_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    #: Cascade from both parents: a deleted user or organisation must not leave
    #: a live session behind. ``tenant_id`` is redundant with the user's own, and
    #: that is deliberate (D2-02) — it makes a future "revoke every session in
    #: this tenant" one indexed query instead of a join.
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Shared by a token and every token that replaced it. Reuse of any member
    #: revokes all of them, which logs out the attacker *and* the victim rather
    #: than leaving the attacker with a working session.
    family_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    #: Hex SHA-256 of the raw token. Fixed width, so ``String(64)`` is exact.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The user's ``token_epoch`` when this token was minted. A mismatch at
    #: redemption time means the session was revoked; it is a refusal, not a
    #: refreshable condition.
    token_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    #: Non-null once redeemed. Doubles as the rotation marker and the reuse
    #: evidence, so no separate boolean is needed.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    #: Non-null once the family was killed (logout, reuse detection, or an
    #: operator revoke). Checked before ``used_at`` so a revoked token reports
    #: the honest reason in logs without the caller learning the difference.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
