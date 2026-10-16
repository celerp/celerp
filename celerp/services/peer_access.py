# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company-scoped grants for bounded accountant integrations."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company_peer import CompanyPeerGrant
from celerp.services.company_lock import locked_company, lock_company

# Each entity the Celerp connector sends, and the grant scope that authorizes it.
ENTITY_SCOPE = {
    "contacts": "contacts", "products": "products", "documents": "documents",
    "attachments": "documents", "inventory": "inventory",
}
SCOPE_ORDER = tuple(dict.fromkeys(ENTITY_SCOPE.values()))
SCOPES = frozenset(SCOPE_ORDER)
MAX_ACTIVE = 5
MAX_DAYS = 90
# A key this close to expiry is renewed by the sender and warned about to the owner.
RENEW_WITHIN = timedelta(days=7)
# How long a rotated key keeps working, so a renewal reply lost on its way back
# never stops synchronization: the sender retries with the old key.
ROTATION_GRACE = timedelta(days=1)
EXPIRY_NOTICE = "notice.peer_keys_expiring"
PAIRING_PREFIX = "cpair1"
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


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical(value: str) -> uuid.UUID:
    try:
        uid = uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(401, "Invalid integration credentials") from None
    if str(uid) != value:
        raise HTTPException(401, "Invalid integration credentials")
    return uid


def _parse(token: object) -> tuple[uuid.UUID, uuid.UUID, str]:
    if not isinstance(token, str) or len(token) > 160:
        raise HTTPException(401, "Invalid integration credentials")
    bits = token.split(".")
    if len(bits) != 4 or bits[0] != "cp1":
        raise HTTPException(401, "Invalid integration credentials")
    company_id, grant_id = _canonical(bits[1]), _canonical(bits[2])
    encoded = bits[3]
    if len(encoded) != 43 or not all(c.isalnum() and c.isascii() or c in "_-" for c in encoded):
        raise HTTPException(401, "Invalid integration credentials")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=")
    except (ValueError, base64.binascii.Error):
        raise HTTPException(401, "Invalid integration credentials") from None
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
        raise HTTPException(401, "Invalid integration credentials")
    return company_id, grant_id, token


async def issue(session: AsyncSession, *, company_id: uuid.UUID, source_company_id: uuid.UUID,
                label: str, scopes: frozenset[str], lifetime_days: int = 30,
                now: datetime | None = None) -> tuple[CompanyPeerGrant, str]:
    if (not isinstance(label, str) or not 1 <= len(label.strip()) <= 128 or
            not scopes or not scopes <= SCOPES or
            not isinstance(lifetime_days, int) or isinstance(lifetime_days, bool) or
            not 1 <= lifetime_days <= MAX_DAYS):
        raise HTTPException(422, "Invalid integration grant")
    when = now or datetime.now(timezone.utc)
    company = await locked_company(session, company_id)
    if company is None or not company.is_active or company.is_migration_staged:
        raise HTTPException(404, "Company unavailable")
    await _within_limit(session, company_id, when)
    return await _new_grant(session, company_id=company_id, source_company_id=source_company_id,
                            label=label.strip(), scopes=scopes, lifetime_days=lifetime_days, when=when)


async def _within_limit(session: AsyncSession, company_id: uuid.UUID, when: datetime) -> None:
    """Refuse a further live key once MAX_ACTIVE are live; a key being rotated out is not counted."""
    count = await session.scalar(select(func.count()).select_from(CompanyPeerGrant).where(
        CompanyPeerGrant.company_id == company_id,
        CompanyPeerGrant.expires_at > when,
        CompanyPeerGrant.revoked_at.is_(None),
        CompanyPeerGrant.replaced_by_id.is_(None),
    ))
    if count >= MAX_ACTIVE:
        raise HTTPException(409, "Too many active integrations")


async def _new_grant(session: AsyncSession, *, company_id: uuid.UUID, source_company_id: uuid.UUID,
                     label: str, scopes, lifetime_days: int,
                     when: datetime) -> tuple[CompanyPeerGrant, str]:
    grant_id = uuid.uuid4()
    raw = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    credential = f"cp1.{company_id}.{grant_id}.{raw}"
    grant = CompanyPeerGrant(
        id=grant_id, company_id=company_id, source_company_id=source_company_id,
        label=label, scopes=sorted(scopes), lifetime_days=lifetime_days,
        token_hash=hashlib.sha256(credential.encode()).hexdigest(),
        created_at=when, expires_at=when + timedelta(days=lifetime_days),
    )
    session.add(grant)
    await session.flush()
    return grant, credential


async def authenticate(session: AsyncSession, credential: object, *,
                       source_company_id: uuid.UUID, scope: str,
                       now: datetime | None = None) -> CompanyPeerGrant:
    """Authenticate inside the domain transaction before mutating a company."""
    if scope not in SCOPES:
        raise HTTPException(403, "Integration operation unavailable")
    row = await held(session, credential, source_company_id=source_company_id, now=now)
    if scope not in row.scopes:
        raise HTTPException(401, "Invalid integration credentials")
    return row


