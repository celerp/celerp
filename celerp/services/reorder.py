# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Reorder points, low-stock detection, and the daily proactive-alerts digest.

This is the single source of truth for "is this item below its reorder point".
`reorder_point` defaults to 0, so the predicate is byte-identical to the old
hardcoded ``quantity <= 0`` for any item that has never set a reorder point, and
becomes threshold-aware the moment a user sets one.

The alert loop is a daily scheduled scan (mirrors connectors/daily_scheduler.py),
not an event-driven trigger: it debounces naturally and needs no listener
infrastructure. It runs each check in the ``_CHECKS`` registry (low stock honours
its company on/off setting; expiring and overdue always run) and emits one combined
digest per company per day. Each check notifies once per dip via its own per-company
latch in ``Company.settings`` (e.g. ``reorder_alerted_ids``).
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
_EXPIRING_WINDOW_DAYS = 30          # expiring-lot look-ahead window

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


def group_by_supplier(items: list[dict]) -> dict[str, list[str]]:
    """Group item entity_ids by their preferred supplier so a reorder selection can
    become one draft PO per supplier. Items with no supplier group under '' (the
    caller renders that as an 'unassigned' draft). Order is preserved per group."""
    groups: dict[str, list[str]] = {}
    for it in items:
        sup = (it.get("preferred_supplier") or "").strip()
        eid = it.get("entity_id") or it.get("id")
        if eid:
            groups.setdefault(sup, []).append(eid)
    return groups


async def _projections(session: AsyncSession, company, entity_type: str) -> list:
    """All projection rows of one entity type for a company (shared fetch)."""
    from celerp.models.projections import Projection

    return (await session.execute(
        select(Projection).where(
            Projection.company_id == company.id,
            Projection.entity_type == entity_type,
        )
    )).scalars().all()


async def _detect_low_stock(session: AsyncSession, company) -> list[dict]:
    """Items at or below their reorder point (available stock only)."""
    rows = await _projections(session, company, "item")
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
    return below


async def _detect_expiring(session: AsyncSession, company) -> list[dict]:
    """In-stock items whose ``expires_at`` falls within the company's window."""
    from datetime import date

    cutoff = date.today() + timedelta(days=_EXPIRING_WINDOW_DAYS)
    rows = await _projections(session, company, "item")
    out: list[dict] = []
    for r in rows:
        st = r.state or {}
        if (st.get("status") or "available") != "available":
            continue
        if float(st.get("quantity", 0) or 0) <= 0:
            continue
        exp_str = st.get("expires_at")
        if not exp_str:
            continue
        try:
            exp = date.fromisoformat(str(exp_str)[:10])
        except (TypeError, ValueError):
            continue
        if exp <= cutoff:
            out.append({"entity_id": r.entity_id, "name": st.get("name") or st.get("sku"),
                        "expires_at": str(exp_str)[:10], "quantity": float(st.get("quantity", 0) or 0)})
    return out


async def _detect_overdue(session: AsyncSession, company) -> list[dict]:
    """Invoices past their due date with a positive outstanding balance."""
    from datetime import date

    today = date.today().isoformat()
    rows = await _projections(session, company, "doc")
    out: list[dict] = []
    for r in rows:
        st = r.state or {}
        if st.get("doc_type") != "invoice" or st.get("status") in ("void", "paid", "draft", "converted"):
            continue
        due = st.get("due_date")
        if not due or str(due)[:10] >= today:
            continue
        bal = float(st.get("amount_outstanding", st.get("total", 0)) or 0)
        if bal <= 0:
            continue
        out.append({"entity_id": r.entity_id, "name": st.get("ref_id") or r.entity_id,
                    "due_date": str(due)[:10], "balance": bal, "currency": st.get("currency", "USD")})
    return out


def _low_line(it: dict) -> str:
    return f"{it.get('name') or it.get('sku')} ({it['quantity']:g} on hand / reorder at {it['reorder_point']:g})"


def _exp_line(it: dict) -> str:
    return f"{it.get('name')} - expires {it['expires_at']} ({it['quantity']:g} on hand)"


def _ovd_line(it: dict) -> str:
    return f"{it['name']} - due {it['due_date']}, {it['currency']} {it['balance']:g} outstanding"


