"""add series fields to bookrequest

Revision ID: 20251215_01_add_series_fields
Revises: 20251128_01_merge_heads
Create Date: 2025-12-15

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20251215_01_add_series_fields"
down_revision: Union[str, None] = "20251128_01_merge_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add series_name and series_position columns to bookrequest table
    with op.batch_alter_table("bookrequest", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("series_name", sa.String(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("series_position", sa.String(), nullable=True)
        )


def downgrade() -> None:
    # Remove series_name and series_position columns from bookrequest table
    with op.batch_alter_table("bookrequest", schema=None) as batch_op:
        batch_op.drop_column("series_position")
        batch_op.drop_column("series_name")
