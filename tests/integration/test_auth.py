"""Auth + /auth/me effective-permission integration tests."""
import pytest

from .conftest import PASSWORD

API = "/api/v1/auth"


async def test_login_success_returns_token_pair(client, org):
    r = await client.post(f"{API}/login", json={"email": "hr@t.com", "password": PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"].lower() == "bearer"


async def test_login_bad_password_401(client, org):
    r = await client.post(f"{API}/login", json={"email": "hr@t.com", "password": "wrong"})
    assert r.status_code == 401


async def test_login_unknown_user_401(client, org):
    r = await client.post(f"{API}/login", json={"email": "nobody@t.com", "password": PASSWORD})
    assert r.status_code == 401


async def test_login_inactive_user_403(client, org, db):
    org.staff1.is_active = False
    db.add(org.staff1)
    await db.commit()
    r = await client.post(f"{API}/login", json={"email": "staff1@t.com", "password": PASSWORD})
    assert r.status_code == 403


async def test_me_requires_token(client, org):
    r = await client.get(f"{API}/me")
    assert r.status_code in (401, 403)   # HTTPBearer → 403 when header absent


@pytest.mark.parametrize(
    "email,expected_perm_count",
    [
        ("sysadmin@t.com", 10),
        ("hr@t.com", 9),
        ("lm.eng@t.com", 5),
        ("staff1@t.com", 0),
    ],
)
async def test_me_effective_permissions_by_role(client, org, login, email, expected_perm_count):
    headers = await login(email)
    r = await client.get(f"{API}/me", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == email
    assert len(body["permissions"]) == expected_perm_count
    # full_name is populated from name
    assert body["full_name"] == body["name"]


async def test_me_line_manager_permission_keys(client, org, login):
    headers = await login("lm.eng@t.com")
    perms = set((await client.get(f"{API}/me", headers=headers)).json()["permissions"])
    assert perms == {
        "kb.view", "assessment.create", "assessment.distribute",
        "assessment.review", "results.team",
    }


async def test_refresh_issues_new_access_token(client, org):
    login_resp = (await client.post(f"{API}/login", json={"email": "hr@t.com", "password": PASSWORD})).json()
    r = await client.post(f"{API}/refresh", params={"refresh_token": login_resp["refresh_token"]})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


async def test_refresh_rejects_access_token(client, org):
    login_resp = (await client.post(f"{API}/login", json={"email": "hr@t.com", "password": PASSWORD})).json()
    # Passing the *access* token where a refresh token is expected must fail.
    r = await client.post(f"{API}/refresh", params={"refresh_token": login_resp["access_token"]})
    assert r.status_code == 400


async def test_change_password_then_login_with_new(client, org, login):
    headers = await login("staff1@t.com")
    r = await client.post(
        f"{API}/change-password",
        headers=headers,
        json={"current_password": PASSWORD, "new_password": "BrandNew123!"},
    )
    assert r.status_code == 200, r.text
    # old password no longer works; new one does
    assert (await client.post(f"{API}/login", json={"email": "staff1@t.com", "password": PASSWORD})).status_code == 401
    assert (await client.post(f"{API}/login", json={"email": "staff1@t.com", "password": "BrandNew123!"})).status_code == 200


async def test_change_password_wrong_current_400(client, org, login):
    headers = await login("staff1@t.com")
    r = await client.post(
        f"{API}/change-password",
        headers=headers,
        json={"current_password": "nope", "new_password": "BrandNew123!"},
    )
    assert r.status_code == 400
