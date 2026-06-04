"""
Seed script — populates a fresh database with one organisation, departments,
security groups, and test users covering every role.

Usage:
    python seed.py

Passwords (bcrypt):  all seeded users use  Password123!
"""

import asyncio
import uuid
from datetime import datetime

import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal, engine, Base
from models import (
    User, Organisation, Department, UserDepartment,
    SecurityGroup, GroupMembership, UserRole, GroupType,
)

PLAIN_PASSWORD = "Password123!"


# ── helpers ─────────────────────────────────────────────────────────

def uid() -> uuid.UUID:
    return uuid.uuid4()


def now() -> datetime:
    return datetime.utcnow()


def hash_pw(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


# ── seed data ────────────────────────────────────────────────────────

async def seed(db: AsyncSession) -> None:
    print("Seeding organisation …")
    org = Organisation(
        id=uid(),
        name="Acme Corp",
        slug="acme-corp",
        created_at=now(),
    )
    db.add(org)
    await db.flush()

    # ── Default security groups (Ops / Line Managers / People & Culture) ──
    from services.permissions import ensure_default_groups
    await ensure_default_groups(db, org.id)

    # ── Departments ────────────────────────────────────────────────
    print("Seeding departments …")
    dept_names = ["Engineering", "Sales", "HR", "Operations", "Finance"]
    departments: dict[str, Department] = {}
    for name in dept_names:
        dept = Department(id=uid(), org_id=org.id, name=name, created_at=now())
        db.add(dept)
        departments[name] = dept
    await db.flush()

    # ── Users ──────────────────────────────────────────────────────
    print("Seeding users …")

    def make_user(email, name, role) -> User:
        return User(
            id=uid(),
            email=email,
            name=name,
            hashed_password=hash_pw(PLAIN_PASSWORD),
            role=role,
            org_id=org.id,
            is_active=True,
            created_at=now(),
            updated_at=now(),
        )

    sys_admin   = make_user("sysadmin@acme.com",  "System Admin",     UserRole.SYSTEM_ADMIN)
    hr_admin    = make_user("hr@acme.com",         "HR Admin",         UserRole.HR_ADMIN)
    lm_eng      = make_user("lm.eng@acme.com",     "Alice (LM Eng)",   UserRole.LINE_MANAGER)
    lm_sales    = make_user("lm.sales@acme.com",   "Bob (LM Sales)",   UserRole.LINE_MANAGER)
    staff_1     = make_user("staff1@acme.com",      "Charlie Staff",    UserRole.STAFF)
    staff_2     = make_user("staff2@acme.com",      "Diana Staff",      UserRole.STAFF)
    staff_3     = make_user("staff3@acme.com",      "Eve Staff",        UserRole.STAFF)
    staff_4     = make_user("staff4@acme.com",      "Frank Staff",      UserRole.STAFF)

    all_users = [sys_admin, hr_admin, lm_eng, lm_sales, staff_1, staff_2, staff_3, staff_4]
    for u in all_users:
        db.add(u)
    await db.flush()

    # ── Department memberships ────────────────────────────────────
    print("Assigning users to departments …")
    memberships = [
        # (user,    dept,          title,                    line_manager)
        (lm_eng,   "Engineering",  "Engineering Manager",    None),
        (lm_sales, "Sales",        "Sales Manager",          None),
        (hr_admin, "HR",           "HR Business Partner",    None),
        (staff_1,  "Engineering",  "Software Engineer",      lm_eng),
        (staff_2,  "Engineering",  "QA Engineer",            lm_eng),
        (staff_3,  "Sales",        "Account Executive",      lm_sales),
        (staff_4,  "Sales",        "Sales Development Rep",  lm_sales),
    ]
    for user, dept_name, title, manager in memberships:
        db.add(UserDepartment(
            user_id=user.id,
            department_id=departments[dept_name].id,
            title=title,
            line_manager_id=manager.id if manager else None,
        ))
    await db.flush()

    # ── Security Groups ───────────────────────────────────────────
    print("Seeding security groups …")

    def make_group(name, gtype, dept=None) -> SecurityGroup:
        return SecurityGroup(
            id=uid(),
            org_id=org.id,
            name=name,
            group_type=gtype,
            department_id=departments[dept].id if dept else None,
            created_at=now(),
        )

    grp_eng_owners  = make_group("owner.engineering",      GroupType.OWNER,        "Engineering")
    grp_eng_members = make_group("member.engineering",     GroupType.MEMBER,       "Engineering")
    grp_sales_owners = make_group("owner.sales",           GroupType.OWNER,        "Sales")
    grp_sales_members = make_group("member.sales",         GroupType.MEMBER,       "Sales")
    grp_hr_owners   = make_group("owner.hr",               GroupType.OWNER,        "HR")
    grp_all_collab  = make_group("collaborator.all-staff", GroupType.COLLABORATOR, None)

    all_groups = [
        grp_eng_owners, grp_eng_members,
        grp_sales_owners, grp_sales_members,
        grp_hr_owners, grp_all_collab,
    ]
    for g in all_groups:
        db.add(g)
    await db.flush()

    # ── Group Memberships ─────────────────────────────────────────
    print("Assigning users to security groups …")
    group_memberships = [
        (lm_eng,   grp_eng_owners,   True),
        (staff_1,  grp_eng_members,  False),
        (staff_2,  grp_eng_members,  False),
        (lm_sales, grp_sales_owners, True),
        (staff_3,  grp_sales_members, False),
        (staff_4,  grp_sales_members, False),
        (hr_admin, grp_hr_owners,    True),
        # all staff are collaborators
        (staff_1,  grp_all_collab,   False),
        (staff_2,  grp_all_collab,   False),
        (staff_3,  grp_all_collab,   False),
        (staff_4,  grp_all_collab,   False),
        (lm_eng,   grp_all_collab,   False),
        (lm_sales, grp_all_collab,   False),
    ]
    for user, group, is_owner in group_memberships:
        db.add(GroupMembership(
            user_id=user.id,
            group_id=group.id,
            is_owner=is_owner,
            joined_at=now(),
        ))

    # ── Capability-group memberships (Ops / Line Managers / People & Culture) ──
    # Mirror each user's role into the matching capability group (created by
    # `ensure_default_groups`) so the security-groups admin UI shows real members
    # and permissions flow through groups — role defaults remain the fallback.
    # Staff map to no capability group.
    print("Assigning users to capability groups …")
    from sqlalchemy import select
    role_to_slug = {
        UserRole.SYSTEM_ADMIN: "ops",
        UserRole.HR_ADMIN:     "people-culture",
        UserRole.LINE_MANAGER: "line-managers",
    }
    cap_groups = {
        g.slug: g for g in (await db.execute(
            select(SecurityGroup).where(
                SecurityGroup.org_id == org.id,
                SecurityGroup.slug.in_(list(role_to_slug.values())),
            )
        )).scalars().all()
    }
    for u in all_users:
        slug = role_to_slug.get(u.role)
        grp = cap_groups.get(slug) if slug else None
        if grp is not None:
            db.add(GroupMembership(user_id=u.id, group_id=grp.id, joined_at=now()))

    await db.commit()
    print("\n✓ Seed complete.\n")
    print("Login credentials (password: Password123!):")
    print(f"  system_admin  → {sys_admin.email}")
    print(f"  hr_admin      → {hr_admin.email}")
    print(f"  line_manager  → {lm_eng.email} / {lm_sales.email}")
    print(f"  staff         → staff1@acme.com … staff4@acme.com")


# ── entry point ──────────────────────────────────────────────────────

async def main() -> None:
    # Optionally create tables if running outside Docker
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        await seed(db)


if __name__ == "__main__":
    asyncio.run(main())
