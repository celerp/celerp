# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from ipaddress import ip_address, ip_network

import bcrypt
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

# The UI server calls this API over loopback on a visitor's behalf and names the visitor's
# address in this header, so a rate limit counts that visitor rather than the UI itself.
CALLER_HEADER = "X-Celerp-Caller"

# The Connect gateway marks every request it proxies, and the UI server passes the
# mark on to the API.
CONNECT_HEADER = "X-Celerp-Connect"

# The relay names each Connect visitor in this header by the address it saw them from.
# Mirrors the relay's constant, which is authoritative.
VISITOR_HEADER = "X-Celerp-Visitor"

# The caller every Connect visitor without a valid address counts as: never the person
# at this computer, and never an address the visitor chose.
CONNECT_CALLER = "connect"

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


# Accounts restored from backups made before the current scheme carry a bcrypt hash.
# They are checked with bcrypt directly (passlib's bcrypt handler does not load with
# the installed bcrypt) and replaced with the current scheme on the next sign-in.
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def verify_password(plain: str, hashed: str) -> bool:
    """Whether ``plain`` matches ``hashed``. An unreadable hash never matches."""
    if hashed.startswith(_BCRYPT_PREFIXES):
        # bcrypt only ever read the first 72 bytes when these hashes were made.
        try:
            return bcrypt.checkpw(plain.encode()[:72], hashed.encode())
        except ValueError:
            return False
    try:
        return pwd_context.verify(plain, hashed)
    except (ValueError, TypeError):
        return False


# Failed password checks slow the next try down, never shut anyone out: an account takes
# FREE_ACCOUNT_FAILURES from every caller together and a caller FREE_CALLER_FAILURES across
# every account, then each further failure doubles the wait from one second up to
# MAX_WAIT_SECONDS. A key quiet for FORGET_AFTER_SECONDS starts again, and so does an
# account whose password has changed. A loopback caller
# names no one visitor (the desktop user and any proxy on this machine arrive from it, while
# each Connect visitor is named apart), so it has no budget of its own; the account's budget
# still holds.
FREE_ACCOUNT_FAILURES = 5
FREE_CALLER_FAILURES = 20
MAX_WAIT_SECONDS = 300
FORGET_AFTER_SECONDS = 3600
_clock = time.monotonic


@dataclass
class _Tally:
    free: int
    # The password the account's failures were counted against; a new one starts the count again.
    auth_hash: str | None = None
    failures: int = 0
    last_failure: float = 0.0

    def open_at(self) -> float:
        """When the next attempt may start."""
        if self.failures < self.free:
            return self.last_failure
        return self.last_failure + min(2 ** min(self.failures - self.free, 16), MAX_WAIT_SECONDS)


