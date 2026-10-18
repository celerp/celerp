# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company-scoped grants for bounded accountant integrations."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company_peer import CompanyPeerGrant
from celerp.services.company_lock import locked_company, lock_company

# Each entity the Celerp connector sends, and the grant scope that authorizes it.
ENTITY_SCOPE = {
    "contacts": "contacts", "products": "products", "documents": "documents",
    "attachments": "documents", "inventory": "inventory",
    # The receiving company's own journals and lock date, fetched by the sending company,
    # and the sending company's journals sent back.
    "journals_in": "journals", "period_lock": "journals", "journals": "journals",
}
SCOPE_ORDER = tuple(dict.fromkeys(ENTITY_SCOPE.values()))
SCOPES = frozenset(SCOPE_ORDER)
# The receiving company's own routes a key opens, each with the entity whose scope it
# needs. A key reaches no other route.
PEER_ROUTES = {
    ("POST", "/crm/contacts/import/batch"): "contacts",
    ("POST", "/items/import/batch"): "products",
    ("POST", "/docs/import/batch"): "documents",
    ("POST", "/docs/{entity_id}/files"): "attachments",
    ("POST", "/lists/import/batch"): "inventory",
    ("GET", "/accounting/peer/journals"): "journals_in",
    ("GET", "/accounting/peer/lock"): "period_lock",
    ("POST", "/accounting/peer/journals"): "journals",
}
ENTITY_ROUTE = {entity: path for (_, path), entity in PEER_ROUTES.items()}
# The header naming the sending company on every request signed with its key.
SOURCE_HEADER = "X-Celerp-Source-Company"
UNAVAILABLE = "Integration operation unavailable"
MAX_ACTIVE = 5


@dataclass(frozen=True)
class PeerCaller:
    """The connected company a request comes from, kept so each record it carries is
    authenticated again in that record's own transaction."""
    credential: str
    source_company_id: uuid.UUID
    scope: str

# How long a key works, from issue and from each renewal.
KEY_LIFETIME = timedelta(days=365)
# A key's last use is written at most this often.
USED_EVERY = timedelta(minutes=1)
# Both companies are warned this long before a key expires.
RENEW_WITHIN = timedelta(days=14)
EXPIRY_NOTICE = "notice.peer_key_expiring"
INVALID = "Invalid integration credentials"
PAIRING_PREFIX = "cpair1"
LINK_PREFIX = "cplink1"
MAX_LABEL = 128


def pairing_code(company_id: uuid.UUID, name: str) -> str:
    """The code a sending company shares so the receiving owner never types its ID."""
    label = base64.urlsafe_b64encode(name.strip()[:MAX_LABEL].encode()).decode().rstrip("=")
    return f"{PAIRING_PREFIX}.{company_id}.{label}"


def parse_pairing_code(code: object) -> tuple[uuid.UUID, str]:
    """Return the sending company's ID and name, or raise ValueError for anything else."""
    if not isinstance(code, str) or len(code) > 1024:
        raise ValueError("Invalid pairing code")
    bits = code.strip().split(".")
    if len(bits) != 3 or bits[0] != PAIRING_PREFIX:
        raise ValueError("Invalid pairing code")
    company_id = uuid.UUID(bits[1])
    if str(company_id) != bits[1]:
        raise ValueError("Invalid pairing code")
    try:
        raw = base64.urlsafe_b64decode(bits[2] + "=" * (-len(bits[2]) % 4))
        name = raw.decode().strip()
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):
        raise ValueError("Invalid pairing code") from None
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != bits[2] or len(name) > MAX_LABEL:
        raise ValueError("Invalid pairing code")
    return company_id, name


def pairing_link(address: str, credential: str) -> str:
    """What the receiving owner gives the sending company: its HTTPS address and key in one
    string. Raises ValueError when either part is not valid."""
    if not isinstance(credential, str):
        raise ValueError("Invalid pairing link")
    encoded = base64.urlsafe_b64encode(address.encode()).decode().rstrip("=")
    link = f"{LINK_PREFIX}.{encoded}.{credential}"
    parse_pairing_link(link)
    return link


def parse_pairing_link(link: object) -> tuple[str, str]:
    """Return the address and key in a pairing link, or raise ValueError for anything else."""
    if not isinstance(link, str) or len(link) > 2048:
        raise ValueError("Invalid pairing link")
    prefix, _, rest = link.strip().partition(".")
    encoded, _, credential = rest.partition(".")
    if prefix != LINK_PREFIX or not credential.startswith("cp1."):
        raise ValueError("Invalid pairing link")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        address = raw.decode()
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):
        raise ValueError("Invalid pairing link") from None
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
        raise ValueError("Invalid pairing link")
    try:
        _parse(credential)
    except HTTPException:
        raise ValueError("Invalid pairing link") from None
    if not is_https_origin(address):
        raise ValueError("Invalid pairing link")
    return address, credential


