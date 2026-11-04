"""Operator override and review flag on agent decisions — gap 19.

Revision ID: 0004
Revises: 0003
Create Date: 2025-11-04

`0003` gave `agent_decisions` an optimistic-lock column while nothing mutated a
decision: a re-run appended a new row rather than editing one. That made the
table lockable but left the two corrections the Phase-0 shapes assume — override
a channel, flag a decision for review — unimplementable. This revision adds the
columns those routes need.

Three columns, and each is load-bearing:

* `original_channel` keeps the channel the policy chose the first time a person
  disagrees. Without it an override is a destructive edit: the row would say
  "sms" with no trace that the deterministic policy ever said "email", and the
  only surviving evidence would be prose in the audit log.
* `overridden_by` names the operator. It is a plain `CHAR(36)` GUID rather than a
  foreign key, matching `extracted_fields.reviewed_by` — the audit log is the
  durable record of who did what, and deleting a user must not rewrite history
  in the decisions table.
* `rerun_count` records how much history preceded the row, so "re-run for the
  third time" is readable from the decision itself instead of a per-request
  aggregate.

**`original_channel` is nullable and is NOT backfilled.** NULL means "no person
has overridden this decision", which is true of every existing row. Copying
`channel` into it would assert that a human confirmed a choice a script made.

Written by hand, like `0001`-`0003`, for the same reason: a migration is a frozen
historical record and must keep producing the same DDL after `app.db.models`
changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The stored values of ``app.db.enums.Channel``, spelled out because a migration
#: may not import application code: `0001`-`0003` are frozen for exactly this
#: reason, and an enum member renamed in Python must not silently change DDL that
#: has already run in a deployed database.
CHANNEL_VALUES: tuple[str, ...] = ("email", "sms", "voice")


def upgrade() -> None:
    # ``op.add_column`` rather than ``batch_alter_table``, following `0002`: a
    # nullable column and a NOT NULL column with a server default are both
    # supported by SQLite directly, so the MySQL path avoids a full table copy.
    op.add_column(
        "agent_decisions",
        sa.Column(
            "original_channel",
            # ``native_enum=True`` to match ``enum_column``: on MySQL this is a real
            # ENUM, on SQLite it is VARCHAR(10) plus a CHECK. Declaring it as plain
            # VARCHAR here would migrate cleanly and then make ``alembic check``
            # report a permanent difference against the model.
            sa.Enum(*CHANNEL_VALUES, name="channel", native_enum=True, length=10),
            nullable=True,
        ),
    )
    op.add_column(
        "agent_decisions",
        sa.Column("overridden_by", sa.CHAR(length=36), nullable=True),
    )
    # A server default as well as the model default: rows written by any path that
    # does not go through the ORM (a fixture load, a future bulk import) must still
    # read as "never re-run" rather than NULL.
    op.add_column(
        "agent_decisions",
        sa.Column("rerun_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    # Reverse order, mirroring `0002`: columns disappear in the reverse of the
    # order they arrived.
    op.drop_column("agent_decisions", "rerun_count")
    op.drop_column("agent_decisions", "overridden_by")
    op.drop_column("agent_decisions", "original_channel")
