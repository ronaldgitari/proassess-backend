"""Case-study (scenario) human-assisted review: PENDING_REVIEW → approve → EVALUATED.

The AI feedback draft is produced at submit time by `generate_scenario_feedback`
(GPT + web). Rather than mock that, we seed an already-drafted PENDING_REVIEW
attempt directly, then exercise the review queue + approval endpoints, which are
the human-verification step that actually finalises the score.
"""
import uuid
from datetime import datetime

import pytest_asyncio

ASMT = "/api/v1/assessments"


@pytest_asyncio.fixture
async def pending_scenario(db, org):
    """Seed a deployed SCENARIO assessment owned by lm_eng with one staff1 attempt
    parked in PENDING_REVIEW (two answers, AI draft scores 40 and 60)."""
    from models import (
        Assessment, Question, StaffAssessment, StaffAnswer,
        AssessmentType, QuestionType, InformationSource, TargetType,
        AssessmentStatus, StaffAssessmentStatus,
    )
    now = datetime.utcnow()
    a = Assessment(
        id=uuid.uuid4(), org_id=org.id, created_by=org.lm_eng.id,
        name="Incident response case study", assessment_type=AssessmentType.TECHNICAL,
        question_type=QuestionType.SCENARIO, topic="incident response",
        information_source=InformationSource.KNOWLEDGE_BASE, num_questions=2,
        status=AssessmentStatus.DEPLOYED, target_type=TargetType.ORGANISATION,
        rag_metadata={"scenario": "A production outage at 02:00 ..."}, created_at=now,
        deployed_at=now,
    )
    db.add(a)
    await db.flush()

    q1 = Question(id=uuid.uuid4(), assessment_id=a.id, order_index=0,
                  text="Triage step?", question_type=QuestionType.SCENARIO,
                  correct_answer_text="Declare incident, page on-call.",
                  explanation="Rubric: prioritise containment.", difficulty=3)
    q2 = Question(id=uuid.uuid4(), assessment_id=a.id, order_index=1,
                  text="Comms plan?", question_type=QuestionType.SCENARIO,
                  correct_answer_text="Status page + stakeholder updates.",
                  explanation="Rubric: clear cadence.", difficulty=3)
    db.add_all([q1, q2])

    sa = StaffAssessment(
        id=uuid.uuid4(), assessment_id=a.id, user_id=org.staff1.id,
        status=StaffAssessmentStatus.PENDING_REVIEW, started_at=now, submitted_at=now,
        score_pct=50.0, questions_correct=None, questions_total=2,
    )
    db.add(sa)
    await db.flush()

    db.add_all([
        StaffAnswer(id=uuid.uuid4(), staff_assessment_id=sa.id, question_id=q1.id,
                    answer_text="I would page on-call.", score=40.0, ai_feedback="Partial."),
        StaffAnswer(id=uuid.uuid4(), staff_assessment_id=sa.id, question_id=q2.id,
                    answer_text="Post on status page.", score=60.0, ai_feedback="Good start."),
    ])
    await db.commit()
    return {"assessment_id": a.id, "sa_id": sa.id, "q1": q1.id, "q2": q2.id}


async def test_pending_review_visible_to_creator(client, org, login, pending_scenario):
    r = await client.get(f"{ASMT}/reviews/pending", headers=await login("lm.eng@t.com"))
    assert r.status_code == 200, r.text
    ids = {row["staff_assessment_id"] for row in r.json()}
    assert str(pending_scenario["sa_id"]) in ids


async def test_pending_review_hidden_from_other_lm(client, org, login, pending_scenario):
    """lm_sales did not create it and isn't HR → it must not appear in their queue."""
    r = await client.get(f"{ASMT}/reviews/pending", headers=await login("lm.sales@t.com"))
    assert r.status_code == 200, r.text
    ids = {row["staff_assessment_id"] for row in r.json()}
    assert str(pending_scenario["sa_id"]) not in ids


async def test_review_detail_includes_case_and_answers(client, org, login, pending_scenario):
    r = await client.get(f"{ASMT}/reviews/{pending_scenario['sa_id']}",
                         headers=await login("lm.eng@t.com"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["case"].startswith("A production outage")
    assert len(body["answers"]) == 2
    assert body["status"] == "pending_review"


async def test_staff_feedback_blocked_until_approved(client, org, login, pending_scenario):
    """Feedback endpoint only serves EVALUATED attempts — pending review must 404."""
    r = await client.get(f"{ASMT}/{pending_scenario['sa_id']}/feedback",
                         headers=await login("staff1@t.com"))
    assert r.status_code == 404


async def test_approve_finalises_score_and_evaluates(client, org, login, db, pending_scenario):
    from sqlalchemy import select
    from models import StaffAssessment, StaffAssessmentStatus

    sa_id = pending_scenario["sa_id"]
    # Approve, overriding q1's score to 80 → final = (80 + 60) / 2 = 70
    r = await client.post(
        f"{ASMT}/reviews/{sa_id}/approve", headers=await login("lm.eng@t.com"),
        json={"answers": [{"question_id": str(pending_scenario["q1"]), "score": 80}],
              "note": "Looks solid after adjustment."},
    )
    assert r.status_code == 200, r.text
    assert r.json()["score_pct"] == 70.0

    # Persisted: status EVALUATED, reviewer recorded
    sa = (await db.execute(select(StaffAssessment).where(StaffAssessment.id == sa_id))).scalar_one()
    await db.refresh(sa)
    assert sa.status == StaffAssessmentStatus.EVALUATED
    assert sa.score_pct == 70.0
    assert sa.reviewed_by_id == org.lm_eng.id

    # Now the candidate can see their feedback
    fb = await client.get(f"{ASMT}/{sa_id}/feedback", headers=await login("staff1@t.com"))
    assert fb.status_code == 200, fb.text


async def test_approve_rejects_non_pending(client, org, login, db, pending_scenario):
    """Approving twice (or any non-pending attempt) is a 400."""
    sa_id = pending_scenario["sa_id"]
    lm = await login("lm.eng@t.com")
    first = await client.post(f"{ASMT}/reviews/{sa_id}/approve", headers=lm, json={"answers": []})
    assert first.status_code == 200, first.text
    second = await client.post(f"{ASMT}/reviews/{sa_id}/approve", headers=lm, json={"answers": []})
    assert second.status_code == 400
