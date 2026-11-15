"""A stamp for "this document has been through extraction", so the queue can end.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-07

``extract_contract`` has been able to turn a stored document into rows since F7, but
nothing ever called it on a timer: ``python -m app.cli extract`` handles one contract
at a time, run by a person. The extraction worker needs a predicate it can *finish*,
and that is what this column is for.

The obvious predicate — "has a stored document and no ``extracted_fields`` rows" —
cannot finish. A scanned page with no text layer, or a contract that simply never
states an e-mail address, yields no row; such a contract would match on every tick for
ever and warn on every tick for ever. That is exactly the starvation the planner's id
cursor and the scanner's status transitions exist to avoid, except that here it would
be the normal case rather than an edge one.

So the queue is ``storage_path IS NOT NULL AND extraction_attempted_at IS NULL``, and
the worker stamps this column in the same transaction as the rows it writes. "Attempted
and found nothing" is a finished state; "never attempted" is the only thing left to do.

Three deliberate choices:

**Nullable, and ``NULL`` means "never attempted".** Every contract that already exists
is legacy data with ``NULL`` here, which is the correct reading of it: none of them has
been through the worker. So this migration *is* the backfill — no data step, and the
first tick examines every stored document the deployment holds.

**Stamped on success only.** The worker stamps the ``extracted``, ``empty`` and
``reviewed`` outcomes: the three where it managed to read the document's text and run
extraction over it. It does not stamp a document it could not load, decrypt or parse,
because that is a bytes, key-ring or build problem, and the retry is a retry of the
*reading* — the part a redeploy or a restored key actually repairs. Such a row keeps
its place in the queue and cannot starve anything, because the claim is ``id > cursor``
over pages that wrap.

**Indexed, though the column is nearly all-``NULL`` at first.** The claim is an ``IS
NULL`` equality against a single column, which is the one shape a plain single-column
index answers well; without it every tick scans the contract table.

See also: no unique constraint and no ``NOT NULL``. This is bookkeeping, not a state
machine, and a wrong stamp is repaired by an ordinary ``UPDATE``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, so the DDL is legal on a table that already has rows — and so that
    # every existing contract is "never attempted", which is what the worker wants.
    op.add_column(
        "contracts",
        sa.Column("extraction_attempted_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_contracts_extraction_attempted_at",
        "contracts",
        ["extraction_attempted_at"],
    )


def downgrade() -> None:
    # Index first: MySQL leaves the column's index dangling otherwise, and no foreign
    # key references this column, so the drop cannot hit ``errno 1553``.
    op.drop_index("ix_contracts_extraction_attempted_at", table_name="contracts")
    op.drop_column("contracts", "extraction_attempted_at")