def is_https_origin(address: str) -> bool:
    """An address a key may be sent to: https, a host, and nothing after it."""
    try:
        origin = urlsplit(address)
        return (origin.scheme == "https" and origin.hostname is not None
                and origin.port in (None, 443) and origin.username is None
                and origin.password is None and origin.path in ("", "/")
                and not origin.query and not origin.fragment)
    except ValueError:
        return False


def route_entity(method: str, path: str) -> str | None:
    """The entity a request to ``path`` sends, when it is one of PEER_ROUTES; a ``{name}``
    segment matches any one path segment."""
    parts = path.rstrip("/").split("/")
    for (verb, template), entity in PEER_ROUTES.items():
        expected = template.split("/")
        if verb == method and len(expected) == len(parts) and all(
                e == p or (e.startswith("{") and e.endswith("}") and p)
                for e, p in zip(expected, parts)):
            return entity
    return None


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical(value: str) -> uuid.UUID:
    try:
        uid = uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(401, INVALID) from None
    if str(uid) != value:
        raise HTTPException(401, INVALID)
    return uid


def _parse(token: object) -> tuple[uuid.UUID, uuid.UUID, str]:
    if not isinstance(token, str) or len(token) > 160:
        raise HTTPException(401, INVALID)
    bits = token.split(".")
    if len(bits) != 4 or bits[0] != "cp1":
        raise HTTPException(401, INVALID)
    company_id, grant_id = _canonical(bits[1]), _canonical(bits[2])
    encoded = bits[3]
    if len(encoded) != 43 or not all(c.isalnum() and c.isascii() or c in "_-" for c in encoded):
        raise HTTPException(401, INVALID)
    try:
        raw = base64.urlsafe_b64decode(encoded + "=")
    except (ValueError, base64.binascii.Error):
        raise HTTPException(401, INVALID) from None
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
        raise HTTPException(401, INVALID)
    return company_id, grant_id, token


def key_ids(credential: object) -> tuple[uuid.UUID, uuid.UUID]:
    """The company that issued a key and the key's grant, read from the key itself; the
    key is not checked."""
    company_id, grant_id, _ = _parse(credential)
    return company_id, grant_id


async def issue(session: AsyncSession, *, company_id: uuid.UUID, source_company_id: uuid.UUID,
                label: str, firm: str, scopes: frozenset[str],
                now: datetime | None = None) -> tuple[CompanyPeerGrant, str]:
    if (not isinstance(label, str) or not 1 <= len(label.strip()) <= 128 or
            not isinstance(firm, str) or not 1 <= len(firm.strip()) <= 160 or
            not scopes or not scopes <= SCOPES):
        raise HTTPException(422, "Invalid integration grant")
    when = now or datetime.now(timezone.utc)
    company = await locked_company(session, company_id)
    if company is None or not company.is_active or company.is_migration_staged:
        raise HTTPException(404, "Company unavailable")
    await _within_limit(session, company_id, when)
    grant_id = uuid.uuid4()
    raw = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    credential = f"cp1.{company_id}.{grant_id}.{raw}"
    grant = CompanyPeerGrant(
        id=grant_id, company_id=company_id, source_company_id=source_company_id,
        label=label.strip(), firm=firm.strip(), scopes=sorted(scopes),
        token_hash=hashlib.sha256(credential.encode()).hexdigest(),
        created_at=when, expires_at=when + KEY_LIFETIME,
    )
    session.add(grant)
    await session.flush()
    return grant, credential


async def _within_limit(session: AsyncSession, company_id: uuid.UUID, when: datetime) -> None:
    """Refuse a further live key once MAX_ACTIVE are live."""
    count = await session.scalar(select(func.count()).select_from(CompanyPeerGrant).where(
        CompanyPeerGrant.company_id == company_id,
        CompanyPeerGrant.expires_at > when,
        CompanyPeerGrant.revoked_at.is_(None),
    ))
    if count >= MAX_ACTIVE:
        raise HTTPException(409, "Too many active integrations")


async def authenticate(session: AsyncSession, credential: object, *,
                       source_company_id: uuid.UUID, scope: str,
                       now: datetime | None = None) -> CompanyPeerGrant:
    """Authenticate inside the domain transaction before mutating a company."""
    if scope not in SCOPES:
        raise HTTPException(403, UNAVAILABLE)
    row = await held(session, credential, source_company_id=source_company_id, now=now)
    if scope not in row.scopes:  # a live key, just not given this; its holder learns nothing new
        raise HTTPException(403, UNAVAILABLE)
    return row


