import uuid as _uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import aliased

from database import get_db
from timeutil import iso_utc
from models import (
    Assessment, StaffAssessment, StaffAnswer, Question, KnowledgeSource, User,
    UserRole, UserDepartment, Department, AssessmentStatus, StaffAssessmentStatus,
    QuestionType, AuditLog,
)
from schemas import OrgStatsOut
from services.auth_service import require_hr, get_current_user, require_permission

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats", response_model=OrgStatsOut)
async def org_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_hr),
):
    org_id = current_user.org_id

    # Total = every assessment ever created for the org, ALL statuses
    # (drafts/undeployed, deployed, cancelled, completed, archived).
    total_assessments = await db.scalar(
        select(func.count()).where(Assessment.org_id == org_id)
    )
    # Active = currently deployed only (the "Active (deployed) Assessments" tile).
    active_assessments = await db.scalar(
        select(func.count()).where(
            Assessment.org_id == org_id,
            Assessment.status == AssessmentStatus.DEPLOYED,
        )
    )
    total_chunks = await db.scalar(
        select(func.sum(KnowledgeSource.chunk_count)).where(
            KnowledgeSource.org_id == org_id,
            KnowledgeSource.is_active == True,
        )
    )
    sources_count = await db.scalar(
        select(func.count()).where(
            KnowledgeSource.org_id == org_id,
            KnowledgeSource.is_active == True,
        )
    )
    avg_score = await db.scalar(
        select(func.avg(StaffAssessment.score_pct)).where(
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED
        )
    )
    total_assessed = await db.scalar(
        select(func.count(StaffAssessment.user_id.distinct())).where(
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED
        )
    )

    return OrgStatsOut(
        total_assessments=total_assessments or 0,
        active_assessments=active_assessments or 0,
        total_staff_assessed=total_assessed or 0,
        avg_score_pct=round(float(avg_score or 0), 1),
        total_chunks=total_chunks or 0,
        knowledge_sources_count=sources_count or 0,
    )


@router.get("/audit-log")
async def audit_log(
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_hr),
):
    result = await db.execute(
        select(AuditLog, User.name)
        .join(User, AuditLog.user_id == User.id, isouter=True)
        .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())  # stable tiebreaker for pagination
        .offset(skip)
        .limit(limit)
    )
    rows = result.all()

    def _reason(detail):
        return detail.get("reason") if isinstance(detail, dict) else None

    return [
        {
            "id": str(row[0].id),
            "user_name": row[1],
            "action": row[0].action,
            "resource_type": row[0].resource_type,
            "resource_id": str(row[0].resource_id) if row[0].resource_id else None,
            "detail": row[0].detail,
            "reason": _reason(row[0].detail),
            "timestamp": iso_utc(row[0].timestamp),
        }
        for row in rows
    ]


@router.get("/assessment-averages")
async def assessment_averages(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_hr),
):
    """Average score per (scored) assessment for the org, newest first."""
    from models import QuestionType
    rows = (await db.execute(
        select(
            Assessment.id,
            Assessment.name,
            Assessment.created_at,
            func.avg(StaffAssessment.score_pct).label("avg_score"),
            func.count(StaffAssessment.id).label("attempts"),
        )
        .join(StaffAssessment, StaffAssessment.assessment_id == Assessment.id)
        .where(
            Assessment.org_id == current_user.org_id,
            Assessment.question_type != QuestionType.PERSONALITY,   # personality has no score
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED,
        )
        .group_by(Assessment.id, Assessment.name, Assessment.created_at)
        .order_by(Assessment.created_at.desc())
        .limit(limit)
    )).all()
    return [
        {
            "assessment_id": str(r[0]),
            "name": r[1],
            "created_at": iso_utc(r[2]),
            "avg_score": round(float(r[3] or 0), 1),
            "attempts": r[4],
        }
        for r in rows
    ]


