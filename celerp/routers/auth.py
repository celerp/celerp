# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

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
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    get_current_user,
    hash_password,
    verify_password,
)

router = APIRouter()

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or str(uuid.uuid4())


class RegisterRequest(BaseModel):
    company_name: str
    email: str
    name: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


@router.get("/bootstrap-status")
async def bootstrap_status(session: AsyncSession = Depends(get_session)) -> dict:
    """Public endpoint: returns whether the system has been bootstrapped.

    UI uses this to decide whether to show the first-admin registration wizard
    (bootstrapped=false) or the normal login screen (bootstrapped=true).
    Once any user exists, registration is locked out from the public UI.
    """
    count = (await session.execute(select(User))).scalars().first()
    return {"bootstrapped": count is not None}


@router.post("/register")
async def register(payload: RegisterRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """Register first admin. Locked out after bootstrap (any user exists)."""
    existing = (await session.execute(select(User))).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=403, detail="System already bootstrapped. Contact your admin.")

    slug = _slugify(payload.company_name)
    company = Company(id=uuid.uuid4(), name=payload.company_name, slug=slug, settings={"fiscal_year_start": "01-01"})
    user = User(
        id=uuid.uuid4(),
        company_id=company.id,
        email=payload.email,
        name=payload.name,
        role="owner",
        auth_hash=hash_password(payload.password),
        api_key=None,
        is_active=True,
    )
    session.add(company)
    session.add(user)
    await session.flush()  # persist company + user first (Postgres FK enforcement)
    # Link user to company after flush so user_id FK is satisfied
    link = UserCompany(id=uuid.uuid4(), user_id=user.id, company_id=company.id, role="owner")
    session.add(link)
    await session.flush()  # ensure IDs are set before module hooks
    # Fire module lifecycle hooks (e.g. celerp-accounting seeds chart of accounts)
    from celerp.modules.slots import fire_lifecycle
    await fire_lifecycle("on_company_created", session=session, company_id=company.id)
    from celerp.services.demo import seed_demo_items
    await seed_demo_items(session, company.id, user.id)
    # Seed company self-contacts (customer + vendor) with company name, owner name + admin email
    try:
        from celerp.events.engine import emit_event as _emit
        _seed_data = {
            "name": payload.name,
            "company_name": payload.company_name,
            "email": payload.email,
        }
        _customer_id = f"contact:{uuid.uuid4()}"
        await _emit(
            session,
            company_id=company.id,
            entity_id=_customer_id,
            entity_type="contact",
            event_type="crm.contact.created",
            data={**_seed_data, "contact_type": "customer"},
            actor_id=user.id,
            location_id=None,
            source="registration",
            idempotency_key=f"reg:contact:customer:{company.id}",
            metadata_={},
        )
        _vendor_id = f"contact:{uuid.uuid4()}"
        await _emit(
            session,
            company_id=company.id,
            entity_id=_vendor_id,
            entity_type="contact",
            event_type="crm.contact.created",
            data={**_seed_data, "contact_type": "vendor"},
            actor_id=user.id,
            location_id=None,
            source="registration",
            idempotency_key=f"reg:contact:vendor:{company.id}",
            metadata_={},
        )
    except Exception as _exc:
        logger.warning("contact seeding failed (non-fatal): %s", _exc)
    # Seed a default "Head Office" location
    head_office = Location(
        id=uuid.uuid4(),
        company_id=company.id,
        name="Head Office",
        type="office",
        address=None,
        is_default=True,
    )
    session.add(head_office)
    try:
        await session.commit()
    except Exception as e:
        await session.rollback()
        logger.error("register failed: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=f"Registration failed: {e}") from e

    return {
        "access_token": create_access_token(str(user.id), str(company.id), user.role),
        "refresh_token": create_refresh_token(str(user.id), str(company.id), user.role),
    }


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

    from celerp.gateway.client import get_client as _get_client
    from celerp.services.session_tracker import active_user_ids as _active_ids
    if _get_client() is None and _active_ids(company_id=str(user.company_id), exclude=str(user.id)):
        raise HTTPException(status_code=409, detail="direct_connection_limit")

    return {
        "access_token": create_access_token(str(user.id), str(user.company_id), user.role),
        "refresh_token": create_refresh_token(str(user.id), str(user.company_id), user.role),
    }


@router.post("/login-force")
@limiter.limit("5/minute")
async def login_force(request: Request, payload: LoginRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """Like /login but evicts all other active sessions from the tracker first."""
    user = (await session.execute(select(User).where(User.email == payload.email))).scalar_one_or_none()
    if not user or not user.auth_hash or not verify_password(payload.password, user.auth_hash) or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    from celerp.services.session_tracker import clear as _clear_tracker
    _clear_tracker()

    return {
        "access_token": create_access_token(str(user.id), str(user.company_id), user.role),
        "refresh_token": create_refresh_token(str(user.id), str(user.company_id), user.role),
    }


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/token/refresh")
async def refresh_token(payload: RefreshRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """Exchange a valid refresh token for a new access token + rotated refresh token."""
    claims = decode_refresh_token(payload.refresh_token)
    user_id = claims.get("sub")
    company_id = claims.get("company_id")
    role = claims.get("role", "")

    user = await session.get(User, uuid.UUID(str(user_id)))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    link = await session.scalar(
        select(UserCompany).where(
            UserCompany.user_id == user.id,
            UserCompany.company_id == uuid.UUID(str(company_id)),
            UserCompany.is_active == True,  # noqa: E712
        )
    )
    if link is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    return {
        "access_token": create_access_token(user_id, company_id, role),
        "refresh_token": create_refresh_token(user_id, company_id, role),
    }


@router.post("/api-key")
async def create_api_key(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    user.api_key = str(uuid.uuid4())
    await session.commit()
    return {"api_key": user.api_key}


@router.get("/my-companies")
async def my_companies(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List all companies the current user has access to."""
    links = (
        await session.execute(
            select(UserCompany).where(UserCompany.user_id == user.id, UserCompany.is_active == True)  # noqa: E712
        )
    ).scalars().all()
    result = []
    for link in links:
        company = await session.get(Company, link.company_id)
        if company and company.is_active:
            result.append({
                "company_id": str(company.id),
                "company_name": company.name,
                "slug": company.slug,
                "role": link.role,
            })
    return {"items": result, "total": len(result)}


@router.post("/switch-company/{company_id}")
async def switch_company(
    company_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Switch the active company. Returns a new JWT scoped to the target company.

    Only succeeds if the user has an active entry in user_companies for that company.
    """
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
    return {"access_token": create_access_token(str(user.id), str(company_id), link.role)}


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
            f"this email — your password won't change.</p>"
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
    await session.commit()
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
    await session.commit()
    return {"detail": "Password changed successfully."}
