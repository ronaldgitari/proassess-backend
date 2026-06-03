"""add start_date + force_password_change to users (HR user management)

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-02 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column(
        "users",
        sa.Column("force_password_change", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("users", "force_password_change")
    op.drop_column("users", "start_date")
