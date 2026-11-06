"""Refresh tokens, stored as digests and rotated — gap 6.

Revision ID: 0006
Revises: 0005
Create Date: 2025-11-08

Access tokens last `ACCESS_TOKEN_EXPIRE_MINUTES` (15) and, until now, could not be
renewed at all: every 15 minutes the user re-entered their password. `config.py`
has carried `refresh_token_expire_days` since F1 precisely because this table was
missing, so the setting was readable and inert.

**The raw token is never stored — only its SHA-256.** A refresh token is a bearer
credential with a week of life, so a leaked copy of this table must be worthless
on its own. `secrets.token_urlsafe(32)` is 256 bits, which is why a plain digest
with no salt is enough: there is no dictionary to run against it. The same
argument as `login_attempts.key_hash`, and it is the reason this table is safe to
back up.

**`used_at` makes rotation detectable.** A plain signed JWT would be valid until
its `exp` no matter what the server learned; a row can be marked spent. Redeeming
a token sets `used_at` and mints a successor in the same `family_id`, so a second
presentation of the same token is provably a replay — and the response is to
revoke the entire family rather than to refuse one request. The attacker's copy
and the victim's successor both die; the user logs in again.

**`token_epoch` is copied, not derived.** F3's epoch bump already invalidates
access tokens on the next request. Without a copy here, a refresh token minted
before the bump would keep minting *fresh* access tokens carrying the *old* epoch
— which would then be rejected, but only after being issued, and the session
would look like a bug rather than a revocation. Storing the epoch makes the
refresh path refuse at the same instant.

**`expires_at` is inherited on rotation, so the session has a hard ceiling.**
`REFRESH_TOKEN_EXPIRE_DAYS` bounds the session from login rather than sliding a
week ahead forever, which is the difference between "a week of sessions" and "a
session forever".

**`tenant_id` is repeated here (D2-02).** `login_attempts` deliberately omits it
because it is written before a tenant is known; this table is written after
authentication, when the user row is in hand, so the rule applies. It also makes
"revoke every session in this tenant" a single indexed statement.

Written by hand, like `0001`-`0005`, for the same reason: a migration is a frozen
historical record and must keep producing the same DDL after `app.db.models`
changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column("user_id", sa.CHAR(length=36), nullable=False),
        sa.Column("tenant_id", sa.CHAR(length=36), nullable=False),
        sa.Column("family_id", sa.CHAR(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_epoch", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        # Both cascades are load-bearing: deleting a user must not leave a live
        # session able to mint access tokens for the deleted identity.
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_refresh_tokens_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_refresh_tokens_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )

    # The redemption path (``WHERE token_hash = ? AND used_at IS NULL``) is
    # answered by ``uq_refresh_tokens_token_hash`` alone, so no composite index is
    # needed for the hot path. These three serve the cold ones: the expiry sweep,
    # a future "revoke this tenant's sessions", and the family revoke below.
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_tenant_id", "refresh_tokens", ["tenant_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])


def downgrade() -> None:
    # One statement, deliberately. MySQL refuses to ``DROP INDEX
    # ix_refresh_tokens_tenant_id`` while the foreign key on that column still
    # needs an index (errno 1553), so an index-by-index teardown fails before it
    # reaches ``drop_table``. Dropping the table removes its indexes, its unique
    # constraint and both foreign keys together, which is what ``0001``'s
    # downgrade does and for the same reason.
    op.drop_table("refresh_tokens")
