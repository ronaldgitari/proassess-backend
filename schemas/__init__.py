from __future__ import annotations
from typing import List, Optional, Any, Dict
from uuid import UUID
from datetime import datetime, date, timezone
from pydantic import BaseModel, EmailStr, Field, model_validator, field_serializer
from models.assessment import AssessmentType, QuestionType, AssessmentStatus, InformationSource, TargetType, StaffAssessmentStatus
from models.knowledge import SourceType, SourceStatus
from models.user import UserRole, GroupType


class _UTCDatetimeMixin(BaseModel):
    """
    Serialize naive datetimes as UTC ('Z' suffix) so the frontend's
    `new Date(str)` converts UTC → the viewer's local timezone correctly.
    Without this, a tz-less ISO string is read as LOCAL time → wrong offset.

    mode="wrap": only datetimes are transformed; every other field (incl.
    nested models/lists) is delegated to Pydantic's default `handler` so
    normal recursive serialization is preserved.
    """
    @field_serializer("*", mode="wrap", when_used="json", check_fields=False)
    def _ser_dt(self, v, handler, _info):
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return handler(v)


# ─────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────

class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(_UTCDatetimeMixin):
    id: UUID
    email: str
    name: str
    full_name: str = ""
    role: UserRole
    org_id: UUID
    is_active: bool
    force_password_change: bool = False
    permissions: List[str] = []                          # effective capability keys (role ∪ groups ± overrides)
    effective_settings: Optional[Dict[str, Any]] = None  # computed platform settings for this user
    created_at: datetime
    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def populate_full_name(self) -> "UserOut":
        if not self.full_name:
            self.full_name = self.name
        return self


class StaffProfileOut(BaseModel):
    id: UUID
    full_name: str
    email: str
    role: UserRole
    department: Optional[str] = None
    job_title: Optional[str] = None
    line_manager: Optional[str] = None


# ─────────────────────────────────────────────────────────────────
# HR user management
# ─────────────────────────────────────────────────────────────────

class AdminUserOut(BaseModel):
    id: UUID
    email: str
    name: str
    role: UserRole
    is_active: bool
    start_date: Optional[date] = None
    department_id: Optional[UUID] = None
    department: Optional[str] = None
    job_title: Optional[str] = None
    line_manager_id: Optional[UUID] = None
    line_manager: Optional[str] = None
    created_at: Optional[datetime] = None


class UserCreateRequest(BaseModel):
    email: EmailStr
    name: str = Field(..., min_length=2, max_length=255)
    role: UserRole = UserRole.STAFF
    password: str = Field(..., min_length=8, max_length=128)
    start_date: Optional[date] = None
    department_id: Optional[UUID] = None
    job_title: Optional[str] = None
    line_manager_id: Optional[UUID] = None


class UserUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None
    start_date: Optional[date] = None
    department_id: Optional[UUID] = None
    job_title: Optional[str] = None
    line_manager_id: Optional[UUID] = None


class PasswordResetOut(BaseModel):
    temp_password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


class DepartmentCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)


class DepartmentOut(BaseModel):
    id: UUID
    name: str


class StaffResultSummary(BaseModel):
    staff_assessment_id: UUID
    assessment_id: UUID
    assessment_name: str
    assessment_type: str
    score_pct: float
    questions_correct: int
    questions_total: int
    submitted_at: datetime
    passed: bool


# ─────────────────────────────────────────────────────────────────
# Assessments
# ─────────────────────────────────────────────────────────────────

class AssessmentCreateRequest(BaseModel):
    name: str = Field(..., min_length=3, max_length=255)
    description: Optional[str] = None
    assessment_type: AssessmentType
    question_type: QuestionType = QuestionType.MCQ
    topic: str = Field(..., min_length=2, max_length=255)
    information_source: InformationSource = InformationSource.KNOWLEDGE_BASE
    context_prompt: Optional[str] = None
    # Lower bound enforced here; upper bound depends on question_type (see validator below)
    num_questions: int = Field(default=10, ge=5)
    time_limit_minutes: int = Field(default=30, ge=5, le=120)
    target_type: TargetType
    target_ids: List[UUID] = Field(..., min_length=1)   # dept IDs or user IDs
    source_id: Optional[UUID] = None                    # specific KB document to retrieve from
    language: Optional[str] = None                       # programming language (coding assessments)

    @model_validator(mode="after")
    def validate_num_questions(self) -> "AssessmentCreateRequest":
        # 16Personalities is fixed at 60; case studies are few & deep (≤8);
        # all other formats cap at 30.
        if self.question_type == QuestionType.PERSONALITY:
            cap = 60
        elif self.question_type == QuestionType.SCENARIO:
            cap = 8
        else:
            cap = 30
        if self.num_questions > cap:
            raise ValueError(f"num_questions must be ≤ {cap} for this question format")
        return self

    @model_validator(mode="after")
    def validate_grounding(self) -> "AssessmentCreateRequest":
        # Grounded sources: 'kb' (KB doc only) or 'hybrid' (KB doc + credible web
        # case-study sources). Both require a source_id (the document to ground on).
        kb_like = (InformationSource.KNOWLEDGE_BASE, InformationSource.HYBRID)
        if self.question_type == QuestionType.SCENARIO and self.information_source not in kb_like:
            raise ValueError("Case study assessments must be grounded (information_source 'kb' or 'hybrid')")
        if self.information_source == InformationSource.HYBRID and self.source_id is None:
            raise ValueError("Hybrid assessments require a source_id (the KB document to ground on)")
        if self.question_type == QuestionType.SCENARIO and self.source_id is None:
            raise ValueError("Case study assessments require a source_id (the KB document to ground the case)")
        return self


