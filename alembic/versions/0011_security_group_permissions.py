"""configurable security-group permissions + per-user overrides

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-03 00:00:00.000000

Existing DBs (not alembic-stamped): apply the ALTERs directly.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS extra_permissions jsonb")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS denied_permissions jsonb")
    op.execute("ALTER TABLE security_groups ADD COLUMN IF NOT EXISTS slug varchar(100)")
    op.execute("ALTER TABLE security_groups ADD COLUMN IF NOT EXISTS permissions jsonb")
    op.execute("ALTER TABLE security_groups ADD COLUMN IF NOT EXISTS is_system boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE security_groups ADD COLUMN IF NOT EXISTS description text")


def downgrade() -> None:
    op.execute("ALTER TABLE security_groups DROP COLUMN IF EXISTS description")
    op.execute("ALTER TABLE security_groups DROP COLUMN IF EXISTS is_system")
    op.execute("ALTER TABLE security_groups DROP COLUMN IF EXISTS permissions")
    op.execute("ALTER TABLE security_groups DROP COLUMN IF EXISTS slug")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS denied_permissions")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS extra_permissions")
