# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Every company a user can open, with its accountant sync, last sync and what needs
attention: the companies an accounting firm looks after, built on the existing users and
roles. Sync and attention are shown to each company's owner only."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.accounting import UserCompany
from celerp.models.company import Company
from celerp.models.company_peer import CompanyPeerGrant
from celerp.models.migration import MigrationRun, MigrationStatus
from celerp.services import company_handoff_sender as sender
from celerp.services import peer_attention
from celerp.services.peer_access import RENEW_WITHIN

SYNC_STATES = {"on": "On", "off": "Off", "failing": "Failing", "expiring": "Key expiring"}
INVALID_SYNC = "Choose a sync status: On, Off, Failing or Key expiring."
OWNER_ONLY = "Only the company's owner sees its sync and what needs attention."
KEY_PAGE = "/settings/accountant-access"
COPIES_PAGE = "/settings/company-handoff"
_FINISHED = {MigrationStatus.COMPLETED.value, MigrationStatus.CANCELLED.value}


def _instant(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _item(kind: str, label: str, reason: str, href: str | None = None,
          when: datetime | None = None) -> dict:
    return {"kind": kind, "label": label, "reason": reason, "href": href,
            "at": when.isoformat() if when else None}


def _keys(grants: list[CompanyPeerGrant], refused: bool, now: datetime) -> tuple[str, list[dict]]:
    """The company's sync status from the newest key of each connected company, and the
    keys that need renewing."""
    newest: dict[uuid.UUID, CompanyPeerGrant] = {}
    for grant in grants:
        held = newest.get(grant.source_company_id)
        if held is None or _instant(grant.created_at) > _instant(held.created_at):
            newest[grant.source_company_id] = grant
    if not newest:
        return "off", []
    items, expired, expiring = [], False, False
    for grant in sorted(newest.values(), key=lambda g: g.label):
        ends = _instant(grant.expires_at)
        if ends <= now:
            expired = True
            items.append(_item("key_expired", grant.label,
                               f"The key expired on {ends.date().isoformat()}, so this company "
                               "receives nothing from it. Renew the key to sync again.", KEY_PAGE))
        elif ends - now <= RENEW_WITHIN:
            expiring = True
            items.append(_item("key_expiring", grant.label,
                               f"The key expires on {ends.date().isoformat()}. Renew it before "
                               "then to keep syncing.", KEY_PAGE))
    status = "failing" if expired or refused else "expiring" if expiring else "on"
    return status, items


def _copy_items(copies: list[dict]) -> list[dict]:
    """Company copies sent and not opened yet, and copies whose link expired unopened."""
    items = []
    for copy in copies:
        recipient = copy.get("recipient") or "--"
        expires = str(copy.get("expires_at") or "")[:10] or "--"
        if copy.get("status") == "sent" and not copy.get("company_opened"):
            items.append(_item("copy_not_opened", recipient,
                               f"Sent and not opened yet. The link expires on {expires}.",
                               COPIES_PAGE))
        elif copy.get("status") == "expired" and not copy.get("company_opened"):
            items.append(_item("copy_expired", recipient,
                               f"The link expired on {expires} before the copy was opened. "
                               "Send a new copy if it is still needed.", COPIES_PAGE))
    return items


async def _copies(company_id: uuid.UUID) -> tuple[list[dict], str | None]:
    try:
        return _copy_items(await sender.company_copies(company_id)), None
    except sender.HandoffAccountNeeded:
        return [], None  # no account on this installation: it sends no copies
    except sender.HandoffSendError as exc:
        return [], str(exc)


def _matches(row: dict, q: str | None, sync: str | None) -> bool:
    if q and q.casefold() not in row["company_name"].casefold():
        return False
    return sync is None or row["sync"] == sync


def _activity(row: dict) -> str:
    stamps = [row["last_sync"] or ""] + [item["at"] or "" for item in row["attention"]]
    return max(stamps)


async def firm_view(session: AsyncSession, user_id: uuid.UUID, *, q: str | None = None,
                    sync: str | None = None) -> list[dict]:
    """The rows newest activity first: one per company the user can open, and one per
    company move the user started that has not finished."""
    if sync is not None and sync not in SYNC_STATES:
        raise HTTPException(422, INVALID_SYNC)
    q = (q or "").strip() or None
    now = datetime.now(timezone.utc)
    pairs = (await session.execute(
        select(Company, UserCompany.role).join(UserCompany, UserCompany.company_id == Company.id)
        .where(UserCompany.user_id == user_id, UserCompany.is_active == True,  # noqa: E712
               Company.is_active == True))).all()  # noqa: E712
    owned = [company.id for company, role in pairs if role == "owner"]
    grants = (await session.scalars(select(CompanyPeerGrant).where(
        CompanyPeerGrant.company_id.in_(owned), CompanyPeerGrant.revoked_at.is_(None)))).all() if owned else []
    attention = await peer_attention.open_items(session, owned)
    copies = dict(zip(owned, await asyncio.gather(*(_copies(cid) for cid in owned))))

    rows = []
    for company, role in pairs:
        row = {"company_id": str(company.id), "company_name": company.name, "role": role,
               "sync": None, "last_sync": None, "attention": [], "unavailable": []}
        if role != "owner":
            row["unavailable"] = [OWNER_ONLY]
            rows.append(row)
            continue
        own = [g for g in grants if g.company_id == company.id]
        refused = [_item(a.kind, a.label, a.reason, None, _instant(a.created_at))
                   for a in attention if a.company_id == company.id]
        row["sync"], key_items = _keys(own, bool(refused), now)
        used = [_instant(g.last_used_at) for g in own if g.last_used_at is not None]
        row["last_sync"] = max(used).isoformat() if used else None
        copy_items, unavailable = copies[company.id]
        row["attention"] = refused + key_items + copy_items
        row["unavailable"] = [unavailable] if unavailable else []
        rows.append(row)

    runs = (await session.execute(
        select(MigrationRun, Company.name).join(Company, Company.id == MigrationRun.company_id)
        .where(MigrationRun.created_by_user_id == user_id,
               MigrationRun.status.not_in(_FINISHED)))).all()
    for run, name in runs:
        rows.append({
            "company_id": str(run.company_id), "company_name": name, "role": None,
            "sync": None, "last_sync": None, "unavailable": [],
            "attention": [_item("migration", run.source_system,
                                f"The move from {run.source_system} is {run.status.replace('_', ' ')} "
                                "and has not finished.", f"/migrations/{run.id}",
                                _instant(run.created_at))]})

    rows = [row for row in rows if _matches(row, q, sync)]
    rows.sort(key=lambda row: row["company_name"].casefold())
    rows.sort(key=_activity, reverse=True)
    return rows
