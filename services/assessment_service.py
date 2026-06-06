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

async def _charge_department_ids(user_id, db: AsyncSession) -> set[str]:
    """Departments 'in an LM's charge' = those containing staff whose line manager
    is this user (derived from user_departments.line_manager_id)."""
    from models import UserDepartment
    rows = (await db.execute(
        select(UserDepartment.department_id)
        .where(UserDepartment.line_manager_id == user_id)
        .distinct()
    )).scalars().all()
    return {str(r) for r in rows}


async def create_assessment(
    *,
    req: AssessmentCreateRequest,
    current_user: User,
    db: AsyncSession,
    background_tasks: BackgroundTasks,
    origin_ip: str | None = None,
) -> Assessment:
    """Create a draft assessment and trigger async question generation."""

    # Department-charge restriction: a creator without org-wide authority
    # (`users.manage` — i.e. an LM) may only target departments they line-manage.
    from models import TargetType
    if req.target_type == TargetType.DEPARTMENT:
        from services.auth_service import has_permission
        if not await has_permission(current_user, "users.manage", db):
            charge = await _charge_department_ids(current_user.id, db)
            if any(str(t) not in charge for t in req.target_ids):
                raise HTTPException(403, "You can only assign to departments you line-manage")

    rag_meta = {}
    if req.source_id:
        rag_meta["source_id"] = str(req.source_id)
    if req.language:
        rag_meta["language"] = req.language

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
        rag_metadata=rag_meta or None,
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
        origin_ip=origin_ip,
    )

    await _audit(db, current_user.id, "CREATE_ASSESSMENT", "assessment", assessment.id)
    return assessment


# Limit how many assessments generate concurrently (per worker). Without this, a
# burst of "create" actions all hit embeddings/retrieval at once → retrieval thins
# out → the grader honestly aborts (insufficient_context), as the stress test showed.
# Lazily created so the limit binds to the running event loop.
import asyncio as _asyncio
_GEN_SEMAPHORE: "_asyncio.Semaphore | None" = None


def _gen_semaphore() -> "_asyncio.Semaphore":
    global _GEN_SEMAPHORE
    if _GEN_SEMAPHORE is None:
        from config import settings
        _GEN_SEMAPHORE = _asyncio.Semaphore(max(1, settings.MAX_CONCURRENT_GENERATIONS))
    return _GEN_SEMAPHORE


