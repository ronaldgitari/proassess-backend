"""pipeline_spans table + run capsule metadata columns

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-02 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Capsule metadata on the run
    op.add_column("pipeline_runs", sa.Column("origin_ip", sa.String(64), nullable=True))
    op.add_column("pipeline_runs", sa.Column("server_ip", sa.String(64), nullable=True))
    op.add_column("pipeline_runs", sa.Column("system_id", sa.String(128), nullable=True))

    # Per-service spans
    op.create_table(
        "pipeline_spans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("pipeline_runs.id"), nullable=False),
        sa.Column("service", sa.String(40), nullable=False),
        sa.Column("operation", sa.String(120), nullable=False),
        sa.Column("phase", sa.String(80), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_pipeline_spans_run_id", "pipeline_spans", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_spans_run_id", table_name="pipeline_spans")
    op.drop_table("pipeline_spans")
    op.drop_column("pipeline_runs", "system_id")
    op.drop_column("pipeline_runs", "server_ip")
    op.drop_column("pipeline_runs", "origin_ip")
