"""RBAC integration tests — capability-based guards (security groups) end-to-end."""
import uuid

KB = "/api/v1/knowledge"
ASMT = "/api/v1/assessments"


# ── Knowledge base: kb.view vs kb.manage ──────────────────────────

async def test_lm_can_read_kb(client, org, login):
    """LMs have kb.view (read-only KB) — listing must succeed."""
    r = await client.get(f"{KB}/", headers=await login("lm.eng@t.com"))
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


async def test_staff_cannot_read_kb(client, org, login):
    """Staff have no KB capability at all."""
    r = await client.get(f"{KB}/", headers=await login("staff1@t.com"))
    assert r.status_code == 403


async def test_lm_cannot_mutate_kb(client, org, login):
    """kb.manage is HR/Ops only — an LM deleting a source is forbidden (403 before 404)."""
    r = await client.delete(f"{KB}/{uuid.uuid4()}", headers=await login("lm.eng@t.com"))
    assert r.status_code == 403


async def test_hr_can_reach_kb_mutation(client, org, login):
    """HR has kb.manage, so it passes the guard and reaches the handler (404 = not found,
    NOT 403 = forbidden)."""
    r = await client.delete(f"{KB}/{uuid.uuid4()}", headers=await login("hr@t.com"))
    assert r.status_code == 404


# ── Assessment creation: role + department-charge scoping ─────────

def _create_body(target_type, target_ids):
    return {
        "name": "RBAC probe assessment",
        "assessment_type": "technical",
        "question_type": "mcq",
        "topic": "general knowledge",
        "information_source": "ai",
        "num_questions": 5,
        "time_limit_minutes": 30,
        "target_type": target_type,
        "target_ids": [str(t) for t in target_ids],
    }


async def test_staff_cannot_create_assessment(client, org, login):
    r = await client.post(
        f"{ASMT}/", headers=await login("staff1@t.com"),
        json=_create_body("organisation", [org.id]),
    )
    assert r.status_code == 403


async def test_lm_can_target_own_department(client, org, login):
    """lm_eng line-manages staff1 in Engineering → Engineering is in their charge."""
    r = await client.post(
        f"{ASMT}/", headers=await login("lm.eng@t.com"),
        json=_create_body("department", [org.dept_eng.id]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft"


async def test_lm_cannot_target_foreign_department(client, org, login):
    """lm_eng does NOT line-manage anyone in Sales → 403."""
    r = await client.post(
        f"{ASMT}/", headers=await login("lm.eng@t.com"),
        json=_create_body("department", [org.dept_sales.id]),
    )
    assert r.status_code == 403


async def test_targets_departments_scoped_to_charge_for_lm(client, org, login):
    """The department picker returns only departments an LM line-manages."""
    r = await client.get(f"{ASMT}/targets/departments", headers=await login("lm.eng@t.com"))
    assert r.status_code == 200, r.text
    names = {d["name"] for d in r.json()}
    assert names == {"Engineering"}


async def test_targets_departments_org_wide_for_hr(client, org, login):
    """HR has users.manage → sees all departments org-wide."""
    r = await client.get(f"{ASMT}/targets/departments", headers=await login("hr@t.com"))
    assert r.status_code == 200, r.text
    names = {d["name"] for d in r.json()}
    assert names == {"Engineering", "Sales"}
