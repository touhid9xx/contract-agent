"""Failed-login attempt log — gap 9.

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

from sqlalchemy import DateTime, Index, String
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
