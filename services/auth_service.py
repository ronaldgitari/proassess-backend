from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from config import settings
from database import get_db
from models import User, UserRole

# bcrypt hashes only the first 72 bytes of a password (algorithm limit); we truncate
# (matching passlib's prior behaviour) so longer inputs don't error. Output is standard
# $2b$ bcrypt, so any password hashed previously via passlib still verifies here.
_BCRYPT_MAX_BYTES = 72
bearer_scheme = HTTPBearer()


# ─────────────────────────────────────────────────────────────────
# Password helpers
# ─────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8")[:_BCRYPT_MAX_BYTES], bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:_BCRYPT_MAX_BYTES], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ─────────────────────────────────────────────────────────────────
# Token creation
# ─────────────────────────────────────────────────────────────────

def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload: dict[str, Any] = {"sub": subject, "exp": expire, "type": "access"}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(subject: str) -> str:
    expire = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {"sub": subject, "exp": expire, "type": "refresh"}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


# ─────────────────────────────────────────────────────────────────
# FastAPI dependencies
# ─────────────────────────────────────────────────────────────────

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    payload = decode_token(credentials.credentials)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")

    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == UUID(user_id)))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


def require_roles(*roles: UserRole):
    """Dependency factory that enforces role-based access."""
    async def _check(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of: {[r.value for r in roles]}",
            )
        return current_user
    return _check


# Convenience role guards
require_staff = require_roles(UserRole.STAFF, UserRole.LINE_MANAGER, UserRole.HR_ADMIN, UserRole.SYSTEM_ADMIN)
require_lm = require_roles(UserRole.LINE_MANAGER, UserRole.HR_ADMIN, UserRole.SYSTEM_ADMIN)
require_hr = require_roles(UserRole.HR_ADMIN, UserRole.SYSTEM_ADMIN)
require_system_admin = require_roles(UserRole.SYSTEM_ADMIN)


def require_permission(*keys: str):
    """Dependency factory enforcing capability-based access (configurable security
    groups). Passes if the user's effective permissions include ANY of `keys`.
    Backed by the same resolver as /auth/me, so role defaults + custom groups +
    per-user overrides all apply."""
    async def _check(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        from services.permissions import get_effective_permissions
        perms = set(await get_effective_permissions(current_user, db))
        if not any(k in perms for k in keys):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires permission: {' or '.join(keys)}",
            )
        return current_user
    return _check


async def has_permission(user: User, key: str, db: AsyncSession) -> bool:
    from services.permissions import get_effective_permissions
    return key in set(await get_effective_permissions(user, db))


async def get_user_from_token(token: str, db: AsyncSession) -> User:
    """Resolve a user from a raw access token — used by the SSE stream endpoint,
    since EventSource cannot send an Authorization header (token passed as query param)."""
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user
