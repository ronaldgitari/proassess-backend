"""Dashboard drill-down endpoint: /admin/assessments."""
import uuid
from datetime import datetime

import pytest_asyncio

ADMIN = "/api/v1/admin"


@pytest_asyncio.fixture
async def seeded(db, org):
    """One DEPLOYED assessment created by lm_eng, targeted at staff1 individually."""
    from models import (
        Assessment, AssessmentTarget,
        AssessmentType, QuestionType, InformationSource, TargetType, AssessmentStatus,
    )
    now = datetime.utcnow()
    a = Assessment(
        id=uuid.uuid4(), org_id=org.id, created_by=org.lm_eng.id,
        name="Network Fundamentals", assessment_type=AssessmentType.TECHNICAL,
        question_type=QuestionType.MCQ, topic="TCP/IP",
        information_source=InformationSource.AI_GENERATED, num_questions=5,
        status=AssessmentStatus.DEPLOYED, target_type=TargetType.INDIVIDUALS,
        created_at=now, deployed_at=now,
    )
    db.add(a)
    await db.flush()
    db.add(AssessmentTarget(assessment_id=a.id, target_type=TargetType.INDIVIDUALS, target_id=org.staff1.id))
    await db.commit()
    return {"assessment_id": a.id}


async def test_assessments_lists_with_resolved_fields(client, org, login, seeded):
    r = await client.get(f"{ADMIN}/assessments", headers=await login("hr@t.com"))
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["id"] == str(seeded["assessment_id"]))
    assert row["name"] == "Network Fundamentals"
    assert row["topic"] == "TCP/IP"
    assert row["status"] == "deployed"
    assert row["created_by"] == "LM Eng"
    # Individual target resolves to "name – department"
    assert row["deployed_to"] == "Staff One – Engineering"


async def test_assessments_forbidden_for_staff(client, org, login, seeded):
    r = await client.get(f"{ADMIN}/assessments", headers=await login("staff1@t.com"))
    assert r.status_code == 403
