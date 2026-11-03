"""Optimistic locking for agent decisions and notifications — gaps 19 and 21.

Revision ID: 0003
Revises: 0002
Create Date: 2025-06-24

`contracts` and `extracted_fields` have carried a `version` column since `0001`,
because both are edited by a person: two operators accepting the same field, or
re-recording the same consent, must not silently overwrite each other. The two
delivery tables were left out — `agent_decisions` was append-only, and
`notifications` had no writer at all until F10.

F10 made the omission load-bearing rather than theoretical:

* **`notifications` (gap 21).** `idempotency_key` stops a *second row* being
  created for a milestone. It says nothing about two workers mutating the *same*
  row: the second commit was a plain last-writer-wins `UPDATE`, so one worker could
  revert another's `SENT` transition or overwrite its `attempts` increment, and the
  database would report one send after the customer received two. This column turns
  the loser's write into a zero-row match, which SQLAlchemy raises as
  `StaleDataError` — the route maps that to a 409 instead of discarding the winner.
* **`agent_decisions` (gap 19).** Nothing mutates a decision today: a re-run
  *appends* a new row. The column is therefore inert until an override or
  flag-for-review route exists. It is added here, in the same revision, because a
  table that cannot be locked is the structural reason such a route could not be
  written — and one revision that makes both tables lockable is simpler than the
  second migration the alternative would need.

Written by hand, like `0001` and `0002`, for the same reason: a migration is a
frozen historical record and must keep producing the same DDL after
`app.db.models` changes.

**`server_default=1` is deliberate and kept.** Both tables already hold rows in any
deployed database, so the column cannot be added `NOT NULL` without a value for
them, and every existing row is at a state no writer has ever raced over — version
1 is the truthful value, not a placeholder. Keeping the default (rather than
dropping it after backfill) also means a raw `INSERT` that omits the column still
lands at 1, matching the model's Python-side `default=1`, so the two insert paths
cannot disagree.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``op.add_column`` with a server default rather than ``batch_alter_table``:
    # MySQL backfills the constant in place, and SQLite accepts a NOT NULL column
    # as long as a default is supplied, so neither path pays for a table copy.
    op.add_column(
        "agent_decisions",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "notifications",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )


def downgrade() -> None:
    # Mirror of the order they arrived in. Nothing references either column, so
    # dropping them loses only the lock state itself — and a downgrade is an
    # admission that the lock is no longer wanted, not a data migration.
    op.drop_column("notifications", "version")
    op.drop_column("agent_decisions", "version")