@router.get("/department-results")
async def department_results(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permission("results.org")),
):
    """Results arranged by DEPARTMENT (People & Culture view): each department with
    its line manager(s) and members, each member's overall average score, drilling
    into individuals. Replaces the by-assessment arrangement."""
    org_id = current_user.org_id
    LineManager = aliased(User)
    rows = (await db.execute(
        select(UserDepartment, User, Department, LineManager)
        .join(User, UserDepartment.user_id == User.id)
        .join(Department, UserDepartment.department_id == Department.id)
        .outerjoin(LineManager, UserDepartment.line_manager_id == LineManager.id)
        .where(Department.org_id == org_id)
    )).all()

    # Per-user overall average (evaluated, non-personality).
    avg_rows = (await db.execute(
        select(StaffAssessment.user_id, func.avg(StaffAssessment.score_pct), func.count())
        .join(Assessment, StaffAssessment.assessment_id == Assessment.id)
        .where(
            Assessment.org_id == org_id,
            Assessment.question_type != QuestionType.PERSONALITY,
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED,
        )
        .group_by(StaffAssessment.user_id)
    )).all()
    user_avg = {uid: (round(float(a or 0), 1), c) for uid, a, c in avg_rows}

    depts: dict = {}
    for ud, u, d, lm in rows:
        dd = depts.setdefault(d.id, {"id": str(d.id), "name": d.name, "lms": {}, "members": []})
        if lm:
            dd["lms"][str(lm.id)] = lm.name
        avg, cnt = user_avg.get(u.id, (None, 0))
        dd["members"].append({
            "staff_id": str(u.id), "staff_name": u.name, "job_title": ud.title,
            "avg_score": avg, "scored_count": cnt,
        })

    out = []
    for d in depts.values():
        scored = [m["avg_score"] for m in d["members"] if m["avg_score"] is not None]
        out.append({
            "department_id": d["id"],
            "name": d["name"],
            "line_managers": list(d["lms"].values()),
            "member_count": len(d["members"]),
            "avg_score": round(sum(scored) / len(scored), 1) if scored else None,
            "members": sorted(d["members"], key=lambda m: (m["avg_score"] is None, -(m["avg_score"] or 0))),
        })
    out.sort(key=lambda x: x["name"])
    return out


@router.get("/assessment-averages/{assessment_id}/scores")
async def assessment_staff_scores(
    assessment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_hr),
):
    """Per-staff scores for one assessment, highest to lowest."""
    import uuid as _uuid
    assessment = await db.get(Assessment, _uuid.UUID(assessment_id))
    if not assessment or assessment.org_id != current_user.org_id:
        from fastapi import HTTPException
        raise HTTPException(404, "Assessment not found")

    from models import UserDepartment, Department
    from sqlalchemy.orm import aliased

    rows = (await db.execute(
        select(
            User.id, User.name, StaffAssessment.score_pct, StaffAssessment.submitted_at,
            UserDepartment.title, Department.name,
        )
        .join(User, StaffAssessment.user_id == User.id)
        .outerjoin(UserDepartment, UserDepartment.user_id == User.id)
        .outerjoin(Department, UserDepartment.department_id == Department.id)
        .where(
            StaffAssessment.assessment_id == _uuid.UUID(assessment_id),
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED,
        )
        .order_by(StaffAssessment.score_pct.desc())
    )).all()
    return {
        "assessment_id": assessment_id,
        "name": assessment.name,
        "created_at": iso_utc(assessment.created_at),
        "scores": [
            {
                "staff_id": str(r[0]),
                "staff_name": r[1],
                "score_pct": round(float(r[2] or 0), 1),
                "submitted_at": iso_utc(r[3]),
                "job_title": r[4],
                "department": r[5],
            }
            for r in rows
        ],
    }


