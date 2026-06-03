"""
Assessment service — all business logic for assessment lifecycle
and staff submission/evaluation.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from fastapi import HTTPException, status, BackgroundTasks

from models import (
    Assessment, AssessmentTarget, Question, StaffAssessment, StaffAnswer,
    AssessmentStatus, StaffAssessmentStatus, QuestionType, User, UserRole,
    KnowledgeSource, AuditLog,
)
from schemas import (
    AssessmentCreateRequest, StartAssessmentRequest, SubmitAssessmentRequest,
    AssessmentFeedbackOut, AnswerFeedback,
)
from rag import generate_questions_for_assessment
from rag.evaluator import evaluate_mcq, evaluate_written, compute_summary


# ─────────────────────────────────────────────────────────────────
# Create (draft)
# ─────────────────────────────────────────────────────────────────

async def create_assessment(
    *,
    req: AssessmentCreateRequest,
    current_user: User,
    db: AsyncSession,
    background_tasks: BackgroundTasks,
) -> Assessment:
    """Create a draft assessment and trigger async question generation."""

    assessment = Assessment(
        id=uuid.uuid4(),
        org_id=current_user.org_id,
        created_by=current_user.id,
        name=req.name,
        description=req.description,
        assessment_type=req.assessment_type,
        question_type=req.question_type,
        topic=req.topic,
        information_source=req.information_source,
        context_prompt=req.context_prompt,
        num_questions=req.num_questions,
        time_limit_minutes=req.time_limit_minutes,
        status=AssessmentStatus.DRAFT,
        target_type=req.target_type,
    )
    db.add(assessment)

    for target_id in req.target_ids:
        db.add(AssessmentTarget(
            assessment_id=assessment.id,
            target_type=req.target_type,
            target_id=target_id,
        ))

    await db.flush()

    # Kick off question generation in background
    background_tasks.add_task(
        _generate_questions_background,
        assessment_id=str(assessment.id),
    )

    await _audit(db, current_user.id, "CREATE_ASSESSMENT", "assessment", assessment.id)
    return assessment


async def _generate_questions_background(assessment_id: str):
    """Background task: run the RAG pipeline and populate questions."""
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Assessment).where(Assessment.id == uuid.UUID(assessment_id))
        )
        assessment = result.scalar_one_or_none()
        if not assessment:
            return
        try:
            await generate_questions_for_assessment(assessment, db)
            await db.commit()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(
                "Question generation failed for %s: %s", assessment_id, e
            )


# ─────────────────────────────────────────────────────────────────
# Deploy
# ─────────────────────────────────────────────────────────────────

async def deploy_assessment(
    *,
    assessment_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> Assessment:
    assessment = await _get_assessment_owned(assessment_id, current_user, db)

    if assessment.status != AssessmentStatus.DRAFT:
        raise HTTPException(400, "Only draft assessments can be deployed")

    # Ensure questions have been generated
    q_count = await db.scalar(
        select(func.count()).where(Question.assessment_id == assessment_id)
    )
    if not q_count:
        raise HTTPException(400, "Assessment has no questions yet; generation may still be running")

    assessment.status = AssessmentStatus.DEPLOYED
    assessment.deployed_at = datetime.utcnow()
    db.add(assessment)

    await _audit(db, current_user.id, "DEPLOY_ASSESSMENT", "assessment", assessment.id)
    return assessment


# ─────────────────────────────────────────────────────────────────
# Cancel
# ─────────────────────────────────────────────────────────────────

async def cancel_assessment(
    *,
    assessment_id: uuid.UUID,
    reason: str | None,
    current_user: User,
    db: AsyncSession,
) -> Assessment:
    assessment = await _get_assessment_owned(assessment_id, current_user, db)

    if assessment.status in (AssessmentStatus.DRAFT, AssessmentStatus.CANCELLED):
        raise HTTPException(400, "Can only cancel deployed or active assessments")

    assessment.status = AssessmentStatus.CANCELLED
    assessment.cancelled_at = datetime.utcnow()
    assessment.cancelled_reason = reason
    db.add(assessment)

    await _audit(db, current_user.id, "CANCEL_ASSESSMENT", "assessment", assessment.id,
                 detail={"reason": reason})
    return assessment


# ─────────────────────────────────────────────────────────────────
# Staff: start an assessment session
# ─────────────────────────────────────────────────────────────────

async def start_assessment(
    *,
    req: StartAssessmentRequest,
    current_user: User,
    db: AsyncSession,
) -> tuple[StaffAssessment, list[Question]]:
    """
    Create a StaffAssessment session. Returns the session and the
    questions — without correct answers (those are server-only).
    """
    result = await db.execute(
        select(Assessment).where(
            Assessment.id == req.assessment_id,
            Assessment.org_id == current_user.org_id,
            Assessment.status == AssessmentStatus.DEPLOYED,
        )
    )
    assessment = result.scalar_one_or_none()
    if not assessment:
        raise HTTPException(404, "Assessment not found or not available")

    # Check the user is a valid target
    await _verify_user_is_target(req.assessment_id, current_user, db)

    # Idempotent: return existing session if already started
    existing = await db.execute(
        select(StaffAssessment).where(
            StaffAssessment.assessment_id == req.assessment_id,
            StaffAssessment.user_id == current_user.id,
        )
    )
    sa = existing.scalar_one_or_none()
    if sa and sa.status != StaffAssessmentStatus.NOT_STARTED:
        questions = await _get_questions_safe(req.assessment_id, db)
        return sa, questions

    # Create new session
    sa = StaffAssessment(
        id=uuid.uuid4(),
        assessment_id=req.assessment_id,
        user_id=current_user.id,
        status=StaffAssessmentStatus.IN_PROGRESS,
        started_at=datetime.utcnow(),
        pre_check_passed=req.pre_check_passed,
        pre_check_data=req.pre_check_data,
    )
    db.add(sa)
    await db.flush()

    questions = await _get_questions_safe(req.assessment_id, db)
    return sa, questions


# ─────────────────────────────────────────────────────────────────
# Staff: submit and evaluate
# ─────────────────────────────────────────────────────────────────

async def submit_assessment(
    *,
    req: SubmitAssessmentRequest,
    current_user: User,
    db: AsyncSession,
) -> AssessmentFeedbackOut:
    """Submit answers, evaluate, store results, return detailed feedback."""

    # Load session
    result = await db.execute(
        select(StaffAssessment).where(
            StaffAssessment.id == req.staff_assessment_id,
            StaffAssessment.user_id == current_user.id,
        )
    )
    sa = result.scalar_one_or_none()
    if not sa:
        raise HTTPException(404, "Assessment session not found")
    if sa.status == StaffAssessmentStatus.SUBMITTED:
        raise HTTPException(400, "Assessment already submitted")

    # Load questions with answers
    q_result = await db.execute(
        select(Question).where(Question.assessment_id == sa.assessment_id)
    )
    questions: list[Question] = q_result.scalars().all()
    q_map = {str(q.id): q for q in questions}

    # Build evaluation payloads and store answers
    eval_payloads = []
    for submitted in req.answers:
        qid = str(submitted.question_id)
        q = q_map.get(qid)
        if not q:
            continue

        answer = StaffAnswer(
            id=uuid.uuid4(),
            staff_assessment_id=sa.id,
            question_id=submitted.question_id,
            answer_index=submitted.answer_index,
            answer_text=submitted.answer_text,
        )
        db.add(answer)

        eval_payloads.append({
            "question_id": qid,
            "question_type": q.question_type.value,
            "question_text": q.text,
            "correct_answer_index": q.correct_answer_index,
            "correct_answer_text": q.correct_answer_text,
            "given_index": submitted.answer_index,
            "given_text": submitted.answer_text,
        })

    await db.flush()

    # Evaluate answers
    answer_results = []
    for payload in eval_payloads:
        if payload["question_type"] == QuestionType.MCQ.value:
            r = evaluate_mcq(
                question_id=payload["question_id"],
                correct_index=payload["correct_answer_index"] or 0,
                given_index=payload["given_index"],
            )
        else:
            r = await evaluate_written(
                question_id=payload["question_id"],
                question_text=payload["question_text"],
                model_answer=payload["correct_answer_text"] or "",
                staff_response=payload["given_text"] or "",
            )
        answer_results.append(r)

    # Persist evaluation results
    answer_map = {}
    for child in sa.answers:
        answer_map[str(child.question_id)] = child

    for r in answer_results:
        child = answer_map.get(r.question_id)
        if child:
            child.is_correct = r.is_correct
            child.score = r.score
            child.ai_feedback = r.ai_feedback
            db.add(child)

    # Update session summary
    summary = compute_summary(answer_results)
    sa.status = StaffAssessmentStatus.EVALUATED
    sa.submitted_at = datetime.utcnow()
    sa.evaluated_at = datetime.utcnow()
    sa.score_pct = summary["score_pct"]
    sa.questions_correct = summary["questions_correct"]
    sa.questions_total = summary["questions_total"]
    db.add(sa)

    await db.flush()
    await _audit(db, current_user.id, "SUBMIT_ASSESSMENT", "staff_assessment", sa.id)

    # Build feedback response
    assessment = await db.get(Assessment, sa.assessment_id)
    result_map = {r.question_id: r for r in answer_results}

    feedback_answers = []
    for payload in eval_payloads:
        qid = payload["question_id"]
        q = q_map.get(qid)
        r = result_map.get(qid)
        if not q or not r:
            continue
        feedback_answers.append(AnswerFeedback(
            question_id=q.id,
            question_text=q.text,
            question_type=q.question_type,
            options=q.options,
            given_answer_index=payload["given_index"],
            given_answer_text=payload["given_text"],
            correct_answer_index=q.correct_answer_index,
            correct_answer_text=q.correct_answer_text,
            is_correct=r.is_correct,
            score=r.score,
            explanation=q.explanation,
            source_reference=q.source_reference,
            ai_feedback=r.ai_feedback,
        ))

    return AssessmentFeedbackOut(
        staff_assessment_id=sa.id,
        assessment_name=assessment.name,
        assessment_type=assessment.assessment_type,
        score_pct=summary["score_pct"],
        questions_correct=summary["questions_correct"],
        questions_total=summary["questions_total"],
        submitted_at=sa.submitted_at,
        answers=feedback_answers,
    )


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

async def _get_assessment_owned(
    assessment_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> Assessment:
    """Load assessment and verify the caller owns it (or is HR)."""
    result = await db.execute(
        select(Assessment).where(Assessment.id == assessment_id)
    )
    assessment = result.scalar_one_or_none()
    if not assessment:
        raise HTTPException(404, "Assessment not found")

    if current_user.role not in (UserRole.HR_ADMIN, UserRole.SYSTEM_ADMIN):
        if str(assessment.created_by) != str(current_user.id):
            raise HTTPException(403, "Not authorised to modify this assessment")

    return assessment


async def _verify_user_is_target(
    assessment_id: uuid.UUID,
    user: User,
    db: AsyncSession,
):
    """Verify a staff member is in the assessment's target list."""
    # TODO: implement full department membership check
    # For now: any active user in the same org can take an assessment
    pass


async def _get_questions_safe(
    assessment_id: uuid.UUID,
    db: AsyncSession,
) -> list[Question]:
    """Return questions WITHOUT the correct_answer_index set (safe for staff)."""
    result = await db.execute(
        select(Question)
        .where(Question.assessment_id == assessment_id)
        .order_by(Question.order_index)
    )
    questions = result.scalars().all()
    # Null out answers before returning to client
    for q in questions:
        q.correct_answer_index = None
        q.correct_answer_text = None
        q.explanation = None
    return questions


async def _audit(
    db: AsyncSession,
    user_id,
    action: str,
    resource_type: str,
    resource_id,
    detail: dict | None = None,
):
    db.add(AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
    ))
