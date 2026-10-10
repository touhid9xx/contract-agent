"""initial_empty_baseline

Revision ID: 4aef4ded1b53
Revises: 6ad6f6ab3bc9
Create Date: 2026-10-10 12:25:33.775204

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "4aef4ded1b53"
down_revision: Union[str, Sequence[str], None] = "6ad6f6ab3bc9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
