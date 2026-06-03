"""Unit tests for AssessmentCreateRequest validators (schemas/__init__.py)."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from schemas import AssessmentCreateRequest
from models.assessment import AssessmentType, QuestionType, InformationSource, TargetType


def make(**over):
    base = dict(
        name="Test Assessment",
        assessment_type=AssessmentType.TECHNICAL,
        question_type=QuestionType.MCQ,
        topic="Networking fundamentals",
        information_source=InformationSource.AI_GENERATED,
        num_questions=10,
        time_limit_minutes=30,
        target_type=TargetType.INDIVIDUALS,
        target_ids=[uuid4()],
    )
    base.update(over)
    return AssessmentCreateRequest(**base)


# ── num_questions caps per format ─────────────────────────────────
def test_mcq_default_is_valid():
    req = make()
    assert req.num_questions == 10


def test_mcq_cap_30():
    make(num_questions=30)                     # ok
    with pytest.raises(ValidationError):
        make(num_questions=31)


def test_personality_cap_60():
    make(question_type=QuestionType.PERSONALITY, num_questions=60)   # ok
    with pytest.raises(ValidationError):
        make(question_type=QuestionType.PERSONALITY, num_questions=61)


def test_scenario_cap_8():
    make(question_type=QuestionType.SCENARIO,
         information_source=InformationSource.KNOWLEDGE_BASE, source_id=uuid4(),
         num_questions=8)                       # ok
    with pytest.raises(ValidationError):
        make(question_type=QuestionType.SCENARIO,
             information_source=InformationSource.KNOWLEDGE_BASE, source_id=uuid4(),
             num_questions=9)


# ── Grounding rules ───────────────────────────────────────────────
def test_scenario_requires_kb_or_hybrid():
    # AI source is not allowed for case studies.
    with pytest.raises(ValidationError):
        make(question_type=QuestionType.SCENARIO,
             information_source=InformationSource.AI_GENERATED, num_questions=5)


def test_scenario_requires_source_id():
    with pytest.raises(ValidationError):
        make(question_type=QuestionType.SCENARIO,
             information_source=InformationSource.KNOWLEDGE_BASE, source_id=None,
             num_questions=5)


def test_scenario_hybrid_with_source_is_valid():
    req = make(question_type=QuestionType.SCENARIO,
               information_source=InformationSource.HYBRID, source_id=uuid4(),
               num_questions=5)
    assert req.information_source == InformationSource.HYBRID


def test_hybrid_requires_source_id():
    # Hybrid (any format) must name a KB document.
    with pytest.raises(ValidationError):
        make(information_source=InformationSource.HYBRID, source_id=None)
    make(information_source=InformationSource.HYBRID, source_id=uuid4())   # ok


def test_num_questions_floor_5():
    with pytest.raises(ValidationError):
        make(num_questions=4)
