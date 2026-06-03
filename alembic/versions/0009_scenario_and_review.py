"""add scenario (case study) question type + human-review workflow fields

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-03 00:00:00.000000

Adds:
  - questiontype enum value 'SCENARIO'         (case-study / scenario assessments)
  - staffassessmentstatus enum value 'PENDING_REVIEW'  (LM verification gate)
  - staff_assessments.reviewed_by_id / reviewed_at     (who confirmed the score)
  - staff_answers.feedback_sources (jsonb)             (grounded + web citations
                                                        backing the AI feedback)

NOTE: enum ADD VALUE must run outside a transaction (autocommit_block); the
column adds are plain DDL. On existing DBs that aren't alembic-stamped, the
equivalent raw SQL can be applied directly (see docs/CASE_STUDY_FEATURE.md).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLAlchemy binds enums by MEMBER NAME → uppercase labels.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE questiontype ADD VALUE IF NOT EXISTS 'SCENARIO'")
        op.execute("ALTER TYPE staffassessmentstatus ADD VALUE IF NOT EXISTS 'PENDING_REVIEW'")

    op.execute("ALTER TABLE staff_assessments ADD COLUMN IF NOT EXISTS reviewed_by_id uuid")
    op.execute("ALTER TABLE staff_assessments ADD COLUMN IF NOT EXISTS reviewed_at timestamp")
    op.execute("ALTER TABLE staff_answers ADD COLUMN IF NOT EXISTS feedback_sources jsonb")


def downgrade() -> None:
    # Enum value removal is unsafe in Postgres; leave the labels in place.
    op.execute("ALTER TABLE staff_answers DROP COLUMN IF EXISTS feedback_sources")
    op.execute("ALTER TABLE staff_assessments DROP COLUMN IF EXISTS reviewed_at")
    op.execute("ALTER TABLE staff_assessments DROP COLUMN IF EXISTS reviewed_by_id")
