"""add HYBRID information source (KB doc + credible web case-study sources)

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-03 00:00:00.000000

Existing DBs (not alembic-stamped): apply directly —
    ALTER TYPE informationsource ADD VALUE IF NOT EXISTS 'HYBRID';
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE informationsource ADD VALUE IF NOT EXISTS 'HYBRID'")


def downgrade() -> None:
    pass
