# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Reorder points, low-stock detection, and the daily low-stock alert digest.

This is the single source of truth for "is this item below its reorder point".
`reorder_point` defaults to 0, so the predicate is byte-identical to the old
hardcoded ``quantity <= 0`` for any item that has never set a reorder point, and
becomes threshold-aware the moment a user sets one.

The alert loop is a daily scheduled scan (mirrors connectors/daily_scheduler.py),
not an event-driven trigger: it debounces naturally and needs no listener
infrastructure. It notifies once per dip via a per-company latch stored in
``Company.settings["reorder_alerted_ids"]``.
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# Velocity-assist constants (used only to *suggest* a value; never stored).
_VELOCITY_WINDOW_DAYS = 90   # trailing window of outbound history to average over
_REVIEW_DAYS = 14            # a suggested reorder_qty covers one review cycle
_LEAD_DAYS = 7               # a suggested reorder_point covers supplier lead time
_SAFETY = 0                  # extra safety buffer on the suggested reorder_point

# Daily-scan gating (mirror the connector scheduler idiom).
_CHECK_INTERVAL_SECONDS = 3600      # loop wakes hourly
_MIN_HOURS_BETWEEN_SCANS = 23       # at most one digest per company per day
_SCAN_HOUR_UTC = 8                  # run the scan when the UTC hour matches

# Outbound event types and where each stores its quantity in the ledger `data`.
_OUTBOUND_QTY_KEYS = {
    "item.fulfilled": "quantity_fulfilled",
    "item.consumed": "quantity_consumed",
}


def reorder_point_of(state: dict) -> float:
    """The item's reorder point (0 == unset == no reorder rule)."""
    try:
        return float(state.get("reorder_point", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def is_below_reorder(state: dict) -> bool:
    """True when on-hand is at or below the reorder point.

    ``reorder_point`` defaults to 0, so this is identical to the legacy
    ``quantity <= 0`` for any item without a reorder point set.
    """
    try:
        qty = float(state.get("quantity", 0) or 0)
    except (TypeError, ValueError):
        qty = 0.0
    return qty <= reorder_point_of(state)


def is_reorder_alert(state: dict) -> bool:
    """True when an item should raise a *reorder* alert.

    Stricter than :func:`is_below_reorder`: an item with no reorder point set
    (rp == 0) that is merely at qty 0 is "out of stock" (already surfaced on the
    dashboard), not a configured reorder alert, so it is excluded here.
    """
    return reorder_point_of(state) > 0 and is_below_reorder(state)


async def suggest_reorder(
    session: AsyncSession, company_id: uuid.UUID, entity_id: str,
) -> dict[str, int | None]:
    """Compute a suggested reorder point / qty from trailing outbound velocity.

    Read-only helper - never stores anything. Degrades honestly: with no
    outbound history it returns ``{"reorder_point": None, "reorder_qty": None}``
    rather than fabricating a number.
    """
    from celerp.models.ledger import LedgerEntry

    since = datetime.now(timezone.utc) - timedelta(days=_VELOCITY_WINDOW_DAYS)
    rows = (await session.execute(
        select(LedgerEntry.event_type, LedgerEntry.data).where(
            LedgerEntry.company_id == company_id,
            LedgerEntry.entity_id == entity_id,
            LedgerEntry.event_type.in_(tuple(_OUTBOUND_QTY_KEYS)),
            LedgerEntry.ts >= since,
        )
    )).all()

    total_out = 0.0
    for event_type, data in rows:
        key = _OUTBOUND_QTY_KEYS.get(event_type)
        if not key:
            continue
        try:
            total_out += float((data or {}).get(key, 0) or 0)
        except (TypeError, ValueError):
            continue

    if total_out <= 0:
        return {"reorder_point": None, "reorder_qty": None}

    avg_daily_out = total_out / _VELOCITY_WINDOW_DAYS
    return {
        "reorder_point": int(math.ceil(avg_daily_out * _LEAD_DAYS)) + _SAFETY,
        "reorder_qty": int(math.ceil(avg_daily_out * _REVIEW_DAYS)),
    }


def _digest_body(items: list[dict], limit: int = 5) -> str:
    """First few ``name (on-hand / reorder_point)`` lines for the digest body."""
    lines = []
    for it in items[:limit]:
        name = it.get("name") or it.get("sku") or it.get("entity_id") or "Item"
        qty = it.get("quantity", 0)
        rp = it.get("reorder_point", 0)
        lines.append(f"{name} ({qty:g} / {rp:g})")
    if len(items) > limit:
        lines.append(f"…and {len(items) - limit} more")
    return "\n".join(lines)


async def run_reorder_scan(session: AsyncSession, company) -> object | None:
    """Scan one company for below-reorder items and, on a *new* dip, create one
    digest notification. Idempotent: re-running with no change notifies nothing.

    Returns the created Notification, or None when nothing new was alerted.
    Latch + timestamp are persisted on ``company.settings``.
    """
    from celerp.models.projections import Projection
    from celerp.notifications import service as notif_service

    settings = dict(company.settings or {})
    if not settings.get("reorder_alerts_enabled", True):
        return None

    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == company.id,
            Projection.entity_type == "item",
        )
    )).scalars().all()

    below: list[dict] = []
    for r in rows:
        st = r.state or {}
        if (st.get("status") or "available") != "available":
            continue
        if not is_reorder_alert(st):
            continue
        below.append({
            "entity_id": r.entity_id,
            "sku": st.get("sku"),
            "name": st.get("name"),
            "quantity": float(st.get("quantity", 0) or 0),
            "reorder_point": reorder_point_of(st),
        })

    below_ids = {it["entity_id"] for it in below}
    latch = set(settings.get("reorder_alerted_ids") or [])
    newly = [it for it in below if it["entity_id"] not in latch]

    # Re-arm: the new latch is exactly the currently-below set (items that rose
    # back above their reorder point drop out and can alert again on the next dip).
    settings["reorder_alerted_ids"] = sorted(below_ids)
    settings["reorder_last_scan_at"] = datetime.now(timezone.utc).isoformat()
    company.settings = settings
    session.add(company)

    if not newly:
        await session.flush()
        return None

    at_or_below_zero = any(it["quantity"] <= 0 for it in newly)
    title = f"{len(newly)} item{'s' if len(newly) != 1 else ''} need reordering"
    notif = await notif_service.create(
        session,
        company.id,
        "inventory",
        title,
        _digest_body(newly),
        action_url="/inventory?filter=low_stock",
        priority="high" if at_or_below_zero else "medium",
    )

    if settings.get("reorder_alert_email"):
        await _maybe_send_email(session, company, newly)

    await session.flush()
    return notif


