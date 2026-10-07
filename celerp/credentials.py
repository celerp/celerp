# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Credential issuance: the only code that mints access and refresh tokens.

Protected by module admission policy like the AI internals: a third-party module
whose code imports it is refused. Modules act for a signed-in user through
``celerp.modules.api``."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import settings
from celerp.models.company import User
from celerp.services.auth import AUTH_TOKEN_VERSION, lock_issuance_company


def create_access_token(
    subject: str,
    company_id: str,
    role: str,
    email: str = "",
    jti: str | None = None,
    *,
    snonce: str,
) -> tuple[str, str]:
    """Return (encoded_token, jti).

    If *jti* is provided (token refresh path) the same JTI is reused so the
    session slot is not duplicated.  Otherwise a fresh UUID4 is minted.

    *snonce* is mandatory and keyword-only: it must be the caller-provided
    per-user nonce fetched from DB via
    ``session_tracker.get_nonce(session, user_id)`` before calling this function.
    There is no default - a session-bound token can never be minted without one.

    *role* and *email* are UI/client hints only - they are NEVER used for
    server authorization, which derives the role from current DB membership.
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
        "exp": datetime.now(timezone.utc) + timedelta(minutes=expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), token_jti


def create_refresh_token(subject: str, company_id: str, *, snonce: str) -> str:
    """Return an encoded v2 refresh token bound to the per-user *snonce*.

    The refresh token carries NO authoritative role or email: on refresh they
    are read from current DB state, so neither is accepted here.  *snonce* is
    mandatory and keyword-only.
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


async def issue_token_pair(
    session: AsyncSession,
    *,
    user: User,
    company_id: uuid.UUID,
    expected_snonce: str | None,
    jti: str | None = None,
) -> dict:
    """The single access+refresh issuance point.

    First holds *company_id* against removal and re-checks, under that lock, that the
    user can still work in it (``lock_issuance_company``); the role comes from that
    link. Then locks the per-user ``UserAuthState`` row FOR UPDATE, reads the current nonce
    under that lock, builds the enabled-module UI hint list, mints a v2 access
    token and a v2 refresh token bound to exactly the locked nonce, registers the
    access JTI + expiry in the same transaction, commits once, and returns
    ``{"access_token", "refresh_token"}``.  Every caller (register, login,
    force-login, refresh, switch-company, create-company) routes through here so
    no path can issue a token that misses the version/type/nonce contract.

    *expected_snonce* distinguishes a continuation from a fresh credential, and every
    caller states which it is:

    - A continuation (refresh, sliding refresh, switch-company, create-company,
      company reset, signed-in backup restore and reactivation) passes the snonce it authenticated on.  If it no longer equals the locked
      nonce, a concurrent revocation advanced the generation, so a neutral 401
      is raised BEFORE minting or registering any JTI - the continuation can
      never jump onto the newer generation (F2).
    - A fresh credential (register, password login, force-login, setup-code and
      password-authenticated restores and migration starts) passes
      ``expected_snonce=None`` and always mints on whatever the locked row holds.

    Holding the lock across the read-check-mint-register-commit window is what
    serializes issuance against ``invalidate_sessions``/``invalidate_all_sessions``.
    """
    from celerp.services.session_tracker import (
        lock_auth_state as _lock,
        register_token as _register,
    )

    role = (await lock_issuance_company(session, user.id, company_id)).role
    user_id = str(user.id)
    company_id = str(company_id)
    auth_state = await _lock(session, user_id)
    if expected_snonce is not None and expected_snonce != auth_state.nonce:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    snonce = auth_state.nonce
    access_token, token_jti = create_access_token(
        user_id, company_id, role, user.email, jti=jti, snonce=snonce
    )
    # Cap at 24h to match create_access_token's internal cap so DB expiry = JWT exp.
    capped_minutes = min(int(settings.access_token_expire_minutes), 24 * 60)
    expiry_dt = datetime.now(timezone.utc) + timedelta(minutes=capped_minutes)
    await _register(session, token_jti, user_id, company_id, expiry_dt, commit=False)
    await session.commit()
    refresh_token = create_refresh_token(user_id, company_id, snonce=snonce)
    return {"access_token": access_token, "refresh_token": refresh_token}


async def issue_token_pair_by_id(session: AsyncSession, user_id, company_id, *,
                                 expected_snonce: str | None) -> dict:
    """``issue_token_pair`` for the user and company named by id."""
    user = await session.get(User, uuid.UUID(str(user_id)))
    return await issue_token_pair(session, user=user, company_id=uuid.UUID(str(company_id)),
                                  expected_snonce=expected_snonce)
