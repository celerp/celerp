# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import settings
from celerp.db import get_session
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

ROLE_LEVELS = {"viewer": 1, "operator": 2, "manager": 3, "admin": 4, "owner": 5}

# Legacy role migration: old JWTs carry these until they expire
_ROLE_MIGRATION = {"salesperson": "operator"}


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(subject: str, company_id: str, role: str) -> str:
    expire_minutes = min(int(settings.access_token_expire_minutes), 24 * 60)
    payload = {
        "sub": subject,
        "company_id": company_id,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(subject: str, company_id: str, role: str) -> str:
    payload = {
        "sub": subject,
        "company_id": company_id,
        "role": role,
        "type": "refresh",
        "exp": datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_refresh_token(token: str) -> dict:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token") from e
    if claims.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    return claims


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from e


async def get_current_user(token: str = Depends(oauth2_scheme), session: AsyncSession = Depends(get_session)) -> User:
    claims = _decode_token(token)
    user_id = claims.get("sub")
    company_id = claims.get("company_id")

    if not user_id or not company_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = await session.get(User, uuid.UUID(str(user_id)))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    # Validate company_id against UserCompany membership (supports multi-company users)
    link = await session.scalar(
        select(UserCompany).where(
            UserCompany.user_id == user.id,
            UserCompany.company_id == uuid.UUID(str(company_id)),
            UserCompany.is_active == True,  # noqa: E712
        )
    )
    if link is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    # Block access to deactivated companies
    company = await session.get(Company, uuid.UUID(str(company_id)))
    if company is None or not company.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Company is deactivated")

    from celerp.services.session_tracker import record as _record_activity
    _record_activity(str(user.id))

    return user


def get_current_company_id(token: str = Depends(oauth2_scheme)) -> uuid.UUID:
    claims = _decode_token(token)
    company_id = claims.get("company_id")
    if not company_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return uuid.UUID(str(company_id))


def require_min_role(min_role: str):
    """DRY guard: require the caller's role to be >= min_role in the hierarchy."""
    min_level = ROLE_LEVELS[min_role]

    def _guard(token: str = Depends(oauth2_scheme)) -> None:
        claims = _decode_token(token)
        raw_role = claims.get("role", "viewer")
        role = _ROLE_MIGRATION.get(raw_role, raw_role)
        if ROLE_LEVELS.get(role, 0) < min_level:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Requires {min_role} role or higher")

    return Depends(_guard)


def require_admin(token: str = Depends(oauth2_scheme)) -> None:
    """Raise 403 if role < admin."""
    claims = _decode_token(token)
    raw_role = claims.get("role", "viewer")
    role = _ROLE_MIGRATION.get(raw_role, raw_role)
    if ROLE_LEVELS.get(role, 0) < ROLE_LEVELS["admin"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")


def require_manager(token: str = Depends(oauth2_scheme)) -> None:
    """Raise 403 if role < manager."""
    claims = _decode_token(token)
    raw_role = claims.get("role", "viewer")
    role = _ROLE_MIGRATION.get(raw_role, raw_role)
    if ROLE_LEVELS.get(role, 0) < ROLE_LEVELS["manager"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Manager role required")


def get_current_role(token: str = Depends(oauth2_scheme)) -> str:
    """Return the role from the current token, applying legacy migration."""
    claims = _decode_token(token)
    raw_role = claims.get("role", "viewer")
    return _ROLE_MIGRATION.get(raw_role, raw_role)