@router.get("/staff/{staff_id}/profile")
async def staff_profile(
    staff_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Full staff profile for HR/admin or the staff member's line manager:
    profile details (+ personality type/summary), scored results, and a
    strengths/weaknesses skill assessment (per-assessment score bands).
    """
    sid = _uuid.UUID(staff_id)
    staff = (await db.execute(select(User).where(User.id == sid))).scalar_one_or_none()
    if not staff or staff.org_id != current_user.org_id:
        raise HTTPException(404, "Staff member not found")

    # ── Department / title / line manager (single-department model) ──
    LineManager = aliased(User)
    row = (await db.execute(
        select(UserDepartment, Department, LineManager)
        .join(Department, UserDepartment.department_id == Department.id)
        .outerjoin(LineManager, UserDepartment.line_manager_id == LineManager.id)
        .where(UserDepartment.user_id == sid)
        .limit(1)
    )).first()
    department = job_title = line_manager = None
    line_manager_id = None
    if row:
        ud, dept, lm = row
        department, job_title = dept.name, ud.title
        if lm:
            line_manager, line_manager_id = lm.name, lm.id

    # ── Permission: HR/admin OR this staff's line manager ──────────
    is_hr = current_user.role in (UserRole.HR_ADMIN, UserRole.SYSTEM_ADMIN)
    is_their_lm = line_manager_id is not None and line_manager_id == current_user.id
    if not (is_hr or is_their_lm):
        raise HTTPException(403, "Not authorised to view this staff profile")

    # ── Completed evaluated assessments (scored + personality) ─────
    sa_rows = (await db.execute(
        select(StaffAssessment, Assessment)
        .join(Assessment, StaffAssessment.assessment_id == Assessment.id)
        .where(
            StaffAssessment.user_id == sid,
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED,
        )
        .order_by(StaffAssessment.submitted_at.desc())
    )).all()

    # Personality type + summary (most recent personality result)
    from rag.evaluator import compute_personality_result
    personality_type = personality_summary = None
    results = []
    strengths, developing, weaknesses = [], [], []

    for sa, a in sa_rows:
        if a.question_type == QuestionType.PERSONALITY:
            if personality_type is None:
                ans = (await db.execute(
                    select(StaffAnswer, Question)
                    .join(Question, StaffAnswer.question_id == Question.id)
                    .where(StaffAnswer.staff_assessment_id == sa.id)
                )).all()
                scored = [{
                    "dimension": (q.retrieved_chunk_ids or {}).get("dimension"),
                    "direction": (q.retrieved_chunk_ids or {}).get("direction", 1),
                    "answer_index": child.answer_index,
                } for child, q in ans]
                prof = compute_personality_result(scored)
                personality_type = f"{prof['type_code']} · {prof['type_name']}"
                personality_summary = prof.get("description")
            continue

        # Scored assessment → result row + strength/weakness band
        pct = round(float(sa.score_pct or 0), 1)
        entry = {
            "staff_assessment_id": str(sa.id),
            "assessment_id": str(a.id),
            "assessment_name": a.name,
            "assessment_type": a.assessment_type.value,
            "score_pct": pct,
            "questions_correct": sa.questions_correct or 0,
            "questions_total": sa.questions_total or 0,
            "submitted_at": iso_utc(sa.submitted_at),
            "passed": pct >= 70,
        }
        results.append(entry)
        band = {"name": a.name, "score_pct": pct, "submitted_at": entry["submitted_at"]}
        if pct >= 70:
            strengths.append(band)
        elif pct >= 50:
            developing.append(band)
        else:
            weaknesses.append(band)

    scored = [r["score_pct"] for r in results]
    overall_avg = round(sum(scored) / len(scored), 1) if scored else None

    return {
        "staff_id": str(staff.id),
        "profile": {
            "full_name": staff.name,
            "email": staff.email,
            "role": staff.role.value,
            "department": department,
            "job_title": job_title,
            "line_manager": line_manager,
            "start_date": iso_utc(staff.start_date),
            "personality_type": personality_type,
            "personality_summary": personality_summary,
        },
        "results": results,
        "skill_assessment": {
            "overall_avg": overall_avg,
            "scored_count": len(results),
            "strengths": sorted(strengths, key=lambda x: x["score_pct"], reverse=True),
            "developing": sorted(developing, key=lambda x: x["score_pct"], reverse=True),
            "weaknesses": sorted(weaknesses, key=lambda x: x["score_pct"]),
        },
    }


@router.get("/staff-assessment/{staff_assessment_id}/feedback")
async def staff_assessment_feedback(
    staff_assessment_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Per-question feedback for a completed scored assessment, viewable by
    HR/admin or the assessed staff member's line manager. (Personality has
    no per-question feedback — returns is_personality with no answers.)
    """
    said = _uuid.UUID(staff_assessment_id)
    sa = (await db.execute(
        select(StaffAssessment).where(
            StaffAssessment.id == said,
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED,
        )
    )).scalar_one_or_none()
    if not sa:
        raise HTTPException(404, "Feedback not found or not yet evaluated")

    assessed = (await db.execute(select(User).where(User.id == sa.user_id))).scalar_one_or_none()
    if not assessed or assessed.org_id != current_user.org_id:
        raise HTTPException(404, "Not found")

    # Permission: HR/admin OR the assessed user's line manager
    is_hr = current_user.role in (UserRole.HR_ADMIN, UserRole.SYSTEM_ADMIN)
    is_their_lm = False
    if not is_hr:
        lm_id = (await db.execute(
            select(UserDepartment.line_manager_id).where(UserDepartment.user_id == sa.user_id)
        )).scalars().first()
        is_their_lm = lm_id is not None and lm_id == current_user.id
    if not (is_hr or is_their_lm):
        raise HTTPException(403, "Not authorised to view this feedback")

    assessment = await db.get(Assessment, sa.assessment_id)
    rows = (await db.execute(
        select(StaffAnswer, Question)
        .join(Question, StaffAnswer.question_id == Question.id)
        .where(StaffAnswer.staff_assessment_id == said)
        .order_by(Question.order_index)
    )).all()

    if assessment.question_type == QuestionType.PERSONALITY:
        return {"is_personality": True, "assessment_name": assessment.name,
                "score_pct": None, "answers": []}

    answers = []
    for child, q in rows:
        answers.append({
            "question_id": str(q.id),
            "question_text": q.text,
            "question_type": q.question_type.value,
            "options": q.options,
            "given_answer_index": child.answer_index,
            "given_answer_text": child.answer_text,
            "correct_answer_index": q.correct_answer_index,
            "correct_answer_text": q.correct_answer_text,
            "is_correct": child.is_correct,
            "score": child.score,
            "explanation": q.explanation,
            "source_reference": q.source_reference,
            "ai_feedback": child.ai_feedback,
        })
    return {
        "is_personality": False,
        "assessment_name": assessment.name,
        "assessment_type": assessment.assessment_type.value,
        "score_pct": round(float(sa.score_pct or 0), 1),
        "questions_correct": sa.questions_correct or 0,
        "questions_total": sa.questions_total or 0,
        "answers": answers,
    }


@router.get("/completion-by-department")
async def completion_by_department(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_hr),
):
    """Returns average score per department for the admin chart."""
    from models import UserDepartment, Department
    result = await db.execute(
        select(
            Department.name,
            func.avg(StaffAssessment.score_pct).label("avg_score"),
            func.count(StaffAssessment.id).label("count"),
        )
        .join(User, StaffAssessment.user_id == User.id)
        .join(UserDepartment, User.id == UserDepartment.user_id)
        .join(Department, UserDepartment.department_id == Department.id)
        .where(
            Department.org_id == current_user.org_id,
            StaffAssessment.status == StaffAssessmentStatus.EVALUATED,
        )
        .group_by(Department.name)
        .order_by(func.avg(StaffAssessment.score_pct).desc())
    )
    return [
        {"department": row[0], "avg_score": round(float(row[1] or 0), 1), "count": row[2]}
        for row in result.all()
    ]