async def _generate_questions_background(assessment_id: str, origin_ip: str | None = None):
    """Background task: run the RAG pipeline and populate questions."""
    from database import AsyncSessionLocal
    from services import pipeline_tracker as pt
    pt.set_origin_ip(origin_ip)   # so capture_server_meta records the real origin
    # Throttle concurrent generations so retrieval/grading stays healthy under bursts.
    async with _gen_semaphore():
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
                import logging, traceback
                logging.getLogger(__name__).error(
                    "Question generation failed for %s: %s\n%s",
                    assessment_id, e, traceback.format_exc(),
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

    assessment = await db.get(Assessment, sa.assessment_id)

    # ── Personality branch (typology, no pass-fail scoring) ───────
    if assessment.question_type == QuestionType.PERSONALITY:
        from rag.evaluator import compute_personality_result

        scored_items = []
        for submitted in req.answers:
            q = q_map.get(str(submitted.question_id))
            if not q:
                continue
            db.add(StaffAnswer(
                id=uuid.uuid4(),
                staff_assessment_id=sa.id,
                question_id=submitted.question_id,
                answer_index=submitted.answer_index,
                answer_text=None,
            ))
            tags = q.retrieved_chunk_ids or {}
            scored_items.append({
                "dimension": tags.get("dimension"),
                "direction": tags.get("direction", 1),
                "answer_index": submitted.answer_index,
            })

        profile = compute_personality_result(scored_items)

        sa.status = StaffAssessmentStatus.EVALUATED
        sa.submitted_at = datetime.utcnow()
        sa.evaluated_at = datetime.utcnow()
        sa.score_pct = None
        sa.questions_correct = None
        sa.questions_total = len(scored_items)
        db.add(sa)
        await db.flush()
        await _audit(db, current_user.id, "SUBMIT_ASSESSMENT", "staff_assessment", sa.id)

        return AssessmentFeedbackOut(
            staff_assessment_id=sa.id,
            assessment_name=assessment.name,
            assessment_type=assessment.assessment_type,
            score_pct=None,
            questions_correct=None,
            questions_total=len(scored_items),
            submitted_at=sa.submitted_at,
            answers=[],
            is_personality=True,
            personality_result=profile,
        )

    # ── Scenario / case-study branch (AI drafts → LM confirms) ────
    # Generate a rich, grounded (+ optionally web-sourced) draft score + feedback
    # per answer, then park the attempt in PENDING_REVIEW. The score is NOT created
    # (status stays out of EVALUATED) until a Line Manager approves it.
    if assessment.question_type == QuestionType.SCENARIO:
        from rag.feedback import generate_scenario_feedback
        from services import pipeline_tracker as pt

        submitted_map = {str(a.question_id): a for a in req.answers}
        for q in questions:
            sub = submitted_map.get(str(q.id))
            db.add(StaffAnswer(
                id=uuid.uuid4(), staff_assessment_id=sa.id, question_id=q.id,
                answer_index=None, answer_text=(sub.answer_text if sub else None),
            ))
        await db.flush()

        case_text = (assessment.rag_metadata or {}).get("scenario")
        run_id = await pt.create_run(
            kind="evaluation", label=f"Feedback · {assessment.name}",
            steps=[("feedback", "Generate AI feedback (grounded + web)"),
                   ("persist", "Persist draft & queue for LM review")],
            org_id=assessment.org_id, ref_id=sa.id,
        )
        pt.set_current_run(run_id)
        await pt.capture_server_meta(run_id)

        try:
            await pt.start_step(run_id, "feedback")
            saved = (await db.execute(
                select(StaffAnswer).where(StaffAnswer.staff_assessment_id == sa.id)
            )).scalars().all()
            saved_map = {str(a.question_id): a for a in saved}

            # Draft feedback per question runs CONCURRENTLY (each is an independent
            # web search + GPT call) — collapses a ~6×-serial wait into one round.
            # The run contextvar propagates into the gathered tasks, so spans still
            # attribute correctly; generate_scenario_feedback doesn't touch `db`, so
            # the ORM session isn't used concurrently — results are applied after.
            async def _draft(q):
                child = saved_map.get(str(q.id))
                fb = await generate_scenario_feedback(
                    topic=assessment.topic,
                    question_text=q.text,
                    rubric=q.explanation,
                    model_answer=q.correct_answer_text,
                    staff_response=child.answer_text if child else None,
                    case_text=case_text,
                )
                return child, fb

            drafted = await _asyncio.gather(*[_draft(q) for q in questions])
            scores: list[float] = []
            for child, fb in drafted:
                if child:
                    child.score = fb["score"]
                    child.ai_feedback = fb["feedback"]
                    child.feedback_sources = fb["sources"] or None
                    child.is_correct = None
                    db.add(child)
                scores.append(fb["score"])
            await pt.finish_step(run_id, "feedback", "ok", f"{len(scores)} answers drafted")

            await pt.start_step(run_id, "persist")
            draft = round(sum(scores) / len(scores), 1) if scores else 0.0
            sa.status = StaffAssessmentStatus.PENDING_REVIEW
            sa.submitted_at = datetime.utcnow()
            sa.evaluated_at = None
            sa.score_pct = draft               # DRAFT — not confirmed until LM approves
            sa.questions_correct = None
            sa.questions_total = len(questions)
            db.add(sa)
            async with pt.track_span("postgres", "UPDATE staff_answers + staff_assessment",
                                     phase="persist", detail=f"{len(scores)} answers + draft"):
                await db.flush()
            await pt.finish_step(run_id, "persist", "ok", f"draft {draft}% · queued for review")
            await pt.finish_run(run_id, "completed")
        except Exception as e:
            await pt.finish_run(run_id, "failed", error=str(e)[:500])
            raise

        await _audit(db, current_user.id, "SUBMIT_ASSESSMENT", "staff_assessment", sa.id)
        return AssessmentFeedbackOut(
            staff_assessment_id=sa.id,
            assessment_name=assessment.name,
            assessment_type=assessment.assessment_type,
            score_pct=None,                    # withheld until LM confirms
            questions_correct=None,
            questions_total=len(questions),
            submitted_at=sa.submitted_at,
            answers=[],
            pending_review=True,
        )

    # Map the answers the staff actually submitted, by question id
    submitted_map = {str(a.question_id): a for a in req.answers}

    # Build evaluation payloads over ALL questions so skipped ones count toward
    # the total (unanswered = 0). This keeps the denominator = full question count.
    eval_payloads = []
    for q in questions:
        qid = str(q.id)
        submitted = submitted_map.get(qid)
        given_index = submitted.answer_index if submitted else None
        given_text = submitted.answer_text if submitted else None

        db.add(StaffAnswer(
            id=uuid.uuid4(),
            staff_assessment_id=sa.id,
            question_id=q.id,
            answer_index=given_index,
            answer_text=given_text,
        ))

        eval_payloads.append({
            "question_id": qid,
            "question_type": q.question_type.value,
            "question_text": q.text,
            "correct_answer_index": q.correct_answer_index,
            "correct_answer_text": q.correct_answer_text,
            "given_index": given_index,
            "given_text": given_text,
        })

    await db.flush()

    # Evaluate answers — tracked for the ops dashboard
    from services import pipeline_tracker as pt
    run_id = await pt.create_run(
        kind="evaluation", label=f"Evaluate · {assessment.name}",
        steps=[("score", "Score answers"), ("persist", "Persist results & summary")],
        org_id=assessment.org_id, ref_id=sa.id,
    )
    pt.set_current_run(run_id)
    await pt.capture_server_meta(run_id)

    try:
        await pt.start_step(run_id, "score")
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
        await pt.finish_step(run_id, "score", "ok", f"{len(answer_results)} answers scored")

        await pt.start_step(run_id, "persist")
        # Persist evaluation results — query explicitly to avoid async lazy-load
        saved_answers_result = await db.execute(
            select(StaffAnswer).where(StaffAnswer.staff_assessment_id == sa.id)
        )
        answer_map = {str(a.question_id): a for a in saved_answers_result.scalars().all()}

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

        async with pt.track_span("postgres", "UPDATE staff_answers + staff_assessment", phase="persist",
                                 detail=f"{len(answer_results)} answers + summary"):
            await db.flush()
        await pt.finish_step(run_id, "persist", "ok", f"score {summary['score_pct']}%")
        await pt.finish_run(run_id, "completed")
    except Exception as e:
        await pt.finish_run(run_id, "failed", error=str(e)[:500])
        raise

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
    """
    Gate take-time access: the user must be covered by at least one of the
    assessment's AssessmentTarget rows —
      - organisation  → any active user in the org (target_id = org_id)
      - department     → user belongs to that department (via user_departments)
      - individuals    → target_id == user.id
    Raises 403 if not targeted. (HR/admins bypass, e.g. for previewing.)
    """
    from models import AssessmentTarget, TargetType, UserDepartment

    if user.role in (UserRole.HR_ADMIN, UserRole.SYSTEM_ADMIN):
        return  # admins are not gated

    targets = (await db.execute(
        select(AssessmentTarget).where(AssessmentTarget.assessment_id == assessment_id)
    )).scalars().all()

    if not targets:
        # No targets recorded → fail closed (nobody is explicitly targeted)
        raise HTTPException(403, "You are not assigned to this assessment")

    # Organisation-wide?
    for ttarget in targets:
        if ttarget.target_type == TargetType.ORGANISATION:
            return  # everyone in the org is targeted

    # Individual?
    individual_ids = {str(tt.target_id) for tt in targets if tt.target_type == TargetType.INDIVIDUALS}
    if str(user.id) in individual_ids:
        return

    # Department membership?
    dept_target_ids = {tt.target_id for tt in targets if tt.target_type == TargetType.DEPARTMENT}
    if dept_target_ids:
        user_dept_ids = set((await db.execute(
            select(UserDepartment.department_id).where(UserDepartment.user_id == user.id)
        )).scalars().all())
        if user_dept_ids & dept_target_ids:
            return

    raise HTTPException(403, "You are not assigned to this assessment")


async def _get_questions_safe(
    assessment_id: uuid.UUID,
    db: AsyncSession,
) -> list[Question]:
    """Return questions WITHOUT the correct_answer_index set (safe for staff)."""
    from sqlalchemy.orm import make_transient

    result = await db.execute(
        select(Question)
        .where(Question.assessment_id == assessment_id)
        .order_by(Question.order_index)
    )
    questions = result.scalars().all()
    # Detach from session before nulling out answer fields so the
    # session never sees the mutations and will not commit them to DB.
    for q in questions:
        db.expunge(q)
        make_transient(q)
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