def _low_urgent(items: list[dict]) -> bool:
    """A digest is desktop-push urgent only when an item is actually out of stock
    (on-hand at or below zero) - the pre-existing low-stock severity rule."""
    return any(it["quantity"] <= 0 for it in items)


@dataclass(frozen=True)
class _AlertCheck:
    enabled_setting: str | None   # company on/off flag, or None for an always-on check
    latch_key: str
    label: str
    detect: object
    line: object
    urgent: object = None         # optional items -> bool; None == never desktop-urgent


# The registry - the one place a proactive check is declared. Adding a check is a
# row here + its detect function; the scan loop, latch, and digest need no change.
_CHECKS: tuple[_AlertCheck, ...] = (
    _AlertCheck("reorder_alerts_enabled", "reorder_alerted_ids", "Low stock", _detect_low_stock, _low_line, _low_urgent),
    _AlertCheck(None, "expiring_alerted_ids", "Expiring soon", _detect_expiring, _exp_line),
    _AlertCheck(None, "overdue_alerted_ids", "Overdue invoices", _detect_overdue, _ovd_line),
)


def _digest_lines(items: list[dict], line, limit: int = 5) -> str:
    out = [line(it) for it in items[:limit]]
    if len(items) > limit:
        out.append(f"…and {len(items) - limit} more")
    return "\n".join(out)


async def run_all_alerts(session: AsyncSession, company) -> object | None:
    """Run every enabled proactive check and, on newly-alerting items, emit ONE
    combined digest (notification + optional email). Idempotent per latch; each
    check re-arms independently when an item leaves its alert set."""
    from celerp.notifications import service as notif_service

    settings = dict(company.settings or {})
    sections: list[tuple[str, list[dict], object]] = []
    urgent = False
    for check in _CHECKS:
        if check.enabled_setting and not settings.get(check.enabled_setting, True):
            continue
        current = await check.detect(session, company)
        current_ids = {it["entity_id"] for it in current}
        latch = set(settings.get(check.latch_key) or [])
        newly = [it for it in current if it["entity_id"] not in latch]
        settings[check.latch_key] = sorted(current_ids)
        if newly:
            sections.append((check.label, newly, check.line))
            if check.urgent and check.urgent(newly):
                urgent = True

    settings["reorder_last_scan_at"] = datetime.now(timezone.utc).isoformat()
    company.settings = settings
    session.add(company)

    if not sections:
        await session.flush()
        return None

    total = sum(len(n) for _, n, _ in sections)
    title = f"{total} alert{'s' if total != 1 else ''} need attention"
    body = "\n\n".join(f"{label} ({len(newly)}):\n{_digest_lines(newly, line)}"
                       for label, newly, line in sections)
    notif = await notif_service.create(
        session, company.id, "inventory", title, body,
        action_url="/dashboard", priority="high" if urgent else "medium",
    )
    if settings.get("reorder_alert_email"):
        await _send_combined_email(session, company, title, sections)
    await session.flush()
    return notif


async def _send_combined_email(session: AsyncSession, company, title: str, sections) -> None:
    """Best-effort: email the owner one combined digest. Never raises."""
    try:
        from celerp.models.accounting import UserCompany
        from celerp.models.company import User
        from celerp.services.email import send_email

        recipient = (await session.execute(
            select(User.email).join(UserCompany, UserCompany.user_id == User.id).where(
                UserCompany.company_id == company.id,
                UserCompany.role == "owner",
                UserCompany.is_active.is_(True),
            ).limit(1)
        )).scalar()
        if not recipient:
            return
        blocks = ""
        for label, newly, line in sections:
            items = "".join(f"<li>{line(it)}</li>" for it in newly[:10])
            blocks += f"<h3>{label} ({len(newly)})</h3><ul>{items}</ul>"
        await send_email(recipient, title, f"<p>{title}.</p>{blocks}")
    except Exception:
        log.warning("alerts: combined digest email failed", exc_info=True)


async def reorder_alert_loop() -> None:
    """Background loop: once a day per company, run the proactive checks and alert.

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
                        await run_all_alerts(session, company)
                    except Exception as exc:
                        log.error("alerts: scan failed for %s: %s", company.id, exc)
                await session.commit()
        except Exception as exc:
            log.error("alerts: loop error: %s", exc)
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
