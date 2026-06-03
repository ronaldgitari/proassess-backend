import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Boolean, DateTime, ForeignKey,
    Enum as SAEnum, Text, Integer, Float, JSON
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from database import Base
import enum


class AssessmentType(str, enum.Enum):
    TECHNICAL = "technical"
    PROFESSIONAL = "professional"


class QuestionType(str, enum.Enum):
    MCQ = "mcq"
    WRITTEN = "written"
    PERSONALITY = "personality"   # 16Personalities-style Likert items (no right/wrong)
    CODING = "coding"             # written-style, answered in an embedded code editor
    SCENARIO = "scenario"         # case-study: one shared KB-grounded case + written analysis Qs


class AssessmentStatus(str, enum.Enum):
    DRAFT = "draft"
    DEPLOYED = "deployed"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class InformationSource(str, enum.Enum):
    KNOWLEDGE_BASE = "kb"          # org RAG index
    AI_GENERATED = "ai"            # pure GPT domain knowledge
    INDUSTRY = "industry"          # curated frameworks (16P, ISO, etc.)
    CUSTOM_URL = "url"             # specific external URLs
    HYBRID = "hybrid"             # KB doc + credible web case-study sources (domain-specific)


class TargetType(str, enum.Enum):
    DEPARTMENT = "department"
    INDIVIDUALS = "individuals"
    ORGANISATION = "organisation"  # HR only


class StaffAssessmentStatus(str, enum.Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    PENDING_REVIEW = "pending_review"   # scenario: AI-drafted score awaiting LM confirmation
    EVALUATED = "evaluated"


# ─────────────────────────────────────────────────────────────────

class Assessment(Base):
    __tablename__ = "assessments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    assessment_type = Column(SAEnum(AssessmentType), nullable=False)
    question_type = Column(SAEnum(QuestionType), nullable=False, default=QuestionType.MCQ)
    topic = Column(String(255), nullable=False)
    information_source = Column(SAEnum(InformationSource), nullable=False)
    context_prompt = Column(Text, nullable=True)       # LM-provided context
    num_questions = Column(Integer, default=10)
    time_limit_minutes = Column(Integer, default=30)
    status = Column(SAEnum(AssessmentStatus), nullable=False, default=AssessmentStatus.DRAFT)
    target_type = Column(SAEnum(TargetType), nullable=False)
    rag_metadata = Column(JSONB, nullable=True)        # stored retrieval config
    created_at = Column(DateTime, default=datetime.utcnow)
    deployed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancelled_reason = Column(Text, nullable=True)
    # Soft-delete: archived assessments are hidden from LM/staff lists but their
    # questions + completed staff results/feedback are preserved.
    is_archived = Column(Boolean, nullable=False, default=False, server_default="false")

    # Relationships
    created_by_user = relationship("User", back_populates="created_assessments", foreign_keys=[created_by])
    questions = relationship("Question", back_populates="assessment", cascade="all, delete-orphan", order_by="Question.order_index")
    targets = relationship("AssessmentTarget", back_populates="assessment", cascade="all, delete-orphan")
    staff_assessments = relationship("StaffAssessment", back_populates="assessment")


class AssessmentTarget(Base):
    """Records who a deployed assessment is targeted at."""
    __tablename__ = "assessment_targets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assessment_id = Column(UUID(as_uuid=True), ForeignKey("assessments.id"), nullable=False)
    target_type = Column(SAEnum(TargetType), nullable=False)
    target_id = Column(UUID(as_uuid=True), nullable=False)  # dept_id or user_id

    assessment = relationship("Assessment", back_populates="targets")


class Question(Base):
    __tablename__ = "questions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assessment_id = Column(UUID(as_uuid=True), ForeignKey("assessments.id"), nullable=False)
    order_index = Column(Integer, nullable=False, default=0)
    text = Column(Text, nullable=False)
    question_type = Column(SAEnum(QuestionType), nullable=False)
    # MCQ fields
    options = Column(JSONB, nullable=True)              # ["option A", "option B", ...]
    correct_answer_index = Column(Integer, nullable=True)  # 0-based index for MCQ
    correct_answer_text = Column(Text, nullable=True)      # for written Qs
    explanation = Column(Text, nullable=True)
    source_reference = Column(String(500), nullable=True)  # URL or doc title
    difficulty = Column(Integer, default=3)             # 1-5
    # RAG provenance
    retrieved_chunk_ids = Column(JSONB, nullable=True)  # list of chunk IDs used

    assessment = relationship("Assessment", back_populates="questions")
    answers = relationship("StaffAnswer", back_populates="question")


# ─────────────────────────────────────────────────────────────────

class StaffAssessment(Base):
    """A single staff member's attempt at an assessment."""
    __tablename__ = "staff_assessments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assessment_id = Column(UUID(as_uuid=True), ForeignKey("assessments.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    status = Column(SAEnum(StaffAssessmentStatus), nullable=False, default=StaffAssessmentStatus.NOT_STARTED)
    started_at = Column(DateTime, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    evaluated_at = Column(DateTime, nullable=True)
    # Score summary
    score_pct = Column(Float, nullable=True)
    questions_correct = Column(Integer, nullable=True)
    questions_total = Column(Integer, nullable=True)
    # Latency check result
    pre_check_passed = Column(Boolean, nullable=True)
    pre_check_data = Column(JSONB, nullable=True)
    # Human-assisted verification (scenario assessments): who confirmed the
    # AI-drafted score and when. Until reviewed, status stays PENDING_REVIEW and
    # the result is excluded from staff results + stats (which filter on EVALUATED).
    reviewed_by_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)

    assessment = relationship("Assessment", back_populates="staff_assessments")
    user = relationship("User", back_populates="staff_assessments", foreign_keys=[user_id])
    answers = relationship("StaffAnswer", back_populates="staff_assessment", cascade="all, delete-orphan")


class StaffAnswer(Base):
    __tablename__ = "staff_answers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    staff_assessment_id = Column(UUID(as_uuid=True), ForeignKey("staff_assessments.id"), nullable=False)
    question_id = Column(UUID(as_uuid=True), ForeignKey("questions.id"), nullable=False)
    # MCQ: index chosen; written: free text
    answer_index = Column(Integer, nullable=True)
    answer_text = Column(Text, nullable=True)
    # Evaluation
    is_correct = Column(Boolean, nullable=True)
    score = Column(Float, nullable=True)         # 0-100 for written
    ai_feedback = Column(Text, nullable=True)    # GPT feedback for written Qs
    # Credible sources (grounded KB + web) backing the rich scenario feedback:
    # [{ "title", "url", "snippet", "kind": "kb"|"web" }, ...]
    feedback_sources = Column(JSONB, nullable=True)
    answered_at = Column(DateTime, default=datetime.utcnow)

    staff_assessment = relationship("StaffAssessment", back_populates="answers")
    question = relationship("Question", back_populates="answers")
