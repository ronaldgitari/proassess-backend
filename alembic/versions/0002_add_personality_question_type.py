"""add personality question type

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLAlchemy binds Python enums by MEMBER NAME, so the DB enum labels are
    # uppercase (MCQ, WRITTEN). Add the matching 'PERSONALITY' label.
    # ALTER TYPE ... ADD VALUE must run outside an explicit transaction block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE questiontype ADD VALUE IF NOT EXISTS 'PERSONALITY'")


def downgrade() -> None:
    # Postgres does not support removing enum values; no-op.
    pass
