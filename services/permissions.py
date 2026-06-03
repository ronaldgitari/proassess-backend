"""
Configurable permission layer (security groups).

A fixed CATALOG of capability keys is gated throughout the app. A user's
*effective* permissions are resolved as:

    role defaults  ∪  (permissions of every SecurityGroup they belong to)
                   ∪  user.extra_permissions
                   −  user.denied_permissions

Role defaults make the system additive and safe: existing users keep working
with no group membership (their role maps to a sensible default set), while
admins can layer on custom groups + per-individual overrides. The three seeded
default groups (Ops / Line Managers / People & Culture) mirror the role defaults.
"""

from __future__ import annotations

from models import UserRole


# ── Capability catalog ───────────────────────────────────────────
PERMISSION_CATALOG: list[tuple[str, str]] = [
    ("stats.view",            "View the Stats dashboard"),
    ("system.view",           "View System Processes (observability)"),
    ("users.manage",          "Manage users & departments (org-wide)"),
    ("kb.view",               "Browse the knowledge base (read-only)"),
    ("kb.manage",             "Upload / reindex / delete knowledge sources"),
    ("assessment.create",     "Create assessments"),
    ("assessment.distribute", "Deploy / share assessments"),
    ("assessment.review",     "View & approve case-study reviews"),
    ("results.team",          "View results for own reports / departments"),
    ("results.org",           "View org-wide per-individual results"),
]
ALL_PERMISSIONS: set[str] = {k for k, _ in PERMISSION_CATALOG}


# ── Default group permission sets ────────────────────────────────
OPS: set[str] = set(ALL_PERMISSIONS)            # top admin — everything
PEOPLE_CULTURE: set[str] = {
    "stats.view", "users.manage", "kb.view", "kb.manage",
    "assessment.create", "assessment.distribute", "assessment.review",
    "results.team", "results.org",
}                                               # like Ops minus system.view
LINE_MANAGER: set[str] = {
    "kb.view", "assessment.create", "assessment.distribute",
    "assessment.review", "results.team",
}
STAFF: set[str] = set()


# Role → default permission set (fallback so existing users work without groups).
ROLE_DEFAULTS: dict = {
    UserRole.SYSTEM_ADMIN: OPS,
    UserRole.HR_ADMIN:     PEOPLE_CULTURE,
    UserRole.LINE_MANAGER: LINE_MANAGER,
    UserRole.STAFF:        STAFF,
}

# Seeded default groups: (slug, display name, permission set).
DEFAULT_GROUPS: list[tuple[str, str, set]] = [
    ("ops",            "Ops",               OPS),
    ("line-managers",  "Line Managers",     LINE_MANAGER),
    ("people-culture", "People & Culture",  PEOPLE_CULTURE),
]


def role_default_permissions(role) -> set[str]:
    return set(ROLE_DEFAULTS.get(role, set()))


async def get_effective_permissions(user, db) -> list[str]:
    """Resolve a user's effective permission keys (sorted)."""
    from sqlalchemy import select
    from models import SecurityGroup, GroupMembership

    perms = role_default_permissions(user.role)
    group_perm_lists = (await db.execute(
        select(SecurityGroup.permissions)
        .join(GroupMembership, GroupMembership.group_id == SecurityGroup.id)
        .where(GroupMembership.user_id == user.id)
    )).scalars().all()
    for pl in group_perm_lists:
        if pl:
            perms |= set(pl)

    perms |= set(getattr(user, "extra_permissions", None) or [])
    perms -= set(getattr(user, "denied_permissions", None) or [])
    return sorted(perms)


async def ensure_default_groups(db, org_id) -> int:
    """Idempotently create the 3 default security groups for an org. Returns #created.
    Does NOT commit — the caller owns the transaction."""
    import uuid as _uuid
    from sqlalchemy import select
    from models import SecurityGroup, GroupType

    existing = {
        g.slug for g in (await db.execute(
            select(SecurityGroup).where(SecurityGroup.org_id == org_id)
        )).scalars().all() if getattr(g, "slug", None)
    }
    created = 0
    for slug, name, perms in DEFAULT_GROUPS:
        if slug in existing:
            continue
        db.add(SecurityGroup(
            id=_uuid.uuid4(), org_id=org_id, name=name, slug=slug,
            group_type=GroupType.MEMBER, permissions=sorted(perms),
            is_system=True, description=f"Default {name} group",
        ))
        created += 1
    return created
