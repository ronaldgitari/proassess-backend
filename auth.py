from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import User
from schemas import LoginRequest, TokenPair, UserOut, StaffProfileOut, ChangePasswordRequest
from services.auth_service import (
    verify_password,
    hash_password,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenPair)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == req.email))
    user = result.scalar_one_or_none()

    if not user or not user.hashed_password:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is inactive")

    extra = {"role": user.role.value, "org_id": str(user.org_id)}
    return TokenPair(
        access_token=create_access_token(str(user.id), extra=extra),
        refresh_token=create_refresh_token(str(user.id)),
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(refresh_token: str, db: AsyncSession = Depends(get_db)):
    payload = decode_token(refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(400, "Invalid token type")

    user = await db.get(User, payload["sub"])
    if not user or not user.is_active:
        raise HTTPException(401, "User not found")

    extra = {"role": user.role.value, "org_id": str(user.org_id)}
    return TokenPair(
        access_token=create_access_token(str(user.id), extra=extra),
        refresh_token=create_refresh_token(str(user.id)),
    )


@router.get("/me", response_model=UserOut)
async def me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from services.permissions import get_effective_permissions
    from models.user import Organisation
    from system_settings import effective_settings as compute_effective_settings

    out = UserOut.model_validate(current_user)
    out.permissions = await get_effective_permissions(current_user, db)

    org_result = await db.execute(select(Organisation).where(Organisation.id == current_user.org_id))
    org = org_result.scalar_one_or_none()
    org_settings = (org.settings or {}) if org else {}
    out.effective_settings = compute_effective_settings(org_settings, current_user.settings)

    return out


@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change own password. Clears the force_password_change flag."""
    if not current_user.hashed_password or not verify_password(req.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if req.new_password == req.current_password:
        raise HTTPException(status_code=400, detail="New password must differ from the current one")

    current_user.hashed_password = hash_password(req.new_password)
    current_user.force_password_change = False
    db.add(current_user)
    return {"status": "ok"}


@router.get("/profile", response_model=StaffProfileOut)
async def get_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return enriched user profile including department and line manager."""
    from sqlalchemy.orm import aliased
    from models.user import UserDepartment, Department

    LineManager = aliased(User)

    result = await db.execute(
        select(UserDepartment, Department, LineManager)
        .join(Department, UserDepartment.department_id == Department.id)
        .outerjoin(LineManager, UserDepartment.line_manager_id == LineManager.id)
        .where(UserDepartment.user_id == current_user.id)
        .limit(1)
    )
    row = result.first()

    department = job_title = line_manager = None
    if row:
        ud, dept, lm = row
        department = dept.name
        job_title = ud.title
        line_manager = lm.name if lm else None

    return StaffProfileOut(
        id=current_user.id,
        full_name=current_user.name,
        email=current_user.email,
        role=current_user.role,
        department=department,
        job_title=job_title,
        line_manager=line_manager,
    )
