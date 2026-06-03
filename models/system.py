"""
System process observability — pipeline runs and their phased steps.

A PipelineRun represents one execution of a system process (question
generation, document indexing, or submission evaluation). Each run has an
ordered list of PipelineStep rows whose status transitions drive the
phased checklist in the ops dashboard.

Status values are plain strings (no native enum) to avoid migrations:
  run.status  : "running" | "completed" | "failed"
  step.status : "pending" | "running" | "ok" | "error" | "warn"
"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, ForeignKey, Integer, Text, Float
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database import Base


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), nullable=True)
    kind = Column(String(40), nullable=False)        # generation | indexing | evaluation
    ref_id = Column(UUID(as_uuid=True), nullable=True)  # assessment / source / staff_assessment id
    label = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False, default="running")
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    # Capsule metadata (real origin/server capture)
    origin_ip = Column(String(64), nullable=True)    # client IP that triggered the transaction
    server_ip = Column(String(64), nullable=True)    # host the API ran on
    system_id = Column(String(128), nullable=True)   # server / container hostname

    steps = relationship(
        "PipelineStep",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="PipelineStep.order_index",
    )
    spans = relationship(
        "PipelineSpan",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="PipelineSpan.started_at",
    )


class PipelineSpan(Base):
    """
    A fine-grained span: one real backing-service call within a run
    (an OpenAI request, a Chroma query, a Postgres flush, …). Powers the
    'logs grouped by service' Log Capsule with true per-call timings.

    service : openai | chroma | postgres | redis | minio | app
    status  : ok | error
    """
    __tablename__ = "pipeline_spans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("pipeline_runs.id"), nullable=False)
    service = Column(String(40), nullable=False)
    operation = Column(String(120), nullable=False)   # e.g. "chat.completion", "similarity_search"
    phase = Column(String(80), nullable=True)         # owning phase key, if any
    status = Column(String(20), nullable=False, default="ok")
    detail = Column(Text, nullable=True)
    duration_ms = Column(Float, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    run = relationship("PipelineRun", back_populates="spans")


class PipelineStep(Base):
    __tablename__ = "pipeline_steps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("pipeline_runs.id"), nullable=False)
    order_index = Column(Integer, nullable=False, default=0)
    phase = Column(String(80), nullable=False)       # machine key, unique within a run
    label = Column(String(255), nullable=False)      # human-readable
    status = Column(String(20), nullable=False, default="pending")
    detail = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    run = relationship("PipelineRun", back_populates="steps")