async def _maybe_send_email(session: AsyncSession, company, items: list[dict]) -> None:
    """Best-effort: email the owner(s) the low-stock digest. Never raises."""
    try:
        from celerp.models.accounting import UserCompany
        from celerp.models.company import User
        from celerp.services.email import send_email

        recipient = (await session.execute(
            select(User.email)
            .join(UserCompany, UserCompany.user_id == User.id)
            .where(
                UserCompany.company_id == company.id,
                UserCompany.role == "owner",
                UserCompany.is_active.is_(True),
            )
            .limit(1)
        )).scalar()
        if not recipient:
            return
        rows_html = "".join(
            f"<li>{it.get('name') or it.get('sku')} "
            f"({it['quantity']:g} on hand / reorder at {it['reorder_point']:g})</li>"
            for it in items
        )
        body_html = (
            f"<p>{len(items)} item(s) have reached their reorder point.</p>"
            f"<ul>{rows_html}</ul>"
            f"<p>Review the low-stock list in Celerp to draft a purchase order.</p>"
        )
        await send_email(recipient, f"{len(items)} items need reordering", body_html)
    except Exception:
        log.warning("reorder: low-stock email failed", exc_info=True)


async def reorder_alert_loop() -> None:
    """Background loop: once a day per company, scan for low stock and alert.

    Started from the API lifespan. Gates on hour-of-day + a per-company
    ``reorder_last_scan_at`` so a restart can't double-fire and the digest lands
    at most once per day.
    """
    from celerp.db import get_session_ctx
    from celerp.models.company import Company

    while True:
        try:
            now = datetime.now(timezone.utc)
            async with get_session_ctx() as session:
                companies = (await session.execute(
                    select(Company).where(Company.is_active.is_(True))
                )).scalars().all()
                for company in companies:
                    if not _scan_due(company, now):
                        continue
                    try:
                        await run_reorder_scan(session, company)
                    except Exception as exc:
                        log.error("reorder: scan failed for %s: %s", company.id, exc)
                await session.commit()
        except Exception as exc:
            log.error("reorder: alert loop error: %s", exc)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


def _scan_due(company, now: datetime) -> bool:
    """True when this company is due for its daily scan at the current tick."""
    if now.hour != _SCAN_HOUR_UTC:
        return False
    last = (company.settings or {}).get("reorder_last_scan_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if now - last_dt < timedelta(hours=_MIN_HOURS_BETWEEN_SCANS):
                return False
        except (TypeError, ValueError):
            pass
    return True
