# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import uuid
from dataclasses import dataclass
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

# Legacy role migration: old roles carried in DB state until they are edited.
# Applied to the authoritative DB role, never to a JWT claim.
_ROLE_MIGRATION = {"salesperson": "operator"}

# Token format version. Bumping this rejects every token minted by an older
# build: the code version itself is the cutover boundary (no DB migration).
# v2 introduced the type/auth_ver/snonce contract and the DB-authoritative
# validator; a token whose auth_ver != AUTH_TOKEN_VERSION is rejected outright.
AUTH_TOKEN_VERSION = 2


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

    *role*, *email* and *modules* are UI/client hints only - they are NEVER used
    for server authorization, which derives the role from current DB membership.

    *modules* is the list of enabled module names for the company, embedded so
    the UI can filter the sidebar without any additional DB or API calls.
    """
    import uuid as _uuid
    expire_minutes = min(int(settings.access_token_expire_minutes), 24 * 60)
    token_jti = jti or str(_uuid.uuid4())
    payload = {
        "auth_ver": AUTH_TOKEN_VERSION,
        "type": "access",
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


def create_refresh_token(subject: str, company_id: str, role: str, email: str = "", snonce: str = "") -> str:
    """Return an encoded v2 refresh token bound to the per-user *snonce*.

    The refresh token carries NO authoritative role or email: on refresh they
    are read from current DB state.  *role* and *email* are accepted for
    signature compatibility with callers but are intentionally not embedded.
    """
    payload = {
        "auth_ver": AUTH_TOKEN_VERSION,
        "type": "refresh",
        "sub": subject,
        "company_id": company_id,
        "snonce": snonce,
        "exp": datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_refresh_token(token: str) -> dict:
    """Strictly decode a v2 refresh token, or raise a neutral 401.

    Requires: valid signature, unexpired, auth_ver == AUTH_TOKEN_VERSION,
    type == "refresh", and non-empty sub/company_id/snonce.  Every failure
    mode returns the same "Invalid refresh token" detail so nothing about which
    element failed leaks to the caller.
    """
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token") from e
    if claims.get("auth_ver") != AUTH_TOKEN_VERSION:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    if claims.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    if not claims.get("sub") or not claims.get("company_id") or not claims.get("snonce"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    return claims


def decode_access_token(token: str) -> dict:
    """Verify signature + expiry and require the full v2 access-token contract.

    Requires: valid signature, unexpired, auth_ver == AUTH_TOKEN_VERSION,
    type == "access", and non-empty sub/company_id/jti/snonce.  Any failure
    raises a neutral 401 "Invalid token" - no information about which check
    failed leaks.  A missing snonce fails closed (there is no legacy accept
    path).
    """
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from e
    if claims.get("auth_ver") != AUTH_TOKEN_VERSION:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if claims.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    if not claims.get("sub") or not claims.get("company_id") or not claims.get("jti") or not claims.get("snonce"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return claims


def get_token_claims(token: str) -> dict | None:
    """Decode a v2 access token and return its claims, or None if invalid.

    This is NOT an authorization check - it runs the same strict signature and
    contract checks as ``decode_access_token`` but never touches the DB or the
    session nonce.  Use only where a lightweight, non-authoritative claims read
    is needed; every authenticated path must go through ``validate_access_token``.
    """
    try:
        return decode_access_token(token)
    except HTTPException:
        return None


@dataclass(frozen=True)
class AuthContext:
    """The single authoritative result of validating an access token.

    Every FastAPI auth dependency resolves this once per request (via
    ``get_auth_context``) and reads its field, so no dependency can become
    authenticated from claims alone.
    """

    claims: dict
    user: User
    company: Company
    company_id: uuid.UUID
    role: str
    snonce: str


async def validate_access_token(session: AsyncSession, token: str) -> AuthContext:
    """The ONE authoritative access-token validator.

    Verifies the v2 signed-token contract, then binds it to current DB state:
    active user, active membership for the token's company, the company-active
    rule (owners may still authenticate into a deactivated company), and exact
    per-user nonce equality.  Returns the authorization role from the current
    ``UserCompany.role`` (never a JWT claim), with legacy role names migrated.
    """
    claims = decode_access_token(token)

    try:
        user_uuid = uuid.UUID(str(claims["sub"]))
        company_uuid = uuid.UUID(str(claims["company_id"]))
    except (ValueError, AttributeError, KeyError) as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from e

    user = await session.get(User, user_uuid)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    link = await session.scalar(
        select(UserCompany).where(
            UserCompany.user_id == user.id,
            UserCompany.company_id == company_uuid,
            UserCompany.is_active == True,  # noqa: E712
        )
    )
    if link is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    # Block access to deactivated companies - but owners can still authenticate
    # so they can create a new company or reactivate the existing one.
    company = await session.get(Company, company_uuid)
    if company is None or (not company.is_active and link.role != "owner"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Company is deactivated")

    # Nonce equality is mandatory: a token whose snonce no longer matches the
    # current per-user nonce (logout, force-login, or any security-sensitive
    # account change rotated it) is rejected regardless of expiry.
    from celerp.services.session_tracker import get_nonce as _get_nonce, pop_evicted_by_ip as _pop_ip
    token_nonce = claims.get("snonce", "")
    current_nonce = await _get_nonce(session, str(user.id))
    if token_nonce != current_nonce:
        evicting_ip = await _pop_ip(session, str(user.id))
        detail = f"Session expired|{evicting_ip}" if evicting_ip else "Session expired"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

    role = _ROLE_MIGRATION.get(link.role, link.role)
    return AuthContext(
        claims=claims,
        user=user,
        company=company,
        company_id=company_uuid,
        role=role,
        snonce=token_nonce,
    )


async def get_auth_context(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> AuthContext:
    """FastAPI dependency: the one validated auth context for the request.

    FastAPI dependency caching resolves this once per request even when a route
    asks for user, company_id and role separately.
    """
    return await validate_access_token(session, token)


async def get_current_user(ctx: AuthContext = Depends(get_auth_context)) -> User:
    return ctx.user


async def get_current_company_id(ctx: AuthContext = Depends(get_auth_context)) -> uuid.UUID:
    return ctx.company_id


async def get_current_role(ctx: AuthContext = Depends(get_auth_context)) -> str:
    """Return the authorization role from current DB membership (migrated)."""
    return ctx.role


async def issue_token_pair(
    session: AsyncSession,
    *,
    user: User,
    company: Company,
    role: str,
    jti: str | None = None,
) -> dict:
    """The single access+refresh issuance point.

    Obtains the current per-user nonce, builds the enabled-module UI hint list,
    mints a v2 access token and a v2 refresh token bound to the same nonce,
    registers the access JTI + expiry in the session registry, and returns
    ``{"access_token", "refresh_token"}``.  Every caller (register, login,
    force-login, refresh, switch-company, create-company) routes through here so
    no path can issue a token that misses the version/type/nonce contract.
    """
    from celerp.services.session_tracker import get_nonce as _get_nonce, register_token as _register
    from celerp.modules.registry import get_enabled as _get_enabled

    user_id = str(user.id)
    company_id = str(company.id)
    snonce = await _get_nonce(session, user_id)
    enabled_modules = sorted(_get_enabled(company.settings or {}))
    access_token, token_jti = create_access_token(
        user_id, company_id, role, user.email, jti=jti, snonce=snonce, modules=enabled_modules
    )
    # Cap at 24h to match create_access_token's internal cap so DB expiry = JWT exp.
    capped_minutes = min(int(settings.access_token_expire_minutes), 24 * 60)
    expiry_dt = datetime.now(timezone.utc) + timedelta(minutes=capped_minutes)
    await _register(session, token_jti, user_id, expiry_dt)
    refresh_token = create_refresh_token(user_id, company_id, role, user.email, snonce=snonce)
    return {"access_token": access_token, "refresh_token": refresh_token}