class PasswordAttempts:
    """Every password check, counted per account and per caller.

    The API runs as one process (celerp.capacity), so the counts live in it, and a check is
    admitted and counted as a failure in one step under one lock before the password is
    read: of many attempts arriving at once only the ones the counts allow are checked.
    The right password then takes its count back."""

    def __init__(self) -> None:
        self._tallies: dict[str, _Tally] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._tallies.clear()

    def check(self, email: str, password: str, auth_hash: str | None, caller: str | None) -> bool:
        """Whether *password* is the one *auth_hash* holds, as one attempt on the account
        *email* names from *caller*; no hash never matches. 429 with Retry-After, and the
        password left unread, while the account or the caller must wait."""
        keys = [(_account(email), FREE_ACCOUNT_FAILURES, auth_hash)]
        if caller and not is_loopback(caller):
            keys.append((f"caller|{caller}", FREE_CALLER_FAILURES, None))
        now = _clock()
        with self._lock:
            tallies = [self._tally(key, free, held, now) for key, free, held in keys]
            wait = max(tally.open_at() - now for tally in tallies)
            if wait > 0:
                seconds = math.ceil(wait)
                raise HTTPException(
                    status_code=429, headers={"Retry-After": str(seconds)},
                    detail=t("auth.sign_ins_paused_one") if seconds == 1 else t("auth.sign_ins_paused", seconds=seconds))
            for tally in tallies:
                tally.failures += 1
                tally.last_failure = now
            if len(self._tallies) > 10_000:
                self._sweep(now)
        if not (auth_hash and verify_password(password, auth_hash)):
            return False
        with self._lock:
            self._tallies.pop(keys[0][0], None)
            if len(keys) > 1 and (tally := self._tallies.get(keys[1][0])):
                tally.failures = max(tally.failures - 1, 0)
        return True

    def _tally(self, key: str, free: int, auth_hash: str | None, now: float) -> _Tally:
        tally = self._tallies.get(key)
        changed = None not in (auth_hash, tally and tally.auth_hash) and tally.auth_hash != auth_hash
        if tally is None or now - tally.last_failure >= FORGET_AFTER_SECONDS or changed:
            tally = self._tallies[key] = _Tally(free, auth_hash)
        elif auth_hash is not None:
            tally.auth_hash = auth_hash
        return tally

    def _sweep(self, now: float) -> None:
        for key in [k for k, v in self._tallies.items() if now - v.last_failure >= FORGET_AFTER_SECONDS]:
            del self._tallies[key]


def _account(email: str) -> str:
    return f"account|{str(email or '').strip().lower()}"


password_attempts = PasswordAttempts()


def password_needs_rehash(hashed: str) -> bool:
    """Whether ``hashed`` is a legacy hash to replace with the current scheme."""
    return hashed.startswith(_BCRYPT_PREFIXES)


def hash_password(password: str) -> str:
    # Final backend invariant: no hash is ever produced for a sub-policy password,
    # so a future caller cannot silently bypass the length rule.
    validate_password(password)
    return pwd_context.hash(password)


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
    # Set when a connected company's integration key signed the request (peer_access).
    peer: object | None = None


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
# The signed access token a request was authenticated with, kept on its session.
SIGNED_TOKEN = "celerp_signed_access_token"


async def get_auth_context(
    request: Request,
    token: str | None = Depends(oauth2_scheme_optional),
    session: AsyncSession = Depends(get_session),
) -> AsyncIterator[AuthContext]:
    """FastAPI dependency: the one validated auth context for the request.

    FastAPI dependency caching resolves this once per request even when a route
    asks for user, company_id and role separately.

    A token scoped to a migration-staged company is isolated here, centrally: it
    reaches the migration routes only. The same user's tokens for other companies
    are unaffected.

    A module's routes answer only for a company that uses the module.

    A connected company's integration key (``Authorization: Peer <key>``) reaches only
    the routes peer_access.PEER_ROUTES lists, acting as the receiving company's owner.
    """
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    if scheme == "Peer":
        ctx, token = await _peer_context(request, session, credential), None
    elif token:
        ctx = await validate_access_token(session, token)
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})
    if ctx.company.is_migration_staged and not request.url.path.startswith(STAGED_ALLOWED_PREFIX):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=t("auth.company_staged"))
    from celerp.modules.boundary import require_company_uses
    from celerp.modules.loader import reads_when_off, route_module

    module = route_module(request.scope)
    if module and reads_when_off(request.scope, module):
        module = None
    if module:
        require_company_uses(ctx.company.settings, module)
    # The authority the request starts with is judged again under the company lock
    # (company_lock), so a write that waits there never runs on revoked access or
    # on a module the company has since turned off.
    from celerp.services.permissions import authorize_request, end_request
    authority = authorize_request(session, ctx.company_id, ctx.user.id, ctx.role, module)
    if token is not None:
        session.info[SIGNED_TOKEN] = token
    request.state.company_id = ctx.company_id  # the company a refused request places stock for (OlderStockMiddleware)
    try:
        yield ctx
    finally:
        end_request(session, authority)
        if token is not None and session.info.get(SIGNED_TOKEN) is token:
            session.info.pop(SIGNED_TOKEN, None)


