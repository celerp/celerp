# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.config import settings
from celerp.db import get_session
from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.services.company_lock import hold_company
from ui.i18n import t

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

# Non-erroring variant: routes that accept EITHER a Bearer access token or a
# refresh-token body (logout) read the header without 401ing when it is absent,
# so a refresh-only credential can still be honored.
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

ROLE_LEVELS = {"viewer": 1, "operator": 2, "manager": 3, "admin": 4, "owner": 5}

# Legacy role migration: old roles carried in DB state until they are edited.
# Applied to the authoritative DB role, never to a JWT claim.
_ROLE_MIGRATION = {"salesperson": "operator"}


def normalize_role(role: str) -> str:
    """Map a legacy DB role alias to its current name (identity for current names).

    The one public normalization point: every place that compares or surfaces a
    stored ``UserCompany.role`` routes through here so a legacy value like
    ``salesperson`` is ranked and displayed as ``operator``. Never applied to a
    JWT role claim, which is a UI hint and never server authority.
    """
    return _ROLE_MIGRATION.get(role, role)

# Token format version. Bumping this rejects every token minted by an older
# build: the code version itself is the cutover boundary (no DB migration).
# v2 introduced the type/auth_ver/snonce contract and the DB-authoritative
# validator; a token whose auth_ver != AUTH_TOKEN_VERSION is rejected outright.
AUTH_TOKEN_VERSION = 2


# The one backend password policy: at least this many characters. No composition
# rules. Every password-setting path validates against this single constant, and
# the UI preflight reads it too so the client and server never diverge.
MIN_PASSWORD_LENGTH = 8


def validate_password(password: str) -> None:
    """Raise ValueError('password_too_short') when a password is below policy.

    The single source of the length rule. Callers convert the ValueError into
    their own user-facing message so copy stays localized at the edge.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError("password_too_short")


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def hash_password(password: str) -> str:
    # Final backend invariant: no hash is ever produced for a sub-policy password,
    # so a future caller cannot silently bypass the length rule.
    validate_password(password)
    return pwd_context.hash(password)


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

    link = await usable_company_link(session, user.id, company_uuid)
    if link is None:
        # Name the deactivated company case; owners still authenticate into it so
        # they can reactivate it or create a new company.
        held = await session.scalar(select(UserCompany.id).where(
            UserCompany.user_id == user.id, UserCompany.company_id == company_uuid,
            UserCompany.is_active.is_(True)))
        detail = "Company is deactivated" if held is not None else "Invalid token"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
    company = await session.get(Company, company_uuid)

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

    role = normalize_role(link.role)
    return AuthContext(
        claims=claims,
        user=user,
        company=company,
        company_id=company_uuid,
        role=role,
        snonce=token_nonce,
    )


# The only routes a staged company's own token reaches. Token refresh, logout and health
# do not authenticate through this dependency, so they stay available as well.
STAGED_ALLOWED_PREFIX = "/migrations/"
MODULE_OFF = "This module is turned off for your company."


async def get_auth_context(
    request: Request,
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> AsyncIterator[AuthContext]:
    """FastAPI dependency: the one validated auth context for the request.

    FastAPI dependency caching resolves this once per request even when a route
    asks for user, company_id and role separately.

    A token scoped to a migration-staged company is isolated here, centrally: it
    reaches the migration routes only. The same user's tokens for other companies
    are unaffected.

    A module's routes answer only for a company that uses the module.
    """
    ctx = await validate_access_token(session, token)
    if ctx.company.is_migration_staged and not request.url.path.startswith(STAGED_ALLOWED_PREFIX):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=t("auth.company_staged"))
    from celerp.modules.loader import route_module
    from celerp.modules.registry import uses_module

    module = route_module(request.scope)
    if module and not uses_module(ctx.company.settings, module):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MODULE_OFF)
    # The authority the request starts with is judged again under the company lock
    # (company_lock), so a write that waits there never runs on revoked access.
    from celerp.services.permissions import authorize_request, end_request
    authority = authorize_request(session, ctx.company_id, ctx.user.id, ctx.role)
    try:
        yield ctx
    finally:
        end_request(session, authority)


async def get_current_user(ctx: AuthContext = Depends(get_auth_context)) -> User:
    return ctx.user


async def installation_root_user_id(session: AsyncSession) -> uuid.UUID | None:
    """Return the durable installation-owner identity."""
    return await session.scalar(
        select(User.id).where(User.is_install_owner.is_(True)).limit(1)
    )


async def is_install_owner(session: AsyncSession, user_id) -> bool:
    root_id = await installation_root_user_id(session)
    return root_id is not None and root_id == user_id


async def require_install_owner(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Require authority for installation-wide operations."""
    if not await is_install_owner(session, user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=t("auth.install_owner_required"),
        )
    return user


