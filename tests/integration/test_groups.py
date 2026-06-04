"""Security-group admin API: CRUD + membership/overrides changing effective perms."""
GROUPS = "/api/v1/groups"
ME = "/api/v1/auth/me"


async def test_catalog_requires_users_manage(client, org, login):
    assert (await client.get(f"{GROUPS}/catalog", headers=await login("hr@t.com"))).status_code == 200
    # LM lacks users.manage
    assert (await client.get(f"{GROUPS}/catalog", headers=await login("lm.eng@t.com"))).status_code == 403


async def test_catalog_has_ten_capabilities(client, org, login):
    r = await client.get(f"{GROUPS}/catalog", headers=await login("hr@t.com"))
    assert r.status_code == 200, r.text
    keys = {c["key"] for c in r.json()}
    assert len(keys) == 10
    assert "system.view" in keys and "results.org" in keys


async def test_default_groups_seeded_and_protected(client, org, login):
    hr = await login("hr@t.com")
    groups = (await client.get(GROUPS, headers=hr)).json()
    system = [g for g in groups if g["is_system"]]
    assert {g["name"] for g in system} >= {"Ops", "Line Managers", "People & Culture"}

    # A default (system) group cannot be deleted.
    r = await client.delete(f"{GROUPS}/{system[0]['id']}", headers=hr)
    assert r.status_code == 400


async def test_group_membership_grants_permission(client, org, login):
    hr = await login("hr@t.com")

    # staff1 starts with no permissions
    before = (await client.get(ME, headers=await login("staff1@t.com"))).json()["permissions"]
    assert "stats.view" not in before

    # create a custom group with stats.view and add staff1
    grp = (await client.post(GROUPS, headers=hr,
                             json={"name": "Analysts", "permissions": ["stats.view"]})).json()
    put = await client.put(f"{GROUPS}/{grp['id']}/members", headers=hr,
                           json={"user_ids": [str(org.staff1.id)]})
    assert put.status_code == 200, put.text

    # staff1 now has stats.view via the group
    after = (await client.get(ME, headers=await login("staff1@t.com"))).json()["permissions"]
    assert "stats.view" in after


async def test_user_deny_override_revokes_group_permission(client, org, login):
    hr = await login("hr@t.com")
    grp = (await client.post(GROUPS, headers=hr,
                             json={"name": "Analysts", "permissions": ["stats.view"]})).json()
    await client.put(f"{GROUPS}/{grp['id']}/members", headers=hr,
                     json={"user_ids": [str(org.staff1.id)]})

    # deny overrides the group grant
    ov = await client.patch(f"{GROUPS}/users/{org.staff1.id}/overrides", headers=hr,
                            json={"extra_permissions": [], "denied_permissions": ["stats.view"]})
    assert ov.status_code == 200, ov.text
    assert "stats.view" not in ov.json()["effective_permissions"]

    perms = (await client.get(ME, headers=await login("staff1@t.com"))).json()["permissions"]
    assert "stats.view" not in perms


async def test_user_extra_override_grants_permission(client, org, login):
    hr = await login("hr@t.com")
    ov = await client.patch(f"{GROUPS}/users/{org.staff1.id}/overrides", headers=hr,
                            json={"extra_permissions": ["results.org"], "denied_permissions": []})
    assert ov.status_code == 200, ov.text
    assert "results.org" in ov.json()["effective_permissions"]


async def test_lm_cannot_access_groups_admin(client, org, login):
    assert (await client.get(GROUPS, headers=await login("lm.eng@t.com"))).status_code == 403
