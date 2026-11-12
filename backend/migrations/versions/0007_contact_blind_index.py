"""A keyed contact index, so an inbound reply can find the contract it is about.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-05

Every value column on ``extracted_fields`` is ciphertext under a random IV (gap 20),
so "the contract holding this phone number" was not a query the schema could answer.
That was deliberate and it was correct — until the product grew an inbound direction.
A customer replying STOP to a reminder has no tenant and no contract id to offer; the
number they sent from is the only handle there is.

This adds one nullable column holding HMAC-SHA256 of the normalized value under a
separate key (``CONTACT_BLIND_INDEX_KEY``), plus an index on it. See
``app/core/blind_index.py`` for why the digest is keyed, deterministic and
domain-separated, and for the two limits worth knowing: the key cannot be rotated in
place, and holding both key and table lets an attacker test a guessed number.

**Nullable on purpose, and the two ``NULL`` cases are different.** A date has no
inbound reply, so it gets no index — that is a permanent, correct ``NULL``. A contact
field written before this migration also gets ``NULL``, and that one is a gap: the
opt-out path cannot find it until the value is re-indexed. ``0007`` therefore ships
with a repair command (``python -m app.cli backfill-blind-index``) rather than leaving
a half-populated index to be discovered during an incident.

**No unique constraint.** Two contracts may legitimately hold the same number — the
same customer with two policies — and an opt-out must reach *both*. A unique
constraint would make the second insert fail and would encode "one contract per phone
number", which was never true.

**Why an index and not a prefix column.** The lookup is a plain equality against a
64-character hex digest, and the tenant cannot be part of the predicate because the
caller has no tenant. That leaves a single-column index as the only useful shape.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, so the DDL is legal on a table that already has rows. The backfill is
    # a separate, resumable operator step: rewriting every contact row inside the
    # migration would need the decryption key in the schema path, which is exactly
    # where it must never be.
    op.add_column(
        "extracted_fields",
        sa.Column("blind_index", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_extracted_fields_blind_index",
        "extracted_fields",
        ["blind_index"],
    )


def downgrade() -> None:
    # Index first: MySQL leaves the column's index dangling otherwise, and the foreign
    # key on ``tenant_id`` is untouched by this pair, so no ``errno 1553`` here.
    op.drop_index("ix_extracted_fields_blind_index", table_name="extracted_fields")
    op.drop_column("extracted_fields", "blind_index")
