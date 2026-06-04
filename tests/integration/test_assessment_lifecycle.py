"""End-to-end assessment lifecycle: create → deploy → start → submit → feedback.

Question *generation* is stubbed (see conftest `_stub_generation`); we insert
questions directly via `add_questions` to simulate the RAG pipeline completing,
then drive the real lifecycle endpoints. MCQ scoring is deterministic (no AI).
"""
import uuid

from .conftest import add_questions

ASMT = "/api/v1/assessments"


def _create_body(org_id, num_questions=5):
    return {
        "name": "Lifecycle assessment",
        "assessment_type": "technical",
        "question_type": "mcq",
        "topic": "general knowledge",
        "information_source": "ai",
        "num_questions": num_questions,
        "time_limit_minutes": 30,
        "target_type": "organisation",
        "target_ids": [str(org_id)],
    }


async def _create_draft(client, login, org, num_questions=5):
    r = await client.post(f"{ASMT}/", headers=await login("lm.eng@t.com"),
                          json=_create_body(org.id, num_questions))
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def test_full_mcq_lifecycle(client, org, login, db):
    lm = await login("lm.eng@t.com")
    staff = await login("staff1@t.com")

    # 1. create draft
    aid = await _create_draft(client, login, org, num_questions=5)

    # 2. generation completes (simulated) → questions exist, status flips to ready
    await add_questions(db, uuid.UUID(aid), n=5, correct_index=0)
    gs = await client.get(f"{ASMT}/{aid}/generation-status", headers=lm)
    assert gs.status_code == 200, gs.text
    assert gs.json()["ready"] is True
    assert gs.json()["question_count"] == 5

    # 3. deploy
    dep = await client.post(f"{ASMT}/{aid}/deploy", headers=lm)
    assert dep.status_code == 200, dep.text
    assert dep.json()["status"] == "deployed"

    # 4. it shows in the staff's available list
    avail = await client.get(f"{ASMT}/available", headers=staff)
    assert avail.status_code == 200
    assert aid in {a["id"] for a in avail.json()}

    # 5. start — questions returned WITHOUT correct answers
    start = await client.post(f"{ASMT}/{aid}/start", headers=staff, json={"assessment_id": aid})
    assert start.status_code == 200, start.text
    body = start.json()
    sa_id = body["staff_assessment_id"]
    questions = body["questions"]
    assert len(questions) == 5
    assert all("correct_answer_index" not in q for q in questions)

    # 6. submit — answer every question with index 0 (the correct one)
    answers = [{"question_id": q["id"], "answer_index": 0} for q in questions]
    sub = await client.post(f"{ASMT}/submit", headers=staff,
                            json={"staff_assessment_id": sa_id, "answers": answers})
    assert sub.status_code == 200, sub.text
    fb = sub.json()
    assert fb["score_pct"] == 100.0
    assert fb["questions_correct"] == 5
    assert len(fb["answers"]) == 5

    # 7. feedback is retrievable afterwards
    got = await client.get(f"{ASMT}/{sa_id}/feedback", headers=staff)
    assert got.status_code == 200, got.text
    assert got.json()["score_pct"] == 100.0


async def test_partial_score(client, org, login, db):
    """Wrong answers count toward the denominator (unanswered/incorrect = 0)."""
    lm = await login("lm.eng@t.com")
    staff = await login("staff1@t.com")
    aid = await _create_draft(client, login, org, num_questions=5)
    await add_questions(db, uuid.UUID(aid), n=5, correct_index=0)
    await client.post(f"{ASMT}/{aid}/deploy", headers=lm)

    start = (await client.post(f"{ASMT}/{aid}/start", headers=staff,
                               json={"assessment_id": aid})).json()
    qs = start["questions"]
    # 3 correct (index 0), 2 wrong (index 1) → 60%
    answers = []
    for i, q in enumerate(qs):
        answers.append({"question_id": q["id"], "answer_index": 0 if i < 3 else 1})
    sub = await client.post(f"{ASMT}/submit", headers=staff,
                            json={"staff_assessment_id": start["staff_assessment_id"], "answers": answers})
    assert sub.status_code == 200, sub.text
    assert sub.json()["score_pct"] == 60.0


async def test_deploy_without_questions_rejected(client, org, login):
    lm = await login("lm.eng@t.com")
    aid = await _create_draft(client, login, org)
    r = await client.post(f"{ASMT}/{aid}/deploy", headers=lm)
    assert r.status_code == 400   # generation hasn't produced questions


async def test_start_requires_targeting(client, org, login, db):
    """A staff member NOT covered by any target gets 403 at take-time.
    Here the assessment targets only staff1 (individuals); staff2 is excluded."""
    lm = await login("lm.eng@t.com")
    body = _create_body(org.id)
    body["target_type"] = "individuals"
    body["target_ids"] = [str(org.staff1.id)]
    aid = (await client.post(f"{ASMT}/", headers=lm, json=body)).json()["id"]
    await add_questions(db, uuid.UUID(aid), n=5)
    await client.post(f"{ASMT}/{aid}/deploy", headers=lm)

    # staff2 is not targeted → 403
    r = await client.post(f"{ASMT}/{aid}/start", headers=await login("staff2@t.com"),
                          json={"assessment_id": aid})
    assert r.status_code == 403
    # staff1 is targeted → 200
    ok = await client.post(f"{ASMT}/{aid}/start", headers=await login("staff1@t.com"),
                           json={"assessment_id": aid})
    assert ok.status_code == 200, ok.text


async def test_generation_status_insufficient_context(client, org, login, db):
    """The grading-loop honest-failure path surfaces a distinct error_kind."""
    from sqlalchemy import select
    from models import Assessment

    lm = await login("lm.eng@t.com")
    aid = await _create_draft(client, login, org, num_questions=5)

    # Simulate _record_generation_error writing the grader verdict onto rag_metadata.
    a = (await db.execute(select(Assessment).where(Assessment.id == uuid.UUID(aid)))).scalar_one()
    meta = dict(a.rag_metadata or {})
    meta["generation_error"] = {
        "kind": "insufficient_context",
        "missing": ["advanced topic X"],
        "covered": ["intro"],
    }
    a.rag_metadata = meta
    db.add(a)
    await db.commit()

    gs = (await client.get(f"{ASMT}/{aid}/generation-status", headers=lm)).json()
    assert gs["ready"] is False
    assert gs["failed"] is True
    assert gs["error_kind"] == "insufficient_context"
    assert gs["missing"] == ["advanced topic X"]
