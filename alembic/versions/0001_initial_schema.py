"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-29 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Enums ───────────────────────────────────────────────────────
    user_role = postgresql.ENUM(
        "staff", "lm", "hr_admin", "system_admin",
        name="userrole", create_type=True,
    )
    group_type = postgresql.ENUM(
        "owner", "collaborator", "member", "backup",
        name="grouptype", create_type=True,
    )
    assessment_type = postgresql.ENUM(
        "technical", "professional",
        name="assessmenttype", create_type=True,
    )
    question_type = postgresql.ENUM(
        "mcq", "written",
        name="questiontype", create_type=True,
    )
    assessment_status = postgresql.ENUM(
        "draft", "deployed", "active", "completed", "cancelled",
        name="assessmentstatus", create_type=True,
    )
    information_source = postgresql.ENUM(
        "kb", "ai", "industry", "url",
        name="informationsource", create_type=True,
    )
    target_type = postgresql.ENUM(
        "department", "individuals", "organisation",
        name="targettype", create_type=True,
    )
    staff_assessment_status = postgresql.ENUM(
        "not_started", "in_progress", "submitted", "evaluated",
        name="staffassessmentstatus", create_type=True,
    )
    source_type = postgresql.ENUM(
        "pdf", "docx", "xlsx", "web", "url",
        name="sourcetype", create_type=True,
    )
    source_status = postgresql.ENUM(
        "pending", "indexing", "active", "failed", "stale",
        name="sourcestatus", create_type=True,
    )

    for enum in (
        user_role, group_type, assessment_type, question_type,
        assessment_status, information_source, target_type,
        staff_assessment_status, source_type, source_status,
    ):
        enum.create(op.get_bind(), checkfirst=True)

    # ── organisations ───────────────────────────────────────────────
    op.create_table(
        "organisations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("slug", name="uq_organisations_slug"),
    )

    # ── users ───────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=True),
        sa.Column("role", sa.Enum("staff", "lm", "hr_admin", "system_admin", name="userrole"), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ── departments ─────────────────────────────────────────────────
    op.create_table(
        "departments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    # ── user_departments ────────────────────────────────────────────
    op.create_table(
        "user_departments",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("departments.id"), primary_key=True),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("line_manager_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
    )

    # ── security_groups ─────────────────────────────────────────────
    op.create_table(
        "security_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("group_type", sa.Enum("owner", "collaborator", "member", "backup", name="grouptype"), nullable=False),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    # ── group_memberships ───────────────────────────────────────────
    op.create_table(
        "group_memberships",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("security_groups.id"), primary_key=True),
        sa.Column("is_owner", sa.Boolean(), nullable=True),
        sa.Column("joined_at", sa.DateTime(), nullable=True),
    )

    # ── knowledge_sources ───────────────────────────────────────────
    op.create_table(
        "knowledge_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("source_type", sa.Enum("pdf", "docx", "xlsx", "web", "url", name="sourcetype"), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("s3_key", sa.String(500), nullable=True),
        sa.Column("domain_tag", sa.String(100), nullable=True),
        sa.Column("status", sa.Enum("pending", "indexing", "active", "failed", "stale", name="sourcestatus"), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(), nullable=True),
        sa.Column("index_error", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
    )

    # ── document_chunks ─────────────────────────────────────────────
    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("knowledge_sources.id"), nullable=False),
        sa.Column("chroma_id", sa.String(100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("chroma_id", name="uq_document_chunks_chroma_id"),
    )

    # ── audit_log ───────────────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_audit_log_timestamp", "audit_log", ["timestamp"])

    # ── assessments ─────────────────────────────────────────────────
    op.create_table(
        "assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organisations.id"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("assessment_type", sa.Enum("technical", "professional", name="assessmenttype"), nullable=False),
        sa.Column("question_type", sa.Enum("mcq", "written", name="questiontype"), nullable=False),
        sa.Column("topic", sa.String(255), nullable=False),
        sa.Column("information_source", sa.Enum("kb", "ai", "industry", "url", name="informationsource"), nullable=False),
        sa.Column("context_prompt", sa.Text(), nullable=True),
        sa.Column("num_questions", sa.Integer(), nullable=True),
        sa.Column("time_limit_minutes", sa.Integer(), nullable=True),
        sa.Column("status", sa.Enum("draft", "deployed", "active", "completed", "cancelled", name="assessmentstatus"), nullable=False),
        sa.Column("target_type", sa.Enum("department", "individuals", "organisation", name="targettype"), nullable=False),
        sa.Column("rag_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("deployed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_reason", sa.Text(), nullable=True),
    )

    # ── assessment_targets ──────────────────────────────────────────
    op.create_table(
        "assessment_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("assessments.id"), nullable=False),
        sa.Column("target_type", sa.Enum("department", "individuals", "organisation", name="targettype"), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
    )

    # ── questions ───────────────────────────────────────────────────
    op.create_table(
        "questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("assessments.id"), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("question_type", sa.Enum("mcq", "written", name="questiontype"), nullable=False),
        sa.Column("options", postgresql.JSONB(), nullable=True),
        sa.Column("correct_answer_index", sa.Integer(), nullable=True),
        sa.Column("correct_answer_text", sa.Text(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("source_reference", sa.String(500), nullable=True),
        sa.Column("difficulty", sa.Integer(), nullable=True),
        sa.Column("retrieved_chunk_ids", postgresql.JSONB(), nullable=True),
    )

    # ── staff_assessments ───────────────────────────────────────────
    op.create_table(
        "staff_assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("assessments.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.Enum("not_started", "in_progress", "submitted", "evaluated", name="staffassessmentstatus"), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(), nullable=True),
        sa.Column("score_pct", sa.Float(), nullable=True),
        sa.Column("questions_correct", sa.Integer(), nullable=True),
        sa.Column("questions_total", sa.Integer(), nullable=True),
        sa.Column("pre_check_passed", sa.Boolean(), nullable=True),
        sa.Column("pre_check_data", postgresql.JSONB(), nullable=True),
    )

    # ── staff_answers ───────────────────────────────────────────────
    op.create_table(
        "staff_answers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("staff_assessment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("staff_assessments.id"), nullable=False),
        sa.Column("question_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("questions.id"), nullable=False),
        sa.Column("answer_index", sa.Integer(), nullable=True),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("is_correct", sa.Boolean(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("ai_feedback", sa.Text(), nullable=True),
        sa.Column("answered_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("staff_answers")
    op.drop_table("staff_assessments")
    op.drop_table("questions")
    op.drop_table("assessment_targets")
    op.drop_table("assessments")
    op.drop_table("audit_log")
    op.drop_table("document_chunks")
    op.drop_table("knowledge_sources")
    op.drop_table("group_memberships")
    op.drop_table("security_groups")
    op.drop_table("user_departments")
    op.drop_table("departments")
    op.drop_table("users")
    op.drop_table("organisations")

    for name in (
        "staffassessmentstatus", "targettype", "informationsource",
        "assessmentstatus", "questiontype", "assessmenttype",
        "sourcestatus", "sourcetype", "grouptype", "userrole",
    ):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)
