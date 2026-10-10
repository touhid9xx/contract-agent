"""initial_empty_baseline

Revision ID: 2f92edddb7fc
Revises: 4aef4ded1b53
Create Date: 2026-10-10 13:31:26.775598

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2f92edddb7fc"
down_revision: Union[str, Sequence[str], None] = "4aef4ded1b53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
