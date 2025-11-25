"""add MAM availability flags to BookRequest

Revision ID: f2b04e4d50d1
Revises: 1b4b49edaf79
Create Date: 2025-11-25 22:25:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b04e4d50d1"
down_revision: Union[str, None] = "1b4b49edaf79"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(inspector: sa.engine.reflection.Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    with op.batch_alter_table("bookrequest") as batch_op:
        if not _has_column(inspector, "bookrequest", "mam_unavailable"):
            batch_op.add_column(
                sa.Column(
                    "mam_unavailable",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )
        if not _has_column(inspector, "bookrequest", "mam_last_check"):
            batch_op.add_column(
                sa.Column(
                    "mam_last_check",
                    sa.DateTime(),
                    nullable=True,
                )
            )

    # Clean up server_default so future inserts use application defaults
    with op.batch_alter_table("bookrequest") as batch_op:
        batch_op.alter_column("mam_unavailable", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("bookrequest") as batch_op:
        if _has_column(sa.inspect(op.get_bind()), "bookrequest", "mam_last_check"):
            batch_op.drop_column("mam_last_check")
        if _has_column(sa.inspect(op.get_bind()), "bookrequest", "mam_unavailable"):
            batch_op.drop_column("mam_unavailable")
