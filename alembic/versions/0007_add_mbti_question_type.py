"""add mbti question type

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLAlchemy binds enums by MEMBER NAME → uppercase label.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE questiontype ADD VALUE IF NOT EXISTS 'MBTI'")


def downgrade() -> None:
    pass
