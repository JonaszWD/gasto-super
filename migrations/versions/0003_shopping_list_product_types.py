"""shopping list product types

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30 17:22:52.499155

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('shoppinglistitem', sa.Column('product_type', sqlmodel.sql.sqltypes.AutoString(length=60), nullable=True))
    op.add_column('shoppinglistitem', sa.Column('amount', sa.Numeric(precision=10, scale=3, asdecimal=False), nullable=True))
    op.add_column('shoppinglistitem', sa.Column('amount_unit', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=True))
    op.alter_column('shoppinglistitem', 'canonical_product_id',
               existing_type=sa.INTEGER(),
               nullable=True)
    op.create_unique_constraint('shoppinglistitem_product_type_key', 'shoppinglistitem', ['product_type'])
    # Not detected by autogenerate: a list item is a product or a type, never both or neither.
    op.create_check_constraint(
        'ck_shoppinglistitem_product_or_type', 'shoppinglistitem',
        '(canonical_product_id IS NULL) <> (product_type IS NULL)',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_shoppinglistitem_product_or_type', 'shoppinglistitem', type_='check')
    op.drop_constraint('shoppinglistitem_product_type_key', 'shoppinglistitem', type_='unique')
    op.execute("DELETE FROM shoppinglistitem WHERE canonical_product_id IS NULL")  # type items can't be kept
    op.alter_column('shoppinglistitem', 'canonical_product_id',
               existing_type=sa.INTEGER(),
               nullable=False)
    op.drop_column('shoppinglistitem', 'amount_unit')
    op.drop_column('shoppinglistitem', 'amount')
    op.drop_column('shoppinglistitem', 'product_type')
