"""
Security-group administration API (configurable RBAC).

Lets `users.manage` holders (People & Culture / Ops) create & edit security
groups, toggle their permissions, manage memberships, and set per-individual
permission overrides. Everything is org-scoped.
"""
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import SecurityGroup, GroupMembership, User, GroupType
from services.auth_service import require_permission
from services.permissions import PERMISSION_CATALOG, ALL_PERMISSIONS, get_effective_permissions

router = APIRouter(prefix="/groups", tags=["groups"])

MANAGE = require_permission("users.manage")


# ── Schemas ───────────────────────────────────────────────────────
class GroupCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    description: Optional[str] = None
    permissions: List[str] = []


class GroupUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    description: Optional[str] = None
    permissions: Optional[List[str]] = None


class MembersSet(BaseModel):
    user_ids: List[uuid.UUID] = []


class OverridesSet(BaseModel):
    extra_permissions: List[str] = []
    denied_permissions: List[str] = []


def _clean(perms) -> list[str]:
    return sorted({p for p in (perms or []) if p in ALL_PERMISSIONS})


async def _owned_group(group_id: uuid.UUID, current_user: User, db: AsyncSession) -> SecurityGroup:
    g = await db.get(SecurityGroup, group_id)
    if not g or g.org_id != current_user.org_id:
        raise HTTPException(404, "Group not found")
    return g


# ── Permission catalog (for the UI) ───────────────────────────────
@router.get("/catalog")
async def catalog(current_user: User = Depends(MANAGE)):
    return [{"key": k, "label": l} for k, l in PERMISSION_CATALOG]


# ── Org users (member picker + override editor) ───────────────────
@router.get("/users")
async def users_with_permissions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(MANAGE),
):
    users = (await db.execute(
        select(User).where(User.org_id == current_user.org_id).order_by(User.name)
    )).scalars().all()
    # Membership map (explicit query — avoid async lazy-load on User.memberships)
    mem_map: dict = {}
    for uid, gid in (await db.execute(select(GroupMembership.user_id, GroupMembership.group_id))).all():
        mem_map.setdefault(uid, []).append(str(gid))
    out = []
    for u in users:
        out.append({
            "id": str(u.id), "name": u.name, "email": u.email, "role": u.role.value,
            "extra_permissions": u.extra_permissions or [],
            "denied_permissions": u.denied_permissions or [],
            "effective_permissions": await get_effective_permissions(u, db),
            "group_ids": mem_map.get(u.id, []),
        })
    return out


@router.patch("/users/{user_id}/overrides")
async def set_user_overrides(
    user_id: uuid.UUID,
    req: OverridesSet,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(MANAGE),
):
    u = await db.get(User, user_id)
    if not u or u.org_id != current_user.org_id:
        raise HTTPException(404, "User not found")
    u.extra_permissions = _clean(req.extra_permissions) or None
    u.denied_permissions = _clean(req.denied_permissions) or None
    db.add(u)
    await db.flush()
    return {"user_id": str(u.id), "effective_permissions": await get_effective_permissions(u, db)}


# ── Groups CRUD ───────────────────────────────────────────────────
@router.get("")
async def list_groups(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(MANAGE),
):
    groups = (await db.execute(
        select(SecurityGroup).where(
            SecurityGroup.org_id == current_user.org_id,
            SecurityGroup.permissions.isnot(None),   # hide legacy non-permission groups
        )
        .order_by(SecurityGroup.is_system.desc(), SecurityGroup.name)
    )).scalars().all()
    counts = dict((gid, c) for gid, c in (await db.execute(
        select(GroupMembership.group_id, func.count()).group_by(GroupMembership.group_id)
    )).all())
    return [{
        "id": str(g.id), "name": g.name, "slug": g.slug, "is_system": g.is_system,
        "description": g.description, "permissions": g.permissions or [],
        "member_count": counts.get(g.id, 0),
    } for g in groups]


@router.post("")
async def create_group(
    req: GroupCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(MANAGE),
):
    g = SecurityGroup(
        id=uuid.uuid4(), org_id=current_user.org_id, name=req.name,
        slug=None, group_type=GroupType.MEMBER, permissions=_clean(req.permissions),
        is_system=False, description=req.description,
    )
    db.add(g)
    await db.flush()
    return {"id": str(g.id), "name": g.name, "permissions": g.permissions or [], "member_count": 0, "is_system": False}


@router.patch("/{group_id}")
async def update_group(
    group_id: uuid.UUID,
    req: GroupUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(MANAGE),
):
    g = await _owned_group(group_id, current_user, db)
    if req.name is not None:
        g.name = req.name
    if req.description is not None:
        g.description = req.description
    if req.permissions is not None:
        g.permissions = _clean(req.permissions)
    db.add(g)
    await db.flush()
    return {"id": str(g.id), "name": g.name, "permissions": g.permissions or []}


@router.delete("/{group_id}", status_code=204)
async def delete_group(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(MANAGE),
):
    g = await _owned_group(group_id, current_user, db)
    if g.is_system:
        raise HTTPException(400, "Default groups cannot be deleted")
    await db.execute(delete(GroupMembership).where(GroupMembership.group_id == group_id))
    await db.delete(g)
    await db.flush()


# ── Membership ────────────────────────────────────────────────────
@router.get("/{group_id}/members")
async def list_members(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(MANAGE),
):
    await _owned_group(group_id, current_user, db)
    rows = (await db.execute(
        select(User).join(GroupMembership, GroupMembership.user_id == User.id)
        .where(GroupMembership.group_id == group_id).order_by(User.name)
    )).scalars().all()
    return [{"id": str(u.id), "name": u.name, "email": u.email, "role": u.role.value} for u in rows]


@router.put("/{group_id}/members")
async def set_members(
    group_id: uuid.UUID,
    req: MembersSet,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(MANAGE),
):
    await _owned_group(group_id, current_user, db)
    # Only users in the caller's org are accepted.
    valid = set((await db.execute(
        select(User.id).where(User.org_id == current_user.org_id, User.id.in_(req.user_ids or [None]))
    )).scalars().all())
    await db.execute(delete(GroupMembership).where(GroupMembership.group_id == group_id))
    for uid in valid:
        db.add(GroupMembership(user_id=uid, group_id=group_id))
    await db.flush()
    return {"group_id": str(group_id), "member_count": len(valid)}
