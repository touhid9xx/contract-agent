"""Failed-login attempts, for login admission control — gap 9.

Revision ID: 0005
Revises: 0004
Create Date: 2025-11-07

`POST /auth/login` verifies passwords with Argon2id and, until now, accepted an
unlimited number of wrong guesses. That is a guessing oracle and a way to spend
the server's CPU, and no configuration in the app limited it. This revision adds
the table that makes a limit enforceable across process restarts and replicas.

**One row per failed attempt, not a counter column.** A counter would have to be
incremented under a lock and would need a separate "when did the window start"
column that is easy to reset wrongly; a row carries its own timestamp, so the
window is a `WHERE attempted_at >= ?` and the block lifts by itself when the
oldest row ages out. There is no lockout flag that a bug — or a process dying
mid-write — can leave stuck on.

**`key_hash` is a SHA-256 digest, not an email and not a tenant id.** Nothing
here needs to know *who* tried to log in, only whether two attempts are the same
attempt; a digest answers that and keeps an address out of a new table that
would otherwise become the app's first unencrypted store of one. It also lets the
row exist before a tenant is resolved, which is required: a login against an
unknown slug must be counted exactly like one against a real tenant, or the
throttle's own speed becomes a tenant-enumeration oracle.

**No tenant column and no foreign key, deliberately.** This is the second
pre-authentication table in the schema (after nothing, since `users` is
tenant-rowed) and the same exception as `IdentityDirectory`: it is written on a
path where the principal is unknown, so it is not tenant-scoped. It holds no
tenant-owned data, so it cannot become a cross-tenant leak.

Rows are pruned for the key being written at insert time, so the table grows
with distinct `(tenant slug, email)` pairs that have failed recently rather than
with total attempts. A flood cannot make it grow without bound while the window
is short, because each attempt also costs an Argon2id verification.

Written by hand, like `0001`-`0004`, for the same reason: a migration is a frozen
historical record and must keep producing the same DDL after `app.db.models`
changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.CHAR(length=36), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("attempted_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_login_attempts"),
    )

    # Serves both the window count and the prune, which filter on the same two
    # columns; a single-column index would leave the range scan on the table.
    op.create_index(
        "ix_login_attempts_key_hash_attempted_at",
        "login_attempts",
        ["key_hash", "attempted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_login_attempts_key_hash_attempted_at", table_name="login_attempts")
    op.drop_table("login_attempts")
