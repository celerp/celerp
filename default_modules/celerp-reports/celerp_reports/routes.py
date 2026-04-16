# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.models.projections import Projection
from celerp.services.auth import get_current_company_id, get_current_user, require_manager

router = APIRouter(dependencies=[Depends(get_current_user)])


def _parse_d(val: str | None) -> Decimal:
    if val is None:
        return Decimal(0)
    try:
        return Decimal(str(val))
    except Exception:
        return Decimal(0)


def _in_range(ts: str | None, date_from: str | None, date_to: str | None) -> bool:
    if not ts:
        return True
    if date_from and ts < date_from:
        return False
    if date_to and ts > date_to + "T23:59:59":
        return False
    return True


# ---------------------------------------------------------------------------
# AR aging (moved from docs router for co-location)
# ---------------------------------------------------------------------------

@router.get("/ar-aging")
async def ar_aging(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Accounts receivable aging buckets per customer."""
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "doc",
            )
        )
    ).scalars().all()

    today = date.today()

    # customer_id -> {current, d30, d60, d90, d90plus}
    buckets: dict[str, dict[str, Decimal]] = defaultdict(lambda: {
        "current": Decimal(0), "d30": Decimal(0), "d60": Decimal(0),
        "d90": Decimal(0), "d90plus": Decimal(0),
    })
    names: dict[str, str] = {}

    for row in rows:
        state = row.state
        doc_type = state.get("doc_type", state.get("type", ""))
        if doc_type not in ("invoice", "Invoice"):
            continue
        status = state.get("status", "")
        if status in ("void", "paid"):
            continue
        outstanding = _parse_d(state.get("amount_outstanding") or state.get("total"))
        if outstanding <= 0:
            continue

        customer_id = state.get("contact_id") or state.get("customer_id") or "unlinked"
        names[customer_id] = state.get("contact_name") or state.get("customer_name") or "Unlinked Invoices"

        due_date_str = state.get("due_date") or state.get("date") or today.isoformat()
        try:
            due = date.fromisoformat(due_date_str[:10])
        except ValueError:
            due = today
        days_overdue = (today - due).days

        b = buckets[customer_id]
        if days_overdue <= 0:
            b["current"] += outstanding
        elif days_overdue <= 30:
            b["d30"] += outstanding
        elif days_overdue <= 60:
            b["d60"] += outstanding
        elif days_overdue <= 90:
            b["d90"] += outstanding
        else:
            b["d90plus"] += outstanding

    lines = []
    for cid, b in sorted(buckets.items(), key=lambda x: names.get(x[0], x[0])):
        total = sum(b.values())
        lines.append({
            "customer_id": cid,
            "customer_name": names.get(cid, cid),
            "current": float(b["current"]),
            "d30": float(b["d30"]),
            "d60": float(b["d60"]),
            "d90": float(b["d90"]),
            "d90plus": float(b["d90plus"]),
            "total": float(total),
        })

    # Aggregate bucket totals for chart display
    agg_buckets = {"current": 0.0, "1-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    for b in buckets.values():
        agg_buckets["current"] += float(b["current"])
        agg_buckets["1-30"] += float(b["d30"])
        agg_buckets["31-60"] += float(b["d60"])
        agg_buckets["61-90"] += float(b["d90"])
        agg_buckets["90+"] += float(b["d90plus"])

    return {"as_of": today.isoformat(), "lines": lines, "buckets": agg_buckets}


# ---------------------------------------------------------------------------
# AP aging
# ---------------------------------------------------------------------------

@router.get("/ap-aging")
async def ap_aging(
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Accounts payable aging buckets per supplier (from purchase orders)."""
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "doc",
            )
        )
    ).scalars().all()

    today = date.today()

    buckets: dict[str, dict[str, Decimal]] = defaultdict(lambda: {
        "current": Decimal(0), "d30": Decimal(0), "d60": Decimal(0),
        "d90": Decimal(0), "d90plus": Decimal(0),
    })
    names: dict[str, str] = {}

    for row in rows:
        state = row.state
        doc_type = state.get("doc_type", state.get("type", ""))
        if doc_type not in ("purchase_order", "PO"):
            continue
        status = state.get("status", "")
        if status in ("void", "received"):
            continue
        outstanding = _parse_d(state.get("amount_outstanding") or state.get("total"))
        if outstanding <= 0:
            continue

        supplier_id = state.get("contact_id") or state.get("supplier_id") or "Unlinked"
        names[supplier_id] = state.get("contact_name") or state.get("supplier_name") or supplier_id

        due_date_str = state.get("due_date") or state.get("expected_delivery") or today.isoformat()
        try:
            due = date.fromisoformat(due_date_str[:10])
        except ValueError:
            due = today
        days_overdue = (today - due).days

        b = buckets[supplier_id]
        if days_overdue <= 0:
            b["current"] += outstanding
        elif days_overdue <= 30:
            b["d30"] += outstanding
        elif days_overdue <= 60:
            b["d60"] += outstanding
        elif days_overdue <= 90:
            b["d90"] += outstanding
        else:
            b["d90plus"] += outstanding

    lines = []
    for sid, b in sorted(buckets.items(), key=lambda x: names.get(x[0], x[0])):
        total = sum(b.values())
        lines.append({
            "supplier_id": sid,
            "supplier_name": names.get(sid, sid),
            "current": float(b["current"]),
            "d30": float(b["d30"]),
            "d60": float(b["d60"]),
            "d90": float(b["d90"]),
            "d90plus": float(b["d90plus"]),
            "total": float(total),
        })

    return {"as_of": today.isoformat(), "lines": lines}


