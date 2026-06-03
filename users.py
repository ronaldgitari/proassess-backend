"""
HR user management — create / list / update users, assign to departments,
deactivate (no hard delete), and reset passwords (temp + forced change).
All routes require HR Admin or System Admin.
"""
import secrets
import uuid
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete as sql_delete
from sqlalchemy.orm import aliased

from database import get_db
from models import User, UserRole, Department, UserDepartment, AuditLog
from schemas import (
    AdminUserOut, UserCreateRequest, UserUpdateRequest,
    PasswordResetOut, DepartmentCreateRequest, DepartmentOut,
)
from services.auth_service import require_hr, hash_password

router = APIRouter(prefix="/users", tags=["users"])


async def _user_out(u: User, db: AsyncSession) -> AdminUserOut:
    """Assemble an AdminUserOut with the user's (single) department + line manager."""
    LineManager = aliased(User)
    row = (await db.execute(
        select(UserDepartment, Department, LineManager)
        .join(Department, UserDepartment.department_id == Department.id)
        .outerjoin(LineManager, UserDepartment.line_manager_id == LineManager.id)
        .where(UserDepartment.user_id == u.id)
        .limit(1)
    )).first()

    dept_id = dept = title = lm_id = lm = None
    if row:
        ud, d, manager = row
        dept_id, dept, title = d.id, d.name, ud.title
        if manager:
            lm_id, lm = manager.id, manager.name

    return AdminUserOut(
        id=u.id, email=u.email, name=u.name, role=u.role, is_active=u.is_active,
        start_date=u.start_date, department_id=dept_id, department=dept,
        job_title=title, line_manager_id=lm_id, line_manager=lm, created_at=u.created_at,
    )


def _guard_role_assignment(target_role: UserRole, current_user: User):
    """Only a system_admin may grant the system_admin role."""
    if target_role == UserRole.SYSTEM_ADMIN and current_user.role != UserRole.SYSTEM_ADMIN:
        raise HTTPException(403, "Only a system admin can assign the system_admin role")


async def _validate_org_refs(department_id, line_manager_id, org_id, db: AsyncSession):
    """Ensure any referenced department / line manager belongs to the caller's org
    (prevents cross-tenant references)."""
    if department_id:
        d = (await db.execute(
            select(Department).where(Department.id == department_id, Department.org_id == org_id)
        )).scalar_one_or_none()
        if not d:
            raise HTTPException(400, "Department not found in your organisation")
    if line_manager_id:
        m = (await db.execute(
            select(User).where(User.id == line_manager_id, User.org_id == org_id)
        )).scalar_one_or_none()
        if not m:
            raise HTTPException(400, "Line manager not found in your organisation")


async def _set_department(user_id, department_id, job_title, line_manager_id, db: AsyncSession):
    """Replace the user's department membership (single-department model)."""
    await db.execute(sql_delete(UserDepartment).where(UserDepartment.user_id == user_id))
    if department_id:
        db.add(UserDepartment(
            user_id=user_id, department_id=department_id,
            title=job_title, line_manager_id=line_manager_id,
        ))


# ── Departments ───────────────────────────────────────────────────

@router.get("/departments", response_model=List[DepartmentOut])
async def list_departments(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_hr),
):
    rows = (await db.execute(
        select(Department).where(Department.org_id == current_user.org_id).order_by(Department.name)
    )).scalars().all()
    return [DepartmentOut(id=d.id, name=d.name) for d in rows]


@router.post("/departments", response_model=DepartmentOut)
async def create_department(
    req: DepartmentCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_hr),
):
    dept = Department(id=uuid.uuid4(), org_id=current_user.org_id, name=req.name)
    db.add(dept)
    await db.flush()
    db.add(AuditLog(user_id=current_user.id, action="CREATE_DEPARTMENT",
                    resource_type="department", resource_id=dept.id, detail={"name": req.name}))
    return DepartmentOut(id=dept.id, name=dept.name)


