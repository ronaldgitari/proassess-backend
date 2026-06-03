"""add coding question type

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLAlchemy binds enums by MEMBER NAME → uppercase label.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE questiontype ADD VALUE IF NOT EXISTS 'CODING'")


def downgrade() -> None:
    pass
