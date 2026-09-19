# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, Location, User
from celerp.services.auth import (
    AuthContext,
    decode_refresh_token,
    get_auth_context,
    get_current_company_id,
    get_current_user,
    hash_password,
    issue_token_pair,
    oauth2_scheme_optional,
    validate_access_token,
    verify_password,
)

router = APIRouter()

logger = logging.getLogger(__name__)


async def _issue_tokens(
    session: AsyncSession,
    user: User,
    company: Company,
    role: str,
    jti: str | None = None,
    expected_snonce: str | None = None,
) -> dict:
    """Adapter over the central ``issue_token_pair`` for this router's callers.

    Register, login, force-login and switch-company already hold the ``User``
    and ``Company`` rows they authenticated against, so they pass them straight
    through to the one issuance point.  A continuation of an authenticated
    session (refresh, switch-company) passes *expected_snonce* so it cannot mint
    onto a generation advanced by a concurrent revocation.
    """
    return await issue_token_pair(
        session, user=user, company=company, role=role, jti=jti, expected_snonce=expected_snonce
    )


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or str(uuid.uuid4())


class RegisterRequest(BaseModel):
    company_name: str
    email: str
    name: str
    password: str
    setup_code: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


def _setup_code_hash() -> str:
    """The one-time setup-code hash for a headless install, or '' if none required."""
    from celerp.config import read_config
    return read_config().get("auth", {}).get("setup_code_hash", "") or ""


def _clear_setup_code() -> None:
    """Best-effort cleanup after the first admin has already committed.

    The DB user row is the authoritative one-time registration gate. Config-lock
    contention here must never turn a successful registration into a 500 that
    tells the operator to retry an operation that already completed.
    """
    from celerp.config import _update_config, config_path
    try:
        _update_config(lambda cfg: cfg.get("auth", {}).pop("setup_code_hash", None))
    except Exception as exc:
        logger.warning("setup-code config cleanup deferred after registration: %s",
                       type(exc).__name__)
    try:
        (config_path().parent / "setup-code").unlink()
    except OSError:
        pass


@router.get("/bootstrap-status")
async def bootstrap_status(session: AsyncSession = Depends(get_session)) -> dict:
    """Public endpoint: returns whether the system has been bootstrapped.

    UI uses this to decide whether to show the first-admin registration wizard
    (bootstrapped=false) or the normal login screen (bootstrapped=true).
    Once any user exists, registration is locked out from the public UI.
    """
    count = (await session.execute(select(User))).scalars().first()
    return {"bootstrapped": count is not None, "setup_code_required": bool(_setup_code_hash())}