# ── Users ─────────────────────────────────────────────────────────

@router.get("/", response_model=List[AdminUserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_hr),
):
    # Single query (left joins) instead of N per-user lookups
    LineManager = aliased(User)
    rows = (await db.execute(
        select(User, Department, UserDepartment, LineManager)
        .outerjoin(UserDepartment, UserDepartment.user_id == User.id)
        .outerjoin(Department, UserDepartment.department_id == Department.id)
        .outerjoin(LineManager, UserDepartment.line_manager_id == LineManager.id)
        .where(User.org_id == current_user.org_id)
        .order_by(User.name)
    )).all()

    out: list[AdminUserOut] = []
    for u, dept, ud, manager in rows:
        out.append(AdminUserOut(
            id=u.id, email=u.email, name=u.name, role=u.role, is_active=u.is_active,
            start_date=u.start_date,
            department_id=dept.id if dept else None,
            department=dept.name if dept else None,
            job_title=ud.title if ud else None,
            line_manager_id=manager.id if manager else None,
            line_manager=manager.name if manager else None,
            created_at=u.created_at,
        ))
    return out


@router.post("/", response_model=AdminUserOut)
async def create_user(
    req: UserCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_hr),
):
    _guard_role_assignment(req.role, current_user)

    existing = (await db.execute(select(User).where(User.email == req.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "A user with that email already exists")

    await _validate_org_refs(req.department_id, req.line_manager_id, current_user.org_id, db)

    user = User(
        id=uuid.uuid4(),
        email=req.email,
        name=req.name,
        hashed_password=hash_password(req.password),
        role=req.role,
        org_id=current_user.org_id,
        is_active=True,
        start_date=req.start_date,
        force_password_change=True,   # new accounts must set their own password
    )
    db.add(user)
    await db.flush()

    await _set_department(user.id, req.department_id, req.job_title, req.line_manager_id, db)

    db.add(AuditLog(user_id=current_user.id, action="CREATE_USER",
                    resource_type="user", resource_id=user.id,
                    detail={"email": req.email, "role": req.role.value}))
    await db.flush()
    return await _user_out(user, db)


@router.patch("/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: uuid.UUID,
    req: UserUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_hr),
):
    user = (await db.execute(
        select(User).where(User.id == user_id, User.org_id == current_user.org_id)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    # Self-lockout guards
    if user.id == current_user.id:
        if req.is_active is False:
            raise HTTPException(400, "You cannot deactivate your own account")
        if req.role is not None and req.role != current_user.role:
            raise HTTPException(400, "You cannot change your own role")

    if req.role is not None:
        _guard_role_assignment(req.role, current_user)
        user.role = req.role
    if req.name is not None:
        user.name = req.name
    if req.is_active is not None:
        user.is_active = req.is_active
    if req.start_date is not None:
        user.start_date = req.start_date

    # Department membership update (only when department_id provided in payload)
    if req.department_id is not None:
        await _validate_org_refs(req.department_id, req.line_manager_id, current_user.org_id, db)
        await _set_department(user.id, req.department_id, req.job_title, req.line_manager_id, db)

    db.add(user)
    db.add(AuditLog(user_id=current_user.id, action="UPDATE_USER",
                    resource_type="user", resource_id=user.id,
                    detail={"is_active": user.is_active, "role": user.role.value}))
    await db.flush()
    return await _user_out(user, db)


@router.post("/{user_id}/reset-password", response_model=PasswordResetOut)
async def reset_password(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_hr),
):
    user = (await db.execute(
        select(User).where(User.id == user_id, User.org_id == current_user.org_id)
    )).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    temp = secrets.token_urlsafe(9)  # ~12 chars, satisfies the 8-char minimum
    user.hashed_password = hash_password(temp)
    user.force_password_change = True
    db.add(user)
    db.add(AuditLog(user_id=current_user.id, action="RESET_PASSWORD",
                    resource_type="user", resource_id=user.id))
    return PasswordResetOut(temp_password=temp)
