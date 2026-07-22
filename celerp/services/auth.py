# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

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


def create_access_token(
    subject: str,
    company_id: str,
    role: str,
    email: str = "",
    jti: str | None = None,
    snonce: str = "",
    modules: list[str] | None = None,
) -> tuple[str, str]:
    """Return (encoded_token, jti).

    If *jti* is provided (token refresh path) the same JTI is reused so the
    session slot is not duplicated.  Otherwise a fresh UUID4 is minted.

    *snonce* must be the caller-provided per-user nonce fetched from DB via
    ``session_tracker.get_nonce(session, user_id)`` before calling this function.

    *modules* is the list of enabled module names for the company, embedded so
    the UI can filter the sidebar without any additional DB or API calls.
    """
    import uuid as _uuid
    expire_minutes = min(int(settings.access_token_expire_minutes), 24 * 60)
    token_jti = jti or str(_uuid.uuid4())
    payload = {
        "sub": subject,
        "email": email,
        "company_id": company_id,
        "role": role,
        "jti": token_jti,
        "snonce": snonce,
        "modules": modules or [],
        "exp": datetime.now(timezone.utc) + timedelta(minutes=expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), token_jti


def create_refresh_token(subject: str, company_id: str, role: str, email: str = "") -> str:
    payload = {
        "sub": subject,
        "email": email,
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


def get_token_claims(token: str) -> dict | None:
    """Decode token and return claims dict, or None if invalid/expired.

    Does NOT hit the database or validate session nonce.  Use only where a
    lightweight claims-only check is needed (e.g. SSE session-watch setup).
    """
    try:
        return _decode_token(token)
    except HTTPException:
        return None


async def get_current_user(
    token: str = Depends(oauth2_scheme), session: AsyncSession = Depends(get_session)
) -> User:
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

    # Block access to deactivated companies - but owners can still authenticate
    # so they can create a new company or reactivate the existing one.
    company = await session.get(Company, uuid.UUID(str(company_id)))
    if company is None or (not company.is_active and link.role != "owner"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Company is deactivated")

    # Validate session nonce: rejects tokens minted before the last logout/force-login.
    # Tokens without the snonce claim (empty string) are allowed through - these are
    # tokens minted directly in tests or before this deploy, where no nonce was embedded.
    from celerp.services.session_tracker import get_nonce as _get_nonce, pop_evicted_by_ip as _pop_ip
    token_nonce = claims.get("snonce", "")
    if token_nonce:
        current_nonce = await _get_nonce(session, str(user.id))
        if token_nonce != current_nonce:
            evicting_ip = await _pop_ip(session, str(user.id))
            detail = f"Session expired|{evicting_ip}" if evicting_ip else "Session expired"
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

    return user


def get_current_company_id(token: str = Depends(oauth2_scheme)) -> uuid.UUID:
    claims = _decode_token(token)
    company_id = claims.get("company_id")
    if not company_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return uuid.UUID(str(company_id))


def get_current_role(token: str = Depends(oauth2_scheme)) -> str:
    """Return the role from the current token, applying legacy migration."""
    claims = _decode_token(token)
    raw_role = claims.get("role", "viewer")
    return _ROLE_MIGRATION.get(raw_role, raw_role)
