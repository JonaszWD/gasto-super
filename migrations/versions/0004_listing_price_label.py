"""listing price label

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-04 15:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('listing', sa.Column('price_label', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('listing', 'price_label')