async def held(session: AsyncSession, credential: object, *,
                source_company_id: uuid.UUID, now: datetime | None) -> CompanyPeerGrant:
    """The live grant this credential belongs to, with its company locked."""
    company_id, grant_id, exact = _parse(credential)
    digest = hashlib.sha256(exact.encode()).hexdigest()
    candidate = await session.scalar(select(CompanyPeerGrant.id).where(
        CompanyPeerGrant.id == grant_id, CompanyPeerGrant.token_hash == digest,
        CompanyPeerGrant.company_id == company_id))
    if candidate is None:
        raise HTTPException(401, "Invalid integration credentials")
    company = await locked_company(session, company_id)
    if company is None or not company.is_active or company.is_migration_staged:
        raise HTTPException(401, "Invalid integration credentials")
    row = await session.get(CompanyPeerGrant, grant_id, populate_existing=True)
    when = now or datetime.now(timezone.utc)
    if (row is None or row.company_id != company_id or row.source_company_id != source_company_id
            or row.revoked_at is not None or _instant(row.expires_at) <= _instant(when)
            or not hmac.compare_digest(row.token_hash, digest)):
        raise HTTPException(401, "Invalid integration credentials")
    return row


async def rotate(session: AsyncSession, credential: object, *, source_company_id: uuid.UUID,
                 now: datetime | None = None) -> tuple[CompanyPeerGrant, str]:
    """The sending company exchanges its live key for a new one with the same records and
    lifetime. The old key keeps working for ROTATION_GRACE; a retry with it (a lost reply)
    revokes the key issued before and issues another, so only the newest key survives."""
    when = now or datetime.now(timezone.utc)
    row = await held(session, credential, source_company_id=source_company_id, now=when)
    if row.replaced_by_id is not None:
        earlier = await session.get(CompanyPeerGrant, row.replaced_by_id)
        if earlier is not None and earlier.revoked_at is None:
            earlier.revoked_at = when
    grant, key = await _new_grant(
        session, company_id=row.company_id, source_company_id=row.source_company_id,
        label=row.label, scopes=row.scopes, lifetime_days=row.lifetime_days, when=when)
    row.replaced_by_id = grant.id
    row.expires_at = min(_instant(row.expires_at), when + ROTATION_GRACE)
    await session.flush()
    return grant, key


async def extend(session: AsyncSession, company_id: uuid.UUID, grant_id: uuid.UUID,
                 *, now: datetime | None = None) -> CompanyPeerGrant:
    """The owner renews a key in one click: the same key works for another lifetime,
    also after it expired, so the sender changes nothing."""
    company = await locked_company(session, company_id)
    if company is None:
        raise HTTPException(404, "Company unavailable")
    row = await session.get(CompanyPeerGrant, grant_id)
    if row is None or row.company_id != company_id:
        raise HTTPException(404, "Integration unavailable")
    if row.revoked_at is not None or row.replaced_by_id is not None:
        raise HTTPException(409, "This key was revoked or replaced. Issue a new key instead.")
    when = now or datetime.now(timezone.utc)
    if _instant(row.expires_at) <= _instant(when):
        await _within_limit(session, company_id, when)
    row.expires_at = when + timedelta(days=row.lifetime_days)
    await session.flush()
    return row


async def notify_expiring(session: AsyncSession, company_id: uuid.UUID,
                          *, now: datetime | None = None) -> None:
    """Keep one unmissable notice standing while any live key expires within RENEW_WITHIN,
    naming each key and its date; remove it once none does. Caller commits."""
    from celerp.notifications import service as notification_service
    from ui.i18n import t

    when = now or datetime.now(timezone.utc)
    rows = (await session.execute(select(CompanyPeerGrant).where(
        CompanyPeerGrant.company_id == company_id,
        CompanyPeerGrant.revoked_at.is_(None),
        CompanyPeerGrant.replaced_by_id.is_(None),
        CompanyPeerGrant.expires_at > when,
        CompanyPeerGrant.expires_at <= when + RENEW_WITHIN,
    ).order_by(CompanyPeerGrant.expires_at))).scalars().all()
    title = t(f"{EXPIRY_NOTICE}.title", "en")
    if not rows:
        await notification_service.clear_standing(session, company_id, "integrations", title)
        return
    keys = ", ".join(f"{row.label} ({_instant(row.expires_at).date().isoformat()})" for row in rows)
    await notification_service.notify_standing(
        session, company_id, "integrations", title,
        t(f"{EXPIRY_NOTICE}.body", "en", keys=keys),
        action_url="/settings/accountant-access",
        i18n={"title": f"{EXPIRY_NOTICE}.title", "body": f"{EXPIRY_NOTICE}.body",
              "params": {"keys": keys}})


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
