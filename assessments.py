from uuid import UUID
from typing import List

from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from datetime import datetime, timezone

from database import get_db
from timeutil import iso_utc
from models import Assessment, StaffAssessment, StaffAnswer, Question, User, AssessmentStatus, StaffAssessmentStatus, UserRole
from schemas import (
    AssessmentCreateRequest, AssessmentOut, QuestionOut,
    StartAssessmentRequest, SubmitAssessmentRequest, AssessmentFeedbackOut,
    AssessmentDeployRequest, AssessmentCancelRequest, AssessmentShareRequest,
    ScenarioReviewApproveRequest,
)
from services.auth_service import get_current_user, require_lm, require_staff
from models import StaffAssessmentStatus
from services.assessment_service import (
    create_assessment, deploy_assessment, cancel_assessment,
    start_assessment, submit_assessment, _get_assessment_owned, _audit,
    _generate_questions_background,
)

router = APIRouter(prefix="/assessments", tags=["assessments"])


# ── Line Manager: create draft ───────────────────────────────────

@router.post("/", response_model=AssessmentOut)
async def create(
    req: AssessmentCreateRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Create a draft assessment and trigger RAG question generation."""
    origin_ip = request.client.host if request.client else None
    return await create_assessment(
        req=req, current_user=current_user, db=db,
        background_tasks=background_tasks, origin_ip=origin_ip,
    )


@router.post("/{assessment_id}/deploy", response_model=AssessmentOut)
async def deploy(
    assessment_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    return await deploy_assessment(
        assessment_id=assessment_id, current_user=current_user, db=db
    )


@router.post("/{assessment_id}/cancel", response_model=AssessmentOut)
async def cancel(
    assessment_id: UUID,
    req: AssessmentCancelRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    return await cancel_assessment(
        assessment_id=assessment_id,
        reason=req.reason,
        current_user=current_user,
        db=db,
    )


@router.delete("/{assessment_id}", status_code=204)
async def delete_draft(
    assessment_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Delete a draft or cancelled assessment and all related records."""
    from models import StaffAssessment, StaffAnswer, AssessmentTarget, AuditLog
    from sqlalchemy import delete as sql_delete

    result = await db.execute(
        select(Assessment).where(
            Assessment.id == assessment_id,
            Assessment.created_by == current_user.id,
        )
    )
    assessment = result.scalar_one_or_none()
    if not assessment:
        raise HTTPException(404, "Assessment not found")
    if assessment.status not in (AssessmentStatus.DRAFT, AssessmentStatus.CANCELLED):
        raise HTTPException(400, "Only draft or cancelled assessments can be deleted")

    # Completed attempts (submitted/evaluated) are preserved; incomplete ones are purged.
    completed_count = await db.scalar(
        select(func.count()).where(
            StaffAssessment.assessment_id == assessment_id,
            StaffAssessment.status.in_([StaffAssessmentStatus.SUBMITTED, StaffAssessmentStatus.EVALUATED]),
        )
    )

    # Always remove INCOMPLETE attempts (not_started / in_progress) + their answers,
    # so the assessment disappears from those staff members' dashboards.
    incomplete_ids = (await db.execute(
        select(StaffAssessment.id).where(
            StaffAssessment.assessment_id == assessment_id,
            StaffAssessment.status.in_([StaffAssessmentStatus.NOT_STARTED, StaffAssessmentStatus.IN_PROGRESS]),
        )
    )).scalars().all()
    if incomplete_ids:
        await db.execute(sql_delete(StaffAnswer).where(StaffAnswer.staff_assessment_id.in_(incomplete_ids)))
        await db.execute(sql_delete(StaffAssessment).where(StaffAssessment.id.in_(incomplete_ids)))

    archived = bool(completed_count)
    db.add(AuditLog(
        user_id=current_user.id,
        action="DELETE_ASSESSMENT",
        resource_type="assessment",
        resource_id=assessment.id,
        detail={
            "name": assessment.name,
            "prior_status": assessment.status.value,
            "reason": assessment.cancelled_reason,
            "archived": archived,
            "completed_preserved": completed_count or 0,
        },
    ))

    if archived:
        # Soft-delete: keep questions + completed results/feedback intact, hide from lists
        assessment.is_archived = True
        db.add(assessment)
    else:
        # Hard-delete: nobody completed it — purge everything in FK order
        await db.execute(sql_delete(Question).where(Question.assessment_id == assessment_id))
        await db.execute(sql_delete(AssessmentTarget).where(AssessmentTarget.assessment_id == assessment_id))
        await db.execute(sql_delete(Assessment).where(Assessment.id == assessment_id))


@router.get("/my", response_model=List[AssessmentOut])
async def list_my_assessments(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """LM: list all assessments created by the current user."""
    result = await db.execute(
        select(Assessment)
        .where(
            Assessment.created_by == current_user.id,
            Assessment.is_archived == False,
        )
        .order_by(Assessment.created_at.desc())
    )
    return result.scalars().all()


# ── LM: selectable assessment targets ────────────────────────────

@router.get("/targets/departments")
async def list_target_departments(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Departments for the 'Specific Department' target picker. Org-wide creators
    (People & Culture / Ops — `users.manage`) see all; an LM sees only departments
    they line-manage (their charge)."""
    from models import Department
    from services.auth_service import has_permission
    from services.assessment_service import _charge_department_ids
    rows = (await db.execute(
        select(Department)
        .where(Department.org_id == current_user.org_id)
        .order_by(Department.name)
    )).scalars().all()
    if not await has_permission(current_user, "users.manage", db):
        charge = await _charge_department_ids(current_user.id, db)
        rows = [d for d in rows if str(d.id) in charge]
    return [{"id": str(d.id), "name": d.name} for d in rows]


@router.get("/targets/users")
async def list_target_users(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Active staff + line managers in the caller's org — for the 'Specific Individuals'
    target picker. Line managers are included because they can be assessed too."""
    rows = (await db.execute(
        select(User)
        .where(
            User.org_id == current_user.org_id,
            User.is_active == True,
            User.role.in_([UserRole.STAFF, UserRole.LINE_MANAGER]),
        )
        .order_by(User.name)
    )).scalars().all()
    return [{"id": str(u.id), "name": u.name, "email": u.email} for u in rows]


# ── Post-deployment sharing (creator or HR/system_admin) ──────────

async def _existing_target_ids(assessment_id: UUID, db: AsyncSession) -> set[str]:
    from models import AssessmentTarget
    rows = (await db.execute(
        select(AssessmentTarget.target_id).where(AssessmentTarget.assessment_id == assessment_id)
    )).scalars().all()
    return {str(r) for r in rows}


@router.get("/{assessment_id}/share-candidates")
async def share_candidates(
    assessment_id: UUID,
    target_type: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Departments / staff this assessment is NOT yet targeted at (creator or HR only)."""
    from services.assessment_service import _get_assessment_owned
    from models import Department

    await _get_assessment_owned(assessment_id, current_user, db)  # permission check
    excluded = await _existing_target_ids(assessment_id, db)

    if target_type == "department":
        rows = (await db.execute(
            select(Department).where(Department.org_id == current_user.org_id).order_by(Department.name)
        )).scalars().all()
        return [{"id": str(d.id), "name": d.name} for d in rows if str(d.id) not in excluded]

    rows = (await db.execute(
        select(User).where(
            User.org_id == current_user.org_id, User.is_active == True, User.role == UserRole.STAFF
        ).order_by(User.name)
    )).scalars().all()
    return [{"id": str(u.id), "name": u.name, "email": u.email} for u in rows if str(u.id) not in excluded]


@router.post("/{assessment_id}/share")
async def share_assessment(
    assessment_id: UUID,
    req: AssessmentShareRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Add new department/individual targets to a deployed assessment.

    Returns {shared, duplicates: [{id, name}], skipped}.
    • 409 when every requested target is already assigned.
    • 200 (partial) when some are new and some are duplicates.
    """
    from services.assessment_service import _get_assessment_owned
    from models import AssessmentTarget, AuditLog, Department, TargetType

    assessment = await _get_assessment_owned(assessment_id, current_user, db)
    if assessment.status not in (AssessmentStatus.DEPLOYED, AssessmentStatus.ACTIVE):
        raise HTTPException(400, "Only deployed assessments can be shared")

    excluded = await _existing_target_ids(assessment_id, db)

    # Separate incoming target_ids into new vs. already-assigned
    dup_ids: list[UUID] = []
    new_ids: list[UUID] = []
    for tid in req.target_ids:
        (dup_ids if str(tid) in excluded else new_ids).append(tid)

    # Resolve human-readable names for duplicates so the caller knows exactly
    # who is already assigned without a separate round-trip.
    dup_names: dict[str, str] = {}
    if dup_ids:
        if req.target_type == TargetType.DEPARTMENT:
            rows = (await db.execute(
                select(Department.id, Department.name).where(Department.id.in_(dup_ids))
            )).all()
            dup_names = {str(r.id): r.name for r in rows}
        else:  # INDIVIDUALS
            rows = (await db.execute(
                select(User.id, User.name).where(User.id.in_(dup_ids))
            )).all()
            dup_names = {str(r.id): r.name for r in rows}

    duplicates = [
        {"id": str(did), "name": dup_names.get(str(did), str(did))}
        for did in dup_ids
    ]

    # All selected targets are already assigned → hard reject with 409
    if dup_ids and not new_ids:
        noun = "department" if req.target_type == TargetType.DEPARTMENT else "recipient"
        names_str = ", ".join(d["name"] for d in duplicates[:5])
        suffix = f" and {len(duplicates) - 5} more" if len(duplicates) > 5 else ""
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"All selected {noun}s already have access to this assessment: "
                    f"{names_str}{suffix}."
                ),
                "duplicates": duplicates,
            },
        )

    # Add the genuinely new targets
    for tid in new_ids:
        db.add(AssessmentTarget(
            assessment_id=assessment_id,
            target_type=req.target_type,
            target_id=tid,
        ))

    added = len(new_ids)
    db.add(AuditLog(
        user_id=current_user.id,
        action="SHARE_ASSESSMENT",
        resource_type="assessment",
        resource_id=assessment_id,
        detail={"target_type": req.target_type.value, "count": added, "skipped_duplicates": len(dup_ids)},
    ))
    await db.commit()
    return {"shared": added, "duplicates": duplicates, "skipped": len(dup_ids)}


# ── Staff: recent results ────────────────────────────────────────

@router.get("/staff/my-results")
async def my_results(
    limit: int = 5,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    """Return the most recent evaluated assessments for the logged-in staff member."""
    rows = (await db.execute(
        select(StaffAssessment, Assessment)
        .join(Assessment, StaffAssessment.assessment_id == Assessment.id)
        .where(
            StaffAssessment.user_id == current_user.id,
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED,
        )
        .order_by(StaffAssessment.submitted_at.desc())
        .limit(limit)
    )).all()

    from models import QuestionType
    from rag.evaluator import compute_personality_result

    # Batch-load answers for all personality attempts in ONE query (avoids N+1)
    personality_sa_ids = [sa.id for sa, a in rows if a.question_type == QuestionType.PERSONALITY]
    answers_by_sa: dict = {}
    if personality_sa_ids:
        ans_rows = (await db.execute(
            select(StaffAnswer, Question)
            .join(Question, StaffAnswer.question_id == Question.id)
            .where(StaffAnswer.staff_assessment_id.in_(personality_sa_ids))
        )).all()
        for child, q in ans_rows:
            tags = q.retrieved_chunk_ids or {}
            answers_by_sa.setdefault(child.staff_assessment_id, []).append({
                "dimension": tags.get("dimension"),
                "direction": tags.get("direction", 1),
                "answer_index": child.answer_index,
            })

    out = []
    for sa, a in rows:
        is_personality = a.question_type == QuestionType.PERSONALITY
        personality_type = None

        personality_summary = None
        if is_personality:
            profile = compute_personality_result(answers_by_sa.get(sa.id, []))
            personality_type = f"{profile['type_code']} · {profile['type_name']}"
            personality_summary = profile.get("description")

        out.append({
            "staff_assessment_id": str(sa.id),
            "assessment_id": str(a.id),
            "assessment_name": a.name,
            "assessment_type": a.assessment_type.value,
            "score_pct": round(sa.score_pct or 0, 1),
            "questions_correct": sa.questions_correct or 0,
            "questions_total": sa.questions_total or 0,
            "submitted_at": iso_utc(sa.submitted_at),
            "passed": (sa.score_pct or 0) >= 70,
            "is_personality": is_personality,
            "personality_type": personality_type,
            "personality_summary": personality_summary,
        })
    return out


@router.get("/staff/my-skills")
async def my_skills(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    """
    The logged-in staff member's own strengths/developing/weaknesses bands,
    computed over ALL their scored (non-personality) evaluated assessments.
    Mirrors the `skill_assessment` block of the HR staff-profile endpoint so
    the staff sees the same analysis HR/their LM sees.
    """
    from models import QuestionType
    rows = (await db.execute(
        select(StaffAssessment, Assessment)
        .join(Assessment, StaffAssessment.assessment_id == Assessment.id)
        .where(
            StaffAssessment.user_id == current_user.id,
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED,
            Assessment.question_type != QuestionType.PERSONALITY,
        )
        .order_by(StaffAssessment.submitted_at.desc())
    )).all()

    strengths, developing, weaknesses = [], [], []
    scores = []
    for sa, a in rows:
        pct = round(float(sa.score_pct or 0), 1)
        scores.append(pct)
        band = {"name": a.name, "score_pct": pct, "submitted_at": iso_utc(sa.submitted_at)}
        if pct >= 70:
            strengths.append(band)
        elif pct >= 50:
            developing.append(band)
        else:
            weaknesses.append(band)

    return {
        "overall_avg": round(sum(scores) / len(scores), 1) if scores else None,
        "scored_count": len(scores),
        "strengths": sorted(strengths, key=lambda x: x["score_pct"], reverse=True),
        "developing": sorted(developing, key=lambda x: x["score_pct"], reverse=True),
        "weaknesses": sorted(weaknesses, key=lambda x: x["score_pct"]),
    }


# ── Line Manager: team results (average score by assessment, own reports) ──

@router.get("/lm/team-averages")
async def lm_team_averages(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Average score per (scored) assessment over the LM's direct reports
    (staff whose line manager is the caller). Newest assessment first."""
    from models import QuestionType, UserDepartment
    reports = select(UserDepartment.user_id).where(UserDepartment.line_manager_id == current_user.id)
    rows = (await db.execute(
        select(
            Assessment.id, Assessment.name, Assessment.created_at,
            func.avg(StaffAssessment.score_pct).label("avg_score"),
            func.count(StaffAssessment.id).label("attempts"),
        )
        .join(StaffAssessment, StaffAssessment.assessment_id == Assessment.id)
        .where(
            Assessment.org_id == current_user.org_id,
            Assessment.question_type != QuestionType.PERSONALITY,
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED,
            StaffAssessment.user_id.in_(reports),
        )
        .group_by(Assessment.id, Assessment.name, Assessment.created_at)
        .order_by(Assessment.created_at.desc())
        .limit(limit)
    )).all()
    return [{
        "assessment_id": str(r[0]), "name": r[1], "created_at": iso_utc(r[2]),
        "avg_score": round(float(r[3] or 0), 1), "attempts": r[4],
    } for r in rows]


@router.get("/lm/team-averages/{assessment_id}/scores")
async def lm_team_scores(
    assessment_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Per-staff scores for one assessment, restricted to the LM's direct reports."""
    from models import UserDepartment, Department
    assessment = await db.get(Assessment, assessment_id)
    if not assessment or assessment.org_id != current_user.org_id:
        raise HTTPException(404, "Assessment not found")
    reports = select(UserDepartment.user_id).where(UserDepartment.line_manager_id == current_user.id)
    rows = (await db.execute(
        select(
            User.id, User.name, StaffAssessment.score_pct, StaffAssessment.submitted_at,
            UserDepartment.title, Department.name,
        )
        .join(User, StaffAssessment.user_id == User.id)
        .outerjoin(UserDepartment, UserDepartment.user_id == User.id)
        .outerjoin(Department, UserDepartment.department_id == Department.id)
        .where(
            StaffAssessment.assessment_id == assessment_id,
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED,
            StaffAssessment.user_id.in_(reports),
        )
        .order_by(StaffAssessment.score_pct.desc())
    )).all()
    return {
        "assessment_id": str(assessment_id),
        "name": assessment.name,
        "created_at": iso_utc(assessment.created_at),
        "scores": [{
            "staff_id": str(r[0]), "staff_name": r[1],
            "score_pct": round(float(r[2] or 0), 1), "submitted_at": iso_utc(r[3]),
            "job_title": r[4], "department": r[5],
        } for r in rows],
    }


# ── Staff: list available assessments ────────────────────────────

@router.get("/available", response_model=List[AssessmentOut])
async def list_available(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    """Staff: list deployed assessments this user is TARGETED by (org / dept / individual).
    HR/admins see all deployed (for preview). Mirrors `_verify_user_is_target` so the
    list and the take-time gate agree."""
    from models import AssessmentTarget, TargetType, UserDepartment, UserRole as _Role

    result = await db.execute(
        select(Assessment).where(
            Assessment.org_id == current_user.org_id,
            Assessment.status == AssessmentStatus.DEPLOYED,
            Assessment.is_archived == False,
        ).order_by(Assessment.deployed_at.desc()).limit(50)
    )
    deployed = result.scalars().all()

    # Admins preview everything
    if current_user.role in (_Role.HR_ADMIN, _Role.SYSTEM_ADMIN):
        return deployed[:20]

    # The user's department ids (for department-targeted assessments)
    user_dept_ids = set((await db.execute(
        select(UserDepartment.department_id).where(UserDepartment.user_id == current_user.id)
    )).scalars().all())

    a_ids = [a.id for a in deployed]
    targets_by_assessment: dict = {}
    if a_ids:
        rows = (await db.execute(
            select(AssessmentTarget).where(AssessmentTarget.assessment_id.in_(a_ids))
        )).scalars().all()
        for tt in rows:
            targets_by_assessment.setdefault(tt.assessment_id, []).append(tt)

    def _is_targeted(a) -> bool:
        for tt in targets_by_assessment.get(a.id, []):
            if tt.target_type == TargetType.ORGANISATION:
                return True
            if tt.target_type == TargetType.INDIVIDUALS and tt.target_id == current_user.id:
                return True
            if tt.target_type == TargetType.DEPARTMENT and tt.target_id in user_dept_ids:
                return True
        return False

    return [a for a in deployed if _is_targeted(a)][:20]


# ── Staff: start and submit ───────────────────────────────────────

@router.post("/{assessment_id}/start")
async def start(
    assessment_id: UUID,
    req: StartAssessmentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    """Begin an assessment session. Returns questions (no answers)."""
    req.assessment_id = assessment_id
    session, questions = await start_assessment(
        req=req, current_user=current_user, db=db
    )
    assessment = await db.get(Assessment, assessment_id)
    meta = assessment.rag_metadata or {}
    return {
        "staff_assessment_id": session.id,
        "started_at": session.started_at,
        "time_limit_minutes": assessment.time_limit_minutes,
        "language": meta.get("language"),          # coding assessments
        "scenario": meta.get("scenario"),          # case-study narrative (shared stimulus)
        "questions": [
            QuestionOut.model_validate(q) for q in questions
        ],
    }


@router.post("/submit", response_model=AssessmentFeedbackOut)
async def submit(
    req: SubmitAssessmentRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    """Submit answers. Returns full scored feedback."""
    from services import pipeline_tracker as pt
    pt.set_origin_ip(request.client.host if request.client else None)
    return await submit_assessment(req=req, current_user=current_user, db=db)


@router.get("/{staff_assessment_id}/feedback", response_model=AssessmentFeedbackOut)
async def get_feedback(
    staff_assessment_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    """Retrieve previously evaluated feedback for a staff session."""
    result = await db.execute(
        select(StaffAssessment).where(
            StaffAssessment.id == staff_assessment_id,
            StaffAssessment.user_id == current_user.id,
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED,
        )
    )
    sa = result.scalar_one_or_none()
    if not sa:
        raise HTTPException(404, "Feedback not found or assessment not yet evaluated")

    assessment = await db.get(Assessment, sa.assessment_id)
    from schemas import AnswerFeedback
    from models import StaffAnswer, QuestionType

    answers_q = await db.execute(
        select(StaffAnswer, Question)
        .join(Question, StaffAnswer.question_id == Question.id)
        .where(StaffAnswer.staff_assessment_id == staff_assessment_id)
    )
    rows = answers_q.all()

    # ── Personality: recompute the profile from stored answers ────
    if assessment.question_type == QuestionType.PERSONALITY:
        from rag.evaluator import compute_personality_result
        scored_items = []
        for child, q in rows:
            tags = q.retrieved_chunk_ids or {}
            scored_items.append({
                "dimension": tags.get("dimension"),
                "direction": tags.get("direction", 1),
                "answer_index": child.answer_index,
            })
        profile = compute_personality_result(scored_items)
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

    feedback_answers = []
    for row in rows:
        child, q = row
        feedback_answers.append(AnswerFeedback(
            question_id=q.id,
            question_text=q.text,
            question_type=q.question_type,
            options=q.options,
            given_answer_index=child.answer_index,
            given_answer_text=child.answer_text,
            correct_answer_index=q.correct_answer_index,
            correct_answer_text=q.correct_answer_text,
            is_correct=child.is_correct,
            score=child.score,
            explanation=q.explanation,
            source_reference=q.source_reference,
            ai_feedback=child.ai_feedback,
        ))

    return AssessmentFeedbackOut(
        staff_assessment_id=sa.id,
        assessment_name=assessment.name,
        assessment_type=assessment.assessment_type,
        score_pct=sa.score_pct,
        questions_correct=sa.questions_correct,
        questions_total=sa.questions_total,
        submitted_at=sa.submitted_at,
        answers=feedback_answers,
        scenario=(assessment.rag_metadata or {}).get("scenario"),
    )


# ── Generation status polling ─────────────────────────────────────

@router.get("/{assessment_id}/generation-status")
async def generation_status(
    assessment_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """
    Poll the question generation status for a draft assessment.
    Returns question count and a ready flag.
    """
    result = await db.execute(
        select(Assessment).where(Assessment.id == assessment_id)
    )
    assessment = result.scalar_one_or_none()
    if not assessment:
        raise HTTPException(404, "Assessment not found")

    question_count = await db.scalar(
        select(func.count()).where(Question.assessment_id == assessment_id)
    )

    count = question_count or 0
    ready = count >= assessment.num_questions

    # Detect timeout: a draft with 0 questions persisted after the allowed window = failed.
    # Personality persists all questions in one flush at the END, so `count` stays 0 the
    # whole time — the window must scale with num_questions so a 60-item personality
    # assessment isn't marked failed while still generating.
    # ~15s/item budget: 30 Q → 10 min, 60 Q → 12 min, 93 Q → ~23 min. Floor of 10 min.
    timed_out = False
    if not ready and assessment.status.value == "draft" and assessment.created_at:
        age_seconds = (datetime.utcnow() - assessment.created_at).total_seconds()
        window = max(600, (assessment.num_questions or 0) * 15)
        timed_out = age_seconds > window and count == 0

    # Distinct "insufficient source material" outcome from the grading reflection loop:
    # the grader concluded the selected KB document doesn't cover the topic, so the run
    # stopped honestly rather than generating weak questions. Recorded on rag_metadata.
    gen_error = (assessment.rag_metadata or {}).get("generation_error") or {}
    insufficient = not ready and gen_error.get("kind") == "insufficient_context"

    return {
        "assessment_id": str(assessment_id),
        "status": assessment.status.value,
        "question_count": count,
        "ready": ready,
        "failed": timed_out or insufficient,
        "error_kind": gen_error.get("kind") if insufficient else None,
        "missing": gen_error.get("missing") if insufficient else None,
        "covered": gen_error.get("covered") if insufficient else None,
        "num_questions_requested": assessment.num_questions,
        "age_seconds": int((datetime.utcnow() - assessment.created_at).total_seconds()) if assessment.created_at else 0,
    }


# ── Line Manager: preview generated questions + regenerate (pre-deploy approval) ──

@router.get("/{assessment_id}/questions")
async def preview_questions(
    assessment_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Full generated questions (WITH answers/rubric) for the owning LM to review
    before deploying. Includes the case + web sources for scenario/hybrid."""
    assessment = await _get_assessment_owned(assessment_id, current_user, db)  # 403 if not owner/HR
    qs = (await db.execute(
        select(Question).where(Question.assessment_id == assessment_id).order_by(Question.order_index)
    )).scalars().all()
    meta = assessment.rag_metadata or {}
    return {
        "assessment_id": str(assessment_id),
        "question_type": assessment.question_type.value,
        "scenario": meta.get("scenario"),
        "web_sources": meta.get("web_sources"),
        "questions": [{
            "order_index": q.order_index,
            "text": q.text,
            "question_type": q.question_type.value,
            "options": q.options,
            "correct_answer_index": q.correct_answer_index,
            "correct_answer_text": q.correct_answer_text,
            "explanation": q.explanation,
            "source_reference": q.source_reference,
            "difficulty": q.difficulty,
            "tags": q.retrieved_chunk_ids,   # e.g. personality {dimension,direction} / coding {language}
        } for q in qs],
    }


@router.post("/{assessment_id}/regenerate", response_model=AssessmentOut)
async def regenerate_questions(
    assessment_id: UUID,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Discard the current questions and re-run generation for a DRAFT assessment.
    Lets the LM reject a generated set and try again before deploying."""
    assessment = await _get_assessment_owned(assessment_id, current_user, db)
    if assessment.status != AssessmentStatus.DRAFT:
        raise HTTPException(400, "Only draft assessments can be regenerated")

    # Purge existing questions (+ any incomplete attempts) so generation starts clean.
    sa_ids = (await db.execute(
        select(StaffAssessment.id).where(StaffAssessment.assessment_id == assessment_id)
    )).scalars().all()
    if sa_ids:
        await db.execute(delete(StaffAnswer).where(StaffAnswer.staff_assessment_id.in_(sa_ids)))
        await db.execute(delete(StaffAssessment).where(StaffAssessment.assessment_id == assessment_id))
    await db.execute(delete(Question).where(Question.assessment_id == assessment_id))

    # Clear prior generation artefacts so it regenerates from scratch.
    meta = dict(assessment.rag_metadata or {})
    for k in ("generation_error", "scenario", "web_sources"):
        meta.pop(k, None)
    assessment.rag_metadata = meta
    assessment.created_at = datetime.utcnow()   # reset the generation-status timeout baseline
    db.add(assessment)
    await db.flush()

    origin_ip = request.client.host if request.client else None
    background_tasks.add_task(
        _generate_questions_background, assessment_id=str(assessment_id), origin_ip=origin_ip,
    )
    await _audit(db, current_user.id, "REGENERATE_ASSESSMENT", "assessment", assessment_id)
    return assessment


# ── Line Manager: scenario review queue (human-assisted verification) ──
# Case-study submissions land in PENDING_REVIEW with AI-drafted scores + feedback.
# The owning LM (or HR) reviews, optionally adjusts, and confirms — only then is the
# score finalised (EVALUATED) and surfaced to the candidate + counted in stats.

@router.get("/reviews/pending")
async def list_pending_reviews(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Scenario submissions awaiting this LM's confirmation (HR/admin see all in their org)."""
    q = (
        select(StaffAssessment, Assessment, User)
        .join(Assessment, StaffAssessment.assessment_id == Assessment.id)
        .join(User, StaffAssessment.user_id == User.id)
        .where(
            StaffAssessment.status == StaffAssessmentStatus.PENDING_REVIEW,
            Assessment.org_id == current_user.org_id,
        )
        .order_by(StaffAssessment.submitted_at.desc())
    )
    if current_user.role not in (UserRole.HR_ADMIN, UserRole.SYSTEM_ADMIN):
        q = q.where(Assessment.created_by == current_user.id)
    rows = (await db.execute(q)).all()
    return [{
        "staff_assessment_id": str(sa.id),
        "assessment_id": str(a.id),
        "assessment_name": a.name,
        "assessment_type": a.assessment_type.value,
        "staff_name": u.name,
        "submitted_at": iso_utc(sa.submitted_at),
        "draft_score": sa.score_pct,
        "questions_total": sa.questions_total,
    } for sa, a, u in rows]


@router.get("/reviews/{staff_assessment_id}")
async def get_review_detail(
    staff_assessment_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Full review payload: the case + each question with the staff answer, AI draft
    score, AI feedback, and the credible sources behind it."""
    sa = (await db.execute(
        select(StaffAssessment).where(StaffAssessment.id == staff_assessment_id)
    )).scalar_one_or_none()
    if not sa:
        raise HTTPException(404, "Submission not found")
    assessment = await _get_assessment_owned(sa.assessment_id, current_user, db)  # 403 if not owner/HR
    staff = await db.get(User, sa.user_id)

    questions = (await db.execute(
        select(Question).where(Question.assessment_id == sa.assessment_id).order_by(Question.order_index)
    )).scalars().all()
    answers = (await db.execute(
        select(StaffAnswer).where(StaffAnswer.staff_assessment_id == sa.id)
    )).scalars().all()
    amap = {str(a.question_id): a for a in answers}

    items = []
    for q in questions:
        ans = amap.get(str(q.id))
        items.append({
            "question_id": str(q.id),
            "question_text": q.text,
            "rubric": q.explanation,
            "model_answer": q.correct_answer_text,
            "staff_answer": ans.answer_text if ans else None,
            "draft_score": ans.score if ans else None,
            "ai_feedback": ans.ai_feedback if ans else None,
            "sources": ans.feedback_sources if ans else None,
        })

    return {
        "staff_assessment_id": str(sa.id),
        "assessment_id": str(assessment.id),
        "assessment_name": assessment.name,
        "assessment_type": assessment.assessment_type.value,
        "staff_name": staff.name if staff else None,
        "status": sa.status.value,
        "submitted_at": iso_utc(sa.submitted_at),
        "draft_score": sa.score_pct,
        "case": (assessment.rag_metadata or {}).get("scenario"),
        "answers": items,
    }


@router.post("/reviews/{staff_assessment_id}/approve", response_model=AssessmentFeedbackOut)
async def approve_review(
    staff_assessment_id: UUID,
    req: ScenarioReviewApproveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_lm),
):
    """Confirm a scenario result (with optional per-answer score/feedback edits). This is
    the human-assisted verification step — the score is created only on approval."""
    sa = (await db.execute(
        select(StaffAssessment).where(StaffAssessment.id == staff_assessment_id)
    )).scalar_one_or_none()
    if not sa:
        raise HTTPException(404, "Submission not found")
    if sa.status != StaffAssessmentStatus.PENDING_REVIEW:
        raise HTTPException(400, "This submission is not pending review")
    assessment = await _get_assessment_owned(sa.assessment_id, current_user, db)  # 403 if not owner/HR

    answers = (await db.execute(
        select(StaffAnswer).where(StaffAnswer.staff_assessment_id == sa.id)
    )).scalars().all()
    overrides = {str(o.question_id): o for o in req.answers}

    scores: list[float] = []
    for child in answers:
        o = overrides.get(str(child.question_id))
        if o:
            if o.score is not None:
                child.score = o.score
            if o.feedback is not None:
                child.ai_feedback = o.feedback
            db.add(child)
        scores.append(float(child.score or 0.0))

    final = round(sum(scores) / len(scores), 1) if scores else 0.0
    sa.score_pct = final
    sa.status = StaffAssessmentStatus.EVALUATED
    sa.evaluated_at = datetime.utcnow()
    sa.reviewed_by_id = current_user.id
    sa.reviewed_at = datetime.utcnow()
    db.add(sa)
    await db.flush()

    await _audit(db, current_user.id, "REVIEW_ASSESSMENT", "staff_assessment", sa.id,
                 detail={"final_score": final, "note": req.note,
                         "adjustments": len(overrides)})

    return AssessmentFeedbackOut(
        staff_assessment_id=sa.id,
        assessment_name=assessment.name,
        assessment_type=assessment.assessment_type,
        score_pct=final,
        questions_correct=None,
        questions_total=sa.questions_total or len(answers),
        submitted_at=sa.submitted_at,
        answers=[],
        pending_review=False,
    )
