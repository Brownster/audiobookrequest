"""Merge media_type and mam_flags heads

Revision ID: 20251128_01_merge_heads
Revises: 20251127_01_media_type, f2b04e4d50d1
Create Date: 2025-11-28
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20251128_01_merge_heads"
down_revision: Union[str, None] = ("20251127_01_media_type", "f2b04e4d50d1")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No-op merge migration.
    pass


def downgrade() -> None:
    # No-op merge migration.
    pass
