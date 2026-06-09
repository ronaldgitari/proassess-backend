"""
Platform Settings — sysadmin management of org-wide config + per-user overrides.

Org-wide settings are stored as JSONB on the Organisation row.
Per-user overrides are stored as JSONB on the User row (only for USER_OVERRIDEABLE keys).

GET  /system/settings              → org defaults + computed effective settings
PATCH /system/settings             → update org-wide settings (system_admin only)
GET  /system/settings/users        → all org users with their per-user overrides
PATCH /system/settings/users/{id}  → set per-user override (system_admin only)
"""

from uuid import UUID
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models.user import User, Organisation
from services.auth_service import get_current_user, require_system_admin, require_permission

router = APIRouter(prefix="/system", tags=["system-settings"])

# ── Default settings ──────────────────────────────────────────────

DEFAULT_SETTINGS: dict = {
    # Session & Security
    "idle_timeout_minutes": 20,
    "session_token_lifetime_minutes": 480,
    # Assessment Rules
    "assessment_pass_mark_pct": 70,
    "max_questions_standard": 30,
    "max_questions_personality": 60,
    "max_questions_scenario": 8,
    # AI & Evaluation
    "enable_self_correcting_eval": True,
    "eval_max_attempts": 3,
    "eval_maker_model": "gpt-4o-mini",
    "eval_checker_model": "gpt-4o",
    # Generation / RAG
    "max_concurrent_generations": 3,
    "generation_timeout_seconds": 600,
    "max_regrade": 2,
}

# Only these keys can be overridden per-user (everything else is org-wide only)
USER_OVERRIDEABLE: set = {"idle_timeout_minutes"}


def effective_settings(org_settings: dict | None, user_settings: dict | None) -> dict:
    """Merge: defaults → org overrides → user overrides (USER_OVERRIDEABLE only)."""
    base = {**DEFAULT_SETTINGS, **(org_settings or {})}
    user_overrides = {k: v for k, v in (user_settings or {}).items() if k in USER_OVERRIDEABLE}
    return {**base, **user_overrides}


# ── Routes ────────────────────────────────────────────────────────

@router.get("/settings")
async def get_org_settings(
    current_user: User = Depends(require_permission("system.view")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return org default settings, any org-level overrides, and the computed effective settings."""
    result = await db.execute(select(Organisation).where(Organisation.id == current_user.org_id))
    org = result.scalar_one_or_none()
    org_overrides = (org.settings or {}) if org else {}
    return {
        "defaults": DEFAULT_SETTINGS,
        "org_overrides": org_overrides,
        "effective": effective_settings(org_overrides, None),
    }


@router.patch("/settings")
async def update_org_settings(
    body: Dict[str, Any],
    current_user: User = Depends(require_system_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Partially update org-wide settings. Only known keys accepted (400 on unknown)."""
    unknown = set(body.keys()) - set(DEFAULT_SETTINGS.keys())
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown setting keys: {sorted(unknown)}")

    result = await db.execute(select(Organisation).where(Organisation.id == current_user.org_id))
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organisation not found")

    current_org_settings = dict(org.settings or {})
    current_org_settings.update(body)
    org.settings = current_org_settings
    db.add(org)
    await db.commit()
    await db.refresh(org)

    org_overrides = org.settings or {}
    return {
        "defaults": DEFAULT_SETTINGS,
        "org_overrides": org_overrides,
        "effective": effective_settings(org_overrides, None),
    }


@router.get("/settings/users")
async def get_user_overrides(
    current_user: User = Depends(require_permission("system.view")),
    db: AsyncSession = Depends(get_db),
) -> list:
    """Return all org users with their per-user setting overrides."""
    result = await db.execute(
        select(User).where(User.org_id == current_user.org_id, User.is_active == True)
        .order_by(User.name)
    )
    users = result.scalars().all()

    org_result = await db.execute(select(Organisation).where(Organisation.id == current_user.org_id))
    org = org_result.scalar_one_or_none()
    org_settings = (org.settings or {}) if org else {}
    effective_idle_org = effective_settings(org_settings, None).get("idle_timeout_minutes", DEFAULT_SETTINGS["idle_timeout_minutes"])

    rows = []
    for u in users:
        raw_overrides = u.settings or {}
        # Expose only USER_OVERRIDEABLE keys
        overrides = {k: v for k, v in raw_overrides.items() if k in USER_OVERRIDEABLE}
        effective_idle = overrides.get("idle_timeout_minutes", effective_idle_org)
        rows.append({
            "id": str(u.id),
            "name": u.name,
            "email": u.email,
            "role": u.role.value,
            "overrides": overrides,
            "effective_idle": effective_idle,
        })
    return rows


@router.patch("/settings/users/{user_id}")
async def update_user_override(
    user_id: UUID,
    body: Dict[str, Any],
    current_user: User = Depends(require_system_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set per-user setting overrides. Only USER_OVERRIDEABLE keys permitted."""
    disallowed = set(body.keys()) - USER_OVERRIDEABLE
    if disallowed:
        raise HTTPException(
            status_code=400,
            detail=f"Keys not overrideable per-user: {sorted(disallowed)}. Allowed: {sorted(USER_OVERRIDEABLE)}",
        )

    result = await db.execute(
        select(User).where(User.id == user_id, User.org_id == current_user.org_id)
    )
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found in this organisation")

    current_user_settings = dict(target.settings or {})
    current_user_settings.update(body)
    target.settings = current_user_settings
    db.add(target)
    await db.commit()
    await db.refresh(target)

    overrides = {k: v for k, v in (target.settings or {}).items() if k in USER_OVERRIDEABLE}

    # Compute effective idle for response
    org_result = await db.execute(select(Organisation).where(Organisation.id == current_user.org_id))
    org = org_result.scalar_one_or_none()
    org_settings = (org.settings or {}) if org else {}
    effective_idle_org = effective_settings(org_settings, None).get("idle_timeout_minutes", DEFAULT_SETTINGS["idle_timeout_minutes"])
    effective_idle = overrides.get("idle_timeout_minutes", effective_idle_org)

    return {
        "id": str(target.id),
        "name": target.name,
        "email": target.email,
        "role": target.role.value,
        "overrides": overrides,
        "effective_idle": effective_idle,
    }
