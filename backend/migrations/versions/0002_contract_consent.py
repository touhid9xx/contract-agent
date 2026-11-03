"""Contract consent and opt-out — F9.

Revision ID: 0002
Revises: 0001
Create Date: 2025-06-20

Adds the columns the customer-contact policy needs before an agent decision can
name a channel. `0001` created the contract row without them, and the Phase-0
fixtures never modelled consent at all, so until this revision the rule "no
channel switch that bypasses consent, opt-out or quiet hours" had nothing in the
schema to enforce it against.

Written by hand, like `0001`, for the same reason: a migration is a frozen
historical record and must keep producing the same DDL after `app.db.models`
changes.

**Defaults are `false` for all three consent flags on purpose.** Backfilling
`true` would grant consent nobody gave, which is exactly the failure the column
exists to prevent. The consequence is that every contract that existed before
this revision becomes un-contactable until an operator records consent through
`PUT /contracts/{id}/consent` — fail closed, and a one-off data task rather than
a silent policy change.

`do_not_contact` is a separate column from the three consent flags because it is
a veto, not a channel choice: it must be able to overrule a mistaken grant.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``op.add_column`` rather than ``batch_alter_table``: adding a nullable column
    # or a NOT NULL column with a server default is supported by SQLite directly,
    # so the MySQL path does not have to pay for a full table copy either.
    op.add_column(
        "contracts",
        sa.Column("do_not_contact", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "contracts",
        sa.Column("consent_email", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "contracts",
        sa.Column("consent_sms", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "contracts",
        sa.Column("consent_voice", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "contracts",
        sa.Column("consent_updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    # Reverse order, so the columns disappear in the mirror of the order they
    # arrived and nothing in between depends on one that is already gone.
    op.drop_column("contracts", "consent_updated_at")
    op.drop_column("contracts", "consent_voice")
    op.drop_column("contracts", "consent_sms")
    op.drop_column("contracts", "consent_email")
    op.drop_column("contracts", "do_not_contact")