async def held(session: AsyncSession, credential: object, *,
                source_company_id: uuid.UUID, now: datetime | None) -> CompanyPeerGrant:
    """The live grant this credential belongs to, with its company locked. A key that only
    expired is refused with the receiving company's name, so its holder knows whom to ask
    for a renewal; every other refusal says nothing."""
    company_id, grant_id, exact = _parse(credential)
    digest = hashlib.sha256(exact.encode()).hexdigest()
    candidate = await session.scalar(select(CompanyPeerGrant.id).where(
        CompanyPeerGrant.id == grant_id, CompanyPeerGrant.token_hash == digest,
        CompanyPeerGrant.company_id == company_id))
    if candidate is None:
        raise HTTPException(401, INVALID)
    company = await locked_company(session, company_id)
    if company is None or not company.is_active or company.is_migration_staged:
        raise HTTPException(401, INVALID)
    row = await session.get(CompanyPeerGrant, grant_id, populate_existing=True)
    if (row is None or row.company_id != company_id or row.source_company_id != source_company_id
            or row.revoked_at is not None or not hmac.compare_digest(row.token_hash, digest)):
        raise HTTPException(401, INVALID)
    if _instant(row.expires_at) <= _instant(now or datetime.now(timezone.utc)):
        raise HTTPException(401, {"code": "key_expired", "company_name": company.name})
    return row


def mark_used(grant: CompanyPeerGrant, now: datetime | None = None) -> None:
    """Note that the connected company reached this one with the key: its last sync."""
    when = now or datetime.now(timezone.utc)
    if grant.last_used_at is None or _instant(grant.last_used_at) <= when - USED_EVERY:
        grant.last_used_at = when


async def renew(session: AsyncSession, company_id: uuid.UUID, grant_id: uuid.UUID,
                *, now: datetime | None = None) -> CompanyPeerGrant:
    """The owner renews a key in one click: the same key works for a year from now, also
    after it expired, so the sending company changes nothing. Repeating it on the same day
    changes nothing further."""
    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(404, "Company unavailable")
    row = await session.get(CompanyPeerGrant, grant_id)
    if row is None or row.company_id != company_id:
        raise HTTPException(404, "Integration unavailable")
    if row.revoked_at is not None:
        raise HTTPException(409, "This key was revoked. Issue a new key instead.")
    when = now or datetime.now(timezone.utc)
    if _instant(row.expires_at) <= _instant(when):
        await _within_limit(session, company_id, when)
    row.expires_at = when + KEY_LIFETIME
    await session.flush()
    return row


async def notify_expiring(session: AsyncSession, company_id: uuid.UUID,
                          *, now: datetime | None = None) -> None:
    """Keep one unmissable notice standing per key that expires within RENEW_WITHIN or has
    expired and was not revoked, naming its company and date; remove each once renewed or
    revoked. Caller commits."""
    from celerp.notifications import service as notification_service
    from ui.i18n import t

    when = now or datetime.now(timezone.utc)
    rows = (await session.execute(select(CompanyPeerGrant).where(
        CompanyPeerGrant.company_id == company_id).order_by(
        CompanyPeerGrant.expires_at))).scalars().all()
    due = [row for row in rows
           if row.revoked_at is None and _instant(row.expires_at) <= when + RENEW_WITHIN]
    standing = set()
    for row in due:
        params = {"company": row.label, "date": _instant(row.expires_at).date().isoformat()}
        body = f"{EXPIRY_NOTICE}.{'expired' if _instant(row.expires_at) <= when else 'body'}"
        title = t(f"{EXPIRY_NOTICE}.title", "en", **params)
        standing.add(title)
        await notification_service.notify_standing(
            session, company_id, "integrations", title, t(body, "en", **params),
            action_url="/settings/accountant-access",
            i18n={"title": f"{EXPIRY_NOTICE}.title", "body": body, "params": params})
    for row in rows:
        title = t(f"{EXPIRY_NOTICE}.title", "en", company=row.label)
        if title not in standing:
            await notification_service.clear_standing(session, company_id, "integrations", title)


async def expiry_warning_loop(interval_seconds: int = 3600) -> None:
    """Background loop: every hour, warn each company whose keys expire soon."""
    import asyncio
    import logging

    from celerp.db import get_session_ctx

    log = logging.getLogger(__name__)
    while True:
        try:
            async with get_session_ctx() as session:
                companies = (await session.execute(
                    select(CompanyPeerGrant.company_id).distinct())).scalars().all()
                for company_id in companies:
                    await notify_expiring(session, company_id)
                await session.commit()
        except Exception:
            log.exception("company key expiry check failed")
        await asyncio.sleep(interval_seconds)


async def revoke(session: AsyncSession, company_id: uuid.UUID, grant_id: uuid.UUID,
                 *, now: datetime | None = None) -> None:
    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(404, "Company unavailable")
    row = await session.get(CompanyPeerGrant, grant_id)
    if row is None or row.company_id != company_id:
        raise HTTPException(404, "Integration unavailable")
    if row.revoked_at is None:
        row.revoked_at = now or datetime.now(timezone.utc)
        await session.flush()
