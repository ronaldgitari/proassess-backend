"""Unit tests for the configurable permission layer (services/permissions.py)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from models import UserRole
from services import permissions as perms


def _user(role, extra=None, denied=None):
    # `id` is needed because the resolver builds `GroupMembership.user_id == user.id`.
    return SimpleNamespace(id=uuid4(), role=role, extra_permissions=extra, denied_permissions=denied)


# ── Catalogue + default group sets ────────────────────────────────
def test_catalog_has_ten_capabilities():
    assert len(perms.PERMISSION_CATALOG) == 10
    assert len(perms.ALL_PERMISSIONS) == 10


def test_ops_has_everything():
    assert perms.OPS == perms.ALL_PERMISSIONS


def test_people_culture_is_ops_minus_system_view():
    assert "system.view" not in perms.PEOPLE_CULTURE
    assert perms.PEOPLE_CULTURE == perms.ALL_PERMISSIONS - {"system.view"}


def test_line_manager_capability_set():
    assert perms.LINE_MANAGER == {
        "kb.view", "assessment.create", "assessment.distribute",
        "assessment.review", "results.team",
    }
    assert "kb.manage" not in perms.LINE_MANAGER   # read-only KB
    assert "results.org" not in perms.LINE_MANAGER  # team-only


# ── Role → default mapping ────────────────────────────────────────
def test_role_default_counts():
    assert len(perms.role_default_permissions(UserRole.SYSTEM_ADMIN)) == 10
    assert len(perms.role_default_permissions(UserRole.HR_ADMIN)) == 9
    assert len(perms.role_default_permissions(UserRole.LINE_MANAGER)) == 5
    assert perms.role_default_permissions(UserRole.STAFF) == set()


def test_role_default_returns_fresh_copy():
    s = perms.role_default_permissions(UserRole.LINE_MANAGER)
    s.add("hacked")
    assert "hacked" not in perms.LINE_MANAGER   # mutating the result must not leak


# ── Effective-permission resolution (groups + overrides) ──────────
def _mock_db(group_perm_lists):
    """A db whose execute(...).scalars().all() returns the given group permission lists."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = group_perm_lists
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


async def test_effective_permissions_union_of_role_and_groups():
    user = _user(UserRole.STAFF)
    db = _mock_db([["kb.view", "results.org"]])   # one group grants these
    eff = await perms.get_effective_permissions(user, db)
    assert "kb.view" in eff and "results.org" in eff   # from group (role STAFF gives none)


async def test_effective_permissions_extra_and_denied_overrides():
    user = _user(UserRole.STAFF, extra=["stats.view"], denied=["results.org"])
    db = _mock_db([["kb.view", "results.org"]])
    eff = await perms.get_effective_permissions(user, db)
    assert "stats.view" in eff             # granted via per-user extra
    assert "kb.view" in eff                # via group
    assert "results.org" not in eff        # denied wins even though the group granted it


async def test_effective_permissions_sorted_list():
    user = _user(UserRole.LINE_MANAGER)
    db = _mock_db([])
    eff = await perms.get_effective_permissions(user, db)
    assert eff == sorted(eff) and isinstance(eff, list)