@router.post("/register")
async def register(payload: RegisterRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """Register first admin. Locked out after bootstrap (any user exists)."""
    existing = (await session.execute(select(User))).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=403, detail="System already bootstrapped. Contact your admin.")

    # Headless installs mint a one-time setup code the operator reads off the box,
    # so a network-exposed first-admin page can't be claimed by a stranger.
    required = _setup_code_hash()
    if required:
        import hashlib
        import hmac as _hmac
        provided = (payload.setup_code or "").strip()
        if not provided or not _hmac.compare_digest(
            hashlib.sha256(provided.encode()).hexdigest(), required
        ):
            raise HTTPException(status_code=403, detail="Invalid or missing setup code.")

    slug = _slugify(payload.company_name)
    company = Company(id=uuid.uuid4(), name=payload.company_name, slug=slug, settings={"fiscal_year_start": "01-01"})
    user = User(
        id=uuid.uuid4(),
        email=payload.email,
        name=payload.name,
        auth_hash=hash_password(payload.password),
        api_key=None,
        is_active=True,
    )
    session.add(company)
    session.add(user)
    await session.flush()  # persist company + user first (Postgres FK enforcement)
    # Link user to company - UserCompany is the single source of role+company truth
    link = UserCompany(id=uuid.uuid4(), user_id=user.id, company_id=company.id, role="owner")
    session.add(link)
    await session.flush()  # ensure IDs are set before module hooks
    # Fire module lifecycle hooks (e.g. celerp-accounting seeds chart of accounts)
    from celerp.modules.slots import fire_lifecycle
    await fire_lifecycle("on_company_created", session=session, company_id=company.id)
    # Seed a default "Head Office" location before demo items so items land in it
    head_office = Location(
        id=uuid.uuid4(),
        company_id=company.id,
        name="Head Office",
        type="office",
        address=None,
        is_default=True,
    )
    session.add(head_office)
    await session.flush()
    from celerp.services.demo import seed_demo_items
    await seed_demo_items(session, company.id, user.id, default_location_id=head_office.id)
    # Seed the company's single self-contact (typed `both`) with company name, owner name + admin email
    from celerp.services.demo import seed_self_contacts
    await seed_self_contacts(
        session,
        company_id=company.id,
        actor_id=user.id,
        person_name=payload.name,
        company_name=payload.company_name,
        email=payload.email,
    )
    try:
        await session.commit()
    except Exception as e:
        await session.rollback()
        logger.error("register failed: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=f"Registration failed: {e}") from e

    if required:
        await asyncio.to_thread(_clear_setup_code)

    return await _issue_tokens(session, user, company, link.role)


from slowapi import Limiter
from slowapi.util import get_remote_address

# Module-level limiter for /auth/login rate limiting.
# Tests reset this via conftest: celerp.routers.auth.limiter._storage.reset()
limiter = Limiter(key_func=get_remote_address)


@router.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, payload: LoginRequest, session: AsyncSession = Depends(get_session)) -> dict:
    user = (await session.execute(select(User).where(User.email == payload.email))).scalar_one_or_none()
    if not user or not user.auth_hash or not verify_password(payload.password, user.auth_hash) or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    from celerp.gateway.state import get_session_token as _get_session_token
    from celerp.services.session_tracker import active_user_ids as _active_ids
    if not _get_session_token():
        active = await _active_ids(session)
        if active:
            raise HTTPException(status_code=409, detail="direct_connection_limit")

    # Pick the user's active company link. If they belong to multiple companies
    # they must use /switch-company after login; we pick the first active one here.
    link = (
        await session.execute(
            select(UserCompany).where(
                UserCompany.user_id == user.id,
                UserCompany.is_active == True,  # noqa: E712
            ).order_by(UserCompany.id).limit(1)
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=401, detail="No active company membership")

    company = await session.get(Company, link.company_id)
    return await _issue_tokens(session, user, company, link.role)


@router.post("/login-force")
@limiter.limit("5/minute")
async def login_force(request: Request, payload: LoginRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """Like /login but evicts all other active sessions from the tracker first."""
    user = (await session.execute(select(User).where(User.email == payload.email))).scalar_one_or_none()
    if not user or not user.auth_hash or not verify_password(payload.password, user.auth_hash) or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    from celerp.services.session_tracker import invalidate_all_sessions as _invalidate_all
    evicting_ip = request.client.host if request.client else None
    await _invalidate_all(session, str(user.id), evicting_ip=evicting_ip)

    link = (
        await session.execute(
            select(UserCompany).where(
                UserCompany.user_id == user.id,
                UserCompany.is_active == True,  # noqa: E712
            ).order_by(UserCompany.id).limit(1)
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=401, detail="No active company membership")

    company = await session.get(Company, link.company_id)
    return await _issue_tokens(session, user, company, link.role)


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/token/refresh")
async def refresh_token(payload: RefreshRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """Exchange a valid refresh token for a new access token + rotated refresh token.

    Fully DB-authoritative: the refresh token is decoded strictly (v2, type,
    non-empty snonce), then re-bound to current DB state - active user, active
    membership, company-validity rule, and exact nonce equality. The new pair's
    role and email come from current DB membership, never from the refresh JWT.
    Every failure mode returns the same neutral "Invalid refresh token" so the
    caller learns nothing about which element failed.
    """
    claims = decode_refresh_token(payload.refresh_token)

    try:
        user_uuid = uuid.UUID(str(claims["sub"]))
        company_uuid = uuid.UUID(str(claims["company_id"]))
    except (ValueError, AttributeError, KeyError) as e:
        raise HTTPException(status_code=401, detail="Invalid refresh token") from e

    user = await session.get(User, user_uuid)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    link = await session.scalar(
        select(UserCompany).where(
            UserCompany.user_id == user.id,
            UserCompany.company_id == company_uuid,
            UserCompany.is_active == True,  # noqa: E712
        )
    )
    if link is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    company = await session.get(Company, company_uuid)
    if company is None or (not company.is_active and link.role != "owner"):
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    # Nonce equality is enforced once, under the issuance row lock: the refresh
    # is a continuation, so it presents the snonce it decoded. If a concurrent
    # revocation advanced the generation, issue_token_pair rejects it (401)
    # before minting - no separate, unlocked nonce read that could race.
    return await _issue_tokens(
        session, user, company, link.role, expected_snonce=claims["snonce"]
    )


@router.post("/api-key")
async def create_api_key(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    user.api_key = str(uuid.uuid4())
    await session.commit()
    return {"api_key": user.api_key}


@router.get("/my-companies")
async def my_companies(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    current_company_id: uuid.UUID = Depends(get_current_company_id),
) -> dict:
    """List all companies the current user has access to."""
    links = (
        await session.execute(
            select(UserCompany).where(UserCompany.user_id == user.id, UserCompany.is_active == True)  # noqa: E712
        )
    ).scalars().all()
    company_ids = [link.company_id for link in links]
    if not company_ids:
        return {"items": [], "total": 0}
    companies_rows = (
        await session.execute(
            select(Company).where(Company.id.in_(company_ids), Company.is_active == True)  # noqa: E712
        )
    ).scalars().all()
    companies_by_id = {c.id: c for c in companies_rows}
    role_by_id = {link.company_id: link.role for link in links}
    result = [
        {
            "company_id": str(c.id),
            "company_name": c.name,
            "slug": c.slug,
            "role": role_by_id[c.id],
            "is_current": c.id == current_company_id,
        }
        for c in companies_rows
        if c.id in role_by_id
    ]
    return {"items": result, "total": len(result)}


@router.post("/switch-company/{company_id}")
async def switch_company(
    company_id: uuid.UUID,
    ctx: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Switch the active company. Returns a new JWT scoped to the target company.

    Only succeeds if the user has an active entry in user_companies for that company.
    """
    user = ctx.user
    link = (
        await session.execute(
            select(UserCompany).where(
                UserCompany.user_id == user.id,
                UserCompany.company_id == company_id,
                UserCompany.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=403, detail="Access to this company not granted")
    company = await session.get(Company, company_id)
    if company is None or not company.is_active:
        raise HTTPException(status_code=403, detail="Company is deactivated")
    # A company switch is a continuation of the current session: pass the snonce
    # it authenticated on so a concurrent revocation cannot be jumped over.
    return await _issue_tokens(session, user, company, link.role, expected_snonce=ctx.snonce)


# ── Password Reset ────────────────────────────────────────────────────────────

class PasswordResetRequestBody(BaseModel):
    email: str


class PasswordResetConfirmBody(BaseModel):
    token: str
    new_password: str


_RESET_TOKEN_TTL_MINUTES = 15


@router.post("/password-reset/request")
@limiter.limit("3/minute")
async def password_reset_request(
    request: Request,
    payload: PasswordResetRequestBody,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Request a password reset link. Always returns 200 (prevents user enumeration)."""
    user = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if user:
        token = secrets.token_urlsafe(32)
        user.reset_token = token
        user.reset_token_expires = datetime.now(timezone.utc) + timedelta(minutes=_RESET_TOKEN_TTL_MINUTES)
        await session.commit()

        from celerp.config import settings
        import asyncio
        base = settings.celerp_public_url or ""
        reset_link = f"{base}/reset-password?token={token}"
        body_html = (
            f"<p>Hi {user.name},</p>"
            f"<p>We received a request to reset the password for your Celerp account "
            f"(<strong>{user.email}</strong>).</p>"
            f"<p style='margin:24px 0;'>"
            f"<a href='{reset_link}' style='background:#1a1a1a;color:#fff;padding:12px 24px;"
            f"border-radius:6px;text-decoration:none;font-weight:600;'>Reset my password</a>"
            f"</p>"
            f"<p>This link expires in <strong>{_RESET_TOKEN_TTL_MINUTES} minutes</strong>.</p>"
            f"<p style='color:#888;font-size:13px;'>If you didn't request this, you can safely ignore "
            f"this email - your password won't change.</p>"
        )
        body_text = (
            f"Hi {user.name},\n\n"
            f"Reset your Celerp password:\n{reset_link}\n\n"
            f"This link expires in {_RESET_TOKEN_TTL_MINUTES} minutes.\n\n"
            f"If you didn't request this, ignore this email."
        )
        from celerp.services.email import send_email
        asyncio.create_task(send_email(
            user.email,
            "Reset your Celerp password",
            body_html,
            body_text=body_text,
            branded=False,  # security email: keep it minimal
        ))

    return {"detail": "If that email exists, you'll receive a reset link."}


@router.post("/password-reset/confirm")
async def password_reset_confirm(
    payload: PasswordResetConfirmBody,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Confirm password reset with token and new password."""
    user = (
        await session.execute(select(User).where(User.reset_token == payload.token))
    ).scalar_one_or_none()
    if not user or not user.reset_token_expires:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    expires = user.reset_token_expires
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    user.auth_hash = hash_password(payload.new_password)
    user.reset_token = None
    user.reset_token_expires = None
    # Rotate the user's nonce so every access and refresh token issued before the
    # reset dies immediately (a reset is a security-sensitive credential change).
    # invalidate_sessions commits the session, so no earlier independent commit.
    from celerp.services.session_tracker import invalidate_sessions as _invalidate
    await _invalidate(session, str(user.id))
    return {"detail": "Password updated successfully."}


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordBody,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Change password for the currently authenticated user."""
    if not user.auth_hash or not verify_password(payload.current_password, user.auth_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    user.auth_hash = hash_password(payload.new_password)
    # Rotate the user's nonce so every access and refresh token minted before the
    # change dies immediately, including the caller's current session.
    # invalidate_sessions commits the session, so no earlier independent commit.
    from celerp.services.session_tracker import invalidate_sessions as _invalidate
    await _invalidate(session, str(user.id))
    return {"detail": "Password changed successfully."}


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


@router.post("/logout")
async def logout(
    payload: LogoutRequest | None = None,
    token: str | None = Depends(oauth2_scheme_optional),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Revoke the caller's session, then rotate their nonce so every existing
    access AND refresh token for this user is immediately rejected.

    Accepts EITHER a current access token (Authorization header) or a current v2
    refresh token (JSON body), so a browser whose access cookie has expired but
    whose refresh cookie is still live can still log out server-side (F3).
    Revocation only happens after the presented credential's snonce is confirmed
    current under the ``UserAuthState`` row lock: a stale credential cannot
    rotate a newer session generation, and logout stays idempotent (always 200).
    """
    from celerp.services.session_tracker import invalidate_sessions as _invalidate

    # Access-token path: validate fully, then revoke on its authenticated snonce.
    if token:
        try:
            ctx = await validate_access_token(session, token)
        except HTTPException:
            ctx = None
        if ctx is not None:
            await _invalidate(session, str(ctx.user.id), expected_snonce=ctx.snonce)
            return {"detail": "Logged out."}

    # Refresh-only path: strictly decode the v2 refresh token, then revoke on the
    # snonce it carries. A stale snonce is skipped by the locked revocation.
    if payload is not None and payload.refresh_token:
        try:
            claims = decode_refresh_token(payload.refresh_token)
        except HTTPException:
            claims = None
        if claims is not None:
            await _invalidate(
                session, str(claims["sub"]), expected_snonce=claims["snonce"]
            )

    return {"detail": "Logged out."}