class AssessmentDeployRequest(BaseModel):
    assessment_id: UUID


class AssessmentCancelRequest(BaseModel):
    reason: Optional[str] = None


class AssessmentShareRequest(BaseModel):
    target_type: TargetType                              # department | individuals
    target_ids: List[UUID] = Field(..., min_length=1)


class QuestionOut(BaseModel):
    id: UUID
    order_index: int
    text: str
    question_type: QuestionType
    options: Optional[List[str]] = None
    # Note: correct_answer_index is NOT returned to staff
    model_config = {"from_attributes": True}


class QuestionWithAnswerOut(QuestionOut):
    """Full question data returned after submission or to LM."""
    correct_answer_index: Optional[int] = None
    correct_answer_text: Optional[str] = None
    explanation: Optional[str] = None
    source_reference: Optional[str] = None
    difficulty: int


class AssessmentOut(_UTCDatetimeMixin):
    id: UUID
    name: str
    description: Optional[str]
    assessment_type: AssessmentType
    question_type: QuestionType
    topic: str
    information_source: InformationSource
    num_questions: int
    time_limit_minutes: int
    status: AssessmentStatus
    created_at: datetime
    deployed_at: Optional[datetime]
    question_count: Optional[int] = None
    model_config = {"from_attributes": True}


# ─────────────────────────────────────────────────────────────────
# Staff assessment session
# ─────────────────────────────────────────────────────────────────

class StartAssessmentRequest(BaseModel):
    assessment_id: UUID
    pre_check_passed: bool = True
    pre_check_data: Optional[Dict[str, Any]] = None


class AnswerSubmit(BaseModel):
    question_id: UUID
    answer_index: Optional[int] = None        # MCQ
    answer_text: Optional[str] = None         # written

    @model_validator(mode="after")
    def check_answer_provided(self) -> "AnswerSubmit":
        if self.answer_index is None and not self.answer_text:
            raise ValueError("Either answer_index or answer_text must be provided")
        return self


class SubmitAssessmentRequest(BaseModel):
    staff_assessment_id: UUID
    answers: List[AnswerSubmit]


class AnswerFeedback(BaseModel):
    question_id: UUID
    question_text: str
    question_type: QuestionType
    options: Optional[List[str]]
    given_answer_index: Optional[int]
    given_answer_text: Optional[str]
    correct_answer_index: Optional[int]
    correct_answer_text: Optional[str]
    is_correct: Optional[bool] = None
    score: Optional[float] = None
    explanation: Optional[str]
    source_reference: Optional[str]
    ai_feedback: Optional[str]


class PersonalityDimensionOut(BaseModel):
    key: str
    pos_label: str
    neg_label: str
    pos_letter: str
    neg_letter: str
    letter: str
    toward_pos_pct: float
    winning_label: str
    winning_pct: float


class PersonalityResultOut(BaseModel):
    type_code: str
    base_code: str
    identity: Optional[str] = None
    type_name: str
    description: str = ""
    dimensions: List[PersonalityDimensionOut]


class AssessmentFeedbackOut(_UTCDatetimeMixin):
    staff_assessment_id: UUID
    assessment_name: str
    assessment_type: AssessmentType
    score_pct: Optional[float] = None
    questions_correct: Optional[int] = None
    questions_total: int
    submitted_at: datetime
    answers: List[AnswerFeedback]
    is_personality: bool = False
    personality_result: Optional[PersonalityResultOut] = None
    # Scenario/case-study: AI-drafted score awaiting LM confirmation. While true,
    # scores/feedback are withheld from the candidate until a Line Manager approves.
    pending_review: bool = False
    scenario: Optional[str] = None   # case-study narrative (shared stimulus), for the feedback view


# ── Scenario (case study) LM review ───────────────────────────────

class ScenarioAnswerReview(BaseModel):
    """Optional per-answer override an LM can apply when confirming a scenario result."""
    question_id: UUID
    score: Optional[float] = Field(default=None, ge=0, le=100)
    feedback: Optional[str] = None


class ScenarioReviewApproveRequest(BaseModel):
    answers: List[ScenarioAnswerReview] = []
    note: Optional[str] = None


# ─────────────────────────────────────────────────────────────────
# Knowledge sources
# ─────────────────────────────────────────────────────────────────

class KnowledgeSourceOut(BaseModel):
    id: UUID
    name: str
    source_type: SourceType
    url: Optional[str]
    domain_tag: Optional[str]
    status: SourceStatus
    chunk_count: int
    indexed_at: Optional[datetime]
    created_at: datetime
    model_config = {"from_attributes": True}


class AddUrlSourceRequest(BaseModel):
    url: str = Field(..., min_length=10)
    name: Optional[str] = None
    domain_tag: Optional[str] = None


# ─────────────────────────────────────────────────────────────────
# RAG generation status
# ─────────────────────────────────────────────────────────────────

class GenerateQuestionsRequest(BaseModel):
    assessment_id: UUID


class GenerationStatusOut(BaseModel):
    assessment_id: UUID
    status: str         # pending | generating | complete | failed
    questions_generated: int
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────
# Admin / analytics
# ─────────────────────────────────────────────────────────────────

class OrgStatsOut(BaseModel):
    total_assessments: int
    active_assessments: int
    total_staff_assessed: int
    avg_score_pct: float
    total_chunks: int
    knowledge_sources_count: int