# ---------------------------------------------------------------------------
# Sales report
# ---------------------------------------------------------------------------

@router.get("/sales")
async def sales_report(
    group_by: str = "customer",  # customer | item | period
    period: str = "monthly",     # daily | weekly | monthly (for group_by=period)
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Sales report from invoice projections.

    group_by=customer: revenue/cost/profit per customer.
    group_by=item: revenue/cost/profit per item (from line items).
    group_by=period: revenue/cost/profit aggregated by day/week/month.
    """
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "doc",
            )
        )
    ).scalars().all()

    # Only finalized (non-void, non-draft) invoices
    invoices = [
        r.state for r in rows
        if r.state.get("doc_type", r.state.get("type", "")) in ("invoice", "Invoice")
        and r.state.get("status", "") not in ("void", "draft")
        and _in_range(r.state.get("date") or r.state.get("created_at"), date_from, date_to)
    ]

    if group_by == "customer":
        data: dict[str, dict] = defaultdict(lambda: {
            "invoice_count": 0, "total_revenue": Decimal(0),
            "total_cost": Decimal(0), "name": "",
        })
        for inv in invoices:
            cid = inv.get("contact_id") or inv.get("customer_id") or "Unlinked"
            data[cid]["name"] = inv.get("contact_name") or inv.get("customer_name") or cid
            data[cid]["invoice_count"] += 1
            data[cid]["total_revenue"] += _parse_d(inv.get("total"))
            data[cid]["total_cost"] += _parse_d(inv.get("cost_total"))
        lines = []
        for cid, d in sorted(data.items(), key=lambda x: -x[1]["total_revenue"]):
            rev = float(d["total_revenue"])
            cost = float(d["total_cost"])
            gp = rev - cost
            lines.append({
                "customer_id": cid,
                "customer_name": d["name"],
                "invoice_count": d["invoice_count"],
                "total_revenue": rev,
                "total_cost": cost,
                "gross_profit": gp,
                "margin_pct": round(gp / rev * 100, 1) if rev else 0,
            })

    elif group_by == "item":
        data = defaultdict(lambda: {
            "qty_sold": Decimal(0), "total_revenue": Decimal(0),
            "total_cost": Decimal(0), "name": "",
        })
        for inv in invoices:
            for line in inv.get("line_items", inv.get("items", [])):
                iid = line.get("item_id") or line.get("entity_id") or "Unlinked"
                data[iid]["name"] = line.get("name") or line.get("description") or iid
                data[iid]["qty_sold"] += _parse_d(line.get("quantity") or 1)
                data[iid]["total_revenue"] += _parse_d(line.get("line_total") or line.get("price") or 0)
                data[iid]["total_cost"] += _parse_d(line.get("cost_total") or 0)
        lines = []
        for iid, d in sorted(data.items(), key=lambda x: -x[1]["qty_sold"]):
            rev = float(d["total_revenue"])
            cost = float(d["total_cost"])
            gp = rev - cost
            lines.append({
                "item_id": iid,
                "item_name": d["name"],
                "qty_sold": float(d["qty_sold"]),
                "total_revenue": rev,
                "total_cost": cost,
                "gross_profit": gp,
                "avg_price": round(rev / float(d["qty_sold"]), 2) if d["qty_sold"] else 0,
            })

    elif group_by == "price_range":
        _BUCKETS = [(0, 1000, "0-1000"), (1001, 5000, "1001-5000"), (5001, 20000, "5001-20000"), (20001, None, "20000+")]
        data_pr: dict[str, dict] = {label: {"invoice_count": 0, "total_revenue": Decimal(0), "total_cost": Decimal(0)} for _, _, label in _BUCKETS}
        for inv in invoices:
            total = _parse_d(inv.get("total"))
            label = _BUCKETS[-1][2]
            for lo, hi, lbl in _BUCKETS:
                if hi is None or total <= hi:
                    label = lbl
                    break
            data_pr[label]["invoice_count"] += 1
            data_pr[label]["total_revenue"] += total
            data_pr[label]["total_cost"] += _parse_d(inv.get("cost_total"))
        lines = []
        for _, _, label in _BUCKETS:
            d_pr = data_pr[label]
            rev = float(d_pr["total_revenue"])
            cost = float(d_pr["total_cost"])
            gp = rev - cost
            lines.append({
                "price_range": label,
                "invoice_count": d_pr["invoice_count"],
                "total_revenue": rev,
                "total_cost": cost,
                "gross_profit": gp,
            })

    else:  # period
        def _period_key(ts: str) -> str:
            try:
                d = date.fromisoformat(ts[:10])
            except ValueError:
                return "unknown"
            if period == "daily":
                return d.isoformat()
            elif period == "weekly":
                # ISO week start (Monday)
                return (d - timedelta(days=d.weekday())).isoformat()
            else:  # monthly
                return d.strftime("%Y-%m")

        data = defaultdict(lambda: {
            "invoice_count": 0, "total_revenue": Decimal(0), "total_cost": Decimal(0),
        })
        for inv in invoices:
            ts = inv.get("date") or inv.get("created_at") or ""
            key = _period_key(ts)
            data[key]["invoice_count"] += 1
            data[key]["total_revenue"] += _parse_d(inv.get("total"))
            data[key]["total_cost"] += _parse_d(inv.get("cost_total"))
        lines = []
        for key in sorted(data):
            rev = float(data[key]["total_revenue"])
            cost = float(data[key]["total_cost"])
            gp = rev - cost
            lines.append({
                "period": key,
                "invoice_count": data[key]["invoice_count"],
                "total_revenue": rev,
                "total_cost": cost,
                "gross_profit": gp,
            })

    total_revenue = sum(l["total_revenue"] for l in lines)
    total_cost = sum(l.get("total_cost", 0) for l in lines)

    return {
        "group_by": group_by,
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "lines": lines,
        "total_revenue": total_revenue,
        "total_cost": total_cost,
        "gross_profit": total_revenue - total_cost,
    }


# ---------------------------------------------------------------------------
# Purchasing report
# ---------------------------------------------------------------------------

@router.get("/purchases")
async def purchases_report(
    group_by: str = "supplier",  # supplier | item | period
    period: str = "monthly",
    date_from: str | None = None,
    date_to: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id), _: None = Depends(require_manager),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Purchasing report from purchase order projections."""
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "doc",
            )
        )
    ).scalars().all()

    pos = [
        r.state for r in rows
        if r.state.get("doc_type", r.state.get("type", "")) in ("purchase_order", "PO")
        and r.state.get("status", "") not in ("void", "draft")
        and _in_range(r.state.get("date") or r.state.get("created_at"), date_from, date_to)
    ]

    if group_by == "supplier":
        data: dict[str, dict] = defaultdict(lambda: {
            "po_count": 0, "total_spend": Decimal(0), "name": "",
        })
        for po in pos:
            sid = po.get("contact_id") or po.get("supplier_id") or "Unlinked"
            data[sid]["name"] = po.get("contact_name") or po.get("supplier_name") or sid
            data[sid]["po_count"] += 1
            data[sid]["total_spend"] += _parse_d(po.get("total"))
        lines = [
            {
                "supplier_id": sid,
                "supplier_name": d["name"],
                "po_count": d["po_count"],
                "total_spend": float(d["total_spend"]),
            }
            for sid, d in sorted(data.items(), key=lambda x: -x[1]["total_spend"])
        ]

    elif group_by == "item":
        data = defaultdict(lambda: {
            "qty_purchased": Decimal(0), "total_spend": Decimal(0), "name": "",
        })
        for po in pos:
            for line in po.get("line_items", po.get("items", [])):
                iid = line.get("item_id") or line.get("entity_id") or "Unlinked"
                data[iid]["name"] = line.get("name") or line.get("description") or iid
                data[iid]["qty_purchased"] += _parse_d(line.get("quantity") or 1)
                data[iid]["total_spend"] += _parse_d(line.get("line_total") or line.get("price") or 0)
        lines = [
            {
                "item_id": iid,
                "item_name": d["name"],
                "qty_purchased": float(d["qty_purchased"]),
                "total_spend": float(d["total_spend"]),
                "avg_unit_cost": round(float(d["total_spend"]) / float(d["qty_purchased"]), 2) if d["qty_purchased"] else 0,
            }
            for iid, d in sorted(data.items(), key=lambda x: -x[1]["total_spend"])
        ]

    elif group_by == "price_range":
        _BUCKETS_PO = [(0, 1000, "0-1000"), (1001, 5000, "1001-5000"), (5001, 20000, "5001-20000"), (20001, None, "20000+")]
        data_pr_po: dict[str, dict] = {label: {"po_count": 0, "total_spend": Decimal(0)} for _, _, label in _BUCKETS_PO}
        for po in pos:
            total = _parse_d(po.get("total"))
            label = _BUCKETS_PO[-1][2]
            for lo, hi, lbl in _BUCKETS_PO:
                if hi is None or total <= hi:
                    label = lbl
                    break
            data_pr_po[label]["po_count"] += 1
            data_pr_po[label]["total_spend"] += total
        lines = [
            {
                "price_range": label,
                "po_count": data_pr_po[label]["po_count"],
                "total_spend": float(data_pr_po[label]["total_spend"]),
            }
            for _, _, label in _BUCKETS_PO
        ]

    else:  # period
        def _period_key(ts: str) -> str:
            try:
                d = date.fromisoformat(ts[:10])
            except ValueError:
                return "unknown"
            if period == "daily":
                return d.isoformat()
            elif period == "weekly":
                return (d - timedelta(days=d.weekday())).isoformat()
            else:
                return d.strftime("%Y-%m")

        data = defaultdict(lambda: {"po_count": 0, "total_spend": Decimal(0)})
        for po in pos:
            ts = po.get("date") or po.get("created_at") or ""
            key = _period_key(ts)
            data[key]["po_count"] += 1
            data[key]["total_spend"] += _parse_d(po.get("total"))
        lines = [
            {
                "period": key,
                "po_count": data[key]["po_count"],
                "total_spend": float(data[key]["total_spend"]),
            }
            for key in sorted(data)
        ]

    total_spend = sum(l["total_spend"] for l in lines)

    return {
        "group_by": group_by,
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "lines": lines,
        "total_spend": total_spend,
    }


# ---------------------------------------------------------------------------
# Expiring items report
# ---------------------------------------------------------------------------

@router.get("/expiring")
async def expiring_items(
    days: int = 30,
    company_id: uuid.UUID = Depends(get_current_company_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Items expiring within `days` days, sorted by soonest first."""
    rows = (
        await session.execute(
            select(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_type == "item",
            )
        )
    ).scalars().all()

    today = date.today()
    cutoff = today + timedelta(days=days)
    lines = []

    for row in rows:
        state = row.state
        exp_str = state.get("expires_at")
        if not exp_str:
            continue
        try:
            exp = date.fromisoformat(str(exp_str)[:10])
        except ValueError:
            continue
        days_remaining = (exp - today).days
        if days_remaining > days:
            continue
        lines.append({
            "entity_id": row.entity_id,
            "sku": state.get("sku"),
            "name": state.get("name"),
            "category": state.get("category"),
            "expires_at": exp.isoformat(),
            "days_remaining": days_remaining,
            "location_id": str(row.location_id) if row.location_id else None,
            "status": state.get("status"),
        })

    lines.sort(key=lambda x: x["days_remaining"])
    return {"as_of": today.isoformat(), "days_threshold": days, "count": len(lines), "lines": lines}
