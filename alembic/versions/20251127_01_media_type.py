"""Add media_type to bookrequest and downloadjob

Revision ID: 20251127_01_media_type
Revises: 03bea7e891dd
Create Date: 2025-11-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "20251127_01_media_type"
down_revision: Union[str, None] = "03bea7e891dd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("bookrequest", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "media_type",
                sa.Enum("audiobook", "ebook", name="mediatype"),
                nullable=False,
                server_default="audiobook",
            )
        )

    with op.batch_alter_table("downloadjob", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "media_type",
                sa.Enum("audiobook", "ebook", name="mediatype"),
                nullable=False,
                server_default="audiobook",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("downloadjob", schema=None) as batch_op:
        batch_op.drop_column("media_type")

    with op.batch_alter_table("bookrequest", schema=None) as batch_op:
        batch_op.drop_column("media_type")

    # Drop the enum if it exists (Postgres)
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS mediatype")