async def get_current_company_id(ctx: AuthContext = Depends(get_auth_context)) -> uuid.UUID:
    return ctx.company_id


async def get_current_role(ctx: AuthContext = Depends(get_auth_context)) -> str:
    """Return the authorization role from current DB membership (migrated)."""
    return ctx.role


# Sign-in refusal for a login with no active company left.
NO_COMPANY = "No active company membership"
# Locale key, translated where raised so the reader gets their own language.
HAS_COMPANY = "auth.has_company"


# The one rule for whether a login can work in a company: an active membership in an
# active company, or an owner's active membership in a deactivated or still-being-moved-in
# company, so the owner can reactivate it or finish the move.
USABLE_LINK = and_(UserCompany.is_active.is_(True),
                   or_(Company.is_active.is_(True), UserCompany.role == "owner"))
# Which usable link a sign-in lands on first: active, then still being moved in, then
# deactivated (an owner's, to reactivate).
_LANDING = case((and_(Company.is_active.is_(True), Company.is_migration_staged.is_(False)), 0),
                (Company.is_migration_staged.is_(True), 1), else_=2)


def _usable_links(user_id):
    return (select(UserCompany).join(Company, Company.id == UserCompany.company_id)
            .where(UserCompany.user_id == user_id, USABLE_LINK))


async def first_usable_company_link(session: AsyncSession, user_id) -> UserCompany | None:
    """The company a sign-in lands on: the user's first usable company link, or None when
    the login has no company it can work in.

    A user in several companies uses /switch-company afterwards. An active company
    comes first, then one still being moved in, and only then a deactivated company
    its owner can reactivate, so a sign-in lands on a working company whenever one
    exists."""
    return (await session.execute(
        _usable_links(user_id).order_by(_LANDING, UserCompany.id).limit(1)
    )).scalar_one_or_none()


async def hold_companyless_login(session: AsyncSession, user_id) -> bool:
    """Lock the login FOR UPDATE until the transaction ends, then say whether it has no
    company. The answer stays true until the transaction ends: another request holding
    the login (a second start, a backup restore) waits and then reads the membership this
    one committed, and adding a membership for the login from another transaction waits
    too, since its foreign key needs a share lock on the row. The same transaction still
    adds the login's own membership.

    Taken after the direct sign-in lock and before any company or ``UserAuthState`` row,
    and only by transactions that have not yet written a row referencing the login."""
    await session.execute(select(User.id).where(User.id == user_id).with_for_update())
    return await first_usable_company_link(session, user_id) is None


async def usable_company_link(session: AsyncSession, user_id, company_id) -> UserCompany | None:
    """The user's link to *company_id* when the login can work in that company, else None."""
    return (await session.execute(
        _usable_links(user_id).where(UserCompany.company_id == company_id)
    )).scalar_one_or_none()


class CompanyUnavailable(HTTPException):
    """The company a session was about to be issued for was removed, or the login can no
    longer work in it. Nothing was issued."""

    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")


async def lock_issuance_company(session: AsyncSession, user_id, company_id) -> UserCompany:
    """Hold *company_id* against removal until the transaction ends and return the user's
    usable link to it, or raise ``CompanyUnavailable``.

    The row is taken FOR KEY SHARE: a company reset takes it FOR UPDATE before deleting
    anything, so a session issued under this lock either commits before the reset starts
    (and the reset then ends it) or waits and finds the company gone. Lock order, kept by
    every issuance path: the direct sign-in lock, then the login FOR UPDATE when the path
    gives it a company (``hold_companyless_login``), then the company, then
    ``UserAuthState``."""
    link = await usable_company_link(session, user_id, company_id) if await hold_company(session, company_id) else None
    if link is None:
        raise CompanyUnavailable()
    return link


async def issue_token_pair(
    session: AsyncSession,
    *,
    user: User,
    company_id: uuid.UUID,
    jti: str | None = None,
    expected_snonce: str | None = None,
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

    *expected_snonce* distinguishes a continuation from a fresh credential:

    - A continuation (refresh, sliding refresh, switch-company, create-company)
      passes the snonce it authenticated on.  If it no longer equals the locked
      nonce, a concurrent revocation advanced the generation, so a neutral 401
      is raised BEFORE minting or registering any JTI - the continuation can
      never jump onto the newer generation (F2).
    - A fresh credential (register, password login, force-login) passes
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