async def _peer_context(request: Request, session: AsyncSession, credential: str) -> AuthContext:
    """The context of a request signed with a connected company's key: the key must hold
    the scope of the route it calls, and the request acts as the company's owner."""
    from celerp.services import peer_access

    entity = peer_access.route_entity(request.method, request.url.path)
    if entity is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=peer_access.UNAVAILABLE)
    try:
        source = uuid.UUID(request.headers.get(peer_access.SOURCE_HEADER, ""))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=peer_access.INVALID) from None
    scope = peer_access.ENTITY_SCOPE[entity]
    grant = await peer_access.authenticate(session, credential, source_company_id=source, scope=scope)
    company = await session.get(Company, grant.company_id)
    user = await session.scalar(select(User).join(UserCompany, UserCompany.user_id == User.id).where(
        UserCompany.company_id == company.id, UserCompany.role == "owner",
        UserCompany.is_active.is_(True), User.is_active.is_(True)).order_by(User.created_at, User.id).limit(1))
    if user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=peer_access.UNAVAILABLE)
    return AuthContext(claims={}, user=user, company=company, company_id=company.id, role="owner",
                       snonce="", peer=peer_access.PeerCaller(credential, source, scope))


async def signed_request_context(session: AsyncSession) -> AuthContext | None:
    """The caller of the request *session* serves, validated again from the signed
    access token get_auth_context accepted; None when there is no such request."""
    token = session.info.get(SIGNED_TOKEN)
    return await validate_access_token(session, token) if isinstance(token, str) else None


def is_loopback(address: str) -> bool:
    try:
        return ip_address(address).is_loopback
    except ValueError:
        return False


def _ip(address: str):
    """The one IP address *address* names, an IPv4-mapped IPv6 one as its IPv4 address;
    None when it names anything else."""
    try:
        ip = ip_address(address.strip())
    except ValueError:
        return None
    return getattr(ip, "ipv4_mapped", None) or ip


def _counted_as(address: str) -> str:
    """The caller an address counts as: an IPv6 address by its /64, which one visitor holds
    whole, so stepping through it is not a new caller each time. A loopback address stays
    itself."""
    ip = _ip(address)
    if ip is None:
        return address
    if ip.version == 6 and not ip.is_loopback:
        return str(ip_network(f"{ip}/64", strict=False))
    return str(ip)


def visitor_address(value: str | None) -> str | None:
    """The address a Connect visitor is named by, from the one value the relay set; None
    unless it is one valid address that is not this computer's, so no visitor can count as
    the person at it."""
    ip = _ip(value) if value else None
    if ip is None or ip.is_loopback or ip.is_unspecified:
        return None
    return str(ip)


def connect_caller(values: list[str]) -> str:
    """The caller a Connect visitor is named as from the address values the request carries:
    the address when there is exactly one valid one, else the Connect caller every unnamed
    visitor shares."""
    return (visitor_address(values[0]) if len(values) == 1 else None) or CONNECT_CALLER


def request_caller(request: Request) -> str:
    """The caller a request is counted for. CALLER_HEADER counts only on a loopback request,
    where it comes from the UI server or the Connect gateway, so no one reaching the API from
    elsewhere can name an address of their choosing. A loopback request with the Connect mark
    is a visitor, never the person at this computer, whatever address it names."""
    host = request.client.host if request.client else ""
    if not is_loopback(host):
        return _counted_as(host)
    if CONNECT_HEADER in request.headers:
        return _counted_as(connect_caller(request.headers.getlist(CALLER_HEADER)))
    return _counted_as(request.headers.get(CALLER_HEADER) or host)


def is_local_person(request: Request) -> bool:
    """Whether the request comes from the person at this computer rather than through Connect."""
    return is_loopback(request_caller(request)) and CONNECT_HEADER not in request.headers


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


